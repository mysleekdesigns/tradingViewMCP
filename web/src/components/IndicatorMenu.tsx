import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { INDICATORS } from '../indicators/registry'
import type { IndicatorDef, IndicatorId, Placement } from '../indicators/types'
import { MAX_INDICATORS } from '../state/indicators'
import {
  countByType,
  filterIndicators,
  flattenGroups,
  groupIndicators,
} from './indicatorMenuFilter'
import './Indicators.css'

/** How long the "Added" tick stays on a row. */
const FLASH_MS = 1400

interface IndicatorMenuProps {
  /** Active instances (only `type` is read), for the per-row count. */
  items: readonly { type: IndicatorId }[]
  /** Adds one instance; returns the new uid, or null when the cap is reached. */
  onAdd: (type: IndicatorId) => string | null
  onClose: () => void
}

interface Flash {
  type: IndicatorId
  /** Bumped on every add so repeated adds of one type restart the timer. */
  seq: number
}

/**
 * Modal "Indicators" picker. Stays open after an add so several can be stacked quickly.
 * Mount it only while open: it captures the previously focused element on mount and
 * restores it on unmount.
 */
export function IndicatorMenu({ items, onAdd, onClose }: IndicatorMenuProps) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [flash, setFlash] = useState<Flash | null>(null)
  const [limitHit, setLimitHit] = useState(false)
  const [announce, setAnnounce] = useState('')
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const ids = useId()
  const listId = `${ids}-list`
  const titleId = `${ids}-title`
  const optionId = (id: IndicatorId) => `${ids}-opt-${id}`

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    inputRef.current?.focus()
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  useEffect(() => {
    if (flash === null) return
    const t = window.setTimeout(() => setFlash(null), FLASH_MS)
    return () => window.clearTimeout(t)
  }, [flash])

  const groups = useMemo(() => groupIndicators(filterIndicators(INDICATORS, query)), [query])
  const shown = useMemo(() => flattenGroups(groups), [groups])
  const counts = useMemo(() => countByType(items), [items])
  const total = items.length

  const activeIndex = shown.length === 0 ? -1 : Math.min(active, shown.length - 1)
  const activeDef = activeIndex >= 0 ? shown[activeIndex] : null

  useEffect(() => {
    if (!activeDef) return
    document.getElementById(`${ids}-opt-${activeDef.id}`)?.scrollIntoView({ block: 'nearest' })
  }, [activeDef, ids])

  const add = (def: IndicatorDef) => {
    const uid = onAdd(def.id)
    if (uid === null) {
      setLimitHit(true)
      setAnnounce(`Limit of ${MAX_INDICATORS} indicators reached`)
      return
    }
    setLimitHit(false)
    setFlash((f) => ({ type: def.id, seq: (f?.seq ?? 0) + 1 }))
    const n = (counts.get(def.id) ?? 0) + 1
    setAnnounce(`Added ${def.name}${n > 1 ? ` (${n} on chart)` : ''}`)
  }

  const changeQuery = (q: string) => {
    setQuery(q)
    setActive(0)
  }

  const onInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const n = shown.length
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        if (n > 0) setActive((activeIndex + 1) % n)
        break
      case 'ArrowUp':
        e.preventDefault()
        if (n > 0) setActive((activeIndex - 1 + n) % n)
        break
      case 'Home':
        if (n > 0 && query === '') {
          e.preventDefault()
          setActive(0)
        }
        break
      case 'End':
        if (n > 0 && query === '') {
          e.preventDefault()
          setActive(n - 1)
        }
        break
      case 'Enter':
        e.preventDefault()
        if (activeDef && !e.repeat) add(activeDef)
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
        className="ind-dialog ind-menu"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onDialogKeyDown}
      >
        <div className="ind-dialog__head">
          <h2 id={titleId} className="ind-dialog__title">
            Indicators
          </h2>
          <span className="ind-menu__total" aria-hidden="true">
            {total} / {MAX_INDICATORS} on chart
          </span>
          <button
            type="button"
            className="icon-btn"
            aria-label="Close indicators"
            title="Close (Esc)"
            onClick={onClose}
          >
            <CloseIcon />
          </button>
        </div>

        <div className="ind-menu__field">
          <svg
            className="ind-menu__glass"
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
            className="ind-menu__input"
            type="text"
            role="combobox"
            aria-label="Search indicators"
            aria-expanded={shown.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={activeDef ? optionId(activeDef.id) : undefined}
            placeholder="Search, e.g. EMA, RSI, Bollinger"
            autoComplete="off"
            autoCorrect="off"
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

        {limitHit && (
          <p className="ind-menu__limit" role="alert">
            Limit of {MAX_INDICATORS} indicators reached. Remove one from the chart legend to add
            another.
          </p>
        )}

        <div className="ind-menu__body">
          {shown.length === 0 ? (
            <div className="ind-menu__empty" role="status">
              No indicators match <strong>{query.trim()}</strong>.
            </div>
          ) : (
            <div id={listId} className="ind-menu__list" role="listbox" aria-label="Indicators">
              {groups.map((g) => (
                <div key={g.placement} role="group" aria-labelledby={`${ids}-grp-${g.placement}`}>
                  <div
                    id={`${ids}-grp-${g.placement}`}
                    className="ind-menu__group"
                    role="presentation"
                  >
                    <GroupIcon placement={g.placement} />
                    {g.label}
                  </div>
                  {g.items.map((def) => {
                    const i = shown.indexOf(def)
                    const count = counts.get(def.id) ?? 0
                    const flashing = flash?.type === def.id
                    return (
                      <div
                        key={def.id}
                        id={optionId(def.id)}
                        role="option"
                        aria-selected={i === activeIndex}
                        className={`ind-row${i === activeIndex ? ' ind-row--active' : ''}`}
                        onPointerMove={() => {
                          if (i !== activeIndex) setActive(i)
                        }}
                        onClick={() => {
                          add(def)
                          inputRef.current?.focus()
                        }}
                      >
                        <span className="ind-row__main">
                          <span className="ind-row__name">{def.name}</span>
                          <span className="ind-row__desc">{def.description}</span>
                        </span>
                        <span className="ind-row__short">{def.shortName}</span>
                        <span className="ind-row__state">
                          {flashing ? (
                            <span key={flash.seq} className="ind-row__added">
                              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                                <path
                                  d="M2.5 6.2 5 8.5l4.5-5"
                                  fill="none"
                                  stroke="currentColor"
                                  strokeWidth="1.6"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                />
                              </svg>
                              Added
                            </span>
                          ) : count > 0 ? (
                            <span className="ind-row__count" title={`${count} on chart`}>
                              {count}
                              <span className="visually-hidden"> on chart</span>
                            </span>
                          ) : (
                            <span className="ind-row__plus" aria-hidden="true">
                              +
                            </span>
                          )}
                        </span>
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          )}
        </div>

        <p className="visually-hidden" role="status" aria-live="polite">
          {announce}
        </p>

        <div className="ind-dialog__foot ind-menu__foot" aria-hidden="true">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>Enter</kbd> add
          </span>
          <span>
            <kbd>Esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
      <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

function GroupIcon({ placement }: { placement: Placement }) {
  // Tiny glyphs: a line over price, volume bars, an oscillator in its own pane.
  const d =
    placement === 'overlay'
      ? 'M1.5 9.5 4.5 6l2.5 2 4.5-5'
      : placement === 'volume'
        ? 'M2.5 11V7M5.5 11V4M8.5 11V8M11.5 11V5.5'
        : 'M1 6.5c1.5-3 3-3 4.5 0s3 3 4.5 0 1.5-2 2-2'
  return (
    <svg
      className="ind-menu__group-icon"
      width="13"
      height="13"
      viewBox="0 0 13 13"
      aria-hidden="true"
    >
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
