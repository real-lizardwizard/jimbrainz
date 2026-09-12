"""
The saved library scan, disc numbers, and the track viewer's endpoint.

Real files with real tags throughout, as in test_library.py - the risk in all three is what
actually comes back off a file or out of the database, which is exactly what a mock would hide.
"""

import asyncio
import shutil
import sqlite3

import pytest
from mutagen.flac import FLAC

from src import library
from src.library import (SCAN_FORMAT, drain_cache_changes, forget_cached_album, scan_library,
                         seed_cache, snapshot_library)
from src.organizer import tag_values
from src.retag import execute_retag, plan_retag, read_current_tags
from src.store import JobStore

STREAMINFO = (
    b"fLaC" + b"\x80\x00\x00\x22"
    + b"\x10\x00\x10\x00\x00\x00\x00\x00\x00\x00\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)


def write_flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(STREAMINFO)
    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = str(value)
    audio.save()
    return path


def seed_album(root, artist="Tame Impala", folder="The Slow Rush (2020)", album="The Slow Rush",
               tracks=2):
    directory = root / artist / folder
    for n in range(1, tracks + 1):
        write_flac(directory / f"{n:02d} - Track {n}.flac", album=album, albumartist=artist,
                   artist=artist, title=f"Track {n}", tracknumber=str(n), date="2020")
    return directory


def run(coroutine):
    return asyncio.run(coroutine)


def boom(*_args, **_kwargs):
    raise AssertionError("this should not have touched the disk")


@pytest.fixture(autouse=True)
def fresh_cache():
    """Every module-level cache, emptied - including which root the route thinks it loaded."""
    from src.routes import library as route

    library.clear_scan_cache()
    route._cache_loaded_for = None
    yield
    library.clear_scan_cache()
    route._cache_loaded_for = None


@pytest.fixture
def store(tmp_path):
    job_store = JobStore(str(tmp_path / "state" / "jimbrainz.db"))
    job_store.init()
    return job_store


# ---------------------------------------------------------------- disc numbers, read

def two_disc_album(root, discnumber=lambda disc: str(disc)):
    """
    Two discs of two tracks, named so that ordering by number alone would interleave them:
    "01 - D1T1", "01 - D2T1", "02 - D1T2", "02 - D2T2".
    """
    directory = root / "Pink Floyd" / "The Wall (1979)"
    for disc in (1, 2):
        for n in (1, 2):
            write_flac(directory / f"{n:02d} - D{disc}T{n}.flac", album="The Wall",
                       albumartist="Pink Floyd", artist="Pink Floyd", title=f"D{disc}T{n}",
                       tracknumber=str(n), discnumber=discnumber(disc), date="1979")
    return directory


def test_a_multi_disc_album_runs_disc_by_disc(tmp_path):
    """Numbering restarts per disc, so ordering on the number alone deals the discs out alternately."""
    two_disc_album(tmp_path)
    [album] = scan_library(str(tmp_path))["albums"]

    assert [t["title"] for t in album["tracks"]] == ["D1T1", "D1T2", "D2T1", "D2T2"]
    assert [t["disc"] for t in album["tracks"]] == [1, 1, 2, 2]
    assert album["disc_count"] == 2


def test_a_disc_written_as_n_of_m_is_read_as_the_disc(tmp_path):
    two_disc_album(tmp_path, discnumber=lambda disc: f"{disc}/2")
    [album] = scan_library(str(tmp_path))["albums"]
    assert [t["disc"] for t in album["tracks"]] == [1, 1, 2, 2]


def test_an_album_with_no_disc_tags_claims_no_discs(tmp_path):
    """0, not a guessed 1 - the viewer only splits by disc when the files actually say so."""
    seed_album(tmp_path)
    [album] = scan_library(str(tmp_path))["albums"]
    assert album["disc_count"] == 0
    assert all(t["disc"] is None for t in album["tracks"])


# ---------------------------------------------------------------- the snapshot

def test_there_is_no_snapshot_before_anything_has_been_scanned(tmp_path):
    seed_album(tmp_path)
    assert snapshot_library(str(tmp_path)) is None


def test_a_snapshot_answers_without_touching_the_disk(tmp_path, monkeypatch):
    seed_album(tmp_path)
    scan_library(str(tmp_path))

    monkeypatch.setattr(library.os, "walk", boom)
    monkeypatch.setattr(library, "read_album_dir", boom)

    result = snapshot_library(str(tmp_path))
    assert result["stale"] is True
    assert result["album_count"] == 1
    assert result["albums"][0]["album"] == "The Slow Rush"


def test_a_real_scan_is_never_marked_stale(tmp_path):
    seed_album(tmp_path)
    assert scan_library(str(tmp_path))["stale"] is False


def test_a_snapshot_holds_only_its_own_librarys_albums(tmp_path):
    seed_album(tmp_path / "one")
    seed_album(tmp_path / "two", artist="Boards of Canada", folder="Geogaddi (2002)", album="Geogaddi")
    scan_library(str(tmp_path / "one"))

    assert snapshot_library(str(tmp_path / "two")) is None
    assert snapshot_library(str(tmp_path / "one"))["album_count"] == 1


def test_decorating_a_response_leaves_the_cache_alone(tmp_path):
    """
    The scan used to hand out the cached dicts themselves, and _mark_multi_edition labels an
    unlabelled edition "Standard" in place. So once its sibling was deleted, the survivor - whose
    own folder hadn't changed, so it came straight from the cache - went on calling itself
    Standard with nothing to be standard beside.
    """
    seed_album(tmp_path)
    deluxe = seed_album(tmp_path, folder="The Slow Rush (2020) [Deluxe]", tracks=3)

    first = scan_library(str(tmp_path))["albums"]
    assert sorted(a["edition"] for a in first) == ["Deluxe", "Standard"]

    shutil.rmtree(deluxe)
    [survivor] = scan_library(str(tmp_path))["albums"]

    assert survivor["edition"] == ""
    assert survivor["edition_count"] == 1


# ---------------------------------------------------------------- what gets saved

def test_the_first_scan_saves_every_album_and_an_unchanged_rescan_saves_none(tmp_path):
    seed_album(tmp_path)
    seed_album(tmp_path, artist="Boards of Canada", folder="Geogaddi (2002)", album="Geogaddi")

    scan_library(str(tmp_path))
    upserts, removals = drain_cache_changes()
    assert len(upserts) == 2 and removals == []

    scan_library(str(tmp_path))
    assert drain_cache_changes() == ([], [])


def test_forgetting_an_album_removes_its_saved_copy_too(tmp_path):
    """Otherwise a restart would load the pre-retag tags straight back against a matching mtime."""
    directory = seed_album(tmp_path)
    scan_library(str(tmp_path))
    drain_cache_changes()

    forget_cached_album(str(directory))

    assert drain_cache_changes() == ([], [str(directory)])


def test_a_folder_that_disappears_is_removed_from_the_saved_copy(tmp_path):
    directory = seed_album(tmp_path)
    scan_library(str(tmp_path))
    drain_cache_changes()

    shutil.rmtree(directory)
    scan_library(str(tmp_path))

    assert drain_cache_changes() == ([], [str(directory)])


def test_a_forced_rescan_resaves_everything(tmp_path):
    seed_album(tmp_path)
    scan_library(str(tmp_path))
    drain_cache_changes()

    scan_library(str(tmp_path), force=True)
    upserts, removals = drain_cache_changes()

    assert len(upserts) == 1
    #? re-read, so it is saved again - not deleted
    assert removals == []


def test_the_saved_scan_survives_a_restart(tmp_path, monkeypatch, store):
    """The point of the whole exercise: after a restart, no tag is re-read for an unchanged folder."""
    root = tmp_path / "library"
    seed_album(root)
    scan_library(str(root))
    upserts, removals = drain_cache_changes()
    assert run(store.save_library_cache(str(root), upserts, removals, SCAN_FORMAT))

    library.clear_scan_cache()  # the restart

    assert seed_cache(run(store.load_library_cache(str(root), SCAN_FORMAT))) == 1

    monkeypatch.setattr(library, "read_album_dir", boom)
    result = scan_library(str(root))
    assert result["album_count"] == 1
    assert result["cached"] == 1


def test_seeding_never_overwrites_what_this_process_already_read(tmp_path):
    directory = seed_album(tmp_path)
    scan_library(str(tmp_path))

    seed_cache([(str(directory), 0.0, {"album": "a stale copy"})])

    assert library._album_cache[str(directory)][1]["album"] == "The Slow Rush"


def test_saved_entries_from_another_scan_format_are_not_loaded(tmp_path, store):
    """An old row would otherwise be served forever for any folder nobody touched since the upgrade."""
    root = tmp_path / "library"
    seed_album(root)
    scan_library(str(root))
    upserts, removals = drain_cache_changes()
    run(store.save_library_cache(str(root), upserts, removals, SCAN_FORMAT - 1))

    assert run(store.load_library_cache(str(root), SCAN_FORMAT)) == []


def test_a_saved_row_that_will_not_parse_is_skipped_rather_than_fatal(tmp_path, store):
    root = tmp_path / "library"
    seed_album(root)
    scan_library(str(root))
    upserts, removals = drain_cache_changes()
    run(store.save_library_cache(str(root), upserts, removals, SCAN_FORMAT))

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO library_cache (path, root, mtime, format, album) VALUES (?, ?, ?, ?, ?)",
            (str(root / "broken"), str(root), 1.0, SCAN_FORMAT, "{not json"),
        )

    assert len(run(store.load_library_cache(str(root), SCAN_FORMAT))) == 1


def test_an_unavailable_store_saves_and_loads_nothing_quietly(tmp_path):
    broken = JobStore(str(tmp_path / "state" / "db"))  # never init()ed, so unavailable
    assert run(broken.save_library_cache(str(tmp_path), [], [], SCAN_FORMAT)) is False
    assert run(broken.load_library_cache(str(tmp_path), SCAN_FORMAT)) == []
    assert run(broken.library_scan_info(str(tmp_path))) is None


# ---------------------------------------------------------------- the route

def make_client(root, monkeypatch, store=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.config import Config
    from src.routes import library as library_route

    monkeypatch.setattr(Config, "LIBRARY_PATH", str(root))
    app = FastAPI()
    app.state.store = store
    app.include_router(library_route.router, prefix="/jimbrainz/library")
    return TestClient(app)


def test_asking_for_a_snapshot_with_nothing_cached_gets_a_real_scan(tmp_path, monkeypatch):
    """So the interface never draws an empty library that is merely unscanned."""
    seed_album(tmp_path)
    client = make_client(tmp_path, monkeypatch)

    body = client.get("/jimbrainz/library/albums", params={"snapshot": "true"}).json()
    assert body["stale"] is False
    assert body["album_count"] == 1


def test_a_snapshot_is_the_last_scan_not_the_current_disk(tmp_path, monkeypatch, store):
    directory = seed_album(tmp_path)
    client = make_client(tmp_path, monkeypatch, store)
    client.get("/jimbrainz/library/albums")

    shutil.rmtree(directory)

    snapshot = client.get("/jimbrainz/library/albums", params={"snapshot": "true"}).json()
    assert snapshot["stale"] is True
    assert snapshot["album_count"] == 1
    #? says when that was, so the interface can say how old it is
    assert snapshot["scanned_at"]

    assert client.get("/jimbrainz/library/albums").json()["album_count"] == 0


def test_after_a_restart_the_snapshot_comes_from_the_database(tmp_path, monkeypatch, store):
    from src.routes import library as route

    seed_album(tmp_path)
    make_client(tmp_path, monkeypatch, store).get("/jimbrainz/library/albums")

    library.clear_scan_cache()
    route._cache_loaded_for = None  # the restart

    body = make_client(tmp_path, monkeypatch, store).get(
        "/jimbrainz/library/albums", params={"snapshot": "true"}
    ).json()

    assert body["stale"] is True
    assert body["album_count"] == 1
    #? decorated like any other response - the queue is derived, never stored
    assert "no_art" in body["albums"][0]["issues"]


def test_a_snapshot_never_prunes_albums_it_has_not_seen(tmp_path, monkeypatch, store):
    """
    An album filed since the snapshot was taken is absent from it. Pruning on a snapshot would
    delete that album's brand-new import row as an orphan before anyone had looked at it.
    """
    seed_album(tmp_path)
    client = make_client(tmp_path, monkeypatch, store)
    client.get("/jimbrainz/library/albums")

    just_filed = "Someone/Just Filed (2024)"
    run(store.record_albums_seen([{"path": just_filed, "artist": "Someone", "album": "Just Filed"}],
                                 source="import"))

    client.get("/jimbrainz/library/albums", params={"snapshot": "true"})
    assert just_filed in run(store.album_reviews())

    #? and a real scan, which has looked, does prune it - so it is the snapshot guard doing this
    client.get("/jimbrainz/library/albums")
    assert just_filed not in run(store.album_reviews())


def test_deleting_an_album_drops_it_from_the_saved_scan(tmp_path, monkeypatch, store):
    seed_album(tmp_path)
    client = make_client(tmp_path, monkeypatch, store)
    client.get("/jimbrainz/library/albums")

    response = client.post("/jimbrainz/library/delete",
                           json={"album_path": "Tame Impala/The Slow Rush (2020)"})
    assert response.status_code == 200

    assert run(store.load_library_cache(str(tmp_path), SCAN_FORMAT)) == []


# ---------------------------------------------------------------- the track viewer

def test_the_track_viewer_reads_every_tag_live(tmp_path, monkeypatch):
    directory = tmp_path / "Pink Floyd" / "The Dark Side of the Moon (1973)"
    write_flac(directory / "01 - Speak to Me.flac", title="Speak to Me", artist="Pink Floyd",
               tracknumber="1", discnumber="1", genre="Progressive Rock", composer="Nick Mason",
               custom_thing="kept anyway")

    client = make_client(tmp_path, monkeypatch)
    response = client.get("/jimbrainz/library/tracks",
                          params={"album": "Pink Floyd/The Dark Side of the Moon (1973)"})

    assert response.status_code == 200
    [track] = response.json()["files"]

    assert track["tags"]["genre"] == "Progressive Rock"
    assert track["tags"]["composer"] == "Nick Mason"
    assert track["disc"] == 1 and track["position"] == 1
    assert track["format"] == "flac"
    #? a tag nobody named still reaches the viewer, under the file's own key for it
    assert ["custom_thing", "kept anyway"] in [[k.lower(), v] for k, v in track["raw"]]


def test_the_track_viewer_is_in_running_order(tmp_path, monkeypatch):
    two_disc_album(tmp_path)
    client = make_client(tmp_path, monkeypatch)

    files = client.get("/jimbrainz/library/tracks",
                       params={"album": "Pink Floyd/The Wall (1979)"}).json()["files"]

    assert [f["tags"]["title"] for f in files] == ["D1T1", "D1T2", "D2T1", "D2T2"]


@pytest.mark.parametrize("attempt", [
    "../../../../etc",
    "Tame Impala/../../../../etc",
    "/etc",
    "",
])
def test_the_track_viewer_refuses_to_read_outside_the_library(tmp_path, monkeypatch, attempt):
    """The second endpoint that turns user input into a filesystem read, so it copies /art's guard."""
    seed_album(tmp_path)
    client = make_client(tmp_path, monkeypatch)
    assert client.get("/jimbrainz/library/tracks", params={"album": attempt}).status_code == 404


# ---------------------------------------------------------------- disc numbers, written

TWO_DISCS = {
    "artist": "Pink Floyd",
    "album": "The Wall",
    "year": "1979",
    "release_mbid": "mbid-the-wall",
    "tracks": [
        {"position": 1, "title": "In the Flesh", "disc": 1, "disc_position": 1},
        {"position": 2, "title": "The Thin Ice", "disc": 1, "disc_position": 2},
        {"position": 3, "title": "Hey You", "disc": 2, "disc_position": 1},
        {"position": 4, "title": "Nobody Home", "disc": 2, "disc_position": 2},
    ],
}


def test_a_multi_disc_release_is_tagged_per_disc():
    values = tag_values(TWO_DISCS, TWO_DISCS["tracks"][2])
    assert values["tracknumber"] == "1"
    assert values["discnumber"] == "2"


def test_a_single_disc_release_writes_no_disc_number():
    """Or every album in the library would gain a discnumber change and never read 'nothing to change'."""
    release = {**TWO_DISCS, "tracks": [
        {"position": 1, "title": "Speak to Me", "disc": 1, "disc_position": 1},
        {"position": 2, "title": "Breathe", "disc": 1, "disc_position": 2},
    ]}
    values = tag_values(release, release["tracks"][1])
    assert values["tracknumber"] == "2"
    assert "discnumber" not in values


def test_a_tracklist_without_disc_fields_is_numbered_as_it_always_was():
    """A job stored before discs existed must still organize exactly as it would have."""
    release = {**TWO_DISCS, "tracks": [{"position": n, "title": f"T{n}"} for n in (1, 2, 3)]}
    values = tag_values(release, release["tracks"][2])
    assert values["tracknumber"] == "3"
    assert "discnumber" not in values


def test_retagging_a_two_disc_album_writes_the_discs_and_agrees_with_its_preview(tmp_path):
    directory = tmp_path / "Pink Floyd" / "The Wall (1979)"
    #? tagged the old way: one running sequence, no disc
    for track in TWO_DISCS["tracks"]:
        write_flac(directory / f"{track['position']:02d} - {track['title']}.flac",
                   album="The Wall", albumartist="Pink Floyd", artist="Pink Floyd",
                   title=track["title"], tracknumber=str(track["position"]), date="1979")

    plan = plan_retag("Pink Floyd/The Wall (1979)", TWO_DISCS, str(tmp_path))
    hey_you = next(f for f in plan["files"] if f["track_title"] == "Hey You")
    assert hey_you["changes"]["tracknumber"] == {"from": "3", "to": "1"}
    assert hey_you["changes"]["discnumber"] == {"from": "", "to": "2"}

    results = execute_retag(plan, TWO_DISCS, "apply")
    assert results["failed"] == 0

    written = read_current_tags(directory / "03 - Hey You.flac")
    assert written["tracknumber"] == "1"
    assert written["discnumber"] == "2"

    #? the write did exactly what the preview said, so previewing again finds nothing to do
    assert plan_retag("Pink Floyd/The Wall (1979)", TWO_DISCS, str(tmp_path))["empty"]


def test_the_download_request_keeps_the_disc_fields():
    """pydantic drops undeclared fields silently, which would lose discs between browser and organizer."""
    from src.routes.download import Track

    kept = Track(position=3, title="Hey You", disc=2, disc_position=1).model_dump()
    assert kept["disc"] == 2 and kept["disc_position"] == 1
