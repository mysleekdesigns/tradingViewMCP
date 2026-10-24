import { useCallback, useEffect, useMemo, useState } from 'react'
import { tradableSymbols } from '../components/symbolRank'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { SymbolInfo } from '../types'

export interface UseSymbols {
  /** Tradable symbols (status TRADING, quote USDT/USDC/BTC/USD); null while loading or on error. */
  symbols: readonly SymbolInfo[] | null
  byName: ReadonlyMap<string, SymbolInfo> | null
  error: string | null
  /** Retry after an error (the provider does not cache failed fetches). */
  retry: () => void
}

type State =
  | { provider: MarketDataProvider; symbols: readonly SymbolInfo[]; error: null }
  | { provider: MarketDataProvider; symbols: null; error: string }

function describe(err: unknown): string {
  if (err instanceof MarketDataError) {
    switch (err.kind) {
      case 'rate-limited':
      case 'banned':
        return 'The exchange is rate-limiting requests. Try again shortly.'
      case 'region-blocked':
        return 'This exchange is not available from your location.'
      case 'network':
        return 'Could not reach the exchange. Check your connection.'
      default:
        return err.message
    }
  }
  return err instanceof Error ? err.message : 'Could not load the symbol list.'
}

/** Loads the exchange's symbol list once per provider; the request is aborted on unmount. */
export function useSymbols(provider: MarketDataProvider): UseSymbols {
  const [state, setState] = useState<State | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    provider.getSymbols({ signal: ctrl.signal }).then(
      (all) => {
        if (!ctrl.signal.aborted) setState({ provider, symbols: tradableSymbols(all), error: null })
      },
      (err: unknown) => {
        if (ctrl.signal.aborted) return
        if (err instanceof MarketDataError && err.kind === 'aborted') return
        setState({ provider, symbols: null, error: describe(err) })
      },
    )
    return () => ctrl.abort()
  }, [provider, attempt])

  const current = state !== null && state.provider === provider ? state : null
  const symbols = current?.symbols ?? null
  const byName = useMemo(
    () => (symbols ? new Map(symbols.map((s) => [s.symbol, s] as const)) : null),
    [symbols],
  )
  const retry = useCallback(() => {
    setState(null)
    setAttempt((n) => n + 1)
  }, [])
  return { symbols, byName, error: current?.error ?? null, retry }
}
