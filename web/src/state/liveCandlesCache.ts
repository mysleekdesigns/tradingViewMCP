import type { CandleSource } from '../data/timeframeProvider'
import type { Timeframe } from '../types'
import { LiveCandlesController, type LiveCandlesSnapshot } from './liveCandles'

/**
 * Everything that changes what a LiveCandlesController loads. Two panels share a controller
 * only when every field is equal (the provider by object identity: providers are module-level
 * singletons in App.tsx), so panels with a different `limit` or `maxBars` never share.
 */
export interface LiveCandlesParams {
  provider: CandleSource
  symbol: string
  interval: Timeframe
  limit: number
  source: string
  maxBars: number | undefined
}

/** One subscriber's handle on a shared controller. */
export interface LiveCandlesLease {
  /** The latest snapshot of the shared controller. */
  getSnapshot(): LiveCandlesSnapshot
  /**
   * LiveCandlesController.reload on the shared controller. Every panel on the same market
   * sees the refresh (same market, same data). A no-op once this lease is released.
   */
  reload(): void
  /**
   * LiveCandlesController.loadOlder on the shared controller: the older page is prepended for
   * every panel on the same market, including a `beyondLimit` Strategy Tester history load.
   * A no-op once this lease is released.
   */
  loadOlder(beyondLimit: boolean): void
  /** Stop listening; the last release stops the controller (after the grace delay). Idempotent. */
  release(): void
}

export type LiveCandlesLeaseListener = (
  snapshot: LiveCandlesSnapshot,
  lease: LiveCandlesLease,
) => void

export interface LiveCandlesCacheOptions {
  /**
   * Delay between the last release and stopping the controller; a re-acquire within it reuses
   * the running controller. Default 0: one macrotask, enough for StrictMode's effect
   * mount/unmount/mount and for panels remounted in the same commit (layout changes), while a
   * real symbol/interval switch still aborts the old socket and requests at once. Keep it small:
   * a lingering controller holds its socket and its REST requests (and Coinbase throttle slots).
   */
  graceMs?: number
  /** Controller factory (tests). */
  create?: (
    params: LiveCandlesParams,
    listener: (snapshot: LiveCandlesSnapshot) => void,
  ) => LiveCandlesController
}

interface Entry {
  key: string
  controller: LiveCandlesController
  listeners: Set<(snapshot: LiveCandlesSnapshot) => void>
  stopTimer: ReturnType<typeof setTimeout> | null
}

const providerIds = new WeakMap<object, number>()
let nextProviderId = 0

function providerId(provider: CandleSource): number {
  let id = providerIds.get(provider)
  if (id === undefined) {
    id = ++nextProviderId
    providerIds.set(provider, id)
  }
  return id
}

/** Cache key: provider identity plus every other parameter, exact (no normalisation). */
export function liveCandlesKey(p: LiveCandlesParams): string {
  return JSON.stringify([
    providerId(p.provider),
    p.symbol,
    p.interval,
    p.limit,
    p.source,
    p.maxBars ?? null,
  ])
}

/**
 * Ref-counted LiveCandlesController cache: panels showing the same market (same provider,
 * symbol, interval, limit, source and maxBars) share one controller, so one socket and one
 * REST load. Each acquire gets the current snapshot synchronously and every later one; the
 * last release stops the controller after `graceMs` (closing its socket and aborting its
 * requests), and a stopped controller never emits.
 */
export class LiveCandlesCache {
  private readonly entries = new Map<string, Entry>()
  private readonly graceMs: number
  private readonly create: NonNullable<LiveCandlesCacheOptions['create']>

  constructor(opts?: LiveCandlesCacheOptions) {
    this.graceMs = opts?.graceMs ?? 0
    this.create =
      opts?.create ?? ((params, listener) => new LiveCandlesController(params, listener))
  }

  /** Running (or in-grace) controllers, for tests and diagnostics. */
  get size(): number {
    return this.entries.size
  }

  /**
   * Subscribe to the shared controller for `params`, starting it when none is running.
   * `listener` is called synchronously with the current snapshot (the initial snapshot for a
   * fresh controller), then on every emit until the lease is released.
   */
  acquire(params: LiveCandlesParams, listener: LiveCandlesLeaseListener): LiveCandlesLease {
    const key = liveCandlesKey(params)
    let entry = this.entries.get(key)
    const fresh = entry === undefined
    if (entry === undefined) {
      const listeners = new Set<(snapshot: LiveCandlesSnapshot) => void>()
      const controller = this.create(
        {
          provider: params.provider,
          symbol: params.symbol,
          interval: params.interval,
          limit: params.limit,
          source: params.source,
          maxBars: params.maxBars,
        },
        (snapshot) => {
          // Copy: a listener may release (or another acquire) while we fan out.
          for (const l of [...listeners]) l(snapshot)
        },
      )
      entry = { key, controller, listeners, stopTimer: null }
      this.entries.set(key, entry)
    } else if (entry.stopTimer !== null) {
      clearTimeout(entry.stopTimer)
      entry.stopTimer = null
    }

    const shared = entry
    let released = false
    const lease: LiveCandlesLease = {
      getSnapshot: () => shared.controller.getSnapshot(),
      reload: () => {
        if (!released) shared.controller.reload()
      },
      loadOlder: (beyondLimit) => {
        if (!released) shared.controller.loadOlder(beyondLimit)
      },
      release: () => {
        if (released) return
        released = true
        shared.listeners.delete(notify)
        if (shared.listeners.size === 0) this.scheduleStop(shared)
      },
    }
    const notify = (snapshot: LiveCandlesSnapshot) => {
      if (!released) listener(snapshot, lease)
    }
    shared.listeners.add(notify)
    // A fresh controller emits its initial snapshot from start(); a running one is caught up
    // here so a late subscriber never waits for the next tick.
    if (fresh) shared.controller.start()
    else notify(shared.controller.getSnapshot())
    return lease
  }

  private scheduleStop(entry: Entry): void {
    if (entry.stopTimer !== null) clearTimeout(entry.stopTimer)
    entry.stopTimer = setTimeout(() => {
      entry.stopTimer = null
      if (entry.listeners.size > 0) return
      if (this.entries.get(entry.key) === entry) this.entries.delete(entry.key)
      entry.controller.stop()
    }, this.graceMs)
  }
}

/** The app-wide cache used by useLiveCandles. */
export const liveCandlesCache = new LiveCandlesCache()
