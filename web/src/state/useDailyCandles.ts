import { useEffect, useState } from 'react'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle } from '../types'

const DAY_SECONDS = 86_400
const DAY_MS = DAY_SECONDS * 1000

/**
 * 1d bars asked for: far more than the SMA(50) + ATR(14) warm-up, and about 2.7 years, so older
 * intraday history scrolled into view still has levels. One request on Binance and the local
 * server (their cap is 1000); Coinbase pages it into 4 requests of 300 days.
 */
export const DAILY_CANDLE_LIMIT = 1000

/**
 * A 1d row counts as closed this long after its UTC day ends, and the refresh runs then: the
 * venue's last row needs a moment to take the day's final trades.
 */
export const DAILY_REFRESH_SETTLE_MS = 10_000

/**
 * Waits between tries of one load, while it fails or the last closed day is not there yet: 15 s
 * up to 5 min, 6 requests over almost 9 minutes. The last wait outlasts a venue's 5-minute CDN
 * cache (Coinbase candles).
 */
const RETRY_DELAYS_MS: readonly number[] = [15_000, 30_000, 60_000, 120_000, 300_000]

/**
 * Wait between tries after those, until the next refresh takes over: an outage longer than the
 * fast tries must not leave the chart without today's levels for the rest of the day.
 */
const SLOW_RETRY_MS = 1_800_000

/**
 * Longest single timer towards the next refresh. Timers can stall while a laptop sleeps, so a
 * long wait is split and each step checks `Date.now()`: a chart left open overnight refreshes
 * within this long of waking, not hours later.
 */
const REFRESH_CHECK_MS = 60_000

const MISSING_DAY_MESSAGE = 'The latest closed daily candle is not available yet'

/**
 * The closed candles of ascending `rows`: open + 86400 <= nowSeconds and a later row in the same
 * answer. The clock alone cannot say a row is final: a fast local clock passes a row that is still
 * forming, and an answer read just before the day ended ends on a row that has not taken its last
 * trades. A venue moves on to the next day's row only once a day is over.
 */
export function closedDailyCandles(rows: readonly Candle[], nowSeconds: number): Candle[] {
  return rows.slice(0, -1).filter((c) => c.time + DAY_SECONDS <= nowSeconds)
}

/**
 * Ms until the next UTC midnight plus DAILY_REFRESH_SETTLE_MS. Inside the settle window of a
 * midnight that has just passed, that midnight's refresh is the next one (not tomorrow's); exactly
 * at the refresh instant the next one is a full day away.
 */
export function msToNextDailyRefresh(nowMs: number): number {
  const sinceMidnight = ((nowMs % DAY_MS) + DAY_MS) % DAY_MS
  return sinceMidnight < DAILY_REFRESH_SETTLE_MS
    ? DAILY_REFRESH_SETTLE_MS - sinceMidnight
    : DAY_MS + DAILY_REFRESH_SETTLE_MS - sinceMidnight
}

export interface DailyCandlesState {
  /** Closed 1d candles of `symbol`, ascending, or null while none are known for it. */
  candles: readonly Candle[] | null
  error: string | null
  loading: boolean
}

type DailyProvider = Pick<MarketDataProvider, 'getCandles'>

interface Result {
  provider: DailyProvider
  symbol: string
  state: DailyCandlesState
}

const OFF: DailyCandlesState = { candles: null, error: null, loading: false }
const LOADING: DailyCandlesState = { candles: null, error: null, loading: true }

function sameCandles(a: readonly Candle[], b: readonly Candle[]): boolean {
  if (a.length !== b.length) return false
  return a.every((c, i) => {
    const o = b[i]
    return (
      c.time === o.time &&
      c.open === o.open &&
      c.high === o.high &&
      c.low === o.low &&
      c.close === o.close &&
      c.volume === o.volume
    )
  })
}

/** Open time of the newest candle, or -Infinity when there is none. */
function newestTime(candles: readonly Candle[]): number {
  return candles.at(-1)?.time ?? Number.NEGATIVE_INFINITY
}

/** Ms before try `tries + 1`. */
function retryDelay(tries: number): number {
  return RETRY_DELAYS_MS[tries] ?? SLOW_RETRY_MS
}

/**
 * Ms before try `tries + 1` after a failure, or null when this load gives up. A ban, an unknown
 * symbol and a region block do not pass within minutes. A venue's Retry-After is respected.
 */
function failureRetryDelay(err: unknown, tries: number): number | null {
  const wait = retryDelay(tries)
  if (!(err instanceof MarketDataError)) return wait
  if (err.kind === 'banned' || err.kind === 'bad-symbol' || err.kind === 'region-blocked') {
    return null
  }
  return Math.max(wait, (err.retryAfter ?? 0) * 1000)
}

/**
 * Keeps one market's closed 1d candles up to date until the returned stop function runs. At most
 * one request or timer exists at a time, because each next step is armed by the one before it.
 * `publish` is called only when the state changes; nothing is called after stop.
 */
function watchDailyCandles(
  provider: DailyProvider,
  symbol: string,
  publish: (state: DailyCandlesState) => void,
): () => void {
  const controller = new AbortController()
  const { signal } = controller
  let timer: ReturnType<typeof setTimeout> | undefined
  let candles: readonly Candle[] | null = null
  let error: string | null = null
  let published = false

  function update(nextCandles: readonly Candle[] | null, nextError: string | null): void {
    if (published && nextCandles === candles && nextError === error) return
    candles = nextCandles
    error = nextError
    published = true
    publish({ candles, error, loading: false })
  }

  function waitUntil(at: number): void {
    timer = setTimeout(
      () => {
        if (Date.now() >= at) load(0)
        else waitUntil(at)
      },
      Math.min(at - Date.now(), REFRESH_CHECK_MS),
    )
  }

  /**
   * `sent`: when the load that ended here was sent. Counted from then, not from its answer: a
   * refresh instant that passed while the answer travelled still runs, at once.
   */
  function waitForNextDay(sent: number): void {
    waitUntil(sent + msToNextDailyRefresh(sent))
  }

  function retry(wait: number, tries: number): void {
    const now = Date.now()
    const toRefresh = msToNextDailyRefresh(now)
    // A slow try never delays a new day's refresh, which starts over with the fast tries.
    if (tries >= RETRY_DELAYS_MS.length && toRefresh <= wait) waitUntil(now + toRefresh)
    else timer = setTimeout(() => load(tries + 1), wait)
  }

  function load(tries: number): void {
    // Taken before the request: a row that ends while the answer travels was read while it was
    // still forming, so it must not pass as closed.
    const sent = Date.now()
    const cutoff = Math.floor((sent - DAILY_REFRESH_SETTLE_MS) / 1000)
    const lastClosedDay = Math.floor(cutoff / DAY_SECONDS) * DAY_SECONDS - DAY_SECONDS
    provider.getCandles(symbol, '1d', DAILY_CANDLE_LIMIT, undefined, { signal }).then(
      (rows) => {
        if (signal.aborted) return
        const closed = closedDailyCandles(rows, cutoff)
        // An answer older than what is known (a stale copy, an empty body) never takes days away.
        const stale = candles !== null && newestTime(closed) < newestTime(candles)
        const same = candles !== null && sameCandles(closed, candles)
        const next = (stale || same) && candles !== null ? candles : closed
        if (newestTime(next) >= lastClosedDay) {
          update(next, null)
          waitForNextDay(sent)
          return
        }
        update(next, tries >= RETRY_DELAYS_MS.length ? MISSING_DAY_MESSAGE : null)
        retry(retryDelay(tries), tries)
      },
      (err: unknown) => {
        if (signal.aborted) return
        update(candles, err instanceof Error ? err.message : String(err))
        const wait = failureRetryDelay(err, tries)
        if (wait === null) waitForNextDay(sent)
        else retry(wait, tries)
      },
    )
  }

  load(0)
  return () => {
    controller.abort()
    if (timer !== undefined) clearTimeout(timer)
  }
}

/**
 * Closed 1d candles for an intraday chart, through `provider.getCandles` only, so in server mode
 * they come from the server's cache. The forming 1d row is never exposed: a row counts only once
 * its day ended DAILY_REFRESH_SETTLE_MS before the request was sent and the answer holds a later
 * row, so a quiet venue that has no row for the new day yet shows as a late day, and retries.
 *
 * Asks again at each UTC midnight plus the settle delay. A failed refresh keeps the last good
 * candles and sets `error`; a refresh that brings the same closed candles keeps the same array.
 * Every load is retried quickly while it fails or the last closed UTC day is missing, then every
 * 30 minutes until the next refresh; a day still missing after the quick tries sets `error` beside
 * the candles and leaves a gap for the consumer. `candles` is [] when the venue answered but no
 * day has closed.
 *
 * The answer belongs to one provider and symbol: a different one reads as loading in that same
 * render, and leaving a market or disabling drops its candles.
 */
export function useDailyCandles(
  provider: DailyProvider,
  symbol: string,
  enabled: boolean,
): DailyCandlesState {
  const [result, setResult] = useState<Result | null>(null)

  useEffect(() => {
    if (!enabled) return
    const stop = watchDailyCandles(provider, symbol, (state) => {
      setResult({ provider, symbol, state })
    })
    return () => {
      stop()
      setResult(null)
    }
  }, [provider, symbol, enabled])

  if (!enabled) return OFF
  if (result === null || result.provider !== provider || result.symbol !== symbol) return LOADING
  return result.state
}
