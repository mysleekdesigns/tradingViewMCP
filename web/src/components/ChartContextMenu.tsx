import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import type { ChartContextMenuRequest } from '../chart/drawings/types'
import { ToolIcon, TrashIcon } from './DrawingToolbar'
import { clampMenuPosition } from './alertsView'
import './Drawings.css'

export interface ChartContextMenuProps {
  request: ChartContextMenuRequest
  symbol: string
  formatPrice(price: number): string
  alertsAvailable: boolean
  onAddAlert(price: number): void
  onAddHorizontalLine(price: number): void
  onDeleteDrawing(id: string): void
  onClose(): void
}

interface MenuItem {
  key: string
  label: string
  icon: ReactNode
  hint?: string
  disabled?: boolean
  danger?: boolean
  /** Draw a separator above this item. */
  separated?: boolean
  run(): void
}

/**
 * Right-click menu on the chart. Fixed at the pointer, kept inside the viewport. Arrows move,
 * Enter/Space runs, Esc, a click outside, scrolling, resizing, or losing window focus closes.
 * Mount with a fresh `key` per request so it re-measures and refocuses.
 */
export function ChartContextMenu({
  request,
  symbol,
  formatPrice,
  alertsAvailable,
  onAddAlert,
  onAddHorizontalLine,
  onDeleteDrawing,
  onClose,
}: ChartContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null)
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [active, setActive] = useState(0)
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  })

  const { price, drawingId } = request
  const items: MenuItem[] = []
  if (price !== null) {
    const p = formatPrice(price)
    items.push({
      key: 'alert',
      label: `Add alert on ${symbol} at ${p}`,
      icon: <BellIcon />,
      hint: alertsAvailable ? 'Alt+A' : 'Needs the local server',
      disabled: !alertsAvailable,
      run: () => onAddAlert(price),
    })
    items.push({
      key: 'hline',
      label: `Draw horizontal line at ${p}`,
      icon: <ToolIcon tool="hline" />,
      run: () => onAddHorizontalLine(price),
    })
  }
  if (drawingId !== null) {
    items.push({
      key: 'delete',
      label: 'Delete drawing',
      icon: <TrashIcon size={20} />,
      hint: 'Del',
      danger: true,
      separated: items.length > 0,
      run: () => onDeleteDrawing(drawingId),
    })
  }
  const count = items.length

  // Measure, then place inside the viewport before paint (positioned via the DOM, not state).
  useLayoutEffect(() => {
    const el = menuRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const { x, y } = clampMenuPosition(
      request.clientX,
      request.clientY,
      r.width,
      r.height,
      window.innerWidth,
      window.innerHeight,
    )
    el.style.left = `${x}px`
    el.style.top = `${y}px`
  }, [request.clientX, request.clientY, count])

  // Focus the first item on open; restore focus on close.
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    ;(itemRefs.current[0] ?? menuRef.current)?.focus()
    return () => {
      if (previous?.isConnected && previous !== document.body) previous.focus()
    }
  }, [])

  useEffect(() => {
    const close = () => onCloseRef.current()
    const onPointerDown = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) close()
    }
    const onScroll = (e: Event) => {
      if (!menuRef.current?.contains(e.target as Node)) close()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('wheel', close, { passive: true })
    window.addEventListener('resize', close)
    window.addEventListener('blur', close)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('wheel', close)
      window.removeEventListener('resize', close)
      window.removeEventListener('blur', close)
    }
  }, [])

  const focusItem = (i: number) => {
    setActive(i)
    itemRefs.current[i]?.focus()
  }

  const runItem = (item: MenuItem) => {
    if (item.disabled) return
    onClose()
    item.run()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // The menu owns the keyboard while open (no chart shortcuts underneath).
    e.stopPropagation()
    if (count === 0) {
      if (e.key === 'Escape' || e.key === 'Tab') {
        e.preventDefault()
        onClose()
      }
      return
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        focusItem((active + 1) % count)
        break
      case 'ArrowUp':
        e.preventDefault()
        focusItem((active - 1 + count) % count)
        break
      case 'Home':
        e.preventDefault()
        focusItem(0)
        break
      case 'End':
        e.preventDefault()
        focusItem(count - 1)
        break
      case 'Escape':
      case 'Tab':
        e.preventDefault()
        onClose()
        break
    }
  }

  return (
    <div
      ref={menuRef}
      className="chart-menu"
      role="menu"
      aria-label="Chart actions"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => e.preventDefault()}
    >
      {count === 0 && <p className="chart-menu__empty">No actions here</p>}
      {items.map((item, i) => (
        <div key={item.key} role="none">
          {item.separated && <div className="chart-menu__sep" role="separator" />}
          <button
            ref={(el) => {
              itemRefs.current[i] = el
            }}
            type="button"
            role="menuitem"
            tabIndex={i === active ? 0 : -1}
            aria-disabled={item.disabled || undefined}
            className={`chart-menu__item${item.danger ? ' chart-menu__item--danger' : ''}`}
            onFocus={() => setActive(i)}
            onPointerEnter={() => focusItem(i)}
            onClick={() => runItem(item)}
          >
            <span className="chart-menu__icon">{item.icon}</span>
            <span className="chart-menu__label">{item.label}</span>
            {item.hint && <span className="chart-menu__hint">{item.hint}</span>}
          </button>
        </div>
      ))}
    </div>
  )
}

function BellIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 22 22"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M6 15.5V10a5 5 0 0 1 10 0v5.5l1.5 1.5h-13zM9.2 17.5a1.9 1.9 0 0 0 3.6 0" />
      <path d="M17 4.5l1.5-1.2M5 4.5L3.5 3.3" />
    </svg>
  )
}
