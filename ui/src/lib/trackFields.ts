import type { LibraryTrack, TrackDetails } from '../api/types'
import { formatSize } from './format'

/**
 * The fields the library's track viewer can show, and how to read each one.
 *
 * One registry for both places a field appears - as a column in an album's track table and as
 * a row in one track's property list - so the menu that toggles them means the same thing in
 * both. That is the whole reason for a registry rather than hand-written markup: the viewer's
 * contents are chosen by the user, so the markup cannot know them in advance.
 */

/**
 * Everything the viewer knows about one track.
 *
 * `track` is the scan's copy: always present, so the basic columns fill in the moment an album
 * is selected. `details` is the file read just now by /library/tracks, and arrives a moment
 * later. Where both have a value the file's own wins, because it is the fresher of the two - the
 * scan is cached on the folder's mtime, which a retag by another tool doesn't move.
 */
export interface TrackRow {
  track: LibraryTrack
  details: TrackDetails | undefined
}

export type FieldGroup = 'Tags' | 'Audio' | 'MusicBrainz' | 'File'

export const FIELD_GROUPS: readonly FieldGroup[] = ['Tags', 'Audio', 'MusicBrainz', 'File']

export interface TrackField {
  id: string
  label: string
  group: FieldGroup
  /** A CSS grid track size, for the table column. */
  width: string
  align?: 'right'
  /** Set in the data face, because it is a number or an id that reads in columns. */
  mono?: boolean
  /** On before anyone has chosen. */
  initial: boolean
  /**
   * True when the scan already carries this, so the cell can fill in before the file's own
   * details arrive. False fields show a placeholder until then rather than a misleading blank.
   */
  fromScan: boolean
  value: (row: TrackRow) => string
}

/** A tag from the file's details, or '' until they arrive. */
const tag = (key: string) => (row: TrackRow) => row.details?.tags[key] ?? ''

function trackTime(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const whole = Math.round(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

function channels(n: number | null | undefined): string {
  if (!n) return ''
  if (n === 1) return 'Mono'
  if (n === 2) return 'Stereo'
  if (n === 6) return '5.1'
  return `${n} ch`
}

export const TRACK_FIELDS: readonly TrackField[] = [
  {
    id: 'number', label: '#', group: 'Tags', width: '2.6em', align: 'right', mono: true,
    initial: true, fromScan: true,
    value: ({ track, details }) => {
      const n = details?.position ?? track.position
      return n === null || n === undefined ? '' : String(n).padStart(2, '0')
    },
  },
  {
    id: 'disc', label: 'Disc', group: 'Tags', width: '3.8em', align: 'right', mono: true,
    initial: true, fromScan: true,
    value: ({ track, details }) => {
      const n = details?.disc ?? track.disc
      return n === null || n === undefined ? '' : String(n)
    },
  },
  {
    id: 'artist', label: 'Artist', group: 'Tags', width: 'minmax(8em, 0.7fr)', initial: true,
    fromScan: true, value: ({ track, details }) => details?.tags['artist'] ?? track.artist,
  },
  {
    id: 'albumartist', label: 'Album artist', group: 'Tags', width: 'minmax(8em, 0.6fr)',
    initial: false, fromScan: true,
    value: ({ track, details }) => details?.tags['albumartist'] ?? track.albumartist,
  },
  {
    id: 'album', label: 'Album', group: 'Tags', width: 'minmax(8em, 0.6fr)', initial: false,
    fromScan: true, value: ({ track, details }) => details?.tags['album'] ?? track.album,
  },
  {
    id: 'date', label: 'Date', group: 'Tags', width: '6.5em', mono: true, initial: false,
    fromScan: true, value: ({ track, details }) => details?.tags['date'] ?? track.date,
  },
  {
    id: 'originaldate', label: 'Original date', group: 'Tags', width: '6.5em', mono: true,
    initial: false, fromScan: true,
    value: ({ track, details }) => details?.tags['originaldate'] ?? track.originaldate ?? '',
  },
  { id: 'genre', label: 'Genre', group: 'Tags', width: 'minmax(6em, 0.5fr)', initial: false, fromScan: false, value: tag('genre') },
  { id: 'composer', label: 'Composer', group: 'Tags', width: 'minmax(7em, 0.5fr)', initial: false, fromScan: false, value: tag('composer') },
  { id: 'conductor', label: 'Conductor', group: 'Tags', width: 'minmax(7em, 0.5fr)', initial: false, fromScan: false, value: tag('conductor') },
  { id: 'lyricist', label: 'Lyricist', group: 'Tags', width: 'minmax(7em, 0.5fr)', initial: false, fromScan: false, value: tag('lyricist') },
  { id: 'discsubtitle', label: 'Disc subtitle', group: 'Tags', width: 'minmax(7em, 0.5fr)', initial: false, fromScan: false, value: tag('discsubtitle') },
  {
    id: 'label', label: 'Label', group: 'Tags', width: 'minmax(7em, 0.5fr)', initial: false,
    fromScan: false,
    //? Vorbis files use either name and ID3 only knows `organization` - show whichever is there
    value: ({ details }) => details?.tags['label'] ?? details?.tags['organization'] ?? '',
  },
  { id: 'catalognumber', label: 'Catalog #', group: 'Tags', width: '8em', mono: true, initial: false, fromScan: false, value: tag('catalognumber') },
  { id: 'barcode', label: 'Barcode', group: 'Tags', width: '9em', mono: true, initial: false, fromScan: false, value: tag('barcode') },
  { id: 'isrc', label: 'ISRC', group: 'Tags', width: '9em', mono: true, initial: false, fromScan: false, value: tag('isrc') },
  { id: 'releasecountry', label: 'Country', group: 'Tags', width: '4.5em', initial: false, fromScan: false, value: tag('releasecountry') },
  { id: 'media', label: 'Media', group: 'Tags', width: '6em', initial: false, fromScan: false, value: tag('media') },
  { id: 'bpm', label: 'BPM', group: 'Tags', width: '3.5em', align: 'right', mono: true, initial: false, fromScan: false, value: tag('bpm') },
  { id: 'language', label: 'Language', group: 'Tags', width: '5em', initial: false, fromScan: false, value: tag('language') },
  { id: 'copyright', label: 'Copyright', group: 'Tags', width: 'minmax(8em, 0.5fr)', initial: false, fromScan: false, value: tag('copyright') },
  { id: 'comment', label: 'Comment', group: 'Tags', width: 'minmax(8em, 0.6fr)', initial: false, fromScan: false, value: tag('comment') },

  //? widths are measured against the HEADER label plus its padding, not the values - "Sample
  //? rate" is far wider than "44.1 kHz", and a header cut to "Samp…" names nothing
  {
    id: 'length', label: 'Length', group: 'Audio', width: '5em', align: 'right', mono: true,
    initial: true, fromScan: true,
    value: ({ track, details }) => trackTime(details?.length ?? track.length),
  },
  {
    id: 'format', label: 'Format', group: 'Audio', width: '5em', initial: true, fromScan: true,
    value: ({ track }) => track.format.toUpperCase(),
  },
  { id: 'codec', label: 'Codec', group: 'Audio', width: '6em', initial: false, fromScan: false, value: ({ details }) => details?.codec ?? '' },
  {
    id: 'bitrate', label: 'Bitrate', group: 'Audio', width: '6em', align: 'right', mono: true,
    initial: true, fromScan: false,
    value: ({ details }) => (details?.bitrate ? `${Math.round(details.bitrate / 1000)} kbps` : ''),
  },
  {
    id: 'sample_rate', label: 'Sample rate', group: 'Audio', width: '7em', align: 'right',
    mono: true, initial: true, fromScan: false,
    value: ({ details }) =>
      details?.sample_rate ? `${(details.sample_rate / 1000).toFixed(details.sample_rate % 1000 ? 1 : 0)} kHz` : '',
  },
  {
    id: 'bits', label: 'Bit depth', group: 'Audio', width: '6em', align: 'right', mono: true,
    initial: true, fromScan: false,
    value: ({ details }) => (details?.bits_per_sample ? `${details.bits_per_sample}-bit` : ''),
  },
  { id: 'channels', label: 'Channels', group: 'Audio', width: '6em', initial: false, fromScan: false, value: ({ details }) => channels(details?.channels) },

  { id: 'musicbrainz_trackid', label: 'Recording ID', group: 'MusicBrainz', width: '19em', mono: true, initial: false, fromScan: false, value: tag('musicbrainz_trackid') },
  { id: 'musicbrainz_releasetrackid', label: 'Track ID', group: 'MusicBrainz', width: '19em', mono: true, initial: false, fromScan: false, value: tag('musicbrainz_releasetrackid') },
  {
    id: 'musicbrainz_albumid', label: 'Release ID', group: 'MusicBrainz', width: '19em', mono: true,
    initial: false, fromScan: true,
    value: ({ track, details }) => details?.tags['musicbrainz_albumid'] ?? track.release_mbid,
  },
  { id: 'musicbrainz_releasegroupid', label: 'Release group ID', group: 'MusicBrainz', width: '19em', mono: true, initial: false, fromScan: false, value: tag('musicbrainz_releasegroupid') },
  { id: 'musicbrainz_artistid', label: 'Artist ID', group: 'MusicBrainz', width: '19em', mono: true, initial: false, fromScan: false, value: tag('musicbrainz_artistid') },

  {
    id: 'size', label: 'Size', group: 'File', width: '5em', align: 'right', mono: true,
    initial: false, fromScan: true, value: ({ track }) => formatSize(track.size),
  },
  {
    id: 'filename', label: 'File name', group: 'File', width: 'minmax(10em, 1fr)', mono: true,
    initial: false, fromScan: true, value: ({ track }) => track.filename,
  },
]

const FIELD_BY_ID = new Map(TRACK_FIELDS.map((field) => [field.id, field]))

export function fieldById(id: string): TrackField | undefined {
  return FIELD_BY_ID.get(id)
}

/**
 * The visible field ids, reconciled against what exists now.
 *
 * Ids that no longer exist are dropped, and fields the stored choice has never heard of take
 * their own default. Without the second half a field added in a later version would be off for
 * everyone who had ever touched the menu, with nothing to say it existed.
 */
export function reconcileVisible(saved: { visible: string[]; seen: string[] } | null): string[] {
  if (!saved) return TRACK_FIELDS.filter((f) => f.initial).map((f) => f.id)

  const seen = new Set(saved.seen)
  const visible = new Set(saved.visible)

  return TRACK_FIELDS
    .filter((field) => (seen.has(field.id) ? visible.has(field.id) : field.initial))
    .map((field) => field.id)
}
