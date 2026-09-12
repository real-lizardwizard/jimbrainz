import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.config import Config
from src.library import (SCAN_FORMAT, delete_album, drain_cache_changes, forget_cached_album,
                         load_album_art, read_album_details, scan_library, seed_cache,
                         snapshot_library, summarize_for_deletion)
from src.logger import logger
from src.metadata_health import ISSUE_TYPES, attach_issues
from src.organizer import is_within
from src.api.coverart_endpoint import CoverArtClient
from src.retag import execute_retag, plan_cover_art, plan_retag, save_cover_art

#? One client for the process, closed with the app in src/api/app.py. Cover art is fetched
#? rarely and one at a time, so there is nothing to gain from per-request clients and a
#? little to lose - each would open a fresh TLS connection to archive.org.
coverart_client = CoverArtClient()

router = APIRouter()


def _store(request: Request):
    """
    The SQLite store, or None.

    Looked up defensively rather than assumed: the store is attached by the app's lifespan, so
    a route imported into a test or the dev harness without one must still answer. Every
    caller below treats None as "nothing is remembered", which is the same shape as the store
    having failed to open - so there is one degraded path, not two.
    """
    return getattr(request.app.state, "store", None)


#? Which LIBRARY_PATH's saved scan has been loaded into memory. Once per root per process:
#? after that the in-memory cache is the fresher of the two, and re-seeding would be waste.
_cache_loaded_for: str | None = None
_cache_load_lock = asyncio.Lock()


async def _ensure_cache_loaded(request: Request) -> None:
    """
    Load the saved scan into memory, the first time the library is asked for.

    Lazily rather than at startup, for the same reason the library isn't scanned until its tab
    is opened: people who never look at it shouldn't pay for it. It is one indexed read, and
    it is the difference between the first visit after a restart re-reading every tag in the
    library and merely statting every folder.
    """
    global _cache_loaded_for
    root = Config.LIBRARY_PATH or ""

    if not root or _cache_loaded_for == root:
        return

    async with _cache_load_lock:
        if _cache_loaded_for == root:
            return

        store = _store(request)
        if store is not None:
            taken = seed_cache(await store.load_library_cache(root, SCAN_FORMAT))
            if taken:
                logger.info(f"library: loaded {taken} album(s) from the saved scan")

        _cache_loaded_for = root


async def _persist_cache(request: Request, scan: dict | None = None) -> None:
    """
    Save what the scan cache learned, so a restart picks up from it instead of from nothing.

    Called after every real scan AND after every forget. The forget matters as much as the
    scan: an in-place retag doesn't move the folder's mtime, so a saved row left behind would
    load the pre-edit tags straight back in on the next restart and they would match.
    """
    upserts, removals = drain_cache_changes()
    store = _store(request)

    if store is None:
        return

    root = Config.LIBRARY_PATH or ""

    if upserts or removals:
        await store.save_library_cache(root, upserts, removals, SCAN_FORMAT)

    if scan is not None and not scan.get("problem"):
        await store.record_library_scan(
            root, scan["scanned_at"], scan["scan_seconds"], scan["album_count"]
        )


async def _scan_with_queue(request: Request, force: bool, snapshot: bool = False) -> dict:
    """
    Scan the library, then say what still needs attention and why.

    The issues are derived here, on every response, rather than stored: metadata_health.py is
    pure, so recomputing costs nothing measurable next to the scan it decorates, and an album
    that has just been fixed drops out of the queue without anything having to remember to
    clear a flag. The only persisted state is what the user chose to ignore.

    Albums are also enrolled for review as a side effect, which is what gives `first_seen` a
    meaning. It is `INSERT OR IGNORE`, so a scan can never overwrite the fact that an album
    arrived as a download rather than being found sitting there.

    `snapshot` answers from the cache without touching the disk, when there is a cache to
    answer from - see library.snapshot_library. It falls through to a real scan when there
    isn't, so asking for one never returns an empty library that is merely unscanned.
    """
    root = Config.LIBRARY_PATH or ""
    store = _store(request)

    await _ensure_cache_loaded(request)

    result = await asyncio.to_thread(snapshot_library, root) if snapshot else None

    if result is not None:
        #? when the disk was last actually looked at, so the interface can say how old this is
        info = await store.library_scan_info(root) if store else None
        result["scanned_at"] = info["scanned_at"] if info else None
    else:
        result = await asyncio.to_thread(scan_library, root, force)
        await _persist_cache(request, result)

    reviews = await store.album_reviews() if store else {}

    result["queue"] = attach_issues(result["albums"], reviews)
    result["issue_types"] = ISSUE_TYPES
    #? so the interface can say "your ignores aren't being saved" rather than silently
    #? forgetting them, exactly as the downloads panel does for job tracking
    result["review_tracking_enabled"] = bool(store and store.available)

    #? Neither of these runs on a snapshot. Enrolling from one would be harmless, but pruning
    #? from one would not: an album filed since the snapshot was taken is absent from it, and
    #? its brand-new import row would be deleted as an orphan before anyone had seen it.
    if store and not result["stale"]:
        await store.record_albums_seen(result["albums"])

        #? Only after a scan that found something. An orphaned row - one whose folder was
        #? renamed or removed outside jimbrainz - is counted by the tab badge but has no album
        #? in this list to put a chip on and no queue entry to step through, so it reports an
        #? album needing attention that it cannot name or clear. Guarded on the scan being
        #? trustworthy because an empty library is far more often an unmounted volume than a
        #? deleted collection.
        if not result["problem"] and result["albums"]:
            await store.forget_missing_albums({a["path"] for a in result["albums"]})

    return result


@router.get("/albums")
async def albums(request: Request, snapshot: bool = False):
    """
    Everything currently in LIBRARY_PATH, and what's wrong with it.

    Scanning touches the filesystem and reads tags, so it runs in a thread rather than
    blocking the event loop - a first scan of a large library takes seconds, and the download
    poller must keep running while it does.

    A missing or unset LIBRARY_PATH is reported in `problem` rather than raised: the library
    view is perfectly capable of rendering "you haven't configured this yet", and a 500 here
    would look like a broken app instead of an unfinished setup.

    `?snapshot=true` answers from the saved scan without touching the disk, marked `stale`.
    The interface draws that at once and then asks again without it - see useLibrary.
    """
    try:
        result = await _scan_with_queue(request, False, snapshot)

        if result["stale"]:
            #? debug, not the event log: every visit to the tab now makes two requests, and
            #? the real scan right behind this one reports the figure worth reading
            logger.debug(f"library: {result['album_count']} album(s) from the saved scan")
        elif result["problem"]:
            logger.warning(f"library scan: {result['problem']}", extra={"frontend": True})
        else:
            queue = result["queue"]
            waiting = f", {queue['total']} needing metadata" if queue["total"] else ""
            logger.info(
                f"library: {result['album_count']} album(s) by {result['artist_count']} "
                f"artist(s) in {result['scan_seconds']}s "
                f"({result['cached']} unchanged){waiting}",
                extra={"frontend": True},
            )

        return result

    except Exception as e:
        logger.error(f"Exception in /albums endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error scanning library: {e}")


@router.post("/rescan")
async def rescan(request: Request):
    """
    Drop the per-folder cache and read everything again.

    The cache keys on directory mtime, which catches tracks being added or removed but not a
    file being re-tagged in place on some filesystems. This is the manual escape hatch for
    that, and for when you simply don't trust what you're looking at.
    """
    try:
        return await _scan_with_queue(request, True)

    except Exception as e:
        logger.error(f"Exception in /rescan endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error rescanning library: {e}")


@router.get("/art")
async def art(album: str):
    """
    An album's cover, from a file beside the tracks or from inside the audio itself.

    `album` is a path RELATIVE to LIBRARY_PATH, exactly as the scan reports it. That makes
    this the only endpoint in jimbrainz that turns user input into a filesystem read, so it
    is checked before it is used:

      - the resolved directory must sit inside LIBRARY_PATH. `is_within` resolves both sides
        first, so neither a "../.." nor a symlink pointing out of the library gets through.
      - an empty path is refused rather than quietly resolving to the library root.

    Without that, `?album=../../../../etc` would happily read anything the container user
    can. The check is the point of the endpoint existing at all rather than serving
    LIBRARY_PATH as static files.
    """
    root_path = Config.LIBRARY_PATH or ""

    if not root_path or not album:
        raise HTTPException(status_code=404, detail="no such album")

    root = Path(root_path)
    directory = root / album

    if not is_within(directory, root) or not directory.is_dir():
        #? deliberately the same 404 as a missing album: a traversal attempt learns nothing
        #? about what does or doesn't exist outside the library
        logger.warning(f"refused library art request outside the library: {album!r}")
        raise HTTPException(status_code=404, detail="no such album")

    result = await asyncio.to_thread(load_album_art, directory)

    if result is None:
        raise HTTPException(status_code=404, detail="no art for this album")

    data, mime = result

    return Response(
        content=data,
        media_type=mime,
        #? Art changes rarely but not never - replacing cover.jpg should show up without a
        #? hard refresh. Five minutes keeps scrolling a large library cheap while still
        #? letting an edit appear on its own.
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/tracks")
async def tracks(album: str):
    """
    Every tag on every file in one album, read from the files right now. For the track viewer.

    Read live rather than served from the scan, because the viewer's job is to show what is on
    disk: the scan is cached on the folder's mtime, and a retag by any other tool doesn't move
    that. It is also far more than the scan carries - thirty-odd named tags, the audio
    properties and every raw tag - which the library-wide payload has no business holding.

    Takes a path relative to LIBRARY_PATH, so it copies /art's guard exactly: containment is
    checked before anything is read, and every refusal is the same 404 as a missing album.
    """
    root_path = Config.LIBRARY_PATH or ""

    if not root_path or not album:
        raise HTTPException(status_code=404, detail="no such album")

    root = Path(root_path)
    directory = root / album

    if not is_within(directory, root) or not directory.is_dir():
        logger.warning(f"refused library tracks request outside the library: {album!r}")
        raise HTTPException(status_code=404, detail="no such album")

    return {"album": album, "files": await asyncio.to_thread(read_album_details, directory)}


class RetagRelease(BaseModel):
    """
    The release to apply. Same shape the download path stores with a job, so an album
    corrected by hand ends up carrying exactly the tags one downloaded fresh would have.
    """
    artist: str = ""
    album: str = ""
    year: str | None = None
    #? the album's original release year, which is what the folder is named after
    original_year: str | None = None
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    disambiguation: str | None = None
    media_format: str | None = None
    country: str | None = None
    catalog_number: str | None = None
    edition_label: str | None = None
    edition_tags: list[str] = Field(default_factory=list)
    tracks: list[dict] = Field(default_factory=list)


class RetagRequest(BaseModel):
    #? relative to LIBRARY_PATH, as the scan reports it
    album_path: str
    release: RetagRelease
    #? off unless asked. Fetching art is a network round trip and can overwrite a cover the
    #? user chose themselves, so it is never a side effect of correcting tags.
    fetch_art: bool = False


@router.post("/retag/preview")
async def retag_preview(body: RetagRequest):
    """
    What applying this release would change. Writes nothing.

    Separate from apply on purpose rather than an `apply=false` flag: this is the endpoint
    the interface calls while you're still choosing, so it must be impossible for it to
    modify anything by accident.
    """
    if not Config.LIBRARY_PATH:
        raise HTTPException(status_code=400, detail="LIBRARY_PATH is not set")

    try:
        return await asyncio.to_thread(
            plan_retag, body.album_path, body.release.model_dump(),
            Config.LIBRARY_PATH, body.fetch_art,
        )

    except Exception as e:
        logger.error(f"Exception in /retag/preview endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error planning the retag: {e}")


@router.post("/retag/apply")
async def retag_apply(request: Request, body: RetagRequest):
    """
    Write the tags and re-file the folder.

    The plan is recomputed here rather than accepted from the client: a plan is a list of
    file operations, and taking one over the wire would let anyone hand us arbitrary paths
    to write to. Recomputing costs a directory read and keeps every guard in plan_retag on
    the only path that can actually write.
    """
    if not Config.LIBRARY_PATH:
        raise HTTPException(status_code=400, detail="LIBRARY_PATH is not set")

    release = body.release.model_dump()

    try:
        plan = await asyncio.to_thread(
            plan_retag, body.album_path, release, Config.LIBRARY_PATH, body.fetch_art
        )

        if plan["source"] is None:
            raise HTTPException(status_code=400, detail="; ".join(plan["problems"]))

        #? Fetched here rather than inside execute_retag: that module writes to the user's
        #? filesystem and nothing else, and giving it a network dependency would make it
        #? untestable without one. A failure is not fatal - the tags are the point.
        art = None
        if plan["art"]["action"]:
            art = await coverart_client.fetch_front(release.get("release_mbid") or "")

        results = await asyncio.to_thread(execute_retag, plan, release, "apply", art)

        #? The cache keys on the folder's mtime, which a retag does not move - so without
        #? this the interface would keep showing the old tags and the edit would look like
        #? it had failed. Both the old and new locations go, since a move leaves the source
        #? key pointing at a folder that no longer exists.
        forget_cached_album(plan["source"])
        if results.get("moved_to"):
            forget_cached_album(results["moved_to"])
        await _persist_cache(request)

        #? Applying a release IS reviewing the album, so this clears it from the new-import
        #? prompt without a second click. It follows the rename because album_review is keyed
        #? on the path: leaving the row behind would orphan the history of an album that is
        #? still very much there, and re-enrol it as brand new on the next scan.
        store = _store(request)
        if store is not None:
            await store.mark_album_reviewed(
                body.album_path,
                plan["target_path"] if results.get("moved_to") else None,
            )

        logger.info(
            f"retagged {results['tagged']} file(s) in {body.album_path}"
            + (f", re-filed as {Path(results['moved_to']).name}" if results.get("moved_to") else ""),
            extra={"frontend": True},
        )

        return {"plan": plan, "results": results}

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /retag/apply endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error applying the retag: {e}")


class CoverArtRequest(BaseModel):
    #? relative to LIBRARY_PATH, as the scan reports it
    album_path: str
    #? off by default. Somebody's hand-picked sleeve is not ours to overwrite because the
    #? Archive happens to have one too - the same instinct as execute_plan refusing to clobber.
    replace: bool = False
    #? Optional. Absent, the release comes from the album's own tags, which is what makes the
    #? plain path require no decision. The editor sends one because it is showing you that
    #? release's cover at the time, so the choice is deliberate and visible rather than implied.
    release_mbid: str | None = None


@router.post("/art/fetch")
async def fetch_cover_art(request: Request, body: CoverArtRequest):
    """
    Put a cover in an album's folder. Changes nothing else about it.

    The narrow counterpart to /retag/apply. Applying a release rewrites every file's tags and
    can rename the folder, which is a lot to agree to when the only thing missing is the
    picture - and an album whose tags are already correct shouldn't have to be re-tagged to
    gain a sleeve.

    Nothing is chosen here. The release comes from the album's own tags, so this asks the Cover
    Art Archive for art belonging to the release the album already claims to be. That is what
    makes it safe to fire without a preview: there is no judgement to get wrong.
    """
    if not Config.LIBRARY_PATH:
        raise HTTPException(status_code=400, detail="LIBRARY_PATH is not set")

    try:
        plan = await asyncio.to_thread(
            plan_cover_art, body.album_path, Config.LIBRARY_PATH, body.replace, body.release_mbid
        )

        if plan["problem"]:
            raise HTTPException(status_code=400, detail=plan["problem"])

        art = await coverart_client.fetch_front(plan["release_mbid"] or "")

        if art is None:
            raise HTTPException(
                status_code=404,
                detail="the Cover Art Archive has no front cover for that release",
            )

        results = await asyncio.to_thread(
            save_cover_art, body.album_path, Config.LIBRARY_PATH, art
        )

        if results["problem"]:
            raise HTTPException(status_code=500, detail=results["problem"])

        #? the folder's mtime doesn't move when a cover REPLACES one of the same name, so
        #? without this the scan would keep serving the old art_mtime and the interface would
        #? go on showing the previous cover - see read_album_dir
        forget_cached_album(str(Path(Config.LIBRARY_PATH) / body.album_path))
        await _persist_cache(request)

        return {"written": results["written"], "replaced": plan["existing"]}

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /art/fetch endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching the cover art: {e}")


class QueueRequest(BaseModel):
    #? relative to LIBRARY_PATH, as the scan reports it - and what album_review is keyed on
    album_path: str
    #? which issues you're accepting. Sent by the client so it can only ever ignore the
    #? problems it actually showed you: an empty list would silently mute nothing, and
    #? ignoring "everything wrong with this album" computed server-side could mute a problem
    #? that appeared between the page loading and the button being pressed.
    issues: list[str] = Field(default_factory=list)


@router.get("/queue/new_imports")
async def new_imports(request: Request):
    """
    Albums jimbrainz has filed that you haven't looked at yet.

    Answerable without touching the filesystem, which is the entire point of it: the library is
    deliberately not scanned until you open its tab, so a "you have new albums" prompt that
    needed a scan would either be missing when it mattered or would tax every page load. This
    reads one indexed table.
    """
    store = _store(request)

    if store is None:
        return {"count": 0, "albums": [], "tracking_enabled": False}

    return await store.new_import_summary()


@router.post("/queue/ignore")
async def ignore_issues(request: Request, body: QueueRequest):
    """
    Accept an album as it is, so it stops appearing in the queue.

    Stored per issue rather than as one "ignored" flag, so agreeing that a bootleg will never
    be in MusicBrainz doesn't also silence the day its cover art goes missing - the album comes
    back on its own if something new is wrong with it.
    """
    store = _store(request)

    if store is None or not store.available:
        raise HTTPException(status_code=503, detail="ignores can't be saved, the job store isn't available")

    if not await store.ignore_album_issues(body.album_path, body.issues):
        raise HTTPException(status_code=500, detail="could not save that")

    logger.info(
        f"ignoring {len(body.issues)} metadata issue(s) on {body.album_path}",
        extra={"frontend": True},
    )

    return {"ignored": True, "album_path": body.album_path, "issues": sorted(set(body.issues))}


@router.post("/queue/unignore")
async def unignore(request: Request, body: QueueRequest):
    """Put an album back in the queue after it was ignored."""
    store = _store(request)

    if store is None or not store.available:
        raise HTTPException(status_code=503, detail="the job store isn't available")

    await store.unignore_album(body.album_path)
    return {"ignored": False, "album_path": body.album_path}


@router.post("/queue/reviewed")
async def reviewed(request: Request, body: QueueRequest):
    """
    Note that you've looked at an album, which is what clears it from the new-import prompt.

    Deliberately NOT the same thing as ignoring it. Reviewing says "I've seen this"; the album
    keeps whatever issues it has and stays in the queue, because a queue that empties when you
    glance at things is a queue that lies. This exists so the prompt for a freshly imported
    album can stop being a prompt.
    """
    store = _store(request)

    if store is None or not store.available:
        raise HTTPException(status_code=503, detail="the job store isn't available")

    await store.mark_album_reviewed(body.album_path)
    return {"reviewed": True, "album_path": body.album_path}


class DeleteRequest(BaseModel):
    #? relative to LIBRARY_PATH, as the scan reports it
    album_path: str


@router.post("/delete")
async def delete(request: Request, body: DeleteRequest):
    """
    Remove an album folder and everything in it. Permanent.

    A POST rather than a DELETE verb only because the body carries a path; the guards are in
    src/library.py::delete_album, which is where they belong - this endpoint deliberately
    adds no judgement of its own beyond refusing when LIBRARY_PATH isn't set.

    Refusals come back as 400 with a reason rather than as a 404, because unlike the art
    endpoint there is nothing to hide here: you are deleting your own library, and being told
    "that folder holds no audio" is more useful than silence.
    """
    if not Config.LIBRARY_PATH:
        raise HTTPException(status_code=400, detail="LIBRARY_PATH is not set")

    try:
        result = await asyncio.to_thread(delete_album, Config.LIBRARY_PATH, body.album_path)

        if not result["deleted"]:
            raise HTTPException(status_code=400, detail=result["problem"])

        #? delete_album forgot the folder; this makes the saved scan forget it too, or a restart
        #? would draw a deleted album until the next scan noticed it was gone
        await _persist_cache(request)

        return result

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /delete endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting the album: {e}")


@router.get("/deletion_summary")
async def deletion_summary(album: str):
    """
    What deleting this album would remove. Touches nothing.

    Read live rather than from the scan so the confirmation describes the folder as it is
    now, and so it can count files the scan ignores - a rip log or a cue sheet in there is
    worth knowing about before it goes.
    """
    root_path = Config.LIBRARY_PATH or ""

    if not root_path or not album:
        raise HTTPException(status_code=400, detail="no album given")

    root = Path(root_path)
    directory = root / album

    if not is_within(directory, root) or not directory.is_dir():
        raise HTTPException(status_code=400, detail="that album is not inside the library")

    return await asyncio.to_thread(summarize_for_deletion, directory)
