import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { Drawing, DrawingPatch, DrawingStyle } from '../chart/drawings/types'
import { ToolIcon, TrashIcon } from './DrawingToolbar'
import {
  DRAWING_KIND_LABELS,
  DRAWING_PALETTE,
  LINE_STYLES,
  LINE_WIDTHS,
  colorName,
  gridMove,
} from './drawingTools'
import './Drawings.css'

export interface DrawingStyleBarProps {
  drawing: Drawing
  onChange(patch: DrawingPatch): void
  onDelete(): void
}

type PopoverId = 'color' | 'width' | 'style'

/**
 * Floating toolbar for the selected drawing: colour, line width, line style, delete. Renders
 * absolutely at the top centre of its positioned parent (the chart area).
 */
export function DrawingStyleBar({ drawing, onChange, onDelete }: DrawingStyleBarProps) {
  const [open, setOpen] = useState<PopoverId | null>(null)
  const { style } = drawing
  const kindLabel = DRAWING_KIND_LABELS[drawing.kind]

  const setStyle = (patch: Partial<DrawingStyle>) => onChange({ style: { ...style, ...patch } })
  const toggle = (id: PopoverId) => setOpen((o) => (o === id ? null : id))

  const paletteIndex = DRAWING_PALETTE.findIndex((p) => p.color === style.color.toLowerCase())
  const styleDef = LINE_STYLES.find((s) => s.style === style.lineStyle) ?? LINE_STYLES[0]

  return (
    <div
      className="draw-stylebar"
      role="toolbar"
      aria-label={`${kindLabel} style`}
      onContextMenu={(e) => e.preventDefault()}
    >
      <span className="draw-stylebar__kind" title={kindLabel}>
        <ToolIcon tool={drawing.kind} />
        <span className="draw-stylebar__kind-name">{kindLabel}</span>
      </span>
      <span className="draw-stylebar__sep" aria-hidden="true" />

      <StylePopover
        open={open === 'color'}
        onToggle={() => toggle('color')}
        onClose={() => setOpen(null)}
        label={`Line colour: ${colorName(style.color)}`}
        title="Line colour"
        button={<span className="draw-stylebar__chip" style={{ background: style.color }} />}
        options={DRAWING_PALETTE.map((p) => ({
          key: p.color,
          label: p.name,
          content: <span className="draw-swatch" style={{ background: p.color }} />,
        }))}
        selectedIndex={paletteIndex}
        columns={5}
        listClassName="draw-pop__list--grid"
        onPick={(i) => setStyle({ color: DRAWING_PALETTE[i].color })}
        footer={
          <label className="draw-pop__custom">
            <input
              type="color"
              className="draw-pop__color-input"
              value={style.color}
              onChange={(e) => setStyle({ color: e.target.value.toLowerCase() })}
            />
            <span>Custom colour</span>
          </label>
        }
      />

      <StylePopover
        open={open === 'width'}
        onToggle={() => toggle('width')}
        onClose={() => setOpen(null)}
        label={`Line width: ${style.lineWidth}px`}
        title="Line width"
        button={
          <>
            <LineSample width={style.lineWidth} dash="" />
            <span className="draw-stylebar__value">{style.lineWidth}px</span>
          </>
        }
        options={LINE_WIDTHS.map((w) => ({
          key: String(w),
          label: `${w}px`,
          content: (
            <>
              <LineSample width={w} dash="" long />
              <span>{w}px</span>
            </>
          ),
        }))}
        selectedIndex={LINE_WIDTHS.indexOf(style.lineWidth)}
        columns={1}
        onPick={(i) => setStyle({ lineWidth: LINE_WIDTHS[i] })}
      />

      <StylePopover
        open={open === 'style'}
        onToggle={() => toggle('style')}
        onClose={() => setOpen(null)}
        label={`Line style: ${styleDef.label}`}
        title="Line style"
        button={<LineSample width={2} dash={styleDef.dash} />}
        options={LINE_STYLES.map((s) => ({
          key: s.style,
          label: s.label,
          content: (
            <>
              <LineSample width={2} dash={s.dash} long />
              <span>{s.label}</span>
            </>
          ),
        }))}
        selectedIndex={LINE_STYLES.findIndex((s) => s.style === style.lineStyle)}
        columns={1}
        onPick={(i) => setStyle({ lineStyle: LINE_STYLES[i].style })}
      />

      <span className="draw-stylebar__sep" aria-hidden="true" />
      <button
        type="button"
        className="draw-stylebar__btn draw-stylebar__btn--danger"
        aria-label={`Delete ${kindLabel.toLowerCase()}`}
        aria-keyshortcuts="Delete"
        title="Delete (Del)"
        onClick={onDelete}
      >
        <TrashIcon size={18} />
      </button>
    </div>
  )
}

interface PopoverOption {
  key: string
  label: string
  content: ReactNode
}

interface StylePopoverProps {
  open: boolean
  onToggle(): void
  onClose(): void
  label: string
  title: string
  button: ReactNode
  options: readonly PopoverOption[]
  /** -1 when the current value is not among the options (e.g. a custom colour). */
  selectedIndex: number
  columns: number
  listClassName?: string
  onPick(index: number): void
  footer?: ReactNode
}

/** Button + listbox popover. Arrows move, Enter/Space picks, Esc or a click outside closes. */
function StylePopover({
  open,
  onToggle,
  onClose,
  label,
  title,
  button,
  options,
  selectedIndex,
  columns,
  listClassName,
  onPick,
  footer,
}: StylePopoverProps) {
  const [active, setActive] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const listId = useId()
  // The parent passes a new closure each render; read the latest without re-running the effect
  // (re-running would pull focus back to the list, e.g. away from the custom colour input).
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  })

  useEffect(() => {
    if (!open) return
    listRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) onCloseRef.current()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [open])

  const openList = () => {
    setActive(Math.max(0, selectedIndex))
    onToggle()
  }

  const close = (refocus: boolean) => {
    onClose()
    if (refocus) buttonRef.current?.focus()
  }

  const pick = (i: number) => {
    onPick(i)
    close(true)
  }

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault()
      openList()
    }
  }

  const onListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    const moved = gridMove(active, e.key, options.length, columns)
    if (moved !== null) {
      e.preventDefault()
      setActive(moved)
      return
    }
    switch (e.key) {
      case 'Enter':
      case ' ':
        e.preventDefault()
        pick(active)
        break
      case 'Tab':
        close(false)
        break
    }
  }

  return (
    <div className="draw-pop" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="draw-stylebar__btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={label}
        title={title}
        onClick={() => (open ? close(false) : openList())}
        onKeyDown={onButtonKeyDown}
      >
        {button}
        <svg
          className="draw-stylebar__caret"
          width="8"
          height="8"
          viewBox="0 0 8 8"
          aria-hidden="true"
        >
          <path d="M1 2.5 4 5.5 7 2.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      </button>
      {open && (
        <div
          className="draw-pop__panel"
          onKeyDown={(e) => {
            // Keep keys away from global chart shortcuts (Delete, Escape, Alt+…).
            e.stopPropagation()
            if (e.key !== 'Escape') return
            e.preventDefault()
            close(true)
          }}
        >
          <ul
            ref={listRef}
            id={listId}
            className={`draw-pop__list${listClassName ? ` ${listClassName}` : ''}`}
            role="listbox"
            tabIndex={-1}
            aria-label={title}
            aria-activedescendant={`${listId}-${active}`}
            onKeyDown={onListKeyDown}
          >
            {options.map((o, i) => (
              <li
                key={o.key}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={i === selectedIndex}
                aria-label={o.label}
                title={o.label}
                className={`draw-pop__option${i === active ? ' draw-pop__option--active' : ''}`}
                onPointerEnter={() => setActive(i)}
                onClick={() => pick(i)}
              >
                {o.content}
              </li>
            ))}
          </ul>
          {footer}
        </div>
      )}
    </div>
  )
}

function LineSample({
  width,
  dash,
  long = false,
}: {
  width: number
  dash: string
  long?: boolean
}) {
  const w = long ? 40 : 20
  return (
    <svg
      className="draw-line-sample"
      width={w}
      height="12"
      viewBox={`0 0 ${w} 12`}
      aria-hidden="true"
    >
      <path
        d={`M1 6H${w - 1}`}
        stroke="currentColor"
        strokeWidth={width}
        strokeDasharray={dash || undefined}
        strokeLinecap={dash ? 'round' : 'butt'}
      />
    </svg>
  )
}
