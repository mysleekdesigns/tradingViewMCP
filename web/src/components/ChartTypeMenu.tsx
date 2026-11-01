import { memo, useId, type KeyboardEvent } from 'react'
import { CHART_TYPES, type ChartType } from '../types'
import { useMenu } from './useMenu'

const CHART_TYPE_LABELS: Record<ChartType, string> = {
  candles: 'Candles',
  hollow: 'Hollow candles',
  line: 'Line',
  area: 'Area',
  'heikin-ashi': 'Heikin Ashi',
}

interface ChartTypeMenuProps {
  value: ChartType
  onChange: (type: ChartType) => void
}

/** Button + listbox. Arrow keys move, Enter/Space picks, Esc or a click outside closes. */
export const ChartTypeMenu = memo(function ChartTypeMenu({ value, onChange }: ChartTypeMenuProps) {
  const {
    open,
    active,
    setActive,
    rootRef,
    buttonRef,
    menuRef,
    openMenu,
    close,
    toggle,
    onMenuKey,
    rove,
  } = useMenu<HTMLUListElement>({ openAt: () => Math.max(0, CHART_TYPES.indexOf(value)) })
  const listId = useId()

  const pick = (type: ChartType) => {
    close(true)
    if (type !== value) onChange(type)
  }

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      openMenu()
    }
  }

  const onListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    if (rove(e, CHART_TYPES.length) || onMenuKey(e)) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      pick(CHART_TYPES[active])
    }
  }

  return (
    <div className="ct-menu" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="ct-menu__button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`Chart type: ${CHART_TYPE_LABELS[value]}`}
        title="Chart type"
        onClick={toggle}
        onKeyDown={onButtonKeyDown}
      >
        <ChartTypeIcon type={value} />
        <svg className="ct-menu__caret" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
          <path d="M1 2.5 4 5.5 7 2.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      </button>
      {open && (
        <ul
          ref={menuRef}
          id={listId}
          className="ct-menu__list"
          role="listbox"
          tabIndex={-1}
          aria-label="Chart type"
          aria-activedescendant={`${listId}-${CHART_TYPES[active]}`}
          onKeyDown={onListKeyDown}
        >
          {CHART_TYPES.map((type, i) => (
            <li
              key={type}
              id={`${listId}-${type}`}
              role="option"
              aria-selected={type === value}
              className={`ct-menu__option${i === active ? ' ct-menu__option--active' : ''}`}
              onPointerEnter={() => setActive(i)}
              onClick={() => pick(type)}
            >
              <ChartTypeIcon type={type} />
              <span>{CHART_TYPE_LABELS[type]}</span>
              {type === value && (
                <svg
                  className="ct-menu__check"
                  width="12"
                  height="12"
                  viewBox="0 0 12 12"
                  aria-hidden="true"
                >
                  <path d="M2 6.5 5 9.5 10 3" fill="none" stroke="currentColor" strokeWidth="1.6" />
                </svg>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
})

function ChartTypeIcon({ type }: { type: ChartType }) {
  const common = { width: 18, height: 18, viewBox: '0 0 18 18', 'aria-hidden': true } as const
  switch (type) {
    case 'candles':
      return (
        <svg {...common}>
          <path d="M5 2v14M13 3v12" stroke="currentColor" strokeWidth="1" />
          <rect x="3" y="5" width="4" height="7" fill="currentColor" />
          <rect x="11" y="6" width="4" height="5" fill="currentColor" />
        </svg>
      )
    case 'hollow':
      return (
        <svg {...common}>
          <path d="M5 2v3M5 12v4M13 3v3M13 11v4" stroke="currentColor" strokeWidth="1" />
          <rect x="3.5" y="5.5" width="3" height="6" fill="none" stroke="currentColor" />
          <rect x="11" y="6" width="4" height="5" fill="currentColor" />
        </svg>
      )
    case 'heikin-ashi':
      return (
        <svg {...common}>
          <path d="M5 3v13M13 2v12" stroke="currentColor" strokeWidth="1" />
          <rect x="3" y="7" width="4" height="6" rx="1" fill="currentColor" />
          <rect x="11" y="4" width="4" height="7" rx="1" fill="currentColor" />
        </svg>
      )
    case 'line':
      return (
        <svg {...common}>
          <path
            d="M2 13 6 8l3 3 7-8"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
        </svg>
      )
    case 'area':
      return (
        <svg {...common}>
          <path d="M2 13 6 8l3 3 7-8v13H2z" fill="currentColor" opacity="0.35" />
          <path
            d="M2 13 6 8l3 3 7-8"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
        </svg>
      )
  }
}
