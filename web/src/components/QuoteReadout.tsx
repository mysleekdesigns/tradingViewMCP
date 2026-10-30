import { memo, useMemo } from 'react'
import { useLiveSelector, type LiveStore, type PanelKey } from '../state/liveStore'
import type { Candle } from '../types'
import { liveQuoteSelector, NO_QUOTE, sameQuote, summarizeQuote, type Quote } from './quote'

/** The header quote: last price, change and its basis (or an empty placeholder). */
export const QuoteView = memo(function QuoteView({ quote }: { quote: Quote | null }) {
  return (
    // No aria-live: once prices stream, it would announce every tick.
    <div className={`quote quote--${quote?.direction ?? 'flat'}`}>
      {quote ? (
        <>
          <span className="quote__price">{quote.price}</span>
          <span className="quote__change">
            {quote.change} ({quote.changePct})
          </span>
          <span className="quote__basis">{quote.basis}</span>
        </>
      ) : (
        <span className="quote__placeholder" aria-hidden="true" />
      )}
    </div>
  )
})

interface QuoteReadoutProps {
  liveStore: LiveStore
  /** The panel whose live candles are quoted (the active chart). */
  panel: PanelKey
  /** Replayed bars to quote instead of the live ones (replay on that chart), or null. */
  replayCandles: readonly Candle[] | null
}

/**
 * The quote as a leaf that reads the live store itself, so a forming-bar tick re-renders only
 * this (and only when the text changes: sameQuote), never the Header around it.
 */
export const QuoteReadout = memo(function QuoteReadout({
  liveStore,
  panel,
  replayCandles,
}: QuoteReadoutProps) {
  const replaying = replayCandles !== null
  // In replay the live candles are hidden: do not follow them.
  const select = useMemo(
    () => (replaying ? NO_QUOTE : liveQuoteSelector(panel)),
    [panel, replaying],
  )
  const liveQuote = useLiveSelector(liveStore, select, sameQuote)
  const replayQuote = useMemo(
    () => (replayCandles ? summarizeQuote(replayCandles, panel.interval) : null),
    [replayCandles, panel.interval],
  )
  return <QuoteView quote={replaying ? replayQuote : liveQuote} />
})
