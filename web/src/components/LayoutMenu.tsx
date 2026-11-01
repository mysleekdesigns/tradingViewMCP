import { memo, useId, type KeyboardEvent } from 'react'
import { LAYOUT_KINDS, type LayoutKind } from '../state/layout'
import { roveFocus, useMenu } from './useMenu'
import './Layout.css'

const LAYOUT_LABELS: Record<LayoutKind, string> = {
  '1': 'One chart',
  '2': 'Two charts, side by side',
  '4': 'Four charts, 2 × 2',
}

const LAYOUT_SHORT: Record<LayoutKind, string> = {
  '1': '1',
  '2': '2',
  '4': '2×2',
}

/** Focus the current layout tile so arrows start from it. */
function focusCheckedTile(menu: HTMLElement): void {
  menu.querySelector<HTMLElement>('[aria-checked="true"]')?.focus()
}

interface LayoutMenuProps {
  kind: LayoutKind
  onKindChange: (kind: LayoutKind) => void
  syncSymbol: boolean
  onSyncSymbolChange: (on: boolean) => void
  syncCrosshair: boolean
  onSyncCrosshairChange: (on: boolean) => void
}

/** Toolbar button + menu: layout tiles (1 / 2 / 2×2) and the symbol / crosshair sync toggles. */
export const LayoutMenu = memo(function LayoutMenu({
  kind,
  onKindChange,
  syncSymbol,
  onSyncSymbolChange,
  syncCrosshair,
  onSyncCrosshairChange,
}: LayoutMenuProps) {
  const { open, rootRef, buttonRef, menuRef, openMenu, close, toggle, onMenuKey } =
    useMenu<HTMLDivElement>({ focusOnOpen: focusCheckedTile })
  const menuId = useId()

  const pickKind = (next: LayoutKind) => {
    close(true)
    if (next !== kind) onKindChange(next)
  }

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      openMenu()
    }
  }

  // Roving focus over every item, in reading order.
  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[data-menu-item]') ?? [],
    )
    if (!roveFocus(e, items, document.activeElement, true)) onMenuKey(e)
  }

  return (
    <div className="layout-menu" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="icon-btn layout-menu__button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Chart layout: ${LAYOUT_LABELS[kind]}`}
        title="Chart layout and sync"
        onClick={toggle}
        onKeyDown={onButtonKeyDown}
      >
        <LayoutGlyph kind={kind} size={18} />
      </button>
      {open && (
        <div
          ref={menuRef}
          id={menuId}
          className="layout-menu__panel"
          role="menu"
          aria-label="Chart layout"
          onKeyDown={onMenuKeyDown}
        >
          <div className="layout-menu__heading" aria-hidden="true">
            Layout
          </div>
          <div className="layout-menu__tiles" role="group" aria-label="Layout">
            {LAYOUT_KINDS.map((k) => (
              <button
                key={k}
                type="button"
                role="menuitemradio"
                aria-checked={k === kind}
                aria-label={LAYOUT_LABELS[k]}
                title={LAYOUT_LABELS[k]}
                tabIndex={k === kind ? 0 : -1}
                data-menu-item=""
                className="layout-menu__tile"
                onClick={() => pickKind(k)}
              >
                <LayoutGlyph kind={k} size={30} />
                <span className="layout-menu__tile-label" aria-hidden="true">
                  {LAYOUT_SHORT[k]}
                </span>
              </button>
            ))}
          </div>
          <div className="layout-menu__sep" role="separator" />
          <div className="layout-menu__heading" aria-hidden="true">
            Sync between charts
          </div>
          <SyncToggle
            label="Symbol"
            hint="Changing the active chart's symbol changes every chart"
            on={syncSymbol}
            onChange={onSyncSymbolChange}
          />
          <SyncToggle
            label="Crosshair"
            hint="Hovering a chart marks the same time on the others"
            on={syncCrosshair}
            onChange={onSyncCrosshairChange}
          />
          {kind === '1' && (
            <p className="layout-menu__note">Sync applies when two or four charts are shown.</p>
          )}
        </div>
      )}
    </div>
  )
})

interface SyncToggleProps {
  label: string
  hint: string
  on: boolean
  onChange: (on: boolean) => void
}

function SyncToggle({ label, hint, on, onChange }: SyncToggleProps) {
  return (
    <button
      type="button"
      role="menuitemcheckbox"
      aria-checked={on}
      tabIndex={-1}
      data-menu-item=""
      className="layout-menu__toggle"
      onClick={() => onChange(!on)}
    >
      <span className="layout-menu__toggle-text">
        <span className="layout-menu__toggle-label">{label}</span>
        <span className="layout-menu__toggle-hint">{hint}</span>
      </span>
      <span className={`layout-switch${on ? ' layout-switch--on' : ''}`} aria-hidden="true">
        <span className="layout-switch__knob" />
      </span>
    </button>
  )
}

/** The layout as a miniature grid of panes. */
export function LayoutGlyph({ kind, size }: { kind: LayoutKind; size: number }) {
  const cells =
    kind === '1'
      ? [[2.5, 2.5, 13, 13]]
      : kind === '2'
        ? [
            [2.5, 2.5, 6, 13],
            [9.5, 2.5, 6, 13],
          ]
        : [
            [2.5, 2.5, 6, 6],
            [9.5, 2.5, 6, 6],
            [2.5, 9.5, 6, 6],
            [9.5, 9.5, 6, 6],
          ]
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden="true">
      {cells.map(([x, y, w, h], i) => (
        <rect
          key={i}
          x={x}
          y={y}
          width={w}
          height={h}
          rx="1.2"
          fill="none"
          stroke="currentColor"
          strokeWidth={size > 20 ? 0.9 : 1.3}
        />
      ))}
    </svg>
  )
}
