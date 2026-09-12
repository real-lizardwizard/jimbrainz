/**
 * Display formatting. Behaviour matches the vanilla helpers in interface/scripts/main.js
 * exactly - these render into the same panels, and a unit that changes mid-migration reads
 * as a bug.
 */

/** Bytes per second. Returns 'unknown' for 0/falsy, same as the vanilla helper. */
export function formatSpeed(bytesPerSecond: number | null | undefined): string {
  if (!bytesPerSecond) return 'unknown'

  const kb = bytesPerSecond / 1024
  if (kb < 1024) return `${Math.round(kb)} KB/s`
  return `${(kb / 1024).toFixed(1)} MB/s`
}

/** Returns '' for 0/falsy so it can be dropped into a joined list without a gap. */
export function formatSize(bytes: number | null | undefined): string {
  if (!bytes) return ''

  const mb = bytes / (1024 * 1024)
  if (mb < 1024) return `${Math.round(mb)} MB`
  return `${(mb / 1024).toFixed(1)} GB`
}

/** An album's running time, coarse: "42 min", "1h 12m". '' for zero. */
export function formatDuration(seconds: number): string {
  if (!seconds) return ''
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** A track's running time, "4:05". '' for zero. */
export function trackTime(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const whole = Math.round(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/**
 * How long ago a unix timestamp (seconds) was, roughly. '' when unknown.
 *
 * Deliberately coarse. It labels a saved scan so you can judge whether to trust it, and "3 h
 * ago" answers that where a timestamp would make you do arithmetic.
 */
export function formatAge(unixSeconds: number | null | undefined, now = Date.now()): string {
  if (!unixSeconds) return ''
  const seconds = Math.max(0, Math.round(now / 1000 - unixSeconds))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  const days = Math.floor(seconds / 86400)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

/**
 * The URL for an album's cover as it exists on disk.
 *
 * `v` is the art file's own mtime, and it is not decoration. The endpoint sends
 * `Cache-Control: private, max-age=300` so that scrolling a large library doesn't refetch
 * every sleeve, which means a URL that never changes gets answered from the browser cache for
 * five minutes - including immediately after you have just replaced the cover, which is
 * precisely when you are looking at it.
 *
 * The ART's mtime rather than the album's: replacing cover.jpg in place doesn't touch the
 * DIRECTORY's mtime, so `modified_at` holds still through the one operation that has to be
 * noticed. See read_album_dir.
 */
export function albumArtUrl(album: { path: string; art_mtime?: number }): string {
  const version = album.art_mtime ?? 0
  return `/jimbrainz/library/art?album=${encodeURIComponent(album.path)}&v=${version}`
}
