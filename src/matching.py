"""
Scoring slskd search results against the MusicBrainz release the user actually picked.

This is the whole point of cutting Lidarr out. Lidarr only ever learned "go find album N",
so a search for a 2011 remaster looked identical to a search for the 1979 original. Here we
still have the release the user clicked: its tracklist, durations, year and edition tags. So
candidate folders get ranked against that instead of guessed at.

Deliberately pure - no network, no disk, no config. Everything in here is a plain function
over plain dicts so it can be tested without a live slskd (tests/test_matching.py).
"""

import re
from difflib import SequenceMatcher


AUDIO_EXTENSIONS = {
    "flac", "mp3", "m4a", "ogg", "opus", "wav", "aac", "wma", "alac", "aiff", "aif", "ape", "wv"
}

LOSSLESS_EXTENSIONS = {"flac", "wav", "alac", "aiff", "aif", "ape", "wv"}

#? mirrors EDITION_KEYWORDS in interface/scripts/main.js. the frontend tags the release,
#? this tags the candidate folder name, and then we compare the two.
EDITION_PATTERNS = [
    (re.compile(r"super\s*deluxe"), "SUPER DELUXE"),
    (re.compile(r"deluxe"), "DELUXE"),
    (re.compile(r"box\s*set|boxset"), "BOX SET"),
    (re.compile(r"anniversary"), "ANNIVERSARY"),
    (re.compile(r"expanded"), "EXPANDED"),
    (re.compile(r"limited\s*edition"), "LIMITED"),
    (re.compile(r"special\s*edition"), "SPECIAL EDITION"),
    (re.compile(r"remaster"), "REMASTER"),

    #? ALTERNATE PERFORMANCES, which are a different kind of edition from the ones above and
    #? matter more, not less.
    #?
    #? A deluxe edition is MORE OF THE SAME ALBUM: collide it with the standard press and you
    #? lose some bonus tracks. An instrumental is a DIFFERENT RECORDING of it - same track
    #? titles, same numbers, same count - so colliding it means every single file matches an
    #? existing one, all of them are skipped, and you end up with neither album while the job
    #? reports that there was nothing to do.
    #?
    #? Dance Gavin Dance is the case that surfaced it: MusicBrainz holds the instrumental as a
    #? RELEASE inside the ordinary album's group, with "(instrumental)" in the title and an
    #? EMPTY disambiguation - so every source resolve_edition_label() consults came back blank
    #? and it resolved to the standard album's own folder. Verified against the live API.
    (re.compile(r"instrumental"), "INSTRUMENTAL"),
    (re.compile(r"acoustic"), "ACOUSTIC"),
    #? "a cappella", "acappella" and the common misspelling "a capella"
    (re.compile(r"a\s*capp?ella"), "A CAPPELLA"),
]

#? how much each signal contributes to the final score. title/count identify the album,
#? edition picks the right *version* of it, which is the thing that was broken before.
WEIGHTS = {
    "title_match": 0.30,
    "track_count": 0.20,
    "duration_match": 0.15,
    "edition": 0.15,
    "format": 0.12,
    "peer": 0.08,
}

TITLE_MATCH_THRESHOLD = 0.60
DURATION_TOLERANCE_SEC = 8


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. For comparing messy filenames."""
    if not text:
        return ""

    text = text.lower()
    text = re.sub(r"[\[\](){}_\-.,'\"!?/\\:;&+]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_remote_path(path: str) -> tuple[str, str]:
    """
    Soulseek paths are Windows-ish (`@@abc\\Music\\Artist\\Album\\01 track.flac`) but not
    reliably so. Split on whichever separator the peer happened to use.
    """
    if not path:
        return "", ""

    normalized = path.replace("\\", "/")
    if "/" not in normalized:
        return "", path

    directory, _, filename = normalized.rpartition("/")
    return directory, filename


def file_extension(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    return ext.lower() if ext and ext != filename else ""


def filename_stem(filename: str) -> str:
    stem, _, ext = filename.rpartition(".")
    return stem if stem and ext != filename else filename


def is_audio(filename: str) -> bool:
    return file_extension(filename) in AUDIO_EXTENSIONS


def detect_edition_tags(text: str) -> set[str]:
    """Pull edition markers out of a folder name, same vocabulary the UI tags releases with."""
    haystack = normalize(text)
    tags = set()

    for pattern, label in EDITION_PATTERNS:
        if pattern.search(haystack):
            tags.add(label)

    #? "super deluxe" also trips the plain "deluxe" pattern, keep only the specific one
    if "SUPER DELUXE" in tags:
        tags.discard("DELUXE")

    return tags


def title_similarity(track_title: str, filename: str) -> float:
    """
    How well does this file look like this track? Filenames usually carry track numbers and
    junk ("03 - The Color of the Fire (2013 remaster).flac"), so a containment check comes
    first and fuzzy ratio is the fallback.
    """
    title = normalize(track_title)
    stem = normalize(filename_stem(filename))

    if not title or not stem:
        return 0.0

    if title in stem:
        return 1.0

    return SequenceMatcher(None, title, stem).ratio()


def match_tracks_to_files(expected_tracks: list[dict], files: list[dict]) -> dict[int, dict]:
    """
    Greedily pair each expected track with its best unclaimed file.

    Returned mapping is reused later by the organizer: it's what lets us write correct track
    numbers and titles even when the peer named everything "Track 04.mp3".
    """
    remaining = list(files)
    mapping: dict[int, dict] = {}

    for track in expected_tracks:
        best_file = None
        best_score = 0.0

        for candidate_file in remaining:
            _, filename = split_remote_path(candidate_file.get("filename", ""))
            score = title_similarity(track.get("title", ""), filename)

            if score > best_score:
                best_score = score
                best_file = candidate_file

        if best_file is not None and best_score >= TITLE_MATCH_THRESHOLD:
            mapping[track.get("position")] = {
                "file": best_file,
                "score": round(best_score, 3),
                "track": track,
            }
            remaining.remove(best_file)

    return mapping


def score_track_count(expected_count: int, actual_count: int) -> float | None:
    """None means "no opinion" - the signal gets dropped from the weighted average."""
    if not expected_count:
        return None

    if actual_count == expected_count:
        return 1.0

    difference = abs(actual_count - expected_count)

    #? a folder missing/gaining one track is plausible (hidden track, bonus). Way off is not.
    return max(0.0, 1.0 - (difference / expected_count))


def score_durations(mapping: dict[int, dict]) -> float | None:
    """
    Compare MusicBrainz track lengths against the peer's file lengths. Different masterings
    drift by a second or two, but a wholly different recording won't line up at all.
    """
    comparable = 0
    close = 0

    for entry in mapping.values():
        expected_ms = entry["track"].get("length_ms")
        actual_sec = entry["file"].get("length")

        if not expected_ms or not actual_sec:
            continue

        comparable += 1
        if abs((expected_ms / 1000) - actual_sec) <= DURATION_TOLERANCE_SEC:
            close += 1

    if not comparable:
        return None

    return close / comparable


def score_format(files: list[dict], format_preference: str) -> float:
    extensions = {file_extension(split_remote_path(f.get("filename", ""))[1]) for f in files}
    has_lossless = bool(extensions & LOSSLESS_EXTENSIONS)

    if format_preference == "lossless_only":
        return 1.0 if has_lossless else 0.0

    if format_preference == "prefer_lossless":
        return 1.0 if has_lossless else 0.45

    return 1.0


def score_edition(directory: str, expected_tags: list[str] | None, expected_year: str | None) -> float:
    """
    The signal that motivated this whole rewrite.

    Caveat worth remembering: Soulseek folder names are typed by strangers and frequently
    omit edition text entirely, so this can only ever be a weighted nudge - never a hard
    filter, or legitimate results vanish. The year is a useful proxy since reissues usually
    carry their reissue year in the folder name.
    """
    folder_tags = detect_edition_tags(directory)
    expected = set(expected_tags or [])

    if expected:
        overlap = len(expected & folder_tags) / len(expected)
        score = overlap
    else:
        #? user picked a plain edition, so a "DELUXE" folder is probably the wrong thing
        score = 1.0 if not folder_tags else 0.4

    if expected_year and expected_year in directory:
        score = min(1.0, score + 0.25)

    return score


def score_peer(response: dict) -> float:
    """Availability tiebreaker - a perfect match from someone with a 400-deep queue isn't."""
    score = 0.0

    if response.get("hasFreeUploadSlot"):
        score += 0.5

    queue_length = response.get("queueLength", 0) or 0
    if queue_length == 0:
        score += 0.3
    elif queue_length < 10:
        score += 0.15

    upload_speed = response.get("uploadSpeed", 0) or 0
    if upload_speed > 1_000_000:
        score += 0.2
    elif upload_speed > 100_000:
        score += 0.1

    return min(1.0, score)


def group_files_by_directory(responses: list[dict]) -> list[dict]:
    """
    A peer's `files` list is flat and can span several of their folders, so an album isn't a
    response - it's a (user, directory) pair. Split them out and drop non-audio clutter.
    """
    candidates: dict[tuple[str, str], dict] = {}

    for response in responses:
        username = response.get("username", "")

        for file_entry in response.get("files", []) or []:
            filename_full = file_entry.get("filename", "")
            directory, filename = split_remote_path(filename_full)

            if not is_audio(filename):
                continue

            key = (username, directory)
            if key not in candidates:
                candidates[key] = {
                    "username": username,
                    "directory": directory,
                    "directory_name": directory.replace("\\", "/").rpartition("/")[2] or directory,
                    "files": [],
                    "response": response,
                }

            candidates[key]["files"].append(file_entry)

    return list(candidates.values())


def score_candidate(candidate: dict, expected: dict, format_preference: str = "prefer_lossless") -> dict:
    """Score one (user, directory) candidate against the picked release. Returns it enriched."""
    files = candidate["files"]
    expected_tracks = expected.get("tracks") or []

    mapping = match_tracks_to_files(expected_tracks, files) if expected_tracks else {}

    signals: dict[str, float | None] = {}

    signals["title_match"] = (len(mapping) / len(expected_tracks)) if expected_tracks else None
    signals["track_count"] = score_track_count(len(expected_tracks), len(files))
    signals["duration_match"] = score_durations(mapping) if mapping else None
    signals["edition"] = score_edition(
        candidate["directory"], expected.get("edition_tags"), expected.get("year")
    )
    signals["format"] = score_format(files, format_preference)
    signals["peer"] = score_peer(candidate["response"])

    #? when a release has no tracklist (release-group level search) the tracklist-dependent
    #? signals are None. Renormalize over whatever we do have rather than scoring those
    #? candidates as though they'd failed.
    active = {k: v for k, v in signals.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in active)
    score = sum(WEIGHTS[k] * v for k, v in active.items()) / total_weight if total_weight else 0.0

    response = candidate["response"]

    return {
        **candidate,
        "score": round(score, 4),
        "signals": {k: (round(v, 3) if v is not None else None) for k, v in signals.items()},
        "matched_tracks": len(mapping),
        "expected_tracks": len(expected_tracks),
        "audio_file_count": len(files),
        "track_mapping": mapping,
        "detected_edition_tags": sorted(detect_edition_tags(candidate["directory"])),
        "formats": sorted({file_extension(split_remote_path(f.get("filename", ""))[1]) for f in files}),
        #? Peer stats, surfaced so the UI can show and filter on them.
        #?
        #? UNITS: BYTES/sec, not bits. slskd's own web UI renders it as
        #? `formatBytes(response.uploadSpeed)/s`, and the thresholds in score_peer() are only
        #? sensible read that way (1 MB/s fast, 100 KB/s decent; as bits those would be 125 and
        #? 12.5 KB/s). A comment here previously said bits, which would invite someone to
        #? "correct" the display by a factor of eight.
        #?
        #? MEANING: the peer's average upload speed over their whole history, to everyone, as
        #? reported by Soulseek - NOT the rate this transfer will run at. It is divided among
        #? however many people they are serving at once and averaged over conditions that have
        #? since changed, so it reads high far more often than not. An earlier comment here
        #? called it "our download speed", which is what the interface then went and claimed
        #? too. score_peer() weights it accordingly: 0.2 at most, inside a signal worth 0.08.
        "upload_speed": response.get("uploadSpeed", 0) or 0,
        "queue_length": response.get("queueLength", 0) or 0,
        "has_free_slot": bool(response.get("hasFreeUploadSlot")),
        "total_size": sum(f.get("size", 0) or 0 for f in files),
        "bitrates": sorted({f["bitRate"] for f in files if f.get("bitRate")}),
    }


def rank_candidates(
    responses: list[dict],
    expected: dict,
    format_preference: str = "prefer_lossless",
    limit: int = 50,
) -> list[dict]:
    """Full pipeline: raw slskd responses in, ranked scored candidates out."""
    candidates = group_files_by_directory(responses)
    scored = [score_candidate(c, expected, format_preference) for c in candidates]

    if format_preference == "lossless_only":
        scored = [c for c in scored if c["signals"]["format"] > 0]

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]
