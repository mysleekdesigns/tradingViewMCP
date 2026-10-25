import { Fragment, useEffect, useId, useMemo, useRef } from 'react'
import { shortcutGroups, type ShortcutItem } from '../state/shortcuts'
import './Shortcuts.css'
import { useModalKeys } from './useModalKeys'

export interface ShortcutsDialogProps {
  isMac: boolean
  onClose: () => void
}

function KeySequence({ seq, isMac }: { seq: string[]; isMac: boolean }) {
  return (
    <span className="kbd-seq">
      {seq.map((k, i) => (
        <Fragment key={`${i}-${k}`}>
          {/* macOS writes chords without a separator (⌥1); other platforms use "+" (Alt+1). */}
          {i > 0 && !isMac && (
            <span className="kbd-seq__plus" aria-hidden="true">
              +
            </span>
          )}
          <kbd className="kbd">{k}</kbd>
        </Fragment>
      ))}
    </span>
  )
}

function ShortcutRow({ item, isMac }: { item: ShortcutItem; isMac: boolean }) {
  return (
    <div className="shortcuts-row">
      <dt className="shortcuts-row__label">{item.label}</dt>
      <dd className="shortcuts-row__keys">
        {item.keys.map((seq, i) => (
          <Fragment key={seq.join(' ')}>
            {i > 0 && <span className="shortcuts-row__or">or</span>}
            <KeySequence seq={seq} isMac={isMac} />
          </Fragment>
        ))}
      </dd>
    </div>
  )
}

/**
 * Read-only list of every keyboard shortcut. Modal: keys go to `useModalKeys` (Esc closes, Tab
 * stays inside, nothing reaches the chart behind it); the close button or a click on the
 * backdrop also closes it. Focus moves in on mount and returns on unmount.
 */
export function ShortcutsDialog({ isMac, onClose }: ShortcutsDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const ids = useId()
  const titleId = `${ids}-title`
  const hintId = `${ids}-hint`
  const groups = useMemo(() => shortcutGroups(isMac), [isMac])

  useModalKeys(dialogRef, onClose)

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (closeRef.current) closeRef.current.focus()
    else dialogRef.current?.focus()
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  return (
    <div
      className="modal-backdrop shortcuts-dialog__backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="shortcuts-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={hintId}
        tabIndex={-1}
      >
        <div className="shortcuts-dialog__head">
          <span className="shortcuts-dialog__head-icon" aria-hidden="true">
            <svg
              width="18"
              height="18"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="2" y="5" width="16" height="10.5" rx="1.8" />
              <path d="M5 8.2h.01M8 8.2h.01M11 8.2h.01M14 8.2h1M5 10.6h1M8.5 10.6h.01M11.5 10.6h.01M14.5 10.6h.01M6.5 13h7" />
            </svg>
          </span>
          <h2 id={titleId} className="shortcuts-dialog__title">
            Keyboard shortcuts
          </h2>
          <button
            ref={closeRef}
            type="button"
            className="icon-btn"
            aria-label="Close keyboard shortcuts"
            title="Close (Esc)"
            onClick={onClose}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </button>
        </div>

        <div className="shortcuts-dialog__body" tabIndex={0} aria-label="Shortcut list">
          {groups.map((group) => (
            <section
              key={group.title}
              className="shortcuts-group"
              aria-labelledby={`${ids}-${group.title}`}
            >
              <h3 id={`${ids}-${group.title}`} className="shortcuts-group__title">
                {group.title}
              </h3>
              <dl className="shortcuts-group__list">
                {group.items.map((item) => (
                  <ShortcutRow key={item.label} item={item} isMac={isMac} />
                ))}
              </dl>
            </section>
          ))}
        </div>

        <div className="shortcuts-dialog__foot">
          <span id={hintId} className="shortcuts-dialog__hint">
            Shortcuts pause while you type in a field or a dialog is open.
          </span>
          <span className="shortcuts-dialog__hint shortcuts-dialog__hint--end">
            Press <kbd className="kbd kbd--small">?</kbd> to open this list
          </span>
        </div>
      </div>
    </div>
  )
}
