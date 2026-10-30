import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CandleSource } from '../data/timeframeProvider'
import type { Timeframe } from '../types'
import { initialSnapshot, type LiveCandlesSnapshot } from './liveCandles'
import { liveCandlesCache, type LiveCandlesLease } from './liveCandlesCache'

interface Params {
  provider: CandleSource
  symbol: string
  interval: Timeframe
  limit: number
  source: string
  maxBars: number | undefined
}

interface Entry {
  params: Params
  lease: LiveCandlesLease
  snapshot: LiveCandlesSnapshot
}

export interface UseLiveCandles extends LiveCandlesSnapshot {
  /** Re-fetch recent candles over REST (ignored during a Retry-After window). */
  reload: () => void
  /**
   * Fetch one page of older candles and prepend it (no-op while one is running, or once
   * `maxBars` raw bars are loaded unless `beyondLimit` is true).
   */
  loadOlder: (beyondLimit?: boolean) => void
}

function sameParams(a: Params, b: Params): boolean {
  return (
    a.provider === b.provider &&
    a.symbol === b.symbol &&
    a.interval === b.interval &&
    a.limit === b.limit &&
    a.source === b.source &&
    a.maxBars === b.maxBars
  )
}

/**
 * Live candles for one symbol/interval, from the shared controller cache (`liveCandlesCache`):
 * hook instances with equal provider, symbol, interval, limit, source and maxBars share one
 * controller, so one socket and one REST load. A change of any of them releases the old lease
 * (the last release stops that controller, closing its socket and aborting its requests, one
 * macrotask later) and acquires the new one. StrictMode's mount/unmount/mount re-acquires the
 * same controller within that grace. A released lease never delivers snapshots and its
 * reload/loadOlder are no-ops, so a previous pair's data cannot leak into state.
 *
 * Shared controller: `reload` and `loadOlder` act on the market, so every panel showing the
 * same market sees the refresh or the prepended older page.
 *
 * The returned object is stable: the same object while the snapshot and the lease are
 * unchanged, so consumers can compare it by identity (ChartPanel reports it up only on change).
 */
export function useLiveCandles(
  /** A MarketDataProvider (native Intervals) or a TimeframeProvider (any Timeframe). */
  provider: CandleSource,
  symbol: string,
  interval: Timeframe,
  limit: number,
  source: string,
  /** Raw-bar limit for loadOlder (see LiveCandlesOptions.maxBars). */
  maxBars?: number,
): UseLiveCandles {
  const [entry, setEntry] = useState<Entry | null>(null)

  useEffect(() => {
    const params = { provider, symbol, interval, limit, source, maxBars }
    const lease = liveCandlesCache.acquire(params, (snapshot, l) =>
      setEntry({ params, lease: l, snapshot }),
    )
    return () => lease.release()
  }, [provider, symbol, interval, limit, source, maxBars])

  // Until the new controller's first snapshot lands, never show the previous pair's data.
  const current =
    entry !== null &&
    sameParams(entry.params, { provider, symbol, interval, limit, source, maxBars })
      ? entry
      : null
  const lease = current?.lease ?? null
  const reload = useCallback(() => lease?.reload(), [lease])
  const loadOlder = useCallback(
    (beyondLimit?: boolean) => lease?.loadOlder(beyondLimit === true),
    [lease],
  )
  const snapshot = current?.snapshot ?? initialSnapshot
  return useMemo(() => ({ ...snapshot, reload, loadOlder }), [snapshot, reload, loadOlder])
}
