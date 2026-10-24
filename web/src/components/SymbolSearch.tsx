import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import type { SymbolInfo } from '../types'
import { quoteAssetsOf, searchSymbols } from './symbolRank'

const RESULT_LIMIT = 100

interface SymbolSearchProps {
  /** Text already typed (e.g. the key that opened the dialog). */
  initialQuery: string
  /** Tradable symbols; null while loading or after an error. */
  symbols: readonly SymbolInfo[] | null
  error: string | null
  onRetry: () => void
  activeSymbol: string
  source: string
  isInWatchlist: (symbol: string) => boolean
  onToggleWatchlist: (symbol: string) => void
  onSelect: (symbol: string) => void
  onClose: () => void
}

/**
 * Modal symbol picker. Mount it only while open: it captures the previously focused
 * element on mount and restores it on unmount.
 */
export function SymbolSearch({
  initialQuery,
  symbols,
  error,
  onRetry,
  activeSymbol,
  source,
  isInWatchlist,
  onToggleWatchlist,
  onSelect,
  onClose,
}: SymbolSearchProps) {
  const [query, setQuery] = useState(initialQuery)
  const [quote, setQuote] = useState<string | null>(null)
  const [active, setActive] = useState(0)
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const ids = useId()
  const listId = `${ids}-list`
  const titleId = `${ids}-title`

  // Restore focus to whatever opened the dialog.
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const input = inputRef.current
    input?.focus()
    // Put the caret after a prefilled character instead of selecting it.
    input?.setSelectionRange(input.value.length, input.value.length)
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  const quotes = useMemo(() => (symbols ? quoteAssetsOf(symbols) : []), [symbols])
  const results = useMemo(
    () => (symbols ? searchSymbols(symbols, query, RESULT_LIMIT, { quote }) : []),
    [symbols, query, quote],
  )
  const activeIndex = results.length === 0 ? -1 : Math.min(active, results.length - 1)
  const activeResult = activeIndex >= 0 ? results[activeIndex] : null
  const optionId = (s: string) => `${ids}-opt-${s}`

  useEffect(() => {
    if (!activeResult) return
    document
      .getElementById(`${ids}-opt-${activeResult.symbol}`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeResult, ids])

  const changeQuery = (q: string) => {
    setQuery(q)
    setActive(0)
  }

  const changeQuote = (q: string | null) => {
    setQuote(q)
    setActive(0)
    inputRef.current?.focus()
  }

  const onInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const n = results.length
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        if (n > 0) setActive((activeIndex + 1) % n)
        break
      case 'ArrowUp':
        e.preventDefault()
        if (n > 0) setActive((activeIndex - 1 + n) % n)
        break
      case 'PageDown':
        e.preventDefault()
        if (n > 0) setActive(Math.min(n - 1, activeIndex + 8))
        break
      case 'PageUp':
        e.preventDefault()
        if (n > 0) setActive(Math.max(0, activeIndex - 8))
        break
      case 'Enter':
        e.preventDefault()
        if (!activeResult) break
        if (e.shiftKey) onToggleWatchlist(activeResult.symbol)
        else onSelect(activeResult.symbol)
        break
    }
  }

  const onDialogKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !dialogRef.current) return
    // Keep Tab inside the dialog.
    const focusables = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([tabindex="-1"]), input, [tabindex="0"]',
      ),
    )
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  return (
    <div
      className="modal-backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="symbol-search"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onDialogKeyDown}
      >
        <div className="symbol-search__head">
          <h2 id={titleId} className="symbol-search__title">
            Symbol search
          </h2>
          <button
            type="button"
            className="icon-btn"
            aria-label="Close symbol search"
            title="Close (Esc)"
            onClick={onClose}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </button>
        </div>

        <div className="symbol-search__field">
          <svg
            className="symbol-search__glass"
            width="16"
            height="16"
            viewBox="0 0 16 16"
            aria-hidden="true"
          >
            <circle cx="7" cy="7" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path
              d="M10.5 10.5 14 14"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
          </svg>
          <input
            ref={inputRef}
            className="symbol-search__input"
            type="text"
            role="combobox"
            aria-label="Search symbols"
            aria-expanded={results.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={activeResult ? optionId(activeResult.symbol) : undefined}
            placeholder="Search, e.g. BTC, ETH/USDT"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="characters"
            spellCheck={false}
            value={query}
            onChange={(e) => changeQuery(e.target.value)}
            onKeyDown={onInputKeyDown}
          />
          {query !== '' && (
            <button
              type="button"
              className="icon-btn icon-btn--small"
              aria-label="Clear search"
              onClick={() => {
                changeQuery('')
                inputRef.current?.focus()
              }}
            >
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                <path d="M2 2l6 6M8 2 2 8" stroke="currentColor" strokeWidth="1.5" />
              </svg>
            </button>
          )}
        </div>

        {quotes.length > 1 && (
          <div className="chips" role="group" aria-label="Quote asset">
            <button
              type="button"
              className="chip"
              aria-pressed={quote === null}
              onClick={() => changeQuote(null)}
            >
              All
            </button>
            {quotes.map((q) => (
              <button
                key={q}
                type="button"
                className="chip"
                aria-pressed={quote === q}
                onClick={() => changeQuote(q)}
              >
                {q}
              </button>
            ))}
          </div>
        )}

        <div className="symbol-search__body">
          {symbols === null && error === null && (
            <ul className="symbol-search__list" aria-hidden="true">
              {Array.from({ length: 8 }, (_, i) => (
                <li key={i} className="symbol-row symbol-row--skeleton">
                  <span className="skeleton-line" style={{ width: `${90 + ((i * 37) % 60)}px` }} />
                  <span className="skeleton-line skeleton-line--faint" />
                </li>
              ))}
            </ul>
          )}
          {symbols === null && error === null && (
            <p className="visually-hidden" role="status">
              Loading symbols…
            </p>
          )}

          {error !== null && (
            <div className="symbol-search__message" role="alert">
              <p>{error}</p>
              <button type="button" className="btn btn--ghost" onClick={onRetry}>
                Try again
              </button>
            </div>
          )}

          {symbols !== null && results.length === 0 && (
            <div className="symbol-search__message" role="status">
              <p>
                No symbols match <strong>{query.trim() || '…'}</strong>
                {quote ? ` in ${quote}` : ''}.
              </p>
            </div>
          )}

          {symbols !== null && results.length > 0 && (
            <ul
              ref={listRef}
              id={listId}
              className="symbol-search__list"
              role="listbox"
              aria-label="Symbols"
            >
              {results.map((s, i) => {
                const starred = isInWatchlist(s.symbol)
                return (
                  <li
                    key={s.symbol}
                    id={optionId(s.symbol)}
                    role="option"
                    aria-selected={i === activeIndex}
                    className={`symbol-row${i === activeIndex ? ' symbol-row--active' : ''}${
                      s.symbol === activeSymbol ? ' symbol-row--current' : ''
                    }`}
                    onPointerMove={() => {
                      if (i !== activeIndex) setActive(i)
                    }}
                    onClick={() => onSelect(s.symbol)}
                  >
                    <span className="symbol-row__symbol">{s.symbol}</span>
                    <span className="symbol-row__desc">
                      {s.baseAsset} / {s.quoteAsset}
                    </span>
                    <span className="symbol-row__source">{source}</span>
                    <button
                      type="button"
                      tabIndex={-1}
                      className={`star-btn${starred ? ' star-btn--on' : ''}`}
                      aria-pressed={starred}
                      aria-label={
                        starred
                          ? `Remove ${s.symbol} from watchlist`
                          : `Add ${s.symbol} to watchlist`
                      }
                      title={starred ? 'Remove from watchlist' : 'Add to watchlist (Shift+Enter)'}
                      onClick={(e) => {
                        e.stopPropagation()
                        onToggleWatchlist(s.symbol)
                        inputRef.current?.focus()
                      }}
                    >
                      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                        <path
                          d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z"
                          fill={starred ? 'currentColor' : 'none'}
                          stroke="currentColor"
                          strokeWidth="1.2"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="symbol-search__foot" aria-hidden="true">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>Enter</kbd> open
          </span>
          <span>
            <kbd>Shift</kbd>+<kbd>Enter</kbd> watchlist
          </span>
          <span>
            <kbd>Esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  )
}
