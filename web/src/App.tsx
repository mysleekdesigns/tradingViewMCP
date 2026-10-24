import { useEffect, useState } from 'react'
import { Chart } from './chart/Chart'
import { BinanceProvider } from './data/binance'
import { MarketDataError } from './data/provider'
import type { Candle, Interval } from './types'

const provider = new BinanceProvider()

const SYMBOL = 'BTCUSDT'
const INTERVAL: Interval = '1h'
const LIMIT = 500

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string; retryAfter?: number }
  | { status: 'ready'; candles: Candle[] }

function describeError(err: unknown, symbol: string): string {
  if (err instanceof MarketDataError) {
    switch (err.kind) {
      case 'bad-symbol':
        return `Unknown symbol "${symbol}"`
      case 'network':
        return 'Network error — check connection'
      case 'rate-limited':
        return 'Rate limited by the exchange — wait before retrying'
      case 'banned':
        return 'The exchange has temporarily banned this IP — wait before retrying'
      case 'region-blocked':
        return 'This exchange is not available in your region'
      default:
        return `Could not load market data${err.status !== undefined ? ` (HTTP ${err.status})` : ''}`
    }
  }
  return 'Could not load market data'
}

function App() {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [retryBlocked, setRetryBlocked] = useState(false)

  useEffect(() => {
    let stale = false
    const controller = new AbortController()
    provider.getCandles(SYMBOL, INTERVAL, LIMIT, undefined, { signal: controller.signal }).then(
      (candles) => {
        if (!stale) setState({ status: 'ready', candles })
      },
      (err: unknown) => {
        if (stale) return
        const retryAfter = err instanceof MarketDataError ? err.retryAfter : undefined
        setState({ status: 'error', message: describeError(err, SYMBOL), retryAfter })
        setRetryBlocked(retryAfter !== undefined && retryAfter > 0)
      },
    )
    return () => {
      stale = true
      controller.abort()
    }
  }, [attempt])

  // Keep Retry disabled for the Retry-After window of a 429/418.
  const retryAfter = state.status === 'error' ? state.retryAfter : undefined
  useEffect(() => {
    if (!retryBlocked || retryAfter === undefined) return
    const id = setTimeout(() => setRetryBlocked(false), retryAfter * 1000)
    return () => clearTimeout(id)
  }, [retryBlocked, retryAfter])

  const retry = () => {
    setState({ status: 'loading' })
    setAttempt((n) => n + 1)
  }

  return (
    <main className="app">
      {state.status === 'ready' && (
        <Chart candles={state.candles} symbol={SYMBOL} interval={INTERVAL} />
      )}
      {state.status === 'loading' && (
        <div className="app__status" role="status">
          Loading {SYMBOL} {INTERVAL}…
        </div>
      )}
      {state.status === 'error' && (
        <div className="app__status app__status--error" role="alert">
          <p>{state.message}</p>
          <button type="button" onClick={retry} disabled={retryBlocked}>
            Retry
          </button>
        </div>
      )}
    </main>
  )
}

export default App
