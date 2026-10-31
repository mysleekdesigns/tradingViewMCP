import { useEffect } from 'react'

/**
 * Esc calls `onClose` wherever focus is, except inside a dialog (it closes that first). Listens
 * only while `enabled`.
 */
export function useEscapeToClose(onClose: () => void, enabled = true): void {
  useEffect(() => {
    if (!enabled) return
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing) return
      const t = e.target
      if (t instanceof Element && t.closest('[role="dialog"], [role="alertdialog"]')) return
      e.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, enabled])
}
