import { useCallback, useEffect, useRef, useState } from 'preact/hooks'

import * as api from '../api/library'
import type {
  LibraryAlbum, LibraryArtist, LibraryResponse, MetadataIssueType, MetadataQueueSummary,
} from '../api/types'

/** Nothing needing attention, for before the first load finishes. */
const EMPTY_QUEUE: MetadataQueueSummary = {
  total: 0, by_issue: {}, new_imports: 0, ignored_albums: 0,
}

export interface LibraryState {
  albums: LibraryAlbum[]
  artists: LibraryArtist[]
  /** Counts for the queue facets. Derived server-side from the same scan as `albums`. */
  queue: MetadataQueueSummary
  /** Issue code -> label and hint. The server owns this vocabulary; see api/types.ts. */
  issueTypes: Record<string, MetadataIssueType>
  /** False when ignores can't be saved because the SQLite store couldn't be opened. */
  reviewTracking: boolean
  /** Set when the library can't be read at all — unset/missing LIBRARY_PATH. Not an error. */
  problem: string | null
  /** Set when the request itself failed. */
  error: string | null
  loading: boolean
  /** False until the first load finishes, so the empty state doesn't flash before data. */
  loaded: boolean
  /**
   * True while what's on screen is the SAVED scan rather than the disk. It is drawn at once
   * and a real scan runs underneath it; this goes false when that scan lands.
   */
  stale: boolean
  /** Unix seconds when the disk was last read — the snapshot's age, or this scan's time. */
  scannedAt: number | null
  scanSeconds: number
  libraryPath: string
  /**
   * Refetch. Resolves with the fresh album list, so a caller holding on to one album (the
   * metadata editor does) can find its new state rather than keeping a stale copy.
   */
  reload: (force?: boolean) => Promise<LibraryAlbum[]>
}

type LoadMode = 'snapshot' | 'scan' | 'rescan'

/**
 * Load the library, once, when it's first needed.
 *
 * `enabled` is the Library tab being open. A first scan reads tags off every file in the
 * library, so doing it on page load would tax people who never open this tab - and doing it
 * on a timer would tax everyone. It loads when you look at it, and otherwise only when you
 * ask.
 *
 * It loads TWICE on that first look, on purpose. The saved scan comes first and is drawn
 * straight away - it touches no disk, so it is instant however large or slow the library is.
 * Then a real scan runs underneath it and replaces it. The real scan is cheap when little has
 * changed, but "cheap" still means statting every folder, which on a network share or a
 * spun-down array is exactly the wait this exists to hide.
 */
export function useLibrary(enabled: boolean): LibraryState {
  const [albums, setAlbums] = useState<LibraryAlbum[]>([])
  const [artists, setArtists] = useState<LibraryArtist[]>([])
  const [queue, setQueue] = useState<MetadataQueueSummary>(EMPTY_QUEUE)
  const [issueTypes, setIssueTypes] = useState<Record<string, MetadataIssueType>>({})
  const [reviewTracking, setReviewTracking] = useState(true)
  const [problem, setProblem] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [stale, setStale] = useState(false)
  const [scannedAt, setScannedAt] = useState<number | null>(null)
  const [scanSeconds, setScanSeconds] = useState(0)
  const [libraryPath, setLibraryPath] = useState('')

  /*
   * Only the NEWEST request may write state. A rescan clicked while the background scan is still
   * running would otherwise race it, and whichever answered last would win - which can be the
   * older question. Every load still resolves with its own albums for its own caller.
   */
  const latest = useRef(0)
  const inFlight = useRef(0)
  const started = useRef(false)

  const load = useCallback(async (mode: LoadMode): Promise<LibraryResponse | null> => {
    const id = ++latest.current
    inFlight.current += 1
    setLoading(true)

    try {
      const result = mode === 'rescan'
        ? await api.rescan()
        : await api.listAlbums({ snapshot: mode === 'snapshot' })

      if (id === latest.current) {
        setAlbums(result.albums)
        setArtists(result.artists)
        //? both derived from this same scan, so they can't disagree with the album list they
        //? describe - which is the reason they arrive with it rather than from a second endpoint
        setQueue(result.queue ?? EMPTY_QUEUE)
        setIssueTypes(result.issue_types ?? {})
        setReviewTracking(result.review_tracking_enabled !== false)
        setProblem(result.problem)
        setLibraryPath(result.library_path)
        setScanSeconds(result.scan_seconds)
        setScannedAt(result.scanned_at ?? null)
        setStale(Boolean(result.stale))
        setError(null)
      }
      return result
    } catch (caught) {
      if (id === latest.current) {
        //? the snapshot, if any, stays on screen - a failed refresh is not a reason to empty
        //? a list that was right a moment ago
        setError(caught instanceof Error ? caught.message : 'failed to read the library')
      }
      return null
    } finally {
      inFlight.current -= 1
      setLoading(inFlight.current > 0)
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    // deliberately not re-running when the tab is closed and reopened - the scan is cached
    // server-side anyway, and reloading on every visit makes the view flicker for no gain
    if (!enabled || started.current) return
    started.current = true

    void (async () => {
      const first = await load('snapshot')
      //? a snapshot is followed by the real thing; a real scan (nothing was saved yet) is final
      if (first?.stale) await load('scan')
    })()
  }, [enabled, load])

  const reload = useCallback(
    async (force = false) => (await load(force ? 'rescan' : 'scan'))?.albums ?? [],
    [load],
  )

  return {
    albums, artists, queue, issueTypes, reviewTracking,
    problem, error, loading, loaded, stale, scannedAt, scanSeconds, libraryPath, reload,
  }
}
