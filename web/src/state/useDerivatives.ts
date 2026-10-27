import { useEffect, useRef, useState } from 'react'
import type { DerivativesOverlay } from '../chart/derivativesPanes'
import { MAX_REGIME_BARS } from '../chart/regimeRibbon'
import { LabError, type LabDerivativesClient, type LabErrorKind } from '../data/labApi'
import type { DerivativesResponse } from '../data/labTypes'
import { isSecondsTimeframe, timeframeSeconds } from '../data/timeframes'
import type { Candle, Timeframe } from '../types'
import { regimeRequestDelay, type RegimeLastRequest } from './useRegime'

/** The lab's request limit (the same cap as `/lab/regime`, MAX_REGIME_BARS in lab/lab/api.py). */
export const MAX_DERIVATIVES_BARS = MAX_REGIME_BARS
/** `bar_seconds` the lab accepts: 1 minute .. 1 week. */
export const DERIVATIVES_MIN_BAR_SECONDS = 60
export const DERIVATIVES_MAX_BAR_SECONDS = 604_800
/**
 * Binance symbols (BTCUSDT): upper-case letters and digits only. Other venues' symbols
 * (Coinbase BTC-USD) have no USDⓈ-M archive.
 */
const BINANCE_SYMBOL = /^[A-Z0-9]{2,30}$/

/**
 * Why the funding / OI panes are off on this chart (no requests are made), or null when they
 * are supported. Off on seconds charts (a bar closes every second), on bars the lab does not
 * accept (longer than a week, e.g. 2w; months do not exist here) and on non-Binance symbols.
 */
export function derivativesUnsupportedReason(tf: Timeframe, symbol: string): string | null {
  let seconds: number
  try {
    if (isSecondsTimeframe(tf)) return 'Funding / OI: not on seconds timeframes'
    seconds = timeframeSeconds(tf)
  } catch {
    return 'Funding / OI: not on this timeframe'
  }
  if (seconds < DERIVATIVES_MIN_BAR_SECONDS || seconds > DERIVATIVES_MAX_BAR_SECONDS) {
    return 'Funding / OI: up to 1W bars only'
  }
  if (!BINANCE_SYMBOL.test(symbol)) return 'Funding / OI: Binance symbols only'
  return null
}

/** The closed bars a request would cover: oldest and newest open time (UNIX seconds). */
export interface DerivativesWindow {
  first: number
  newest: number
}

/**
 * Oldest and newest of the newest MAX_DERIVATIVES_BARS closed bars (the forming last bar is
 * never sent), or null when none is closed. O(1).
 */
export function derivativesWindow(
  candles: readonly Candle[],
  formingLast: boolean,
): DerivativesWindow | null {
  const n = formingLast ? candles.length - 1 : candles.length
  if (n <= 0) return null
  return { first: candles[Math.max(0, n - MAX_DERIVATIVES_BARS)].time, newest: candles[n - 1].time }
}

/** Open times of the newest MAX_DERIVATIVES_BARS closed bars, ascending. */
export function derivativesRequestTimes(
  candles: readonly Candle[],
  formingLast: boolean,
): number[] {
  const n = formingLast ? Math.max(0, candles.length - 1) : candles.length
  const out: number[] = []
  for (let i = Math.max(0, n - MAX_DERIVATIVES_BARS); i < n; i++) out.push(candles[i].time)
  return out
}

/**
 * True when the newest bar of the answer is already past the archive for both series: both
 * values are null and its time is after each series' last archived timestamp (or the series has
 * nothing downloaded). Every later bar is further past the archive, so it would get null too:
 * new closing bars need no request (the panes simply have no point there, drawn like
 * whitespace).
 */
export function newestBeyondArchive(res: DerivativesResponse): boolean {
  const f = res.funding.at(-1)
  const o = res.open_interest.at(-1)
  if (f === undefined || o === undefined) return false
  if (f.value !== null || o.value !== null) return false
  const fLast = res.available.funding.last ?? Number.NEGATIVE_INFINITY
  const oLast = res.available.open_interest.last ?? Number.NEGATIVE_INFINITY
  return f.time > fLast && o.time > oLast
}

/** What a finished request covered (see derivativesCovers). */
export interface DerivativesCoverage extends DerivativesWindow {
  /** The newest bar sent was already past the archive (newestBeyondArchive). */
  beyondArchive: boolean
  /**
   * The lab answered with an error that new bars cannot fix (503 no data downloaded, 422):
   * asked again only when older history is prepended, the market changes, or the panes are
   * switched off and on.
   */
  sticky: boolean
  /**
   * The request failed in a way that may pass (lab offline, timeout, HTTP error, bad body).
   * Such an answer covers nothing: the hook retries it on a backoff (derivativesRetryDelay).
   */
  failed: boolean
}

/**
 * The cheap refetch rule. A finished request still covers the chart's closed bars when it did
 * not fail transiently (`failed`), no older history was prepended (`first` did not move back;
 * it may move forward when the 20 000-bar cap slides or bar replay steps back) and either no
 * newer bar closed, or the newest bar sent was already past the archive (later bars can only be
 * null), or the answer was a sticky error. Otherwise the bars are sent again (throttled to
 * about once per bar).
 */
export function derivativesCovers(
  coverage: DerivativesCoverage | null,
  win: DerivativesWindow,
): boolean {
  if (coverage === null || coverage.failed) return false
  if (win.first < coverage.first) return false
  return win.newest <= coverage.newest || coverage.beyondArchive || coverage.sticky
}

/** Request identity: the market and the closed-bar window sent. */
export function derivativesRequestKey(series: string, win: DerivativesWindow): string {
  return `${series}|${win.first}|${win.newest}`
}

/**
 * What the request effect is keyed on, or null when nothing is to be sent (off, or no closed
 * bar). The last answer's own key while it still covers `win` (derivativesCovers), so live
 * ticks, bar replay inside the answered range (entering replay, stepping back, or stepping
 * forward up to the newest bar answered) and the forming bar flipping to closed keep the same
 * key: no request, and an in-flight request is not aborted. A different key aborts the older
 * request and sends the current window. Whether the last bar is forming is deliberately not
 * part of the trigger: it only shapes `win`.
 */
export function derivativesTrigger(
  series: string,
  win: DerivativesWindow | null,
  last: (DerivativesCoverage & { requestKey: string }) | null,
): string | null {
  if (win === null) return null
  return last !== null && derivativesCovers(last, win)
    ? last.requestKey
    : derivativesRequestKey(series, win)
}

/** Backoff before retrying a transient failure: 15 s, 30 s, 60 s, then no more retries. */
export const DERIVATIVES_RETRY_MS: readonly number[] = [15_000, 30_000, 60_000]

/**
 * Delay before retry number `failures` (1 after the first failure) of the same request, or null
 * once the retries are spent: then nothing is asked until the trigger changes (a bar closes,
 * older history loads, the market changes) or the panes are switched off and on.
 */
export function derivativesRetryDelay(failures: number): number | null {
  if (!Number.isInteger(failures) || failures < 1) return null
  return DERIVATIVES_RETRY_MS[failures - 1] ?? null
}

/** Errors that a new bar cannot fix (see DerivativesCoverage.sticky). */
export function isStickyDerivativesError(kind: LabErrorKind | null): boolean {
  return kind === 'no-data' || kind === 'invalid'
}

/** A command quoted in backticks inside a lab message (e.g. the archive download CLI), or null. */
export function quotedCommand(message: string): string | null {
  const m = /`([^`]+)`/.exec(message)
  return m === null ? null : m[1]
}

/** Newest archived timestamp over both series (UNIX seconds), or null when neither has data. */
export function derivativesArchiveEnd(
  available: DerivativesResponse['available'] | null,
): number | null {
  if (available === null) return null
  const lasts = [available.funding.last, available.open_interest.last].filter(
    (t): t is number => t !== null,
  )
  return lasts.length === 0 ? null : Math.max(...lasts)
}

export interface UseDerivativesOptions {
  /** On, supported here (derivativesUnsupportedReason) and the chart has bars. */
  enabled: boolean
  symbol: string
  /** A label only (e.g. '4h'); part of the request key. */
  timeframe: string
  /** Bar length in seconds (sent as `bar_seconds`; also spaces requests). */
  barSeconds: number
  /** Every loaded bar, ascending; the last one may be forming (see `formingLast`). */
  candles: readonly Candle[]
  /** The last bar is still forming and is never sent. Default true (live data). */
  formingLast?: boolean
}

export interface DerivativesState {
  /** Values for the closed bars sent, or null while there are none for this market. */
  overlay: DerivativesOverlay | null
  /** Archive range of each series from the last answer, or null. */
  available: DerivativesResponse['available'] | null
  error: string | null
  errorKind: LabErrorKind | null
  loading: boolean
}

interface Result extends DerivativesCoverage {
  series: string
  requestKey: string
  overlay: DerivativesOverlay | null
  available: DerivativesResponse['available'] | null
  error: string | null
  errorKind: LabErrorKind | null
}

const OFF: DerivativesState = {
  overlay: null,
  available: null,
  error: null,
  errorKind: null,
  loading: false,
}

function errorOf(err: unknown): { message: string; kind: LabErrorKind | null } {
  if (err instanceof LabError) return { message: err.message, kind: err.kind }
  return { message: err instanceof Error ? err.message : String(err), kind: null }
}

/**
 * Historical funding and open interest for the chart's closed bars, from the lab
 * (`POST /lab/derivatives`, the data.binance.vision USDⓈ-M archive; no live stream).
 *
 * Asks again only when `derivativesCovers` says the last answer no longer covers the closed
 * bars: older history prepended, the market or timeframe changed, or a bar closed while the
 * archive still reached the newest bar sent. Since the archive lags real time, a live chart
 * normally asks once per market. Requests are debounced and spaced like the regime ribbon's,
 * a newer request aborts the older, and while a refresh runs (or after a transient failure)
 * the previous values for the same market stay. A transient failure (lab offline, timeout) is
 * retried after 15 s, 30 s and 60 s, then left until the trigger changes. Switching the panes
 * off and on asks again (e.g. after downloading more of the archive).
 */
export function useDerivatives(
  client: LabDerivativesClient,
  opts: UseDerivativesOptions,
): DerivativesState {
  const { enabled, symbol, timeframe, barSeconds, candles } = opts
  const formingLast = opts.formingLast ?? true
  const series = `${symbol}|${timeframe}|${barSeconds}`
  const [result, setResult] = useState<Result | null>(null)
  const candlesRef = useRef(candles)
  // Read at send time only: entering or leaving replay flips it, and that alone must not
  // re-send (the trigger already says whether the answer covers the new window).
  const formingLastRef = useRef(formingLast)
  const lastRequestRef = useRef<(RegimeLastRequest & { series: string }) | null>(null)

  const current = result !== null && result.series === series ? result : null
  const win = enabled ? derivativesWindow(candles, formingLast) : null
  const key = derivativesTrigger(series, win, current)

  useEffect(() => {
    candlesRef.current = candles
    formingLastRef.current = formingLast
  }, [candles, formingLast])

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
      derivativesWindow(candlesRef.current, formingLastRef.current)?.newest ?? null,
      barSeconds,
    )
    let failures = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const send = () => {
      timer = undefined
      const times = derivativesRequestTimes(candlesRef.current, formingLastRef.current)
      const first = times[0]
      const newest = times.at(-1)
      if (first === undefined || newest === undefined) return
      lastRequestRef.current = { series, at: Date.now(), newest }
      client.fetchDerivatives({ symbol, times, bar_seconds: barSeconds }, controller.signal).then(
        (res) => {
          if (controller.signal.aborted) return
          setResult({
            series,
            requestKey: key,
            first,
            newest,
            beyondArchive: newestBeyondArchive(res),
            sticky: false,
            failed: false,
            overlay: { funding: res.funding, openInterest: res.open_interest },
            available: res.available,
            error: null,
            errorKind: null,
          })
        },
        (err: unknown) => {
          if (controller.signal.aborted) return
          const { message, kind } = errorOf(err)
          const sticky = isStickyDerivativesError(kind)
          setResult((prev) => {
            const keep = !sticky && prev?.series === series
            return {
              series,
              requestKey: key,
              first,
              newest,
              beyondArchive: false,
              sticky,
              failed: !sticky,
              overlay: keep ? prev.overlay : null,
              available: keep ? prev.available : null,
              error: message,
              errorKind: kind,
            }
          })
          if (sticky) return
          // The failed answer covers nothing, but its key is the trigger for the same window,
          // so this effect stays mounted and owns the retries (cleared on any trigger change).
          failures += 1
          const retry = derivativesRetryDelay(failures)
          if (retry !== null) timer = setTimeout(send, retry)
        },
      )
    }
    timer = setTimeout(send, delay)
    return () => {
      if (timer !== undefined) clearTimeout(timer)
      controller.abort()
    }
  }, [client, key, series, symbol, barSeconds])

  if (!enabled) return OFF
  const settled = current !== null && current.requestKey === key
  return {
    overlay: current?.overlay ?? null,
    available: current?.available ?? null,
    error: settled ? current.error : null,
    errorKind: settled ? current.errorKind : null,
    loading: key !== null && !settled,
  }
}
