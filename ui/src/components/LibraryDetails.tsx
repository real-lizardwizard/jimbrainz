import { Fragment, type ComponentChildren } from 'preact'
import { useEffect, useMemo, useRef, useState } from 'preact/hooks'

import type { LibraryAlbum, LibraryTrack, MetadataIssueType } from '../api/types'
import type { TrackDetailsState } from '../hooks/useTrackDetails'
import { useTrackFields } from '../hooks/useTrackFields'
import { formatAge, formatDuration, formatSize, trackTime } from '../lib/format'
import type { AlbumGroup } from '../lib/groupAlbums'
import {
  editionNodeId, groupAddedAt, groupNodeId, nodeIdForAlbum, trackNodeId, type Selected,
} from '../lib/libraryTree'
import { isNewImport, outstandingIssues } from '../lib/metadataQueue'
import {
  fieldById, FIELD_GROUPS, TRACK_FIELDS, type TrackField, type TrackRow,
} from '../lib/trackFields'
import { ArtViewer } from './ArtViewer'
import { AlbumArt, ArtistIcon, coverSources, GetArtButton } from './LibraryParts'
import { Loading } from './Loading'

export interface LibrarySummary {
  albums: number
  artists: number
  tracks: number
  size: number
  duration: number
  libraryPath: string
  scannedAt: number | null
  stale: boolean
}

interface Props {
  selected: Selected
  selectedId: string | null
  /** The selected album's files, read live. */
  details: TrackDetailsState
  issueTypes: Record<string, MetadataIssueType>
  /** Every album, for the overview's "recently changed" shelf. */
  groups: readonly AlbumGroup[]
  summary: LibrarySummary
  onSelect: (id: string) => void
  onEdit: (album: LibraryAlbum) => void
  onDelete: (album: LibraryAlbum) => void
  onArtFetched: () => void
  onSearchArtist: (artist: string) => void
  onSearchAlbum: (group: AlbumGroup) => void
  /** Phones only: the pane is a sheet over the tree there, and this closes it. */
  onBack: () => void
}

/**
 * The right-hand pane: whatever is selected in the tree, in detail.
 *
 * This is where the space went. The old list gave every album a full-width row whose middle was
 * empty; the tree now carries the navigating, and this pane spends the rest of the width on the
 * one thing you are looking at - its cover, its properties, what is wrong with it, and every
 * track with whichever fields you asked to see.
 *
 * Laid out like Explorer on purpose: a command bar across the top whose commands follow the
 * selection, and a details view below it whose columns are yours to choose.
 */
export function LibraryDetails(props: Props) {
  const { selected, selectedId, details, issueTypes, groups, summary } = props
  const [fields, toggleField, resetFields] = useTrackFields()
  const [menuOpen, setMenuOpen] = useState(false)
  const [viewing, setViewing] = useState<LibraryAlbum | null>(null)

  const album = selected.kind === 'album' || selected.kind === 'track' ? selected.album : null
  const group = selected.kind === 'group' || selected.kind === 'album' || selected.kind === 'track'
    ? selected.group
    : null

  //? a new selection starts at the top - landing halfway down an artist because the previous
  //? album's tracklist was scrolled would read as the pane showing the wrong thing
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => bodyRef.current?.scrollTo({ top: 0 }), [selectedId])

  const body = (() => {
    switch (selected.kind) {
      case 'none':
        return <Overview summary={summary} groups={groups} onSelect={props.onSelect} />
      case 'artist':
        return (
          <section class="details-section">
            <div class="details-header">
              <div class="details-artist-badge" aria-hidden="true"><ArtistIcon /></div>
              <div class="details-heading">
                <h2 class="details-title">{selected.node.artist}</h2>
                <div class="details-facts text default-muted">
                  {artistFacts(selected.node.groups)}
                </div>
              </div>
            </div>
            <h3 class="details-subheading">Albums</h3>
            <CoverGrid groups={selected.node.groups} showArtist={false} onSelect={props.onSelect} />
          </section>
        )
      case 'group':
        return (
          <GroupDetails
            group={selected.group}
            issueTypes={issueTypes}
            onSelect={props.onSelect}
            onSearchArtist={props.onSearchArtist}
            onOpenArt={setViewing}
          />
        )
      case 'album':
        return (
          <AlbumDetails
            album={selected.album}
            group={selected.group}
            details={details}
            fields={fields}
            issueTypes={issueTypes}
            onSelect={props.onSelect}
            onSearchArtist={props.onSearchArtist}
            onOpenArt={setViewing}
            onHeaderMenu={() => setMenuOpen(true)}
          />
        )
      case 'track':
        return (
          <TrackDetailsView
            album={selected.album}
            group={selected.group}
            track={selected.track}
            details={details}
            fields={fields}
            onSelect={props.onSelect}
            onOpenArt={setViewing}
          />
        )
    }
  })()

  return (
    <>
      <div class="details-commandbar" role="toolbar" aria-label="Commands">
        <button type="button" class="commandbar-button commandbar-back" onClick={props.onBack}>
          ‹ Library
        </button>

        {album && (
          <>
            <button
              type="button"
              class="commandbar-button"
              title="Match this album to a release and correct its tags"
              onClick={() => props.onEdit(album)}
            >
              Edit metadata…
            </button>
            <GetArtButton album={album} onDone={props.onArtFetched} class="commandbar-button" />
          </>
        )}

        {group && (
          <button
            type="button"
            class="commandbar-button"
            title={`search MusicBrainz for ${group.album}`}
            onClick={() => props.onSearchAlbum(group)}
          >
            Find on MusicBrainz
          </button>
        )}

        {selected.kind === 'artist' && (
          <button
            type="button"
            class="commandbar-button"
            title={`search MusicBrainz for ${selected.node.artist}`}
            onClick={() => props.onSearchArtist(selected.node.artist)}
          >
            Find on MusicBrainz
          </button>
        )}

        {album && (
          <button
            type="button"
            class="commandbar-button is-danger"
            title={group && group.editions.length > 1
              ? 'Delete this edition from disk - the others stay'
              : 'Delete this album from disk'}
            onClick={() => props.onDelete(album)}
          >
            Delete…
          </button>
        )}

        <span class="commandbar-spacer" />

        {(selected.kind === 'album' || selected.kind === 'track') && (
          <div class="fields-control">
            <button
              type="button"
              class="commandbar-button"
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              title="Choose which fields the track viewer shows"
              onClick={() => setMenuOpen((open) => !open)}
            >
              Fields ▾
            </button>
            {menuOpen && (
              <FieldsMenu
                visible={fields}
                onToggle={toggleField}
                onReset={resetFields}
                onClose={() => setMenuOpen(false)}
              />
            )}
          </div>
        )}
      </div>

      <div class="details-body scrollable" ref={bodyRef}>{body}</div>

      {viewing && (
        <ArtViewer
          images={[{
            label: viewing.album,
            sources: coverSources(viewing, 'full'),
            missing: 'No cover on disk, and none on the Cover Art Archive for this release',
          }]}
          onClose={() => setViewing(null)}
        />
      )}
    </>
  )
}

function artistFacts(groups: readonly AlbumGroup[]): string {
  const tracks = groups.reduce((n, g) => n + g.trackCount, 0)
  const size = groups.reduce((n, g) => n + g.totalSize, 0)
  const duration = groups.reduce((n, g) => n + g.duration, 0)
  return [
    `${groups.length} album${groups.length === 1 ? '' : 's'}`,
    `${tracks} tracks`,
    formatDuration(duration),
    formatSize(size),
  ].filter(Boolean).join(' · ')
}

/* ------------------------------------------------------------------ pieces */

/** Label / value pairs, Explorer's details-pane layout. A null value drops the row; '' reads —. */
function PropertyGrid({ rows }: { rows: [string, ComponentChildren][] }) {
  return (
    <dl class="property-grid">
      {rows.filter(([, value]) => value !== null && value !== undefined).map(([label, value]) => (
        <Fragment key={label}>
          <dt>{label}</dt>
          <dd>{value === '' ? <span class="text white-tertiary">—</span> : value}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

/** A shelf of covers, Explorer's "large icons" view. Each opens its album in the tree. */
function CoverGrid(
  { groups, showArtist, onSelect }:
  { groups: readonly AlbumGroup[]; showArtist: boolean; onSelect: (id: string) => void },
) {
  return (
    <div class="cover-grid">
      {groups.map((group) => (
        <button
          key={group.key}
          type="button"
          class="cover-tile"
          title={`${group.album} — ${group.artist}`}
          onClick={() => onSelect(groupNodeId(group))}
        >
          <AlbumArt album={group.artFrom} size="large" class="cover-tile-art" />
          <span class="cover-tile-title">{group.album}</span>
          <span class="cover-tile-sub">
            {showArtist ? group.artist : group.yearRange || group.year || ' '}
          </span>
        </button>
      ))}
    </div>
  )
}

function IssueList(
  { album, issueTypes }: { album: LibraryAlbum; issueTypes: Record<string, MetadataIssueType> },
) {
  const issues = outstandingIssues(album)
  if (!issues.length && !album.ignored_issues.length) return null

  return (
    <div class="details-issues">
      {issues.map((code) => (
        <div class="details-issue" key={code}>
          <span class="library-issue-chip">{issueTypes[code]?.label ?? code}</span>
          <span class="text white-tertiary">{issueTypes[code]?.hint}</span>
        </div>
      ))}
      {album.ignored_issues.length > 0 && (
        <div class="details-issue text default-muted">
          {album.ignored_issues.length} issue(s) ignored on this album — the editor can un-ignore them
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ an album */

function AlbumDetails(
  { album, group, details, fields, issueTypes, onSelect, onSearchArtist, onOpenArt, onHeaderMenu }:
  {
    album: LibraryAlbum
    group: AlbumGroup
    details: TrackDetailsState
    fields: string[]
    issueTypes: Record<string, MetadataIssueType>
    onSelect: (id: string) => void
    onSearchArtist: (artist: string) => void
    onOpenArt: (album: LibraryAlbum) => void
    onHeaderMenu: () => void
  },
) {
  const multiple = group.editions.length > 1
  const facts = [
    album.original_year && album.original_year !== album.year
      ? `${album.original_year} (this press ${album.year})`
      : album.year,
    album.disc_count > 1 ? `${album.disc_count} discs` : '',
    `${album.track_count} track${album.track_count === 1 ? '' : 's'}`,
    formatDuration(album.duration),
    formatSize(album.total_size),
    album.formats.join(' / ').toUpperCase(),
  ].filter(Boolean)

  return (
    <section class="details-section">
      <div class="details-header">
        {/* the album's OWN art where it has some, so each edition shows its own sleeve */}
        <AlbumArt
          album={album.art || album.release_mbid ? album : group.artFrom}
          size="large"
          class="details-cover"
          onOpen={() => onOpenArt(album.art || album.release_mbid ? album : group.artFrom ?? album)}
        />

        <div class="details-heading">
          <h2 class="details-title">{album.album}</h2>
          <button
            type="button"
            class="library-link details-artist"
            title={`search MusicBrainz for ${album.artist}`}
            onClick={() => onSearchArtist(album.artist)}
          >
            {album.artist}
          </button>
          <div class="details-facts text default-muted">{facts.join(' · ')}</div>

          <div class="details-chips">
            {(multiple || album.edition) && (
              <span class={`library-edition${multiple ? ' has-siblings' : ''}`}>
                {album.edition || 'Standard'}
              </span>
            )}
            {isNewImport(album) && <span class="library-new-chip">New</span>}
          </div>

          {/* the other pressings, one click away, so comparing them doesn't mean going back to the tree */}
          {multiple && (
            <div class="details-editions" role="group" aria-label="Editions">
              {group.editions.map((edition) => (
                <button
                  key={edition.path}
                  type="button"
                  class={`details-edition-pill${edition.path === album.path ? ' is-current' : ''}`}
                  onClick={() => onSelect(editionNodeId(edition))}
                >
                  {edition.edition || 'Standard'}
                  <span class="text white-tertiary"> · {edition.track_count} trk</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <IssueList album={album} issueTypes={issueTypes} />

      <h3 class="details-subheading">
        Tracks
        {details.loading && <Loading label="reading the files" />}
        {details.error && <span class="text yellow"> — {details.error}</span>}
      </h3>
      <TrackTable
        album={album}
        details={details}
        fields={fields}
        onSelect={onSelect}
        onHeaderMenu={onHeaderMenu}
      />

      <h3 class="details-subheading">Properties</h3>
      <PropertyGrid
        rows={[
          ['Album', album.album],
          ['Album artist', album.artist],
          ['Year', album.year],
          ['Original year', album.original_year],
          ['Edition', multiple || album.edition ? album.edition || 'Standard' : null],
          ['Discs', album.disc_count > 1 ? String(album.disc_count) : null],
          ['Cover', album.art === 'file'
            ? 'An image file in the folder'
            : album.art === 'embedded' ? 'Embedded in the audio' : 'None on disk'],
          ['Release', album.release_mbid
            ? (
              <a
                class="details-mbid"
                href={`https://musicbrainz.org/release/${album.release_mbid}`}
                target="_blank"
                rel="noreferrer"
              >
                {album.release_mbid}
              </a>
            )
            : <span class="text yellow">not tagged with a MusicBrainz release</span>],
          ['Folder', <span class="details-path">{album.path}</span>],
          ['First seen', album.first_seen
            ? `${new Date(album.first_seen).toLocaleDateString()}${album.imported ? ', filed by jimbrainz' : ''}`
            : null],
        ]}
      />
    </section>
  )
}

/** Explorer's details view: one row per track, one column per field you chose. */
function TrackTable(
  { album, details, fields, onSelect, onHeaderMenu }:
  {
    album: LibraryAlbum
    details: TrackDetailsState
    fields: string[]
    onSelect: (id: string) => void
    onHeaderMenu: () => void
  },
) {
  const columns = fields.map(fieldById).filter((f): f is TrackField => Boolean(f))
  //? the number and the disc read before the title, as they would on a sleeve
  const lead = columns.filter((c) => c.id === 'number' || c.id === 'disc')
  const rest = columns.filter((c) => c.id !== 'number' && c.id !== 'disc')

  const template = [...lead.map((c) => c.width), 'minmax(10em, 2fr)', ...rest.map((c) => c.width)]
  //? The table is as wide as the pane, and no narrower than its columns' minimums - past that it
  //? scrolls sideways inside its own box rather than squeezing every column into an ellipsis.
  const minimum = template.reduce((total, width) => total + minimumEm(width), 0) + template.length

  const split = album.disc_count > 1
  let lastDisc: number | null = null

  return (
    <div class="track-table-scroll">
      <div
        class="track-table"
        role="grid"
        aria-label="Tracks"
        style={`--track-columns:${template.join(' ')};min-width:${minimum}em`}
      >
        <div
          class="track-table-head"
          role="row"
          title="Right-click to choose fields"
          onContextMenu={(event) => {
            event.preventDefault()
            onHeaderMenu()
          }}
        >
          {lead.map((c) => <HeaderCell key={c.id} field={c} />)}
          <span role="columnheader" class="track-cell">Title</span>
          {rest.map((c) => <HeaderCell key={c.id} field={c} />)}
        </div>

        {album.tracks.map((track) => {
          const row: TrackRow = { track, details: details.files?.get(track.filename) }
          const disc = track.disc ?? 1
          const divider = split && disc !== lastDisc
          lastDisc = disc

          return (
            <Fragment key={track.filename}>
              {divider && <div class="track-table-disc" role="row">Disc {disc}</div>}
              <div
                role="row"
                class="track-table-row"
                tabIndex={0}
                onClick={() => onSelect(trackNodeId(album, track))}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') onSelect(trackNodeId(album, track))
                }}
              >
                {lead.map((c) => <Cell key={c.id} field={c} row={row} pending={details.loading} />)}
                <span
                  role="gridcell"
                  class={`track-cell track-title${track.has_title_tag ? '' : ' is-untitled'}`}
                  title={track.has_title_tag ? track.title : 'No title tag - this is the file name'}
                >
                  {row.details?.tags['title'] ?? track.title}
                </span>
                {rest.map((c) => <Cell key={c.id} field={c} row={row} pending={details.loading} />)}
              </div>
            </Fragment>
          )
        })}
      </div>
    </div>
  )
}

function minimumEm(width: string): number {
  const match = /(\d+(?:\.\d+)?)em/.exec(width)
  return match ? Number(match[1]) : 6
}

function HeaderCell({ field }: { field: TrackField }) {
  return (
    <span
      role="columnheader"
      class={`track-cell${field.align === 'right' ? ' is-right' : ''}`}
      title={field.label === '#' ? 'Track number' : field.label}
    >
      {field.label}
    </span>
  )
}

function Cell({ field, row, pending }: { field: TrackField; row: TrackRow; pending: boolean }) {
  const value = field.value(row)
  //? a field only the file itself carries shows that it is still coming, rather than a blank
  //? that would read as "this track has no genre"
  const waiting = !value && !field.fromScan && !row.details && pending

  return (
    <span
      role="gridcell"
      class={`track-cell${field.align === 'right' ? ' is-right' : ''}${field.mono ? ' is-mono' : ''}`}
      title={value || undefined}
    >
      {waiting ? <span class="text white-tertiary">·</span> : value}
    </span>
  )
}

/* ------------------------------------------------------------------ a multi-edition album */

function GroupDetails(
  { group, issueTypes, onSelect, onSearchArtist, onOpenArt }:
  {
    group: AlbumGroup
    issueTypes: Record<string, MetadataIssueType>
    onSelect: (id: string) => void
    onSearchArtist: (artist: string) => void
    onOpenArt: (album: LibraryAlbum) => void
  },
) {
  return (
    <section class="details-section">
      <div class="details-header">
        <AlbumArt
          album={group.artFrom}
          size="large"
          class="details-cover"
          onOpen={group.artFrom ? () => group.artFrom && onOpenArt(group.artFrom) : undefined}
        />
        <div class="details-heading">
          <h2 class="details-title">{group.album}</h2>
          <button
            type="button"
            class="library-link details-artist"
            onClick={() => onSearchArtist(group.artist)}
          >
            {group.artist}
          </button>
          <div class="details-facts text default-muted">
            {[group.yearRange || group.year, `${group.editions.length} editions`,
              formatSize(group.totalSize)].filter(Boolean).join(' · ')}
          </div>
        </div>
      </div>

      <h3 class="details-subheading">Editions in your library</h3>
      <div class="track-table-scroll">
        <div
          class="track-table editions-table"
          role="grid"
          style="--track-columns:minmax(10em,2fr) 4em 4em 5em 5em 6em minmax(8em,1fr);min-width:44em"
        >
          <div class="track-table-head" role="row">
            {['Edition', 'Year', 'Tracks', 'Length', 'Size', 'Format', 'Needs'].map((label) => (
              <span key={label} role="columnheader" class="track-cell">{label}</span>
            ))}
          </div>
          {group.editions.map((album) => {
            const issues = outstandingIssues(album)
            return (
              <div
                key={album.path}
                role="row"
                class="track-table-row"
                tabIndex={0}
                onClick={() => onSelect(editionNodeId(album))}
                onKeyDown={(event) => { if (event.key === 'Enter') onSelect(editionNodeId(album)) }}
              >
                <span class="track-cell track-title">{album.edition || 'Standard'}</span>
                <span class="track-cell is-mono">{album.year}</span>
                <span class="track-cell is-mono is-right">{album.track_count}</span>
                <span class="track-cell is-mono is-right">{formatDuration(album.duration)}</span>
                <span class="track-cell is-mono is-right">{formatSize(album.total_size)}</span>
                <span class="track-cell">{album.formats.join('/').toUpperCase()}</span>
                <span class="track-cell" title={issues.map((c) => issueTypes[c]?.label ?? c).join(', ')}>
                  {issues.length ? <span class="text yellow">{issueTypes[issues[0] ?? '']?.label ?? issues[0]}{issues.length > 1 ? ` +${issues.length - 1}` : ''}</span> : ''}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ one track */

function TrackDetailsView(
  { album, group, track, details, fields, onSelect, onOpenArt }:
  {
    album: LibraryAlbum
    group: AlbumGroup
    track: LibraryTrack
    details: TrackDetailsState
    fields: string[]
    onSelect: (id: string) => void
    onOpenArt: (album: LibraryAlbum) => void
  },
) {
  const file = details.files?.get(track.filename)
  const row: TrackRow = { track, details: file }
  const at = album.tracks.findIndex((t) => t.filename === track.filename)
  const previous = album.tracks[at - 1]
  const next = album.tracks[at + 1]

  const where = [
    track.position !== null ? `Track ${track.position}` : 'Unnumbered',
    album.disc_count > 1 && track.disc ? `disc ${track.disc} of ${album.disc_count}` : '',
  ].filter(Boolean).join(', ')

  const shown = TRACK_FIELDS.filter((field) => fields.includes(field.id))

  return (
    <section class="details-section">
      <div class="details-header">
        <AlbumArt album={album} size="large" class="details-cover is-small" onOpen={() => onOpenArt(album)} />
        <div class="details-heading">
          <h2 class="details-title">{file?.tags['title'] ?? track.title}</h2>
          <button
            type="button"
            class="library-link details-artist"
            //? through the tree's own id builder - a group key is not the "artist album" string it
            //? looks like (it carries a NUL), so an id rebuilt by hand resolves to nothing
            onClick={() => onSelect(nodeIdForAlbum(album, group))}
          >
            {album.album}
          </button>
          <div class="details-facts text default-muted">
            {[where, trackTime(track.length), track.format.toUpperCase()].filter(Boolean).join(' · ')}
          </div>
          <div class="details-stepper">
            <button
              type="button"
              class="win-button"
              disabled={!previous}
              onClick={() => previous && onSelect(trackNodeId(album, previous))}
            >
              ◁ Previous
            </button>
            <button
              type="button"
              class="win-button"
              disabled={!next}
              onClick={() => next && onSelect(trackNodeId(album, next))}
            >
              Next ▷
            </button>
          </div>
        </div>
      </div>

      {details.error && <p class="text yellow">{details.error}</p>}

      {FIELD_GROUPS.map((groupName) => {
        const inGroup = shown.filter((field) => field.group === groupName)
        if (!inGroup.length) return null
        return (
          <Fragment key={groupName}>
            <h3 class="details-subheading">{groupName}</h3>
            <PropertyGrid
              rows={inGroup.map((field): [string, ComponentChildren] => {
                const value = field.value(row)
                if (value) return [field.label, <span class={field.mono ? 'is-mono' : undefined}>{value}</span>]
                return [field.label, !file && !field.fromScan && details.loading ? '…' : '']
              })}
            />
          </Fragment>
        )
      })}

      {/*
        Every tag the file carries, under its container's own name - including the ones the
        field menu has never heard of. The menu decides what gets a proper label; this is where
        you go to see what is actually in the file.
      */}
      <details class="details-raw">
        <summary>All tags in this file{file ? ` (${file.raw.length})` : ''}</summary>
        {!file && details.loading && <Loading label="reading the file" />}
        {file && (
          <dl class="property-grid is-raw">
            {file.raw.map(([key, value], i) => (
              <Fragment key={`${key} ${i}`}>
                <dt title={key}>{key}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
          </dl>
        )}
        {file && <p class="text white-tertiary details-raw-note">{track.filename}</p>}
      </details>
    </section>
  )
}

/* ------------------------------------------------------------------ nothing selected */

function Overview(
  { summary, groups, onSelect }:
  { summary: LibrarySummary; groups: readonly AlbumGroup[]; onSelect: (id: string) => void },
) {
  //? the question you usually have when you open the library without a particular album in mind
  //? is "what just arrived" - judged by the same clock as the "Date added" arrangement
  const recent = useMemo(
    () => [...groups].sort((a, b) => groupAddedAt(b) - groupAddedAt(a)).slice(0, 12),
    [groups],
  )

  return (
    <section class="details-section">
      <h2 class="details-title">Library</h2>
      <PropertyGrid
        rows={[
          ['Albums', String(summary.albums)],
          ['Artists', String(summary.artists)],
          ['Tracks', String(summary.tracks)],
          ['Length', formatDuration(summary.duration)],
          ['Size', formatSize(summary.size)],
          ['Folder', summary.libraryPath ? <span class="details-path">{summary.libraryPath}</span> : ''],
          ['Last scanned', summary.scannedAt
            ? `${formatAge(summary.scannedAt)}${summary.stale ? ' (saved scan, checking now)' : ''}`
            : ''],
        ]}
      />
      <p class="text white-tertiary details-hint">
        Pick an artist, an album or a track on the left to see it here. The arrow keys move
        through the tree, and right and left open and close it.
      </p>

      {recent.length > 0 && (
        <>
          <h3 class="details-subheading">Recently added</h3>
          <CoverGrid groups={recent} showArtist onSelect={onSelect} />
        </>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------ the field menu */

function FieldsMenu(
  { visible, onToggle, onReset, onClose }:
  {
    visible: string[]
    onToggle: (id: string, on: boolean) => void
    onReset: () => void
    onClose: () => void
  },
) {
  const menuRef = useRef<HTMLDivElement>(null)

  //? closes on a click anywhere else, or Escape - caught in the capture phase and stopped, so
  //? the Escape doesn't also reach anything listening further out
  useEffect(() => {
    const onPointer = (event: MouseEvent) => {
      const control = menuRef.current?.parentElement
      if (control && !control.contains(event.target as Node)) onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      onClose()
    }
    document.addEventListener('mousedown', onPointer)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [onClose])

  return (
    <div class="fields-menu" ref={menuRef} role="menu" aria-label="Fields">
      <div class="fields-menu-groups">
        {FIELD_GROUPS.map((group) => (
          <fieldset key={group} class="fields-menu-group">
            <legend>{group}</legend>
            {TRACK_FIELDS.filter((field) => field.group === group).map((field) => (
              <label key={field.id} class="fields-menu-item" role="menuitemcheckbox" aria-checked={visible.includes(field.id)}>
                <input
                  type="checkbox"
                  checked={visible.includes(field.id)}
                  onChange={(event) => onToggle(field.id, (event.target as HTMLInputElement).checked)}
                />
                {field.label === '#' ? 'Track number' : field.label}
              </label>
            ))}
          </fieldset>
        ))}
      </div>
      <div class="fields-menu-footer">
        <span class="text white-tertiary">Title is always shown.</span>
        <button type="button" class="win-button" onClick={onReset}>Reset</button>
      </div>
    </div>
  )
}
