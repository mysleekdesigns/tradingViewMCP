import {
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import type { MarketDataProvider } from '../data/provider'
import {
  formatTickerChange,
  formatTickerPct,
  formatTickerPrice,
  tickerDirection,
  type TickMove,
} from '../state/tickers'
import { useTickers } from '../state/useTickers'
import type { SymbolInfo, Ticker } from '../types'
import { ConnectionIndicator } from './ConnectionStatus'
import './Watchlist.css'

export interface WatchlistProps {
  provider: MarketDataProvider
  items: readonly string[]
  activeSymbol: string
  /** Known symbols by name, for base/quote labels and tickSize price formatting; null while loading. */
  symbolInfo: ReadonlyMap<string, SymbolInfo> | null
  onSelect(symbol: string): void
  onRemove(symbol: string): void
  onMove(from: number, to: number): void
  /** Opens the symbol search so the user can add a symbol. */
  onAdd(): void
}

interface DropTarget {
  index: number
  after: boolean
}

/**
 * Right-hand watchlist: live last price and 24h change for each saved symbol. Click or Enter
 * loads a symbol; drag rows (or Alt+Arrow) to reorder; Delete or the × button removes.
 */
export const Watchlist = memo(function Watchlist({
  provider,
  items,
  activeSymbol,
  symbolInfo,
  onSelect,
  onRemove,
  onMove,
  onAdd,
}: WatchlistProps) {
  const { tickers, moves, status } = useTickers(provider, items)
  const helpId = useId()
  const rowRefs = useRef(new Map<string, HTMLDivElement>())
  const pendingFocus = useRef<string | null>(null)
  const [dragFrom, setDragFrom] = useState<number | null>(null)
  const [drop, setDrop] = useState<DropTarget | null>(null)

  // After a keyboard move or remove, keep focus on the moved row (or the neighbour).
  useEffect(() => {
    const symbol = pendingFocus.current
    if (symbol === null) return
    pendingFocus.current = null
    rowRefs.current.get(symbol)?.focus()
  }, [items])

  const activeIndex = items.indexOf(activeSymbol)
  const tabStop = activeIndex >= 0 ? activeIndex : 0

  // Row handlers are created once and read the latest render's values from this ref, so a
  // memoised row re-renders only when its own data changes (not on every ticker emit or parent
  // render). Updated after each commit, which is when the handlers of that render would have
  // been attached before.
  const latest = useRef({ items, dragFrom, drop, onSelect, onRemove, onMove })
  useLayoutEffect(() => {
    latest.current = { items, dragFrom, drop, onSelect, onRemove, onMove }
  })

  const handlers = useMemo<RowHandlers>(() => {
    const focusRow = (index: number) => {
      const { items } = latest.current
      const symbol = items[Math.min(Math.max(index, 0), items.length - 1)]
      if (symbol !== undefined) rowRefs.current.get(symbol)?.focus()
    }

    const remove = (index: number, refocus: boolean) => {
      const { items, onRemove } = latest.current
      const symbol = items[index]
      if (symbol === undefined) return
      if (refocus) pendingFocus.current = items[index + 1] ?? items[index - 1] ?? null
      onRemove(symbol)
    }

    const endDrag = () => {
      setDragFrom(null)
      setDrop(null)
    }

    return {
      rowRef: (symbol, el) => {
        if (el === null) rowRefs.current.delete(symbol)
        else rowRefs.current.set(symbol, el)
      },
      select: (symbol) => latest.current.onSelect(symbol),
      remove: (index) => remove(index, false),
      keyDown: (e, index) => {
        const { items, onSelect, onMove } = latest.current
        const symbol = items[index]
        switch (e.key) {
          case 'Enter':
          case ' ':
            e.preventDefault()
            onSelect(symbol)
            break
          case 'ArrowUp':
          case 'ArrowDown': {
            e.preventDefault()
            const step = e.key === 'ArrowUp' ? -1 : 1
            if (e.altKey) {
              const to = index + step
              if (to < 0 || to >= items.length) return
              pendingFocus.current = symbol
              onMove(index, to)
            } else focusRow(index + step)
            break
          }
          case 'Home':
            e.preventDefault()
            focusRow(0)
            break
          case 'End':
            e.preventDefault()
            focusRow(items.length - 1)
            break
          case 'Delete':
          case 'Backspace':
            e.preventDefault()
            remove(index, true)
            break
        }
      },
      dragStart: (e, symbol, index) => {
        e.dataTransfer.effectAllowed = 'move'
        e.dataTransfer.setData('text/plain', symbol)
        setDragFrom(index)
      },
      dragOver: (e, index) => {
        const { dragFrom, drop } = latest.current
        if (dragFrom === null) return // not one of our rows
        e.preventDefault()
        e.dataTransfer.dropEffect = 'move'
        const rect = e.currentTarget.getBoundingClientRect()
        const after = e.clientY > rect.top + rect.height / 2
        if (drop?.index !== index || drop.after !== after) setDrop({ index, after })
      },
      drop: (e) => {
        const { dragFrom, drop, onMove } = latest.current
        if (dragFrom === null || drop === null) return endDrag()
        e.preventDefault()
        const insertAt = drop.index + (drop.after ? 1 : 0)
        const to = insertAt > dragFrom ? insertAt - 1 : insertAt
        if (to !== dragFrom) onMove(dragFrom, to)
        endDrag()
      },
      dragEnd: endDrag,
    }
  }, [])

  return (
    <section className="watchlist" aria-label="Watchlist">
      <header className="watchlist__header">
        <h2 className="watchlist__title">Watchlist</h2>
        {items.length > 0 && <span className="watchlist__count">{items.length}</span>}
        <span className="watchlist__spacer" />
        <ConnectionIndicator status={status} variant="compact" />
        <button
          type="button"
          className="watchlist__icon-btn"
          onClick={onAdd}
          aria-label="Add symbol"
          title="Add symbol"
        >
          <PlusIcon />
        </button>
      </header>

      {items.length === 0 ? (
        <div className="watchlist__empty">
          <p className="watchlist__empty-title">Your watchlist is empty</p>
          <p className="watchlist__empty-text">Add symbols to follow their live prices here.</p>
          <button type="button" className="btn btn--primary" onClick={onAdd}>
            <PlusIcon />
            Add symbol
          </button>
        </div>
      ) : (
        <>
          <div className="watchlist__columns" aria-hidden="true">
            <span>Symbol</span>
            <span>Last</span>
            <span>Chg</span>
            <span>Chg%</span>
          </div>
          <ul
            className="watchlist__list"
            aria-label="Watchlist symbols"
            aria-describedby={helpId}
            onDragLeave={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDrop(null)
            }}
          >
            {items.map((symbol, index) => (
              <WatchlistRow
                key={symbol}
                symbol={symbol}
                info={symbolInfo?.get(symbol)}
                unknown={symbolInfo !== null && !symbolInfo.has(symbol)}
                ticker={tickers.get(symbol)}
                move={moves.get(symbol)}
                active={symbol === activeSymbol}
                tabbable={index === tabStop}
                dragging={dragFrom === index}
                dropEdge={drop?.index === index ? (drop.after ? 'after' : 'before') : null}
                index={index}
                handlers={handlers}
              />
            ))}
          </ul>
          <p id={helpId} className="watchlist__sr-only">
            Enter loads the symbol. Arrow keys move between rows, Alt plus Arrow reorders, Delete
            removes.
          </p>
        </>
      )}
    </section>
  )
})

/**
 * Row callbacks shared by every row and stable for the Watchlist's lifetime; each takes the
 * row's symbol or index, so no per-row closures are created on a render.
 */
interface RowHandlers {
  rowRef(symbol: string, el: HTMLDivElement | null): void
  select(symbol: string): void
  remove(index: number): void
  keyDown(e: KeyboardEvent<HTMLDivElement>, index: number): void
  dragStart(e: DragEvent<HTMLLIElement>, symbol: string, index: number): void
  dragOver(e: DragEvent<HTMLLIElement>, index: number): void
  drop(e: DragEvent<HTMLLIElement>): void
  dragEnd(): void
}

interface RowProps {
  symbol: string
  /** Position in the list; the handlers take it (reorder and keyboard moves). */
  index: number
  info: SymbolInfo | undefined
  /** The exchange's symbol list is loaded and this symbol is not on it. */
  unknown: boolean
  ticker: Ticker | undefined
  move: TickMove | undefined
  active: boolean
  tabbable: boolean
  dragging: boolean
  dropEdge: 'before' | 'after' | null
  handlers: RowHandlers
}

/** Memoised: re-renders only when this row's ticker, flash, position or state changes. */
const WatchlistRow = memo(function WatchlistRow({
  symbol,
  index,
  info,
  unknown,
  ticker,
  move,
  active,
  tabbable,
  dragging,
  dropEdge,
  handlers,
}: RowProps) {
  const rowRef = useCallback(
    (el: HTMLDivElement | null) => handlers.rowRef(symbol, el),
    [handlers, symbol],
  )
  const tick = info?.tickSize
  const direction = ticker === undefined ? 'flat' : tickerDirection(ticker.changePct)
  const base = info?.baseAsset ?? symbol
  const quote = info?.quoteAsset
  const price = ticker === undefined ? null : formatTickerPrice(ticker.last, tick)
  const change = ticker === undefined ? null : formatTickerChange(ticker.last, ticker.open, tick)
  const pct = ticker === undefined ? null : formatTickerPct(ticker.changePct)
  const label =
    ticker === undefined
      ? `${symbol}, ${unknown ? 'not listed on this exchange' : 'price loading'}`
      : `${symbol}, last ${price}, ${pct}`

  const className = [
    'watchlist__item',
    active && 'watchlist__item--active',
    dragging && 'watchlist__item--dragging',
    dropEdge !== null && `watchlist__item--drop-${dropEdge}`,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <li
      className={className}
      draggable
      onDragStart={(e) => handlers.dragStart(e, symbol, index)}
      onDragOver={(e) => handlers.dragOver(e, index)}
      onDrop={handlers.drop}
      onDragEnd={handlers.dragEnd}
    >
      <div
        ref={rowRef}
        role="button"
        tabIndex={tabbable ? 0 : -1}
        className={`watchlist__row watchlist__row--${direction}`}
        aria-current={active ? 'true' : undefined}
        aria-label={label}
        aria-keyshortcuts="Enter Alt+ArrowUp Alt+ArrowDown Delete"
        title={unknown ? `${symbol} is not listed on this exchange` : undefined}
        onClick={() => handlers.select(symbol)}
        onKeyDown={(e) => handlers.keyDown(e, index)}
      >
        <span className="watchlist__symbol">
          <span className="watchlist__base">{base}</span>
          {quote !== undefined && <span className="watchlist__quote">{quote}</span>}
        </span>
        <span className="watchlist__num watchlist__last">
          {price === null ? (
            <Dash />
          ) : (
            <span
              key={`${ticker?.last}`}
              className={move !== undefined ? `watchlist__flash--${move}` : undefined}
            >
              {price}
            </span>
          )}
        </span>
        <span className="watchlist__num watchlist__chg">{change ?? <Dash />}</span>
        <span className="watchlist__num watchlist__pct">{pct ?? <Dash />}</span>
      </div>
      <button
        type="button"
        className="watchlist__remove"
        tabIndex={-1}
        aria-label={`Remove ${symbol} from watchlist`}
        title="Remove from watchlist"
        onClick={(e) => {
          e.stopPropagation()
          handlers.remove(index)
        }}
      >
        <CloseIcon />
      </button>
    </li>
  )
})

function Dash() {
  return <span className="watchlist__dash">—</span>
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <path d="M2 2l6 6M8 2L2 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}
