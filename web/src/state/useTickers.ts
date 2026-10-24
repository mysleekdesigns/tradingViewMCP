import { useEffect, useRef, useState } from 'react'
import type { MarketDataProvider } from '../data/provider'
import { initialTickersSnapshot, TickersController, type TickersSnapshot } from './tickers'

interface Entry {
  provider: MarketDataProvider
  snapshot: TickersSnapshot
}

/**
 * Live 24h tickers for `items`. One ticker socket per provider: changing `items` only seeds
 * the new symbols over REST and drops removed ones; it never reconnects. A provider change
 * (or unmount) stops the controller, which closes the socket and never emits again.
 */
export function useTickers(
  provider: MarketDataProvider,
  items: readonly string[],
): TickersSnapshot {
  const [entry, setEntry] = useState<Entry | null>(null)
  const controllerRef = useRef<TickersController | null>(null)
  const itemsRef = useRef(items)

  useEffect(() => {
    itemsRef.current = items
  }, [items])

  useEffect(() => {
    const controller = new TickersController({ provider }, (snapshot) =>
      setEntry({ provider, snapshot }),
    )
    controllerRef.current = controller
    controller.setSymbols(itemsRef.current)
    controller.start()
    return () => {
      controller.stop()
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [provider])

  useEffect(() => {
    controllerRef.current?.setSymbols(items)
  }, [items])

  return entry !== null && entry.provider === provider ? entry.snapshot : initialTickersSnapshot
}
