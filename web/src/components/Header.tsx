import type { ConnectionStatus, Interval } from '../types'
import { ConnectionIndicator } from './ConnectionStatus'
import type { Quote } from './quote'

interface HeaderProps {
  symbol: string
  /** Human name of the pair, e.g. "Bitcoin / TetherUS". */
  description: string
  interval: Interval
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
        <span className="topbar__ticker">{symbol}</span>
        <ConnectionIndicator status={status} variant="compact" />
        <span className="topbar__desc">{description}</span>
      </div>

      <span className="topbar__interval" title="Timeframe">
        {interval}
      </span>

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
