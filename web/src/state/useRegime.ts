import { useEffect, useRef, useState } from 'react'
import { regimeQueryKey, regimeRequestBars } from '../chart/regimeRibbon'
import { LabError, type LabRegimeClient } from '../data/labApi'
import type { RegimePoint } from '../data/labTypes'
import { isSecondsTimeframe } from '../data/timeframes'
import type { Candle, Timeframe } from '../types'

/** Wait this long after the closed bars change before asking the lab (history pages land in bursts). */
export const REGIME_DEBOUNCE_MS = 250
/**
 * Whether the ribbon runs on `tf`. Not on seconds charts: a bar closes every second, so the lab
 * would be asked every second. Disabled there; no requests are made.
 */
export function regimeSupported(tf: Timeframe): boolean {
  return !isSecondsTimeframe(tf)
}

/** Longest spacing between two requests: one bar, capped at a minute for long bars. */
export const REGIME_MAX_GAP_MS = 60_000

/** The previous request of a series: when it was sent and the newest closed bar it covered. */
export interface RegimeLastRequest {
  /** Wall-clock ms. */
  at: number
  /** Open time of the newest closed bar sent (UNIX seconds). */
  newest: number
}

/**
 * Milliseconds to wait before the next request. Normally the debounce. When the newest closed
 * bar advanced since `last` (a bar closed), also at least one bar (capped at REGIME_MAX_GAP_MS)
 * after the previous request, so a chart asks the lab at most about once per bar. Any other
 * change (older history prepended, a corrected last close) and the first request after the
 * ribbon is (re-)enabled (`last` null) wait only the debounce.
 */
export function regimeRequestDelay(
  last: RegimeLastRequest | null,
  now: number,
  newest: number | null,
  barSeconds?: number,
): number {
  if (last === null || newest === null || !(newest > last.newest)) return REGIME_DEBOUNCE_MS
  const gap =
    barSeconds !== undefined && Number.isFinite(barSeconds) && barSeconds > 0
      ? Math.min(barSeconds * 1000, REGIME_MAX_GAP_MS)
      : 0
  return Math.max(REGIME_DEBOUNCE_MS, last.at + gap - now)
}

/** Open time of the newest closed bar, or null when none is closed. */
function newestClosedTime(candles: readonly Candle[], formingLast: boolean): number | null {
  const i = formingLast ? candles.length - 2 : candles.length - 1
  return i >= 0 ? candles[i].time : null
}

export interface UseRegimeOptions {
  enabled: boolean
  symbol: string
  /** A label only (e.g. '4h'); part of the request key, never parsed. */
  timeframe: string
  /**
   * Every loaded bar, ascending, no-trade bars included (see regimeSourceBars); the last one may
   * be forming.
   */
  candles: readonly Candle[]
  /** The last bar is still forming and is never sent. Default true (live data). */
  formingLast?: boolean
  /** Lab default (20) when omitted. */
  windowBars?: number
  /** Lab default (0.05) when omitted. */
  threshold?: number
  /** Bar length in seconds: requests are spaced at least one bar apart (see regimeRequestDelay). */
  barSeconds?: number
}

export interface RegimeOverlay {
  /** Labels for the closed bars, or null while there are none for this symbol/timeframe. */
  points: RegimePoint[] | null
  error: string | null
  loading: boolean
}

interface Result {
  key: string
  series: string
  points: RegimePoint[] | null
  error: string | null
}

const OFF: RegimeOverlay = { points: null, error: null, loading: false }

function errorMessage(err: unknown): string {
  if (err instanceof LabError) return err.message
  return err instanceof Error ? err.message : String(err)
}

/**
 * hedgeFund regime labels for the chart's closed bars, from the lab (`POST /lab/regime`).
 *
 * Asks again only when the closed bars change (a bar closes, older history is prepended, or the
 * last closed bar is corrected), never on live ticks of the forming bar; requests are debounced,
 * a bar close is spaced at least a bar after the previous request (capped at a minute), and a
 * newer request aborts the older. While a refresh runs, or after it fails, the previous labels
 * for the same symbol/timeframe/parameters stay: labels are never back-dated, so they still hold.
 */
export function useRegime(client: LabRegimeClient, opts: UseRegimeOptions): RegimeOverlay {
  const { enabled, symbol, timeframe, candles, windowBars, threshold, barSeconds } = opts
  const formingLast = opts.formingLast ?? true
  const key = enabled
    ? regimeQueryKey({ symbol, timeframe, candles, formingLast, windowBars, threshold })
    : null
  const series = [symbol, timeframe, windowBars ?? '', threshold ?? ''].join('|')
  const [result, setResult] = useState<Result | null>(null)
  const candlesRef = useRef(candles)
  /** The last request of this series (throttle); cleared while the ribbon is off. */
  const lastRequestRef = useRef<(RegimeLastRequest & { series: string }) | null>(null)

  useEffect(() => {
    candlesRef.current = candles
  }, [candles])

  useEffect(() => {
    if (key === null) {
      // Off (or nothing closed): the next request after re-enabling is not throttled.
      lastRequestRef.current = null
      return
    }
    const controller = new AbortController()
    const last = lastRequestRef.current
    const delay = regimeRequestDelay(
      last !== null && last.series === series ? last : null,
      Date.now(),
      newestClosedTime(candlesRef.current, formingLast),
      barSeconds,
    )
    const timer = setTimeout(() => {
      const bars = regimeRequestBars(candlesRef.current, formingLast)
      const newest = bars.times.at(-1)
      if (newest !== undefined) lastRequestRef.current = { series, at: Date.now(), newest }
      client
        .regime(
          {
            ...bars,
            ...(windowBars !== undefined ? { window_bars: windowBars } : {}),
            ...(threshold !== undefined ? { threshold } : {}),
          },
          controller.signal,
        )
        .then(
          (res) => {
            if (!controller.signal.aborted) {
              setResult({ key, series, points: res.points, error: null })
            }
          },
          (err: unknown) => {
            if (controller.signal.aborted) return
            setResult((prev) => ({
              key,
              series,
              points: prev?.series === series ? prev.points : null,
              error: errorMessage(err),
            }))
          },
        )
    }, delay)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [client, key, series, formingLast, windowBars, threshold, barSeconds])

  if (!enabled) return OFF
  const current = result !== null && result.series === series ? result : null
  return {
    points: current?.points ?? null,
    error: current?.key === key ? current.error : null,
    loading: key !== null && current?.key !== key,
  }
}
