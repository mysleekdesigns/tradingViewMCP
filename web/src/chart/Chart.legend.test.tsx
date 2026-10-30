import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { isSecondsTimeframe, timeframeLabel } from '../data/timeframes'
import type { Candle, Timeframe } from '../types'
import { Legend } from './Chart'
import { buildLegend, formatAsOfDate, type LegendValues } from './legend.ts'
import { formatDateTime, timezoneLabel } from './timeFormat'

// Phase 19 (B5): the main legend memoises its formatted bar time. These pin the markup to the
// pre-memo JSX (copied verbatim below) so the change stays invisible.

function Field({ label, value }: { label: string; value: string }) {
  return (
    <span className="chart-legend__field">
      <span className="chart-legend__label">{label}</span>
      <span className="chart-legend__value">{value}</span>
    </span>
  )
}

/** The legend as it rendered before the memo (reference copy). */
function ReferenceLegend({
  symbol,
  interval,
  source,
  values,
  timezone,
  children,
}: {
  symbol: string
  interval: Timeframe
  source: string
  values: LegendValues | null
  timezone: string
  children?: ReactNode
}) {
  return (
    <div className="chart-legend" aria-live="off">
      <div className="chart-legend__row">
        <span className="chart-legend__title">
          {symbol} · {timeframeLabel(interval)} · {source}
        </span>
        {values && (
          <span className={`chart-legend__values chart-legend__values--${values.direction}`}>
            <Field label="O" value={values.open} />
            <Field label="H" value={values.high} />
            <Field label="L" value={values.low} />
            <Field label="C" value={values.close} />
            <span className="chart-legend__change">{values.change}</span>
          </span>
        )}
      </div>
      {values && (
        <div className="chart-legend__row chart-legend__row--secondary">
          <Field label="Vol" value={values.volume} />
          <span className="chart-legend__time">
            {formatDateTime(values.time, timezone, { seconds: isSecondsTimeframe(interval) })}{' '}
            {timezoneLabel(timezone, values.time)}
          </span>
        </div>
      )}
      {children}
    </div>
  )
}

const candles: Candle[] = [
  { time: 1_700_000_000, open: 100, high: 102, low: 99, close: 101, volume: 1234 },
  { time: 1_700_000_017, open: 101, high: 103, low: 98, close: 99.5, volume: 4_560_000 },
]

describe('main chart legend markup', () => {
  it('matches the pre-memo legend for every zone, timeframe and value state', () => {
    const intervals: Timeframe[] = ['1s', '1m', '4h', '1d']
    const zones = ['UTC', 'local', 'America/New_York', 'Asia/Tokyo', 'Not/AZone']
    const states = [null, buildLegend(candles, 0), buildLegend(candles, 1)]
    for (const interval of intervals) {
      for (const timezone of zones) {
        for (const values of states) {
          const props = { symbol: 'BTCUSDT', interval, source: 'Binance', values, timezone }
          const child = <span className="child">rows</span>
          expect(renderToStaticMarkup(<Legend {...props}>{child}</Legend>)).toBe(
            renderToStaticMarkup(<ReferenceLegend {...props}>{child}</ReferenceLegend>),
          )
        }
      }
    }
  })
})

describe('formatAsOfDate', () => {
  it('equals the per-call toLocaleDateString it replaces', () => {
    let t = 1_500_000_000
    for (let i = 0; i < 400; i++) {
      // ~3.3 days apart, odd offsets: covers month ends, leap days and the UTC midnight edge.
      t += 287_000 + (i % 13) * 1_111
      const want = new Date(t * 1000).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        timeZone: 'UTC',
      })
      expect(formatAsOfDate(t)).toBe(want)
    }
    expect(formatAsOfDate(Date.UTC(2024, 1, 29, 23, 59, 59) / 1000)).toBe('Feb 29')
    expect(formatAsOfDate(Date.UTC(2024, 2, 1) / 1000)).toBe('Mar 1')
  })
})
