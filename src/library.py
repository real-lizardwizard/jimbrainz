"""
Reading what's already on disk.

The library view answers a different question from the search view: not "what exists" but
"what do I have, and which version of it". That second half is the whole point - the reason
for leaving Lidarr was that a chosen edition couldn't survive the round trip, and a library
that can't show you the deluxe next to the standard has the same blind spot.

Two rules shape everything here:

  Identity comes from tags, not folder names. Two folders are the same edition when they
  share a musicbrainz_albumid, however they happen to be named, so renaming a folder by hand
  doesn't split an album in two.

  A folder we can't identify is its own album, not a match. Libraries that predate jimbrainz
  have no MBIDs at all, and guessing that two untagged folders are "really" the same release
  would merge things the user deliberately keeps apart.

Scanning is per-directory and cached on the folder's mtime, because reading tags is the
expensive part and most of a library doesn't change between two visits to the page.
"""

import os
import re
import threading
import time
from pathlib import Path

from src.logger import logger
from src.matching import AUDIO_EXTENSIONS, file_extension

#? The shape of what read_album_dir() returns, as a number. BUMP IT whenever that dict gains,
#? loses or changes a field. The cache below is persisted to SQLite and outlives the process,
#? so an entry written by an older version would otherwise be served forever - the folder's
#? mtime hasn't moved, so nothing would ever re-read it - and the new field would simply be
#? missing from every album nobody has touched since the upgrade.
#?   1  the original shape
#?   2  tracks carry `disc`, albums carry `disc_count`
SCAN_FORMAT = 2

#? path -> (mtime, album dict). Reading tags costs milliseconds per file and a real library
#? is thousands of files, so a rescan re-reads only the folders that actually changed. The
#? walk itself is cheap; opening files is not.
#?
#? The dicts in here are never handed out directly - every scan returns shallow copies. The
#? response is decorated in place further down the line (_mark_multi_edition, attach_issues),
#? and decorating the cached dict itself is how an album used to go on calling itself
#? "Standard" after its only sibling was deleted.
_album_cache: dict[str, tuple[float, dict]] = {}

#? Cache keys changed since the route last persisted them: read fresh, or dropped. Kept here
#? rather than diffed by the caller because only the scan knows which folders it actually
#? re-read - comparing two whole caches would cost more than the save it decides on.
_dirty: set[str] = set()
_removed: set[str] = set()

#? Serialises scans. Two at once would both re-read every changed folder, and the tidy-up
#? loop at the end of one iterates the cache while the other mutates it. Snapshots do NOT take
#? it: they exist to answer while a slow scan is running, and dict.copy() is atomic.
_scan_lock = threading.Lock()

#? Conventional cover filenames, in preference order. These are what the organizer carries
#? across as companion files and what every other music tool writes.
COVER_BASENAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall", "thumb")

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}

#? Filenames that say outright "this is not the front cover". Anchored and optionally
#? numbered ("disc", "disc2", "cd 1"), NOT substring-matched - "Discovery.jpg" is a real
#? cover and a substring check would throw it away.
#? Deliberately NOT including "scan": a lone "scan001.jpg" is usually the front, because
#? that's the sheet people scan first. Everything listed here names a specific part of the
#? packaging that is definitively not the front.
NON_COVER_PATTERN = re.compile(
    r"(disc|disk|cd|dvd|vinyl|back|rear|inlay|booklet|matrix|label|media|tray|obi|insert|"
    r"inside|sleeve\s*back)[\s._-]*\d*"
)

MIME_BY_EXTENSION = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
}


def _looks_like_a_non_cover(stem: str) -> bool:
    """
    True for images that are demonstrably NOT the front cover.

    Matched as whole names rather than substrings, optionally numbered, so "disc2.jpg" is
    caught while "Discovery.jpg" and "Cdiscover.png" are left alone - a substring check here
    would reject real covers.
    """
    return bool(NON_COVER_PATTERN.fullmatch(stem.strip().lower()))


def find_cover_file(entries: list[Path]) -> Path | None:
    """
    A cover image sitting beside the tracks.

    Preferred over embedded art because it's free to serve - no audio file to open and no
    picture block to decode - and because it's what the organizer already copies across when
    it files a download.
    """
    images = [e for e in entries if file_extension(e.name) in IMAGE_EXTENSIONS]
    if not images:
        return None

    for wanted in COVER_BASENAMES:
        for image in images:
            if image.stem.lower() == wanted:
                return image

    #? No conventionally-named file. A folder holding exactly one image is almost certainly
    #? holding its cover - unless that image is plainly a disc or a back scan, which is how
    #? albums ended up illustrated with a picture of a CD.
    if len(images) == 1 and not _looks_like_a_non_cover(images[0].stem):
        return images[0]

    return None


#? Embedded pictures carry a type, and a file very often holds several. Rips from EAC and
#? dBpoweramp routinely embed the front cover AND a scan of the disc, in no guaranteed order,
#? so taking whichever came first showed people a picture of a CD instead of their album.
#? Lower is better; anything unlisted sits in the middle.
PICTURE_TYPE_PREFERENCE = {
    3: 0,   # COVER_FRONT  - the album art, what we're actually after
    0: 1,   # OTHER        - untyped. Very common, and usually the front in practice.
    5: 3,   # LEAFLET_PAGE
    4: 4,   # COVER_BACK
    6: 5,   # MEDIA        - the label side of the disc. Legible, but not the cover.
    1: 9,   # FILE_ICON    - 32x32 PNGs; never what anyone wants to look at
    2: 9,   # OTHER_FILE_ICON
}
UNRANKED_PICTURE_PREFERENCE = 2


def _best_picture(pictures: list) -> object | None:
    """The most cover-like picture in the list, by its declared type."""
    if not pictures:
        return None

    return min(
        pictures,
        key=lambda p: PICTURE_TYPE_PREFERENCE.get(getattr(p, "type", 0), UNRANKED_PICTURE_PREFERENCE),
    )


def read_embedded_art(path: Path) -> tuple[bytes, str] | None:
    """
    Cover art stored inside the audio file itself.

    Every container does this differently, and mutagen's `easy` interface deliberately
    doesn't expose any of it, so this opens the file again without it. Worth the second open:
    a lot of libraries have no separate cover file and would otherwise show nothing.

    Picks by picture TYPE rather than by order - see PICTURE_TYPE_PREFERENCE.
    """
    import mutagen

    try:
        audio = mutagen.File(str(path))
    except Exception:
        return None

    if audio is None:
        return None

    #? FLAC and Ogg: a list of picture blocks
    picture = _best_picture(list(getattr(audio, "pictures", None) or []))
    if picture is not None:
        return bytes(picture.data), (picture.mime or "image/jpeg")

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    #? MP3: APIC frames, keyed as APIC:description so an exact lookup misses them - and a
    #? file with both a cover and a disc scan has two of them
    try:
        frames = [tags[key] for key in tags.keys() if key.startswith("APIC")]
        frame = _best_picture(frames)
        if frame is not None:
            return bytes(frame.data), (getattr(frame, "mime", None) or "image/jpeg")
    except Exception:
        pass

    #? MP4/M4A: a 'covr' atom, whose format is a flag on the value rather than a mime type
    try:
        covers = tags.get("covr")
        if covers:
            cover = covers[0]
            fmt = getattr(cover, "imageformat", None)
            mime = "image/png" if fmt == 14 else "image/jpeg"
            return bytes(cover), mime
    except Exception:
        pass

    return None


def load_album_art(directory: Path) -> tuple[bytes, str] | None:
    """
    The album's cover, from wherever it actually lives. Cheapest source first.

    Used by the art endpoint. Kept separate from the scan so a scan never has to hold image
    bytes for the whole library in memory at once.
    """
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError:
        return None

    cover = find_cover_file(entries)
    if cover is not None:
        try:
            mime = MIME_BY_EXTENSION.get(file_extension(cover.name), "image/jpeg")
            return cover.read_bytes(), mime
        except OSError:
            pass

    for entry in entries:
        if file_extension(entry.name) in AUDIO_EXTENSIONS:
            return read_embedded_art(entry)

    return None


def _first(audio, key: str) -> str:
    """mutagen's easy interface returns lists. Take the first value, or ''."""
    try:
        values = audio.get(key) or []
    except Exception:
        return ""
    return str(values[0]).strip() if values else ""


def _track_number(raw: str) -> int | None:
    """Track numbers arrive as '4', '04' or '4/12' depending on who tagged the file."""
    if not raw:
        return None
    head = raw.split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def read_track(path: Path) -> dict | None:
    """One audio file's tags, or None if it isn't readable audio."""
    import mutagen

    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as e:
        logger.debug(f"could not read {path.name}: {e}")
        return None

    if audio is None:
        return None

    try:
        length = float(getattr(audio.info, "length", 0) or 0)
    except Exception:
        length = 0.0

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    #? Recorded separately because the fallback below is invisible afterwards: an untitled
    #? file shows its filename stem, which for anything ripped by a normal tool is very close
    #? to what a real title looks like. Without this the interface cannot tell "titled" from
    #? "named after the file it happens to live in".
    tagged_title = _first(audio, "title")

    return {
        "filename": path.name,
        "title": tagged_title or path.stem,
        "has_title_tag": bool(tagged_title),
        "position": _track_number(_first(audio, "tracknumber")),
        #? None for the overwhelming majority of files, which carry no disc tag - and that is
        #? read as disc 1 for ordering rather than as a problem. It matters only for multi-disc
        #? sets, where track numbers restart on every disc and ordering on them alone deals
        #? the two discs out alternately.
        "disc": _track_number(_first(audio, "discnumber")),
        "length": round(length, 1),
        "size": size,
        "format": file_extension(path.name),
        #? kept per-track because a folder can disagree with itself - a mis-tagged file is
        #? exactly the kind of thing this view should make visible rather than average away
        "artist": _first(audio, "artist"),
        "album": _first(audio, "album"),
        "albumartist": _first(audio, "albumartist"),
        "date": _first(audio, "date"),
        "originaldate": _first(audio, "originaldate"),
        "release_mbid": _first(audio, "musicbrainz_albumid"),
    }


def track_order(track: dict) -> tuple:
    """
    Running order: disc, then track number, then filename.

    Disc first because a multi-disc set restarts its numbering on every disc - sorting on the
    number alone interleaves them, 1, 1, 2, 2, 3, 3. An untagged disc counts as disc 1, so the
    ordinary single-disc album sorts exactly as it always has. Unnumbered tracks go last
    within their disc rather than first as a 0 would.
    """
    position = track.get("position")
    return (track.get("disc") or 1, position is None, position or 0, track.get("filename") or "")


def _commonest(values: list[str], fallback: str = "") -> str:
    """The most frequent non-empty value. Ties break toward the first seen, which is fine."""
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return fallback
    return max(counts, key=lambda k: counts[k])


def edition_from_dirname(name: str) -> str:
    """
    Pull the `[...]` suffix back out of a folder the organizer created.

    build_album_dirname() writes the edition there, so for anything jimbrainz filed this is
    the label the user already sees on disk, and reusing it keeps the UI and the filesystem
    telling the same story. Folders from elsewhere simply have no suffix.

    Public because metadata_health.py needs the folder's *own* edition to work out whether a
    folder still matches its tags - `album["edition"]` is not a substitute, since the scan
    rewrites it to "Standard" further down.
    """
    if name.endswith("]") and "[" in name:
        return name[name.rindex("[") + 1:-1].strip()
    return ""


def read_album_dir(directory: Path, library_root: Path) -> dict | None:
    """Everything in one folder, treated as a single album. None if it holds no audio."""
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError as e:
        logger.warning(f"could not list {directory}: {e}")
        return None

    tracks = []
    for entry in entries:
        if file_extension(entry.name) not in AUDIO_EXTENSIONS:
            continue
        track = read_track(entry)
        if track:
            tracks.append(track)

    if not tracks:
        return None

    tracks.sort(key=track_order)

    #? Recorded during the scan so the interface knows whether asking for art is worth a
    #? request at all. The embedded check costs one extra file open per album, which is
    #? cheap beside the per-track opens above and is cached with the rest of the album.
    cover_file = find_cover_file(entries)
    first_audio = next((e for e in entries if file_extension(e.name) in AUDIO_EXTENSIONS), None)

    if cover_file is not None:
        art = "file"
        art_source = cover_file
    else:
        art = "embedded" if first_audio and read_embedded_art(first_audio) else ""
        art_source = first_audio if art else None

    #? When the art last changed, used by the interface to bust its own cache.
    #?
    #? The ART's mtime, deliberately, not the album's. Replacing cover.jpg in place does not
    #? touch the DIRECTORY's mtime - only adding, removing or renaming entries does - so
    #? `modified_at` sits perfectly still through exactly the operation that needs to be
    #? noticed. The file itself is the only thing that always moves.
    try:
        art_mtime = round(art_source.stat().st_mtime) if art_source is not None else 0
    except OSError:
        art_mtime = 0

    #? the album artist is what groups a library; falling back to the track artist keeps
    #? compilations and badly-tagged folders visible instead of dropping them
    artist = (_commonest([t["albumartist"] for t in tracks])
              or _commonest([t["artist"] for t in tracks])
              or directory.parent.name
              or "Unknown Artist")

    album = _commonest([t["album"] for t in tracks]) or directory.name
    date = _commonest([t["date"] for t in tracks])
    original_date = _commonest([t["originaldate"] for t in tracks])
    release_mbid = _commonest([t["release_mbid"] for t in tracks])

    try:
        relative = str(directory.relative_to(library_root))
    except ValueError:
        relative = str(directory)

    return {
        #? identity first, path second. Two folders sharing an MBID are one edition even if
        #? someone renamed one of them; a folder without an MBID can only be itself.
        "key": release_mbid or f"path:{relative}",
        "artist": artist,
        "album": album,
        "year": date[:4] if date else "",
        #? empty for anything not tagged with one, which is most libraries
        "original_year": original_date[:4] if original_date else "",
        "edition": edition_from_dirname(directory.name),
        "release_mbid": release_mbid,
        #? '' means no art on disk. The interface falls back to the Cover Art Archive when
        #? there's a release MBID, exactly as the search view already does.
        "art": art,
        #? changes whenever the cover does, which is what lets the interface ask for the new
        #? one instead of being handed the cached old one
        "art_mtime": art_mtime,
        "path": relative,
        "track_count": len(tracks),
        #? distinct disc numbers the files are TAGGED with, so 0 for an untagged album rather
        #? than a guessed 1. Above 1 is a multi-disc set, which the viewer splits by disc.
        "disc_count": len({t["disc"] for t in tracks if t["disc"]}),
        "total_size": sum(t["size"] for t in tracks),
        "duration": round(sum(t["length"] for t in tracks), 1),
        "formats": sorted({t["format"] for t in tracks if t["format"]}),
        #? rolled up here rather than left for the caller to count, so it survives in the
        #? per-folder cache alongside everything else the scan worked out
        "untitled_tracks": sum(1 for t in tracks if not t["has_title_tag"]),
        "tracks": tracks,
        "modified_at": directory.stat().st_mtime if directory.exists() else 0,
        #? surfaced rather than hidden: a folder whose files disagree about the album name is
        #? usually a real tagging problem the user wants to know about
        "mixed_tags": len({t["album"] for t in tracks if t["album"]}) > 1,
    }


def clear_scan_cache() -> None:
    """
    Drop the whole scan cache.

    Used when LIBRARY_PATH itself changes. Every entry is keyed on a directory under the OLD
    root and validated against that folder's mtime, so after a root change the cache is not
    merely stale - its keys refer to paths that are no longer part of the library at all.

    Only the in-memory copy. Persisted rows are keyed on their root as well as their path, so
    the old root's rows simply stop being loaded - and come back if the path is changed back.
    """
    _album_cache.clear()
    _dirty.clear()
    _removed.clear()


def forget_cached_album(directory: str) -> None:
    """
    Drop a folder from the scan cache so the next scan re-reads it.

    Necessary because the cache keys on the DIRECTORY's mtime, and rewriting a file's tags
    does not change that - only adding, removing or renaming entries does. Measured, not
    assumed. Without this, retagging an album in place is invisible to the scanner and the
    interface keeps showing the old values until someone forces a full rescan, which reads
    exactly like the edit silently failed.

    Recorded as removed, so the persisted copy goes too. Forgetting only the memory would be
    undone by the next restart, which would load the pre-edit tags straight back in from disk
    against a folder mtime that still matches them.
    """
    _album_cache.pop(directory, None)
    _dirty.discard(directory)
    _removed.add(directory)


def seed_cache(entries: list[tuple[str, float, dict]]) -> int:
    """
    Fill the in-memory cache from persisted entries. Returns how many were taken.

    Never overwrites an entry already in memory: anything there was read in this process and is
    at least as fresh as a row saved by an earlier one. Seeding is not a scan - nothing is
    marked dirty, since these rows are by definition what is already saved.
    """
    taken = 0
    for path, mtime, album in entries:
        if path not in _album_cache:
            _album_cache[path] = (mtime, album)
            taken += 1
    return taken


def drain_cache_changes() -> tuple[list[tuple[str, float, dict]], list[str]]:
    """
    What has changed since the last call: entries to save, and cache keys to delete.

    Called by the route after every scan and every forget, so what is persisted tracks what is
    in memory. Draining without the scan lock is deliberate - `list(a_set)` is atomic under the
    GIL, and a change landing between the copy and the clear is simply picked up next time.
    """
    dirty = list(_dirty)
    _dirty.difference_update(dirty)
    removed = list(_removed)
    _removed.difference_update(removed)

    upserts = []
    for path in dirty:
        entry = _album_cache.get(path)
        if entry is not None:
            upserts.append((path, entry[0], entry[1]))

    return upserts, removed


def _relative_to(path: str, root: Path) -> str | None:
    """The cache key as the scan's relative album path, or None if it isn't under this root."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return None


def _unreadable(library_root: str, problem: str) -> dict:
    return {"albums": [], "artists": [], "library_path": library_root, "problem": problem,
            "scanned_at": time.time(), "album_count": 0, "artist_count": 0,
            "scan_seconds": 0.0, "cached": 0, "stale": False}


def _assemble(albums: list[dict], library_root: str, **extra) -> dict:
    """The response shape shared by a real scan and a snapshot, so the two cannot drift."""
    _mark_multi_edition(albums)
    albums.sort(key=lambda a: (a["artist"].lower(), a["album"].lower(), a["edition"].lower()))

    return {
        "albums": albums,
        "artists": _summarize_artists(albums),
        "library_path": library_root,
        "problem": None,
        "album_count": len(albums),
        "artist_count": len({a["artist"] for a in albums}),
        **extra,
    }


def snapshot_library(library_root: str) -> dict | None:
    """
    The library as the cache last saw it, WITHOUT touching the disk. None if nothing is cached.

    This is what makes opening the library instant. A real scan still walks every folder and
    stats it, which on a network share or a spun-down array is the slow part - and after a
    restart, with the cache loaded back from SQLite, it is the ONLY part. So the interface asks
    for this first, draws it, and then asks for a real scan to catch up underneath it.

    `stale` is True on the result, always: this is a claim about the last time anyone looked,
    and the interface says as much rather than presenting it as the current state of the disk.
    """
    if not library_root:
        return None

    root = str(Path(library_root))
    prefix = root.rstrip(os.sep) + os.sep
    #? copied first: a scan may be running in another thread, and dict.copy() is atomic where
    #? iterating the live dict is not
    entries = [album for path, (_, album) in _album_cache.copy().items()
               if path == root or path.startswith(prefix)]

    if not entries:
        return None

    albums = [dict(album) for album in entries]
    return _assemble(albums, library_root, scanned_at=None, scan_seconds=0.0,
                     cached=len(albums), stale=True)


def scan_library(library_root: str, force: bool = False) -> dict:
    """
    Walk the library and return every album found, grouped for display.

    Any directory containing audio is an album, which handles both the organizer's
    `Artist/Album (Year) [Edition]` layout and whatever shape an existing library is in
    without needing to know the difference.
    """
    started = time.perf_counter()

    if not library_root:
        return _unreadable("", "LIBRARY_PATH is not set")

    root = Path(library_root)
    if not root.is_dir():
        return _unreadable(
            library_root, f"LIBRARY_PATH does not exist or is not a directory: {library_root}"
        )

    with _scan_lock:
        albums, reused = _walk(root, force)

    return _assemble(
        albums, library_root,
        scanned_at=time.time(),
        scan_seconds=round(time.perf_counter() - started, 3),
        cached=reused,
        stale=False,
    )


def _walk(root: Path, force: bool) -> tuple[list[dict], int]:
    """Every album under root, reusing cached folders whose mtime hasn't moved. Hold _scan_lock."""
    if force:
        #? everything goes, persisted rows included - a forced rescan is the escape hatch for
        #? not trusting the cache, so it must not quietly keep trusting the saved copy of it
        _removed.update(list(_album_cache))
        _album_cache.clear()
        _dirty.clear()

    albums = []
    reused = 0

    for current, subdirs, files in os.walk(root):
        subdirs.sort()
        if not any(file_extension(f) in AUDIO_EXTENSIONS for f in files):
            continue

        directory = Path(current)
        try:
            mtime = directory.stat().st_mtime
        except OSError:
            continue

        cached = _album_cache.get(current)
        if cached and cached[0] == mtime:
            albums.append(dict(cached[1]))
            reused += 1
            continue

        album = read_album_dir(directory, root)
        if album is None:
            continue

        _album_cache[current] = (mtime, album)
        _dirty.add(current)
        _removed.discard(current)
        albums.append(dict(album))

    #? drop cache entries for folders that no longer exist, so a long-running container
    #? doesn't hold a growing map of albums the user deleted months ago - and so a restart
    #? doesn't load them back from the saved copy either
    live = {a["path"] for a in albums}
    for path in [p for p in _album_cache if _relative_to(p, root) not in live]:
        _album_cache.pop(path, None)
        _dirty.discard(path)
        _removed.add(path)

    return albums, reused


def _mark_multi_edition(albums: list[dict]) -> None:
    """
    Flag albums the user holds more than one version of.

    The headline feature of this view. Grouping is on (artist, album) rather than MBID
    precisely because different editions have *different* MBIDs - that's what makes them
    different editions - so the id that separates them can't also be what gathers them.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for album in albums:
        groups.setdefault((album["artist"].lower(), album["album"].lower()), []).append(album)

    for group in groups.values():
        for album in group:
            album["edition_count"] = len(group)
            #? an unlabelled folder sitting beside a labelled one is the standard press, and
            #? saying so beats leaving a blank column next to "Deluxe edition"
            if len(group) > 1 and not album["edition"]:
                album["edition"] = "Standard"


def _summarize_artists(albums: list[dict]) -> list[dict]:
    """Artist-level rollup for the filter column."""
    by_artist: dict[str, dict] = {}

    for album in albums:
        entry = by_artist.setdefault(album["artist"], {
            "artist": album["artist"], "album_count": 0, "track_count": 0, "total_size": 0,
        })
        entry["album_count"] += 1
        entry["track_count"] += album["track_count"]
        entry["total_size"] += album["total_size"]

    return sorted(by_artist.values(), key=lambda a: a["artist"].lower())


def summarize_for_deletion(directory: Path) -> dict:
    """
    Exactly what removing this folder would take with it.

    Read separately from the scan so the confirmation is describing the folder as it is right
    now, not as it was when the library was last scanned - and so it can count the things the
    scan ignores. A folder holding files that aren't audio or artwork is worth saying out
    loud before it goes: it might be a rip log, or it might be the only copy of something.
    """
    audio = 0
    other: list[str] = []
    total = 0

    for entry in sorted(directory.rglob("*")):
        if not entry.is_file():
            continue

        try:
            total += entry.stat().st_size
        except OSError:
            pass

        extension = file_extension(entry.name)
        if extension in AUDIO_EXTENSIONS:
            audio += 1
        elif extension not in IMAGE_EXTENSIONS:
            other.append(entry.name)

    return {
        "audio_files": audio,
        "total_bytes": total,
        #? capped: the point is "there is other stuff in here", not a full manifest
        "other_files": other[:12],
        "other_file_count": len(other),
    }


def delete_album(library_root: str, album_path: str) -> dict:
    """
    Remove an album folder and everything in it. There is no undo.

    This is the only code in jimbrainz that deletes anything the user did not just download,
    so every guard is deliberate:

      - LIBRARY_PATH must be configured, and the path must be non-empty.
      - It must resolve INSIDE the library. `is_within` resolves both sides, so neither
        "../.." nor a symlink pointing elsewhere gets through.
      - It must not BE the library root. `rmtree` on that would take the whole collection.
      - It must contain audio. That is what makes it an album rather than an artist folder
        or something the user keeps in there, and it means a mistyped path deletes nothing.

    Returns what was removed so the interface can say so rather than just going quiet.
    """
    from src.organizer import is_within

    if not library_root or not album_path:
        return {"deleted": False, "problem": "no album given"}

    root = Path(library_root)
    directory = root / album_path

    if not is_within(directory, root) or directory.resolve() == root.resolve():
        logger.warning(f"refused to delete outside the library: {album_path!r}")
        return {"deleted": False, "problem": "that album is not inside the library"}

    if not directory.is_dir():
        return {"deleted": False, "problem": "that folder is not there any more"}

    summary = summarize_for_deletion(directory)

    #? Audio sitting DIRECTLY in this folder, not anywhere beneath it. That distinction is
    #? the whole guard: an artist folder contains plenty of audio further down, so a
    #? recursive check happily accepts "Tame Impala" and takes the entire discography with
    #? it. It is also how the scanner decides what an album is, so the two agree on what can
    #? be deleted.
    direct_audio = any(
        entry.is_file() and file_extension(entry.name) in AUDIO_EXTENSIONS
        for entry in directory.iterdir()
    )

    if not direct_audio:
        return {"deleted": False, "problem": "that folder holds no audio, so it isn't an album"}

    import shutil

    try:
        shutil.rmtree(directory)
    except OSError as e:
        logger.error(f"could not delete {directory}: {e}")
        return {"deleted": False, "problem": f"could not delete it: {e}"}

    forget_cached_album(str(directory))

    #? tidy the artist folder if that was their last album, but only with rmdir, which
    #? refuses a non-empty directory by construction - anything left is something we didn't
    #? put there and isn't ours to remove
    try:
        parent = directory.parent
        if parent != root and is_within(parent, root):
            parent.rmdir()
            logger.info(f"removed the now-empty {parent.name}")
    except OSError:
        pass

    logger.info(
        f"deleted {album_path} ({summary['audio_files']} track(s), "
        f"{summary['total_bytes'] // (1024 * 1024)} MB)",
        extra={"frontend": True},
    )

    return {"deleted": True, "problem": None, **summary}


# ---------------------------------------------------------------- the track viewer

#? The tags the viewer gives a proper label to, read through mutagen's easy interface so one
#? key means the same thing in FLAC, MP3 and M4A. It decides what gets NAMED, not what gets
#? shown: everything else a file carries still reaches the viewer through `raw` below.
#? `label` and `organization` are both here because Vorbis files use either and ID3 calls it
#? organization - the viewer shows whichever is present.
DETAIL_TAGS = (
    "title", "artist", "albumartist", "album", "tracknumber", "discnumber", "discsubtitle",
    "date", "originaldate", "genre", "composer", "conductor", "lyricist", "isrc", "label",
    "organization", "catalognumber", "barcode", "releasecountry", "media", "bpm", "copyright",
    "language", "comment", "musicbrainz_trackid", "musicbrainz_releasetrackid",
    "musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_artistid",
    "musicbrainz_albumartistid",
)

#? Raw keys that hold pictures or opaque blobs. Shown as nothing rather than as megabytes of
#? base64 or a Python repr - the cover has its own view, and a PRIV frame means nothing to anyone.
RAW_SKIPPED = ("APIC", "PRIV", "GEOB", "COVR", "METADATA_BLOCK_PICTURE", "MCDI", "RVA2", "PCNT")
RAW_VALUE_LIMIT = 500
RAW_TAG_LIMIT = 200


def _all_values(audio, key: str) -> str:
    """Every value for an easy key, joined - a genre or composer tag is often several."""
    try:
        values = audio.get(key) or []
    except Exception:
        return ""
    return "; ".join(str(v).strip() for v in values if str(v).strip())


def _plain(value) -> str:
    """
    One raw tag value as text, whatever container it came out of.

    ID3 hands back frames, MP4 hands back lists of tuples and bytes subclasses, Vorbis hands
    back lists of strings. The viewer wants a line of text for each, so this flattens rather
    than trying to preserve any of their structure.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8").strip()
        except UnicodeDecodeError:
            return ""
    #? MP4 track and disc numbers: (3, 12) means 3 of 12, (3, 0) means no total
    if isinstance(value, tuple) and value and all(isinstance(v, int) for v in value):
        return "/".join(str(v) for v in value if v)
    if isinstance(value, (list, tuple)):
        return "; ".join(part for part in (_plain(v) for v in value) if part)

    #? an ID3 frame. Text frames carry `.text`, URL frames `.url`, UFID its bytes in `.data`
    for attribute in ("text", "url", "data"):
        inner = getattr(value, attribute, None)
        if inner is not None:
            return _plain(inner if isinstance(inner, (str, bytes)) else list(inner))

    return str(value).strip()


def _raw_tags(path: Path) -> list[list[str]]:
    """Every tag in the file under its container's own name, pictures and blobs excepted."""
    import mutagen

    try:
        audio = mutagen.File(str(path))
        items = list(audio.tags.items()) if audio is not None and audio.tags is not None else []
    except Exception:
        return []

    pairs: list[list[str]] = []
    for key, value in items:
        name = str(key)
        if name.upper().startswith(RAW_SKIPPED):
            continue

        text = _plain(value)
        if text:
            pairs.append([name, text[:RAW_VALUE_LIMIT]])

        if len(pairs) >= RAW_TAG_LIMIT:
            break

    return pairs


def _info_number(info, name: str) -> int | None:
    value = getattr(info, name, None)
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def read_track_details(path: Path) -> dict | None:
    """
    Everything one file says about itself: named tags, audio properties, and every raw tag.

    For the track viewer, and deliberately NOT part of the scan. The scan's payload is the
    whole library in one response, and carrying thirty tags per track for thousands of tracks
    would make every visit to the tab pay for detail it only ever shows one album at a time.
    """
    import mutagen

    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as e:
        logger.debug(f"could not read {path.name}: {e}")
        return None

    if audio is None:
        return None

    info = getattr(audio, "info", None)

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    length = float(getattr(info, "length", 0) or 0)

    #? FLAC reports no bitrate of its own on older mutagen, and the figure every player shows
    #? for lossless is simply the file's size over its length anyway
    bitrate = _info_number(info, "bitrate")
    if not bitrate and length and size:
        bitrate = int(size * 8 / length)

    tags = {key: value for key in DETAIL_TAGS if (value := _all_values(audio, key))}

    return {
        "filename": path.name,
        "format": file_extension(path.name),
        "size": size,
        "length": round(length, 1),
        "bitrate": bitrate,
        "sample_rate": _info_number(info, "sample_rate"),
        #? absent for lossy formats, which have no fixed bit depth - None, never a made-up 16
        "bits_per_sample": _info_number(info, "bits_per_sample"),
        "channels": _info_number(info, "channels"),
        "codec": (getattr(info, "codec", None) or type(audio).__name__.removeprefix("Easy")),
        "position": _track_number(tags.get("tracknumber", "")),
        "disc": _track_number(tags.get("discnumber", "")),
        "tags": tags,
        "raw": _raw_tags(path),
    }


def read_album_details(directory: Path) -> list[dict]:
    """read_track_details() for every audio file in one folder, in running order."""
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError as e:
        logger.warning(f"could not list {directory}: {e}")
        return []

    files = [
        details for entry in entries
        if file_extension(entry.name) in AUDIO_EXTENSIONS
        and (details := read_track_details(entry)) is not None
    ]
    files.sort(key=track_order)
    return files
