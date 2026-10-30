// Any Timeframe on top of a MarketDataProvider: native Intervals pass straight through; seconds
// and custom timeframes are aggregated from the finest native source that tiles them
// (`baseFor`): Binance `1s` klines for seconds, a native Interval for the rest.
import type { Candle, Interval, Timeframe } from '../types'
import { RunningBucket, aggregateCandles } from './aggregate'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from './provider'
import {
  baseFor,
  bucketStart,
  isNativeInterval,
  isSecondsTimeframe,
  parseTimeframe,
  timeframeLabel,
  timeframeSeconds,
} from './timeframes'

export interface CandleSubscribeOptions extends SubscribeOptions {
  /**
   * Bar `time` was not streamed in full and will never be emitted (or closed): re-fetch it
   * over REST. Called at most once per bar, after the bar has ended, and never after
   * unsubscribe. Only aggregated timeframes call it; native Intervals never do.
   */
  onRepair?: (time: number) => void
}

/** The subset of a provider the live-candle controller needs, keyed by Timeframe. */
export interface CandleSource {
  getCandles(
    symbol: string,
    tf: Timeframe,
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]>
  /**
   * Same contract as MarketDataProvider.subscribeCandles, `provisional` included: a closed
   * bar sent with it will be sent closed once more (without it) and is not final until then.
   */
  subscribeCandles(
    symbol: string,
    tf: Timeframe,
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: CandleSubscribeOptions,
  ): () => void
}

export interface TimeframeProvider extends CandleSource {
  /** True when `tf` is canonical (see parseTimeframe) and this provider can serve it. */
  supports(tf: Timeframe): boolean
}

/**
 * One-second klines (Binance only). Same contract as MarketDataProvider.getCandles /
 * subscribeCandles with the interval fixed to '1s': ascending unique UNIX-second times, the
 * last REST row may be forming, `endTime` in UNIX seconds, at most 1000 rows per call,
 * reconnect/status handling inside `subscribeCandles`. Errors are MarketDataErrors.
 */
export interface SecondsSource {
  getCandles(
    symbol: string,
    interval: '1s',
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]>
  subscribeCandles(
    symbol: string,
    interval: '1s',
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void
}

/** Rows per base klines request (Binance's maximum). */
export const BASE_PAGE = 1000
/**
 * Most base requests one getCandles call makes: 1 for the newest page, then up to 14 older
 * pages fetched in parallel. 15 pages of 1s bars = 4h10m, e.g. 500 bars of 30s; coarser
 * ratios than that return fewer than `limit` bars (always the newest), and loadOlder pages on.
 */
export const MAX_BASE_PAGES = 15
/**
 * Delays (ms) before each fast retry of a failed live-bucket seed. After the last one fails a
 * forming bucket falls back to slow retries (see seedSlowRetryMs).
 */
export const SEED_RETRY_DELAYS_MS: readonly number[] = [2_000, 4_000, 8_000, 16_000]
/** Fast seed requests per bucket per connection: the first try plus one per retry delay. */
export const SEED_MAX_ATTEMPTS = SEED_RETRY_DELAYS_MS.length + 1
/** Longest slow-retry interval (ms) for a forming bucket past SEED_MAX_ATTEMPTS. */
export const SEED_SLOW_RETRY_MAX_MS = 300_000
/**
 * Seconds every request through the provider is held after a 429 without a usable
 * Retry-After.
 */
export const SEED_RATE_LIMIT_WAIT_S = 60
/** Seconds every request is held after a 418 (IP ban) without a usable Retry-After. */
export const SEED_BAN_WAIT_S = 300

/**
 * Slow-retry interval (ms) for a forming `tfSec`-second bucket whose fast seeds all failed:
 * half a bucket, at most SEED_SLOW_RETRY_MAX_MS (a 12h or 3D bucket: every 5 minutes).
 */
export function seedSlowRetryMs(tfSec: number): number {
  return Math.min((tfSec * 1000) / 2, SEED_SLOW_RETRY_MAX_MS)
}

const DEFAULT_LIMIT = 500

/** A native source of base bars: one Interval of `base`, or the seconds source. */
interface BaseFeed {
  seconds: number
  getCandles(
    symbol: string,
    limit: number,
    endTime: number | undefined,
    signal: AbortSignal | undefined,
  ): Promise<Candle[]>
  subscribe(
    symbol: string,
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts: SubscribeOptions,
  ): () => void
}

function clampLimit(limit: number): number {
  if (!Number.isFinite(limit)) return DEFAULT_LIMIT
  return Math.min(BASE_PAGE, Math.max(1, Math.floor(limit)))
}

function unsupported(tf: Timeframe): MarketDataError {
  return new MarketDataError(
    'http',
    `${timeframeLabel(tf)} candles are not available from this provider`,
  )
}

/**
 * Wrap `base` so it serves every Timeframe. Seconds timeframes need `opts.seconds` (Binance's
 * 1s klines; Coinbase and the local server have none), otherwise `supports` is false for them
 * and getCandles/subscribeCandles reject/throw a MarketDataError.
 *
 * getCandles(symbol, tf, limit, endTime): `limit` and `endTime` are in target bars, like the
 * native call: up to `limit` (max 1000) `tf` bars, ascending, each opening at or before
 * `endTime`, the last possibly forming. It requests the newest base page ending at the end of
 * `endTime`'s bucket, then (in parallel) as many older pages as `limit` needs, up to
 * MAX_BASE_PAGES in total, and drops the oldest bucket when the pages start inside it (unless
 * history itself starts there).
 *
 * subscribeCandles: subscribes to the base stream and emits the aggregated forming bucket on
 * each base update. A bucket the stream did not see from its first base bar (the one forming
 * at subscribe time or after a reconnect) is first seeded over REST; until the seed lands
 * nothing is emitted for it. `closed: true` is emitted only for a bucket built from all its
 * base bars: when its last base bar closes, or, failing that, when the first base bar of a
 * later bucket arrives (after re-seeding the bucket over REST if the stream dropped during
 * it). A base tick more than one base bar after the newest one seen (bars skipped without a
 * reported drop) is handled like a drop: open buckets are re-seeded before they close. A
 * bucket that closes while one of its base bars is a provisional close (`provisional`, see
 * MarketDataProvider.subscribeCandles) is emitted closed and provisional, again only if its
 * values change, and closed without the flag once the base source settles that bar; a
 * bucket is emitted closed without the flag at most once. Status reports pass through
 * unchanged.
 *
 * Seed retries are bounded in rate. A failed seed of a forming bucket is retried after
 * SEED_RETRY_DELAYS_MS (SEED_MAX_ATTEMPTS requests), then every seedSlowRetryMs(tf) for as
 * long as the bucket is forming, so a long bucket (12h, 3D) entered mid-way still goes live
 * after a REST outage. Until a seed lands nothing is emitted for it: the chart keeps that bar
 * as of its last REST load while the status stays 'live' (there is no staleness signal).
 * A 429 or 418 only holds seeds until the shared gate reopens (below); they then resume.
 * A superseded (ended) bucket gets one seed try per connection, no retries. An ended bucket
 * that was never emitted closed is handed to `opts.onRepair(start)` so the caller re-fetches
 * it over REST: when an ended bucket's seed fails, and when a bucket still unseeded ends after
 * a failed seed. A bucket emitted with `closed: true` never gets it.
 *
 * Rate limits are shared: any 429/418 (from a seed or from getCandles, native intervals
 * included) blocks every request through this provider until its Retry-After (or
 * SEED_RATE_LIMIT_WAIT_S / SEED_BAN_WAIT_S) has passed. While blocked, getCandles rejects at
 * once, without a request, with a MarketDataError of the same kind whose `retryAfter` is the
 * remaining wait (whole seconds, at least 1); seeds wait. A call whose signal is already
 * aborted is passed through so it rejects as 'aborted' (it makes no request either).
 */
export function createTimeframeProvider(
  base: MarketDataProvider,
  opts?: { seconds?: SecondsSource | null },
): TimeframeProvider {
  const seconds = opts?.seconds ?? null
  /** Shared by every request of this provider: none before `until` (ms). */
  const gate: RequestGate = { until: 0, kind: 'rate-limited' }

  /** Run `request` unless the gate is closed; a 429/418 it gets closes the gate. */
  const gated = (request: () => Promise<Candle[]>, signal?: AbortSignal): Promise<Candle[]> => {
    if (signal?.aborted !== true) {
      const blocked = gateError(gate)
      if (blocked !== null) return Promise.reject(blocked)
    }
    let p: Promise<Candle[]>
    try {
      p = request()
    } catch (err) {
      p = Promise.reject(err)
    }
    return p.catch((err: unknown) => {
      closeGate(gate, err)
      throw err
    })
  }

  const feedFor = (tf: Timeframe): BaseFeed => {
    const iv = baseFor(tf)
    if (iv === '1s') {
      if (seconds === null) throw unsupported(tf)
      return {
        seconds: 1,
        getCandles: (symbol, limit, endTime, signal) =>
          gated(() => seconds.getCandles(symbol, '1s', limit, endTime, { signal }), signal),
        subscribe: (symbol, onCandle, o) => seconds.subscribeCandles(symbol, '1s', onCandle, o),
      }
    }
    const interval: Interval = iv
    return {
      seconds: timeframeSeconds(interval),
      getCandles: (symbol, limit, endTime, signal) =>
        gated(() => base.getCandles(symbol, interval, limit, endTime, { signal }), signal),
      subscribe: (symbol, onCandle, o) => base.subscribeCandles(symbol, interval, onCandle, o),
    }
  }

  const supports = (tf: Timeframe): boolean => {
    if (typeof tf !== 'string' || parseTimeframe(tf) !== tf) return false
    return !isSecondsTimeframe(tf) || seconds !== null
  }

  return {
    supports,

    async getCandles(symbol, tf, limit = DEFAULT_LIMIT, endTime, o) {
      if (isNativeInterval(tf)) {
        return gated(() => base.getCandles(symbol, tf, limit, endTime, o), o?.signal)
      }
      if (!supports(tf)) throw unsupported(tf)
      const feed = feedFor(tf)
      const tfSec = timeframeSeconds(tf)
      if (tfSec === feed.seconds) return feed.getCandles(symbol, limit, endTime, o?.signal)
      return fetchAggregated(feed, symbol, tf, clampLimit(limit), endTime, o?.signal)
    },

    subscribeCandles(symbol, tf, onCandle, o) {
      if (isNativeInterval(tf)) return base.subscribeCandles(symbol, tf, onCandle, o)
      if (!supports(tf)) throw unsupported(tf)
      const feed = feedFor(tf)
      if (timeframeSeconds(tf) === feed.seconds) {
        return feed.subscribe(symbol, onCandle, { onStatus: o?.onStatus })
      }
      return subscribeAggregated(feed, symbol, tf, onCandle, o, gate)
    },
  }
}

async function fetchAggregated(
  feed: BaseFeed,
  symbol: string,
  tf: Timeframe,
  limit: number,
  endTime: number | undefined,
  signal: AbortSignal | undefined,
): Promise<Candle[]> {
  const tfSec = timeframeSeconds(tf)
  // The bucket that holds endTime is returned whole, as Binance does for a native bar.
  const end = endTime === undefined ? undefined : bucketStart(endTime, tf) + tfSec - 1
  const first = await feed.getCandles(symbol, BASE_PAGE, end, signal)
  if (first.length === 0) return []

  let rows = first
  let oldest = first[0].time
  // A short page means history starts inside it: nothing older exists.
  let exhausted = first.length < BASE_PAGE
  const wantFrom = bucketStart(first[first.length - 1].time, tf) - (limit - 1) * tfSec
  if (!exhausted && oldest > wantFrom) {
    // Each page returns the BASE_PAGE newest rows at or before its endTime, so it covers at
    // least BASE_PAGE base intervals back from it: pages spaced that far apart leave no hole
    // (gaps without rows only make them overlap; duplicates are merged).
    const span = BASE_PAGE * feed.seconds
    const n = Math.min(MAX_BASE_PAGES - 1, Math.ceil((oldest - wantFrom) / span))
    const ends = Array.from({ length: n }, (_, k) => oldest - 1 - k * span)
    const pages = await Promise.all(ends.map((e) => feed.getCandles(symbol, BASE_PAGE, e, signal)))
    for (const page of pages) {
      if (page.length < BASE_PAGE) exhausted = true
      if (page.length > 0 && page[0].time < oldest) oldest = page[0].time
    }
    rows = [...pages.flat(), ...first]
  }
  // Rows start at `oldest`; a bucket that opens before it would be partial. Keep the
  // oldest bucket only when history itself starts there.
  const startBucket = bucketStart(oldest, tf)
  const from = exhausted || startBucket === oldest ? startBucket : startBucket + tfSec
  const inRange = rows.filter((c) => c.time >= from && (end === undefined || c.time <= end))
  return aggregateCandles(inRange, tf).slice(-limit)
}

interface RequestGate {
  /** Wall-clock ms (Date.now) before which no request is made. */
  until: number
  /** What closed the gate (a ban outranks a rate limit). */
  kind: 'rate-limited' | 'banned'
}

/** The error a request gets while the gate is closed, or null when it is open. */
function gateError(gate: RequestGate): MarketDataError | null {
  const remaining = gate.until - Date.now()
  if (remaining <= 0) return null
  const wait = Math.max(1, Math.ceil(remaining / 1000))
  const banned = gate.kind === 'banned'
  return new MarketDataError(
    gate.kind,
    `${banned ? 'IP banned' : 'Rate limited'}: no requests for ${wait}s`,
    banned ? 418 : 429,
    wait,
  )
}

/** Close the gate after a 429/418 (never shortening it); other errors leave it alone. */
function closeGate(gate: RequestGate, err: unknown): void {
  const holdS = holdSeconds(err)
  if (holdS <= 0 || !(err instanceof MarketDataError)) return
  const kind = err.kind === 'banned' ? 'banned' : 'rate-limited'
  const until = Date.now() + holdS * 1000
  if (until > gate.until) {
    gate.until = until
    gate.kind = kind
  } else if (kind === 'banned') gate.kind = 'banned'
}

/** Seconds to hold requests after `err`, or 0 when it is not a rate limit or ban. */
function holdSeconds(err: unknown): number {
  if (!(err instanceof MarketDataError)) return 0
  if (err.kind !== 'rate-limited' && err.kind !== 'banned') return 0
  const ra = err.retryAfter
  if (ra !== undefined && Number.isFinite(ra) && ra > 0) return ra
  return err.kind === 'banned' ? SEED_BAN_WAIT_S : SEED_RATE_LIMIT_WAIT_S
}

/** Flags of one base bar of a bucket (its values live in BucketState.ohlcv). */
interface BaseBar {
  closed: boolean
  /** Closed, but the base source will send it closed once more (it may still change). */
  provisional: boolean
}

interface BucketState {
  start: number
  bars: Map<number, BaseBar>
  /** Running OHLCV of the same base bars as `bars` (set together, never one without the other). */
  ohlcv: RunningBucket
  /** `bars` holds every base bar since `start` (streamed from it or seeded) with no drop since. */
  complete: boolean
  /** In-flight REST seed. */
  seeding: AbortController | null
  /** Pending retry of a failed seed, or a seed waiting out a rate-limit hold. */
  retryTimer: ReturnType<typeof setTimeout> | null
  /** Seed requests made since subscribe or the last drop. */
  attempts: number
  /** A later bucket has started: this one is final. */
  superseded: boolean
  /** `closed: true` was emitted without the provisional flag; nothing more is emitted. */
  done: boolean
  /**
   * The bucket was last emitted closed and provisional, with these values (a provisional base
   * bar is not settled yet). It is emitted again only when its values or flag change.
   */
  provisionalSent: Candle | null
}

function sameCandle(a: Candle, b: Candle): boolean {
  return (
    a.time === b.time &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume
  )
}

/** Record base bar `c` of `st` (replacing an earlier send of the same time). */
function setBar(st: BucketState, c: Candle, closed: boolean, provisional: boolean): void {
  const b = st.bars.get(c.time)
  if (b === undefined) st.bars.set(c.time, { closed, provisional })
  else {
    b.closed = closed
    b.provisional = provisional
  }
  st.ohlcv.set(c)
}

function subscribeAggregated(
  feed: BaseFeed,
  symbol: string,
  tf: Timeframe,
  onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
  opts: CandleSubscribeOptions | undefined,
  gate: RequestGate,
): () => void {
  const tfSec = timeframeSeconds(tf)
  const ratio = tfSec / feed.seconds
  const slowRetryMs = seedSlowRetryMs(tfSec)
  const buckets = new Map<number, BucketState>()
  /** Newest bucket seen. */
  let current: number | null = null
  /** False until the first tick after subscribe or after a drop. */
  let synced = false
  /** Open time of the newest base bar seen (null before the first). */
  let lastBase: number | null = null
  let disposed = false

  const deliver = (c: Candle, closed: boolean, provisional: boolean): void => {
    try {
      // The flag is only passed when set, so a consumer that ignores it sees (c, closed).
      if (provisional) onCandle(c, closed, true)
      else onCandle(c, closed)
    } catch (err) {
      console.error('subscribeCandles: onCandle threw', err)
    }
  }

  const ready = (st: BucketState): boolean => st.complete && st.seeding === null

  /** True when the bucket's last base bar has been seen closed. */
  const lastBarClosed = (st: BucketState): boolean =>
    st.bars.get(st.start + tfSec - feed.seconds)?.closed === true

  const emit = (st: BucketState): void => {
    if (disposed || st.done || !ready(st)) return
    const candle = st.ohlcv.value(st.start)
    if (candle === null) return
    const closed = st.superseded || lastBarClosed(st)
    // Closed on a base bar that is not settled yet: closed and provisional, and not done
    // until the base source's settling send arrives (then once more, unflagged).
    let provisional = false
    if (closed) for (const b of st.bars.values()) if (b.provisional) provisional = true
    if (provisional) {
      if (st.provisionalSent !== null && sameCandle(st.provisionalSent, candle)) return
      st.provisionalSent = candle
    } else if (closed) st.done = true
    deliver(candle, closed, provisional)
  }

  const clearRetry = (st: BucketState): void => {
    if (st.retryTimer !== null) clearTimeout(st.retryTimer)
    st.retryTimer = null
  }

  /** Run seed(st) after `delay` ms (unless the bucket is replaced or the stream torn down). */
  const scheduleSeed = (st: BucketState, delay: number): void => {
    st.retryTimer = setTimeout(() => {
      st.retryTimer = null
      if (!disposed && buckets.get(st.start) === st) seed(st)
    }, delay)
  }

  /** An ended bucket that will never be emitted: drop it and ask the caller to repair it. */
  const abandon = (st: BucketState): void => {
    if (st.done) return
    clearRetry(st)
    st.done = true
    prune()
    if (disposed) return
    try {
      opts?.onRepair?.(st.start)
    } catch (err) {
      console.error('subscribeCandles: onRepair threw', err)
    }
  }

  const seed = (st: BucketState): void => {
    if (disposed || st.done || st.seeding !== null) return
    // An ended bucket whose seed already failed is left to the caller's REST repair.
    if (st.superseded && st.attempts > 0) {
      abandon(st)
      return
    }
    if (st.retryTimer !== null) return
    const hold = gate.until - Date.now()
    if (hold > 0) {
      scheduleSeed(st, hold)
      return
    }
    st.attempts += 1
    const controller = new AbortController()
    st.seeding = controller
    const end = st.start + tfSec - 1
    let request: Promise<Candle[]>
    try {
      request = feed.getCandles(symbol, ratio, end, controller.signal)
    } catch (err) {
      request = Promise.reject(err)
    }
    void request.then(
      (rows) => {
        if (disposed || st.seeding !== controller) return
        st.seeding = null
        let newestRest = -Infinity
        for (const r of rows) if (r.time > newestRest) newestRest = r.time
        for (const r of rows) {
          if (r.time < st.start || r.time > end) continue
          // A closed live bar is final; otherwise REST is at least as authoritative.
          // A provisional close is left for the base source's settling send.
          if (st.bars.get(r.time)?.closed === true) continue
          setBar(st, r, r.time < newestRest, false)
        }
        st.complete = true
        emit(st)
        if (st.done) prune()
      },
      (err: unknown) => {
        if (disposed || st.seeding !== controller) return
        st.seeding = null
        if (err instanceof MarketDataError && err.kind === 'aborted') return
        // A 429/418 has already closed the shared gate (feed requests are gated); the retry
        // below waits for it to reopen.
        if (st.superseded) {
          abandon(st)
          return
        }
        // Still forming: fast retries, then slow ones for as long as it forms.
        const delay =
          st.attempts < SEED_MAX_ATTEMPTS ? SEED_RETRY_DELAYS_MS[st.attempts - 1] : slowRetryMs
        scheduleSeed(st, Math.max(delay, gate.until - Date.now()))
      },
    )
  }

  /** Drop finished buckets older than the current one. */
  const prune = (): void => {
    for (const [start, st] of buckets) {
      if (st.done && current !== null && start < current) buckets.delete(start)
    }
  }

  /** Every bucket older than `start` is final: close it now, or seed it first. */
  const finalizeBefore = (start: number): void => {
    for (const st of buckets.values()) {
      if (st.start >= start || st.done) continue
      st.superseded = true
      if (ready(st)) emit(st)
      else seed(st)
    }
    prune()
  }

  /**
   * A base tick skipped base bars (a silent reconnect that keeps the status 'live', or a base
   * interval with no update): the open buckets may miss them, as after a drop. None is closed
   * from what was seen, and a bucket entered mid-way is not complete: they are seeded over
   * REST first (an ended one whose seed fails goes to onRepair). Unlike a drop, the attempt
   * budget is kept; only a seed sent before the skip is re-sent (it may lack the skipped bars),
   * and it does not count as an attempt.
   */
  const onSkip = (): void => {
    synced = false
    for (const st of buckets.values()) {
      if (st.done) continue
      st.complete = false
      if (st.seeding !== null) {
        st.seeding.abort()
        st.seeding = null
        st.attempts -= 1
      }
    }
  }

  const onBase = (c: Candle, closed: boolean, provisional = false): void => {
    if (disposed) return
    if (synced && lastBase !== null && c.time > lastBase + feed.seconds) onSkip()
    if (lastBase === null || c.time > lastBase) lastBase = c.time
    const start = bucketStart(c.time, tf)
    let st = buckets.get(start)
    if (st === undefined) {
      // A late tick for a bucket already closed and forgotten.
      if (current !== null && start < current) return
      st = {
        start,
        bars: new Map(),
        ohlcv: new RunningBucket(),
        // Streaming without a gap since before this bucket opened: we see all of it.
        complete: synced,
        seeding: null,
        retryTimer: null,
        attempts: 0,
        superseded: false,
        done: false,
        provisionalSent: null,
      }
      buckets.set(start, st)
    }
    if (st.done) return
    setBar(st, c, closed, closed && provisional)
    if (!synced) {
      synced = true
      // Base updates are cumulative, so a bucket entered at its first base bar is complete;
      // entered later, earlier base bars may be missing.
      if (c.time === start) st.complete = true
      else if (!st.complete) seed(st)
    }
    if (current === null || start > current) current = start
    finalizeBefore(start)
    emit(st)
  }

  /** A new connection cycle: every unfinished bucket is re-seeded with a fresh attempt budget. */
  const onDrop = (): void => {
    synced = false
    for (const st of buckets.values()) {
      if (st.done) continue
      st.complete = false
      st.seeding?.abort()
      st.seeding = null
      clearRetry(st)
      st.attempts = 0
    }
  }

  const unsubscribeBase = feed.subscribe(symbol, onBase, {
    onStatus: (status) => {
      if (disposed) return
      if (status === 'reconnecting' || status === 'offline') onDrop()
      opts?.onStatus?.(status)
    },
  })

  return () => {
    if (disposed) return
    disposed = true
    unsubscribeBase()
    for (const st of buckets.values()) {
      st.seeding?.abort()
      st.seeding = null
      clearRetry(st)
    }
    buckets.clear()
  }
}
