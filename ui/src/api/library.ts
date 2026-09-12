import { get, post } from './http'
import type {
  DeleteResult, DeletionSummary, LibraryResponse, NewImportsResponse, RetagPlan, RetagRelease,
  RetagResponse, TrackDetailsResponse,
} from './types'

/**
 * Everything currently in LIBRARY_PATH.
 *
 * The server caches per folder on mtime, so this is cheap to call again — but the first scan
 * of a large library reads tags off every file and can take seconds. Treat it as a load, not
 * a poll.
 *
 * `snapshot` answers from the saved scan without touching the disk, marked `stale`, and falls
 * through to a real scan when nothing has been saved yet. It is what makes the tab open at once.
 */
export function listAlbums(options: { snapshot?: boolean } = {}): Promise<LibraryResponse> {
  return get<LibraryResponse>(options.snapshot ? '/library/albums?snapshot=true' : '/library/albums')
}

/**
 * Every tag on every file in one album, read from disk now. For the track viewer.
 *
 * Deliberately not part of the scan: the scan is the whole library in one payload, and thirty
 * tags a track across thousands of tracks would tax every visit for detail shown one album at
 * a time.
 */
export function trackDetails(albumPath: string): Promise<TrackDetailsResponse> {
  return get<TrackDetailsResponse>(`/library/tracks?album=${encodeURIComponent(albumPath)}`)
}

/** Drop the server's per-folder cache and read everything again. */
export function rescan(): Promise<LibraryResponse> {
  return post<LibraryResponse>('/library/rescan')
}

/**
 * What applying this release to that album would change. Writes nothing.
 *
 * A separate endpoint from apply rather than a flag on it — this runs while you're still
 * choosing, so it must be impossible for it to modify anything by accident.
 */
export function previewRetag(
  albumPath: string,
  release: RetagRelease,
  fetchArt = false,
): Promise<RetagPlan> {
  return post<RetagPlan>('/library/retag/preview', {
    album_path: albumPath, release, fetch_art: fetchArt,
  })
}

/**
 * Write the tags and re-file the folder.
 *
 * The server recomputes the plan rather than taking the previewed one back, so this sends
 * the same two arguments the preview did — a plan is a list of file operations, and handing
 * one over the wire would let a caller name arbitrary paths to write to.
 */
export function applyRetag(
  albumPath: string,
  release: RetagRelease,
  fetchArt = false,
): Promise<RetagResponse> {
  return post<RetagResponse>('/library/retag/apply', {
    album_path: albumPath, release, fetch_art: fetchArt,
  })
}

/* ===== the metadata queue ===== */

/**
 * Albums jimbrainz has filed that you haven't looked at yet.
 *
 * Cheap on purpose — one indexed table read, no filesystem. Safe to call on page load, which
 * `listAlbums` deliberately is not.
 */
export function newImports(): Promise<NewImportsResponse> {
  return get<NewImportsResponse>('/library/queue/new_imports')
}

/**
 * Accept an album as it is, so it drops out of the queue.
 *
 * The issue codes are sent rather than left to the server to work out, so this can only ever
 * mute the problems that were actually on screen. An album that develops a *different* problem
 * later comes back into the queue on its own.
 */
export function ignoreIssues(albumPath: string, issues: string[]): Promise<{ ignored: boolean }> {
  return post('/library/queue/ignore', { album_path: albumPath, issues })
}

/** Put an ignored album back into the queue. */
export function unignoreAlbum(albumPath: string): Promise<{ ignored: boolean }> {
  return post('/library/queue/unignore', { album_path: albumPath })
}

/**
 * Note that you've looked at an album.
 *
 * Clears it from the new-import prompt and nothing else — its issues stand, because a queue
 * that empties when you glance at things is a queue that lies.
 */
export function markReviewed(albumPath: string): Promise<{ reviewed: boolean }> {
  return post('/library/queue/reviewed', { album_path: albumPath })
}

/**
 * Put a cover in an album's folder, changing nothing else about it.
 *
 * The narrow counterpart to `applyRetag`. That one rewrites every file's tags and can rename
 * the folder — a lot to agree to when the only thing missing is the picture. This asks the
 * Cover Art Archive for art belonging to the release the album's own tags already name, so
 * there is nothing to choose and nothing to preview.
 *
 * `replace` is off by default: a sleeve you picked yourself is not ours to overwrite because
 * the Archive happens to have one too.
 */
export function fetchCoverArt(
  albumPath: string,
  options: { replace?: boolean; releaseMbid?: string | null } = {},
): Promise<{ written: string | null; replaced: string | null }> {
  return post('/library/art/fetch', {
    album_path: albumPath,
    replace: options.replace ?? false,
    //? omitted for the plain path, where the release comes from the album's own tags. The
    //? editor sends one because it is showing you that release's cover as you decide.
    release_mbid: options.releaseMbid ?? null,
  })
}

/** What deleting this album would remove. Touches nothing. */
export function deletionSummary(albumPath: string): Promise<DeletionSummary> {
  return get<DeletionSummary>(`/library/deletion_summary?album=${encodeURIComponent(albumPath)}`)
}

/**
 * Delete an album folder and everything in it. Permanent, and the only call in here with no
 * undo — the guards that make it safe live in src/library.py::delete_album.
 */
export function deleteAlbum(albumPath: string): Promise<DeleteResult> {
  return post<DeleteResult>('/library/delete', { album_path: albumPath })
}
