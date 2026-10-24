import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import type { ToolMode } from '../chart/drawings/types'
import { DRAWING_TOOLS, clearAllPrompt, nextTool, toolTitle } from './drawingTools'
import './Drawings.css'

export interface DrawingToolbarProps {
  tool: ToolMode
  onToolChange(tool: ToolMode): void
  symbol: string
  drawingCount: number
  onClearAll(): void
}

/**
 * Left drawing rail (TradingView style): cursor and drawing tools, then "Remove all drawings"
 * with an inline confirm. Shortcuts are shown in the tooltips; the parent handles the keys
 * (see `shortcutCommand` in drawingTools.ts).
 */
export function DrawingToolbar({
  tool,
  onToolChange,
  symbol,
  drawingCount,
  onClearAll,
}: DrawingToolbarProps) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const clearRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const promptId = useId()
  // Closes by itself when the last drawing goes away (e.g. undone elsewhere).
  const open = confirmOpen && drawingCount > 0

  useEffect(() => {
    if (!open) return
    cancelRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (!popRef.current?.contains(t) && !clearRef.current?.contains(t)) setConfirmOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [open])

  const closeConfirm = (refocus: boolean) => {
    setConfirmOpen(false)
    if (refocus) clearRef.current?.focus()
  }

  const onPopKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      closeConfirm(true)
    }
  }

  const empty = drawingCount === 0

  return (
    <nav className="draw-rail" aria-label="Drawing tools">
      <div className="draw-rail__group" role="group" aria-label="Tools">
        {DRAWING_TOOLS.map((def) => (
          <button
            key={def.tool}
            type="button"
            className="draw-rail__btn"
            aria-pressed={tool === def.tool}
            aria-label={def.label}
            aria-keyshortcuts={def.shortcut}
            title={toolTitle(def)}
            onClick={() => onToolChange(nextTool(tool, def.tool))}
          >
            <ToolIcon tool={def.tool} />
          </button>
        ))}
      </div>

      <div className="draw-rail__sep" role="separator" />

      <div className="draw-rail__clear">
        <button
          ref={clearRef}
          type="button"
          className="draw-rail__btn draw-rail__btn--danger"
          aria-label={
            empty ? 'Remove all drawings (none on this symbol)' : `Remove all drawings on ${symbol}`
          }
          aria-haspopup="dialog"
          aria-expanded={open}
          title={empty ? 'No drawings to remove' : 'Remove all drawings'}
          disabled={empty}
          onClick={() => (open ? closeConfirm(false) : setConfirmOpen(true))}
        >
          <TrashIcon />
          {!empty && (
            <span className="draw-rail__count">{drawingCount > 99 ? '99+' : drawingCount}</span>
          )}
        </button>

        {open && (
          <div
            ref={popRef}
            className="draw-confirm"
            role="alertdialog"
            aria-modal="false"
            aria-labelledby={promptId}
            onKeyDown={onPopKeyDown}
          >
            <p id={promptId} className="draw-confirm__text">
              {clearAllPrompt(drawingCount, symbol)}
            </p>
            <div className="draw-confirm__actions">
              <button
                ref={cancelRef}
                type="button"
                className="btn btn--ghost draw-confirm__btn"
                onClick={() => closeConfirm(true)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn draw-confirm__btn draw-confirm__btn--danger"
                onClick={() => {
                  closeConfirm(true)
                  onClearAll()
                }}
              >
                Remove
              </button>
            </div>
          </div>
        )}
      </div>
    </nav>
  )
}

const ICON = {
  width: 22,
  height: 22,
  viewBox: '0 0 22 22',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.3,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

/** Tool glyphs, also used by the style bar. */
export function ToolIcon({ tool }: { tool: ToolMode }) {
  switch (tool) {
    case 'cursor':
      return (
        <svg {...ICON}>
          <path d="M11 3v5M11 14v5M3 11h5M14 11h5" />
          <circle cx="11" cy="11" r="1" fill="currentColor" stroke="none" />
        </svg>
      )
    case 'hline':
      return (
        <svg {...ICON}>
          <path d="M2.5 11h17" />
          <circle cx="11" cy="11" r="2" fill="var(--draw-icon-bg, var(--chrome))" />
        </svg>
      )
    case 'hray':
      return (
        <svg {...ICON}>
          <path d="M7.5 11h12" />
          <path d="M17 8.5l2.5 2.5-2.5 2.5" />
          <circle cx="5" cy="11" r="2" fill="var(--draw-icon-bg, var(--chrome))" />
        </svg>
      )
    case 'trendline':
      return (
        <svg {...ICON}>
          <path d="M6.4 15.6l9.2-9.2" />
          <circle cx="5" cy="17" r="2" fill="var(--draw-icon-bg, var(--chrome))" />
          <circle cx="17" cy="5" r="2" fill="var(--draw-icon-bg, var(--chrome))" />
        </svg>
      )
    case 'fib':
      return (
        <svg {...ICON}>
          <path d="M3 4.5h16M3 9h16M3 13h16M3 17.5h16" />
          <path d="M5 17.5L17 4.5" strokeDasharray="1.6 2" />
        </svg>
      )
    case 'rectangle':
      return (
        <svg {...ICON}>
          <rect x="4" y="6" width="14" height="10" rx="0.5" />
          <circle cx="4" cy="6" r="1.6" fill="var(--draw-icon-bg, var(--chrome))" />
          <circle cx="18" cy="16" r="1.6" fill="var(--draw-icon-bg, var(--chrome))" />
        </svg>
      )
  }
}

export function TrashIcon({ size = 20 }: { size?: number }) {
  return (
    <svg {...ICON} width={size} height={size}>
      <path d="M4.5 6.5h13M9 6.5V4.5h4v2M6.5 6.5l.8 11h7.4l.8-11M9.5 9.5v5.5M12.5 9.5v5.5" />
    </svg>
  )
}
