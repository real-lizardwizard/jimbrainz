import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.slskd_endpoint import build_search_query
from src.config import Config
from src.logger import logger
from src.matching import rank_candidates
from src.organizer import remove_incomplete_downloads
from src.store import (CLEARABLE_STATUSES, OPEN_STATUSES, index_transfers_by_user,
                       summarize_transfers)

router = APIRouter()


class Track(BaseModel):
    position: int | None = None
    title: str = ""
    length_ms: int | None = None


class FindCandidatesRequest(BaseModel):
    artist: str
    album: str
    year: str | None = None
    #? the release GROUP's first-release-date, i.e. when the album came out rather than when
    #? this pressing did. Names the folder; see build_album_dirname.
    original_year: str | None = None
    release_mbid: str | None = None
    edition_tags: list[str] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)
    format_preference: str = "prefer_lossless"
    #? lets the UI re-run a tweaked query when the generated one finds nothing
    query_override: str | None = None


class EnqueueFile(BaseModel):
    filename: str
    size: int


class EnqueueRelease(BaseModel):
    """What the download is *for*. Persisted with the job so organizing it later needs no network."""
    artist: str = ""
    album: str = ""
    year: str | None = None
    #? the release GROUP's first-release-date, i.e. when the album came out rather than when
    #? this pressing did. Names the folder; see build_album_dirname.
    original_year: str | None = None
    release_mbid: str | None = None
    edition_tags: list[str] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)

    #? Everything below feeds src/editions.py, which works out the folder an edition is filed
    #? into. Stored with the job rather than looked up at organize time on purpose: the same
    #? reason the tracklist is denormalized here, namely that MusicBrainz is regularly
    #? unreachable and filing a finished download must not depend on it.
    release_group_mbid: str | None = None
    #? MusicBrainz's own edition descriptor ("deluxe edition", "2011 remaster"). The single
    #? best source for naming an edition, because it is the field editors use to tell
    #? releases apart in the first place.
    disambiguation: str | None = None
    media_format: str | None = None
    country: str | None = None
    catalog_number: str | None = None
    #? An explicit override that beats every derived source. Nothing sets it yet - it is the
    #? hook for a future metadata manager, so that picking an edition by hand is a matter of
    #? writing this field rather than changing how editions are resolved.
    edition_label: str | None = None


class EnqueueRequest(BaseModel):
    username: str
    files: list[EnqueueFile]
    directory: str = ""
    release: EnqueueRelease = Field(default_factory=EnqueueRelease)


@router.post("/find_candidates")
async def find_candidates(request: Request, body: FindCandidatesRequest):
    """
    Search Soulseek for the release the user picked and rank what comes back against it.

    The ranking is the whole point - see src/matching.py. Candidates that don't match the
    requested edition are ranked down but deliberately still returned, since Soulseek folder
    names often omit edition text entirely and filtering would hide real results.
    """
    try:
        slskd_client = request.app.state.slskd_client
        query = body.query_override or build_search_query(body.artist, body.album)

        if not query:
            raise HTTPException(status_code=400, detail="Could not build a search query")

        responses = await slskd_client.search(query)

        expected = {
            "artist": body.artist,
            "album": body.album,
            "year": body.year,
            "edition_tags": body.edition_tags,
            "tracks": [t.model_dump() for t in body.tracks],
        }

        candidates = rank_candidates(responses, expected, body.format_preference)

        if not candidates:
            logger.warning(
                f"no usable candidates for: {query}",
                extra={"frontend": True, "src": "slskd"},
            )

        else:
            logger.info(
                f"ranked {len(candidates)} candidates, best score {candidates[0]['score']}",
                extra={"frontend": True, "src": "slskd"},
            )

        serialized = [_serialize_candidate(c) for c in candidates]
        await _attach_measured_speeds(request, serialized)

        return {
            "query": query,
            "response_count": len(responses),
            "candidates": serialized,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /find_candidates endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error searching slskd: {e}")


async def _attach_measured_speeds(request: Request, candidates: list[dict]) -> None:
    """
    Add what we have actually measured from each peer, where we have measured anything.

    Separate from _serialize_candidate because that one is pure and this one reads the
    database, and because it is one query for the whole list rather than one per candidate.

    Failure here is silent by design. Most peers have never been downloaded from, so a
    candidate with no measurement is the ordinary case and already renders correctly - which
    means a degraded read looks exactly like a peer nobody has met, rather than like an error.
    The advertised figure and the slot/queue chips are all still there.
    """
    if not candidates:
        return

    try:
        measured = await request.app.state.store.peer_speeds(
            [c["username"] for c in candidates]
        )

    except Exception as e:
        logger.debug(f"could not read measured peer speeds: {e}")
        return

    for candidate in candidates:
        row = measured.get(candidate["username"])
        if not row:
            continue

        candidate["measured_speed"] = row["avg_bytes_sec"]
        #? How many transfers that average stands on, so the UI can distinguish "one lucky
        #? download" from a settled figure instead of presenting both as equally certain.
        candidate["measured_samples"] = row["samples"]
        candidate["measured_at"] = row["last_seen"]


def _serialize_candidate(candidate: dict) -> dict:
    """
    Strip the bits the UI doesn't need. `track_mapping` in particular holds whole file dicts
    and gets big; it's recomputed server-side when the organizer needs it (phase 3).
    """
    return {
        "username": candidate["username"],
        "directory": candidate["directory"],
        "directory_name": candidate["directory_name"],
        "score": candidate["score"],
        "signals": candidate["signals"],
        "matched_tracks": candidate["matched_tracks"],
        "expected_tracks": candidate["expected_tracks"],
        "audio_file_count": candidate["audio_file_count"],
        "detected_edition_tags": candidate["detected_edition_tags"],
        "formats": candidate["formats"],
        "upload_speed": candidate["upload_speed"],
        "queue_length": candidate["queue_length"],
        "has_free_slot": candidate["has_free_slot"],
        "total_size": candidate["total_size"],
        "bitrates": candidate["bitrates"],
        "files": [
            {"filename": f["filename"], "size": f.get("size", 0)}
            for f in candidate["files"]
        ],
    }


@router.post("/enqueue")
async def enqueue(request: Request, body: EnqueueRequest):
    try:
        slskd_client = request.app.state.slskd_client
        files = [f.model_dump() for f in body.files]

        ok = await slskd_client.enqueue(body.username, files)

        if not ok:
            raise HTTPException(status_code=502, detail="slskd refused the download")

        #? record only after slskd accepts, so a rejected download never leaves a phantom job
        job_id = await request.app.state.store.create_job(
            username=body.username,
            directory=body.directory,
            files=files,
            release=body.release.model_dump(),
        )

        return {"status": "ok", "queued": len(files), "job_id": job_id}

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /enqueue endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error queueing download: {e}")


@router.get("/jobs")
async def jobs(request: Request):
    """
    Tracked download jobs, merged with live progress from slskd.

    Progress is computed per request rather than stored - slskd is the source of truth for
    it and it changes every second, so persisting it would be churn for no benefit.
    """
    try:
        store = request.app.state.store
        stored = await store.list_jobs()

        if not stored:
            return {"jobs": [], "tracking_enabled": store.available}

        transfers_by_user = {}
        if any(j["status"] in OPEN_STATUSES for j in stored):
            downloads = await request.app.state.slskd_client.get_downloads()
            transfers_by_user = index_transfers_by_user(downloads)

        merged = []
        for job in stored:
            summary = (summarize_transfers(job, transfers_by_user)
                       if job["status"] in OPEN_STATUSES
                       else {"progress": 100.0 if job["status"] != "failed" else 0.0,
                             "state": None, "speed": 0, "bytes_transferred": 0,
                             "files_done": len(job["files"]) if job["status"] != "failed" else 0,
                             "files_total": len(job["files"]), "matched": False})

            queue_position = None
            if job["status"] == "queued" and summary.get("matched"):
                #? only for jobs actually sat in a queue, and only the first file - slskd
                #? answers this one transfer at a time, so asking for every file every poll
                #? would hammer it for information that's identical across the folder anyway
                queue_position = await _first_queue_position(
                    request.app.state.slskd_client, job, transfers_by_user
                )

            merged.append({
                "id": job["id"],
                "artist": job["artist"],
                "album": job["album"],
                "year": job["year"],
                "username": job["username"],
                "directory": job["directory"],
                "status": job["status"],
                "error": job["error"],
                "created_at": job["created_at"],
                "queue_position": queue_position,
                **summary,
            })

        return {"jobs": merged, "tracking_enabled": store.available}

    except Exception as e:
        logger.error(f"Exception in /jobs endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching download jobs: {e}")


@router.get("/downloads")
async def downloads(request: Request):
    """Raw slskd transfer list, untouched. Handy for debugging what the poller is seeing."""
    try:
        slskd_client = request.app.state.slskd_client
        return {"downloads": await slskd_client.get_downloads()}

    except Exception as e:
        logger.error(f"Exception in /downloads endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching downloads: {e}")


async def _first_queue_position(slskd_client, job: dict, transfers_by_user: dict) -> int | None:
    wanted = {f["filename"] for f in job["files"]}

    for transfer in transfers_by_user.get(job["username"], []):
        if transfer.get("filename") in wanted and transfer.get("id"):
            return await slskd_client.queue_position(job["username"], transfer["id"])

    return None


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int):
    """
    Stop a download and mark the job cancelled.

    Cancels every transfer slskd currently holds for this job.

    Anything already filed into the library stays. The half-finished file in slskd's INCOMPLETE
    folder is removed only when SLSKD_INCOMPLETE_PATH says where that folder is - slskd keeps
    partials deliberately so a retried download can resume from them, so pointing jimbrainz at
    it is the explicit "no, a cancelled download is finished with" signal.
    """
    try:
        store = request.app.state.store
        job = await store.get_job(job_id)

        if job is None:
            raise HTTPException(status_code=404, detail="No such download job")

        slskd_client = request.app.state.slskd_client
        downloads = await slskd_client.get_downloads()
        transfers_by_user = index_transfers_by_user(downloads)

        wanted = {f["filename"] for f in job["files"]}
        cancelled = 0

        for transfer in transfers_by_user.get(job["username"], []):
            if transfer.get("filename") in wanted and transfer.get("id"):
                if await slskd_client.cancel_download(job["username"], transfer["id"]):
                    cancelled += 1

        await store.update_status(job_id, "cancelled", "cancelled from jimbrainz")

        #? After the transfers are cancelled, never before: slskd holds the file open while a
        #? transfer is live, and deleting it underneath would be a race with slskd's own writer.
        removed = await asyncio.to_thread(
            remove_incomplete_downloads,
            Config.SLSKD_INCOMPLETE_PATH or "",
            job["files"],
            job.get("directory", ""),
        )

        logger.info(
            f"cancelled {job['artist']} - {job['album']} ({cancelled} transfer(s))"
            + (f", removed {len(removed['removed'])} partial file(s)" if removed["removed"] else ""),
            extra={"frontend": True, "src": "slskd"},
        )

        for skipped in removed["skipped"]:
            logger.warning(
                f"left a partial file in place: {skipped}",
                extra={"frontend": True, "src": "slskd"},
            )

        return {
            "status": "ok",
            "cancelled": cancelled,
            "partials_removed": len(removed["removed"]),
            #? surfaced rather than swallowed so "why is my incomplete folder still full" has
            #? an answer in the response as well as the log
            "partials_problem": removed["problem"],
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Exception in /jobs/{job_id}/cancel: {e}")
        raise HTTPException(status_code=500, detail=f"Error cancelling download: {e}")


@router.post("/jobs/clear")
async def clear_jobs(request: Request):
    """Forget finished jobs. Anything still moving is deliberately left alone."""
    try:
        removed = await request.app.state.store.delete_jobs(CLEARABLE_STATUSES)
        return {"status": "ok", "removed": removed}

    except Exception as e:
        logger.error(f"Exception in /jobs/clear: {e}")
        raise HTTPException(status_code=500, detail=f"Error clearing jobs: {e}")
