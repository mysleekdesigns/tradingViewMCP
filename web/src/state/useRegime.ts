import { useEffect, useRef, useState } from 'react'
import { regimeQueryKey, regimeRequestBars } from '../chart/regimeRibbon'
import { LabError, type LabRegimeClient } from '../data/labApi'
import type { RegimeHmmInfo, RegimeModel, RegimePoint } from '../data/labTypes'
import { isSecondsTimeframe, timeframeSeconds } from '../data/timeframes'
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

/** The lab's HMM floor (`MIN_HMM_INTERVAL_SECONDS` in lab/lab/plan/schema.py). */
export const HMM_MIN_BAR_SECONDS = 3_600
/** Timeframes from 1h up that the lab knows (`INTERVAL_SECONDS` in lab/lab/types.py). */
const HMM_TIMEFRAMES: ReadonlySet<string> = new Set(['1h', '2h', '4h', '6h', '8h', '12h', '1d'])

export const HMM_BELOW_1H_TEXT = 'HMM needs 1h bars or longer'
export const HMM_OTHER_TIMEFRAME_TEXT = 'HMM runs on 1h, 2h, 4h, 6h, 8h, 12h and 1d bars'

/**
 * Why the HMM source cannot run on `tf` (no HMM request is made there), or null when it can.
 * `blocked` is a reason that holds for every chart of the venue (`regimeHmmVenueBlocked`).
 */
export function hmmUnavailableReason(tf: Timeframe, blocked?: string | null): string | null {
  if (blocked !== undefined && blocked !== null) return blocked
  if (timeframeSeconds(tf) < HMM_MIN_BAR_SECONDS) return HMM_BELOW_1H_TEXT
  return HMM_TIMEFRAMES.has(tf) ? null : HMM_OTHER_TIMEFRAME_TEXT
}

/** The model that actually runs on `tf`: the saved source, or Markov where the HMM cannot. */
export function regimeModelFor(
  source: RegimeModel,
  tf: Timeframe,
  blocked?: string | null,
): RegimeModel {
  return source === 'hmm' && hmmUnavailableReason(tf, blocked) === null ? 'hmm' : 'markov'
}

/** The lab has fewer rows than a first decision takes: no ribbon, the chip says so. */
export function hmmHistoryShort(hmm: RegimeHmmInfo | null): boolean {
  return hmm !== null && hmm.bars_have < hmm.bars_needed
}

/** The answer holds no ribbon: too little history, or nothing was fitted (`unavailable`). */
export function hmmRibbonOff(hmm: RegimeHmmInfo | null): boolean {
  return hmm !== null && (hmm.unavailable !== null || hmmHistoryShort(hmm))
}

/** Wait between attempts when the lab is still busy with an HMM fit. */
export const REGIME_HMM_RETRY_MS = 20_000
/**
 * How long one key keeps asking, from its first request; after this the error shows. A time
 * budget, not a count of attempts: a busy answer takes about 2 s and a timeout 180 s, while the
 * fit being waited for can take minutes (about 460 s on a cold 1h series, by estimate).
 */
export const REGIME_HMM_MAX_WAIT_MS = 900_000

/** The fit is still running in the lab, so asking again soon is the way to get the answer. */
export function regimeRetryable(err: unknown): boolean {
  return err instanceof LabError && (err.kind === 'timeout' || err.kind === 'busy')
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
  /** Part of the request key; the HMM source also checks it (see `regimeModelFor`). */
  timeframe: Timeframe
  /** The chosen source. Default Markov; the HMM falls back to Markov where it cannot run. */
  source?: RegimeModel
  /** Why no chart of this venue can use the HMM (`regimeHmmVenueBlocked`), or null. */
  hmmBlocked?: string | null
  /**
   * Every loaded bar, ascending, no-trade bars included (see regimeSourceBars); the last one may
   * be forming.
   */
  candles: readonly Candle[]
  /** The last bar is still forming and is never sent. Default true (live data). */
  formingLast?: boolean
  /** Markov only. Lab default (20) when omitted. */
  windowBars?: number
  /** Markov only. Lab default (0.05) when omitted. */
  threshold?: number
  /** Bar length in seconds: requests are spaced at least one bar apart (see regimeRequestDelay). */
  barSeconds?: number
}

export interface RegimeOverlay {
  /** Labels for the closed bars, or null while there are none for this symbol/timeframe. */
  points: RegimePoint[] | null
  /** The fit behind an HMM answer; null on Markov and before the first HMM answer. */
  hmm: RegimeHmmInfo | null
  error: string | null
  loading: boolean
}

interface Result {
  key: string
  series: string
  points: RegimePoint[] | null
  hmm: RegimeHmmInfo | null
  error: string | null
}

const OFF: RegimeOverlay = { points: null, hmm: null, error: null, loading: false }

function errorMessage(err: unknown): string {
  if (err instanceof LabError) return err.message
  return err instanceof Error ? err.message : String(err)
}

/**
 * Regime labels for the chart's closed bars, from the lab (`POST /lab/regime`): hedgeFund's
 * Markov labels, or the causal HMM's when `source` is 'hmm'. The model is part of the series, so
 * switching source never shows the other model's labels.
 *
 * Asks again only when the closed bars change (a bar closes, older history is prepended, or the
 * last closed bar is corrected), never on live ticks of the forming bar; requests are debounced,
 * a bar close is spaced at least a bar after the previous request (capped at a minute), and a
 * newer request aborts the older. While a refresh runs, or after it fails, the previous labels
 * for the same symbol/timeframe/parameters stay: labels are never back-dated, so they still hold.
 * An HMM request that times out or finds the lab busy is the fit still running there: it is asked
 * again (REGIME_HMM_RETRY_MS, at most REGIME_HMM_MAX_ATTEMPTS per key) and stays `loading` until
 * the last attempt, whose failure is the error shown. A new key starts the count again.
 */
export function useRegime(client: LabRegimeClient, opts: UseRegimeOptions): RegimeOverlay {
  const { enabled, symbol, timeframe, candles, windowBars, threshold, barSeconds } = opts
  const formingLast = opts.formingLast ?? true
  const model = regimeModelFor(opts.source ?? 'markov', timeframe, opts.hmmBlocked)
  const key = enabled
    ? regimeQueryKey({ symbol, timeframe, candles, formingLast, windowBars, threshold, model })
    : null
  const series = [symbol, timeframe, windowBars ?? '', threshold ?? '', model].join('|')
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
    let firstAt: number | null = null
    const send = () => {
      firstAt ??= Date.now()
      const bars = regimeRequestBars(candlesRef.current, formingLast)
      const newest = bars.times.at(-1)
      if (newest !== undefined) lastRequestRef.current = { series, at: Date.now(), newest }
      client
        .regime(
          model === 'hmm'
            ? { ...bars, model, symbol, interval: timeframe }
            : {
                ...bars,
                ...(windowBars !== undefined ? { window_bars: windowBars } : {}),
                ...(threshold !== undefined ? { threshold } : {}),
              },
          controller.signal,
        )
        .then(
          (res) => {
            if (!controller.signal.aborted) {
              setResult({ key, series, points: res.points, hmm: res.hmm ?? null, error: null })
            }
          },
          (err: unknown) => {
            if (controller.signal.aborted) return
            const waited = Date.now() - (firstAt ?? Date.now())
            const inBudget = waited + REGIME_HMM_RETRY_MS <= REGIME_HMM_MAX_WAIT_MS
            if (model === 'hmm' && inBudget && regimeRetryable(err)) {
              timer = setTimeout(send, REGIME_HMM_RETRY_MS)
              return
            }
            setResult((prev) => ({
              key,
              series,
              points: prev?.series === series ? prev.points : null,
              hmm: prev?.series === series ? prev.hmm : null,
              error: errorMessage(err),
            }))
          },
        )
    }
    let timer = setTimeout(send, delay)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [
    client,
    key,
    series,
    model,
    symbol,
    timeframe,
    formingLast,
    windowBars,
    threshold,
    barSeconds,
  ])

  if (!enabled) return OFF
  const current = result !== null && result.series === series ? result : null
  return {
    points: current?.points ?? null,
    hmm: current?.hmm ?? null,
    error: current?.key === key ? current.error : null,
    loading: key !== null && current?.key !== key,
  }
}
