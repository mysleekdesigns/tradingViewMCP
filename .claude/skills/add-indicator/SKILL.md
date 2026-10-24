---
name: add-indicator
description: Add a technical indicator to the web app end to end - pure compute function, registry entry, overlay or pane rendering, settings inputs, incremental tail recompute, a Vitest known-value test, and a no-look-ahead test. Use when the user asks to add an indicator (EMA, RSI, MACD, ATR, Supertrend, ...) to the chart.
argument-hint: [indicator-name]
---

# Add indicator: $ARGUMENTS

Requires Phase 4's registry (`web/src/indicators/`). If it does not exist yet, say so and offer to build the registry first (PLAN.md Phase 4, step 1).

1. **Check first**: is `$ARGUMENTS` already in the registry? Does `technicalindicators` provide it? Prefer a small hand-written TS function when the library's output alignment (leading values dropped) would complicate the time mapping.
2. **Compute** in `web/src/indicators/<id>.ts`: a pure function `(candles: Candle[], inputs) => Record<lineName, { time: number; value: number }[]>`. Rules:
   - Output points carry the **candle's own `time`**; warm-up bars produce no point (not 0, not NaN).
   - Value at bar t uses candles ≤ t only (no centred windows).
   - Inputs have defaults matching the common convention (state it in a comment, e.g. RSI 14 Wilder smoothing).
3. **Registry entry**: `id`, `name`, `placement: 'overlay' | 'pane'`, `inputs` with defaults, default colours, and for pane indicators any guide lines (RSI 30/70, Stochastic 20/80).
4. **Incremental update**: on a live tick, recompute only the tail (enough bars to cover the longest lookback plus smoothing state) and `series.update()` the last point.
5. **Tests** in `web/src/indicators/<id>.test.ts`:
   - Known values: compare against a published example or hand calculation; state the tolerance and why.
   - Warm-up: first output time equals candle `period - 1` (or the documented offset).
   - No look-ahead: change candles after index t, assert outputs ≤ t are identical.
   - Tail recompute equals full recompute on the last bar.
6. **Render**: overlay → `LineSeries` on pane 0; pane → `addSeries(..., paneIndex)`; histograms (MACD) → `HistogramSeries` with per-point colour. Follow the `lightweight-charts-v5` skill. Removing the indicator removes its series and empty pane.
7. Run `npx vitest run src/indicators/<id>.test.ts` and `npx tsc --noEmit` in `web/`. Then ask `quant-reviewer` to check the maths if it is non-trivial (smoothing, MACD, Supertrend).

Report: files changed, test output, and the manual check for the owner ("add <name> from the menu on BTCUSDT 1h and compare with TradingView's values on the same bar").
