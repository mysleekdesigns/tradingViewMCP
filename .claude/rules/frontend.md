---
paths:
  - "web/**"
---

# Frontend (Vite + React + TypeScript)

- Strict TypeScript. Shared types (`Candle`, `Interval`, `SymbolInfo`) live in `web/src/types.ts`; do not redeclare them. `Candle.time` is UNIX **seconds** UTC; format dates only at render time.
- Components never call an exchange directly. All market data goes through the `MarketDataProvider` interface in `web/src/data/provider.ts`, so swapping Binance / Binance.US / Coinbase / `ServerProvider` is a one-file change.
- Lightweight Charts is **v5**: `chart.addSeries(CandlestickSeries, opts, paneIndex?)`, not the v4 `addCandlestickSeries`. Load the `lightweight-charts-v5` skill before writing chart code.
- Every effect that creates a chart, series, primitive, WebSocket, subscription, or timer returns a cleanup that removes it (`chart.remove()`, unsubscribe, `ws.close()`). Re-subscribing on symbol/interval change must not leak sockets.
- Live ticks call `series.update()`; never `setData()` the whole history per tick. Indicators recompute only the tail on live updates.
- Keep state in `web/src/state/`; UI prefs persist to `localStorage` until Phase 5 moves them to SQLite. Wrap `localStorage` access in try/catch.
- Indicator math lives in `web/src/indicators/` as pure functions (candles in, points out) with Vitest tests against known values.
