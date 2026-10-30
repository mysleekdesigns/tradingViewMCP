import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { ConnectionStatus, Ticker } from '../types'

/** Direction of the most recent change in `last`, for the price flash. */
export type TickMove = 'up' | 'down'

export interface TickersSnapshot {
  /** Latest 24h stats for watched symbols only; a symbol is absent until its first data. */
  tickers: ReadonlyMap<string, Ticker>
  /** Direction of each symbol's last price change (absent until the price has moved). */
  moves: ReadonlyMap<string, TickMove>
  /** Ticker stream connection state, as reported by the provider. */
  status: ConnectionStatus
}

export type TickersListener = (snapshot: TickersSnapshot) => void

export interface TickersOptions {
  provider: MarketDataProvider
  /** Minimum gap between data emits (ms). Status changes emit at once. Default 250 (≤ 4/s). */
  throttleMs?: number
  /** Injectable clock for tests. */
  now?: () => number
}

export const DEFAULT_TICKER_THROTTLE_MS = 250

/** Delays before re-trying a failed REST seed (ms); the stream still fills in meanwhile. */
export const SEED_RETRY_DELAYS_MS: readonly number[] = [5_000, 15_000, 45_000]

export const initialTickersSnapshot: TickersSnapshot = {
  tickers: new Map(),
  moves: new Map(),
  status: 'connecting',
}

/**
 * Framework-free owner of the watchlist's live prices: one `subscribeTickers` socket for the
 * controller's lifetime, REST seeds via `getTickers` for symbols that join the watched set
 * and for all of them after a reconnect, and throttled emits. Changing the watched symbols
 * never touches the socket. After stop() the listener is never called again.
 */
export class TickersController {
  private readonly provider: MarketDataProvider
  private readonly listener: TickersListener
  private readonly throttleMs: number
  private readonly now: () => number
  private snapshot: TickersSnapshot = initialTickersSnapshot
  /** Replaced (never mutated) on every change: the provider's `symbols` getter reads it. */
  private watched = new Set<string>()
  /** Provider callbacks to run after `watched` changes (`onSymbolsChange`). */
  private readonly symbolListeners = new Set<() => void>()
  private tickers = new Map<string, Ticker>()
  private moves = new Map<string, TickMove>()
  private started = false
  private stopped = false
  private unsubscribe: (() => void) | null = null
  private seeds = new Set<AbortController>()
  private dropped = false
  private dirty = false
  private lastEmit = Number.NEGATIVE_INFINITY
  private emitTimer: ReturnType<typeof setTimeout> | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private retries = 0
  /** No REST calls before this wall-clock time (ms), from a 429/418 Retry-After. */
  private blockedUntil = 0

  constructor(opts: TickersOptions, listener: TickersListener) {
    this.provider = opts.provider
    this.listener = listener
    this.throttleMs = opts.throttleMs ?? DEFAULT_TICKER_THROTTLE_MS
    this.now = opts.now ?? Date.now
  }

  getSnapshot(): TickersSnapshot {
    return this.snapshot
  }

  /** Open the ticker stream and seed the watched symbols. Call once. */
  start(): void {
    if (this.started || this.stopped) return
    this.started = true
    try {
      this.unsubscribe = this.provider.subscribeTickers((t) => this.onTickers(t), {
        onStatus: (s) => this.onStatus(s),
        // Lets the provider drop unwatched symbols before parsing them; the socket stays open
        // across watchlist edits (the getter is read per push).
        symbols: () => this.watched,
        onSymbolsChange: (listener) => {
          this.symbolListeners.add(listener)
          return () => {
            this.symbolListeners.delete(listener)
          }
        },
      })
    } catch {
      this.setStatus('offline')
    }
    this.seed([...this.watched])
  }

  /**
   * Replace the watched set. Removed symbols are dropped from the snapshot; added ones are
   * seeded over REST (once started). The socket is left alone.
   */
  setSymbols(symbols: readonly string[]): void {
    if (this.stopped) return
    const next = new Set(symbols)
    const added = [...next].filter((s) => !this.watched.has(s))
    const removed = [...this.watched].filter((s) => !next.has(s))
    if (added.length === 0 && removed.length === 0) return
    this.watched = next
    for (const s of removed) {
      this.tickers.delete(s)
      this.moves.delete(s)
    }
    if (removed.length > 0) this.markDirty()
    for (const listener of [...this.symbolListeners]) {
      try {
        listener()
      } catch (err) {
        console.error('TickersController: symbols listener threw', err)
      }
    }
    if (this.started) this.seed(added)
  }

  /** Close the socket, abort seeds, cancel timers. Idempotent. */
  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.unsubscribe?.()
    this.unsubscribe = null
    this.symbolListeners.clear()
    for (const c of this.seeds) c.abort()
    this.seeds.clear()
    if (this.emitTimer !== null) clearTimeout(this.emitTimer)
    this.emitTimer = null
    this.clearRetry()
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = null
  }

  private onStatus(status: ConnectionStatus): void {
    if (this.stopped) return
    if (status === 'reconnecting' || status === 'offline') this.dropped = true
    const reconnected = status === 'live' && this.dropped
    if (status === 'live') this.dropped = false
    this.setStatus(status)
    // Pushes only carry changed symbols, so quiet ones may be stale after an outage.
    if (reconnected) this.reseedAll()
  }

  private setStatus(status: ConnectionStatus): void {
    if (status === this.snapshot.status) return
    this.flush({ status })
  }

  private reseedAll(): void {
    for (const c of this.seeds) c.abort()
    this.seeds.clear()
    this.clearRetry()
    this.retries = 0
    this.seed([...this.watched])
  }

  private onTickers(batch: readonly Ticker[]): void {
    if (this.stopped) return
    let changed = false
    for (const t of batch) {
      if (this.watched.has(t.symbol) && this.apply(t)) changed = true
    }
    if (changed) this.markDirty()
  }

  /** Store `t` unless an equal-or-newer ticker is already held. True when stored. */
  private apply(t: Ticker): boolean {
    const prev = this.tickers.get(t.symbol)
    if (prev !== undefined && t.time < prev.time) return false
    if (prev !== undefined && sameTicker(prev, t)) return false
    this.tickers.set(t.symbol, t)
    if (prev !== undefined && t.last !== prev.last) {
      this.moves.set(t.symbol, t.last > prev.last ? 'up' : 'down')
    }
    return true
  }

  private seed(symbols: readonly string[]): void {
    if (this.stopped || symbols.length === 0) return
    if (this.now() < this.blockedUntil) {
      this.scheduleRetry(this.blockedUntil - this.now())
      return
    }
    const controller = new AbortController()
    this.seeds.add(controller)
    let request: Promise<Ticker[]>
    try {
      request = this.provider.getTickers(symbols, { signal: controller.signal })
    } catch (err) {
      request = Promise.reject(err)
    }
    void request.then(
      (rows) => {
        if (!this.seeds.delete(controller) || this.stopped) return
        this.retries = 0
        let changed = false
        for (const t of rows) {
          if (this.watched.has(t.symbol) && this.apply(t)) changed = true
        }
        if (changed) this.markDirty()
      },
      (err: unknown) => {
        if (!this.seeds.delete(controller) || this.stopped) return
        const retryAfter = err instanceof MarketDataError ? (err.retryAfter ?? 0) : 0
        if (retryAfter > 0) this.blockedUntil = this.now() + retryAfter * 1000
        if (this.retries >= SEED_RETRY_DELAYS_MS.length) return
        const delay = Math.max(SEED_RETRY_DELAYS_MS[this.retries], retryAfter * 1000)
        this.retries += 1
        this.scheduleRetry(delay)
      },
    )
  }

  /** Later, seed whichever watched symbols still have no data. */
  private scheduleRetry(delay: number): void {
    if (this.stopped || this.retryTimer !== null) return
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      this.seed([...this.watched].filter((s) => !this.tickers.has(s)))
    }, delay)
  }

  /** Emit soon: at once if the last emit was long enough ago, else at the end of the window. */
  private markDirty(): void {
    if (this.stopped) return
    this.dirty = true
    if (this.emitTimer !== null) return
    const wait = this.lastEmit + this.throttleMs - this.now()
    if (wait <= 0) {
      this.flush()
      return
    }
    this.emitTimer = setTimeout(() => {
      this.emitTimer = null
      if (this.dirty) this.flush()
    }, wait)
  }

  private flush(patch: Partial<TickersSnapshot> = {}): void {
    if (this.stopped) return
    if (this.emitTimer !== null) clearTimeout(this.emitTimer)
    this.emitTimer = null
    const next: TickersSnapshot = {
      tickers: this.dirty ? new Map(this.tickers) : this.snapshot.tickers,
      moves: this.dirty ? new Map(this.moves) : this.snapshot.moves,
      status: this.snapshot.status,
      ...patch,
    }
    this.dirty = false
    this.lastEmit = this.now()
    this.snapshot = next
    this.listener(next)
  }
}

function sameTicker(a: Ticker, b: Ticker): boolean {
  return (
    a.last === b.last &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.volume === b.volume &&
    a.quoteVolume === b.quoteVolume &&
    a.changePct === b.changePct
  )
}

/** Decimal places implied by an exchange tick size (0.01 → 2, 1 → 0, 1e-8 → 8). */
export function tickDecimals(tickSize: number): number | null {
  if (!Number.isFinite(tickSize) || tickSize <= 0) return null
  for (let d = 0; d <= 12; d++) {
    const scaled = tickSize * 10 ** d
    if (Math.abs(scaled - Math.round(scaled)) < 1e-9 * Math.max(1, scaled)) return d
  }
  return 12
}

/** Fallback decimals by magnitude when the tick size is unknown. */
function magnitudeDecimals(value: number): number {
  const abs = Math.abs(value)
  if (abs >= 1000) return 2
  if (abs >= 1) return abs >= 10 ? 2 : 4
  if (abs >= 0.01) return 5
  if (abs === 0) return 2
  return 8
}

/** en-US fixed-decimal formatters by decimal count (0-12), shared by every row and tick. */
const fixedFormats = new Map<number, Intl.NumberFormat>()

/** Same text as `value.toLocaleString('en-US', { min/max fraction digits = decimals })`. */
function formatFixed(value: number, decimals: number): string {
  let f = fixedFormats.get(decimals)
  if (f === undefined) {
    f = new Intl.NumberFormat('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
    fixedFormats.set(decimals, f)
  }
  return f.format(value)
}

/**
 * Watchlist price text: tick-size decimals when known (else by magnitude), with thousands
 * separators. "—" for non-finite values.
 */
export function formatTickerPrice(value: number, tickSize?: number): string {
  if (!Number.isFinite(value)) return '—'
  const decimals =
    (tickSize !== undefined ? tickDecimals(tickSize) : null) ?? magnitudeDecimals(value)
  return formatFixed(value, decimals)
}

/** Signed absolute 24h change in the same decimals as the price, never "-0.00". */
export function formatTickerChange(last: number, open: number, tickSize?: number): string {
  if (!Number.isFinite(last) || !Number.isFinite(open)) return '—'
  const decimals =
    (tickSize !== undefined ? tickDecimals(tickSize) : null) ?? magnitudeDecimals(last)
  const diff = Number((last - open).toFixed(decimals))
  if (diff === 0) return (0).toFixed(decimals)
  const text = formatFixed(Math.abs(diff), decimals)
  return `${diff > 0 ? '+' : '-'}${text}`
}

/** "+1.23%", "-0.50%", "0.00%"; "—" when not finite. */
export function formatTickerPct(pct: number): string {
  if (!Number.isFinite(pct)) return '—'
  const rounded = Number(pct.toFixed(2))
  if (rounded === 0) return '0.00%'
  return `${rounded > 0 ? '+' : ''}${rounded.toFixed(2)}%`
}

/** Colour of a row: follows the rounded % change the user reads. */
export function tickerDirection(pct: number): 'up' | 'down' | 'flat' {
  if (!Number.isFinite(pct)) return 'flat'
  const rounded = Number(pct.toFixed(2))
  return rounded > 0 ? 'up' : rounded < 0 ? 'down' : 'flat'
}
