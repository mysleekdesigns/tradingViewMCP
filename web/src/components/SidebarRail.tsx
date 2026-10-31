import { memo } from 'react'
import { badgeText } from './alertsView'
import { ResearchIcon, TesterIcon } from './icons'
import './SidebarRail.css'

export type SidebarPanel = 'watchlist' | 'alerts' | 'paper'

export interface SidebarRailProps {
  active: SidebarPanel | null
  onSelect(panel: SidebarPanel | null): void
  alertBadge: number
  /** The Strategy Tester dock (under the chart) is open. Independent of the sidebar panel. */
  testerOpen: boolean
  onToggleTester(): void
  /** The Research view (over the chart area) is open. */
  researchOpen: boolean
  onToggleResearch(): void
}

const PANELS: readonly { id: SidebarPanel; label: string }[] = [
  { id: 'watchlist', label: 'Watchlist' },
  { id: 'alerts', label: 'Alerts' },
  { id: 'paper', label: 'Paper trading' },
]

/**
 * Paper trading, the Strategy Tester and Research load on first open (`lazyPanels.tsx`).
 * Pointing at or focusing their rail button starts that download, so the click usually finds the
 * code ready. Same specifiers as `lazyPanels.tsx`: the module cache shares one request.
 */
const PRELOAD = {
  paper: () => import('./PaperTradingPanel'),
  tester: () => import('./BacktestPanel'),
  research: () => import('./ResearchPage'),
} as const

type PreloadTarget = keyof typeof PRELOAD

/**
 * Hover (mouse, pen, touch) and keyboard focus both come before the click. Only a head start, so
 * a failure is swallowed: it surfaces, and is handled, when the panel really opens. Calling
 * `import()` directly (not the lazyPanels loaders) keeps it speculative: main.tsx reloads the page
 * after a failed chunk only while a real open is in flight (`panelLoadInFlight`), never for this.
 */
function preloadHandlers(target: PreloadTarget) {
  const start = () => {
    PRELOAD[target]().catch(() => {})
  }
  return { onPointerEnter: start, onFocus: start }
}

const PRELOAD_HANDLERS: Record<PreloadTarget, ReturnType<typeof preloadHandlers>> = {
  paper: preloadHandlers('paper'),
  tester: preloadHandlers('tester'),
  research: preloadHandlers('research'),
}

/**
 * Far-right icon rail (TradingView style). Picks the sidebar panel; clicking the open panel's
 * button collapses the sidebar.
 */
export const SidebarRail = memo(function SidebarRail({
  active,
  onSelect,
  alertBadge,
  testerOpen,
  onToggleTester,
  researchOpen,
  onToggleResearch,
}: SidebarRailProps) {
  'use memo'
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
            {...(p.id === 'paper' ? PRELOAD_HANDLERS.paper : null)}
          >
            {p.id === 'watchlist' ? (
              <ListIcon />
            ) : p.id === 'alerts' ? (
              <AlarmIcon />
            ) : (
              <PaperIcon />
            )}
            {withBadge && (
              <span className="side-rail__badge" aria-hidden="true">
                {badge}
              </span>
            )}
          </button>
        )
      })}
      <span className="side-rail__divider" aria-hidden="true" />
      <button
        type="button"
        className="side-rail__btn"
        aria-pressed={testerOpen}
        aria-label="Strategy Tester"
        title={testerOpen ? 'Hide Strategy Tester' : 'Strategy Tester (backtest a plan)'}
        onClick={onToggleTester}
        {...PRELOAD_HANDLERS.tester}
      >
        <TesterIcon size={22} />
      </button>
      <button
        type="button"
        className="side-rail__btn"
        aria-pressed={researchOpen}
        aria-label="Research"
        title={researchOpen ? 'Back to chart' : 'Research (runs, ideas, journal, candidates)'}
        onClick={onToggleResearch}
        {...PRELOAD_HANDLERS.research}
      >
        <ResearchIcon size={22} />
      </button>
    </nav>
  )
})

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

/** A ticket with a buy/sell arrow pair: simulated orders. */
function PaperIcon() {
  return (
    <svg {...ICON}>
      <rect x="3.5" y="4.5" width="15" height="13" rx="1.5" />
      <path d="M8 14.5v-6M5.8 10.7 8 8.5l2.2 2.2M14 8.5v6M11.8 12.3l2.2 2.2 2.2-2.2" />
    </svg>
  )
}
