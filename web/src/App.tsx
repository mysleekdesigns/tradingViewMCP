import { useCallback, useEffect, useMemo, useState } from 'react'
import { Chart } from './chart/Chart'
import { Header } from './components/Header'
import { summarizeQuote, formatUtcClock } from './components/quote'
import { StatusBar } from './components/StatusBar'
import { ErrorView, LoadingView } from './components/StatusViews'
import { SymbolSearch } from './components/SymbolSearch'
import { Watchlist } from './components/Watchlist'
import { BinanceProvider } from './data/binance'
import { loadPrefs, savePrefs } from './state/prefs'
import { useLiveCandles } from './state/useLiveCandles'
import { useSymbols } from './state/useSymbols'
import { useWatchlist } from './state/watchlist'
import type { ChartType, Interval } from './types'

const provider = new BinanceProvider()

const SOURCE = 'Binance.US'
const LIMIT = 500

/** True when a keystroke should go to the focused control rather than open the search. */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  // Listboxes/menus handle their own keys (e.g. the chart type menu).
  return target.closest('[role="listbox"], [role="menu"], [role="dialog"]') !== null
}

interface SearchState {
  initialQuery: string
}

function App() {
  const [initial] = useState(loadPrefs)
  const [symbol, setSymbol] = useState(initial.symbol)
  const [interval, setTimeframe] = useState<Interval>(initial.interval)
  const [chartType, setChartType] = useState<ChartType>(initial.chartType)
  const [search, setSearch] = useState<SearchState | null>(null)
  const [watchlistOpen, setWatchlistOpen] = useState(true)

  useEffect(() => {
    savePrefs({ symbol, interval, chartType })
  }, [symbol, interval, chartType])

  const live = useLiveCandles(provider, symbol, interval, LIMIT, SOURCE)
  const { load } = live
  const symbolList = useSymbols(provider)
  const watchlist = useWatchlist()

  const candles = load.kind === 'ready' ? load.candles : null
  // The last candle may still be forming; the quote is a display value only.
  const quote = useMemo(
    () => (candles ? summarizeQuote(candles, interval) : null),
    [candles, interval],
  )
  const info = symbolList.byName?.get(symbol)
  const description = info ? `${info.baseAsset} / ${info.quoteAsset}` : ''

  const openSearch = useCallback((initialQuery = '') => setSearch({ initialQuery }), [])
  const closeSearch = useCallback(() => setSearch(null), [])

  const selectSymbol = useCallback((next: string) => {
    setSymbol(next)
    setSearch(null)
  }, [])

  const toggleWatchlistItem = useCallback(
    (s: string) => (watchlist.has(s) ? watchlist.remove(s) : watchlist.add(s)),
    [watchlist],
  )

  // Ctrl/Cmd+K opens search; typing a letter or digit outside an input opens it prefilled.
  const searchOpen = search !== null
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (searchOpen || e.isComposing || e.defaultPrevented) return
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        openSearch()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (!/^[a-z0-9]$/i.test(e.key) || isTypingTarget(e.target)) return
      e.preventDefault()
      openSearch(e.key.toUpperCase())
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [searchOpen, openSearch])

  return (
    <div className={`app${watchlistOpen ? ' app--with-sidebar' : ''}`}>
      <Header
        symbol={symbol}
        description={description}
        interval={interval}
        onIntervalChange={setTimeframe}
        chartType={chartType}
        onChartTypeChange={setChartType}
        onOpenSearch={() => openSearch()}
        watchlistOpen={watchlistOpen}
        onToggleWatchlist={() => setWatchlistOpen((o) => !o)}
        source={SOURCE}
        quote={quote}
        status={live.status}
        loading={load.kind === 'loading' || live.refreshing}
        onRefresh={live.reload}
        refreshDisabled={live.retryBlocked}
      />
      <main className="workspace">
        {load.kind === 'ready' && (
          <Chart
            candles={load.candles}
            symbol={symbol}
            interval={interval}
            source={SOURCE}
            chartType={chartType}
            onNearLeftEdge={live.loadOlder}
          />
        )}
        {load.kind === 'ready' && live.loadingOlder && (
          <div className="history-hint" role="status">
            <span className="history-hint__spinner" aria-hidden="true" />
            Loading older bars…
          </div>
        )}
        {load.kind === 'loading' && (
          <LoadingView label={`Loading ${symbol} ${interval} from ${SOURCE}…`} />
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
      {watchlistOpen && (
        <div className="sidebar">
          <Watchlist
            provider={provider}
            items={watchlist.items}
            activeSymbol={symbol}
            symbolInfo={symbolList.byName}
            onSelect={selectSymbol}
            onRemove={watchlist.remove}
            onMove={watchlist.move}
            onAdd={() => openSearch()}
          />
        </div>
      )}
      <StatusBar
        barCount={candles ? candles.length : null}
        status={live.status}
        lastUpdate={live.lastUpdate !== null ? formatUtcClock(new Date(live.lastUpdate)) : null}
        refreshError={load.kind === 'ready' ? live.refreshError : null}
      />
      {search !== null && (
        <SymbolSearch
          initialQuery={search.initialQuery}
          symbols={symbolList.symbols}
          error={symbolList.error}
          onRetry={symbolList.retry}
          activeSymbol={symbol}
          source={SOURCE}
          isInWatchlist={watchlist.has}
          onToggleWatchlist={toggleWatchlistItem}
          onSelect={selectSymbol}
          onClose={closeSearch}
        />
      )}
    </div>
  )
}

export default App
