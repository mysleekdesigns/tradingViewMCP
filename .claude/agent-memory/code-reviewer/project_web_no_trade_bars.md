---
name: web-no-trade-bars
description: Zero-volume ("no-trade") Binance.US bars are hidden from the chart (raw vs plotted split) - confirmed design not to re-flag and open gaps from the 2026-10-24 review
metadata:
  type: project
---

Owner-requested fix (2026-10-24): Binance.US returns flat volume-0 klines for intervals without trades; like TradingView they are not plotted.

Confirmed design (verified in code; do not re-flag):
- LoadState.ready = `{candles: raw, bars: plottableCandles(raw, prevBars)}`. `bars` elements are the raw objects; `prev` array is returned when the traded set is unchanged, so an untraded forming bar or an all-empty older page causes no Chart work (planSeriesSync 'none').
- Raw is used for: loadOlder cursor (`candles[0].time - 1`), gap fill/merge, header quote (24h basis works because flat bars carry prev close), lastClose for alert defaults, Strategy Tester historyPlan/firstBarTime. Plotted is used for: Chart series/volume/indicators/legend/drawings TimeIndex/backtest overlay, lastCandleTime (hline), status bar count.
- Plotted bar losing volume -> shorter array -> planSeriesSync 'set' (tested). Mid-history bar becoming traded after a gap fill -> 'set'.
- Chart `historyStart` effect + `nextOlderChain` (MAX_EMPTY_PAGE_CHAIN=5) re-requests only after an all-empty page while near the left edge; any near-left range event resets the count. Drawings at hidden times interpolate between neighbouring plotted bars (timeIndex). Markers on hidden times snap forward to the next plotted bar; equity filtered to plotted times.
- All bars empty -> Chart unmounts and NoTradesView (EmptyView) shows; App-held drawings/indicators survive; remount is a clean lifecycle.

Open gaps (recheck next review):
- Sparse-but-nonzero pairs: each prepend's range-shift event re-fires loadOlder while plotted bars < viewport+50, so the empty-chain cap never applies; no raw-length budget (could reach 100k+ raw 1m bars, O(n) per tick).
- NoTradesView loads one page per click (no auto-chain) and `actionDisabled` ignores retryBlocked/cooldown/offline (silent no-op).
- tradesOnChart uses raw firstBarTime but markers before the first plotted bar are dropped and planFocus uses plotted first -> count/notice mismatch.

**Why:** saves re-deriving which consumer must read raw vs plotted.
**How to apply:** any new consumer of LoadState must pick `candles` (pagination/quote) or `bars` (anything drawn) deliberately; check that first. See [[web-phase7-strategy-tester]], [[web-conventions]].
