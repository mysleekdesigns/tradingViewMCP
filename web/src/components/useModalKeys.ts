import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE =
  'button:not([disabled]):not([tabindex="-1"]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'

/**
 * Keyboard ownership for a modal dialog, while it is mounted.
 *
 * A bubble-phase `document` keydown listener runs after the dialog's own React handlers (React
 * listens on #root, inside document) and before `window` listeners. It stops every keydown from
 * reaching window listeners behind the modal (chart Delete/Escape, app shortcuts). Unless the
 * dialog already handled the key (`defaultPrevented`), it also handles Escape (calls `onClose`)
 * and Tab (keeps focus inside `ref`). Because it listens on the document, it still works after
 * the focused node unmounts and focus falls back to <body>: Escape closes, and Tab moves focus
 * back into the dialog.
 */
export function useModalKeys(ref: RefObject<HTMLElement | null>, onClose: () => void): void {
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const root = ref.current
      if (!root) return
      e.stopPropagation()
      if (e.defaultPrevented || e.isComposing) return
      if (e.key === 'Escape') {
        e.preventDefault()
        onCloseRef.current()
        return
      }
      if (e.key !== 'Tab') return
      const focusables = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
      const active = document.activeElement
      if (focusables.length === 0) {
        e.preventDefault()
        root.focus()
        return
      }
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (!root.contains(active)) {
        e.preventDefault()
        ;(e.shiftKey ? last : first).focus()
      } else if (e.shiftKey && active === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [ref])
}
