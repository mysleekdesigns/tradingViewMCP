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

## After Wave 2 (terminal)

- Volume profile, unchanged view: 3 `priceToCoordinate` calls per `updateAllViews` instead of
  2 x rows + 3 (up to ~803); off-screen profiles build no rows; no `timeScale().options()`.
- Market structure and sessions: 100 `updateAllViews` with no view change = 1 geometry build.
- Drawings: a repeat `draw()` does no `withAlpha`, `toLocaleString`, `Intl.NumberFormat` or dash
  allocation; a mouse move over an unchanged view makes zero `timeToLogical` calls in the hit test.
- LWC 5.2.1 calls `updateAllViews` on drags, ticks, hover-id changes, scroll and zoom, but not on
  a crosshair-only move (that repaints the top canvas with `draw()` only). So the layer caches pay
  off on drag/tick frames; the drawings' `draw()` cleanup is what the crosshair trace shows.
- Known exceptions (owner to accept or reject):
  - No layer batches strokes by colour: regrouping translucent/overlapping shapes changes pixels.
    Each layer keeps paint order and skips only redundant style sets.
  - Volume profile bar spacing from `|x(1) - x(0)|` differs from `options().barSpacing` by up to
    ~1e-9 px; only a coordinate within ~5e-10 of a half-pixel tie could move one device pixel.
  - hline/hray price-axis label text is still formatted per `updateAllViews` (drag/tick frames,
    not per paint).

## After Wave 3 (terminal)

| Case (150k bars) | Before | After |
|---|---|---|
| SMA(500) window | 112 ms | 4.4 ms |
| Bollinger(500) window | 322 ms | 5.1 ms |
| 1000-bar prepend, SMA/BB (finite-window indicators) | 9-18 ms (full reset) | 0.3-0.8 ms (splice, bit-identical to a reset) |
| 1000-bar gap fill (merge + identity keep) | 20.5 ms | 0.6-1.7 ms |
| 1000-bar `prependOlder` | 1.0-1.6 ms | 0.12-0.15 ms |

- EMA, RSI and MACD still reset on a prepend (their seeds depend on all history); a reset is
  ~10-20 ms on 150k bars with the O(1) primitives.
- Running sums: values may differ from the old re-sum in the last bits (owner accepted; a legend
  last-digit tie can flip on ~1-2% of SMA20 bars). A mean-error guard keeps every SMA / Volume MA /
  Stochastic / Bollinger mean within ~1e-10 of the mean after spikes and crashes.
- Server (offline run, real buildApp + fake upstream): cold request logs one
  `upstream klines` line (`missing+fresh`); the warm request with a live hub logs none and is
  served br-compressed. Closed pages carry `&venue=<id>` and get `immutable` + a venue-tagged ETag.
- Coinbase multi-page loads: requests start 125 ms apart and overlap; completion ~2L + (pages-1)
  x 125 ms instead of ~pages x (L + 125 ms).

## After Wave 4 (terminal)

`npx vite build`:

| Chunk | Baseline gzip | After Wave 4 gzip |
|---|---|---|
| main JS | 310.45 KB (single chunk) | 152.70 KB |
| react (+ react-dom, scheduler) | in main | 68.35 KB |
| lightweight-charts | in main | 58.84 KB |
| lwc-accessibility | in main | 14.15 KB |
| lazy panels/dialogs (8 chunks) | in main | 1.2-9.6 KB each |
| main CSS | 23.31 KB | 13.15 KB (+ 6 lazy CSS files) |

- Vendor chunk hashes unchanged across an app-only change (checked twice).
- Fonts: all six used weights kept (owner); sans 400 and mono 400 woff2 preloaded, rewritten to
  the hashed assets in the build.
- React Compiler: `annotation` mode; exactly the 13 shell functions compiled, 0 bailouts; costs
  ~6 KB gzip on main. `infer` would be ~175 KB main (owner deferred).
- Failed lazy loads show an in-slot "Couldn't load ..." card with Retry/Close; hover preloads
  never reload; a real open after a rebuild reloads at most once per 60 s.
