import { useEffect, useMemo, useState } from 'react'
import { Chart } from './chart/Chart'
import { Header } from './components/Header'
import { summarizeQuote, formatUtcClock } from './components/quote'
import { StatusBar } from './components/StatusBar'
import { ErrorView, LoadingView } from './components/StatusViews'
import { BinanceProvider } from './data/binance'
import { MarketDataError } from './data/provider'
import type { Candle, Interval } from './types'

const provider = new BinanceProvider()

const SOURCE = 'Binance.US'
const SYMBOL = 'BTCUSDT'
const SYMBOL_DESCRIPTION = 'Bitcoin / TetherUS'
const INTERVAL: Interval = '1h'
const LIMIT = 500

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; title: string; message: string }
  | { status: 'ready'; candles: Candle[]; loadedAt: string }

function describeError(err: unknown, symbol: string): { title: string; message: string } {
  if (err instanceof MarketDataError) {
    switch (err.kind) {
      case 'bad-symbol':
        return {
          title: `Unknown symbol "${symbol}"`,
          message: `${SOURCE} does not list this pair. Check the symbol and try again.`,
        }
      case 'network':
        return {
          title: 'Network error — check connection',
          message: `Could not reach ${SOURCE}. Check your internet connection, then try again.`,
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
          message: `${SOURCE} refuses requests from this location. A different data provider is needed.`,
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

function App() {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [refreshing, setRefreshing] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [retryBlockedFor, setRetryBlockedFor] = useState(0)
  /** Title of the last failed refresh; the chart from the previous load stays on screen. */
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const retryBlocked = retryBlockedFor > 0

  useEffect(() => {
    let stale = false
    const controller = new AbortController()
    provider.getCandles(SYMBOL, INTERVAL, LIMIT, undefined, { signal: controller.signal }).then(
      (candles) => {
        if (stale) return
        setState({ status: 'ready', candles, loadedAt: formatUtcClock(new Date()) })
        setRefreshing(false)
        setRefreshError(null)
      },
      (err: unknown) => {
        if (stale) return
        const retryAfter = err instanceof MarketDataError ? err.retryAfter : undefined
        const error = describeError(err, SYMBOL)
        // A failed refresh keeps the chart that is already on screen.
        setState((prev) => (prev.status === 'ready' ? prev : { status: 'error', ...error }))
        setRefreshError(error.title)
        setRefreshing(false)
        setRetryBlockedFor(retryAfter !== undefined && retryAfter > 0 ? retryAfter : 0)
      },
    )
    return () => {
      stale = true
      controller.abort()
    }
  }, [attempt])

  // Keep Retry and Refresh disabled for the Retry-After window of a 429/418.
  useEffect(() => {
    if (retryBlockedFor <= 0) return
    const id = setTimeout(() => setRetryBlockedFor(0), retryBlockedFor * 1000)
    return () => clearTimeout(id)
  }, [retryBlockedFor])

  // A refresh keeps the current chart on screen; only a failed or first load replaces it.
  const reload = () => {
    // Clearing first lets a repeat of the same failure be announced again.
    setRefreshError(null)
    if (state.status === 'ready') setRefreshing(true)
    else setState({ status: 'loading' })
    setAttempt((n) => n + 1)
  }

  const candles = state.status === 'ready' ? state.candles : null
  const quote = useMemo(() => (candles ? summarizeQuote(candles, INTERVAL) : null), [candles])

  return (
    <div className="app">
      <Header
        symbol={SYMBOL}
        description={SYMBOL_DESCRIPTION}
        interval={INTERVAL}
        source={SOURCE}
        quote={quote}
        loading={state.status === 'loading' || refreshing}
        onRefresh={reload}
        refreshDisabled={retryBlocked}
      />
      <main className="workspace">
        {state.status === 'ready' && (
          <Chart candles={state.candles} symbol={SYMBOL} interval={INTERVAL} source={SOURCE} />
        )}
        {state.status === 'loading' && (
          <LoadingView label={`Loading ${SYMBOL} ${INTERVAL} from ${SOURCE}…`} />
        )}
        {state.status === 'error' && (
          <ErrorView
            title={state.title}
            message={state.message}
            onRetry={reload}
            retryDisabled={retryBlocked}
          />
        )}
      </main>
      <StatusBar
        barCount={candles ? candles.length : null}
        loadedAt={state.status === 'ready' ? state.loadedAt : null}
        refreshError={state.status === 'ready' ? refreshError : null}
      />
    </div>
  )
}

export default App
