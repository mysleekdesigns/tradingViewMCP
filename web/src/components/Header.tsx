import type { ChartType, ConnectionStatus, Interval } from '../types'
import { ChartTypeMenu } from './ChartTypeMenu'
import { ConnectionIndicator } from './ConnectionStatus'
import './Indicators.css'
import type { Quote } from './quote'
import { TimeframeToolbar } from './TimeframeToolbar'

interface HeaderProps {
  symbol: string
  /** Pair description, e.g. "BTC / USDT"; empty until the symbol list loads. */
  description: string
  interval: Interval
  onIntervalChange: (interval: Interval) => void
  chartType: ChartType
  onChartTypeChange: (type: ChartType) => void
  /** Opens the "Indicators" picker. */
  onOpenIndicators: () => void
  /** Opens the symbol search dialog. */
  onOpenSearch: () => void
  watchlistOpen: boolean
  onToggleWatchlist: () => void
  source: string
  quote: Quote | null
  /** Live-stream state, shown as a small dot beside the ticker. */
  status: ConnectionStatus
  loading: boolean
  onRefresh: () => void
  refreshDisabled: boolean
}

export function Header({
  symbol,
  description,
  interval,
  onIntervalChange,
  chartType,
  onChartTypeChange,
  onOpenIndicators,
  onOpenSearch,
  watchlistOpen,
  onToggleWatchlist,
  source,
  quote,
  status,
  loading,
  onRefresh,
  refreshDisabled,
}: HeaderProps) {
  return (
    <header className="topbar">
      <div className="topbar__brand" role="img" aria-label="tradingView">
        <LogoMark />
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
          <svg
            className="symbol-btn__glass"
            width="14"
            height="14"
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
          <span className="topbar__ticker">{symbol}</span>
        </button>
        <ConnectionIndicator status={status} variant="compact" />
        {description !== '' && <span className="topbar__desc">{description}</span>}
      </div>

      <div className="topbar__divider" aria-hidden="true" />

      <TimeframeToolbar value={interval} onChange={onIntervalChange} />

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
        <IndicatorsIcon />
        <span className="ind-btn__label">Indicators</span>
      </button>

      <div className="topbar__divider" aria-hidden="true" />

      {/* No aria-live: once prices stream, it would announce every tick. */}
      <div className={`quote quote--${quote?.direction ?? 'flat'}`}>
        {quote ? (
          <>
            <span className="quote__price">{quote.price}</span>
            <span className="quote__change">
              {quote.change} ({quote.changePct})
            </span>
            <span className="quote__basis">{quote.basis}</span>
          </>
        ) : (
          <span className="quote__placeholder" aria-hidden="true" />
        )}
      </div>

      <div className="topbar__spacer" />

      <span className="source-badge" title="Public market data, no API key">
        <span className="source-badge__dot" aria-hidden="true" />
        {source}
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

      <button
        type="button"
        className="icon-btn icon-btn--toggle"
        aria-pressed={watchlistOpen}
        aria-label="Watchlist"
        title={watchlistOpen ? 'Hide watchlist' : 'Show watchlist'}
        onClick={onToggleWatchlist}
      >
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
      </button>
    </header>
  )
}

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
