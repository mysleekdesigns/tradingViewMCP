import type { Candle, ConnectionStatus, Ticker } from '../types'
import { backoffDelay, openReconnectingSocket, type SocketControl } from './binanceStream'
import { MarketDataError } from './provider'

/*
 * Coinbase Exchange WebSocket feed (wss://ws-feed.exchange.coinbase.com), verified live
 * 2026-10-25 with no credentials:
 * - Nothing arrives until the client sends `{ type: 'subscribe', product_ids, channels }`; the
 *   server answers `{ type: 'subscriptions', channels: [...] }`.
 * - `matches`, `heartbeat`, `ticker` and `ticker_batch` are public (only `full`/`level2`/`user`
 *   need auth, and we never use those). An unknown product yields
 *   `{ type: 'error', message: 'Failed to subscribe', reason: 'FOO-BAR is not a valid product' }`.
 * - `matches`: one `last_match` right after subscribing, then `match` per trade:
 *   `{ type, trade_id, side, size, price, product_id, sequence, time }` (strings; ISO time with
 *   microseconds or nanoseconds).
 * - `heartbeat`: once per second per product, `{ type, last_trade_id, product_id, sequence, time }`.
 * - `ticker_batch` (reported back as channel `ticker_1000`): messages of `type: 'ticker'` batched
 *   once a second, plus one snapshot per product on subscribe: `{ product_id, price, open_24h,
 *   volume_24h, low_24h, high_24h, time, ... }`. No quote volume field.
 * - Limits (docs.cdp.coinbase.com/exchange/websocket-feed/rate-limits): 8 connects/s per IP
 *   (burst 20), 100 client messages/s, 10 subscriptions per product per channel.
 * - Heartbeats arrive every second, so a socket silent for the stale timeout is a real stall:
 *   both streams use `staleIsFailure` (status 'reconnecting' -> 'live', the consumer gap-fills)
 *   and report 'live' only on the first answer to the subscribe (`liveOnFirstMessage`).
 * - There is no kline channel, so the forming bar is built here from `matches`.
 */

/** Strict numeric field: finite number or non-empty numeric string, else null. */
function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

const ISO_FRACTION_RE = /^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

/**
 * Coinbase ISO time (`2026-10-25T14:40:43.590266569Z`) -> UNIX seconds (floored), or null. The
 * fraction is cut to milliseconds first: not every engine parses 6-9 fractional digits.
 */
export function parseIsoSeconds(value: unknown): number | null {
  if (typeof value !== 'string') return null
  const m = ISO_FRACTION_RE.exec(value.trim())
  if (m === null) return null
  const frac = m[2] === undefined ? '' : m[2].slice(0, 4)
  const ms = Date.parse(`${m[1]}${frac}${m[3]}`)
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}

export interface CoinbaseTrade {
  productId: string
  tradeId: number
  /** UNIX seconds. */
  time: number
  price: number
  size: number
}

export type FeedMessage =
  | { type: 'match'; trade: CoinbaseTrade }
  | { type: 'heartbeat'; productId: string; time: number }
  | { type: 'ticker'; productId: string; ticker: Omit<Ticker, 'symbol'> }
  | { type: 'subscriptions' }
  | { type: 'error'; message: string }

/**
 * Parse one feed message (string or object). `last_match` is ignored (it predates the
 * subscription and the REST seed already holds it). Returns null for anything malformed or
 * irrelevant. Never throws.
 */
export function parseFeedMessage(data: unknown): FeedMessage | null {
  let msg: unknown = data
  if (typeof msg === 'string') {
    try {
      msg = JSON.parse(msg)
    } catch {
      return null
    }
  }
  if (!isRecord(msg) || typeof msg.type !== 'string') return null
  switch (msg.type) {
    case 'subscriptions':
      return { type: 'subscriptions' }
    case 'error': {
      const parts = [msg.message, msg.reason].filter((p) => typeof p === 'string')
      return { type: 'error', message: parts.join(': ') || 'unknown error' }
    }
    case 'match': {
      const productId = msg.product_id
      const tradeId = finite(msg.trade_id)
      const time = parseIsoSeconds(msg.time)
      const price = finite(msg.price)
      const size = finite(msg.size)
      if (
        typeof productId !== 'string' ||
        tradeId === null ||
        time === null ||
        price === null ||
        size === null ||
        price <= 0 ||
        size < 0
      ) {
        return null
      }
      return { type: 'match', trade: { productId, tradeId, time, price, size } }
    }
    case 'heartbeat': {
      const time = parseIsoSeconds(msg.time)
      if (typeof msg.product_id !== 'string' || time === null) return null
      return { type: 'heartbeat', productId: msg.product_id, time }
    }
    case 'ticker': {
      const time = parseIsoSeconds(msg.time)
      const last = finite(msg.price)
      const open = finite(msg.open_24h)
      const high = finite(msg.high_24h)
      const low = finite(msg.low_24h)
      const volume = finite(msg.volume_24h)
      if (
        typeof msg.product_id !== 'string' ||
        time === null ||
        last === null ||
        open === null ||
        high === null ||
        low === null ||
        volume === null
      ) {
        return null
      }
      return {
        type: 'ticker',
        productId: msg.product_id,
        ticker: makeTickerFields({ last, open, high, low, volume }, time),
      }
    }
    default:
      return null
  }
}

/**
 * Ticker fields from Coinbase 24h stats. Coinbase reports base volume only, so `quoteVolume`
 * is approximated as `volume * last` (used for ranking/display, never for math).
 */
export function makeTickerFields(
  s: { last: number; open: number; high: number; low: number; volume: number },
  time: number,
): Omit<Ticker, 'symbol'> {
  return {
    last: s.last,
    open: s.open,
    high: s.high,
    low: s.low,
    volume: s.volume,
    quoteVolume: s.volume * s.last,
    changePct: s.open === 0 ? 0 : ((s.last - s.open) / s.open) * 100,
    time,
  }
}

/**
 * A WebSocket class that sends the subscribe message(s) as soon as each socket opens (the
 * Coinbase feed is silent until subscribed), so the shared reconnecting socket can be reused
 * as-is. `messages` is evaluated per socket, so a later socket can subscribe differently.
 */
export function subscribingWebSocket(
  WS: typeof WebSocket,
  messages: string | (() => readonly string[]),
): typeof WebSocket {
  const list = typeof messages === 'string' ? () => [messages] : messages
  return class SubscribingWebSocket extends WS {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(url, protocols)
      this.addEventListener('open', () => {
        try {
          for (const m of list()) this.send(m)
        } catch {
          // Closed before the send; the reconnect logic handles it.
        }
      })
    }
  }
}

/** Consecutive rejected subscribes (no 'subscriptions' answer) before giving up: 'offline'. */
export const MAX_SUBSCRIBE_REJECTIONS = 3
/** Product ids per ticker subscribe message, so one bad id cannot sink every product. */
export const TICKER_SUBSCRIBE_CHUNK = 100

const INVALID_PRODUCT_RE = /(\S+) is not a valid product/gi

/** Product ids that a feed error names as invalid (`FOO-BAR is not a valid product`). */
export function invalidProductIds(message: string): string[] {
  return [...message.matchAll(INVALID_PRODUCT_RE)].map((m) => m[1])
}

/**
 * Two views of the same bar (`rest` from REST, `live` built from trades). A view never makes
 * the bar narrower or lighter: high = max, low = min, volume = max (the views overlap, so
 * summing would double count). Open comes from REST (it saw the bucket's first trade). Close:
 * from `live` when `liveHasClose` (the live view saw every trade up to the bucket's end, so a
 * lagging REST row missing its last component cannot override it), else from the fuller view.
 */
export function mergeBars(rest: Candle, live: Candle, liveHasClose = false): Candle {
  return {
    time: rest.time,
    open: rest.open,
    high: Math.max(rest.high, live.high),
    low: Math.min(rest.low, live.low),
    close: liveHasClose || live.volume > rest.volume ? live.close : rest.close,
    volume: Math.max(rest.volume, live.volume),
  }
}

function extend(bar: Candle, t: CoinbaseTrade): Candle {
  return {
    time: bar.time,
    open: bar.open,
    high: Math.max(bar.high, t.price),
    low: Math.min(bar.low, t.price),
    close: t.price,
    volume: bar.volume + t.size,
  }
}

function barFromTrade(time: number, t: CoinbaseTrade): Candle {
  return { time, open: t.price, high: t.price, low: t.price, close: t.price, volume: t.size }
}

function sameBar(a: Candle, b: Candle): boolean {
  return (
    a.time === b.time &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume
  )
}

export const DEFAULT_RECONCILE_DELAY_MS = 1_500
export const DEFAULT_RECONCILE_TIMEOUT_MS = 5_000
/**
 * Second REST read-back of a closed bar whose live view is not known complete (also the wait
 * before retrying a failed stall backfill).
 */
export const DEFAULT_RECONCILE_RETRY_MS = 5_000
/** Retry of a failed REST seed (once per subscription). */
export const DEFAULT_SEED_RETRY_MS = 3_000
/** Most skipped buckets backfilled over REST after a stall (the provider's per-call cap). */
const MAX_BACKFILL_BARS = 1_000
/**
 * Largest step (seconds) between consecutive feed times that still counts as a continuous
 * feed. Heartbeats are 1/s per product, so a longer silence means messages may be lost.
 */
export const MAX_FEED_GAP_S = 3

export interface CandleStreamOptions {
  WebSocketImpl: typeof WebSocket
  url: string
  staleTimeoutMs: number
  /** Start of the bucket (UNIX seconds) that contains `t`, e.g. 4h on UTC 00/04/08..., 1w on Monday. */
  bucketStart: (t: number) => number
  /** Symbol -> product id. A 'bad-symbol' rejection stops for good ('offline'); others retry. */
  resolveProduct: (signal: AbortSignal) => Promise<string>
  /**
   * Up to `limit` candles ending at `endTime` (UNIX s; undefined = now), ascending. Must not
   * step back past an empty window: an empty answer means "no rows there".
   */
  fetchCandles: (
    limit: number,
    endTime: number | undefined,
    signal: AbortSignal,
  ) => Promise<Candle[]>
  /**
   * Bars in time order. A closed bar may be sent once more later with the same time (a
   * corrected REST read-back); consumers replace the bar with that time. `provisional` is
   * true on a closed bar that will be sent once more (a second REST read-back is pending):
   * its values may still change. That second send is closed without the flag, even when the
   * values did not change, and settles the bar. Never set on a forming bar.
   */
  onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void
  onStatus?: (s: ConnectionStatus) => void
  /** Wait after a bucket rolls before reading the closed bar from REST (late trades, REST lag). */
  reconcileDelayMs?: number
  reconcileTimeoutMs?: number
  /**
   * Delay of the second read-back when the live view is not known complete, and of the retry
   * of a failed stall backfill.
   */
  reconcileRetryMs?: number
  /** Delay before retrying a failed REST seed. */
  seedRetryMs?: number
  /** Local clock, ms. Only used to reject a stale REST seed before the first heartbeat. */
  now?: () => number
}

/**
 * Shared start-up for both streams: resolve what to subscribe to (product ids need the
 * product list), retrying with backoff, then open the reconnecting socket. Status reports are
 * de-duplicated across the two phases. Returns an idempotent teardown.
 */
function startStream<T>(opts: {
  resolve: (signal: AbortSignal) => Promise<T>
  open: (resolved: T, emit: (s: ConnectionStatus) => void) => () => void
  onStatus?: (s: ConnectionStatus) => void
  onDispose: () => void
  what: string
  abort: AbortController
}): () => void {
  let disposed = false
  let lastStatus: ConnectionStatus | null = null
  let closeSocket: (() => void) | null = null
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let failures = 0

  const emit = (s: ConnectionStatus): void => {
    if (disposed || s === lastStatus) return
    // The socket reports 'connecting' when it starts; after a failed resolve that would read
    // as a step back from 'reconnecting', so 'connecting' is only ever the first status.
    if (s === 'connecting' && lastStatus !== null) return
    lastStatus = s
    opts.onStatus?.(s)
  }

  const teardown = (): void => {
    if (disposed) return
    disposed = true
    if (retryTimer !== undefined) clearTimeout(retryTimer)
    retryTimer = undefined
    opts.abort.abort()
    closeSocket?.()
    closeSocket = null
    opts.onDispose()
  }

  const attempt = (): void => {
    opts.resolve(opts.abort.signal).then(
      (resolved) => {
        if (disposed) return
        closeSocket = opts.open(resolved, emit)
      },
      (err: unknown) => {
        if (disposed) return
        if (err instanceof MarketDataError && err.kind === 'bad-symbol') {
          console.warn(`${opts.what}: ${err.message}`)
          emit('offline')
          teardown()
          return
        }
        failures += 1
        emit(failures >= 5 ? 'offline' : 'reconnecting')
        retryTimer = setTimeout(
          () => {
            retryTimer = undefined
            attempt()
          },
          backoffDelay(failures - 1),
        )
      },
    )
  }

  emit('connecting')
  attempt()
  return teardown
}

/**
 * A bucket that ended, awaiting its REST read-back; the bucket a subscription started in when
 * its seed failed and no trade landed (no live bar: REST alone); or a run of buckets skipped
 * by a stall.
 */
type CloseItem =
  | {
      kind: 'bar'
      bar: Candle
      /**
       * Live has the close: a trade on the current subscription set it and the feed was
       * continuous from then through the roll.
       */
      coveredEnd: boolean
      /** The feed was continuous from before this bucket opened: live saw every trade in it. */
      fromOpen: boolean
    }
  | { kind: 'rest'; time: number }
  | {
      kind: 'gap'
      from: number
      to: number
      len: number
      /**
       * Its backfill failed while the socket was not live: it stays at the head of the queue
       * (holding back later bars, so the consumer still sees the pre-gap bar as the last one)
       * until the socket's next 'live', which retries it once.
       */
      awaitLive: boolean
    }

/**
 * Live candles for one product from `matches` + `heartbeat`:
 * - Each (re)subscription seeds the forming bar from REST. Trades that arrive while the seed is
 *   in flight are merged with `mergeBars` (never double counted, never narrowing the REST bar);
 *   later trades extend it. A failed seed is retried once; until a seed succeeds the bucket
 *   the subscription started in is not shown as forming (a trades-only bar would replace the
 *   fuller REST-loaded one); it is still closed and reconciled normally.
 * - Heartbeats (1/s) and trade times advance the clock. When a bucket ends, the bar is read
 *   back from REST after `reconcileDelayMs` and emitted with `closed=true` (merged with the
 *   live view, or the live view alone if REST fails; then, unless the live view is known
 *   complete, that close is marked provisional and one more read-back after
 *   `reconcileRetryMs` re-emits the bar closed and final: corrected if it changed, the same
 *   values otherwise, or when the read-back fails).
 *   The close is the live one only when a trade on the current subscription set it and the
 *   feed stayed continuous (no step over MAX_FEED_GAP_S) through the bucket's end; otherwise
 *   the fuller view (more volume) decides. A longer step counts as a fresh start.
 * - If the bucket a subscription started in ends with no live bar (its seed failed and no
 *   trade landed since), it is read back from REST alone and emitted closed (provisional) if
 *   REST has a row; one more read-back after `reconcileRetryMs` (the row failed, was missing
 *   or lagged) re-emits it closed and final (or emits it then, if only that read has a row).
 * - A clock jump of more than one bucket (a stall, sleep/wake) backfills the skipped buckets
 *   over REST, emitted closed in time order before the next forming bar; the newest backfilled
 *   row is provisional and settled by one more read-back, as above. A failed backfill
 *   is retried once after `reconcileRetryMs`. If that fails too and the socket is live, the
 *   status pulses 'reconnecting' -> 'live' so the consumer runs its own REST gap fill. If the
 *   socket is not live, the run (and every later bar) is held until the socket's next 'live',
 *   which retries the backfill once more, then pulses if it still fails: the consumer's
 *   last bar stays the pre-gap one, so its own reconnect check never skips the gap fill.
 * - Updates for the next bucket are held until all of that, so bars are emitted in time order.
 * - A bucket with no trades emits nothing (no forward fill). Trades older than the forming
 *   bucket are folded into a bar still awaiting its close, else dropped.
 * - A subscribe rejected with "not a valid product" stops ('offline'); other rejections fail
 *   the socket and back off, and MAX_SUBSCRIBE_REJECTIONS in a row stop ('offline').
 * - The forming bar is display-only. A `closed=true` bar is final unless it is marked
 *   provisional; a provisional close is settled by a later closed send of the same bar.
 */
export function openCoinbaseCandleStream(opts: CandleStreamOptions): () => void {
  const reconcileDelayMs = opts.reconcileDelayMs ?? DEFAULT_RECONCILE_DELAY_MS
  const reconcileTimeoutMs = opts.reconcileTimeoutMs ?? DEFAULT_RECONCILE_TIMEOUT_MS
  const reconcileRetryMs = opts.reconcileRetryMs ?? DEFAULT_RECONCILE_RETRY_MS
  const seedRetryMs = opts.seedRetryMs ?? DEFAULT_SEED_RETRY_MS
  const abort = new AbortController()
  const { bucketStart } = opts
  const now = opts.now ?? (() => Date.now())

  let disposed = false
  let productId = ''
  /** Bucket of the latest server time seen (heartbeat or trade). */
  let clockBucket: number | null = null
  /** Bar of `clockBucket` built so far, if any trade or seed touched it. */
  let forming: Candle | null = null
  /** Ended buckets and skipped runs, oldest first, awaiting REST reconciliation. */
  const closeQueue: CloseItem[] = []
  let reconciling = false
  let seedSeq = 0
  let seeding = false
  let buffered: CoinbaseTrade[] = []
  let lastTradeId = -Infinity
  let lastEmittedTime = -Infinity
  let delayTimer: ReturnType<typeof setTimeout> | undefined
  /** Read-back and seed retry timers, cleared on dispose. */
  const timers = new Set<ReturnType<typeof setTimeout>>()
  /** Bucket the subscription started in, when its seed failed: no forming bar is shown for it. */
  let unseededBucket: number | null = null
  /** The seed failed before any feed time arrived: the next one names `unseededBucket`. */
  let unseededPending = false
  /** The current socket got its 'subscriptions' answer. */
  let confirmed = false
  let rejections = 0
  /** Bucket of the first feed time on the current subscription (null until one arrives). */
  let epochBucket: number | null = null
  /** Bucket from which the current subscription's feed has been continuous (reset by a jump). */
  let coverageBucket: number | null = null
  /** Latest feed time on the current subscription (null until one arrives). */
  let lastFeedAt: number | null = null
  /** `forming`'s close was set by a trade on the current subscription, with no jump since. */
  let closeLive = false
  /** Status last reported by the socket. */
  let socketStatus: ConnectionStatus | null = null
  let emitStatus: (s: ConnectionStatus) => void = () => {}
  let stopAll: () => void = () => {}

  const later = (fn: () => void, ms: number): void => {
    const timer = setTimeout(() => {
      timers.delete(timer)
      if (!disposed) fn()
    }, ms)
    timers.add(timer)
  }

  const deliver = (c: Candle, closed: boolean, provisional: boolean): void => {
    try {
      // The flag is only passed when set, so a consumer that ignores it sees (c, closed).
      if (provisional) opts.onCandle(c, closed, true)
      else opts.onCandle(c, closed)
    } catch (err) {
      console.error('subscribeCandles: onCandle threw', err)
    }
  }

  const emitCandle = (c: Candle, closed: boolean, provisional = false): void => {
    if (disposed || c.time < lastEmittedTime) return
    lastEmittedTime = c.time
    deliver(c, closed, provisional)
  }

  /**
   * The settled (final) closed bar after the second read-back, changed or not; it may be
   * older than the latest emitted bar (same time = replace).
   */
  const emitSettled = (c: Candle): void => {
    if (disposed) return
    if (c.time >= lastEmittedTime) emitCandle(c, true)
    else deliver(c, true, false)
  }

  const emitForming = (): void => {
    if (seeding || reconciling || closeQueue.length > 0 || forming === null) return
    if (forming.time === unseededBucket) return
    emitCandle(forming, false)
  }

  const sleep = (ms: number): Promise<void> =>
    new Promise((resolve) => {
      delayTimer = setTimeout(() => {
        delayTimer = undefined
        resolve()
      }, ms)
    })

  const readSignal = (): AbortSignal =>
    AbortSignal.any([abort.signal, AbortSignal.timeout(reconcileTimeoutMs)])

  /** The REST row of bucket `time` (undefined when REST lacks it), or null when the request fails. */
  const readBack = async (time: number): Promise<Candle | undefined | null> => {
    try {
      const rows = await opts.fetchCandles(1, time, readSignal())
      return rows.find((r) => r.time === time)
    } catch {
      return null
    }
  }

  /**
   * One more read-back of a closed bar whose live view is not known complete. `emitted` is
   * the bar already sent provisional (null if none was). The result is sent closed and final
   * (no provisional flag): the merged bar, the `emitted` bar unchanged when the merge equals
   * it or REST fails or lacks the row, or nothing when neither exists.
   */
  const scheduleReadBack = (time: number, emitted: Candle | null, liveHasClose: boolean): void => {
    later(() => {
      void readBack(time).then((rest) => {
        if (disposed) return
        if (rest === undefined || rest === null) {
          if (emitted !== null) emitSettled(emitted)
          return
        }
        const fixed = emitted === null ? rest : mergeBars(rest, emitted, liveHasClose)
        emitSettled(emitted !== null && sameBar(fixed, emitted) ? emitted : fixed)
      })
    }, reconcileRetryMs)
  }

  /** REST rows of a skipped run, ascending, or null when the request fails. */
  const backfill = async (gap: Extract<CloseItem, { kind: 'gap' }>): Promise<Candle[] | null> => {
    const count = Math.min(MAX_BACKFILL_BARS, Math.round((gap.to - gap.from) / gap.len) + 1)
    try {
      const rows = await opts.fetchCandles(count, gap.to, readSignal())
      return rows
        .filter((r) => r.time >= gap.from && r.time <= gap.to)
        .sort((a, b) => a.time - b.time)
    } catch (err) {
      console.warn('subscribeCandles: backfill after a stall failed', err)
      return null
    }
  }

  /**
   * The skipped bars could not be read: have the consumer gap-fill over REST itself with a
   * 'reconnecting' -> 'live' pulse. Only called on a live socket (a gap whose backfill failed
   * while the socket was down waits for its 'live' instead).
   */
  const requestConsumerGapFill = (): void => {
    if (socketStatus !== 'live') return
    emitStatus('reconnecting')
    emitStatus('live')
  }

  const reconcile = async (): Promise<void> => {
    if (reconciling) return
    reconciling = true
    try {
      while (closeQueue.length > 0 && !disposed) {
        const first = closeQueue[0]
        // A run whose backfill failed while the socket was down waits for the socket's 'live'.
        if (first.kind === 'gap' && first.awaitLive && socketStatus !== 'live') break
        await sleep(reconcileDelayMs)
        if (disposed) return
        const head = closeQueue[0]
        if (head.kind === 'gap') {
          let rows = await backfill(head)
          if (disposed) return
          if (rows === null && !head.awaitLive) {
            // Held in the queue meanwhile, so later bars still arrive in time order.
            await sleep(reconcileRetryMs)
            if (disposed) return
            rows = await backfill(head)
            if (disposed) return
          }
          if (rows === null && socketStatus !== 'live') {
            // No pulse is possible now, and the socket's own later 'live' may not make the
            // consumer gap-fill once a newer bar is on screen: hold the run (and every later
            // bar) until that 'live' retries it.
            head.awaitLive = true
            break
          }
          // Pulse while the pre-gap bar is still the consumer's last bar, then move on.
          if (rows === null) requestConsumerGapFill()
          closeQueue.shift()
          if (rows !== null && rows.length > 0) {
            // The newest row of the run ended last and can lag (CDN cache) like any fresh
            // read-back: provisional, read back once more and settled like a per-bar close.
            const newest = rows[rows.length - 1]
            for (const r of rows) emitCandle(r, true, r === newest)
            scheduleReadBack(newest.time, newest, false)
          }
          continue
        }
        const time = head.kind === 'bar' ? head.bar.time : head.time
        const rest = await readBack(time)
        if (disposed) return
        // Re-read: a late trade may have replaced the queued item while REST was in flight.
        const live = closeQueue.shift()
        if (live === undefined || live.kind === 'gap') break
        if (live.kind === 'rest') {
          // No live view of this bucket: REST alone (a quiet bucket has no row: emit nothing).
          // A row can fail, be missing or lag (CDN cache): read back once more either way, so
          // this close is provisional.
          if (rest !== undefined && rest !== null) emitCandle(rest, true, true)
          scheduleReadBack(time, rest ?? null, false)
          continue
        }
        const known = rest === undefined || rest === null ? undefined : rest
        const emitted = known === undefined ? live.bar : mergeBars(known, live.bar, live.coveredEnd)
        // A REST row can lag (CDN cache) as well as fail: read back once more unless the live
        // view is known complete. Until then this close is provisional.
        const complete = live.fromOpen && live.coveredEnd
        emitCandle(emitted, true, !complete)
        if (!complete) scheduleReadBack(time, emitted, live.coveredEnd)
      }
    } finally {
      reconciling = false
    }
    emitForming()
  }

  /**
   * Move the clock to `t`; a later bucket queues the forming bar for its close, and a jump of
   * more than one bucket queues the skipped run for a REST backfill.
   */
  const advanceClock = (t: number): void => {
    const b = bucketStart(t)
    if (clockBucket !== null && b <= clockBucket) return
    if (forming !== null) {
      closeQueue.push({
        kind: 'bar',
        bar: forming,
        coveredEnd: closeLive && coverageBucket !== null && coverageBucket <= forming.time,
        fromOpen: coverageBucket !== null && coverageBucket < forming.time,
      })
    } else if (clockBucket !== null && clockBucket === unseededBucket) {
      // The bucket the subscription started in (its seed failed) ended without a live bar:
      // the consumer may still show a partial REST row of it.
      closeQueue.push({ kind: 'rest', time: clockBucket })
    }
    if (clockBucket !== null) {
      const lastSkipped = bucketStart(b - 1)
      if (lastSkipped > clockBucket) {
        const len = b - lastSkipped
        closeQueue.push({
          kind: 'gap',
          from: clockBucket + len,
          to: lastSkipped,
          len,
          awaitLive: false,
        })
      }
    }
    forming = null
    closeLive = false
    clockBucket = b
    if (closeQueue.length > 0) void reconcile()
  }

  /**
   * Note a feed time of the current subscription: the first one names the epoch; a step of
   * more than MAX_FEED_GAP_S past the latest one means messages may be lost, so coverage
   * restarts there and the forming close is no longer known to be the latest.
   */
  const markEpoch = (t: number): void => {
    if (epochBucket === null) {
      epochBucket = bucketStart(t)
      coverageBucket = epochBucket
      if (unseededPending) {
        unseededPending = false
        unseededBucket = epochBucket
      }
    } else if (lastFeedAt !== null && t - lastFeedAt > MAX_FEED_GAP_S) {
      coverageBucket = bucketStart(t)
      closeLive = false
    }
    if (lastFeedAt === null || t > lastFeedAt) lastFeedAt = t
  }

  /** A new subscription: nothing it saw yet, so no live close and no continuous coverage. */
  const resetSubscription = (): void => {
    epochBucket = null
    coverageBucket = null
    lastFeedAt = null
    closeLive = false
  }

  /** A feed time (heartbeat or trade) on the current subscription. */
  const feedTime = (t: number): void => {
    markEpoch(t)
    advanceClock(t)
  }

  const applyTrade = (t: CoinbaseTrade): void => {
    advanceClock(t.time)
    const b = bucketStart(t.time)
    if (clockBucket !== null && b < clockBucket) {
      // A late trade for a REST-only bucket gives it a (partial) live view.
      const r = closeQueue.findIndex((c) => c.kind === 'rest' && c.time === b)
      if (r >= 0) {
        closeQueue[r] = { kind: 'bar', bar: barFromTrade(b, t), coveredEnd: false, fromOpen: false }
        return
      }
      const i = closeQueue.findIndex((c) => c.kind === 'bar' && c.bar.time === b)
      const item = closeQueue[i]
      if (item !== undefined && item.kind === 'bar')
        closeQueue[i] = { ...item, bar: extend(item.bar, t) }
      return
    }
    forming = forming === null ? barFromTrade(b, t) : extend(forming, t)
    closeLive = true
    emitForming()
  }

  /** `retry`: a second attempt on the same subscription, whose live bar is up to date. */
  const seed = (retry = false): void => {
    const seq = ++seedSeq
    seeding = true
    buffered = []
    const finish = (rest: Candle | undefined, failed: boolean): void => {
      if (disposed || seq !== seedSeq) return
      // `seeding` stays true while the buffer is replayed, so the bar is emitted once at the end.
      const pending = buffered
      buffered = []
      let seededTime: number | null = null
      // REST candles are served through a CDN cache (observed live): the newest row can be the
      // previous bucket. Only a row of the current bucket (server clock, else local clock) may
      // seed the forming bar; an older one is closed and the consumer already has it.
      const current = clockBucket ?? bucketStart(Math.floor(now() / 1000))
      if (rest !== undefined && rest.time >= current) {
        advanceClock(rest.time)
        // Rows from REST are always newer-or-equal views of the bucket than our live bar
        // built before a reconnect: merge so neither loses high/low/volume. On a retry the
        // live close is the latest only if a trade on this subscription set it (a close kept
        // from before a reconnect may be stale; then the fuller view decides).
        const same = forming !== null && forming.time === rest.time
        forming = forming !== null && same ? mergeBars(rest, forming, retry && closeLive) : rest
        if (!same) closeLive = false
        seededTime = rest.time
      }
      if (failed) {
        // The bucket the subscription started in (server clock; `clockBucket` may predate a
        // stall). Without a feed time yet, the first one decides.
        if (epochBucket !== null) unseededBucket = epochBucket
        else unseededPending = true
        if (!retry) later(() => seq === seedSeq && seed(true), seedRetryMs)
      } else {
        unseededBucket = null
        unseededPending = false
      }
      let side: Candle | null = null
      for (const t of pending) {
        if (
          seededTime !== null &&
          bucketStart(t.time) === seededTime &&
          clockBucket === seededTime
        ) {
          side = side === null ? barFromTrade(seededTime, t) : extend(side, t)
        } else {
          applyTrade(t)
        }
      }
      if (side !== null && forming !== null && forming.time === side.time) {
        // Every trade since subscribing is in `side`, so its last price is the latest close.
        forming = { ...mergeBars(forming, side), close: side.close }
        closeLive = true
      }
      seeding = false
      emitForming()
    }
    opts.fetchCandles(2, undefined, abort.signal).then(
      (rows) => finish(rows[rows.length - 1], false),
      () => finish(undefined, true),
    )
  }

  /** Give up for good: 'offline', then tear everything down. */
  const stop = (why: string): void => {
    console.warn(`subscribeCandles: ${why}`)
    emitStatus('offline')
    stopAll()
  }

  const onError = (message: string, control: SocketControl): void => {
    if (invalidProductIds(message).length > 0) {
      stop(`Coinbase rejected ${productId}: ${message}`)
      return
    }
    if (confirmed) {
      console.warn(`Coinbase feed error for ${productId}: ${message}`)
      return
    }
    // The subscribe itself was rejected: a failed attempt, not a live stream.
    rejections += 1
    if (rejections >= MAX_SUBSCRIBE_REJECTIONS) {
      stop(`Coinbase rejected the subscription ${rejections} times: ${message}`)
      return
    }
    console.warn(`Coinbase subscribe for ${productId} rejected: ${message}`)
    control.fail()
  }

  const onMessage = (data: unknown, control: SocketControl): void => {
    const msg = parseFeedMessage(data)
    if (msg === null) return
    switch (msg.type) {
      case 'subscriptions':
        if (confirmed) return
        confirmed = true
        rejections = 0
        resetSubscription()
        seed()
        return
      case 'error':
        onError(msg.message, control)
        return
      case 'heartbeat':
        if (msg.productId === productId) feedTime(msg.time)
        return
      case 'match': {
        const t = msg.trade
        if (t.productId !== productId || t.tradeId <= lastTradeId) return
        lastTradeId = t.tradeId
        markEpoch(t.time)
        if (seeding) buffered.push(t)
        else applyTrade(t)
        return
      }
      default:
        return
    }
  }

  stopAll = startStream({
    what: 'subscribeCandles',
    abort,
    onStatus: opts.onStatus,
    resolve: opts.resolveProduct,
    onDispose: () => {
      disposed = true
      if (delayTimer !== undefined) clearTimeout(delayTimer)
      delayTimer = undefined
      for (const timer of timers) clearTimeout(timer)
      timers.clear()
    },
    open: (pid, emit) => {
      productId = pid
      emitStatus = emit
      const message = JSON.stringify({
        type: 'subscribe',
        product_ids: [pid],
        channels: ['matches', 'heartbeat'],
      })
      return openReconnectingSocket({
        WebSocketImpl: subscribingWebSocket(opts.WebSocketImpl, () => {
          // A new socket: its subscription is unconfirmed until 'subscriptions' arrives.
          confirmed = false
          resetSubscription()
          unseededPending = false
          return [message]
        }),
        url: opts.url,
        staleTimeoutMs: opts.staleTimeoutMs,
        staleIsFailure: true,
        liveOnFirstMessage: true,
        onStatus: (st) => {
          socketStatus = st
          emit(st)
          // A stall backfill that failed while the socket was down: retry it now.
          const head = closeQueue[0]
          if (st === 'live' && head !== undefined && head.kind === 'gap' && head.awaitLive) {
            void reconcile()
          }
        },
        onMessage,
      })
    },
  })
  return stopAll
}

export const DEFAULT_TICKER_FLUSH_MS = 250

export interface TickerStreamOptions {
  WebSocketImpl: typeof WebSocket
  url: string
  staleTimeoutMs: number
  /** Tradable product ids to subscribe to and product id -> app symbol. */
  resolveProducts: (signal: AbortSignal) => Promise<{
    productIds: readonly string[]
    toSymbol: ReadonlyMap<string, string>
  }>
  onTickers: (tickers: Ticker[]) => void
  onStatus?: (s: ConnectionStatus) => void
  /** Coalesce ticker messages for this long into one `onTickers` call. */
  flushMs?: number
  /** Product ids per subscribe message (default TICKER_SUBSCRIBE_CHUNK). */
  chunkSize?: number
}

function subscribeMessage(productIds: readonly string[], channel: string): string {
  return JSON.stringify({ type: 'subscribe', product_ids: productIds, channels: [channel] })
}

/**
 * 24h tickers for every tradable product over ONE socket (`ticker_batch`: one update per
 * product per second at most). Messages are coalesced per `flushMs` so each `onTickers` call
 * carries only the symbols that changed, latest value per symbol.
 *
 * The subscribe is sent in chunks of `chunkSize` ids. An error naming invalid ids drops them
 * (for this and every later socket) and resubscribes the rest of their chunk. Any other error
 * before every chunk is confirmed fails the socket (backoff); MAX_SUBSCRIBE_REJECTIONS in a
 * row, or no valid id left, stop ('offline').
 */
export function openCoinbaseTickerStream(opts: TickerStreamOptions): () => void {
  const flushMs = opts.flushMs ?? DEFAULT_TICKER_FLUSH_MS
  const chunkSize = Math.max(1, Math.floor(opts.chunkSize ?? TICKER_SUBSCRIBE_CHUNK))
  const abort = new AbortController()
  let disposed = false
  let toSymbol: ReadonlyMap<string, string> = new Map()
  const pending = new Map<string, Ticker>()
  let flushTimer: ReturnType<typeof setTimeout> | undefined
  /** Ids still subscribed to; invalid ones are removed for good. */
  let productIds: string[] = []
  /** The current socket's chunks (ids per subscribe message). */
  let chunks: string[][] = []
  /** Subscribe messages on the current socket not answered yet. */
  let outstanding = 0
  let rejections = 0
  let emitStatus: (s: ConnectionStatus) => void = () => {}
  let stopAll: () => void = () => {}

  const flush = (): void => {
    flushTimer = undefined
    if (disposed || pending.size === 0) return
    const batch = [...pending.values()]
    pending.clear()
    try {
      opts.onTickers(batch)
    } catch (err) {
      console.error('subscribeTickers: onTickers threw', err)
    }
  }

  const stop = (why: string): void => {
    console.warn(`subscribeTickers: ${why}`)
    emitStatus('offline')
    stopAll()
  }

  const onError = (message: string, control: SocketControl): void => {
    const bad = new Set(invalidProductIds(message).filter((id) => productIds.includes(id)))
    if (bad.size > 0) {
      productIds = productIds.filter((id) => !bad.has(id))
      console.warn(`Coinbase ticker feed: dropped ${[...bad].join(', ')} (${message})`)
      if (productIds.length === 0) {
        stop('no valid product left to subscribe to')
        return
      }
      // The rejected message subscribed nothing: resend its chunk without the bad ids.
      const i = chunks.findIndex((c) => c.some((id) => bad.has(id)))
      const rest = i >= 0 ? chunks[i].filter((id) => !bad.has(id)) : []
      if (i >= 0) chunks[i] = rest
      if (rest.length > 0 && control.send(subscribeMessage(rest, 'ticker_batch'))) return
      outstanding = Math.max(0, outstanding - 1)
      if (outstanding === 0) rejections = 0
      return
    }
    if (outstanding === 0) {
      console.warn(`Coinbase ticker feed error: ${message}`)
      return
    }
    rejections += 1
    if (rejections >= MAX_SUBSCRIBE_REJECTIONS) {
      stop(`Coinbase rejected the subscription ${rejections} times: ${message}`)
      return
    }
    console.warn(`Coinbase ticker subscribe rejected: ${message}`)
    control.fail()
  }

  const onMessage = (data: unknown, control: SocketControl): void => {
    const msg = parseFeedMessage(data)
    if (msg === null) return
    if (msg.type === 'subscriptions') {
      outstanding = Math.max(0, outstanding - 1)
      if (outstanding === 0) rejections = 0
      return
    }
    if (msg.type === 'error') {
      onError(msg.message, control)
      return
    }
    if (msg.type !== 'ticker') return
    const symbol = toSymbol.get(msg.productId)
    if (symbol === undefined) return
    pending.set(symbol, { symbol, ...msg.ticker })
    if (flushTimer === undefined) flushTimer = setTimeout(flush, flushMs)
  }

  stopAll = startStream({
    what: 'subscribeTickers',
    abort,
    onStatus: opts.onStatus,
    resolve: opts.resolveProducts,
    onDispose: () => {
      disposed = true
      if (flushTimer !== undefined) clearTimeout(flushTimer)
      flushTimer = undefined
      pending.clear()
    },
    open: (resolved, emit) => {
      toSymbol = resolved.toSymbol
      productIds = [...resolved.productIds]
      emitStatus = emit
      return openReconnectingSocket({
        WebSocketImpl: subscribingWebSocket(opts.WebSocketImpl, () => {
          chunks = []
          for (let i = 0; i < productIds.length; i += chunkSize) {
            chunks.push(productIds.slice(i, i + chunkSize))
          }
          outstanding = chunks.length
          return chunks.map((c) => subscribeMessage(c, 'ticker_batch'))
        }),
        url: opts.url,
        staleTimeoutMs: opts.staleTimeoutMs,
        staleIsFailure: true,
        liveOnFirstMessage: true,
        onStatus: emit,
        onMessage,
      })
    },
  })
  return stopAll
}
