import { memo, useId, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  PRESET_TIMEFRAMES,
  parseTimeframe,
  timeframeLabel,
  timeframeName,
  timeframeSeconds,
} from '../data/timeframes'
import type { Timeframe, TimeframeUnit } from '../types'
import { useMenu } from './useMenu'
import './Timeframes.css'

/** Quick buttons shown by default (the previous toolbar's intervals). */
const DEFAULT_FAVORITES: readonly Timeframe[] = ['1m', '5m', '15m', '1h', '4h', '1d', '1w']

const GROUPS: readonly { id: string; label: string; units: readonly TimeframeUnit[] }[] = [
  { id: 'seconds', label: 'Seconds', units: ['s'] },
  { id: 'minutes', label: 'Minutes', units: ['m'] },
  { id: 'hours', label: 'Hours', units: ['h'] },
  { id: 'days', label: 'Days', units: ['d', 'w'] },
]

const CUSTOM_UNITS: readonly { unit: TimeframeUnit; label: string }[] = [
  { unit: 's', label: 'seconds' },
  { unit: 'm', label: 'minutes' },
  { unit: 'h', label: 'hours' },
  { unit: 'd', label: 'days' },
  { unit: 'w', label: 'weeks' },
]

/** Why a custom value was refused (mirrors parseTimeframe's limits). */
const LIMIT_HINTS: Record<TimeframeUnit, string> = {
  s: 'Seconds must divide 60: 1, 2, 3, 4, 5, 6, 10, 12, 15, 20 or 30.',
  m: 'Minutes: a whole number from 1 to 720.',
  h: 'Hours: a whole number from 1 to 168.',
  d: 'Days: a whole number from 1 to 365.',
  w: 'Weeks: a whole number from 1 to 52.',
}

const UNAVAILABLE = 'Not available from this data source'

function unitOf(tf: Timeframe): TimeframeUnit {
  return tf.slice(-1) as TimeframeUnit
}

interface TimeframePickerProps {
  value: Timeframe
  onChange: (tf: Timeframe) => void
  /** False for timeframes the data source cannot serve (shown disabled). All allowed if omitted. */
  supports?: (tf: Timeframe) => boolean
  /** Quick buttons; defaults to 1m 5m 15m 1h 4h 1D 1W. The current value is always shown. */
  favorites?: readonly Timeframe[]
}

/**
 * Timeframe quick buttons plus a dropdown of grouped presets and a custom "number + unit"
 * entry. The list is a listbox: arrows move (skipping unavailable entries), Enter/Space picks,
 * Esc or a click outside closes, Tab reaches the custom entry.
 */
export const TimeframePicker = memo(function TimeframePicker({
  value,
  onChange,
  supports,
  favorites = DEFAULT_FAVORITES,
}: TimeframePickerProps) {
  const [count, setCount] = useState('')
  const [unit, setUnit] = useState<TimeframeUnit>('m')
  const [error, setError] = useState<string | null>(null)
  const baseId = useId()
  const listId = `${baseId}-list`
  const errorId = `${baseId}-error`

  const allowed = (tf: Timeframe) => supports?.(tf) ?? true

  const quick = useMemo(
    () => (favorites.includes(value) ? favorites : [...favorites, value]),
    [favorites, value],
  )

  /** Presets plus the current value when it is custom, shortest first. */
  const options = useMemo(() => {
    const all = PRESET_TIMEFRAMES.includes(value)
      ? [...PRESET_TIMEFRAMES]
      : [...PRESET_TIMEFRAMES, value]
    return all.sort((a, b) => timeframeSeconds(a) - timeframeSeconds(b))
  }, [value])

  const groups = GROUPS.map((g) => ({
    ...g,
    items: options.filter((tf) => g.units.includes(unitOf(tf))),
  })).filter((g) => g.items.length > 0)
  /** Options in display (group) order; `active` indexes this. */
  const flat = groups.flatMap((g) => g.items)

  const {
    open,
    active,
    setActive,
    rootRef,
    buttonRef,
    menuRef,
    openMenu: openPanel,
    close,
    onMenuKey,
    rove,
  } = useMenu<HTMLDivElement>({
    openAt: () => {
      const i = flat.indexOf(value)
      return i >= 0 ? i : Math.max(0, flat.findIndex(allowed))
    },
    tabCloses: false, // tabbing out closes through the panel's onBlur
  })

  const openMenu = () => {
    setError(null)
    openPanel()
  }

  const pick = (tf: Timeframe) => {
    if (!allowed(tf)) return
    close(true)
    if (tf !== value) onChange(tf)
  }

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      openMenu()
    }
  }

  const onListKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // Arrows, Home and End skip unavailable entries.
    if (rove(e, flat.length, (i) => allowed(flat[i]))) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (flat[active] !== undefined) pick(flat[active])
    }
  }

  const onCustomSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const tf = parseTimeframe(`${count.trim()}${unit}`)
    if (tf === null) {
      setError(LIMIT_HINTS[unit])
      return
    }
    if (!allowed(tf)) {
      setError(`${timeframeName(tf)}: ${UNAVAILABLE.toLowerCase()}.`)
      return
    }
    setCount('')
    setError(null)
    pick(tf)
  }

  return (
    <div className="tfp" ref={rootRef} role="group" aria-label="Timeframe">
      {quick.map((tf) => {
        const ok = allowed(tf)
        return (
          <button
            key={tf}
            type="button"
            className="tfp__btn"
            aria-pressed={tf === value}
            aria-label={timeframeName(tf)}
            title={ok ? timeframeName(tf) : `${timeframeName(tf)} (${UNAVAILABLE.toLowerCase()})`}
            disabled={!ok}
            onClick={() => {
              if (tf !== value) onChange(tf)
            }}
          >
            {timeframeLabel(tf)}
          </button>
        )
      })}
      <button
        ref={buttonRef}
        type="button"
        className="tfp__more"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? `${baseId}-panel` : undefined}
        aria-label="More timeframes"
        title="More timeframes"
        onClick={() => (open ? close(false) : openMenu())}
        onKeyDown={onButtonKeyDown}
      >
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      </button>
      {open && (
        <div
          id={`${baseId}-panel`}
          className="tfp__panel"
          role="dialog"
          aria-label="Timeframes"
          onKeyDown={onMenuKey}
          onBlur={(e) => {
            // Tabbing out of the panel closes it.
            const next = e.relatedTarget as Node | null
            if (next !== null && !rootRef.current?.contains(next)) close(false)
          }}
        >
          <div
            ref={menuRef}
            id={listId}
            className="tfp__list"
            role="listbox"
            tabIndex={0}
            aria-label="Timeframes"
            aria-activedescendant={
              flat[active] !== undefined ? `${baseId}-opt-${flat[active]}` : undefined
            }
            onKeyDown={onListKeyDown}
          >
            {groups.map((g) => (
              <div key={g.id} role="group" aria-labelledby={`${baseId}-grp-${g.id}`}>
                <div id={`${baseId}-grp-${g.id}`} className="tfp__group" role="presentation">
                  {g.label}
                </div>
                {g.items.map((tf) => {
                  const ok = allowed(tf)
                  const i = flat.indexOf(tf)
                  return (
                    <div
                      key={tf}
                      id={`${baseId}-opt-${tf}`}
                      role="option"
                      aria-selected={tf === value}
                      aria-disabled={!ok || undefined}
                      title={ok ? undefined : UNAVAILABLE}
                      className={`tfp__option${i === active ? ' tfp__option--active' : ''}`}
                      onPointerEnter={() => {
                        if (ok) setActive(i)
                      }}
                      onClick={() => pick(tf)}
                    >
                      <span className="tfp__name">{timeframeName(tf)}</span>
                      {!PRESET_TIMEFRAMES.includes(tf) && <span className="tfp__tag">custom</span>}
                      {tf === value ? (
                        <svg
                          className="tfp__check"
                          width="12"
                          height="12"
                          viewBox="0 0 12 12"
                          aria-hidden="true"
                        >
                          <path
                            d="M2 6.5 5 9.5 10 3"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.6"
                          />
                        </svg>
                      ) : (
                        <span className="tfp__short" aria-hidden="true">
                          {timeframeLabel(tf)}
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
          <form className="tfp__custom" onSubmit={onCustomSubmit} noValidate>
            <label className="tfp__custom-label" htmlFor={`${baseId}-count`}>
              Custom
            </label>
            <div className="tfp__custom-row">
              <input
                id={`${baseId}-count`}
                className="tfp__input"
                type="number"
                inputMode="numeric"
                min={1}
                step={1}
                placeholder="e.g. 10"
                value={count}
                aria-invalid={error !== null}
                aria-describedby={error !== null ? errorId : undefined}
                onChange={(e) => {
                  setCount(e.target.value)
                  setError(null)
                }}
              />
              <select
                className="tfp__select"
                aria-label="Unit"
                value={unit}
                onChange={(e) => {
                  setUnit(e.target.value as TimeframeUnit)
                  setError(null)
                }}
              >
                {CUSTOM_UNITS.map((u) => (
                  <option key={u.unit} value={u.unit}>
                    {u.label}
                  </option>
                ))}
              </select>
              <button type="submit" className="tfp__apply" disabled={count.trim() === ''}>
                Apply
              </button>
            </div>
            {error !== null && (
              <p id={errorId} className="tfp__error" role="alert">
                {error}
              </p>
            )}
          </form>
        </div>
      )}
    </div>
  )
})
