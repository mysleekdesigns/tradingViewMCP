import { INTERVALS, type Interval } from '../types'

/** Toolbar labels; days and weeks use TradingView's capitals. */
const INTERVAL_LABELS: Record<Interval, string> = {
  '1m': '1m',
  '5m': '5m',
  '15m': '15m',
  '1h': '1h',
  '4h': '4h',
  '1d': '1D',
  '1w': '1W',
}

const INTERVAL_NAMES: Record<Interval, string> = {
  '1m': '1 minute',
  '5m': '5 minutes',
  '15m': '15 minutes',
  '1h': '1 hour',
  '4h': '4 hours',
  '1d': '1 day',
  '1w': '1 week',
}

interface TimeframeToolbarProps {
  value: Interval
  onChange: (interval: Interval) => void
}

export function TimeframeToolbar({ value, onChange }: TimeframeToolbarProps) {
  return (
    <div className="tf-toolbar" role="group" aria-label="Timeframe">
      {INTERVALS.map((iv) => (
        <button
          key={iv}
          type="button"
          className="tf-toolbar__btn"
          aria-pressed={iv === value}
          aria-label={INTERVAL_NAMES[iv]}
          title={INTERVAL_NAMES[iv]}
          onClick={() => {
            if (iv !== value) onChange(iv)
          }}
        >
          {INTERVAL_LABELS[iv]}
        </button>
      ))}
    </div>
  )
}
