import { useEffect, useMemo, useRef, useState } from 'preact/hooks'

import { ApiError } from '../api/http'
import * as libraryApi from '../api/library'
import type { LibraryAlbum } from '../api/types'
import { bridge } from '../bridge'
import { useLibrary } from '../hooks/useLibrary'
import { useTrackDetails } from '../hooks/useTrackDetails'
import { formatAge, formatSize } from '../lib/format'
import { groupAlbums, type AlbumGroup } from '../lib/groupAlbums'
import {
  ancestorsOf, arrange, DEFAULT_DIRECTION, groupArtists, indexTree, nodeIdForAlbum, trackNodeId,
  TREE_SORTS, visibleRows, type Selected, type SortDirection, type TreeSort,
} from '../lib/libraryTree'
import { isNewImport, issueLabel, queueAlbums } from '../lib/metadataQueue'
import {
  readLibraryPaneWidth, readLibrarySort, writeLibraryPaneWidth, writeLibrarySort,
} from '../state/persisted'
import { DeleteAlbumDialog } from './DeleteAlbumDialog'
import { LibraryDetails } from './LibraryDetails'
import { LibraryTree, type NodeRow } from './LibraryTree'
import { Loading, LoadingPanel } from './Loading'
import { MetadataEditor } from './MetadataEditor'
import type { TabId } from './Tabs'

interface Props {
  /** The library only loads once this is true — see useLibrary. */
  active: boolean
  onNavigate: (tab: TabId) => void
}

/**
 * Where you are in the review queue.
 *
 * Paths rather than album objects, deliberately. Applying a release rewrites tags and can
 * rename the folder, so an album captured when the queue started is stale the moment you fix
 * it - and holding the objects would mean stepping forward into a copy that no longer matches
 * anything on disk.
 *
 * This is navigation state ONLY. The album on screen is `editing`, which is set from a list
 * the caller is holding rather than derived from `albums` on every render - see the note there.
 */
interface ReviewState {
  paths: string[]
  index: number
}

//? the tree pane's width before anyone drags the splitter, and the least either side may have
const DEFAULT_NAV_WIDTH = 400
const MIN_NAV_WIDTH = 260
const MIN_DETAILS_WIDTH = 340

const EMPTY: ReadonlySet<string> = new Set()

//? Windows 7's music library called this "Arrange by", and it is the right name: three of the
//? four don't just reorder the tree, they take the artist level away
const SORT_LABELS: Record<TreeSort, string> = {
  artist: 'Artist', album: 'Album', released: 'Release date', added: 'Date added',
}

const SORT_HINTS: Record<TreeSort, string> = {
  artist: "Artists A-Z, and each artist's albums in the order they came out",
  album: "Every album by title, whoever it's by",
  released: "The album's own year - the earliest of the editions you hold",
  added: "When jimbrainz first saw each album, or its folder's date if that's earlier",
}

const DIRECTION_LABELS: Record<TreeSort, Record<SortDirection, string>> = {
  artist: { asc: 'A–Z', desc: 'Z–A' },
  album: { asc: 'A–Z', desc: 'Z–A' },
  released: { asc: 'Oldest first', desc: 'Newest first' },
  added: { asc: 'Oldest first', desc: 'Newest first' },
}

/** The saved arrangement, validated - it is JSON a user can edit and an old version may have written. */
function initialOrder(): { sort: TreeSort; direction: SortDirection } {
  const saved = readLibrarySort()
  const sort = TREE_SORTS.find((s) => s === saved?.sort) ?? 'artist'
  const direction = saved?.direction === 'asc' || saved?.direction === 'desc'
    ? saved.direction
    : DEFAULT_DIRECTION[sort]
  return { sort, direction }
}

/**
 * What's already on disk, laid out like a file explorer.
 *
 * A tree on the left - artists, their albums, an album's editions when there are several, then
 * tracks - and the selected thing in detail on the right. It replaced a list of full-width album
 * rows that were mostly empty space in the middle and made you scroll past every album to find
 * one. The tree carries the finding; the pane spends its width on what you found.
 *
 * It still answers the second question the search view never has to: not just what you have,
 * but what is *wrong* with it. The metadata queue lives here as the "Views" above the tree,
 * because every answer to it is an album in this library - the views narrow the same tree, and
 * reviewing opens the same editor the command bar does.
 */
export function LibraryView({ active, onNavigate }: Props) {
  const {
    albums, queue, issueTypes, reviewTracking, problem, error, loading, loaded,
    stale, scannedAt, scanSeconds, libraryPath, reload,
  } = useLibrary(active)

  const [filter, setFilter] = useState('')
  const [multiOnly, setMultiOnly] = useState(false)
  /** Only albums with outstanding metadata issues. */
  const [queueOnly, setQueueOnly] = useState(false)
  /** Narrowed further to one kind of issue, or null for any. */
  const [issueFilter, setIssueFilter] = useState<string | null>(null)
  /** Only albums jimbrainz just filed that haven't been looked at — what the tab badge counts. */
  const [newOnly, setNewOnly] = useState(false)
  const [order, setOrderState] = useState(initialOrder)
  const { sort, direction } = order
  const setOrder = (next: { sort: TreeSort; direction: SortDirection }) => {
    setOrderState(next)
    writeLibrarySort(next)
  }
  /*
   * Only has any effect on a phone, where CSS both reveals the toggle and acts on the class.
   * Starts collapsed because on a phone the views above the tree push the first artist most of
   * the way off the screen, and the library is what you came for.
   */
  const [viewsCollapsed, setViewsCollapsed] = useState(true)

  /* ----- the tree ----- */

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(EMPTY)
  /** Artists or albums closed by hand while a filter had opened them. Reset with the filter. */
  const [closed, setClosed] = useState<ReadonlySet<string>>(EMPTY)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [focusToken, setFocusToken] = useState(0)
  const [revealToken, setRevealToken] = useState(0)
  /** Phones: the details pane is a sheet over the tree, open once something has been picked. */
  const [sheetOpen, setSheetOpen] = useState(false)

  const [navWidth, setNavWidth] = useState(() => readLibraryPaneWidth() ?? DEFAULT_NAV_WIDTH)
  const panesRef = useRef<HTMLDivElement>(null)

  /* ----- the editor, the queue and the bulk actions ----- */

  /*
   * Which album the metadata editor is open for, or null. One at a time on purpose - it is an
   * overlay over the whole view, not a per-row inline form.
   *
   * Held as the album itself, and kept up to date explicitly, rather than being derived from
   * `albums` by path on every render. Deriving it looked tidier and was wrong: applying a
   * release renames the folder, so the reloaded library and the queue's own record of where
   * the album now lives are two separate state commits, and for the render in between them
   * NEITHER the old path nor the new one resolves to anything. The editor unmounted mid-queue
   * every time a rename was applied. One value, updated once, cannot disagree with itself.
   */
  const [editing, setEditing] = useState<LibraryAlbum | null>(null)

  //? set alongside `editing` when the editor was opened from the queue rather than from a row
  const [review, setReview] = useState<ReviewState | null>(null)

  /*
   * Bumped every time the queue moves to a different album, and used as the editor's `key`.
   *
   * The editor deliberately does NOT reset itself when its album prop changes - that happens
   * after an apply, where the release list on screen is still the right one and re-searching
   * MusicBrainz would be wasted. Moving to a different album is the opposite case: nothing
   * about the previous one applies, so it wants a genuinely fresh component, which is what
   * changing the key gives us.
   */
  const [session, setSession] = useState(0)

  /**
   * A bulk cover fetch in progress, or the summary of the last one.
   *
   * Driven from here one album at a time rather than by a single server-side endpoint: you can
   * watch it happen, you can stop it, and every album goes through the exact same tested route
   * a single "get cover" click does.
   */
  const [bulkArt, setBulkArt] = useState<{
    total: number
    done: number
    written: number
    missing: number
    failed: number
    running: boolean
  } | null>(null)

  //? read by the loop to break out, rather than state, so a click lands immediately instead of
  //? on the next render
  const stopBulk = useRef(false)

  //? the album awaiting a delete confirmation, or null
  const [deleting, setDeleting] = useState<LibraryAlbum | null>(null)

  /* ----- what's in view ----- */

  //? grouped first, then filtered, so a filter never splits an album from its own editions
  const groups = useMemo(() => groupAlbums(albums), [albums])

  //? every node, filtered or not, so a selection survives being filtered out of the tree
  const allArtists = useMemo(() => groupArtists(groups, isNewImport), [groups])
  const index = useMemo(() => indexTree(allArtists), [allArtists])
  //? the whole library in the current arrangement, for opening the tree down to a selection
  const fullLayout = useMemo(
    () => arrange(groups, sort, direction, isNewImport),
    [groups, sort, direction],
  )

  const needle = filter.trim().toLowerCase()
  const facetOn = multiOnly || queueOnly || newOnly || issueFilter !== null
  const filtering = needle !== '' || facetOn

  /*
   * The filter matches artists, albums and editions - and song titles, which is the part a flat
   * album list never could: type a song and the tree opens its album to show you where it is.
   * Only from two characters, because one letter matches half the library's tracks.
   */
  const { visibleGroups, trackMatches } = useMemo(() => {
    const matches = new Set<string>()

    const kept = groups.filter((group) => {
      if (multiOnly && group.editions.length < 2) return false
      if (queueOnly && !group.needsAttention) return false
      if (newOnly && !group.editions.some(isNewImport)) return false
      if (issueFilter && !group.issues.includes(issueFilter)) return false
      if (!needle) return true

      if (
        group.album.toLowerCase().includes(needle)
        || group.artist.toLowerCase().includes(needle)
        || group.editions.some((e) => e.edition.toLowerCase().includes(needle))
      ) {
        return true
      }

      if (needle.length < 2) return false

      let found = false
      for (const album of group.editions) {
        for (const track of album.tracks) {
          if (track.title.toLowerCase().includes(needle)) {
            matches.add(trackNodeId(album, track))
            found = true
          }
        }
      }
      return found
    })

    return { visibleGroups: kept, trackMatches: matches as ReadonlySet<string> }
  }, [groups, needle, multiOnly, queueOnly, newOnly, issueFilter])

  const layout = useMemo(
    () => arrange(visibleGroups, sort, direction, isNewImport),
    [visibleGroups, sort, direction],
  )

  //? a new filter opens every match afresh; what you closed under the last one doesn't carry over
  useEffect(() => setClosed(EMPTY), [needle, multiOnly, queueOnly, newOnly, issueFilter])

  const rows = useMemo(
    () => visibleRows(layout, { expanded, closedWhileFiltering: closed, filtering, trackMatches }),
    [layout, expanded, closed, filtering, trackMatches],
  )

  const selected: Selected = (selectedId ? index.get(selectedId) : undefined) ?? { kind: 'none' }
  const selectedAlbum = selected.kind === 'album' || selected.kind === 'track' ? selected.album : null
  const trackDetails = useTrackDetails(selectedAlbum?.path ?? null, albums)

  const multiEditionCount = useMemo(
    () => groups.filter((g) => g.editions.length > 1).length,
    [groups],
  )

  //? what "review" would walk through: the queue, narrowed by the issue facet if one is on,
  //? in the order lib/metadataQueue decides
  const waiting = useMemo(() => queueAlbums(albums, issueFilter), [albums, issueFilter])

  /** Issue facets, most-common first, so the biggest job to do is at the top. */
  const issueFacets = useMemo(
    () => Object.entries(queue.by_issue).sort((a, b) => b[1] - a[1]),
    [queue.by_issue],
  )

  const totals = useMemo(() => ({
    tracks: albums.reduce((n, a) => n + a.track_count, 0),
    size: albums.reduce((n, a) => n + a.total_size, 0),
    duration: albums.reduce((n, a) => n + a.duration, 0),
  }), [albums])

  /**
   * Albums IN VIEW that a cover could be fetched for.
   *
   * Scoped to what the tree is showing so the views compose with it - narrow to "no cover art",
   * or search for one artist, and the bulk action follows. They must already name a release,
   * because that is what the Archive is asked about.
   */
  const artCandidates = useMemo(
    () => visibleGroups.flatMap((group) => group.editions)
                       .filter((album) => !album.art && album.release_mbid),
    [visibleGroups],
  )

  /* ----- tree interaction ----- */

  const setOpen = (id: string, open: boolean) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (open) next.add(id)
      else next.delete(id)
      return next
    })
    setClosed((current) => {
      const next = new Set(current)
      if (open) next.delete(id)
      else next.add(id)
      return next
    })
  }

  /** A click in the tree: select it and open it, which is how you walk down into the library. */
  const activate = (row: NodeRow) => {
    setSelectedId(row.id)
    if ((row.kind === 'artist' || row.kind === 'group' || row.kind === 'edition') && !row.open) {
      setOpen(row.id, true)
    }
    //? a phone opens the details as a sheet for anything with details worth a whole screen;
    //? tapping an artist just opens it in place
    if (row.kind !== 'artist') setSheetOpen(true)
  }

  const selectFromKeyboard = (id: string) => {
    setSelectedId(id)
    setFocusToken((n) => n + 1)
  }

  /** Something in the details pane was picked: select it, and open the tree down to it. */
  const selectFromPane = (id: string) => {
    setSelectedId(id)
    const ancestors = ancestorsOf(id, fullLayout)
    if (ancestors.length) {
      setExpanded((current) => new Set([...current, ...ancestors]))
      setClosed((current) => new Set([...current].filter((c) => !ancestors.includes(c))))
    }
    setRevealToken((n) => n + 1)
  }

  /* ----- the splitter ----- */

  const clampWidth = (width: number) => {
    const available = panesRef.current?.clientWidth ?? width + MIN_DETAILS_WIDTH
    return Math.round(Math.min(Math.max(width, MIN_NAV_WIDTH), Math.max(MIN_NAV_WIDTH, available - MIN_DETAILS_WIDTH)))
  }

  /**
   * Drag to resize the tree against the details, as in Explorer. Pointer capture on the handle
   * means the drag keeps tracking when the cursor outruns it, and the width is saved only on
   * release rather than written to storage on every pixel.
   */
  const startDrag = (event: PointerEvent) => {
    const handle = event.currentTarget as HTMLElement
    const panes = panesRef.current
    if (!panes) return

    event.preventDefault()
    handle.setPointerCapture(event.pointerId)

    //? the rect for POSITION is right here - nothing in this layout is transformed
    const left = panes.getBoundingClientRect().left
    let width = navWidth

    const move = (e: PointerEvent) => {
      width = clampWidth(e.clientX - left)
      setNavWidth(width)
    }
    const end = () => {
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', end)
      handle.removeEventListener('pointercancel', end)
      writeLibraryPaneWidth(width)
    }

    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', end)
    handle.addEventListener('pointercancel', end)
  }

  const nudgeWidth = (event: KeyboardEvent) => {
    const step = event.key === 'ArrowLeft' ? -24 : event.key === 'ArrowRight' ? 24 : 0
    if (!step) return
    event.preventDefault()
    const width = clampWidth(navWidth + step)
    setNavWidth(width)
    writeLibraryPaneWidth(width)
  }

  /* ----- the queue and bulk art ----- */

  /**
   * Fetch covers for every candidate, one at a time.
   *
   * Sequential on purpose. These go out to the Cover Art Archive, which goes unreachable for
   * minutes at a time, and thirty parallel requests would turn one slow patch into thirty
   * failures. A 404 is counted apart from a failure: it means that release has no front cover,
   * which is a fact rather than a fault.
   */
  const fetchAllArt = async () => {
    const targets = artCandidates
    if (!targets.length) return

    stopBulk.current = false
    setBulkArt({ total: targets.length, done: 0, written: 0, missing: 0, failed: 0, running: true })

    let written = 0
    let missing = 0
    let failed = 0

    for (const [position, album] of targets.entries()) {
      if (stopBulk.current) break

      try {
        await libraryApi.fetchCoverArt(album.path)
        written += 1
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 404) missing += 1
        else failed += 1
      }

      setBulkArt({
        total: targets.length, done: position + 1, written, missing, failed, running: true,
      })
    }

    setBulkArt((current) => current && { ...current, running: false })
    //? one reload at the end rather than per album: each write already dropped that folder from
    //? the server's scan cache, so this picks up every new cover in a single pass
    await reload(false)
  }

  //? the tab badge lives in a different render tree and can't see any of this state, so it is
  //? told to recount rather than being left to notice on its own timer - see bridge.ts
  const recountBadge = () => bridge().refreshNewImports?.()

  /**
   * Point the editor at this album's current state, from a list the caller has just received.
   *
   * Always given the freshly loaded array rather than reading `albums`, because the caller is
   * inside the async continuation of a reload and the state it set has not necessarily been
   * committed yet.
   */
  const syncEditing = (fresh: readonly LibraryAlbum[], path: string) => {
    const updated = fresh.find((album) => album.path === path)
    if (updated) setEditing(updated)
    return updated
  }

  const startReview = () => {
    if (!waiting.length) return
    setReview({ paths: waiting.map((album) => album.path), index: 0 })
    setEditing(waiting[0] ?? null)
    setSession((n) => n + 1)
  }

  const leaveQueue = () => {
    const wasReviewing = review !== null

    setEditing(null)
    setReview(null)

    /*
     * Reload once on the way out, not on every step. Stepping through marks each album reviewed
     * on the server, but the facet counts come from the scan payload - so without this you close
     * the queue having cleared the tab badge while the views still say "newly added 1".
     */
    if (wasReviewing) void reload(false)
  }

  const step = (delta: number) => {
    if (!review) return

    //? moving on counts as having looked at it, which is what clears a freshly imported album
    //? from the "something new arrived" prompt. It keeps every issue it had.
    const leaving = review.paths[review.index]
    if (leaving) {
      void libraryApi.markReviewed(leaving).then(recountBadge).catch(() => undefined)
    }

    const position = Math.min(Math.max(review.index + delta, 0), review.paths.length - 1)
    if (position === review.index) return

    const next = albums.find((album) => album.path === review.paths[position])

    //? The album has gone - deleted, or renamed by something other than an apply here. This is
    //? a click rather than a render, so `albums` is settled and the absence is real.
    if (!next) {
      leaveQueue()
      return
    }

    setReview({ ...review, index: position })
    setEditing(next)
    setSession((n) => n + 1)
  }

  const ignoreAlbum = async (album: LibraryAlbum, issues: string[]) => {
    await libraryApi.ignoreIssues(album.path, issues)
    recountBadge()
    syncEditing(await reload(false), album.path)
  }

  const unignoreAlbum = async (album: LibraryAlbum) => {
    await libraryApi.unignoreAlbum(album.path)
    recountBadge()
    syncEditing(await reload(false), album.path)
  }

  const searchArtist = (artist: string) => {
    bridge().runSearch?.({ artist })
    onNavigate('search')
  }

  // the album name alone is ambiguous - there are a lot of records called "Greatest Hits" -
  // so the artist goes along with it
  const searchAlbum = (group: AlbumGroup) => {
    bridge().runSearch?.({ artist: group.artist, album: group.album })
    onNavigate('search')
  }

  const clearFilters = () => {
    setMultiOnly(false)
    setQueueOnly(false)
    setNewOnly(false)
    setIssueFilter(null)
  }

  /* ----- status ----- */

  const age = formatAge(scannedAt)

  const summaryText = !loaded ? (
    <Loading label="Reading your library" />
  ) : stale && loading ? (
    //? the list is the SAVED scan: say so, and say how old, while the real one catches up
    <Loading label={`${groups.length} albums from the saved scan${age ? ` (${age})` : ''} · checking for changes`} />
  ) : loading ? (
    <Loading label={`${groups.length} albums · rescanning`} />
  ) : filtering ? (
    `${visibleGroups.length} of ${groups.length} albums`
  ) : (
    `${groups.length} album${groups.length === 1 ? '' : 's'}`
  )

  return (
    <div id="library-content">
      <div id="library-toolbar">
        <input
          type="search"
          id="library-filter-input"
          class="releases-filter-input"
          placeholder="Search artists, albums and songs…"
          value={filter}
          onInput={(event) => setFilter((event.target as HTMLInputElement).value)}
        />

        <div class="library-sort">
          <label class="text white-tertiary" for="library-sort-select">Arrange by</label>
          <select
            id="library-sort-select"
            value={sort}
            title={SORT_HINTS[sort]}
            onChange={(event) => {
              const next = (event.target as HTMLSelectElement).value as TreeSort
              //? each sort starts its own natural way round - names A-Z, date added newest first
              setOrder({ sort: next, direction: DEFAULT_DIRECTION[next] })
            }}
          >
            {TREE_SORTS.map((s) => <option key={s} value={s}>{SORT_LABELS[s]}</option>)}
          </select>
          <button
            type="button"
            class="win-button library-sort-direction"
            title="Reverse the order"
            onClick={() => setOrder({ sort, direction: direction === 'asc' ? 'desc' : 'asc' })}
          >
            {DIRECTION_LABELS[sort][direction]}
          </button>
        </div>

        <span id="library-summary" class="text default-muted">{summaryText}</span>

        {/*
          The whole point of the queue: start at the first album that needs something and work
          through them without coming back here between each one. Named for what it will do,
          including an issue view's narrowing, so it can't surprise you with a hundred albums.
        */}
        {loaded && waiting.length > 0 && (
          <button
            type="button"
            id="library-review-button"
            title={
              issueFilter
                ? `work through the ${waiting.length} album(s) with: ${issueLabel(issueFilter, issueTypes)}`
                : 'work through every album that needs metadata or has just been added, '
                  + 'one at a time - stepping past a new one is what marks it seen'
            }
            onClick={startReview}
          >
            Review {waiting.length}
            {issueFilter ? ` · ${issueLabel(issueFilter, issueTypes)}` : ''}
            {!issueFilter && queue.new_imports > 0 ? ` · ${queue.new_imports} new` : ''}
          </button>
        )}

        {loaded && artCandidates.length > 0 && !bulkArt?.running && (
          <button
            type="button"
            id="library-bulk-art-button"
            class="win-button"
            title={`fetch a cover for the ${artCandidates.length} album(s) in view that have a `
                 + 'release but no art - nothing else about them changes'}
            onClick={() => void fetchAllArt()}
          >
            Get covers · {artCandidates.length}
          </button>
        )}

        {bulkArt?.running && (
          <button
            type="button"
            id="library-bulk-art-button"
            class="win-button is-running"
            title="Stop after the album currently being fetched"
            onClick={() => { stopBulk.current = true }}
          >
            <Loading label={`${bulkArt.done}/${bulkArt.total} · stop`} />
          </button>
        )}

        <button
          type="button"
          class="win-button"
          disabled={loading && !stale}
          title="Re-read every file, ignoring the cache - for when something changed that jimbrainz couldn't see, like a retag by another program"
          onClick={() => void reload(true)}
        >
          Rescan
        </button>
      </div>

      <div
        id="library-panes"
        ref={panesRef}
        style={`--library-nav-width:${navWidth}px`}
      >
        <nav id="library-nav" aria-label="Library">
          <div id="library-views" class={viewsCollapsed ? 'collapsed' : undefined}>
            <div class="nav-heading">
              <span>Views</span>
              <button
                type="button"
                id="library-clear-filters"
                disabled={!facetOn}
                onClick={clearFilters}
              >
                Clear
              </button>
              <button
                type="button"
                class="filter-collapse-toggle"
                aria-expanded={!viewsCollapsed}
                title="Show or hide the views"
                onClick={() => setViewsCollapsed((on) => !on)}
              >
                {viewsCollapsed ? '▾' : '▴'}
              </button>
            </div>

            <div class="library-views-list">
              <button
                type="button"
                class={`library-facet${!facetOn ? ' active' : ''}`}
                onClick={clearFilters}
              >
                All albums <span class="library-facet-count">{groups.length}</span>
              </button>

              {multiEditionCount > 0 && (
                <button
                  type="button"
                  class={`library-facet${multiOnly ? ' active' : ''}`}
                  onClick={() => setMultiOnly((on) => !on)}
                >
                  Multiple editions <span class="library-facet-count">{multiEditionCount}</span>
                </button>
              )}

              {/*
                The tab badge counts these, so there has to be a way to see WHICH albums it means.
                A freshly imported album with good tags has no issues, so without this it would be
                counted by the badge and shown nowhere.
              */}
              {loaded && queue.new_imports > 0 && (
                <button
                  type="button"
                  class={`library-facet newly-added${newOnly ? ' active' : ''}`}
                  title="Albums jimbrainz just filed that you haven't looked at yet"
                  onClick={() => setNewOnly((on) => !on)}
                >
                  Newly added <span class="library-facet-count">{queue.new_imports}</span>
                </button>
              )}

              {/*
                Only rendered when there is something in it: a permanent "0 need metadata" is a
                line you stop reading, and then the day it says 12 you don't notice either.
              */}
              {loaded && queue.total > 0 && (
                <button
                  type="button"
                  class={`library-facet needs-attention${queueOnly ? ' active' : ''}`}
                  title="Albums with something missing or off-convention"
                  onClick={() => setQueueOnly((on) => !on)}
                >
                  Needs attention <span class="library-facet-count">{queue.total}</span>
                </button>
              )}

              {loaded && issueFacets.map(([code, count]) => (
                <button
                  key={code}
                  type="button"
                  class={`library-facet library-issue-facet${issueFilter === code ? ' active' : ''}`}
                  title={issueTypes[code]?.hint}
                  onClick={() => setIssueFilter((current) => (current === code ? null : code))}
                >
                  {issueLabel(code, issueTypes)}{' '}
                  <span class="library-facet-count">{count}</span>
                </button>
              ))}

              {!reviewTracking && (
                <p class="text yellow library-queue-note">
                  ignores can't be saved — the job store couldn't be opened, so albums you accept
                  will come back
                </p>
              )}
            </div>
          </div>

          <div class="nav-heading nav-heading-artists">
            <span>{layout.by === 'artist' ? 'Artists' : 'Albums'}</span>
            <span class="library-facet-count">
              {layout.by === 'artist' ? layout.artists.length : layout.groups.length}
            </span>
          </div>

          <div class="scrollable" id="library-tree-scroll">
            {!loaded && !problem && !error && (
              <LoadingPanel label="Reading tags from your library…" />
            )}

            {/*
              An unconfigured LIBRARY_PATH is a setup step, not a failure - say which knob to turn
              rather than rendering an empty tree that looks like a broken scan.
            */}
            {problem && <p class="text yellow library-status">{problem}</p>}
            {error && <p class="text red library-status">{error}</p>}

            {!problem && !error && loaded && !albums.length && (
              <p class="text default-muted library-status">
                nothing found in {libraryPath || 'your library'} — downloads appear here once
                they've been organized
              </p>
            )}

            {!problem && loaded && albums.length > 0 && !visibleGroups.length && (
              <p class="text default-muted library-status">Nothing matches that</p>
            )}

            <LibraryTree
              rows={rows}
              selected={selectedId}
              focusToken={focusToken}
              revealToken={revealToken}
              issueTypes={issueTypes}
              onActivate={activate}
              onSelect={selectFromKeyboard}
              onToggle={setOpen}
            />
          </div>
        </nav>

        <div
          class="library-splitter"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the tree"
          aria-valuenow={navWidth}
          tabIndex={0}
          title="Drag to resize · double-click to reset"
          onPointerDown={(event) => startDrag(event as unknown as PointerEvent)}
          onKeyDown={(event) => nudgeWidth(event as unknown as KeyboardEvent)}
          onDblClick={() => {
            setNavWidth(DEFAULT_NAV_WIDTH)
            writeLibraryPaneWidth(DEFAULT_NAV_WIDTH)
          }}
        />

        <section
          id="library-details"
          aria-label="Details"
          class={sheetOpen && selected.kind !== 'none' ? 'is-open' : undefined}
        >
          <LibraryDetails
            selected={selected}
            selectedId={selectedId}
            details={trackDetails}
            issueTypes={issueTypes}
            groups={groups}
            summary={{
              albums: groups.length,
              artists: allArtists.length,
              tracks: totals.tracks,
              size: totals.size,
              duration: totals.duration,
              libraryPath,
              scannedAt,
              stale,
            }}
            onSelect={selectFromPane}
            onEdit={setEditing}
            onDelete={setDeleting}
            /* the server dropped the folder from its scan cache when it wrote the cover, so a
               plain reload picks up the new art_mtime and the URL changes with it */
            onArtFetched={() => void reload(false)}
            onSearchArtist={searchArtist}
            onSearchAlbum={searchAlbum}
            onBack={() => setSheetOpen(false)}
          />
        </section>
      </div>

      {/* Explorer's status bar: what is here, and how old the view of it is */}
      <div id="library-statusbar" role="status">
        <span>{groups.length} albums</span>
        <span>{allArtists.length} artists</span>
        <span>{totals.tracks} tracks</span>
        {totals.size > 0 && <span>{formatSize(totals.size)}</span>}

        {/*
          What the last bulk run did. "No cover on the Archive" is kept apart from "the request
          failed": the first is a fact about the release, the second is worth trying again.
        */}
        {bulkArt && !bulkArt.running && (
          <span class="statusbar-note">
            fetched {bulkArt.written} cover{bulkArt.written === 1 ? '' : 's'}
            {bulkArt.missing ? `, ${bulkArt.missing} had none on the Archive` : ''}
            {bulkArt.failed ? `, ${bulkArt.failed} failed - try those again` : ''}
            {bulkArt.done < bulkArt.total ? ` (stopped at ${bulkArt.done} of ${bulkArt.total})` : ''}
          </span>
        )}

        <span class="statusbar-spacer" />
        <span title={scannedAt ? new Date(scannedAt * 1000).toLocaleString() : undefined}>
          {stale
            ? `Saved scan from ${age || 'an earlier visit'}`
            : scannedAt ? `Scanned ${age}${scanSeconds ? ` in ${scanSeconds}s` : ''}` : ''}
        </span>
      </div>

      {deleting && (
        <DeleteAlbumDialog
          album={deleting}
          onCancel={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null)
            //? the server already dropped it from the scan cache, so a plain reload is enough
            void reload(false)
          }}
        />
      )}

      {editing && (
        <MetadataEditor
          key={session}
          album={editing}
          issueTypes={issueTypes}
          queue={
            review
              ? {
                  position: review.index + 1,
                  total: review.paths.length,
                  onNext: () => step(1),
                  onPrevious: () => step(-1),
                }
              : undefined
          }
          onClose={leaveQueue}
          onIgnore={ignoreAlbum}
          onUnignore={unignoreAlbum}
          /*
            The retag already dropped this folder from the server's cache, so a plain reload
            picks up the new tags and path without a full rescan - but the editor is holding the
            album object it was opened with, which is now stale in every field that just changed.
            Re-resolve it by its new path so the open editor shows what it did.
          */
          onApplied={async (newPath) => {
            const oldPath = editing.path
            const fresh = await reload(false)
            recountBadge()
            const updated = syncEditing(fresh, newPath)

            //? the details pane follows the album to its new folder, rather than falling back
            //? to the overview because the path it was showing no longer exists
            if (updated && selectedAlbum?.path === oldPath) {
              const group = groupAlbums(fresh).find((g) => g.editions.some((e) => e.path === newPath))
              if (group) setSelectedId(nodeIdForAlbum(updated, group))
            }

            //? the queue is holding paths and this album's has just changed under it
            if (review) {
              setReview((current) => current && {
                ...current,
                paths: current.paths.map((path, i) => (i === current.index ? newPath : path)),
              })
            }
          }}
        />
      )}
    </div>
  )
}
