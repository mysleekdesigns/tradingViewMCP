import { memo, type ReactNode } from 'react'
import { shortcutHint } from '../state/appSettings'
import type { ThemeName } from '../state/settings'
import type { ChartType, ConnectionStatus, Timeframe } from '../types'
import './AppShell.css'
import { ChartTypeMenu } from './ChartTypeMenu'
import { ConnectionIndicator } from './ConnectionStatus'
import './Indicators.css'
import type { Quote } from './quote'
import { QuoteView } from './QuoteReadout'
import { TimeframePicker } from './TimeframePicker'

interface HeaderProps {
  symbol: string
  /** Pair description, e.g. "BTC / USDT"; empty until the symbol list loads. */
  description: string
  interval: Timeframe
  onIntervalChange: (interval: Timeframe) => void
  /** False for timeframes the data source cannot serve (e.g. seconds on Coinbase). */
  supportsTimeframe?: (tf: Timeframe) => boolean
  chartType: ChartType
  onChartTypeChange: (type: ChartType) => void
  /** Opens the "Indicators" picker. */
  onOpenIndicators: () => void
  /** Opens the symbol search dialog. */
  onOpenSearch: () => void
  /** Opens the "Create alert" dialog at the last price. */
  onOpenAlert: () => void
  /** Why alerts cannot be created right now (shown as the tooltip), or null when they can. */
  alertDisabledReason: string | null
  /** A bar replay session is open on the active chart. */
  replayOn: boolean
  /** Starts replay on the active chart (pick a start bar), or leaves it. */
  onToggleReplay: () => void
  /** Why replay cannot start right now (tooltip), or null when it can. */
  replayDisabledReason: string | null
  /** The hedgeFund regime ribbon is shown under the candles. */
  regimeOn: boolean
  onToggleRegime: () => void
  /** Historical funding / open-interest panes are shown under the charts (from the lab). */
  derivativesOn?: boolean
  onToggleDerivatives?: () => void
  watchlistOpen: boolean
  onToggleWatchlist: () => void
  source: string
  /** The quote to show; ignored when `quoteSlot` is given. */
  quote?: Quote | null
  /**
   * The quote element in its place (e.g. a QuoteReadout that reads the live store itself, so a
   * tick re-renders only that leaf). Pass a stable element; it wins over `quote`.
   */
  quoteSlot?: ReactNode
  /** Live-stream state, shown as a small dot beside the ticker. */
  status: ConnectionStatus
  loading: boolean
  onRefresh: () => void
  refreshDisabled: boolean
  /** Opens the Seasonals view (monthly / weekday returns) for the current symbol. */
  onOpenSeasonals: () => void
  /** Saves the chart as a PNG. */
  onScreenshot: () => void
  theme: ThemeName
  onToggleTheme: () => void
  onOpenShortcuts: () => void
  onOpenSettings: () => void
  /** Shortcut hints in macOS notation (⌥S) or Alt+S. */
  isMac: boolean
  /** The multi-chart layout picker, first in the chart tools cluster. */
  layoutControl?: ReactNode
}

export const Header = memo(function Header({
  symbol,
  description,
  interval,
  onIntervalChange,
  supportsTimeframe,
  chartType,
  onChartTypeChange,
  onOpenIndicators,
  onOpenSearch,
  onOpenAlert,
  alertDisabledReason,
  replayOn,
  onToggleReplay,
  replayDisabledReason,
  regimeOn,
  onToggleRegime,
  derivativesOn = false,
  onToggleDerivatives,
  watchlistOpen,
  onToggleWatchlist,
  source,
  quote,
  quoteSlot,
  status,
  loading,
  onRefresh,
  refreshDisabled,
  onOpenSeasonals,
  onScreenshot,
  theme,
  onToggleTheme,
  onOpenShortcuts,
  onOpenSettings,
  isMac,
  layoutControl,
}: HeaderProps) {
  const nextTheme = theme === 'dark' ? 'light' : 'dark'
  // "Binance · local server": the transport part can hide on narrow windows (the tooltip keeps it).
  const split = source.indexOf(' · ')
  const sourceVenue = split < 0 ? source : source.slice(0, split)
  const sourceTransport = split < 0 ? '' : source.slice(split)
  return (
    <header className="topbar">
      <div className="topbar__brand" role="img" aria-label="tradingView">
        {LOGO_MARK}
      </div>

      <div className="topbar__divider" aria-hidden="true" />

      <div className="topbar__symbol">
        <button
          type="button"
          className="symbol-btn"
          onClick={onOpenSearch}
          aria-haspopup="dialog"
          aria-label={`Symbol ${symbol}. Change symbol`}
          title="Change symbol (Ctrl/Cmd+K, or just start typing)"
        >
          {SEARCH_GLASS}
          <span className="topbar__ticker">{symbol}</span>
        </button>
        <ConnectionIndicator status={status} variant="compact" />
        {description !== '' && <span className="topbar__desc">{description}</span>}
      </div>

      <div className="topbar__divider" aria-hidden="true" />

      <TimeframePicker value={interval} onChange={onIntervalChange} supports={supportsTimeframe} />

      <div className="topbar__divider" aria-hidden="true" />

      <ChartTypeMenu value={chartType} onChange={onChartTypeChange} />

      <button
        type="button"
        className="ind-btn"
        onClick={onOpenIndicators}
        aria-haspopup="dialog"
        aria-label="Indicators"
        title="Indicators (/)"
      >
        {INDICATORS_ICON}
        <span className="ind-btn__label">Indicators</span>
      </button>

      <button
        type="button"
        className="ind-btn"
        // aria-disabled keeps the tooltip (and focus) working while alerts are unavailable.
        onClick={alertDisabledReason === null ? onOpenAlert : undefined}
        aria-disabled={alertDisabledReason !== null}
        aria-haspopup="dialog"
        aria-label="Create alert"
        title={alertDisabledReason ?? 'Create alert (Alt+A)'}
      >
        {BELL_ICON}
        <span className="ind-btn__label">Alert</span>
      </button>

      <button
        type="button"
        className="ind-btn ind-btn--replay"
        // Off and unavailable: aria-disabled keeps the tooltip explaining why.
        onClick={replayOn || replayDisabledReason === null ? onToggleReplay : undefined}
        aria-disabled={!replayOn && replayDisabledReason !== null}
        aria-pressed={replayOn}
        aria-label="Bar replay"
        title={
          replayOn
            ? 'Exit bar replay and return to live (Esc)'
            : (replayDisabledReason ??
              'Bar replay: click a bar to replay history from it, bar by bar (Shift+→ step, Shift+↓ play)')
        }
      >
        {REPLAY_ICON}
        <span className="ind-btn__label">Replay</span>
      </button>

      <button
        type="button"
        className="ind-btn ind-btn--secondary"
        onClick={onToggleRegime}
        aria-pressed={regimeOn}
        aria-label="Regime ribbon"
        title={
          regimeOn
            ? 'Hide the regime ribbon'
            : 'Regime ribbon: hedgeFund bull / sideways / bear label of each closed bar (needs the lab)'
        }
      >
        {REGIME_ICON}
        <span className="ind-btn__label">Regime</span>
      </button>

      {onToggleDerivatives && (
        <button
          type="button"
          className="ind-btn ind-btn--secondary"
          onClick={onToggleDerivatives}
          aria-pressed={derivativesOn}
          aria-label="Funding and open interest panes"
          title={
            derivativesOn
              ? 'Hide the funding and open-interest panes'
              : "Funding / OI: historical funding rate and open interest panes from Binance's futures archive (needs the lab; no live stream)"
          }
        >
          {DERIVATIVES_ICON}
          <span className="ind-btn__label">Funding / OI</span>
        </button>
      )}

      <button
        type="button"
        className="ind-btn ind-btn--secondary"
        onClick={onOpenSeasonals}
        aria-haspopup="dialog"
        aria-label="Seasonals"
        title={`Seasonals: monthly and weekday returns of ${symbol}`}
      >
        {CALENDAR_ICON}
        <span className="ind-btn__label">Seasonals</span>
      </button>

      <div className="topbar__divider" aria-hidden="true" />

      {quoteSlot !== undefined ? quoteSlot : <QuoteView quote={quote ?? null} />}

      <div className="topbar__spacer" />

      <span className="source-badge" title={`${source}: public market data, no API key`}>
        <span className="source-badge__dot" aria-hidden="true" />
        {sourceVenue}
        {sourceTransport !== '' && (
          <span className="source-badge__transport">{sourceTransport}</span>
        )}
      </span>

      <button
        type="button"
        className="btn btn--ghost"
        // aria-disabled instead of disabled keeps keyboard focus on the button while loading.
        onClick={refreshDisabled || loading ? undefined : onRefresh}
        aria-disabled={refreshDisabled || loading}
        title="Reload recent candles and fill any gap"
      >
        <RefreshIcon spinning={loading} />
        <span>Refresh</span>
      </button>

      <div className="topbar__divider" aria-hidden="true" />

      <div className="topbar__tools" role="group" aria-label="Chart tools">
        {layoutControl}
        <button
          type="button"
          className="icon-btn"
          aria-label="Save chart image"
          title={`Save chart image as PNG (${shortcutHint(isMac, 'screenshot')})`}
          onClick={onScreenshot}
        >
          {CAMERA_ICON}
        </button>
        <button
          type="button"
          className="icon-btn"
          aria-label={`Switch to ${nextTheme} theme`}
          title={`Switch to ${nextTheme} theme (${shortcutHint(isMac, 'theme')})`}
          onClick={onToggleTheme}
        >
          {theme === 'dark' ? SUN_ICON : MOON_ICON}
        </button>
        <button
          type="button"
          className="icon-btn"
          aria-haspopup="dialog"
          aria-label="Keyboard shortcuts"
          title={`Keyboard shortcuts (${shortcutHint(isMac, 'help')})`}
          onClick={onOpenShortcuts}
        >
          {KEYBOARD_ICON}
        </button>
        <button
          type="button"
          className="icon-btn"
          aria-haspopup="dialog"
          aria-label="Settings"
          title="Settings: theme, colours, data provider, time zone"
          onClick={onOpenSettings}
        >
          {GEAR_ICON}
        </button>
      </div>

      <div className="topbar__divider" aria-hidden="true" />

      <button
        type="button"
        className="icon-btn icon-btn--toggle"
        aria-pressed={watchlistOpen}
        aria-label="Watchlist"
        title={watchlistOpen ? 'Hide watchlist' : 'Show watchlist'}
        onClick={onToggleWatchlist}
      >
        {WATCHLIST_GLYPH}
      </button>
    </header>
  )
})

// Static icon elements, created once: the Header re-renders on every quote tick, and reusing the
// same element objects lets React skip these subtrees instead of re-running each icon function.
// (The function declarations below are hoisted, so referencing them here is safe.)
const LOGO_MARK = <LogoMark />
const INDICATORS_ICON = <IndicatorsIcon />
const BELL_ICON = <BellIcon />
const REPLAY_ICON = <ReplayIcon />
const REGIME_ICON = <RegimeIcon />
const DERIVATIVES_ICON = <DerivativesIcon />
const CALENDAR_ICON = <CalendarIcon />
const CAMERA_ICON = <CameraIcon />
const SUN_ICON = <SunIcon />
const MOON_ICON = <MoonIcon />
const KEYBOARD_ICON = <KeyboardIcon />
const GEAR_ICON = <GearIcon />

const SEARCH_GLASS = (
  <svg className="symbol-btn__glass" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
    <circle cx="7" cy="7" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
    <path d="M10.5 10.5 14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
)
const WATCHLIST_GLYPH = (
  <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
    <rect
      x="2.5"
      y="3"
      width="13"
      height="12"
      rx="1.5"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
    />
    <path
      d="M11 3v12M12.8 6.5h1.2M12.8 9h1.2M12.8 11.5h1.2"
      stroke="currentColor"
      strokeWidth="1.3"
    />
  </svg>
)

function LogoMark() {
  // Three candles: a compact, recognisable mark for a charting app.
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <rect x="3" y="7" width="4" height="9" rx="1" fill="var(--down)" />
      <rect x="4.5" y="4" width="1" height="15" fill="var(--down)" />
      <rect x="9" y="5" width="4" height="8" rx="1" fill="var(--up)" />
      <rect x="10.5" y="2" width="1" height="14" fill="var(--up)" />
      <rect x="15" y="9" width="4" height="9" rx="1" fill="var(--accent)" />
      <rect x="16.5" y="6" width="1" height="14" fill="var(--accent)" />
    </svg>
  )
}

function IndicatorsIcon() {
  // "fx": TradingView's indicators glyph.
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M9.6 3.6c-.9-.6-2.4-.3-2.8 1.2L4.9 14.9c-.3 1.5-1.6 1.9-2.6 1.3M3.9 8h4.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path
        d="M11 8.5l5.5 6.5M16.5 8.5 11 15"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

function BellIcon() {
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M10 3.2c-2.6 0-4.4 2-4.4 4.6v2.9L4.2 13.4h11.6l-1.4-2.7V7.8c0-2.6-1.8-4.6-4.4-4.6Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path
        d="M8.1 15.6a2 2 0 0 0 3.8 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

function ReplayIcon() {
  // Rewind to a bar, then play forward: TradingView's replay glyph.
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M3.5 4.5v11"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path d="M10 5.2 5.6 10 10 14.8Z" fill="currentColor" />
      <path d="M16.4 5.2 12 10l4.4 4.8Z" fill="currentColor" />
    </svg>
  )
}

function RegimeIcon() {
  // A price line over a three-colour strip.
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M3 11.5 7 8l3 2.2 4.2-5 2.8 2.3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect x="3" y="14.2" width="4.4" height="2.4" rx=".5" fill="var(--down)" />
      <rect x="7.8" y="14.2" width="4.4" height="2.4" rx=".5" fill="currentColor" opacity=".45" />
      <rect x="12.6" y="14.2" width="4.4" height="2.4" rx=".5" fill="var(--up)" />
    </svg>
  )
}

function DerivativesIcon() {
  // Funding bars either side of a zero line, with the open-interest line above.
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <path
        d="M3 7.5 7 5.5l3.5 1.5L17 4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M3 13.5h14" stroke="currentColor" strokeWidth="1" opacity=".45" />
      <rect x="4" y="10" width="2.2" height="3.5" rx=".4" fill="var(--up)" />
      <rect x="7.6" y="11" width="2.2" height="2.5" rx=".4" fill="var(--up)" />
      <rect x="11.2" y="13.5" width="2.2" height="2.5" rx=".4" fill="var(--down)" />
      <rect x="14.8" y="11.8" width="2.2" height="1.7" rx=".4" fill="var(--up)" />
    </svg>
  )
}

function RefreshIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      className={spinning ? 'icon icon--spin' : 'icon'}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <path
        d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89M13.5 2.5v3h-3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function CalendarIcon() {
  return (
    <svg className="ind-btn__icon" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <rect
        x="3.5"
        y="4.5"
        width="13"
        height="12"
        rx="1.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path
        d="M3.5 8.2h13M7 3v3M13 3v3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <rect x="6" y="10.4" width="2.4" height="2.2" rx=".4" fill="var(--up)" />
      <rect x="9.3" y="10.4" width="2.4" height="2.2" rx=".4" fill="var(--down)" />
      <rect x="12.6" y="10.4" width="2.4" height="2.2" rx=".4" fill="currentColor" opacity=".45" />
      <rect x="6" y="13.3" width="2.4" height="2.2" rx=".4" fill="currentColor" opacity=".45" />
      <rect x="9.3" y="13.3" width="2.4" height="2.2" rx=".4" fill="var(--up)" />
    </svg>
  )
}

function CameraIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M2.5 6.2c0-.8.6-1.4 1.4-1.4h1.9l1.2-1.8h4l1.2 1.8h1.9c.8 0 1.4.6 1.4 1.4v7.3c0 .8-.6 1.4-1.4 1.4H3.9c-.8 0-1.4-.6-1.4-1.4V6.2Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <circle cx="9" cy="9.7" r="2.7" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  )
}

function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <circle cx="9" cy="9" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M9 1.8v1.8M9 14.4v1.8M1.8 9h1.8M14.4 9h1.8M3.9 3.9l1.3 1.3M12.8 12.8l1.3 1.3M3.9 14.1l1.3-1.3M12.8 5.2l1.3-1.3"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M14.8 11.2A6 6 0 0 1 6.8 3.2a6 6 0 1 0 8 8Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function KeyboardIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <rect
        x="1.8"
        y="4.5"
        width="14.4"
        height="9"
        rx="1.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path
        d="M4.6 7.3h.1M7.1 7.3h.1M9.6 7.3h.1M12.1 7.3h.1M4.6 9.6h.1M13.4 9.6h.1M6.4 11.4h5.2"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M7.6 2.2h2.8l.4 1.9 1.3.7 1.8-.7 1.4 2.4-1.5 1.3v1.4l1.5 1.3-1.4 2.4-1.8-.7-1.3.7-.4 1.9H7.6l-.4-1.9-1.3-.7-1.8.7-1.4-2.4 1.5-1.3V7.8L2.7 6.5l1.4-2.4 1.8.7 1.3-.7.4-1.9Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <circle cx="9" cy="9" r="2.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  )
}
