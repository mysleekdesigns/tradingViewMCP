import { badgeText } from './alertsView'
import './SidebarRail.css'

export type SidebarPanel = 'watchlist' | 'alerts'

export interface SidebarRailProps {
  active: SidebarPanel | null
  onSelect(panel: SidebarPanel | null): void
  alertBadge: number
}

const PANELS: readonly { id: SidebarPanel; label: string }[] = [
  { id: 'watchlist', label: 'Watchlist' },
  { id: 'alerts', label: 'Alerts' },
]

/**
 * Far-right icon rail (TradingView style). Picks the sidebar panel; clicking the open panel's
 * button collapses the sidebar.
 */
export function SidebarRail({ active, onSelect, alertBadge }: SidebarRailProps) {
  const badge = badgeText(alertBadge)
  return (
    <nav className="side-rail" aria-label="Sidebar panels">
      {PANELS.map((p) => {
        const pressed = active === p.id
        const withBadge = p.id === 'alerts' && badge !== null
        return (
          <button
            key={p.id}
            type="button"
            className="side-rail__btn"
            aria-pressed={pressed}
            aria-label={withBadge ? `${p.label} (${badge})` : p.label}
            title={pressed ? `Hide ${p.label.toLowerCase()}` : p.label}
            onClick={() => onSelect(pressed ? null : p.id)}
          >
            {p.id === 'watchlist' ? <ListIcon /> : <AlarmIcon />}
            {withBadge && (
              <span className="side-rail__badge" aria-hidden="true">
                {badge}
              </span>
            )}
          </button>
        )
      })}
    </nav>
  )
}

const ICON = {
  width: 22,
  height: 22,
  viewBox: '0 0 22 22',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

function ListIcon() {
  return (
    <svg {...ICON}>
      <path d="M8 6h10M8 11h10M8 16h10" />
      <path d="M4 6h.01M4 11h.01M4 16h.01" strokeWidth="2.2" />
    </svg>
  )
}

function AlarmIcon() {
  return (
    <svg {...ICON}>
      <circle cx="11" cy="12" r="6.5" />
      <path d="M11 8.5V12l2.2 1.6M3.5 6 6 3.5M18.5 6 16 3.5M6.5 18.5 5.2 20M15.5 18.5l1.3 1.5" />
    </svg>
  )
}
