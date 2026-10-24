import { applyLiveCandle, mergeCandles, prependOlder } from '../data/candleStore'
import { plottableCandles } from '../data/plottable'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle, ConnectionStatus, Interval } from '../types'

/** What the chart area shows: a skeleton, an error card, or the candles. */
export type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; title: string; message: string; retryAfter?: number }
  | {
      kind: 'ready'
      /**
       * Every bar the exchange returned, traded or not (Binance.US also returns flat
       * zero-volume bars for intervals without trades). The pagination cursor, gap fills, and
       * the quote use these.
       */
      candles: readonly Candle[]
      /**
       * The bars the chart plots: `candles` without the bars that had no trade
       * (`plottableCandles`). Elements are the same objects as in `candles`; the array keeps
       * its identity while a tick does not change it. May be empty while `candles` is not.
       */
      bars: readonly Candle[]
    }

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
  /** An older-history page request (loadOlder) is running. */
  loadingOlder: boolean
  /** The exchange returned nothing older than the first bar on screen; loadOlder is a no-op. */
  historyExhausted: boolean
  /** An older page failed recently; loadOlder is a no-op until the short cooldown ends. */
  olderCooldown: boolean
}

export interface LiveCandlesOptions {
  provider: MarketDataProvider
  symbol: string
  interval: Interval
  limit: number
  /** Data source name used in error messages, e.g. "Binance.US". */
  source: string
  /**
   * Most bars (raw, traded or not) loadOlder grows the history to, unless called with
   * `beyondLimit` (e.g. a Strategy Tester history load). Unlimited when omitted.
   */
  maxBars?: number
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
  loadingOlder: false,
  historyExhausted: false,
  olderCooldown: false,
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

/** True when `rawCount` loaded bars reach `maxBars` (never when there is no limit). */
export function atHistoryLimit(rawCount: number, maxBars: number | undefined): boolean {
  return maxBars !== undefined && rawCount >= maxBars
}

/** Block used for a 429 without a usable Retry-After header (seconds). */
export const DEFAULT_RATE_LIMIT_BLOCK_S = 60
/** Block used for a 418 (IP ban) without a usable Retry-After header (seconds). */
export const DEFAULT_BAN_BLOCK_S = 120

/**
 * Seconds to block requests after `err`: its Retry-After on a 429/418, a default when that
 * header is missing or unusable, and 0 for any other error.
 */
function retryAfterSeconds(err: unknown): number {
  if (!(err instanceof MarketDataError)) return 0
  const banned = err.kind === 'banned' || err.status === 418
  const limited = err.kind === 'rate-limited' || err.status === 429
  if (!banned && !limited) return 0
  const { retryAfter } = err
  if (retryAfter !== undefined && Number.isFinite(retryAfter) && retryAfter > 0) return retryAfter
  return banned ? DEFAULT_BAN_BLOCK_S : DEFAULT_RATE_LIMIT_BLOCK_S
}

const INTERVAL_SECONDS: Record<Interval, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3_600,
  '4h': 14_400,
  '1d': 86_400,
  '1w': 604_800,
}
/** Binance weekly bars open on Monday 00:00 UTC; the UNIX epoch was a Thursday. */
const WEEK_OFFSET_S = 4 * 86_400

/** Open time (UNIX seconds) of the bar containing `nowSec`. */
function barOpenTime(nowSec: number, interval: Interval): number {
  const len = INTERVAL_SECONDS[interval]
  const offset = interval === '1w' ? WEEK_OFFSET_S : 0
  return Math.floor((nowSec - offset) / len) * len + offset
}

function browserOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

type FetchReason = 'initial' | 'reload' | 'gap-fill'

/**
 * Automatic retries of a failed gap fill, and the delay before each (ms). Also the
 * escalating cooldown after failed older pages (capped at the last entry).
 */
export const GAP_FILL_RETRY_DELAYS_MS: readonly number[] = [2_000, 4_000, 8_000]

/** Largest page the exchange returns per klines request. */
export const MAX_PAGE = 1000

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
  /**
   * In-flight older-history page. Separate from `inflight` so live ticks and gap fills do
   * not cancel it; reload, a replaced window, and stop() abort it.
   */
  private olderInflight: { controller: AbortController; token: number } | null = null
  private nextOlderToken = 0
  /** The current refreshError was set by a failed older page (cleared by the next success). */
  private refreshErrorFromOlder = false
  /** Consecutive failed older pages; sets the cooldown length. */
  private olderFailures = 0
  /** Running while loadOlder is cooling down after a failed page; loadOlder is a no-op. */
  private olderCooldownTimer: ReturnType<typeof setTimeout> | null = null
  /** Wall-clock time (ms) of the last live tick received. */
  private lastTickAt: number | null = null
  /**
   * The bar live-ticked since the last REST apply, and whether a closed (x: true) tick was
   * seen for it. REST rows are authoritative, so a REST apply resets this to null.
   */
  private liveBar: { time: number; closed: boolean } | null = null

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
      this.unsubscribe = provider.subscribeCandles(
        symbol,
        interval,
        (c, closed) => this.onCandle(c, closed),
        {
          onStatus: (status) => this.onStatus(status),
        },
      )
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

  /**
   * Fetch one page of candles older than the oldest loaded bar (traded or not) and prepend
   * it. No-op unless candles are loaded and no older page is running, history is not
   * exhausted, no Retry-After window is active, the cooldown after a failed page has passed,
   * and the browser is not offline, so repeated calls yield one request at a time. Also a
   * no-op once `maxBars` raw bars are loaded, unless `beyondLimit` is true.
   */
  loadOlder(beyondLimit = false): void {
    if (!this.started || this.stopped) return
    if (browserOffline() || this.olderCooldownTimer !== null) return
    const { load, loadingOlder, historyExhausted, retryBlocked } = this.snapshot
    if (load.kind !== 'ready' || load.candles.length === 0) return
    if (!beyondLimit && atHistoryLimit(load.candles.length, this.opts.maxBars)) return
    if (loadingOlder || historyExhausted || retryBlocked || this.olderInflight !== null) return

    const controller = new AbortController()
    const token = ++this.nextOlderToken
    this.olderInflight = { controller, token }
    this.update({ loadingOlder: true })

    const { provider, symbol, interval, limit } = this.opts
    const page = Math.min(limit, MAX_PAGE)
    // Page from the oldest RAW bar, never the oldest plotted one: a page of bars without
    // trades adds no plotted bar, and paging from the plotted one would refetch it forever.
    const endTime = load.candles[0].time - 1
    let request: Promise<Candle[]>
    try {
      request = provider.getCandles(symbol, interval, page, endTime, {
        signal: controller.signal,
      })
    } catch (err) {
      request = Promise.reject(err)
    }
    void request.then(
      (older) => this.onOlderFetched(token, older),
      (err: unknown) => this.onOlderFailed(token, err),
    )
  }

  /** Abort a running older page and drop its late result. */
  private cancelOlder(): void {
    if (this.olderInflight === null) return
    this.olderInflight.controller.abort()
    this.olderInflight = null
    if (this.snapshot.loadingOlder) this.update({ loadingOlder: false })
  }

  private isCurrentOlder(token: number): boolean {
    return !this.stopped && this.olderInflight !== null && this.olderInflight.token === token
  }

  private onOlderFetched(token: number, older: Candle[]): void {
    if (!this.isCurrentOlder(token)) return
    this.olderInflight = null
    this.resetOlderCooldown()
    const { load } = this.snapshot
    if (load.kind !== 'ready') {
      this.update({ loadingOlder: false })
      return
    }
    // Ticks during the request only touched the right edge; prepend onto what is on screen.
    const candles = prependOlder(load.candles, older)
    const clearError = this.refreshErrorFromOlder
    this.refreshErrorFromOlder = false
    if (candles === load.candles) {
      this.update({
        loadingOlder: false,
        historyExhausted: true,
        ...(clearError ? { refreshError: null } : {}),
      })
      return
    }
    this.update({
      load: this.ready(candles),
      loadingOlder: false,
      lastUpdate: this.now(),
      ...(clearError ? { refreshError: null } : {}),
    })
  }

  private onOlderFailed(token: number, err: unknown): void {
    if (!this.isCurrentOlder(token)) return
    this.olderInflight = null
    if (err instanceof MarketDataError && err.kind === 'aborted') {
      this.update({ loadingOlder: false })
      return
    }
    // Escalating cooldown so a failing (e.g. offline) request is not repeated per drag frame.
    const step = Math.min(this.olderFailures, GAP_FILL_RETRY_DELAYS_MS.length - 1)
    this.olderFailures += 1
    if (this.olderCooldownTimer !== null) clearTimeout(this.olderCooldownTimer)
    this.olderCooldownTimer = setTimeout(() => {
      this.olderCooldownTimer = null
      this.update({ olderCooldown: false })
    }, GAP_FILL_RETRY_DELAYS_MS[step])
    const blockFor = retryAfterSeconds(err)
    const error = describeError(err, this.opts.symbol, this.opts.source)
    this.refreshErrorFromOlder = true
    this.update({
      loadingOlder: false,
      olderCooldown: true,
      refreshError: error.title,
      ...(blockFor > 0 ? { retryBlocked: true } : {}),
    })
    if (blockFor > 0) this.startRetryBlock(blockFor)
  }

  /** Unsubscribe, abort in-flight requests, and drop late results. Idempotent. */
  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.unsubscribe?.()
    this.unsubscribe = null
    this.inflight?.controller.abort()
    this.inflight = null
    this.olderInflight?.controller.abort()
    this.olderInflight = null
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = null
    this.clearGapRetry()
    this.resetOlderCooldown()
    this.pendingGapFill = false
    this.pending.clear()
  }

  private resetOlderCooldown(): void {
    this.olderFailures = 0
    if (this.olderCooldownTimer !== null) clearTimeout(this.olderCooldownTimer)
    this.olderCooldownTimer = null
    if (this.snapshot.olderCooldown) this.update({ olderCooldown: false })
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

  /** Ready state for `candles`; reuses the plotted array on screen when it is unchanged. */
  private ready(candles: readonly Candle[]): LoadState {
    const { load } = this.snapshot
    const prev = load.kind === 'ready' ? load.bars : null
    return { kind: 'ready', candles, bars: plottableCandles(candles, prev) }
  }

  private update(patch: Partial<LiveCandlesSnapshot>): void {
    this.emit({ ...this.snapshot, ...patch })
  }

  private onCandle(c: Candle, closed = false): void {
    if (this.stopped) return
    this.lastTickAt = this.now()
    const { load } = this.snapshot
    if (load.kind !== 'ready') {
      this.pending.set(c.time, c)
      return
    }
    this.trackRollover(load.candles, c, closed)
    if (this.inflight !== null) this.pending.set(c.time, c)
    const candles = applyLiveCandle(load.candles, c)
    if (candles === load.candles) return
    this.update({ load: this.ready(candles), lastUpdate: this.now() })
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
      if (this.noBarMissed(load.candles)) return

      this.gapFillRetries = 0
      this.requestGapFill()
    } else if (load.kind === 'error' && !this.snapshot.retryBlocked) this.fetch('reload')
  }

  /**
   * A tick opened a new bar while the previous live-ticked bar never got its closed tick:
   * its final values were missed (e.g. during a silent reconnect), so repair them over REST.
   * Fires once per rollover; the first tick after a REST apply never fires.
   */
  private trackRollover(candles: readonly Candle[], c: Candle, closed: boolean): void {
    const prev = this.liveBar
    if (prev !== null && c.time <= prev.time) {
      // Same bar (or a late tick for an older one): only remember a closed tick.
      if (c.time === prev.time) prev.closed ||= closed
      return
    }
    const lastTime = candles.length > 0 ? candles[candles.length - 1].time : null
    this.liveBar = { time: c.time, closed }
    if (prev === null || lastTime === null || c.time <= lastTime) return
    if (prev.time === lastTime && !prev.closed) {
      this.gapFillRetries = 0
      this.requestGapFill()
    }
  }

  /**
   * True when no bar can have been missed during the drop: the last tick was under one
   * interval ago and the bar open now is still the last candle on screen. The next tick
   * carries that bar's cumulative OHLCV, so no REST gap fill is needed.
   */
  private noBarMissed(candles: readonly Candle[]): boolean {
    if (this.lastTickAt === null || candles.length === 0) return false
    const nowMs = this.now()
    const { interval } = this.opts
    if (nowMs - this.lastTickAt >= INTERVAL_SECONDS[interval] * 1000) return false
    return barOpenTime(Math.floor(nowMs / 1000), interval) === candles[candles.length - 1].time
  }

  private fetch(reason: FetchReason, isRetry = false): void {
    this.inflight?.controller.abort()
    // A newer request supersedes any scheduled or pending gap-fill retry.
    this.clearGapRetry()
    this.pendingGapFill = false
    if (!isRetry) this.gapFillRetries = 0
    // A reload or initial load may replace the window; a stale older page must not land on
    // it. A gap fill keeps the older page running (it only aborts it if it replaces).
    if (reason !== 'gap-fill') this.cancelOlder()
    if (reason === 'reload') this.resetOlderCooldown()
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
    let replaced: boolean
    if (load.kind !== 'ready') {
      candles = fresh
      replaced = true
    } else {
      // If the fresh window starts after the older candles on screen without sharing a bar
      // with them, bars may be missing in between (live ticks during the outage only add
      // the newest bars). A clean recent window beats a silent hole: replace, don't merge.
      const disjoint =
        fresh.length > 0 && load.candles.length > 0 && !overlapsStart(load.candles, fresh[0].time)
      candles = disjoint
        ? fresh
        : keepUnchangedIdentity(load.candles, mergeCandles(load.candles, fresh))
      replaced = disjoint
    }
    // A different window: an older page computed for the old one is stale, and the new
    // window's history has not been probed yet.
    if (replaced) {
      this.cancelOlder()
      this.resetOlderCooldown()
    }
    // Ticks that arrived during the request are newer than (or as new as) its rows.
    const ticks = [...this.pending.values()].sort((a, b) => a.time - b.time)
    this.pending.clear()
    for (const c of ticks) candles = applyLiveCandle(candles, c)
    this.liveBar = null
    this.update({
      load: this.ready(candles),
      refreshing: false,
      refreshError: null,
      lastUpdate: this.now(),
      ...(replaced ? { historyExhausted: false } : {}),
    })
    this.refreshErrorFromOlder = false
  }

  private onFetchFailed(token: number, err: unknown): void {
    if (!this.isCurrent(token) || this.inflight === null) return
    const { reason } = this.inflight
    this.inflight = null
    const blockFor = retryAfterSeconds(err)
    this.refreshErrorFromOlder = false
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
    if (blockFor > 0) this.startRetryBlock(blockFor)
    if (reason === 'gap-fill' && load.kind === 'ready') this.scheduleGapRetry(blockFor > 0)
  }

  /**
   * (Re)start the Retry-After window: retryBlocked is already set by the caller; it clears
   * after `seconds`, then a deferred gap fill runs.
   */
  private startRetryBlock(seconds: number): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      this.update({ retryBlocked: false })
      if (this.pendingGapFill && !this.stopped && this.snapshot.load.kind === 'ready') {
        this.fetch('gap-fill', true)
      }
      this.pendingGapFill = false
    }, seconds * 1000)
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
