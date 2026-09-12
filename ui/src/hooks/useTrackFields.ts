import { useCallback, useState } from 'preact/hooks'

import { reconcileVisible, TRACK_FIELDS } from '../lib/trackFields'
import { readLibraryFields, writeLibraryFields } from '../state/persisted'

const everyId = () => TRACK_FIELDS.map((field) => field.id)

/**
 * Which fields the track viewer shows, remembered in this browser.
 *
 * One hook instance, in the details pane, and the menu and the table both read from it -
 * two instances would each hold their own copy and the menu would stop moving the columns.
 * Kept in registry order whatever order they were ticked in, so the columns never shuffle.
 */
export function useTrackFields(): [
  visible: string[],
  toggle: (id: string, on: boolean) => void,
  reset: () => void,
] {
  const [visible, setVisible] = useState(() => reconcileVisible(readLibraryFields()))

  const save = useCallback((next: string[]) => {
    //? `seen` is written as every field that exists NOW, so a field added later can tell
    //? "you turned me off" from "you were never asked" - see reconcileVisible
    writeLibraryFields({ visible: next, seen: everyId() })
    setVisible(next)
  }, [])

  const toggle = useCallback((id: string, on: boolean) => {
    setVisible((current) => {
      const next = TRACK_FIELDS
        .filter((field) => (field.id === id ? on : current.includes(field.id)))
        .map((field) => field.id)
      writeLibraryFields({ visible: next, seen: everyId() })
      return next
    })
  }, [])

  const reset = useCallback(
    () => save(TRACK_FIELDS.filter((field) => field.initial).map((field) => field.id)),
    [save],
  )

  return [visible, toggle, reset]
}
