"""
Turning a finished slskd download into a tagged, filed album.

This is the only part of jimbrainz that writes to the user's filesystem, so it is built in
two halves that are deliberately kept apart:

  plan_organization()  - pure. Works out what *would* happen. No disk writes, fully testable.
  execute_plan()       - does it, and only what the plan says.

That split is what makes dry-run trustworthy rather than a second code path that might
disagree with the real one: dry-run runs the identical plan and simply declines to execute.

Defaults are cautious on purpose - copy rather than move, never overwrite, and ORGANIZE_MODE
starts at dry_run so a first deploy with a mis-mapped volume reports what it would have done
instead of scattering a music library.
"""

import asyncio
import re
import shutil
from pathlib import Path

from src.editions import edition_discriminator, resolve_edition_label
from src.logger import logger
from src.matching import (AUDIO_EXTENSIONS, file_extension, match_tracks_to_files,
                          split_remote_path)


#? off      - never organize, downloads just sit in slskd's folder
#? dry_run  - plan and log, touch nothing (default: safe first deploy)
#? copy     - copy into the library, leave slskd's copy alone
#? move     - move into the library
ORGANIZE_MODES = ("off", "dry_run", "copy", "move")

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#? Non-audio files worth carrying into the library alongside the tracks. Cover art especially -
#? leaving it behind in slskd's folder loses it, since only audio is ever enqueued explicitly.
COMPANION_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "cue", "log", "nfo", "txt", "m3u", "m3u8", "sfv"
}


def sanitize_filename(name: str, fallback: str = "unknown") -> str:
    """Make a string safe as a single path component on any of the usual filesystems."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", name or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    #? 255 is the common per-component limit; leave room for a numeric suffix
    return (cleaned[:200] or fallback)


def build_album_dirname(release: dict, discriminator: str = "") -> str:
    """
    `Album (Year)`, plus ` [Edition]` when this release is a distinguishable edition.

    The suffix is omitted entirely for ordinary albums - most releases have exactly one
    edition and do not need decorating. It appears only when there is something real to say,
    which is what keeps the library readable while still letting the deluxe and the standard
    press of the same record coexist.

    `discriminator` is the escape hatch for two genuinely different releases that still
    produce the same name; plan_organization() supplies it only after seeing an actual
    collision on disk.
    """
    album = sanitize_filename(release.get("album"), "Unknown Album")

    #? The ALBUM's year, not this pressing's. A 2011 remaster of a 1975 record belongs in
    #? "Wish You Were Here (1975) [2011 remaster]" - the year identifies the album and the
    #? edition identifies the pressing, so putting the reissue year in front files the same
    #? record under two different decades depending on which copy you happened to get.
    #? MusicBrainz keeps this on the release GROUP as first-release-date.
    year = (release.get("original_year") or release.get("year") or "").strip()
    name = f"{album} ({year})" if year else album

    parts = [part for part in (resolve_edition_label(release), discriminator) if part]
    if parts:
        name = f"{name} [{' - '.join(parts)}]"

    return sanitize_filename(name)


def build_target_path(
    library_root: str,
    release: dict,
    track: dict | None,
    extension: str,
    discriminator: str = "",
) -> Path:
    """
    {library}/{artist}/{album} ({year}) [{edition}]/{NN} - {title}.{ext}

    Tracks that couldn't be matched to the tracklist keep their original filename rather than
    being given a made-up number - a wrong track number is worse than none.
    """
    artist = sanitize_filename(release.get("artist"), "Unknown Artist")

    if track and track.get("position") and track.get("title"):
        filename = f"{int(track['position']):02d} - {sanitize_filename(track['title'])}.{extension}"
    else:
        filename = sanitize_filename(track.get("filename") if track else None, "untitled") + f".{extension}"

    return Path(library_root) / artist / build_album_dirname(release, discriminator) / filename


def find_local_file(download_root: str, remote_filename: str, remote_directory: str = "") -> Path | None:
    """
    Locate what slskd actually wrote to disk for a given remote file.

    slskd's on-disk layout isn't something we control or can rely on staying put (it has
    changed between versions, and sanitizes remote directory names), so rather than
    reconstructing the path we search for the basename under the download root and prefer a
    hit whose parent folder matches the remote folder. Slower, but it survives slskd
    reorganizing its own downloads directory.
    """
    root = Path(download_root)
    if not root.is_dir():
        return None

    _, basename = split_remote_path(remote_filename)
    if not basename:
        return None

    matches = [p for p in root.rglob(basename) if p.is_file()]

    if not matches:
        return None

    if len(matches) > 1 and remote_directory:
        wanted_dir = remote_directory.replace("\\", "/").rstrip("/").rpartition("/")[2]
        preferred = [p for p in matches if p.parent.name == wanted_dir]
        if preferred:
            return preferred[0]

    return matches[0]


def is_within(child: Path, parent: Path) -> bool:
    """
    True only if `child` really sits underneath `parent`.

    Guards the one genuinely destructive operation in here - removing a source directory.
    Resolving both sides first means a symlink or a ".." in a configured path can't be used
    to walk the delete outside slskd's download folder.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_companion_files(source_dir: Path, placed: set[str]) -> list[Path]:
    """Art and sidecar files sitting next to the tracks, which are worth keeping."""
    if not source_dir.is_dir():
        return []

    return [
        entry for entry in sorted(source_dir.iterdir())
        if entry.is_file()
        and entry.name not in placed
        and file_extension(entry.name) in COMPANION_EXTENSIONS
    ]


def read_album_mbid(directory: Path) -> str | None:
    """
    The MusicBrainz release id already filed in this folder, if any.

    Identity rather than appearance: two releases are the same edition when they share a
    release MBID, whatever their folders happen to be called. Reads the first audio file it
    can and stops - every track in a folder belongs to the same release, so there is nothing
    to gain from opening the rest.

    Returns None for a folder jimbrainz didn't organize (no MBID tag), which the caller must
    treat as "unknown", NOT as "different" - guessing wrong there would fork someone's
    existing library into duplicate folders.
    """
    import mutagen

    if not directory.is_dir():
        return None

    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or not file_extension(entry.name) in AUDIO_EXTENSIONS:
            continue

        try:
            audio = mutagen.File(str(entry), easy=True)
        except Exception:
            continue

        if audio is None:
            continue

        values = audio.get("musicbrainz_albumid") or []
        if values:
            return str(values[0]).strip() or None

        #? a readable audio file that simply has no MBID tag is answer enough - the folder
        #? wasn't organized by us, so stop rather than opening every track hoping otherwise
        return None

    return None


def resolve_album_dir(library_root: str, release: dict) -> tuple[Path, str]:
    """
    Where this release's folder should be, avoiding a different release's folder.

    The ordinary case resolves on the first try. The interesting case is two releases whose
    names collide anyway - the same album, year and disambiguation, differing only by
    pressing - where we escalate to the catalogue number or the release id rather than
    letting one silently overwrite (or, as before, be silently skipped against) the other.

    Crucially this does NOT treat an untagged folder as a collision. A library that predates
    jimbrainz has no MBIDs, and forking every one of those albums into a second folder would
    be far worse than sharing one.
    """
    artist = sanitize_filename(release.get("artist"), "Unknown Artist")
    artist_dir = Path(library_root) / artist
    wanted_mbid = (release.get("release_mbid") or "").strip()

    for discriminator in ("", edition_discriminator(release)):
        candidate = artist_dir / build_album_dirname(release, discriminator)

        if not candidate.exists():
            return candidate, discriminator

        existing = read_album_mbid(candidate)

        #? free to share the folder: either it is demonstrably the same release, or it is
        #? untagged and we have no business assuming otherwise
        if existing is None or not wanted_mbid or existing == wanted_mbid:
            return candidate, discriminator

        if not discriminator:
            logger.info(
                f"'{candidate.name}' already holds a different release, "
                f"filing this edition separately",
                extra={"frontend": True, "src": "slskd"},
            )

    #? both attempts are taken by other releases, which needs the release id to break
    fallback = artist_dir / build_album_dirname(release, wanted_mbid[:8] or "alt")
    return fallback, wanted_mbid[:8] or "alt"


def plan_organization(job: dict, download_root: str, library_root: str) -> dict:
    """
    Work out every file operation this job implies. Touches nothing.

    The file->track mapping is recomputed here from the stored release and file list rather
    than having been persisted at match time: match_tracks_to_files is pure and needs only
    filenames, so recomputing keeps one source of truth instead of storing derived data that
    could drift from the code that produced it.
    """
    release = job.get("release") or {}
    tracks = release.get("tracks") or []
    files = job.get("files") or []

    mapping = match_tracks_to_files(tracks, files) if tracks else {}
    track_by_filename = {
        entry["file"]["filename"]: entry["track"] for entry in mapping.values()
    }

    #? decided once for the whole job, so every track lands in the same folder even if the
    #? escalation kicked in - resolving per file could split an album across two directories
    _, discriminator = resolve_album_dir(library_root, release)

    operations = []
    problems = []
    used_targets: set[Path] = set()

    for file_entry in files:
        remote_filename = file_entry.get("filename", "")
        source = find_local_file(download_root, remote_filename, job.get("directory", ""))

        if source is None:
            problems.append(f"not found on disk: {remote_filename}")
            continue

        _, basename = split_remote_path(remote_filename)
        track = track_by_filename.get(remote_filename)
        extension = file_extension(basename) or "bin"

        target = build_target_path(
            library_root,
            release,
            track or {"filename": Path(basename).stem},
            extension,
            discriminator,
        )

        #? two source files resolving to one target would silently destroy one of them
        if target in used_targets:
            problems.append(f"two files map to the same destination: {target.name}")
            continue

        used_targets.add(target)
        operations.append({
            "source": str(source),
            "target": str(target),
            "track": track,
            "exists": target.exists(),
        })

    album_dir = Path(operations[0]["target"]).parent if operations else None

    #? Carry cover art and sidecars across too. They're never enqueued (only audio is), so if
    #? slskd happened to fetch them they'd otherwise be stranded - and leaving anything behind
    #? means the source folder can never be tidied up either.
    source_dirs = {Path(op["source"]).parent for op in operations}
    placed_names = {Path(op["source"]).name for op in operations}
    companions = []

    if album_dir is not None:
        for source_dir in sorted(source_dirs):
            for companion in find_companion_files(source_dir, placed_names):
                target = album_dir / sanitize_filename(companion.name, companion.name)
                if target in used_targets:
                    continue
                used_targets.add(target)
                companions.append({
                    "source": str(companion),
                    "target": str(target),
                    "track": None,
                    "companion": True,
                    "exists": target.exists(),
                })

    return {
        "job_id": job.get("id"),
        "album_dir": str(album_dir) if album_dir else None,
        "operations": operations + companions,
        "companion_count": len(companions),
        "source_dirs": sorted(str(d) for d in source_dirs),
        "problems": problems,
        "matched_tracks": len(mapping),
        "total_tracks": len(tracks),
    }


def _is_multi_disc(release: dict) -> bool:
    """True when the release's tracklist spans more than one disc."""
    return len({t.get("disc") for t in release.get("tracks") or [] if t.get("disc")}) > 1


def tag_values(release: dict, track: dict | None) -> dict:
    """
    The tags a file should carry for this release and track.

    Split out from write_tags() so the retag preview can show what applying a release WOULD
    change without duplicating the rules. Two copies of this would drift, and a preview that
    disagrees with the write it is previewing is worse than no preview - the same reason
    dry-run runs the identical plan rather than a parallel one.
    """
    values = {
        "album": release.get("album"),
        "albumartist": release.get("artist"),
        "artist": release.get("artist"),
        "date": release.get("year"),
        #? Picard's convention, and the reason the folder can say 1975 while the file still
        #? records that this particular copy is the 2011 press. Not every container accepts
        #? it (easy MP4 doesn't) and the loop below skips whatever is rejected.
        "originaldate": release.get("original_year"),
        #? the edition's identity, and the only one of these every container supports. The
        #? library scanner groups on it: two folders sharing an MBID are the same edition
        #? however they happen to be named, and that is what survives someone renaming a
        #? folder by hand.
        "musicbrainz_albumid": release.get("release_mbid"),
        #? these feed resolve_edition_label() if the edition ever has to be recomputed from
        #? disk. Not universally supported - the loop below skips whatever a container
        #? rejects, which is why the MBID above carries the identity on its own.
        "musicbrainz_releasegroupid": release.get("release_group_mbid"),
        "releasecountry": release.get("country"),
        "media": release.get("media_format"),
        "catalognumber": release.get("catalog_number"),
    }

    if track:
        values["title"] = track.get("title")

        #? A multi-disc release is numbered PER DISC, the way MusicBrainz and every player number
        #? it: disc 2 opens with track 1 of disc 2, not track 11 of one running sequence.
        #? `position` stays the running number regardless - the matcher keys on it and files are
        #? named after it, which is what keeps a two-disc set in order inside one folder.
        #?
        #? A single-disc release writes no disc tag at all. Writing "1" everywhere would give
        #? every album in the library a discnumber change, so re-opening the editor on an album
        #? that is already right could never again say "nothing to change".
        if _is_multi_disc(release) and track.get("disc") and track.get("disc_position"):
            values["tracknumber"] = str(track["disc_position"])
            values["discnumber"] = str(track["disc"])
        elif track.get("position"):
            values["tracknumber"] = str(track["position"])

    #? empty values are dropped rather than written as blanks - clearing a tag the user
    #? already has because MusicBrainz didn't supply one would be destructive
    return {key: str(value) for key, value in values.items() if value}


def write_tags(path: Path, release: dict, track: dict | None) -> None:
    """
    Tag the file from the MusicBrainz release it came from.

    Writes the MusicBrainz IDs too, so the resulting library stays legible to Picard/beets
    later instead of being a jimbrainz-only artifact. Tagging failures are logged and
    tolerated: a filed-but-untagged file is a far better outcome than a half-organized album.
    """
    import mutagen

    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as e:
        logger.warning(f"could not read tags on {path.name}: {e}")
        return

    if audio is None:
        logger.warning(f"unsupported audio format for tagging: {path.name}")
        return

    for key, value in tag_values(release, track).items():
        if not value:
            continue

        try:
            audio[key] = str(value)
        except Exception:
            #? not every container supports every key (easy mp4 is picky); skip rather than abort
            continue

    try:
        audio.save()
    except Exception as e:
        logger.warning(f"could not write tags to {path.name}: {e}")


def execute_plan(plan: dict, release: dict, mode: str = "dry_run") -> dict:
    """
    Carry out a plan. `mode` decides how far it goes; dry_run stops before touching anything.
    """
    if mode not in ORGANIZE_MODES:
        mode = "dry_run"

    results = {"organized": 0, "skipped": 0, "failed": 0, "dry_run": mode == "dry_run", "mode": mode}

    if mode == "off":
        return results

    for operation in plan["operations"]:
        source = Path(operation["source"])
        target = Path(operation["target"])

        if mode == "dry_run":
            logger.info(f"[dry run] would place {source.name} -> {target}")
            results["organized"] += 1
            continue

        #? never clobber. An existing destination is far more likely to be a real album the
        #? user already has than something safe to overwrite.
        if target.exists():
            logger.warning(f"already exists, leaving it alone: {target}")
            results["skipped"] += 1
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)

            if mode == "move":
                shutil.move(str(source), str(target))
            else:
                shutil.copy2(str(source), str(target))

            if not operation.get("companion"):
                write_tags(target, release, operation.get("track"))

            results["organized"] += 1

        except Exception as e:
            logger.error(f"failed to place {source.name}: {e}")
            results["failed"] += 1

    return results


def cleanup_source_dirs(plan: dict, download_root: str, results: dict) -> list[str]:
    """
    Remove the now-empty slskd folders a completed move left behind.

    Every guard here is deliberate, because this is the only code in jimbrainz that deletes
    anything:

      - move only. copy exists precisely to leave the original alone.
      - nothing failed or was skipped, so we never delete beside a half-finished job.
      - the directory must resolve to somewhere inside the download root, so a stray path or
        symlink can't walk the delete out into the wider filesystem.
      - never the download root itself.
      - rmdir, not rmtree: it refuses on a non-empty directory by construction. Anything left
        is something we didn't put there, so it's reported and kept rather than assumed junk.
    """
    if results.get("failed") or results.get("skipped"):
        return []

    root = Path(download_root)
    removed = []

    for raw in plan.get("source_dirs", []):
        directory = Path(raw)

        if not directory.is_dir():
            continue

        if directory.resolve() == root.resolve() or not is_within(directory, root):
            logger.warning(f"refusing to remove {directory}, it is not inside {download_root}")
            continue

        try:
            directory.rmdir()
            removed.append(str(directory))
            logger.info(f"removed empty download folder {directory.name}")

        except OSError:
            leftovers = sorted(p.name for p in directory.iterdir())
            logger.warning(
                f"left {directory.name} in place, still holds: {', '.join(leftovers[:6])}",
                extra={"frontend": True, "src": "slskd"},
            )

    return removed


def remove_incomplete_downloads(
    incomplete_root: str,
    files: list[dict],
    remote_directory: str = "",
) -> dict:
    """
    Delete the partial files a cancelled download left in slskd's incomplete folder.

    **slskd keeps these on purpose.** It stores a partial download at
    `<incomplete>/<username>/<remote path>/<file>` and, when `retry.partial` is `Resume`, starts
    the next attempt at the partial file's length instead of from zero. So a leftover is a
    feature for a download that failed, and only junk for one you deliberately cancelled - which
    is why this runs on cancel and nowhere else, and why it does nothing at all unless
    SLSKD_INCOMPLETE_PATH has been pointed at that folder.

    Matching is by basename under the root rather than by rebuilding slskd's path, for the same
    reason find_local_file() does it: slskd sanitizes the remote path into the name on disk
    (`C:` becomes `C_`) and that mapping is its business, not ours.

    But it is STRICTER than find_local_file, deliberately. That one picks a best guess because
    guessing wrong means organizing the wrong file; here guessing wrong means DELETING someone
    else's download. A basename that appears more than once, or whose parent folder isn't the
    one this job was downloading from, is left alone - `01 - Intro.flac` is not a rare name.

    Every guard mirrors delete_album(), which is the other place in jimbrainz that removes data
    the user did not just ask for.
    """
    results: dict = {"removed": [], "skipped": [], "problem": None}

    if not incomplete_root:
        results["problem"] = "SLSKD_INCOMPLETE_PATH is not set"
        return results

    root = Path(incomplete_root)

    if not root.is_dir():
        results["problem"] = f"the incomplete folder isn't there: {incomplete_root}"
        return results

    #? the last component of the remote folder, which is the directory name slskd nests the
    #? partial under - same derivation find_local_file uses to prefer the right match
    wanted_dir = remote_directory.replace("\\", "/").rstrip("/").rpartition("/")[2] if remote_directory else ""

    for entry in files:
        remote_filename = entry.get("filename", "")
        _, basename = split_remote_path(remote_filename)

        if not basename:
            continue

        matches = [p for p in root.rglob(basename) if p.is_file()]

        #? the strictness described above: only a file sitting in the folder this job was
        #? downloading from is unambiguously ours to remove
        if wanted_dir:
            matches = [p for p in matches if p.parent.name == wanted_dir]

        if not matches:
            continue

        if len(matches) > 1:
            results["skipped"].append(f"{basename} (several partial files share that name)")
            continue

        partial = matches[0]

        #? resolves both sides, so neither a symlink nor a ".." in the configured path can walk
        #? the delete out of the incomplete folder
        if not is_within(partial, root) or partial.resolve() == root.resolve():
            logger.warning(f"refused to remove {partial}, it is not inside {incomplete_root}")
            results["skipped"].append(f"{basename} (outside the incomplete folder)")
            continue

        try:
            partial.unlink()
            results["removed"].append(str(partial))
        except OSError as e:
            logger.warning(f"could not remove the partial file {partial}: {e}")
            results["skipped"].append(f"{basename} ({e})")

    #? Tidy the folders the partials sat in, innermost first. rmdir refuses a non-empty
    #? directory by construction, so anything still holding a file is left exactly as it was -
    #? the same reasoning as cleanup_source_dirs.
    for directory in sorted({Path(p).parent for p in results["removed"]},
                            key=lambda d: len(d.parts), reverse=True):
        while directory != root and is_within(directory, root):
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent

    return results


async def organize_job(job: dict, download_root: str, library_root: str, mode: str) -> dict:
    """Plan then execute, with the logging the UI's event log surfaces."""
    label = f"{job.get('artist')} - {job.get('album')}"
    plan = await asyncio.to_thread(plan_organization, job, download_root, library_root)

    for problem in plan["problems"]:
        logger.warning(f"{label}: {problem}", extra={"frontend": True, "src": "slskd"})

    if not plan["operations"]:
        logger.error(f"nothing to organize for {label}", extra={"frontend": True, "src": "slskd"})
        return {"organized": 0, "skipped": 0, "failed": 0,
                "dry_run": mode == "dry_run", "mode": mode, "plan": plan}

    results = await asyncio.to_thread(execute_plan, plan, job.get("release") or {}, mode)

    if mode == "move":
        results["removed_dirs"] = await asyncio.to_thread(
            cleanup_source_dirs, plan, download_root, results
        )

    verb = "would organize" if results.get("dry_run") else "organized"
    companions = plan.get("companion_count") or 0
    extra = f" (+{companions} cover/sidecar file(s))" if companions else ""
    logger.info(
        f"{verb} {results['organized']} file(s){extra} for {label} into {plan['album_dir']}",
        extra={"frontend": True, "src": "slskd"},
    )

    results["plan"] = plan
    return results
