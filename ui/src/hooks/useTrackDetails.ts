import { useEffect, useRef, useState } from 'preact/hooks'

import * as api from '../api/library'
import type { TrackDetails } from '../api/types'

//? Arrow-keying down the tree selects every album it passes; only the one you stop on is worth
//? opening a dozen files for. Short enough that stopping on one feels immediate.
const DEBOUNCE_MS = 150

export interface TrackDetailsState {
  /** filename -> what the file says about itself, or null until it has been read. */
  files: Map<string, TrackDetails> | null
  loading: boolean
  error: string | null
}

const IDLE: TrackDetailsState = { files: null, loading: false, error: null }

/**
 * Every file in one album, read live, for whichever album the viewer is showing.
 *
 * Cached per album path until `generation` changes. The caller passes the library's album list
 * for that, so every reload throws the cache away - and it has to: an in-place retag rewrites
 * the files without moving the folder's path or its mtime, so a cache keyed on either would
 * keep showing the tags you had just replaced.
 */
export function useTrackDetails(albumPath: string | null, generation: unknown): TrackDetailsState {
  const cache = useRef(new Map<string, Map<string, TrackDetails>>())
  const [state, setState] = useState<TrackDetailsState>(IDLE)

  //? declared first, so on a commit that changes both it has already run when the fetch looks
  useEffect(() => {
    cache.current = new Map()
  }, [generation])

  useEffect(() => {
    if (!albumPath) {
      setState(IDLE)
      return
    }

    const cached = cache.current.get(albumPath)
    if (cached) {
      setState({ files: cached, loading: false, error: null })
      return
    }

    let cancelled = false
    //? what was on screen stays there until the new album's files replace it - blanking every
    //? column for 150ms on each arrow press would flicker the whole table
    setState((current) => ({ ...current, loading: true, error: null }))

    const timer = setTimeout(async () => {
      try {
        const response = await api.trackDetails(albumPath)
        const files = new Map(response.files.map((file) => [file.filename, file]))
        cache.current.set(albumPath, files)
        if (!cancelled) setState({ files, loading: false, error: null })
      } catch (caught) {
        if (!cancelled) {
          setState({
            files: null,
            loading: false,
            error: caught instanceof Error ? caught.message : "couldn't read the files",
          })
        }
      }
    }, DEBOUNCE_MS)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [albumPath, generation])

  return state
}
