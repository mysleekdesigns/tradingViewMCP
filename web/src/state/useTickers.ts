import { useEffect, useRef } from 'react'
import type { MarketDataProvider } from '../data/provider'
import { initialTickersSnapshot, TickersController, type TickersSnapshot } from './tickers'
import { useController } from './useController'

/**
 * Live 24h tickers for `items`. One ticker socket per provider: changing `items` only seeds
 * the new symbols over REST and drops removed ones; it never reconnects. A provider change
 * (or unmount) stops the controller, which closes the socket and never emits again.
 */
export function useTickers(
  provider: MarketDataProvider,
  items: readonly string[],
): TickersSnapshot {
  const itemsRef = useRef(items)

  useEffect(() => {
    itemsRef.current = items
  }, [items])

  const { snapshot, controllerRef } = useController<TickersController, TickersSnapshot>(
    (emit) => {
      const controller = new TickersController({ provider }, emit)
      controller.setSymbols(itemsRef.current)
      return controller
    },
    [provider],
  )

  useEffect(() => {
    controllerRef.current?.setSymbols(items)
  }, [items, controllerRef])

  return snapshot ?? initialTickersSnapshot
}
