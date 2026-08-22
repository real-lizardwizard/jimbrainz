/**
 * The jimbrainz backend, in types.
 *
 * Derived from the server's own definitions, NOT from the current JS - the vanilla frontend
 * reads some fields loosely and omits others entirely, so copying it would enshrine its
 * guesses. Canonical sources:
 *
 *   src/routes/download.py     pydantic request models + the /jobs response assembly
 *   src/matching.py            score_candidate() - the candidate shape and its signals
 *   src/store.py               job statuses, summarize_transfers()
 *   src/routes/monitor_slskd.py, src/routes/search_musicbrainz.py
 *
 * If you change one of those, change this. Nothing enforces the correspondence.
 */

/* ===== download ===== */

/**
 * Statuses a job can hold. `complete` means downloaded; `organized` means filed into the
 * library. The split matters because organizing is the only step that writes to the user's
 * filesystem and can be off (ORGANIZE_MODE=dry_run) while downloads still succeed.
 */
export type JobStatus =
  | 'queued'
  | 'downloading'
  | 'complete'
  | 'organizing'
  | 'organized'
  | 'failed'
  | 'cancelled'

/** src/store.py OPEN_STATUSES, plus organizing - see ACTIVE_STATUSES in ../lib/jobs.ts */
export type ClearableStatus = Extract<
  JobStatus,
  'complete' | 'organized' | 'failed' | 'cancelled'
>

export interface Track {
  position: number | null
  title: string
  length_ms: number | null
}

/**
 * One row of GET /download/jobs: the stored job merged with live progress from slskd.
 *
 * Progress fields are derived per request rather than persisted (slskd owns them and they
 * change every second), so they are present but meaningless once a job reaches a terminal
 * status.
 */
export interface DownloadJob {
  id: number
  artist: string
  album: string
  year: string | null
  username: string
  directory: string
  status: JobStatus
  error: string | null
  created_at: string

  /** Position in the peer's upload queue. Only ever set for `queued` jobs slskd knows about. */
  queue_position: number | null

  /** 0-100, averaged over the files we asked for rather than the ones slskd reports. */
  progress: number
  /** slskd's raw transfer state string, e.g. "Completed, Succeeded". null when unmatched. */
  state: string | null
  /**
   * slskd's cumulative averageSpeed, summed. This is total bytes over total elapsed, so it
   * only ever creeps upward and never shows the current rate - do NOT render it as a speed.
   * Real speed comes from bytes_transferred deltas between polls (see ../lib/speed.ts).
   */
  speed: number
  bytes_transferred: number
  files_done: number
  files_total: number
  /**
   * Files that have stopped for good without arriving — refused, timed out, errored or
   * cancelled. Absent on terminal jobs, where the stored status is the answer instead.
   *
   * Every one of slskd's "Completed, <substate>" strings other than Succeeded counts here.
   * That matters: a substate nothing recognises is a file that will never move and a job that
   * will never leave `queued` — see TRANSFER_FAILURE_REASONS in src/store.py.
   */
  files_failed?: number
  /** Why they stopped, in words, or null. Files in a folder fail together, so it's one reason. */
  failure_reason?: string | null
  /** Whether slskd currently knows about any of this job's files. */
  matched: boolean
}

export interface JobsResponse {
  jobs: DownloadJob[]
  /**
   * False when the SQLite store could not be opened. Downloads still work - they're just
   * untracked - so this is a banner, not an error state.
   */
  tracking_enabled: boolean
}

export type FormatPreference = 'prefer_lossless' | 'lossless_only' | 'any'

/** Weighted signals from src/matching.py WEIGHTS. null means "no opinion", not zero. */
export interface CandidateSignals {
  title_match: number | null
  track_count: number | null
  duration_match: number | null
  edition: number | null
  format: number | null
  peer: number | null
}

export type SignalName = keyof CandidateSignals

export interface CandidateFile {
  filename: string
  size: number
}

/** POST /download/find_candidates, after _serialize_candidate() trims it. */
export interface Candidate {
  username: string
  directory: string
  directory_name: string
  score: number
  signals: CandidateSignals
  matched_tracks: number
  expected_tracks: number
  audio_file_count: number
  detected_edition_tags: string[]
  formats: string[]
  /**
   * The peer's average upload speed over their whole history, in BYTES/sec.
   *
   * NOT the rate a transfer from them will run at, and it should never be labelled as though
   * it were. Soulseek reports it per user, so it is split between everyone they are serving
   * at once and averaged over conditions that no longer apply — it reads high far more often
   * than it matches. `has_free_slot` and `queue_length` predict the real thing far better.
   *
   * Bytes, not bits — slskd's own web UI renders this field as `formatBytes(uploadSpeed)/s`,
   * and src/matching.py's peer thresholds only make sense as bytes. Do not divide by eight.
   */
  upload_speed: number
  queue_length: number
  has_free_slot: boolean
  total_size: number
  bitrates: number[]
  files: CandidateFile[]
}

export interface FindCandidatesRequest {
  artist: string
  album: string
  year?: string | null
  release_mbid?: string | null
  edition_tags?: string[]
  tracks?: Track[]
  format_preference?: FormatPreference
  /** Lets the UI re-run a tweaked query when the generated one finds nothing. */
  query_override?: string | null
}

export interface FindCandidatesResponse {
  query: string
  response_count: number
  candidates: Candidate[]
}

export interface EnqueueRequest {
  username: string
  files: CandidateFile[]
  directory?: string
  /** Persisted denormalized with the job so organizing later needs no MusicBrainz call. */
  release?: {
    artist?: string
    album?: string
    year?: string | null
    release_mbid?: string | null
    edition_tags?: string[]
    tracks?: Track[]
  }
}

export interface EnqueueResponse {
  status: string
  queued: number
  job_id: number
}

export interface CancelJobResponse {
  status: string
  cancelled: number
}

export interface ClearJobsResponse {
  status: string
  removed: number
}

/* ===== monitor_slskd ===== */

export interface ServerConfig {
  library_path: string
  download_path: string
  organizing_enabled: boolean
  organize_mode: string
  /** Reported back deliberately - a LAN address, not a secret. The API key never is. */
  slskd_url: string
  /** Human-readable explanation of why the URL is unusable, or null when it's fine. */
  slskd_url_problem: string | null
  slskd_apikey_set: boolean
}

export interface PingResponse {
  status: string
  code: number
}

/* ===== search_musicbrainz ===== */

/**
 * MusicBrainz shapes are passed through untouched, so these cover the fields the interface
 * actually reads rather than the full schema. The index signature is the escape hatch for
 * everything else - keep new reads typed by adding them here instead of casting.
 */
export interface ArtistCredit {
  name?: string
  artist?: { id?: string; name?: string }
}

export interface Medium {
  'track-count'?: number
  format?: string
}

export interface ReleaseEvent {
  date?: string
  area?: { 'iso-3166-1-codes'?: string[] }
}

export interface LabelInfo {
  'catalog-number'?: string
  label?: { name?: string }
}

export interface Release {
  id: string
  title?: string
  date?: string
  country?: string
  status?: string
  packaging?: string
  disambiguation?: string
  'artist-credit'?: ArtistCredit[]
  media?: Medium[]
  'label-info'?: LabelInfo[]
  'release-events'?: ReleaseEvent[]
  'text-representation'?: { language?: string; script?: string }
  [key: string]: unknown
}

export interface ReleaseGroup {
  id: string
  title?: string
  'first-release-date'?: string
  'primary-type'?: string
  'secondary-types'?: string[]
  'artist-credit'?: ArtistCredit[]
  [key: string]: unknown
}

/**
 * GET /search_musicbrainz/fully_search.
 *
 * Both keys are absent when the query genuinely matched nothing - the endpoint returns `{}`.
 * That is NOT the same as MusicBrainz being unreachable, which is a 503 and arrives as a
 * MusicBrainzUnavailable error. Conflating the two is what made outages look like bad
 * searches; keep them apart.
 */
export interface FullySearchResponse {
  'release-groups'?: ReleaseGroup[]
  'best-match-releases'?: Release[]
}

export interface ReleasesResponse {
  id: string
  releases: Release[]
}

/* ===== library ===== */

export interface LibraryTrack {
  filename: string
  /** Falls back to the filename stem when untagged — see `has_title_tag` before trusting it. */
  title: string
  /**
   * Whether `title` came from a tag or from the filename. Recorded by the scan because the
   * fallback is invisible afterwards: a stem like "01 - Shine On" is very close to what a real
   * title looks like.
   */
  has_title_tag: boolean
  /** null when the file carries no readable track number. Sorts last rather than as 0. */
  position: number | null
  /** seconds */
  length: number
  size: number
  format: string
  artist: string
  album: string
  albumartist: string
  date: string
  release_mbid: string
}

export interface LibraryAlbum {
  /**
   * Stable identity. The MusicBrainz release id when the files carry one, otherwise
   * `path:<relative dir>`. Two folders sharing an MBID are the same edition however they're
   * named; a folder without one can only ever be itself.
   */
  key: string
  artist: string
  album: string
  year: string
  /** From the `originaldate` tag. '' for anything not carrying one, which is most libraries. */
  original_year: string
  /** Human label ("Deluxe edition"). '' for a single-edition album, 'Standard' when it has siblings. */
  edition: string
  release_mbid: string
  /**
   * Where cover art lives on disk, if anywhere: 'file' (an image beside the tracks),
   * 'embedded' (inside the audio), or '' (neither — fall back to the Cover Art Archive when
   * there's a release_mbid). Detected during the scan so the UI doesn't fire requests that
   * are certain to 404.
   */
  art: 'file' | 'embedded' | ''
  /** Relative to LIBRARY_PATH. Also the key the art endpoint takes. */
  path: string
  /**
   * When the cover last changed, as a unix timestamp — the ART file's mtime, not the album's.
   *
   * Used to version the art URL. The endpoint caches for five minutes, so without this a
   * replaced cover keeps serving the old bytes from the browser cache exactly when you're
   * looking at it. See albumArtUrl in ../lib/format.ts.
   */
  art_mtime: number
  track_count: number
  total_size: number
  /** seconds */
  duration: number
  formats: string[]
  tracks: LibraryTrack[]
  modified_at: number
  /** The folder's files disagree about the album name — usually a real tagging problem. */
  mixed_tags: boolean
  /** How many files carry no title tag. Non-zero raises the `untitled_tracks` issue. */
  untitled_tracks: number
  /** How many versions of this (artist, album) are on disk. 1 is the ordinary case. */
  edition_count: number

  /* ----- the metadata queue. Attached by src/metadata_health.py::attach_issues ----- */

  /**
   * Every issue code found, worst first. Describe them with `LibraryResponse.issue_types`
   * rather than mapping codes to words here — the server ships the labels precisely so there
   * isn't a second copy of this vocabulary to drift out of step.
   *
   * Note this is everything DETECTED, including issues that have been ignored. Use
   * `needs_attention` to decide whether the album is in the queue.
   */
  issues: string[]
  /** Issues you've said you're happy with. Still reported above, just no longer counted. */
  ignored_issues: string[]
  /** True when `issues` holds something not in `ignored_issues`. This is queue membership. */
  needs_attention: boolean
  /** Severity of the worst OUTSTANDING issue: 3 unidentified, 2 wrong, 1 cosmetic, 0 none. */
  severity: number
  /** ISO timestamp of when the album was first recorded, or null when nothing remembers it. */
  first_seen: string | null
  /** jimbrainz filed this album rather than finding it. What the new-import prompt counts. */
  imported: boolean
  /** You've looked at it since. Doesn't clear its issues — see /queue/reviewed. */
  reviewed: boolean
}

/** One kind of metadata problem, as src/metadata_health.py::ISSUE_TYPES describes it. */
export interface MetadataIssueType {
  /** Short chip text, e.g. "no release id". */
  label: string
  /** A sentence saying what it means and what fixes it. */
  hint: string
  /** 3 = the album isn't identified at all, 2 = wrong or incomplete, 1 = cosmetic. */
  severity: number
}

/** What the queue holds right now, counted server-side over the whole library. */
export interface MetadataQueueSummary {
  /** Albums with at least one outstanding issue. */
  total: number
  /** Outstanding count per issue code. Ignored issues are excluded. */
  by_issue: Record<string, number>
  /** Albums jimbrainz filed that haven't been looked at yet. */
  new_imports: number
  ignored_albums: number
}

export interface LibraryArtist {
  artist: string
  album_count: number
  track_count: number
  total_size: number
}

export interface LibraryResponse {
  albums: LibraryAlbum[]
  artists: LibraryArtist[]
  library_path: string
  /** Set when the library can't be read at all (unset or missing LIBRARY_PATH). Not an error. */
  problem: string | null
  scanned_at: number
  album_count: number
  artist_count: number
  scan_seconds: number
  /** How many folders came from cache rather than being re-read. */
  cached: number
  /** The metadata queue, derived from this scan rather than stored. */
  queue: MetadataQueueSummary
  /** Every issue code the server can raise, keyed by code. The labels the UI renders. */
  issue_types: Record<string, MetadataIssueType>
  /**
   * False when the SQLite store couldn't be opened. The queue still works — it just stops
   * remembering what you ignored, the same way downloads still work untracked.
   */
  review_tracking_enabled: boolean
}

/**
 * GET /library/queue/new_imports — albums jimbrainz filed that you haven't looked at.
 *
 * The only part of the queue answerable without a scan, which is why it's a separate endpoint:
 * the library isn't read until its tab is opened, so a prompt that needed a scan would either
 * be missing when it mattered or would tax every page load.
 */
export interface NewImportsResponse {
  count: number
  /** Capped by the server — this names what's waiting, it isn't a second album list. */
  albums: { album_path: string; artist: string; album: string; first_seen: string }[]
  tracking_enabled: boolean
}

/* ===== library: retagging ===== */

/**
 * The release to apply to an album already on disk.
 *
 * Same shape the download path stores with a job, deliberately: an album corrected by hand
 * should end up carrying exactly the tags one downloaded fresh would have.
 */
export interface RetagRelease {
  artist: string
  album: string
  /** This pressing's year. Written to the `date` tag. */
  year?: string | null
  /**
   * The ALBUM's year — MusicBrainz's release-group `first-release-date`. This is what names
   * the folder, so a 2011 remaster of a 1975 record still files under 1975. Written to
   * `originaldate`.
   */
  original_year?: string | null
  release_mbid?: string | null
  release_group_mbid?: string | null
  disambiguation?: string | null
  media_format?: string | null
  country?: string | null
  catalog_number?: string | null
  /** Overrides every derived edition name. This is what "pick which edition this is" writes. */
  edition_label?: string | null
  edition_tags?: string[]
  tracks?: Track[]
}

export interface RetagFileChange {
  filename: string
  /** False when the tracklist didn't reach this file; it keeps its own title and number. */
  matched: boolean
  track_title: string
  track_position: number | null
  /** Only the tags that would actually change, keyed by tag name. */
  changes: Record<string, { from: string; to: string }>
}

/**
 * What applying a release would do. Produced by /retag/preview, which writes nothing.
 *
 * The apply endpoint recomputes this rather than accepting it back, so this is a faithful
 * report rather than an instruction — you cannot edit it into doing something else.
 */
/** What a retag would do about cover art. Decided without a network call — see plan_art. */
export interface RetagArtPlan {
  /** '' = nothing, 'download' = fetch and save, 'replace' = overwrite the existing file. */
  action: '' | 'download' | 'replace'
  /** Why nothing will happen, when that needs saying. */
  reason: string
  /** The cover file already in the folder, if any. */
  existing: string | null
}

export interface RetagPlan {
  album_path: string
  /** null when the album couldn't be read at all; `problems` says why. */
  source: string | null
  target: string | null
  target_path: string | null
  moves: boolean
  edition_label: string
  files: RetagFileChange[]
  changed_file_count: number
  file_count: number
  matched_tracks: number
  expected_tracks: number
  art: RetagArtPlan
  /** Non-fatal warnings worth showing before applying — mismatched track counts and so on. */
  problems: string[]
  /** Nothing to change. The album already carries this release. */
  empty: boolean
}

export interface RetagResults {
  mode: string
  dry_run: boolean
  tagged: number
  failed: number
  moved_to: string | null
  /** The cover filename written, or null. */
  art_written: string | null
  problems: string[]
}

export interface RetagResponse {
  plan: RetagPlan
  results: RetagResults
}

/** What deleting an album would remove. Read live, not from the scan. */
export interface DeletionSummary {
  audio_files: number
  total_bytes: number
  /** Named individually — these are the files that might not exist anywhere else. Capped. */
  other_files: string[]
  other_file_count: number
}

export interface DeleteResult extends DeletionSummary {
  deleted: boolean
  problem: string | null
}

/* ===== interface_logs ===== */

export type LogSource = 'musicbrainz' | 'slskd' | 'jimbrainz' | (string & {})

/** Python levelnames - src/logger.py sends record.levelname straight through. */
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL' | (string & {})

/**
 * One SSE frame from GET /interface_logs/interface_logs.
 *
 * Note this differs from the table in docs/FRONTEND-MIGRATION.md, which was written from the
 * endpoint signature rather than the emitter: SSEHandler.emit() in src/logger.py also sends
 * `event_time`, and only attaches `src` when the log record carried one. Frames without a
 * `src` are real and must render.
 */
export interface LogEvent {
  /** Pre-formatted "HH:MM:SS" from the server. Not a parseable timestamp. */
  event_time: string
  event_type: LogLevel
  event_content: string
  src?: LogSource
}
