import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'

import { MusicBrainzUnavailable } from '../api/http'
import * as libraryApi from '../api/library'
import * as musicbrainz from '../api/musicbrainz'
import type {
  LibraryAlbum, MetadataIssueType, Release, RetagPlan, RetagRelease,
} from '../api/types'
import { albumArtUrl } from '../lib/format'
import { ArtViewer } from './ArtViewer'
import { Loading, LoadingPanel } from './Loading'
import { issueLabel, outstandingIssues } from '../lib/metadataQueue'
import {
  buildRetagRelease,
  describeRelease,
  isCurrentRelease,
  releaseTrackCount,
  scoreReleaseGroupMatch,
  scoreReleaseMatch,
} from '../lib/release'

/**
 * How many release groups get their releases fetched.
 *
 * Each one is at least one request against a service that allows about one a second, and a
 * group like the Black Album's runs to several pages - so this is the main cost of opening the
 * editor. Three is a margin around a ranking that should already have put the right group
 * first; the server caches the responses, so re-opening the same album is free.
 */
const GROUPS_SEARCHED = 3

/**
 * Where this album sits in the review queue, when the editor was opened from it.
 *
 * Absent when you clicked "edit" on a single album, which is still the ordinary way in - the
 * queue adds a way to work through several without going back to the list between each, it
 * doesn't replace editing one.
 */
export interface QueueContext {
  /** 1-based, for display. */
  position: number
  total: number
  onNext: () => void
  onPrevious: () => void
}

interface Props {
  album: LibraryAlbum
  /** Issue code -> label and hint, from the scan. The server owns this vocabulary. */
  issueTypes: Record<string, MetadataIssueType>
  //? `| undefined` is not redundant: exactOptionalPropertyTypes is on, so an optional prop
  //? passed explicitly as undefined - which is what a conditional in JSX produces - is a type
  //? error without it
  queue?: QueueContext | undefined
  onClose: () => void
  /**
   * Called after a successful apply with the album's path afterwards, which differs from the
   * one it opened with whenever the folder was renamed. The library reloads and hands back
   * the refreshed album, so this editor keeps working on what it just wrote rather than on a
   * stale copy pointing at a folder that no longer exists.
   */
  onApplied: (newPath: string) => void
  /**
   * Accept this album as it is. Given the codes on screen rather than left to the server, so
   * it can only ever mute what you were actually shown.
   */
  onIgnore: (album: LibraryAlbum, issues: string[]) => Promise<void>
  /** Put an ignored album back into the queue. */
  onUnignore: (album: LibraryAlbum) => Promise<void>
}

/** The fields you can type into. Everything else comes from the release you pick. */
interface Fields {
  artist: string
  album: string
  /** This pressing's year. */
  year: string
  /** The album's original year. Names the folder — see build_album_dirname. */
  originalYear: string
  editionLabel: string
}

//? A plan is a server round trip, and every keystroke would be one. Long enough to finish a
//? word, short enough that the preview still feels attached to what you typed.
const PREVIEW_DEBOUNCE_MS = 400

/**
 * The cover you'd end up with, beside the one you have.
 *
 * Loaded straight from the Cover Art Archive by the browser rather than through the server -
 * exactly as the library rows already do for albums with no local art. Fetching it server-side
 * to preview it would mean downloading the image twice, and downloading on preview is the
 * thing plan_art() deliberately avoids.
 *
 * Shown whenever a release is selected, not only when the checkbox is ticked: seeing the art
 * is how you decide whether you want it.
 */
function ArtComparison(
  { album, releaseId, onOpen }:
  { album: LibraryAlbum; releaseId: string | null; onOpen: () => void },
) {
  const [currentFailed, setCurrentFailed] = useState(false)
  const [incomingFailed, setIncomingFailed] = useState(false)
  //? the current cover is served whole, so its natural size IS its real resolution - worth
  //? saying beside it, since resolution is usually what the choice actually comes down to
  const [currentSize, setCurrentSize] = useState<string | null>(null)

  //? reset when the release changes, or a single failure would stick for every later pick
  useEffect(() => setIncomingFailed(false), [releaseId])

  const current = album.art ? albumArtUrl(album) : null
  //? Keyed on the id alone, so the cover appears as soon as you click rather than waiting on
  //? the tracklist fetch behind it - the art is how you decide whether you want the release.
  const incoming = releaseId ? `https://coverartarchive.org/release/${releaseId}/front-250` : null

  if (!current && !incoming) return null

  return (
    <div class="metadata-art">
      <div class="metadata-art-slot">
        <span class="text white-tertiary">Current</span>
        {current && !currentFailed ? (
          <button type="button" class="metadata-art-open" title="Compare at full size" onClick={onOpen}>
            <img
              src={current}
              alt=""
              onError={() => setCurrentFailed(true)}
              onLoad={(event) => {
                const img = event.currentTarget as HTMLImageElement
                setCurrentSize(`${img.naturalWidth}×${img.naturalHeight}`)
              }}
            />
          </button>
        ) : (
          <div class="metadata-art-empty">None</div>
        )}
        {currentSize && <span class="text white-tertiary metadata-art-size">{currentSize}</span>}
      </div>

      <span class="metadata-art-arrow text default-secondary">→</span>

      <div class="metadata-art-slot">
        <span class="text white-tertiary">From this release</span>
        {incoming && !incomingFailed ? (
          <button type="button" class="metadata-art-open" title="Compare at full size" onClick={onOpen}>
            <img
              src={incoming}
              alt=""
              /* keyed so switching releases replaces the element rather than reusing one whose
                 onError already fired */
              key={releaseId}
              onError={() => setIncomingFailed(true)}
            />
          </button>
        ) : (
          <div class="metadata-art-empty">{releaseId ? 'none on file' : 'pick a release'}</div>
        )}
      </div>

      <button type="button" class="win-button metadata-art-compare" onClick={onOpen}>
        Compare full size…
      </button>
    </div>
  )
}

/**
 * Pick which MusicBrainz release an album on disk actually is, correct it by hand, and write
 * it back.
 *
 * An overlay rather than a tab: it acts on one album, which you were already looking at in
 * the library, and it should disappear when you're done. It reuses the candidates-window
 * pattern the download flow already uses, because this is structurally the same interaction
 * - here is a thing, here are candidate matches, pick one.
 *
 * Two ways in, deliberately. Picking a release fills the fields from MusicBrainz, which is
 * what you want when the album is mistagged. Typing in the fields directly is what you want
 * when MusicBrainz is wrong, or unreachable, or the record simply isn't in it - so the
 * fields work with no release selected at all.
 *
 * Nothing is written until Apply. The preview comes from the same planner the write uses, so
 * what it shows is what will happen rather than a separate guess at it.
 */
export function MetadataEditor(
  { album, issueTypes, queue, onClose, onApplied, onIgnore, onUnignore }: Props,
) {
  //? An override, normally empty. The search is built from the artist and album fields
  //? below, which are already on screen and editable - a second box holding the same two
  //? values would just be somewhere for them to disagree.
  const [query, setQuery] = useState('')
  const [releases, setReleases] = useState<Release[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)

  /*
   * The selection is two pieces of state, and the split is load-bearing.
   *
   * The list is fetched WITHOUT tracklists, because carrying the contents of all 58 pressings of
   * an album to let you pick one costs five requests and 1.4 MB against one request and 101 KB.
   * The tracklist of the one you actually pick is then fetched on its own.
   *
   * So `selectedId` is what you clicked - it lands instantly and drives the highlight and the
   * cover art, both of which only need an id. `selected` is that release WITH its tracklist, and
   * it is the only thing the retag payload is ever built from. A payload built from a trackless
   * release would silently apply no titles and no track numbers, which writes nothing and reads
   * exactly like the edit having failed - so the two must never be conflated.
   */
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Release | null>(null)
  const [loadingRelease, setLoadingRelease] = useState(false)

  //? seeded from what's on disk, so the editor opens showing the album as it is rather than
  //? empty boxes you have to fill before anything makes sense
  const [fields, setFields] = useState<Fields>({
    artist: album.artist,
    album: album.album,
    year: album.year,
    originalYear: album.original_year,
    editionLabel: album.edition === 'Standard' ? '' : album.edition,
  })

  const [fetchArt, setFetchArt] = useState(!album.art)
  const [plan, setPlan] = useState<RetagPlan | null>(null)
  const [planning, setPlanning] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [applied, setApplied] = useState<string | null>(null)
  const [ignoring, setIgnoring] = useState(false)
  //? the art-only action, which is a different thing from the `fetchArt` checkbox below - that
  //? one rides along with an apply, this one writes the cover and nothing else
  const [savingArt, setSavingArt] = useState(false)
  const [artResult, setArtResult] = useState<string | null>(null)
  //? the two covers side by side at full size - see ArtViewer
  const [comparingArt, setComparingArt] = useState(false)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  /*
   * Re-seed when the album changes underneath us, which happens after an apply: the parent
   * reloads the library and hands back the same album with its new tags, path and art. The
   * fields have to follow, or the editor keeps showing what you changed FROM and a second
   * edit would be computed against values that are no longer on disk.
   *
   * Skipped on mount, because useState already seeded from the same album. Beyond being
   * redundant, running it on mount means a late effect flush can overwrite something typed
   * before it fired - which is exactly what happened the first time this was tested.
   */
  const seeded = useRef(false)

  useEffect(() => {
    if (!seeded.current) {
      seeded.current = true
      return
    }

    setFields({
      artist: album.artist,
      album: album.album,
      year: album.year,
      originalYear: album.original_year,
      editionLabel: album.edition === 'Standard' ? '' : album.edition,
    })
    setFetchArt(!album.art)
  }, [album.path, album.artist, album.album, album.year, album.original_year,
      album.edition, album.art])

  const setField = (key: keyof Fields, value: string) => {
    //? the message describes a write that has already happened; the moment you change
    //? anything it stops describing what the apply button would now do
    setApplied(null)
    setFields((current) => ({ ...current, [key]: value }))
  }

  /**
   * Take on a release: fetch its tracklist, and optionally fill the fields from it.
   *
   * The fetch is the whole reason this is async. The list these come from is deliberately
   * fetched WITHOUT tracklists, so the release handed in here knows what it is but not what is
   * on it - and the retag payload cannot be built from that. Applying a trackless release
   * writes no titles and no track numbers, which looks exactly like the edit having silently
   * done nothing.
   *
   * `seedFields` is false for the release an album is already tagged with, which is selected
   * automatically after a search: that album already claims to BE this release, so replacing
   * what is on disk with MusicBrainz's version of it would quietly undo hand corrections just
   * because somebody opened the editor.
   */
  const loadRelease = async (release: Release, seedFields: boolean) => {
    setSelectedId(release.id)
    setLoadingRelease(true)
    setSearchError(null)

    //? carried across by hand: they were attached to the list entry from its release GROUP, and
    //? a release fetched on its own has no idea which search produced it
    const groupMbid = (release as { _groupMbid?: string })._groupMbid ?? null
    const firstReleaseDate =
      (release as { _firstReleaseDate?: string | null })._firstReleaseDate ?? null

    try {
      const detail = await musicbrainz.getRelease(release.id)
      const full = { ...detail, _groupMbid: groupMbid, _firstReleaseDate: firstReleaseDate } as Release

      setSelected(full)

      if (seedFields) {
        const built = buildRetagRelease(full, {
          artist: album.artist,
          album: album.album,
          releaseGroupMbid: groupMbid,
          firstReleaseDate,
        })
        setFields({
          artist: built.artist,
          album: built.album,
          year: built.year ?? '',
          originalYear: built.original_year ?? '',
          //? the release's own disambiguation is what the folder name would use anyway, so
          //? leaving this blank lets editions.py decide rather than pinning it
          editionLabel: '',
        })
      }
    } catch (caught) {
      //? Cleared rather than left pointing at a release we could not load. Leaving it selected
      //? would let Apply run against an empty tracklist, which is the one outcome this split
      //? exists to prevent.
      setSelectedId(null)
      setSelected(null)
      setSearchError(
        caught instanceof MusicBrainzUnavailable
          ? "MusicBrainz couldn't be reached for that release's tracklist, so it can't be " +
            'applied yet. You can still correct the fields by hand.'
          : caught instanceof Error ? caught.message : 'could not load that release',
      )
    } finally {
      setLoadingRelease(false)
    }
  }

  const search = useCallback(async () => {
    setSearching(true)
    setSearchError(null)

    try {
      /*
       * Fielded, like the main search view, not free text. "Pink Floyd Wish You Were Here"
       * as a plain string matched a one-track 2013 release group - the song, not the album -
       * and since the folder year comes from the group's first-release-date, matching the
       * wrong group files a 1975 record under 2013.
       *
       * Falls back to free text when the fielded query finds nothing, because MusicBrainz's
       * fielded search is exact enough to miss a slightly-off album title, and finding
       * something imperfect beats finding nothing.
       */
      const fielded = `releasegroup:"${fields.album}" AND artist:"${fields.artist}"`
      //? `false` turns off the eager best-match-releases fetch. This editor ranks the groups
      //? itself and then asks for the ones it wants, so that eager walk - five requests and
      //? 1.4 MB for an album like this one - was spent on a payload it dropped on the floor.
      const first = await musicbrainz.fullySearch(query.trim() || fielded, 25, false)

      let groups = first['release-groups'] ?? []
      if (!groups.length && !query.trim()) {
        const loose = await musicbrainz.fullySearch(`${fields.artist} ${fields.album}`.trim(), 25, false)
        groups = loose['release-groups'] ?? []
      }

      /*
       * Rank the groups before choosing which to look inside.
       *
       * MusicBrainz's own order cannot be trusted for this: searching for Metallica's
       * self-titled album returns five groups all scoring exactly 100, with the real one fifth
       * behind two live albums and an interview disc. Taking the first few as they arrived
       * fetched three wrong groups and never asked for the right one - so no amount of ranking
       * the releases underneath could have surfaced the edition, because it was never fetched.
       *
       * See scoreReleaseGroupMatch for the signals. Sorted rather than filtered, so an unusual
       * album is still reachable - it just isn't paid for first.
       */
      //? Ranked against the FIELDS, not the album on disk. They start out identical, and they
      //? differ exactly when the user has corrected something - so typing the right year in and
      //? searching again is how you steer this, which is what you would expect it to do.
      const target = {
        album: fields.album,
        year: fields.year,
        original_year: fields.originalYear,
      }

      const ranked = [...groups].sort(
        (a, b) => scoreReleaseGroupMatch(b, target) - scoreReleaseGroupMatch(a, target),
      )

      // Releases, not release groups: an edition IS a release, so grouping them would hide
      // exactly the distinction this editor exists to make.
      const found: Release[] = []
      for (const group of ranked.slice(0, GROUPS_SEARCHED)) {
        //? Without tracklists. Telling pressings apart needs their format, country, catalogue
        //? number and track COUNT, all of which survive - not the contents of all 58 of them.
        //? The tracklist of whichever one gets picked is fetched by loadRelease below.
        const detail = await musicbrainz.getReleases(group.id, false)
        for (const release of detail.releases ?? []) {
          //? the group's date, not the release's - it's the album's year, and the folder is
          //? named after it so a remaster doesn't refile the record under its reissue year
          found.push({
            ...release,
            _groupMbid: group.id,
            _firstReleaseDate: group['first-release-date'] ?? null,
          } as Release)
        }
      }

      // best match first. For a tagged album that's the release it already names, which is
      // the whole point - you can see what it currently matches instead of hunting for it.
      found.sort((a, b) => scoreReleaseMatch(b, album) - scoreReleaseMatch(a, album))
      setReleases(found)

      const current = found.find((r) => isCurrentRelease(r, album.release_mbid))
      //? Deliberately without re-seeding the fields: this album already claims to BE this
      //? release, so overwriting what is on disk with MusicBrainz's version of it would undo
      //? corrections the user made by hand simply because they opened the editor.
      if (current) void loadRelease(current, false)
    } catch (caught) {
      setSearchError(
        caught instanceof MusicBrainzUnavailable
          ? "MusicBrainz is unreachable right now, so there's nothing to match against. " +
            'You can still correct the fields by hand.'
          : caught instanceof Error
            ? caught.message
            : 'the search failed',
      )
      setReleases([])
    } finally {
      setSearching(false)
      setSearched(true)
    }
    //? the year fields are dependencies because the group ranking reads them - without them
    //? this callback would close over whatever they held when it was last rebuilt, and
    //? correcting the year would change the query without changing which group won
  }, [query, album, fields.artist, fields.album, fields.year, fields.originalYear])

  /*
   * Search as soon as the panel opens. You opened it to match this album against something,
   * so making that a second click was busywork.
   *
   * Once only, guarded by a ref rather than an empty dep array: `search` is a useCallback
   * that changes identity whenever the artist or album fields do, and this must not re-fire
   * on every keystroke - nor after an apply, where the album prop changes but the release
   * list is still valid and the `current` badge moves on its own from the refreshed MBID.
   *
   * Skipped when there is nothing to search on, so an album with no artist or title tags
   * doesn't send `releasegroup:"" AND artist:""`.
   */
  const autoSearched = useRef(false)

  useEffect(() => {
    if (autoSearched.current) return
    if (!album.artist.trim() && !album.album.trim()) return

    autoSearched.current = true
    void search()
  }, [search, album.artist, album.album])

  /** Picking a release replaces the fields with its values, which you can then still edit. */
  const chooseRelease = (release: Release) => {
    setApplied(null)
    void loadRelease(release, true)
  }

  /**
   * What would be applied: the selected release, with the typed fields laid over the top.
   *
   * With no release selected this still produces a usable payload from the album's own
   * values and an empty tracklist - so hand-editing works without MusicBrainz, and an empty
   * tracklist means no file gets a title or track number it didn't ask for.
   */
  const payload = useMemo((): RetagRelease => {
    const base: RetagRelease = selected
      ? buildRetagRelease(selected, {
          artist: album.artist,
          album: album.album,
          releaseGroupMbid: (selected as { _groupMbid?: string })._groupMbid ?? null,
          firstReleaseDate:
            (selected as { _firstReleaseDate?: string | null })._firstReleaseDate ?? null,
        })
      : {
          artist: album.artist,
          album: album.album,
          year: album.year || null,
          original_year: album.original_year || null,
          //? keep the id the album already carries so the folder can still be disambiguated
          release_mbid: album.release_mbid || null,
          tracks: [],
        }

    return {
      ...base,
      artist: fields.artist,
      album: fields.album,
      year: fields.year || null,
      original_year: fields.originalYear || null,
      edition_label: fields.editionLabel.trim() || null,
    }
  }, [selected, fields, album])

  // Debounced: the plan is a server round trip and this runs on every keystroke.
  useEffect(() => {
    setPlanning(true)

    /*
     * Nothing is planned while a release's tracklist is still arriving.
     *
     * Without this the preview would briefly describe a payload built from no tracklist at all -
     * "nothing to change", or a list of files that "didn't match" - and then correct itself a
     * second later. A preview that disagrees with the write it previews is worse than no
     * preview; one that disagrees with ITSELF is worse again, because it teaches you to
     * distrust the thing whose entire job is to be trusted.
     */
    if (loadingRelease) return

    const timer = setTimeout(async () => {
      try {
        setPlan(await libraryApi.previewRetag(album.path, payload, fetchArt))
        setApplyError(null)
      } catch (caught) {
        setApplyError(caught instanceof Error ? caught.message : 'could not work out the changes')
        setPlan(null)
      } finally {
        setPlanning(false)
      }
    }, PREVIEW_DEBOUNCE_MS)

    return () => clearTimeout(timer)
  }, [album.path, payload, fetchArt, loadingRelease])

  const apply = async () => {
    setApplying(true)
    setApplyError(null)

    try {
      const { results } = await libraryApi.applyRetag(album.path, payload, fetchArt)

      if (results.failed) {
        setApplyError(`${results.failed} file(s) could not be written`)
      } else {
        //? "Retagged 0 file(s)" is a silly thing to say when only the folder changed, which
        //? is exactly what happens if you edit nothing but the edition name
        setApplied(
          [
            results.tagged ? `Retagged ${results.tagged} file(s)` : '',
            results.art_written ? `saved ${results.art_written}` : '',
            results.moved_to ? 're-filed the folder' : '',
          ].filter(Boolean).join(', ').replace(/^./, (c) => c.toUpperCase()) + '.',
        )
        //? where the album lives now - the same path unless the folder was renamed
        onApplied(results.moved_to && plan?.target_path ? plan.target_path : album.path)
      }
    } catch (caught) {
      setApplyError(caught instanceof Error ? caught.message : 'could not apply the changes')
    } finally {
      setApplying(false)
    }
  }

  const taggedRelease = album.release_mbid
  const foundCurrent = releases.some((r) => isCurrentRelease(r, taggedRelease))

  //? What put this album in the queue. Recomputed from the album prop rather than passed in,
  //? so it follows the refreshed album after an apply and the list visibly shrinks as you fix
  //? things - which is the only feedback that the edit did what you wanted.
  const issues = outstandingIssues(album)

  /**
   * Write the cover on its own, replacing whatever is there.
   *
   * Deliberately placed beside the art comparison rather than on the library row: this
   * overwrites a file, and a sleeve you chose yourself is not recoverable once it is gone. Here
   * you are looking at both covers as you decide, which is the whole reason the comparison
   * exists. The cheap, safe direction - an album with NO art - is the one that gets a one-click
   * button out in the list.
   */
  const saveArtOnly = async () => {
    setSavingArt(true)
    setArtResult(null)

    try {
      const result = await libraryApi.fetchCoverArt(album.path, {
        replace: true,
        //? the release whose cover you are looking at, falling back to the one the album
        //? already names when nothing is selected
        releaseMbid: selectedId ?? album.release_mbid ?? null,
      })
      setArtResult(`Saved ${result.written}${result.replaced ? `, replacing ${result.replaced}` : ''}.`)
      onApplied(album.path)
    } catch (caught) {
      setArtResult(caught instanceof Error ? caught.message : 'could not save the cover')
    } finally {
      setSavingArt(false)
    }
  }

  const ignore = async () => {
    setIgnoring(true)
    try {
      await onIgnore(album, issues)
    } finally {
      setIgnoring(false)
    }
  }

  return (
    <div id="metadata-window" onClick={(event) => event.stopPropagation()}>
      <div id="metadata-panel">
        <div id="metadata-header">
          <h4 class="text white">
            {album.album} <span class="text default-secondary">{album.artist}</span>
          </h4>
          <span class="text white-tertiary metadata-path">{album.path}</span>

          {queue && (
            <span class="metadata-queue-position text default-muted">
              {queue.position} of {queue.total}
            </span>
          )}

          <button type="button" id="metadata-close-button" title="Close" onClick={onClose}>✕</button>
        </div>

        {/*
          Why this album is in the queue, spelled out. The chips in the library row are two
          words each because a row has no space; here there is room for the sentence that says
          what actually fixes it, which is the difference between a warning and instructions.
        */}
        {(issues.length > 0 || album.ignored_issues.length > 0) && (
          <div id="metadata-issues">
            {issues.map((code) => (
              <span class="metadata-issue" key={code} title={issueTypes[code]?.hint}>
                <span class="library-issue-chip">{issueLabel(code, issueTypes)}</span>
                <span class="text white-tertiary">{issueTypes[code]?.hint}</span>
              </span>
            ))}

            {album.ignored_issues.length > 0 && (
              <span class="metadata-issue is-ignored">
                <span class="text default-muted">
                  {album.ignored_issues.length} issue(s) ignored on this album
                </span>
                <button
                  type="button"
                  class="metadata-issue-undo"
                  title="Put this album back in the queue"
                  onClick={() => void onUnignore(album)}
                >
                  un-ignore
                </button>
              </span>
            )}
          </div>
        )}

        {/*
          What the album currently claims to be. Without this you can't tell whether it's
          tagged at all, let alone which release it points at.
        */}
        <div id="metadata-current" class="text default-muted">
          <span>currently:</span>
          <span class="text white-tertiary">{album.edition || 'no edition'}</span>
          <span class="text white-tertiary">
            {album.original_year && album.original_year !== album.year
              ? `${album.original_year} (this press ${album.year})`
              : album.year || 'no year'}
          </span>
          <span class="text white-tertiary">{album.track_count} tracks</span>
          {album.disc_count > 1 && (
            <span class="text white-tertiary">{album.disc_count} discs</span>
          )}
          <span class={taggedRelease ? 'metadata-mbid' : 'text yellow'}>
            {taggedRelease
              ? `${taggedRelease.slice(0, 8)}…`
              : 'not tagged with a MusicBrainz release'}
          </span>
        </div>

        <div id="metadata-search">
          <input
            type="text"
            class="releases-filter-input"
            value={query}
            placeholder={`releasegroup:"${fields.album}" AND artist:"${fields.artist}"`}
            title="Leave blank to search on the artist and album fields below"
            onInput={(event) => setQuery((event.target as HTMLInputElement).value)}
            onKeyDown={(event) => { if (event.key === 'Enter') void search() }}
          />
          <button type="button" class="columns-toggle-button" disabled={searching} onClick={() => void search()}>
            {searching ? <Loading label="Searching" /> : 'Find releases'}
          </button>
        </div>

        <div id="metadata-body">
          <div class="scrollable metadata-releases">
            {searchError && <h4 class="text yellow metadata-status">{searchError}</h4>}

            {searching && !releases.length && <LoadingPanel label="Asking MusicBrainz…" />}

            {!searchError && searched && !releases.length && !searching && (
              <h4 class="text default-muted metadata-status">No releases found for that search</h4>
            )}

            {!searched && !searching && (
              <h4 class="text default-muted metadata-status">
                search to match this against a release, or just edit the fields
              </h4>
            )}

            {/* the album names a release that isn't in these results - worth saying, since
                the top result is otherwise indistinguishable from a confirmed match */}
            {searched && releases.length > 0 && taggedRelease && !foundCurrent && (
              <h5 class="text yellow metadata-status">
                the release this album is tagged with isn't in these results
              </h5>
            )}

            {releases.map((release) => {
              const tracks = releaseTrackCount(release)
              const current = isCurrentRelease(release, taggedRelease)

              return (
                <button
                  key={release.id}
                  type="button"
                  class={`metadata-release${selectedId === release.id ? ' active' : ''}${current ? ' current' : ''}`}
                  onClick={() => chooseRelease(release)}
                >
                  {current && <span class="metadata-current-badge" title="This album is tagged with this release">Current</span>}
                  <span class="metadata-release-title">{release.title}</span>
                  <span class="metadata-release-detail text default-muted">
                    {[release.date?.substring(0, 4), describeRelease(release)].filter(Boolean).join(' · ')}
                  </span>
                  <span
                    class={`metadata-release-tracks${tracks && tracks !== album.track_count ? ' mismatch' : ''}`}
                    title={(release.media ?? []).length > 1
                      ? (release.media ?? []).map((m, i) => `disc ${i + 1}: ${m['track-count'] ?? '?'} tracks`).join(', ')
                      : undefined}
                  >
                    {(release.media ?? []).length > 1 ? `${(release.media ?? []).length} discs · ` : ''}
                    {tracks || '?'} trk
                  </span>
                </button>
              )
            })}
          </div>

          <div class="scrollable metadata-plan">
            <div class="metadata-fields">
              <label class="metadata-field">
                <span class="text default-secondary">Artist</span>
                <input class="releases-filter-input" value={fields.artist}
                       onInput={(e) => setField('artist', (e.target as HTMLInputElement).value)} />
              </label>

              <label class="metadata-field">
                <span class="text default-secondary">Album</span>
                <input class="releases-filter-input" value={fields.album}
                       onInput={(e) => setField('album', (e.target as HTMLInputElement).value)} />
              </label>

              <label class="metadata-field metadata-field-short">
                <span class="text default-secondary">Year</span>
                <input class="releases-filter-input" value={fields.year}
                       title="This pressing's year, written to the date tag"
                       onInput={(e) => setField('year', (e.target as HTMLInputElement).value)} />
              </label>

              {/* the album's year rather than this pressing's - it names the folder, so a
                  2011 remaster of a 1975 record still files under 1975 */}
              <label class="metadata-field metadata-field-short">
                <span class="text default-secondary">Original</span>
                <input class="releases-filter-input" value={fields.originalYear}
                       placeholder={fields.year || 'year'}
                       title="The album's first release year — this is what names the folder"
                       onInput={(e) => setField('originalYear', (e.target as HTMLInputElement).value)} />
              </label>

              <label class="metadata-field">
                <span class="text default-secondary">Edition</span>
                <input class="releases-filter-input" value={fields.editionLabel}
                       placeholder={plan?.edition_label || 'worked out from the release'}
                       onInput={(e) => setField('editionLabel', (e.target as HTMLInputElement).value)} />
              </label>
            </div>

            <span class="text white-tertiary metadata-hint">
              Blank fields are left alone rather than cleared. The folder is named after the
              original year and the edition, so a remaster files under the album's own year.
            </span>

            <ArtComparison album={album} releaseId={selectedId} onOpen={() => setComparingArt(true)} />

            {/*
              Just the cover, nothing else. The checkbox below rides along with an apply, which
              also rewrites tags and can rename the folder - a lot to agree to when the sleeve
              is the only thing you came to change.
            */}
            {(selectedId || album.release_mbid) && (
              <div class="metadata-art-actions">
                <button
                  type="button"
                  class="columns-toggle-button"
                  disabled={savingArt}
                  title={
                    album.art
                      ? 'replace the cover on disk with this one, and change nothing else'
                      : 'save this cover into the album folder, and change nothing else'
                  }
                  onClick={() => void saveArtOnly()}
                >
                  {savingArt
                    ? <Loading label="Saving" />
                    : album.art ? 'replace cover only' : 'save cover only'}
                </button>

                {artResult && <span class="text white-tertiary metadata-hint">{artResult}</span>}
              </div>
            )}

            <label class="metadata-checkbox">
              <input type="checkbox" checked={fetchArt}
                     onChange={(e) => setFetchArt((e.target as HTMLInputElement).checked)} />
              <span class="text default-secondary">
                {plan?.art.existing ? `replace ${plan.art.existing}` : 'download cover art'}
              </span>
              <span class="text white-tertiary metadata-hint">
                {plan?.art.existing
                  ? 'this album already has a cover; only overwrite it if the new one is better'
                  : 'from the Cover Art Archive, saved into the album folder'}
              </span>
            </label>

            {planning && (
              <h5 class="metadata-status">
                {/* naming the actual wait: the tracklist arrives separately from the list of
                    pressings, so "working out the changes" would be describing the wrong step */}
                <Loading label={loadingRelease ? 'fetching the tracklist' : 'working out the changes'} />
              </h5>
            )}

            {!planning && plan && (
              <>
                {plan.problems.map((problem) => (
                  <h5 class="text yellow metadata-problem" key={problem}>{problem}</h5>
                ))}

                {plan.moves && (
                  <div class="metadata-move">
                    <span class="text default-secondary">Folder</span>
                    <span class="text white-tertiary">{plan.album_path}</span>
                    <span class="text default">→ {plan.target_path}</span>
                  </div>
                )}

                {plan.empty && (
                  <h5 class="text default-muted metadata-status">Nothing to change</h5>
                )}

                {plan.files.filter((file) => Object.keys(file.changes).length).map((file) => (
                  <div class="metadata-file" key={file.filename}>
                    <div class="metadata-file-name text white">{file.filename}</div>
                    {Object.entries(file.changes).map(([tag, change]) => (
                      <div class="metadata-change" key={tag}>
                        <span class="metadata-tag text default-secondary">{tag}</span>
                        <span class="metadata-from text white-tertiary">{change.from || '—'}</span>
                        <span class="metadata-to text default">{change.to}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </>
            )}
          </div>
        </div>

        <div id="metadata-footer">
          {applied && <span class="text green metadata-result">{applied}</span>}
          {applyError && <span class="text red metadata-result">{applyError}</span>}

          {!applyError && plan && !plan.empty && (
            <span class="text default-muted metadata-summary">
              {plan.changed_file_count} of {plan.file_count} file(s) would change
              {plan.art.action === 'download' ? ', cover art would be downloaded' : ''}
              {plan.art.action === 'replace' ? ', the cover art would be replaced' : ''}
              {plan.moves ? ', and the folder would be renamed' : ''}
            </span>
          )}

          {/*
            Accepting an album as it is. Offered wherever the editor is opened from, not just
            in queue mode: deciding a bootleg will never be in MusicBrainz is a thing you
            realise while looking at it, and making you find a different button for that would
            mean the queue keeps asking.
          */}
          {issues.length > 0 && (
            <button
              type="button"
              class="columns-toggle-button metadata-ignore-button"
              disabled={ignoring}
              title={`stop asking about: ${issues.map((c) => issueLabel(c, issueTypes)).join(', ')}`}
              onClick={() => void ignore()}
            >
              {ignoring ? <Loading label="Ignoring" /> : "It's fine as it is"}
            </button>
          )}

          {queue && (
            <div class="metadata-queue-nav">
              <button
                type="button"
                class="columns-toggle-button"
                disabled={queue.position <= 1}
                title="The previous album in the queue"
                onClick={queue.onPrevious}
              >
                ◁
              </button>
              <button
                type="button"
                class="columns-toggle-button"
                disabled={queue.position >= queue.total}
                title="Leave this one for now and move on"
                onClick={queue.onNext}
              >
                skip ▷
              </button>
            </div>
          )}

          <button type="button" class="columns-toggle-button" onClick={onClose}>
            {applied ? 'Close' : 'Cancel'}
          </button>

          {/*
            In queue mode a finished album hands you straight to the next one - going back to
            the list and picking the next by hand is the busywork this whole thing exists to
            remove. The last album has nowhere to go, so it keeps the plain close.
          */}
          {applied && queue && queue.position < queue.total ? (
            <button type="button" id="metadata-apply-button" onClick={queue.onNext}>
              Next album ▷
            </button>
          ) : (
            <button
              type="button"
              id="metadata-apply-button"
              disabled={!plan || plan.empty || planning || applying || loadingRelease}
              onClick={() => void apply()}
            >
              {applying ? <Loading label="Applying" /> : 'Apply'}
            </button>
          )}
        </div>
      </div>

      {/*
        Both covers at full size. The incoming one is the release you have selected, or failing
        that the one the album is already tagged with - the same id "replace cover only" would
        fetch, so what the button saves is exactly what you were looking at.
      */}
      {comparingArt && (() => {
        const incomingId = selectedId ?? (album.release_mbid || null)
        return (
          <ArtViewer
            images={[
              {
                label: 'Current',
                sources: album.art ? [albumArtUrl(album)] : [],
                missing: 'No cover on disk',
              },
              {
                label: selectedId ? 'From this release' : "From the release it's tagged with",
                sources: incomingId ? [`https://coverartarchive.org/release/${incomingId}/front`] : [],
                missing: incomingId
                  ? 'The Cover Art Archive has no front cover for this release, or is unreachable'
                  : 'Pick a release to see its cover',
              },
            ]}
            onClose={() => setComparingArt(false)}
            action={incomingId ? {
              label: album.art ? 'Replace with this cover' : 'Save this cover',
              title: 'Write the right-hand cover into the album folder and change nothing else',
              busy: savingArt,
              //? only once the right-hand cover has actually loaded - see ArtViewer
              requires: 1,
              onClick: () => {
                void saveArtOnly().then(() => setComparingArt(false))
              },
            } : undefined}
          />
        )
      })()}
    </div>
  )
}
