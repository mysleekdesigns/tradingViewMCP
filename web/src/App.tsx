import { useMemo } from 'react'
import { Chart } from './chart/Chart'
import { Header } from './components/Header'
import { summarizeQuote, formatUtcClock } from './components/quote'
import { StatusBar } from './components/StatusBar'
import { ErrorView, LoadingView } from './components/StatusViews'
import { BinanceProvider } from './data/binance'
import { useLiveCandles } from './state/useLiveCandles'
import type { Interval } from './types'

const provider = new BinanceProvider()

const SOURCE = 'Binance.US'
const SYMBOL = 'BTCUSDT'
const SYMBOL_DESCRIPTION = 'Bitcoin / TetherUS'
const INTERVAL: Interval = '1h'
const LIMIT = 500

function App() {
  const live = useLiveCandles(provider, SYMBOL, INTERVAL, LIMIT, SOURCE)
  const { load } = live

  const candles = load.kind === 'ready' ? load.candles : null
  // The last candle may still be forming; the quote is a display value only.
  const quote = useMemo(() => (candles ? summarizeQuote(candles, INTERVAL) : null), [candles])

  return (
    <div className="app">
      <Header
        symbol={SYMBOL}
        description={SYMBOL_DESCRIPTION}
        interval={INTERVAL}
        source={SOURCE}
        quote={quote}
        status={live.status}
        loading={load.kind === 'loading' || live.refreshing}
        onRefresh={live.reload}
        refreshDisabled={live.retryBlocked}
      />
      <main className="workspace">
        {load.kind === 'ready' && (
          <Chart candles={load.candles} symbol={SYMBOL} interval={INTERVAL} source={SOURCE} />
        )}
        {load.kind === 'loading' && (
          <LoadingView label={`Loading ${SYMBOL} ${INTERVAL} from ${SOURCE}…`} />
        )}
        {load.kind === 'error' && (
          <ErrorView
            title={load.title}
            message={load.message}
            onRetry={live.reload}
            retryDisabled={live.retryBlocked}
          />
        )}
      </main>
      <StatusBar
        barCount={candles ? candles.length : null}
        status={live.status}
        lastUpdate={live.lastUpdate !== null ? formatUtcClock(new Date(live.lastUpdate)) : null}
        refreshError={load.kind === 'ready' ? live.refreshError : null}
      />
    </div>
  )
}

export default App
