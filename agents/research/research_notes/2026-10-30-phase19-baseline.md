---
date: 2026-10-30
phase: 19
commit: 1bc71a1
kind: baseline
---

# Phase 19 baseline (before any Wave 1 change)

Recorded on commit `1bc71a1` (Phase 18 complete, Phase 19 untouched). Terminal-measurable parts
only; the Chrome Performance traces and React Profiler runs from report section 7 are the
owner's browser checks (see "Browser baseline" below).

## Per-tick pure path (`PERF=1 npx vitest run src/perf` in `web/`)

One forming-bar tick through `applyLiveCandle` -> `plottableCandles` -> `planSeriesSync`
(median of 7 rounds x 200 ticks, Node, Apple Silicon):

| History | Time per tick |
|---|---|
| 1,000 bars | 8.5 us |
| 10,000 bars | 59.9 us |
| 150,000 bars | 1,001.4 us |

Linear in history length (report finding A1). Target after Wave 1: flat in N, apart from the
immutable array copy (memcpy) if it stays.

Other per-tick costs:

| Case | Time |
|---|---|
| `aggregateBucket` over 60 base bars (per base tick on an aggregated timeframe) | 1.63 us |
| `parseMiniTickerMessage`, 2,000 symbols (one whole-market ticker push) | 370.4 us |

## Bundle (`npx vite build` in `web/`)

| Asset | Raw | Gzip |
|---|---|---|
| `index-*.js` (single chunk) | 1,022.78 kB | 310.45 kB |
| `index-*.css` (single stylesheet) | 147.20 kB | 23.31 kB |
| Fonts | 6 woff2 + 6 woff (sans 400/500/600, mono 400/500/600) | n/a |

182 modules, one JS chunk, Vite warns about the >500 kB chunk.

## Server

- `server/src/cache/candleCache.ts:173-174`: with no `endTime` the fresh tail range is always
  non-null, so every warm-cache `/api/candles` makes one upstream REST call.
- No `@fastify/compress`, ETag or `Cache-Control` anywhere in `server/src`.
- `/ws/tickers` forwards the whole-market `!miniTicker@arr` to every client unfiltered.

## Browser baseline (owner)

Not recorded from the terminal. The owner can record it at any time from this commit:

```
git worktree add ../tradingView-baseline 1bc71a1
cd ../tradingView-baseline/web && npm ci && npm run dev
```

Then run report section 7: 10 s Chrome Performance trace on BTCUSDT 1m with 2 indicators and
1 trendline (idle, then mouse moving), single and 4-panel; the same on Coinbase BTC-USD 1m;
React Profiler "highlight updates"; 60 s allocation sampling.

## After Wave 1 (terminal)

Same `PERF=1 npx vitest run src/perf` run on the Wave 1 tree:

| History | Baseline | After Wave 1 |
|---|---|---|
| 1,000 bars | 8.5 us | 4.9 us |
| 10,000 bars | 59.9 us | 8.3 us |
| 150,000 bars | 1,001.4 us | 216.8 us |

The scans are gone (lineage registry in `data/candleStore.ts`); what still grows with N is the
immutable array copy in `applyLiveCandle` (~95 us of memcpy at 150k plus its allocation/GC).
Removing it needs a non-copying candle container, which would change the identity semantics
every consumer relies on; not done in this phase.

Other Wave 1 numbers:

| Case | Before | After |
|---|---|---|
| Aggregated-timeframe bucket per base tick (60 bars) | 1.5 us (`aggregateBucket`) | 0.033 us (`RunningBucket`) |
| Binance direct miniTicker push, 2,000 symbols, 10 watched (parsed array) | 380 us | 24 us |
| Server `/ws/tickers` bytes, client watching 2 of 202 symbols (10 pushes) | 299,399 B | 3,295 B (1.10%) |
| Coinbase forming-bar emits | every trade | at most one per 100 ms window per bucket |

Memory note: the lineage registry keeps up to two earlier full-history arrays alive per stream
(about 1.2 MB each at 150k bars), so a 4-panel heap snapshot is a few MB larger.

Behaviour note: Coinbase paper limit fills see the coalesced forming bar (at most 10 closes per
second); a price touched only mid-window by a non-final trade no longer reaches `onPrice`.
