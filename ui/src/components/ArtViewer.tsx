import { useEffect, useState } from 'preact/hooks'

import { Loading } from './Loading'

export interface ViewerImage {
  label: string
  /** Tried in order; the next is used when one fails to load. */
  sources: string[]
  /** Said in place of the image when none of the sources load. */
  missing?: string
}

interface Props {
  /** One image, or two to compare side by side. */
  images: ViewerImage[]
  onClose: () => void
  /**
   * An optional decision to make from here, e.g. "Use this cover" in the metadata editor.
   *
   * `requires` is the index of the image the action acts on. The button stays disabled until
   * that image has actually LOADED - offering "replace with this cover" beside a pane that says
   * there is no cover would be offering a request that can only 404.
   */
  action?: {
    label: string
    title?: string
    busy?: boolean
    requires?: number
    onClick: () => void
  } | undefined
}

interface Size {
  width: number
  height: number
}

/**
 * Cover art at full size, one image or two side by side.
 *
 * Exists for the metadata editor's "which cover do I keep" decision, where the thumbnails could
 * not answer the actual question. Two covers of the same record usually look identical at 72px;
 * what differs is resolution and scan quality, which only shows at full size. So each image
 * reports its real pixel dimensions, the larger is marked, and a click shows it at actual size.
 *
 * Escape is caught in the CAPTURE phase and stopped there. The editor underneath listens for
 * Escape on the document to close itself, and without this one keypress would close both.
 */
export function ArtViewer({ images, onClose, action }: Props) {
  const [sizes, setSizes] = useState<(Size | null)[]>(() => images.map(() => null))

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      event.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [onClose])

  //? only once every image has reported in, and only a strict winner - two equal covers get no
  //? badge, rather than one of them being called larger by the order they loaded in
  const pixels = sizes.map((size) => (size ? size.width * size.height : 0))
  const best = Math.max(...pixels)
  const largest =
    images.length > 1 && sizes.every(Boolean) && pixels.filter((p) => p === best).length === 1
      ? pixels.indexOf(best)
      : -1

  return (
    <div id="art-viewer" role="dialog" aria-modal="true" aria-label="Cover art" onClick={onClose}>
      <div class="art-viewer-frame" onClick={(event) => event.stopPropagation()}>
        <div class="window-titlebar">
          <span class="window-title">Cover art</span>
          <button type="button" class="window-close" title="Close (Esc)" onClick={onClose}>✕</button>
        </div>

        <div class={`art-viewer-panes count-${images.length}`}>
          {images.map((image, index) => (
            <ArtPane
              key={`${index}:${image.sources.join('|')}`}
              image={image}
              largest={index === largest}
              onSize={(size) =>
                setSizes((current) => current.map((s, i) => (i === index ? size : s)))}
            />
          ))}
        </div>

        <div class="art-viewer-footer">
          <span class="text white-tertiary art-viewer-hint">
            Click an image to see it at actual size.
          </span>
          {action && (
            <button
              type="button"
              class="win-button is-default"
              disabled={action.busy || (action.requires !== undefined && !sizes[action.requires])}
              title={action.requires !== undefined && !sizes[action.requires]
                ? 'There is no cover to save until it has loaded'
                : action.title}
              onClick={action.onClick}
            >
              {action.busy ? <Loading label="Saving" /> : action.label}
            </button>
          )}
          <button type="button" class="win-button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}

function ArtPane(
  { image, largest, onSize }:
  { image: ViewerImage; largest: boolean; onSize: (size: Size | null) => void },
) {
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<'loading' | 'loaded' | 'failed'>('loading')
  const [size, setSize] = useState<Size | null>(null)
  const [actual, setActual] = useState(false)

  const src = image.sources[attempt]
  const failed = state === 'failed' || !src

  //? a pane with nothing to show still has to report, or the comparison waits on it forever
  useEffect(() => {
    if (failed) onSize(null)
  }, [failed])

  return (
    <figure class="art-viewer-pane">
      <figcaption class="art-viewer-caption">
        <span class="art-viewer-label">{image.label}</span>
        {size && <span class="art-viewer-dims">{size.width} × {size.height} px</span>}
        {largest && <span class="art-viewer-largest" title="More pixels than the other one">Larger</span>}
      </figcaption>

      <div class={`art-viewer-stage${actual ? ' is-actual' : ''}`}>
        {src && !failed && (
          <img
            src={src}
            alt={image.label}
            title={actual ? 'Fit to the window' : 'Show at actual size'}
            style={state === 'loaded' ? undefined : 'visibility:hidden'}
            onLoad={(event) => {
              const img = event.currentTarget as HTMLImageElement
              const measured = { width: img.naturalWidth, height: img.naturalHeight }
              setSize(measured)
              onSize(measured)
              setState('loaded')
            }}
            onError={() => {
              if (attempt + 1 < image.sources.length) setAttempt((n) => n + 1)
              else setState('failed')
            }}
            onClick={() => setActual((on) => !on)}
          />
        )}

        {state === 'loading' && src && (
          <div class="art-viewer-status"><Loading label="Loading full size" /></div>
        )}

        {failed && (
          <div class="art-viewer-status text white-tertiary">{image.missing ?? 'No image'}</div>
        )}
      </div>
    </figure>
  )
}
