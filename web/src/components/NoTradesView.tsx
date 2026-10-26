import { useEffect, useRef } from 'react'
import type { Candle, Timeframe } from '../types'
import { canLoadOlder, noTradesCopy, shouldAutoLoadOlder, type OlderHistoryState } from './noTrades'
import { EmptyView } from './StatusViews'

interface NoTradesViewProps extends OlderHistoryState {
  symbol: string
  interval: Timeframe
  exchange: string
  /** Loaded bars, traded or not (none traded, or this view would not be shown). */
  raw: readonly Candle[]
  maxBars: number
  /** Request one older page (the controller dedupes and enforces limits). */
  onLoadOlder: () => void
}

/**
 * Every loaded bar had no trade, so the chart has nothing to plot. Say so instead of a blank
 * chart, look further back by itself (a few pages; see `shouldAutoLoadOlder`), and keep a
 * button for more. The chart replaces this view as soon as a traded bar is loaded.
 */
export function NoTradesView(props: NoTradesViewProps) {
  const { raw, onLoadOlder } = props
  const copy = noTradesCopy(props)
  const start = raw.length > 0 ? raw[0].time : null
  /** Requests made since this view appeared or the user last clicked. */
  const pagesRef = useRef(0)
  const canLoad = canLoadOlder(props)

  // Runs when a page landed (start moved back) or a request became possible again (a page
  // finished, a cooldown or Retry-After window ended, the connection came back).
  useEffect(() => {
    if (!shouldAutoLoadOlder(pagesRef.current, canLoad)) return
    pagesRef.current += 1
    onLoadOlder()
  }, [canLoad, start, onLoadOlder])

  const onClick = () => {
    pagesRef.current = 0
    onLoadOlder()
  }

  return (
    <EmptyView
      title={copy.title}
      message={copy.message}
      action={copy.action}
      actionDisabled={copy.actionDisabled}
      note={copy.note}
      onAction={onClick}
    />
  )
}
