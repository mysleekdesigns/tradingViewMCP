# Phase 19 browser traces (2026-11-01)

How: headless Chrome for Testing 151 driven over the DevTools protocol. A production build
(`vite build --minify false`, so component names survive) was served by `vite preview` on
127.0.0.1:4174, with `/api` proxied to the owner's server on 8787. The setup was the owner's
saved layout (Market Structure, EMA 20, RSI 14) plus one trendline, on BTCUSDT 1m.

Measured per phase:
- CPU profile sampled every 100 µs.
- React commits, from a `__REACT_DEVTOOLS_GLOBAL_HOOK__` shim that uses the same "did this
  component render" rule as React DevTools (PerformedWork flag, bailed-out subtrees skipped).
  "Outside" means a component rendered that is not under ChartPanel.
- Long tasks (PerformanceObserver).
- WebSocket frames.
- Allocation sampling.

Every phase is 10 s unless stated. The harness lives in the session scratchpad and is not
committed. These are historical measurements on one machine, not a guarantee.

## Results

| Scenario | Ticks / 10 s | JS / 10 s (app · React · LWC) | Long tasks | Commits rendering outside ChartPanel |
|---|---|---|---|---|
| Single, 501 bars, idle | 5 kline | 20.4 ms (3.4 · 4.7 · 9.2) | 0 | quote + last-update leaves; Watchlist on ticker frames only |
| Single, crosshair moving 60 Hz | 8 | 188.8 ms (22.7 · 45.0 · 88.2) | 0 | same |
| Single, 20,002 bars, idle | 5 | 79.1 ms (1.9 · 2.7 · 71.3) | 0 | same |
| 4 panels (1m/1h/4h/1d), idle | 20 kline | 47.3 ms (5.1 · 6.2 · 30.1) | 0 | same |
| 4 panels, crosshair | 25 | 236.7 ms | 0 | same |
| Coinbase BTCUSD 1m (direct), idle | 94 forming flushes | 74.7 ms (10.4 · 8.7 · 43.5) | 0 | quote + last-update leaves only |
| Coinbase, crosshair | 88 | 254.5 ms | 0 | same |

- **Shell commits on a forming-bar tick.** Header, SidebarRail, StatusBar and DrawingToolbar
  never rendered in any phase. The components that did render outside ChartPanel are the live
  store's leaf readers (`QuoteView`/`QuoteReadout`, `LastUpdateClock`/`LastUpdateText`), once
  per tick, which is the Wave 1 design. `Watchlist`/`WatchlistRow` rendered exactly once per
  `/ws/tickers` frame (10 per 10 s), never on kline frames.
- **String / `Intl` work while the crosshair moves.** The only hit was `format` with no app
  frame, 0.45–0.65 ms per 10 s: LWC's own axis labels. No app code formats on the paint path.
- **App per-tick work is flat in history length.** App JS was 3.4 ms per 10 s at 501 bars and
  1.9 ms at 20,002 bars, at the same zoom (about 120 bars visible).
- **Lightweight Charts' per-tick work is not flat.** At 20,002 bars it took 71 ms per 10 s and
  allocated about 8 MB in 30 s (two LWC frames, both `rows().map(...)`). Cause, in LWC 5.2.1
  `dist/lightweight-charts.development.mjs` `_internal__makeValid`: any data change, including
  one `series.update()`, sets `_dataInvalidated`, and `_fillRawPoints` then rebuilds every
  series' full item array. 5.2.1 is the latest release (npm, 2026-11-01).
- **LWC alone** (3 series, 130 bars visible, one update per 100 ms), JS + GC per tick:

  | History | Conflation off | Conflation on |
  |---|---|---|
  | 1,000 bars | 1.19 + 0.09 ms | 0.99 + 0.12 ms |
  | 20,000 bars | 7.80 + 0.97 ms | 8.04 + 0.96 ms |
  | 150,000 bars | 58.2 + 14.7 ms | 60.2 + 13.8 ms |

  Conflation makes no difference. At the 150k cap a Coinbase chart (10 flushes/s) would spend
  most of the main thread inside LWC.
- **Allocation, 30 s of idle ticks.** 309 kB sampled at 501 bars, nearly all LWC. 10 MB at
  20,002 bars, of which about 8.3 MB is LWC `_fillRawPoints`. `applyLiveCandle` accounts for
  117 kB, the accepted one-copy residual.
- **Server responses.** Every `/api/candles` response was `content-encoding: br`. Closed-history
  pages (`endTime` set) carry `cache-control: public, max-age=31536000, immutable` and an ETag;
  the live page carries `no-cache`. Zero upstream calls on a warm cache is not observable from
  the browser: the owner checks the server terminal.

## Incident

When the harness closed its tabs, the app's `keepalive` drawing saves reached the owner's server
despite the request interceptor. Two test trendlines (`be66e1e3…`, `1af6500a…`) were written to
BTCUSDT. They were deleted on 2026-11-01, and `GET /api/drawings?symbol=BTCUSDT` is back to `[]`.
The indicator layout was not changed. Next time: run against a separate server (`TV_PORT`,
`TV_DB_PATH` in a temp directory) instead of relying on interception.

## Second pass (2026-11-01): baseline, sharing, server, visual diff

All runs below used a throwaway server (`TV_PORT=8790`, `TV_DB_PATH` in a temp directory) and
production builds served with `vite preview`. Nothing touched the owner's server or database.

### Baseline: pre-Phase-19 build (`1bc71a1`) vs current (`16a5cb4` + PLAN edits)

The baseline ships as one bundle, so only total JS compares like for like.

| 10 s window | Baseline JS | Current JS | Shell components rendering on a tick (baseline → current) |
|---|---|---|---|
| Single, 501 bars, idle | 27.6 ms | 21.4 ms | App, Header, SidebarRail, StatusBar, DrawingToolbar + ~30 icons, every tick → none |
| Single, crosshair | 168.7 ms | 172.0 ms | same → none |
| Single, ~20k bars, idle | 97.1 ms | 78.4 ms | same → none |
| 4 panels, idle | 63.6 ms | 47.8 ms | same → none |
| 4 panels, crosshair | 235.0 ms | 208.6 ms | same → none |
| Coinbase BTCUSD 1m, idle | 79.7 ms | 52.7 ms | same, about 104 renders per 10 s → none |
| Coinbase, crosshair | 269.3 ms | 247.2 ms | same → StatusBar once in 10 s (bar count changed; not per tick) |

No long tasks in any run of either build.

### Shared live controller

| Layout | Build | `/ws/klines` sockets | 1m `/api/candles` loads |
|---|---|---|---|
| 2 panels, both BTCUSDT 1m | baseline | 2 | 2 |
| 2 panels, both BTCUSDT 1m | current (4 runs) | 1 | 1 `start` |
| 4 panels: 1m, 1m, 1m, 1h | current | 2 (1m + 1h) | 1 |

Now and then a second 1m request appears. Its initiator stack is `requestGapFill`: the
controller's recovery fetch after a reconnect status at startup, not a second panel loading.
Coinbase parallel paging is covered by the `CoinbaseProvider parallel candle paging (Phase 19)`
tests: starts are at least 125 ms apart, and it finishes in about (pages − 1) × 125 ms plus one
round trip.

### Server warm cache (throwaway server log, live `/ws/klines` subscriber held)

Five requests 20 s apart across a bar close: upstream calls 1, 0, 0, 1, 0. Inside a bar, a
warm-cache request makes zero upstream calls. The first request after each bar closes makes one
(`missing+fresh`) to store the closed bar: the cache's clock-skew guard stores a bar only once
upstream has the next one.

### Visual diff: `primitives.css` consolidation (pre-Wave-5 `48b862c` vs current)

- **Method:** 17 UI states at 1920×1080. Each state starts from a fresh reload with storage
  reseeded. Both builds were served the same recorded `/api` responses, with WebSocket stubbed,
  `Date` frozen and transitions off.
- **States:** main view, symbol search, more timeframes, chart type, indicators menu, alert
  dialog, layout menu, shortcuts, settings, indicator settings, Seasonals, alerts panel, paper
  panel, Strategy Tester, Research, keyboard focus ring, light theme.
- **Result:** 0 differing pixels in all 17 (threshold: summed RGB difference above 24). The
  Settings PNGs are byte-identical.
- **Sensitivity check:** two screens that differ only by a focus ring gave 316 differing pixels.

### Lazy panels

- All five lazy panels open: Settings, Seasonals, paper trading, Strategy Tester and Research.
  Research shows its own "lab unavailable" state, because the lab was not running.
- With a panel's chunk blocked, the first open reloads the page once, by design (`main.tsx`
  `vite:preloadError`). The second open shows "Couldn't load Settings" / "Couldn't load the
  Strategy Tester" with Retry and Close.
- **Bug:** in Chrome, Retry does not recover after the network returns. Chrome keeps the failed
  dynamic `import()` for the life of the document, so Retry sends no new request and the card
  stays. Possible fix: retry the failed chunk URL with a cache-busting query in `lazyLoad.ts`.
  Not changed; listed for the owner.
