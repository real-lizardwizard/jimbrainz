import { useEffect, useState } from 'preact/hooks'

import * as libraryApi from '../api/library'
import type { LibraryAlbum, MetadataIssueType } from '../api/types'
import { albumArtUrl } from '../lib/format'
import { describeIssues, issueLabel } from '../lib/metadataQueue'
import { Loading } from './Loading'

/**
 * Small pieces the library's tree and details pane both draw. They lived in the old album-row
 * component, which the tree replaced.
 */

export type CoverSize = 'thumb' | 'large' | 'full'

/**
 * Where an album's cover can come from, best first.
 *
 *   1. the library's own art endpoint - a cover file beside the tracks, or art embedded in the
 *      audio. Works offline and shows what is actually on disk. It serves the file whole, so
 *      it is already full size.
 *   2. the Cover Art Archive, keyed on the RELEASE id - the edition's own art, not the group's.
 *      `full` asks for the original upload rather than a thumbnail, which is what you want when
 *      deciding between two covers: a 250px preview hides exactly the difference in quality.
 *
 * Fallback is driven by the image failing to load rather than by probing first - the browser
 * reports a failure for free, and a HEAD request per album would double the traffic.
 */
export function coverSources(album: LibraryAlbum, size: CoverSize): string[] {
  const sources: string[] = []
  if (album.art) sources.push(albumArtUrl(album))
  if (album.release_mbid) {
    const variant = size === 'full' ? 'front' : size === 'large' ? 'front-500' : 'front-250'
    sources.push(`https://coverartarchive.org/release/${album.release_mbid}/${variant}`)
  }
  return sources
}

export function AlbumArt(
  { album, size = 'thumb', class: className = 'library-art', onOpen }:
  //? `| undefined` is not redundant: exactOptionalPropertyTypes is on, and a caller that only
  //? sometimes has a cover to open passes undefined explicitly
  { album: LibraryAlbum | undefined; size?: CoverSize; class?: string; onOpen?: (() => void) | undefined },
) {
  const sources = album ? coverSources(album, size) : []
  const [attempt, setAttempt] = useState(0)

  //? a different album, or a replaced cover, starts again at the best source - otherwise one
  //? failure would carry over to every album this instance is later asked to draw
  useEffect(() => setAttempt(0), [album?.path, album?.art_mtime])

  const src = sources[attempt]

  // an empty square rather than nothing, so rows don't jump around as art loads or fails
  if (!src) return <div class={`${className} is-empty`} aria-hidden="true" />

  const image = (
    <img
      class={className}
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setAttempt((n) => n + 1)}
    />
  )

  if (!onOpen) return image

  return (
    <button type="button" class="library-art-open" title="View full size" onClick={onOpen}>
      {image}
    </button>
  )
}

//? Two chips and a count at most. An unidentified album trips five or six rules at once - all
//? downstream of the one fact that it was never matched to a release - and a row that turns into
//? a wall of amber for a single underlying problem reads as far worse than it is.
export function IssueChips(
  { issues, types, ignored, max = 2 }:
  { issues: readonly string[]; types: Record<string, MetadataIssueType>; ignored?: boolean; max?: number },
) {
  if (!issues.length) return null

  const shown = issues.slice(0, max)
  const rest = issues.length - shown.length

  return (
    <span
      class={`library-issues${ignored ? ' is-ignored' : ''}`}
      title={describeIssues(issues, types)}
    >
      {shown.map((code) => (
        <span class="library-issue-chip" key={code}>{issueLabel(code, types)}</span>
      ))}
      {rest > 0 && <span class="library-issue-more">+{rest}</span>}
    </span>
  )
}

/**
 * Fetch just the cover for one album.
 *
 * Rendered only for albums that have no art but DO name a release, which is exactly the set it
 * can help - there is nothing to look a cover up by otherwise, and an album that already has
 * one isn't asking.
 */
export function GetArtButton(
  { album, onDone, class: className = 'commandbar-button' }:
  { album: LibraryAlbum; onDone: () => void; class?: string },
) {
  const [state, setState] = useState<'idle' | 'working' | 'failed'>('idle')

  if (album.art || !album.release_mbid) return null

  const fetchArt = async (event: MouseEvent) => {
    event.stopPropagation()
    setState('working')

    try {
      await libraryApi.fetchCoverArt(album.path)
      onDone()
    } catch (caught) {
      setState('failed')
      console.error(caught)
    }
  }

  return (
    <button
      type="button"
      class={`${className}${state === 'failed' ? ' failed' : ''}`}
      /*
       * Disabled only while a request is actually in flight. A failure leaves it clickable on
       * purpose: the usual reason is that the Archive has no front cover for this release, but
       * the second usual reason is that the Archive was briefly unreachable - it goes away for
       * minutes at a time, exactly like MusicBrainz - and a button that latches off after one
       * bad answer turns a passing outage into a permanent dead end with no way to retry.
       */
      disabled={state === 'working'}
      title={
        state === 'failed'
          ? 'no front cover came back for this release - the Archive may not have one, or may '
            + 'have been briefly unreachable. Click to try again.'
          : 'download a cover for this album and change nothing else'
      }
      onClick={(event) => void fetchArt(event as unknown as MouseEvent)}
    >
      {state === 'working' ? <Loading /> : state === 'failed' ? 'Retry cover' : 'Get cover'}
    </button>
  )
}

/** A small person, for artist rows. Drawn in currentColor so it follows the row's state. */
export function ArtistIcon() {
  return (
    <svg class="tree-icon" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="8" cy="5" r="3" fill="currentColor" />
      <path d="M2.5 14.5c0-3.2 2.5-5.3 5.5-5.3s5.5 2.1 5.5 5.3z" fill="currentColor" />
    </svg>
  )
}
