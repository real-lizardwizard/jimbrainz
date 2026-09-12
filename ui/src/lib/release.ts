import type { Release, ReleaseGroup, RetagRelease, Track } from '../api/types'

const EDITION_KEYWORDS: ReadonlyArray<[RegExp, string]> = [
  [/super deluxe/, 'SUPER DELUXE'],
  [/deluxe/, 'DELUXE'],
  [/box set|boxset/, 'BOX SET'],
  [/anniversary/, 'ANNIVERSARY'],
  [/expanded/, 'EXPANDED'],
  [/limited edition/, 'LIMITED'],
  [/special edition/, 'SPECIAL EDITION'],
]

/**
 * Edition markers detected from a release's own text.
 *
 * Mirrors getEditionTags() in interface/scripts/main.js and EDITION_PATTERNS in
 * src/matching.py — the same vocabulary in three places because the frontend tags the
 * release, the backend tags the candidate folder, and then they get compared. Changing one
 * without the others silently weakens the edition signal.
 */
export function detectEditionTags(release: Release): string[] {
  const haystack = `${release.disambiguation ?? ''} ${release.title ?? ''}`.toLowerCase()
  const tags: string[] = []

  if (haystack.includes('remaster')) tags.push('REMASTER')

  for (const [pattern, label] of EDITION_KEYWORDS) {
    if (pattern.test(haystack)) {
      tags.push(label)
      // "super deluxe" also trips the plain "deluxe" pattern on the same text
      if (label === 'SUPER DELUXE') break
    }
  }

  if (release.packaging === 'Box' && !tags.includes('BOX SET')) tags.push('BOX SET')

  return tags
}

/**
 * Every track of every disc, with both numberings.
 *
 * `position` is one running sequence across the whole release, because the matcher keys its
 * file mapping on it and the organizer names files after it - a 2xCD set has two "track 1"s,
 * and a folder holding both would otherwise sort them side by side. `disc` and `disc_position`
 * are MusicBrainz's own numbering, and they are what a multi-disc release is TAGGED with.
 *
 * Must produce the same shape as buildExpectedFromRelease() in interface/scripts/main.js, so an
 * album corrected here carries the same tags as one downloaded fresh.
 */
function flattenTracks(release: Release): Track[] {
  const tracks: Track[] = []
  let position = 0

  for (const [discIndex, medium] of (release.media ?? []).entries()) {
    const discTracks = (medium as { tracks?: unknown[] }).tracks ?? []
    const disc = (medium as { position?: number }).position ?? discIndex + 1

    for (const [trackIndex, raw] of discTracks.entries()) {
      const entry = raw as {
        title?: string
        position?: number
        length?: number | null
        recording?: { title?: string; length?: number | null }
      }
      position += 1
      tracks.push({
        position,
        title: entry.recording?.title || entry.title || '',
        length_ms: entry.recording?.length ?? entry.length ?? null,
        disc,
        disc_position: entry.position ?? trackIndex + 1,
      })
    }
  }

  return tracks
}

/**
 * Turn a MusicBrainz release into the payload the retag endpoints take.
 *
 * The typed counterpart of buildExpectedFromRelease() in main.js, and deliberately produces
 * the same shape: an album corrected by hand should end up carrying exactly the tags one
 * downloaded fresh would have, rather than a second dialect of the same thing.
 *
 * When the search view is ported this replaces the vanilla function outright.
 */
export function buildRetagRelease(
  release: Release,
  context: {
    artist: string
    album: string
    releaseGroupMbid?: string | null
    /** The release group's first-release-date — the album's own year, not this pressing's. */
    firstReleaseDate?: string | null
  },
): RetagRelease {
  const rawDate = release['release-events']?.[0]?.date || release.date || ''
  const catalogNumbers = (release['label-info'] ?? [])
    .map((entry) => entry['catalog-number'])
    .filter((value): value is string => Boolean(value))

  return {
    artist: context.artist,
    album: release.title || context.album,
    year: rawDate ? rawDate.substring(0, 4) : null,
    original_year: context.firstReleaseDate ? context.firstReleaseDate.substring(0, 4) : null,
    release_mbid: release.id,
    release_group_mbid: context.releaseGroupMbid ?? null,
    disambiguation: release.disambiguation || null,
    media_format: (release.media ?? []).map((m) => m.format).filter(Boolean).join(' + ') || null,
    // the raw ISO code, NOT the flag-display mapping — src/editions.py recognises and
    // discards the "worldwide" codes rather than labelling a folder "[UN]"
    country: release['release-events']?.[0]?.area?.['iso-3166-1-codes']?.[0] ?? release.country ?? null,
    catalog_number: catalogNumbers.join(', ') || null,
    edition_tags: detectEditionTags(release),
    tracks: flattenTracks(release),
  }
}

/** A short human description of a release, for picking between siblings in a list. */
export function describeRelease(release: Release): string {
  const parts = [
    release.disambiguation,
    (release.media ?? []).map((m) => m.format).filter(Boolean).join(' + '),
    release['release-events']?.[0]?.area?.['iso-3166-1-codes']?.[0],
    (release['label-info'] ?? []).map((l) => l['catalog-number']).filter(Boolean)[0],
  ].filter(Boolean)

  return parts.join(' · ')
}

/** Total tracks across all discs, for comparing a release against what's on disk. */
export function releaseTrackCount(release: Release): number {
  return (release.media ?? []).reduce((total, m) => total + (m['track-count'] ?? 0), 0)
}

/**
 * How well a release matches an album already on disk. Higher is better.
 *
 * The MBID is decisive and everything else is a tiebreak, which is the same shape as
 * matching.py's candidate scoring: identity first, resemblance second. An album tagged with
 * a release id IS that release — there's nothing to weigh up — so it outranks any amount of
 * agreement on title and track count.
 *
 * The rest only matters for albums with no MBID at all, where the best that can be done is
 * to float the plausible ones up so you aren't reading a list of twenty pressings in the
 * order MusicBrainz happened to return them.
 */
export function scoreReleaseMatch(
  release: Release,
  album: { release_mbid: string; album: string; year: string; track_count: number },
): number {
  if (album.release_mbid && release.id === album.release_mbid) return 1000

  let score = 0

  const tracks = releaseTrackCount(release)
  if (tracks && tracks === album.track_count) score += 100

  const year = (release['release-events']?.[0]?.date || release.date || '').substring(0, 4)
  if (year && year === album.year) score += 50

  if ((release.title ?? '').toLowerCase() === album.album.toLowerCase()) score += 25

  return score
}

/** True when this release is the one the album's tags already name. */
export function isCurrentRelease(release: Release, albumReleaseMbid: string): boolean {
  return Boolean(albumReleaseMbid) && release.id === albumReleaseMbid
}

/**
 * Secondary types that are usually NOT the copy sitting in someone's library.
 *
 * A penalty, never a filter — the same trade edition matching makes. People genuinely do own
 * live albums and compilations, and the year signal below outweighs this, so tagging the 1996
 * live album as 1996 still picks it. This only breaks the tie when nothing else can.
 */
const UNLIKELY_SECONDARY_TYPES = new Set([
  'Live', 'Compilation', 'Interview', 'Remix', 'DJ-mix', 'Demo', 'Mixtape/Street', 'Audiobook',
])

/**
 * How well a release GROUP matches an album on disk. Higher is better.
 *
 * This exists because MusicBrainz's own ordering cannot answer the question. Searching
 * `releasegroup:"Metallica" AND artist:"Metallica"` returns 25 groups, of which the first FIVE
 * all score exactly 100 — a live album, another live album, an interview disc, a compilation,
 * and, in fifth place, the actual 1991 album. Taking the first few in the order they arrived
 * meant fetching three wrong groups and never asking for the right one, so the edition the user
 * wanted could not appear no matter how the releases underneath were ranked.
 *
 * The signals are all group-level, because that is all there is before committing a request per
 * group — track counts and pressings only exist once you have paid for them.
 *
 * Weighted rather than ordered, so no one signal can be decisive on its own:
 *
 *   year          100  the strongest thing we have. An album tagged 1991 belongs to the group
 *                      first released in 1991, whatever type either of them is.
 *   exact title    40  "Metallica" beats "Mandatory Metallica" — MusicBrainz scores both 100.
 *   studio album   30  primary type Album with no secondary type. What a library mostly holds.
 *   unlikely type −30  see above. Never enough to overturn a matching year.
 *   MB's score    ÷10  a tiebreak only, and a weak one — it is what fails here.
 */
export function scoreReleaseGroupMatch(
  group: ReleaseGroup,
  album: { album: string; year: string; original_year: string },
): number {
  let score = 0

  //? the album's own year, not a pressing's — that is what a release group's date is
  const wanted = (album.original_year || album.year || '').trim()
  const groupYear = (group['first-release-date'] ?? '').substring(0, 4)
  if (wanted && groupYear && wanted === groupYear) score += 100

  if ((group.title ?? '').trim().toLowerCase() === album.album.trim().toLowerCase()) score += 40

  const secondary = group['secondary-types'] ?? []

  if (group['primary-type'] === 'Album' && !secondary.length) score += 30

  if (secondary.some((type) => UNLIKELY_SECONDARY_TYPES.has(type))) score -= 30

  const reported = Number((group as { score?: number }).score ?? 0)
  if (Number.isFinite(reported)) score += reported / 10

  return score
}
