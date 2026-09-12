"""
Applying a MusicBrainz release to an album that is already in the library.

This is the write half of the metadata manager: you pick which release an album on disk
actually is, and this rewrites its tags and re-files it under the right edition. It is the
answer to the thing that made Lidarr unusable here - not just choosing an edition at
download time, but being able to correct that choice afterwards.

It is deliberately built the same way as the organizer, for the same reason:

  plan_retag()     - works out every change. Reads, never writes. Fully testable.
  execute_retag()  - carries out exactly what the plan says, and nothing else.

That split is what makes the preview trustworthy. The interface shows the plan, and applying
runs *that* plan rather than recomputing what to do - so what you agreed to is what happens.
The desired tags come from organizer.tag_values(), shared with the download path, so a
preview cannot drift away from the write it is previewing.

Everything here is guarded on staying inside LIBRARY_PATH. This is the second piece of code
in jimbrainz that writes to the user's filesystem, and unlike the organizer it operates on
files the user already had rather than ones we just downloaded - so the cost of getting it
wrong is somebody's actual music collection.
"""

import shutil
from pathlib import Path

from src.editions import resolve_edition_label
from src.logger import logger
from src.library import find_cover_file
from src.matching import AUDIO_EXTENSIONS, file_extension, match_tracks_to_files
from src.api.coverart_endpoint import extension_for
from src.organizer import (build_album_dirname, is_within, read_album_mbid,
                           sanitize_filename, tag_values, write_tags)

#? Same vocabulary as the organizer's ORGANIZE_MODES, minus the copy/move distinction which
#? has no meaning here - a retag either happens or it doesn't.
RETAG_MODES = ("dry_run", "apply")


def read_current_tags(path: Path) -> dict:
    """The tags a file carries now, limited to the keys a retag would touch."""
    import mutagen

    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return {}

    if audio is None:
        return {}

    current = {}
    for key in ("album", "albumartist", "artist", "date", "originaldate", "title", "tracknumber",
                "discnumber", "musicbrainz_albumid", "musicbrainz_releasegroupid",
                "releasecountry", "media", "catalognumber"):
        try:
            values = audio.get(key) or []
        except Exception:
            continue
        if values:
            current[key] = str(values[0])

    return current


def plan_art(entries: list[Path], release: dict, want_art: bool) -> dict:
    """
    Whether this retag would put a cover in the folder, and if not, why not.

    Decided here rather than in the fetcher so the preview can say what will happen without
    a network call - downloading an image just to describe it would make previewing slow and
    would hit the Archive every time someone clicked a release.

    Never replaces existing art unless asked. Somebody's hand-picked sleeve is not ours to
    overwrite because MusicBrainz has one too; the same instinct as execute_plan refusing to
    clobber a file it didn't put there.
    """
    existing = find_cover_file(entries)

    if not want_art:
        return {"action": "", "reason": "", "existing": existing.name if existing else None}

    if not (release.get("release_mbid") or "").strip():
        return {"action": "", "reason": "this release has no MusicBrainz id to look art up by",
                "existing": existing.name if existing else None}

    if existing is not None:
        return {"action": "replace", "reason": "", "existing": existing.name}

    return {"action": "download", "reason": "", "existing": None}


def plan_cover_art(
    album_path: str,
    library_root: str,
    replace: bool = False,
    release_mbid: str | None = None,
) -> dict:
    """
    Whether a cover could be saved into this album, and which release to ask for. Writes nothing.

    Separate from plan_retag because the two answer different questions. That one asks "what
    would applying this release change", and the answer is tags, a folder name and maybe a
    cover - all or nothing. This asks only "can I put a cover in here", which is the far more
    common thing to want: an album whose tags are already right and whose only gap is the
    sleeve. Wanting the picture is not a reason to rewrite twelve files and rename a folder.

    The release id comes from the album's OWN tags. That is what makes this safe to run without
    choosing anything: it fetches art for the release the album already says it is, so there is
    no judgement being made and nothing to get wrong.
    """
    root = Path(library_root)
    directory = root / album_path

    if not album_path or not is_within(directory, root) or not directory.is_dir():
        return {"release_mbid": None, "existing": None, "action": "",
                "problem": "that album is not inside the library"}

    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError as e:
        return {"release_mbid": None, "existing": None, "action": "",
                "problem": f"could not read the album folder: {e}"}

    existing = find_cover_file(entries)
    existing_name = existing.name if existing else None

    if existing is not None and not replace:
        return {"release_mbid": None, "existing": existing_name, "action": "",
                "problem": f"this album already has {existing_name}"}

    #? Normally read from the files: the point of the plain "get art" path is that it needs no
    #? decision from anyone, so it fetches art for the release the album already says it is.
    #?
    #? An explicit id overrides that, and only the editor sends one - it is showing you that
    #? release's cover beside the current one at the time, so the choice has already been made
    #? deliberately and visibly. Fetching art for a release the album is NOT is a reasonable
    #? thing to want (the tags are wrong, or you're about to apply that release anyway); doing
    #? it silently would not be.
    release_mbid = (release_mbid or "").strip() or read_album_mbid(directory)

    if not release_mbid:
        return {"release_mbid": None, "existing": existing_name, "action": "",
                "problem": "this album isn't tagged with a MusicBrainz release, so there's "
                           "nothing to look a cover up by - match it to a release first"}

    return {
        "release_mbid": release_mbid,
        "existing": existing_name,
        "action": "replace" if existing is not None else "download",
        "problem": None,
    }


def save_cover_art(album_path: str, library_root: str, art: tuple[bytes, str]) -> dict:
    """
    Write a cover into an album folder, and touch nothing else.

    The whole point of this function is what it does NOT do: no tags, no rename, no companion
    files. It is the third writer to the user's filesystem and by far the narrowest, which is
    why it takes the bytes already downloaded rather than fetching them - the same split
    execute_retag uses, so this module stays testable without a network.

    Containment is re-checked here rather than trusted from the plan, for the reason the retag
    endpoint recomputes its plan: a path that arrived over the wire is not evidence.
    """
    root = Path(library_root)
    directory = root / album_path

    if not album_path or not is_within(directory, root) or not directory.is_dir():
        return {"written": None, "problem": "that album is not inside the library"}

    name = f"cover.{extension_for(art[1])}"

    try:
        (directory / name).write_bytes(art[0])
    except OSError as e:
        logger.error(f"could not write cover art into {directory}: {e}")
        return {"written": None, "problem": f"could not save the cover: {e}"}

    logger.info(f"saved {name} into {directory.name}", extra={"frontend": True})
    return {"written": name, "problem": None}


def plan_retag(album_path: str, release: dict, library_root: str, want_art: bool = False) -> dict:
    """
    Everything applying `release` to the album at `album_path` would change. Touches nothing.

    `album_path` is relative to LIBRARY_PATH, as the scanner reports it.

    Track matching reuses matching.match_tracks_to_files - the same greedy title matcher the
    download path uses to decide which file is which track. Reusing it means a retag maps
    files to a tracklist exactly the way the original download did, rather than inventing a
    second answer that could disagree.
    """
    root = Path(library_root)
    source = root / album_path

    problems: list[str] = []

    if not album_path or not is_within(source, root) or not source.is_dir():
        return _empty_plan(album_path, "that album is not inside the library")

    try:
        entries = sorted(p for p in source.iterdir() if p.is_file())
    except OSError as e:
        return _empty_plan(album_path, f"could not read the album folder: {e}")

    audio_files = [p for p in entries if file_extension(p.name) in AUDIO_EXTENSIONS]

    if not audio_files:
        return _empty_plan(album_path, "no audio files in that folder")

    #? match_tracks_to_files keys on "filename", the same shape the slskd path passes it
    mapping = match_tracks_to_files(
        release.get("tracks") or [],
        [{"filename": p.name} for p in audio_files],
    )
    track_by_filename = {
        entry["file"]["filename"]: entry["track"] for entry in mapping.values()
    }

    changes = []
    for path in audio_files:
        track = track_by_filename.get(path.name)
        desired = tag_values(release, track)
        current = read_current_tags(path)

        differing = {
            key: {"from": current.get(key, ""), "to": value}
            for key, value in desired.items()
            if current.get(key, "") != value
        }

        changes.append({
            "filename": path.name,
            #? surfaced so the interface can show which files the tracklist didn't reach.
            #? An unmatched file keeps its title and number rather than being given a
            #? made-up one - a wrong track number is worse than none, same as the organizer.
            "matched": track is not None,
            "track_title": (track or {}).get("title", ""),
            "track_position": (track or {}).get("position"),
            #? carried so execute_retag can rebuild the same track the plan was computed from -
            #? without them it would write the running number and no disc, and the write would
            #? disagree with the preview it is carrying out
            "track_disc": (track or {}).get("disc"),
            "track_disc_position": (track or {}).get("disc_position"),
            "changes": differing,
        })

    unmatched = [c["filename"] for c in changes if not c["matched"]]
    if unmatched:
        problems.append(
            f"{len(unmatched)} file(s) didn't match the tracklist and will keep their "
            f"existing title and track number"
        )

    expected = len(release.get("tracks") or [])
    if expected and expected != len(audio_files):
        problems.append(
            f"this release has {expected} track(s) but the folder has {len(audio_files)} - "
            f"check it's the right edition"
        )

    target, move_problem = _resolve_target(root, source, release)
    if move_problem:
        problems.append(move_problem)

    art = plan_art(entries, release, want_art)
    if art["reason"]:
        problems.append(art["reason"])

    return {
        "album_path": album_path,
        "source": str(source),
        "target": str(target) if target else None,
        "target_path": str(target.relative_to(root)) if target else None,
        "moves": bool(target and target != source),
        "edition_label": resolve_edition_label(release),
        "files": changes,
        "changed_file_count": sum(1 for c in changes if c["changes"]),
        "file_count": len(audio_files),
        "matched_tracks": len(mapping),
        "expected_tracks": expected,
        "art": art,
        "problems": problems,
        #? a plan with nothing to do is still a valid plan; the interface says so rather
        #? than offering an apply button that would be a no-op
        "empty": (
            not any(c["changes"] for c in changes)
            and not (target and target != source)
            and not art["action"]
        ),
    }


def _empty_plan(album_path: str, problem: str) -> dict:
    return {
        "album_path": album_path, "source": None, "target": None, "target_path": None,
        "moves": False, "edition_label": "", "files": [], "changed_file_count": 0,
        "file_count": 0, "matched_tracks": 0, "expected_tracks": 0,
        "art": {"action": "", "reason": "", "existing": None},
        "problems": [problem], "empty": True,
    }


def _resolve_target(root: Path, source: Path, release: dict) -> tuple[Path | None, str]:
    """
    Where the album should live once it carries this release, and why it might not move.

    Uses the organizer's own build_album_dirname, so an album corrected by hand ends up named
    exactly as one downloaded fresh would have been - the folder layout stays one convention
    rather than two.
    """
    artist = sanitize_filename(release.get("artist"), "Unknown Artist")
    target = root / artist / build_album_dirname(release)

    if target == source:
        return source, ""

    if not is_within(target, root):
        return None, "the new name would fall outside the library"

    if target.exists():
        #? never merge two albums together, even if they look like the same release. The
        #? user can rename by hand if that's really what they want.
        return None, f"'{target.name}' already exists, so the folder will be left where it is"

    return target, ""


def execute_retag(
    plan: dict,
    release: dict,
    mode: str = "dry_run",
    art: tuple[bytes, str] | None = None,
) -> dict:
    """
    Carry out a plan. `dry_run` reports what it would do and touches nothing.

    Tags are written before the folder moves, so a failure part-way leaves the album where
    the plan said it was rather than half-moved somewhere the interface isn't looking.

    `art` is passed in already downloaded rather than fetched here, deliberately: this module
    writes to the user's filesystem and nothing else, and giving it a network dependency
    would make it untestable without one. The caller does the fetching.
    """
    results = {
        "mode": mode,
        "dry_run": mode != "apply",
        "tagged": 0,
        "failed": 0,
        "moved_to": None,
        "art_written": None,
        "problems": list(plan.get("problems") or []),
    }

    if plan.get("source") is None:
        return results

    source = Path(plan["source"])

    for entry in plan["files"]:
        if not entry["changes"]:
            continue

        path = source / entry["filename"]

        if results["dry_run"]:
            results["tagged"] += 1
            continue

        try:
            track = (
                {
                    "title": entry["track_title"],
                    "position": entry["track_position"],
                    "disc": entry.get("track_disc"),
                    "disc_position": entry.get("track_disc_position"),
                }
                if entry["matched"] else None
            )
            write_tags(path, release, track)
            results["tagged"] += 1

        except Exception as e:
            logger.error(f"could not retag {entry['filename']}: {e}")
            results["failed"] += 1

    #? Before any move, so the write and the folder rename can't disagree about where the
    #? album is. The cover goes in as a plain file rather than being embedded in every
    #? track: it is what find_cover_file() prefers, what the organizer already copies across
    #? as a companion, and what every other music tool expects to find.
    art_action = (plan.get("art") or {}).get("action")

    if art_action and (art is not None or results["dry_run"]):
        name = f"cover.{extension_for(art[1])}" if art else "cover.jpg"

        if results["dry_run"]:
            results["art_written"] = name

        else:
            try:
                #? a replacement leaves the old file behind under its own name only if that
                #? name differs; same-named art is overwritten, which is what "replace" means
                (source / name).write_bytes(art[0])
                results["art_written"] = name
                logger.info(f"wrote {name} into {source.name}", extra={"frontend": True})
            except OSError as e:
                logger.error(f"could not write cover art into {source}: {e}")
                results["problems"].append(f"tags were written but the cover art could not be saved: {e}")

    elif art_action and art is None and not results["dry_run"]:
        results["problems"].append("no cover art was available for that release")

    if plan.get("moves") and plan.get("target"):
        target = Path(plan["target"])

        if results["dry_run"]:
            results["moved_to"] = str(target)

        elif results["failed"]:
            #? moving after a partial failure would scatter one album across two folders
            results["problems"].append("left the folder in place because some files failed")

        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                results["moved_to"] = str(target)
                logger.info(
                    f"re-filed {source.name} as {target.name}",
                    extra={"frontend": True},
                )
            except Exception as e:
                logger.error(f"could not move {source} to {target}: {e}")
                results["problems"].append(f"tags were written but the folder could not be renamed: {e}")

    return results
