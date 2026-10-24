import { applyLiveCandle, mergeCandles } from '../data/candleStore'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle, ConnectionStatus, Interval } from '../types'

/** What the chart area shows: a skeleton, an error card, or the candles. */
export type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; title: string; message: string; retryAfter?: number }
  | { kind: 'ready'; candles: readonly Candle[] }

export interface LiveCandlesSnapshot {
  load: LoadState
  /** WebSocket connection state, as reported by the provider. */
  status: ConnectionStatus
  /** A REST reload or post-reconnect gap fill is running while the chart stays on screen. */
  refreshing: boolean
  /** Title of the last failed refresh; the candles already on screen are kept. */
  refreshError: string | null
  /** True during the Retry-After window of a 429/418: reload() is ignored until it ends. */
  retryBlocked: boolean
  /** Wall-clock time (ms since epoch) of the last applied REST result or live tick. */
  lastUpdate: number | null
}

export interface LiveCandlesOptions {
  provider: MarketDataProvider
  symbol: string
  interval: Interval
  limit: number
  /** Data source name used in error messages, e.g. "Binance.US". */
  source: string
  /** Injectable clock for tests. */
  now?: () => number
}

export type LiveCandlesListener = (snapshot: LiveCandlesSnapshot) => void

export const initialSnapshot: LiveCandlesSnapshot = {
  load: { kind: 'loading' },
  status: 'connecting',
  refreshing: false,
  refreshError: null,
  retryBlocked: false,
  lastUpdate: null,
}

/** User-facing title and message for a failed REST load. */
export function describeError(
  err: unknown,
  symbol: string,
  source: string,
): { title: string; message: string } {
  if (err instanceof MarketDataError) {
    switch (err.kind) {
      case 'bad-symbol':
        return {
          title: `Unknown symbol "${symbol}"`,
          message: `${source} does not list this pair. Check the symbol and try again.`,
        }
      case 'network':
        return {
          title: 'Network error — check connection',
          message: `Could not reach ${source}. Check your internet connection, then try again.`,
        }
      case 'rate-limited':
        return {
          title: 'Rate limited by the exchange',
          message:
            'Too many requests in a short time. The button unlocks when it is safe to retry.',
        }
      case 'banned':
        return {
          title: 'Temporarily blocked by the exchange',
          message: 'The exchange has paused requests from this IP. Wait before trying again.',
        }
      case 'region-blocked':
        return {
          title: 'Not available in your region',
          message: `${source} refuses requests from this location. A different data provider is needed.`,
        }
      default:
        return {
          title: 'Could not load market data',
          message: `The exchange returned an unexpected response${err.status !== undefined ? ` (HTTP ${err.status})` : ''}.`,
        }
    }
  }
  return { title: 'Could not load market data', message: 'Something went wrong. Try again.' }
}

/**
 * Reuse the previous object for every bar whose values did not change, so a gap fill that
 * only touched the tail lets the chart use `series.update()` instead of `setData()`.
 */
function keepUnchangedIdentity(prev: readonly Candle[], next: Candle[]): Candle[] {
  const byTime = new Map<number, Candle>()
  for (const c of prev) byTime.set(c.time, c)
  for (let i = 0; i < next.length; i++) {
    const n = next[i]
    const p = byTime.get(n.time)
    if (
      p !== undefined &&
      p !== n &&
      p.open === n.open &&
      p.high === n.high &&
      p.low === n.low &&
      p.close === n.close &&
      p.volume === n.volume
    ) {
      next[i] = p
    }
  }
  return next
}

/**
 * True when a fresh window starting at `start` joins `existing` without a possible hole:
 * it starts at or before the first bar, or at a bar that is already there.
 */
function overlapsStart(existing: readonly Candle[], start: number): boolean {
  if (start <= existing[0].time) return true
  return existing.some((c) => c.time === start)
}

type FetchReason = 'initial' | 'reload' | 'gap-fill'

/** Automatic retries of a failed gap fill, and the delay before each (ms). */
export const GAP_FILL_RETRY_DELAYS_MS: readonly number[] = [2_000, 4_000, 8_000]

/**
 * Framework-free owner of one symbol/interval's candles: REST snapshot + WebSocket ticks,
 * gap fill after reconnects, and manual reloads. Changing symbol or interval means stop()
 * and a new controller. After stop() the listener is never called again.
 */
export class LiveCandlesController {
  private readonly opts: LiveCandlesOptions
  private readonly listener: LiveCandlesListener
  private readonly now: () => number
  private snapshot: LiveCandlesSnapshot = initialSnapshot
  private started = false
  private stopped = false
  private unsubscribe: (() => void) | null = null
  /** In-flight REST request; a newer request aborts and replaces it. */
  private inflight: { controller: AbortController; token: number; reason: FetchReason } | null =
    null
  private nextToken = 0
  /**
   * Live ticks received while a REST request is in flight (or before the first success),
   * keyed by bar time so only the newest tick per bar is kept. Re-applied on top of the
   * REST result, so an older REST row never overwrites a newer tick.
   */
  private pending = new Map<number, Candle>()
  /** The socket dropped since the last 'live'; the next 'live' triggers a gap fill. */
  private dropped = false
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  /** A gap fill was wanted during a Retry-After block; it runs when the block ends. */
  private pendingGapFill = false
  /** Automatic retries already used by the current gap fill. */
  private gapFillRetries = 0
  private gapRetryTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: LiveCandlesOptions, listener: LiveCandlesListener) {
    this.opts = opts
    this.listener = listener
    this.now = opts.now ?? Date.now
  }

  getSnapshot(): LiveCandlesSnapshot {
    return this.snapshot
  }

  /** Subscribe to live candles and load the REST snapshot in parallel. Call once. */
  start(): void {
    if (this.started || this.stopped) return
    this.started = true
    this.emit(initialSnapshot)
    const { provider, symbol, interval } = this.opts
    try {
      this.unsubscribe = provider.subscribeCandles(symbol, interval, (c) => this.onCandle(c), {
        onStatus: (status) => this.onStatus(status),
      })
    } catch {
      this.update({ status: 'offline' })
    }
    this.fetch('initial')
  }

  /**
   * Re-fetch recent candles over REST. With candles on screen they stay while it runs and
   * if it fails; otherwise the loading view shows. Ignored during a Retry-After window.
   */
  reload(): void {
    if (!this.started || this.stopped || this.snapshot.retryBlocked) return
    this.fetch('reload')
  }

  /** Unsubscribe, abort in-flight requests, and drop late results. Idempotent. */
  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.unsubscribe?.()
    this.unsubscribe = null
    this.inflight?.controller.abort()
    this.inflight = null
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = null
    this.clearGapRetry()
    this.pendingGapFill = false
    this.pending.clear()
  }

  private clearGapRetry(): void {
    if (this.gapRetryTimer !== null) clearTimeout(this.gapRetryTimer)
    this.gapRetryTimer = null
  }

  /** Gap fill now, or once the Retry-After block ends. */
  private requestGapFill(): void {
    if (this.snapshot.retryBlocked) {
      this.pendingGapFill = true
      return
    }
    this.fetch('gap-fill')
  }

  private emit(next: LiveCandlesSnapshot): void {
    if (this.stopped) return
    this.snapshot = next
    this.listener(next)
  }

  private update(patch: Partial<LiveCandlesSnapshot>): void {
    this.emit({ ...this.snapshot, ...patch })
  }

  private onCandle(c: Candle): void {
    if (this.stopped) return
    const { load } = this.snapshot
    if (load.kind !== 'ready') {
      this.pending.set(c.time, c)
      return
    }
    if (this.inflight !== null) this.pending.set(c.time, c)
    const candles = applyLiveCandle(load.candles, c)
    if (candles === load.candles) return
    this.update({ load: { kind: 'ready', candles }, lastUpdate: this.now() })
  }

  private onStatus(status: ConnectionStatus): void {
    if (this.stopped) return
    if (status === 'reconnecting' || status === 'offline') this.dropped = true
    const reconnected = status === 'live' && this.dropped
    if (status === 'live') this.dropped = false
    if (status !== this.snapshot.status) this.update({ status })
    if (!reconnected) return
    const { load } = this.snapshot
    // The initial request (still loading) already covers the gap.
    if (load.kind === 'ready') {
      this.gapFillRetries = 0
      this.requestGapFill()
    } else if (load.kind === 'error' && !this.snapshot.retryBlocked) this.fetch('reload')
  }

  private fetch(reason: FetchReason, isRetry = false): void {
    this.inflight?.controller.abort()
    // A newer request supersedes any scheduled or pending gap-fill retry.
    this.clearGapRetry()
    this.pendingGapFill = false
    if (!isRetry) this.gapFillRetries = 0
    const controller = new AbortController()
    const token = ++this.nextToken
    this.inflight = { controller, token, reason }

    const ready = this.snapshot.load.kind === 'ready'
    if (ready)
      this.update({
        refreshing: true,
        refreshError: reason === 'reload' ? null : this.snapshot.refreshError,
      })
    else if (reason !== 'initial') this.update({ load: { kind: 'loading' }, refreshError: null })

    const { provider, symbol, interval, limit } = this.opts
    let request: Promise<Candle[]>
    try {
      request = provider.getCandles(symbol, interval, limit, undefined, {
        signal: controller.signal,
      })
    } catch (err) {
      request = Promise.reject(err)
    }
    void request.then(
      (fresh) => this.onFetched(token, fresh),
      (err: unknown) => this.onFetchFailed(token, err),
    )
  }

  private isCurrent(token: number): boolean {
    return !this.stopped && this.inflight !== null && this.inflight.token === token
  }

  private onFetched(token: number, fresh: Candle[]): void {
    if (!this.isCurrent(token)) return
    this.inflight = null
    this.gapFillRetries = 0
    const { load } = this.snapshot
    let candles: readonly Candle[]
    if (load.kind !== 'ready') {
      candles = fresh
    } else {
      // If the fresh window starts after the older candles on screen without sharing a bar
      // with them, bars may be missing in between (live ticks during the outage only add
      // the newest bars). A clean recent window beats a silent hole: replace, don't merge.
      const disjoint =
        fresh.length > 0 && load.candles.length > 0 && !overlapsStart(load.candles, fresh[0].time)
      candles = disjoint
        ? fresh
        : keepUnchangedIdentity(load.candles, mergeCandles(load.candles, fresh))
    }
    // Ticks that arrived during the request are newer than (or as new as) its rows.
    const ticks = [...this.pending.values()].sort((a, b) => a.time - b.time)
    this.pending.clear()
    for (const c of ticks) candles = applyLiveCandle(candles, c)
    this.update({
      load: { kind: 'ready', candles },
      refreshing: false,
      refreshError: null,
      lastUpdate: this.now(),
    })
  }

  private onFetchFailed(token: number, err: unknown): void {
    if (!this.isCurrent(token) || this.inflight === null) return
    const { reason } = this.inflight
    this.inflight = null
    const retryAfter = err instanceof MarketDataError ? err.retryAfter : undefined
    const blockFor = retryAfter !== undefined && retryAfter > 0 ? retryAfter : 0
    const error = describeError(err, this.opts.symbol, this.opts.source)
    const { load } = this.snapshot
    if (load.kind === 'ready') {
      // Ticks were already applied live; keep the chart that is on screen.
      this.pending.clear()
      this.update({ refreshing: false, refreshError: error.title, retryBlocked: blockFor > 0 })
    } else {
      this.update({
        load: { kind: 'error', ...error, ...(blockFor > 0 ? { retryAfter: blockFor } : {}) },
        refreshing: false,
        refreshError: null,
        retryBlocked: blockFor > 0,
      })
    }
    if (blockFor > 0) {
      if (this.retryTimer !== null) clearTimeout(this.retryTimer)
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null
        this.update({ retryBlocked: false })
        if (this.pendingGapFill && !this.stopped && this.snapshot.load.kind === 'ready') {
          this.fetch('gap-fill', true)
        }
        this.pendingGapFill = false
      }, blockFor * 1000)
    }
    if (reason === 'gap-fill' && load.kind === 'ready') this.scheduleGapRetry(blockFor > 0)
  }

  /** Retry a failed gap fill with increasing delay; never inside a Retry-After block. */
  private scheduleGapRetry(blocked: boolean): void {
    if (this.stopped) return
    if (this.gapFillRetries >= GAP_FILL_RETRY_DELAYS_MS.length) return // give up; error stays
    const delay = GAP_FILL_RETRY_DELAYS_MS[this.gapFillRetries]
    this.gapFillRetries += 1
    if (blocked) {
      // Counts as an attempt; runs when the Retry-After timer unblocks.
      this.pendingGapFill = true
      return
    }
    this.clearGapRetry()
    this.gapRetryTimer = setTimeout(() => {
      this.gapRetryTimer = null
      if (this.stopped) return
      if (this.snapshot.retryBlocked) {
        this.pendingGapFill = true
        return
      }
      this.fetch('gap-fill', true)
    }, delay)
  }
}
