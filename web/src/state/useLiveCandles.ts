import { useCallback, useEffect, useState } from 'react'
import type { MarketDataProvider } from '../data/provider'
import type { Interval } from '../types'
import { initialSnapshot, LiveCandlesController, type LiveCandlesSnapshot } from './liveCandles'

interface Params {
  provider: MarketDataProvider
  symbol: string
  interval: Interval
  limit: number
  source: string
}

interface Entry {
  params: Params
  controller: LiveCandlesController
  snapshot: LiveCandlesSnapshot
}

export interface UseLiveCandles extends LiveCandlesSnapshot {
  /** Re-fetch recent candles over REST (ignored during a Retry-After window). */
  reload: () => void
  /** Fetch one page of older candles and prepend it (no-op while one is running). */
  loadOlder: () => void
}

function sameParams(a: Params, b: Params): boolean {
  return (
    a.provider === b.provider &&
    a.symbol === b.symbol &&
    a.interval === b.interval &&
    a.limit === b.limit &&
    a.source === b.source
  )
}

/**
 * Live candles for one symbol/interval. A change of provider, symbol, interval, limit, or
 * source stops the old controller (closing its socket and aborting its requests) and starts
 * a new one. StrictMode's double effect run creates and stops one extra controller; a
 * stopped controller never emits, so its snapshots cannot leak into state.
 */
export function useLiveCandles(
  provider: MarketDataProvider,
  symbol: string,
  interval: Interval,
  limit: number,
  source: string,
): UseLiveCandles {
  const [entry, setEntry] = useState<Entry | null>(null)

  useEffect(() => {
    const params = { provider, symbol, interval, limit, source }
    const controller = new LiveCandlesController(params, (snapshot) =>
      setEntry({ params, controller, snapshot }),
    )
    controller.start()
    return () => controller.stop()
  }, [provider, symbol, interval, limit, source])

  // Until the new controller's first snapshot lands, never show the previous pair's data.
  const current =
    entry !== null && sameParams(entry.params, { provider, symbol, interval, limit, source })
      ? entry
      : null
  const controller = current?.controller ?? null
  const reload = useCallback(() => controller?.reload(), [controller])
  const loadOlder = useCallback(() => controller?.loadOlder(), [controller])
  return { ...(current?.snapshot ?? initialSnapshot), reload, loadOlder }
}
