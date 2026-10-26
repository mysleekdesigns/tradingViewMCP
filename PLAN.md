# Local Crypto Charting App — Build Plan

A personal, locally-run charting app in the spirit of TradingView, focused on crypto.
Built from open-source and public pieces only — no copying of TradingView's proprietary client or data feeds.

---

## Goals

- Real-time candlestick charts for crypto pairs (BTC/USDT, ETH/USDT, …)
- Multiple timeframes, watchlist, symbol search
- Technical indicators (overlays + separate panes)
- Drawing tools (trendlines, horizontal lines, Fibonacci)
- Price alerts
- Strategy backtesting
- **AI strategy research agents** that search the web for ideas, backtest them, learn from failures, and refine strategies — with statistical guardrails so "success" means a real edge, not an overfit backtest
- Optional: desktop app wrapper

## Non-goals (for now)

- Stocks / forex (can be added later via another data provider)
- Order execution / live trading
- Multi-user accounts or public hosting

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | **Vite + React + TypeScript** | Fast dev loop, typed data models |
| Charts | **Lightweight Charts v5** (`lightweight-charts`) | Open source (Apache 2.0) by TradingView; candlesticks, panes, plugins |
| Market data | **Binance public REST + WebSocket** | Free, real-time, no API key needed for market data |
| Indicators | **`technicalindicators`** (npm) or hand-written TS | Pure JS, works in browser and Node |
| Backend (Phase 5+) | **Node + Fastify** | Same language as frontend; WebSocket fan-out, alerts, caching |
| Local storage | **SQLite** (`better-sqlite3`) on backend, `localStorage` for UI prefs | Zero-config, single file |
| Backtesting / research lab | **Python + `vectorbt`** (fast sweeps) + **Optuna** (parameter search), served by **FastAPI** | Milliseconds per backtest; one engine shared by the UI and the agents |
| Agent tools | **MCP server** (`strategy-lab`) exposing backtest/ledger tools | Same tools work in interactive Claude Code, headless `claude -p`, and the Agent SDK |
| Agent orchestration | **Claude Agent SDK** (Python) running the research loop | Programmable loop, subagents, budgets, logging |
| Fast judgments | **Jev** (TypeSafe) via `POST /v1/systemone` | Sub-second, typed yes/no / multiple-choice gates (source screening, dedupe, "done?" checks) |
| Desktop (optional) | **Tauri** | Small binaries, wraps the existing web app |

> **Region note:** `api.binance.com` blocks US IP addresses. If you are in the US, use **Binance.US**
> (`api.binance.us` / `wss://stream.binance.us:9443`) or **Coinbase Exchange**
> (`api.exchange.coinbase.com` / `wss://ws-feed.exchange.coinbase.com`). Keep the data layer behind an
> interface (see Phase 1) so switching providers is a one-file change.

---

## Key Data Sources (Binance)

**Historical candles (REST)**
```
GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000
```
Response: array of arrays —
`[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]`
(prices/volumes are strings; max 1000 candles per request — paginate with `endTime` for more history).

**Live candles (WebSocket)**
```
wss://stream.binance.com:9443/ws/btcusdt@kline_1m
```
Each message has `k` with `t` (open time), `o`, `h`, `l`, `c`, `v`, and `x` (true when the candle closed).

**Symbol list**
```
GET https://api.binance.com/api/v3/exchangeInfo
```

**24h tickers (for watchlist prices / % change)**
```
GET https://api.binance.com/api/v3/ticker/24hr
wss://stream.binance.com:9443/ws/!miniTicker@arr
```

Intervals: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`

---

## Research: Existing Trading Bots (as of Oct 2026)

### Open-source bots worth using or learning from

| Bot | License | Best for | Agent hooks | How we use it |
|---|---|---|---|---|
| **Freqtrade** (~55k GitHub stars) | GPL-3.0 | Signal-based crypto strategies, spot + futures; hyperopt; FreqAI (ML) | REST API, Telegram | **Cross-check engine (Phase 7) + paper trading (Phase 8)**. Its dry-run mode is the forward test. |
| **Jesse** | MIT | Clean Python research, backtesting, live trading | **Built-in MCP server** | Alternative lab/cross-check if Freqtrade's GPL license or style doesn't suit |
| **Hummingbot** | Apache-2.0 | Market making, arbitrage, 50+ CEX/DEX connectors | Condor AI harness | Later, if exploring market making (needs low latency and careful tuning) |
| **NautilusTrader** | LGPL-3.0 | Realistic event-driven / order-book backtests | Python API | Alternative cross-check for intraday strategies |
| **OctoBot**, **Superalgos** | GPL / Apache | UI-driven grid/DCA bots, visual design | AI strategy modes | Reference only |

Licensing note: Freqtrade is GPL-3.0. Running it as a separate tool alongside our app is fine. Copying its
code *into* our app would put our app under GPL. Keep it as a separate process called via CLI/REST.

### Are any bots actually successful? What the evidence says

- **There is no independently verified, consistently profitable public retail bot.** Platform
  "leaderboards" and marketplace track records are self-reported, survivorship-biased, and not audited.
- **Typical results are small.** Bitsgap's own H1 2026 data: the *median* closed COMBO bot made **+1.76%**
  (the average, +15.77%, is pulled up by a few leveraged winners). Their headline case study made +149% in a
  week, mostly from one 10x leveraged short. That's what drives the marketing, and it isn't typical.
- **One secondary-source analysis of 500 documented retail bot strategies found only ~3% beat buy-and-hold** after fees, slippage, and subscriptions (unverified, but consistent with everything else).
- **Grid bots** (Pionex, Bitsgap, 3Commas) profit in *sideways* markets and **underperform buy-and-hold in
  strong uptrends** (they sell into the rally). In crashes they end up holding a falling asset. They're a regime bet, not an edge.
- **AI models trading live (Alpha Arena Season 1, late 2025, $10k each, crypto perps):** 4 of 6 frontier
  models lost money (GPT-5 −63%, Gemini 2.5 Pro −57%, Claude Sonnet 4.5 −31%); Qwen3 Max +22%. Losers
  over-traded (Gemini: 165 trades in 9 days, >$1,000 in fees). No Season 2 results had been published as of Aug 2026.
- **Freqtrade community strategies:** Freqle (`freqle.org`) indexes ~14,700 open-source Freqtrade
  strategies with sandbox backtests and look-ahead checks. Its top-ranked entries show 80–95% win rates
  with ~3–6% drawdowns. **Treat those as red flags for overfitting, not proof.** They are excellent
  *test material* for our lab, though.

### What this means for our design

1. **Don't build our own live-trading bot.** Use Freqtrade for paper trading (and, only if you decide to
   later, live trading with trade-only API keys, no withdrawal permission, hard position and daily-loss limits in config).
2. **Use existing strategies as seed material, not as answers.** Import popular Freqtrade strategies
   (official `freqtrade-strategies` repo, top Freqle entries) into the lab and run them through the *same*
   honest pipeline (walk-forward, DSR, PBO, holdout). It's the fastest way to calibrate our tests: we expect most to fail.
3. **Benchmark everything against buy-and-hold BTC and a simple grid bot.** If a strategy can't beat both after costs, it's not worth running.

---

## Research: Quant Trading (as of Oct 2026)

Sources: the `hedgeFund` project (`/Users/simonlacey/Documents/GitHub/hedgeFund`), Lewis Jackson's quant
videos and companion material, and academic/practitioner research found online.

### How quants approach trading

Quants don't read chart patterns. They turn the market into numbers (states, probabilities, factors) and
test whether a rule has a **statistically real, economically explainable edge after costs**. The
professional workflow is:

1. **Hypothesis with a reason**: why should this make money, and who is on the other side of the trade?
   (e.g. "trend persists because investors under-react", "the funding rate pays for providing leverage").
2. **Data**: clean and point-in-time (only what was knowable at the time), with no survivorship.
3. **Signal**: a simple rule or model that turns the data into a forecast.
4. **Portfolio and risk**: position sizing (often volatility targeting), diversification, limits.
5. **Validation**: walk-forward, held-out data, null models, multiple-testing correction.
6. **Execution**: realistic fills and costs. Many "edges" are smaller than the cost of trading them.
7. **Monitoring**: edges decay once published or crowded (**alpha decay**), so live results are tracked against the backtest.

### The quant video and the hedgeFund project

- **Video:** "I Re-Created A Quant Trading Strategy With Claude Code" (Lewis Jackson, May 2026, 27 min,
  [youtube.com/watch?v=ZVMTeDBmSrI](https://www.youtube.com/watch?v=ZVMTeDBmSrI)). It teaches a
  **Markov-chain regime method**: label each day Bull/Bear/Sideways from the trailing 20-day return (±5%),
  count day-to-day transitions into a 3×3 probability matrix, forecast with matrix powers, trade on
  signal = P(Bull) − P(Bear), validate with a walk-forward backtest, and optionally use a Hidden Markov Model instead of fixed thresholds.
- **hedgeFund** re-implements that method and fixes six known weaknesses: overlapping-window bias,
  back-dated chart labels, detection lag, a matrix that drifts over time, an HMM fitted on the wrong inputs,
  and backtests with no costs or significance tests. Phases 0–8 are complete, with 700+ offline tests.
- **What its honest tests found (SPY, daily):** the regime strategy's net Sharpe was **0.22 vs 0.83 for
  buy-and-hold**. It ranked at the **0th percentile** against a permutation null. The best in-sample
  parameter cell (net Sharpe 0.70) dropped to **0.35 on the 2-year holdout**, the same as the default. In
  other words: **no standalone edge**. Its value is as an honest measuring tool and possibly as a
  regime/risk *filter*. That matches the research on AI strategy discovery in Phase 8.

**What we reuse from hedgeFund** (it's your own MIT-licensed code):
- Its statistics and backtest modules: costs with a sensitivity table, benchmarks, permutation null
  through the full pipeline, Probabilistic/Deflated Sharpe, holdout, parameter sensitivity grid, lag
  analysis. Add it to the lab as a local dependency (`uv add --editable ../hedgeFund`) instead of copying
  code. Check which functions assume daily bars or a 252-day year before using them on intraday crypto.
- Its **no-lookahead perturbation test pattern** (change prices after day *t*, assert every output up to *t* is bit-identical) for every lab feature.
- Its **regime engine** as registry features (`regime_signal`, `regime_state`, `hmm_bear_prob`) that agents can use as filters.
- Its **Claude Code setup** as templates for Phase 8: the `honest-backtesting` skill (checklist + report
  template), the `quant-researcher` / `quant-reviewer` agents, and the `trading-safety` / `no-lookahead` rules.

### Lewis Jackson's "Quant Terminal" (the same idea as this project)

Lewis Jackson released a free one-shot Claude Code prompt ("I reverse engineered TradingView", Oct 2026,
[part-timequant.com/tradingview](https://www.part-timequant.com/tradingview)) that builds a local
TradingView-style app on Lightweight Charts with public Binance/Coinbase/Kraken data. The prompt is behind
an email form, and I haven't seen it. **If you get it, read it fully before running it**, as with any pasted
prompt. Its feature list is a useful benchmark for our plan:

| Quant Terminal feature | In our plan? |
|---|---|
| Up to 16 synced charts, any timeframe (1s to 1M, custom) | Multi-chart in Phase 9; add seconds/custom timeframes |
| 87 indicators, 13 drawing tools | Phases 4 and 6 (we start smaller) |
| Alerts with Discord/Telegram/webhooks | Phase 6 |
| Strategy tester with fees/slippage | Phase 7 |
| **Quant checks**: walk-forward, overfitting, deflated Sharpe, **cost stress, Monte Carlo** | Phase 7. Added cost stress and Monte Carlo below |
| **Bar replay** + **paper trading** ($100k pretend account) | **Added to Phase 9** |
| Screener/heatmap, seasonals, economic calendar | Seasonals added to Phase 9; screener optional |

Option: if you'd rather start from a working app, run his prompt in a *separate* folder first and use it as
a reference. Our plan's differentiators are the honest Python lab, the hedgeFund integration, and the AI research agents.

### What the evidence says works in crypto

| Approach | Evidence | Verdict for us |
|---|---|---|
| **Volatility targeting** (scale position size inversely to recent volatility) | One careful test (BTC/ETH/SOL 2017–mid 2025): max drawdown **84% → 17%** with similar Sharpe. A vol-managed equal-weight portfolio with *no* signal had a **Sharpe of 1.24**, beating the trend strategy's 0.79 ([Summitward](https://summitward.com/learn/crypto-trend-following)). Kim, Tse & Wald found much of published trend performance comes from vol scaling | **The most robust tool. Make it a must-beat benchmark and a default overlay** |
| **Time-series momentum / trend following** | Documented in crypto (Liu & Tsyvinski, RFS) but **aged**: the strongest recent test (Grobys et al. 2025, 30 coins, 2016–2023) found it insignificant over the full sample and **negative after July 2020** (per an SSRN review). It adds a small improvement over vol scaling. A simple 200-day long/flat BTC rule: Sharpe 0.89, max drawdown −10% in the same test | Worth testing as a long/flat filter, not a big edge on its own |
| **Cross-sectional momentum** (buy the strongest coins, avoid the weakest) | A priced factor in crypto (Liu, Tsyvinski & Wu, J. Finance; trend factor, JFQA 2025). But ~55% drawdowns, high correlation between coins, and the benefit peaks around 10–15 coins before costs eat it | Phase 8+ candidate; needs a point-in-time multi-coin universe |
| **Carry / basis / funding-rate arbitrage** (long spot, short perpetual or future) | Real, well-studied return source (BIS working paper 1087; funding-rate arbitrage studies on Binance/BitMEX). Market-neutral, but carries exchange, liquidation and basis-blowout risk | Later. Needs futures access, which isn't available to US users on Binance |
| **Short-horizon mean reversion** (bet against the last 15-min candle) | Present in **90% of 183 Binance pairs** out-of-sample, but the gross edge is **~1.3 bp per trade vs ~5 bp round-trip cost** ([arXiv:2608.21888](https://arxiv.org/html/2608.21888v1)) | Real but **not tradable** for retail. A good example of "edge smaller than costs" |
| **Markov/HMM regime signals** (the video's method) | hedgeFund: no standalone edge on SPY after costs and nulls | Use as a filter/feature, not a strategy |
| **Seasonality** ("September is weakest") | Small samples, few cycles | Treat as a hypothesis to test, not a fact |

**Big picture:** in crypto, the dependable wins come from **risk management** (vol targeting, drawdown
control, regime filters, diversification across 10–15 coins), not from clever entry signals. This matches
the MadEvolve finding in Phase 8: LLM search does best on mechanical problems like sizing and risk.

### Five ways crypto backtests fool their authors (build defences into the lab)

1. **Survivorship:** testing today's top coins back through history only tests winners. Use a point-in-time universe (e.g. a coin is eligible only after 1 year of trading history).
2. **Wrong calendar:** crypto trades 365 days a year. Annualizing with 252 days understates volatility by ~17% and inflates Sharpe ratios.
3. **Data gaps:** forward-filling gaps (e.g. delistings or exchange suspensions) creates fake flat periods. Mark gaps as untradeable instead.
4. **Costs that don't scale:** costs must be a percentage of traded value. If a strategy only survives at exactly the assumed cost, the cost assumption is doing the work.
5. **Hidden look-ahead:** volatility warm-up periods, eligibility rules, and "buy-and-hold" benchmarks that secretly rebalance. Defend adversarially: **delay signals** (a real edge decays gradually), **flip signs**, **multiply costs ×2/×4**, and run permutation nulls.

---

## Project Structure (target)

```
tradingView/
├── PLAN.md
├── web/                      # Vite + React frontend
│   ├── src/
│   │   ├── data/             # Market data providers (Binance, Coinbase) behind one interface
│   │   ├── chart/            # Chart wrapper, series, panes, drawing tools
│   │   ├── indicators/       # Indicator calculations + registry
│   │   ├── components/       # Watchlist, toolbar, symbol search, panels
│   │   ├── state/            # App state (selected symbol, timeframe, layout)
│   │   └── types.ts          # Candle, Symbol, Interval types
│   └── package.json
├── server/                   # Phase 5+: Node backend (cache, alerts)
│   ├── src/
│   └── package.json
├── lab/                      # Phase 7+: Python research lab (backtests, ledger, stats)
│   ├── lab/engine/           # vectorbt runner, cost model, walk-forward splits
│   ├── lab/registry/         # Allowed indicators/rules (look-ahead-safe by construction)
│   ├── lab/stats/            # Deflated Sharpe, PBO, bootstrap CIs
│   ├── lab/api.py            # FastAPI endpoints for the UI
│   ├── lab/mcp_server.py     # MCP tools for agents
│   └── pyproject.toml
├── agents/                   # Phase 8: Agent SDK research loop, prompts, Jev gates
│   ├── research_loop.py
│   ├── prompts/
│   └── journal/              # Human-readable research notes per run
└── desktop/                  # Optional Phase 9: Tauri wrapper
```

---

## Phase 0 — Project Setup

**Goal:** an empty app that runs locally.

- [x] Install Node.js 20+ (`node -v` to check)
- [x] `git init` and add a `.gitignore` (`node_modules`, `dist`, `*.db`, `.env`)
- [x] Scaffold frontend: `npm create vite@latest web -- --template react-ts`
- [x] `cd web && npm install && npm install lightweight-charts`
- [x] Run `npm run dev` and confirm the page loads at `http://localhost:5173`
- [x] Add ESLint/Prettier (optional but recommended)
- [x] Decide data provider based on region (Binance / Binance.US / Coinbase) — **Decided 2026-10-24: Binance.US** (`api.binance.us`, `wss://stream.binance.us:9443`); `api.binance.com` returns HTTP 451 from the owner's IP. **Changed 2026-10-24 (Phase 7):** Binance's market-data-only hosts (`data-api.binance.vision`, `wss://data-stream.binance.vision`), because Binance.US was too thin to chart (SUIUSDT 1m: 419/500 bars with no trades) and the lab's archive is Binance global; `TV_MARKET_VENUE=binance-us` switches back.

**Done when:** blank React page runs locally.

---

## Phase 1 — Static Candlestick Chart

**Goal:** draw historical candles for one symbol.

### How
1. Define core types in `src/types.ts`:
   ```ts
   export type Interval = '1m' | '5m' | '15m' | '1h' | '4h' | '1d' | '1w';
   export interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number; } // time in UNIX seconds
   ```
2. Define a provider interface in `src/data/provider.ts`:
   ```ts
   export interface MarketDataProvider {
     getCandles(symbol: string, interval: Interval, limit?: number, endTime?: number): Promise<Candle[]>;
     subscribeCandles(symbol: string, interval: Interval, onCandle: (c: Candle, closed: boolean) => void): () => void;
     getSymbols(): Promise<SymbolInfo[]>;
   }
   ```
3. Implement `BinanceProvider.getCandles` — fetch `/api/v3/klines`, convert strings to numbers, ms → seconds.
4. Create a `<Chart>` component that:
   - creates the chart with `createChart(container, options)`
   - adds a candlestick series: `chart.addSeries(CandlestickSeries, {...})`
   - adds a volume histogram on its own price scale: `chart.addSeries(HistogramSeries, { priceScaleId: '' })`
   - calls `series.setData(candles)`
   - handles resize (`autoSize: true` or a `ResizeObserver`)
   - cleans up with `chart.remove()` on unmount
5. Apply a dark theme (background, grid, text colors) to match the TradingView look.

### Checklist
- [x] `types.ts` with `Candle`, `Interval`, `SymbolInfo`
- [x] `MarketDataProvider` interface
- [x] `BinanceProvider.getCandles` with number parsing and time conversion
- [x] `<Chart>` component with candlesticks + volume
- [x] Crosshair + OHLCV legend showing values under the cursor (`chart.subscribeCrosshairMove`)
- [x] Dark theme
- [x] Chart resizes with the window
- [x] Loading and error states (network failure, bad symbol)

**Done when:** BTCUSDT 1h candles + volume render with a working crosshair legend.

---

## Phase 2 — Real-Time Updates

**Goal:** the last candle moves live; new candles appear when the interval rolls over.

### How
1. Implement `BinanceProvider.subscribeCandles` with a `WebSocket` to `<symbol>@kline_<interval>`.
2. On each message, map `k` → `Candle` and call `series.update(candle)` (updates the last bar or appends a new one).
3. Update the volume series the same way.
4. Return an unsubscribe function that closes the socket; call it when the symbol/interval changes.
5. Reconnect with exponential backoff on `close`/`error`; after reconnecting, re-fetch recent candles via REST to fill any gap.

### Checklist
- [x] WebSocket subscription per symbol/interval
- [x] Live `series.update()` for candles and volume
- [x] Unsubscribe on symbol/interval change (no leaked sockets)
- [x] Auto-reconnect with backoff + gap fill
- [x] Connection status indicator (live / reconnecting / offline)
- [x] Live price line + last price label on the price scale

**Done when:** chart ticks in real time and survives a network drop.

---

## Phase 3 — Navigation: Timeframes, Symbols, Watchlist, History

**Goal:** feel like a real charting app.

### How
- **Timeframes:** toolbar buttons → state change → re-fetch candles + re-subscribe.
- **Symbol search:** load `exchangeInfo` once, filter to `status === 'TRADING'` and quote assets you care about (USDT, USDC, BTC); fuzzy search in a modal (Ctrl/Cmd+K).
- **Watchlist:** sidebar list stored in `localStorage`; live prices + 24h % change from the `!miniTicker@arr` stream (one socket for all symbols).
- **Infinite history:** `chart.timeScale().subscribeVisibleLogicalRangeChange` — when the user scrolls near the left edge, fetch older candles with `endTime = oldestCandle.time*1000 - 1` and `setData([...older, ...existing])`.

### Checklist
- [x] Timeframe toolbar (1m, 5m, 15m, 1h, 4h, 1D, 1W)
- [x] Symbol search modal with keyboard shortcut
- [x] Watchlist sidebar: add/remove/reorder, persisted
- [x] Live watchlist prices + % change (green/red)
- [x] Click watchlist row → load symbol
- [x] Load older history when scrolling left
- [x] Remember last symbol + timeframe across reloads
- [x] Chart types: candles, hollow candles, line, area, Heikin-Ashi (optional)

**Done when:** you can browse any pair on any timeframe and scroll back through history.

---

## Phase 4 — Indicators

**Goal:** add/remove indicators, with settings, in overlay or separate panes.

### How
1. Create an indicator registry:
   ```ts
   interface IndicatorDef {
     id: string;                        // 'ema', 'rsi', ...
     name: string;
     placement: 'overlay' | 'pane';
     inputs: Record<string, number>;    // e.g. { period: 20 }
     compute(candles: Candle[], inputs): { [line: string]: { time: number; value: number }[] };
   }
   ```
2. Overlays (SMA, EMA, Bollinger, VWAP) → `LineSeries` on the main pane.
3. Oscillators (RSI, MACD, Stochastic) → series in a new pane (Lightweight Charts v5 supports panes via the `paneIndex` argument to `addSeries`).
4. On live updates, recompute only the tail (last N candles) rather than the full series, for performance.
5. Settings dialog per indicator (period, source, color); persist active indicators per layout.

### Checklist
- [x] Indicator registry + "Add indicator" menu
- [x] Overlays: SMA, EMA, Bollinger Bands, VWAP
- [x] Pane indicators: RSI (with 30/70 lines), MACD (line + signal + histogram), Stochastic
- [x] Volume moving average
- [x] Settings dialog (inputs + colors)
- [x] Remove/hide indicator from the legend
- [x] Incremental recompute on live ticks
- [x] Unit tests for each indicator against known values (Vitest)

**Done when:** you can stack EMA 20/50 + Bollinger on price and RSI + MACD below, all updating live.

---

## Phase 5 — Local Backend (Cache + Persistence)

**Goal:** move data plumbing to a local server so the app is faster and alerts can run in the background.

### How
1. Scaffold `server/` with Fastify + `@fastify/websocket` + `better-sqlite3`.
2. **Candle cache:** table `candles(symbol, interval, time, o, h, l, c, v, PRIMARY KEY(symbol, interval, time))`. Serve `/api/candles` from SQLite; fetch missing ranges from the exchange and store them.
3. **Stream fan-out:** server holds one upstream exchange socket per symbol/interval and rebroadcasts to all browser tabs.
4. **Persistence:** store watchlists, layouts, drawings, and alerts in SQLite instead of `localStorage`.
5. Add a `ServerProvider` on the frontend implementing the same `MarketDataProvider` interface — swapping is a one-line change.
6. Rate-limit upstream REST calls (Binance weight limits; respect `429` / `Retry-After`).

### Checklist
- [x] Fastify server with health check
- [x] SQLite schema + migrations
- [x] `/api/candles` with cache-fill logic
- [x] `/api/symbols` (cached `exchangeInfo`)
- [x] WebSocket fan-out endpoint
- [x] REST endpoints for watchlists / layouts / drawings
- [x] `ServerProvider` on the frontend
- [x] Upstream rate limiting + retry
- [x] One command to run both: `npm run dev` at root (e.g. with `concurrently`)

**Done when:** reloads are instant from cache and multiple tabs share one exchange connection.

---

## Phase 6 — Drawing Tools & Alerts

**Goal:** annotate charts and get notified on price events.

### Drawing tools — How
- Build with Lightweight Charts' **primitives/plugin API** (`series.attachPrimitive`), or evaluate a community line-tools plugin.
- Each drawing stores anchor points as `{ time, price }` so it stays put when zooming/scrolling.
- Interaction model: pick tool → click anchor(s) → drawing created → select to move/edit/delete.

### Alerts — How
- Alert = `{ symbol, condition: 'crosses_above' | 'crosses_below' | 'greater' | 'less', price, once | every }`.
- Evaluated **on the server** against the live stream so alerts fire even with the browser closed.
- Notify via: browser Notification API (when open), macOS notification, and/or a webhook (Telegram/Discord/Slack).
- Indicator alerts later (e.g. RSI crosses 30).

### Checklist
- [x] Horizontal line + horizontal ray
- [x] Trendline
- [x] Fibonacci retracement
- [x] Rectangle
- [x] Select / move / delete drawings; persisted per symbol
- [x] Alert creation from the chart (right-click price → "Add alert")
- [x] Server-side alert evaluation
- [x] Browser + desktop notifications
- [x] Webhook notifications (optional)
- [x] Alert list with history of triggered alerts

**Done when:** drawings persist across reloads and a price alert pops a notification.

---

## Phase 7 — Backtesting Lab (Python)

**Goal:** a fast, honest backtesting engine that both the UI and the AI agents (Phase 8) use.

> **Why Python instead of a TypeScript engine:** the agents in Phase 8 will run hundreds to thousands of
> backtests. `vectorbt` runs a year of bars in milliseconds and handles parameter sweeps natively, and the
> statistics libraries for overfitting checks are in Python. One engine for everything means the UI shows
> exactly what the agents measured.

### How
1. **Data:** bulk-download historical klines from Binance's public archive (`https://data.binance.vision`,
   monthly zip files per symbol/interval) into Parquet files. Much faster than paging the REST API, and
   it's the same data. Keep the most recent months fresh from the Phase 5 SQLite cache.
2. **Strategies as declarative plans (JSON), not free-form code.** A plan names features from a registry,
   combines them into entry/exit rules, and picks position sizing:
   ```json
   {
     "name": "ema_trend_rsi_pullback",
     "symbol": "BTCUSDT", "interval": "4h",
     "features": { "ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 100}}, "rsi": {"rsi": {"period": 14}} },
     "long_entry": "ema_fast > ema_slow and crosses_below(rsi, 35)",
     "long_exit":  "crosses_above(rsi, 65) or ema_fast < ema_slow",
     "risk": { "stop_loss_pct": 3, "position_size_pct": 100 }
   }
   ```
   Every feature in the registry uses only data up to the current bar, and the engine always fills on the
   **next** bar's open. Look-ahead becomes impossible to express, rather than something agents are asked to avoid.
   (Research finding: a deliberately leaky strategy with a Sharpe of 35 passed every statistical
   overfitting test, so statistics alone can't catch look-ahead. It has to be blocked by design.)
3. **Realistic costs:** taker fee (0.1% Binance spot default), slippage (start with 0.05%, scale with
   volatility), and funding rates if you ever test perpetual futures.
4. **Data splits (the core of honest testing):**
   - **Design window** (e.g. 2019–2023): all searching and tuning happens here.
   - **Validation**: walk-forward windows inside the design period (optimize on N months, test on the next M, roll forward).
   - **Locked holdout** (e.g. the most recent 12 months): **never** visible to search. Checked only for
     final candidates, with a strict budget (e.g. 3 holdout checks per research run).
5. **Trial ledger:** every backtest ever run is recorded in SQLite (plan hash, params, window, metrics).
   Nothing is hidden. This is what lets you correct for "tried 500 things, showed the best one."
6. **Statistics (`lab/stats/`):**
   - **Deflated Sharpe Ratio (DSR)**: adjusts the Sharpe ratio for how many trials were run. The bar rises as the agent searches more.
   - **Probability of Backtest Overfitting (PBO)** via combinatorially symmetric cross-validation (CSCV).
   - Stationary-bootstrap confidence intervals on returns.
   - Always compared against **buy-and-hold BTC**, **volatility-targeted buy-and-hold** (the strongest
     simple benchmark in crypto, see the quant research section), and a **random-entry null** with the same trade count.
   - **Adversarial checks:** signal delay (1, 3, 5 bars; a real edge decays gradually), sign flip, cost
     stress (×2, ×4), Monte Carlo trade-order reshuffles, and a permutation null run through the full pipeline (hedgeFund's method).
   - Annualize with **365 days** (crypto never closes), and use a point-in-time universe with no forward-filled gaps.
   - Reuse `hedgeFund`'s stats/backtest modules as a local dependency where they fit (see the quant research section).
   - Reference implementations: `pypbo`, `quantskills/skill-backtest-overfit` (check the license and test against known values before reusing).
7. **Parameter tuning by Optuna, not by the LLM.** The agent picks the idea and search ranges. Optuna does
   the number-crunching, and every Optuna trial counts toward the trial ledger.
8. **Cross-check engine:** before anything is called "successful," re-run it in an event-driven engine
   (`freqtrade` backtesting or `nautilus_trader`) to catch the vectorbt simplifications (fill timing,
   intrabar stops) that can make results look too good.
9. **UI integration:** FastAPI endpoints so the web app can run backtests, plot trade markers
   (`createSeriesMarkers`) and an equity-curve pane, and browse the ledger.
10. Optional: a regime feature (bull/bear/sideways) using the `markov-hedge-fund-method:regime` skill.

### Checklist
- [x] Binance archive downloader → Parquet (symbol, interval, date range)
- [x] Feature/rule registry with look-ahead-safe implementations + unit tests
- [x] Plan JSON schema + validator with clear error messages (agents fix their own mistakes from these)
- [x] vectorbt engine: next-bar fills, fees, slippage, stop-loss/take-profit
- [x] Design / walk-forward / locked-holdout splits
- [x] Trial ledger (SQLite) recording every run
- [x] Metrics: CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, trades, exposure, turnover
- [x] DSR, PBO, bootstrap CI, buy-and-hold + vol-targeted buy-and-hold + random-entry baselines
- [x] Adversarial checks: signal delay, sign flip, cost stress ×2/×4, Monte Carlo, permutation null
- [x] 365-day annualization; point-in-time universe; gaps marked untradeable (never forward-filled)
- [x] Wire in `hedgeFund` (editable dependency): reuse stats modules + regime features; port its no-lookahead perturbation tests
- [x] Optuna parameter search wired to the ledger
- [x] Event-driven cross-check (freqtrade or nautilus_trader)
- [x] FastAPI endpoints + UI backtest panel (markers, equity pane, metrics table)
- [x] Tests for engine accounting against hand-calculated examples

**Done when:** an EMA-crossover plan on BTCUSDT 4h shows trades on the chart, metrics, its DSR/PBO, and how it compares with buy-and-hold, and every run appears in the ledger.

---

## Phase 8 — AI Strategy Research Agents

**Goal:** agents that look for strategy ideas on the web, test them in the lab, learn from what fails, and
refine strategies until they either **pass strict success criteria** or the agent **concludes there's no edge**.

### What the research says (read this first)

- **Most AI-discovered strategies don't survive honest testing.** A 2026 study ran autonomous LLM
  strategy discovery (GPT-4.1 and Claude Sonnet 5, up to 100 candidates, 5 repeated runs). With
  search-adjusted statistics (DSR, PBO) and held-out data, it **rejected every discovered strategy**, while
  the same tests correctly certified passive buy-and-hold returns
  ([arXiv:2608.27734](https://arxiv.org/html/2608.27734v1)).
- **Autonomy inflates the trial count.** An agent runs dozens of hidden backtests before you see one
  number. "Best of 50" backtests have *negative* expected returns out-of-sample (Bailey, Borwein, López de
  Prado, Zhu). That's why the trial ledger and DSR in Phase 7 are mandatory.
- **LLMs memorize price history.** Models can recall prices from their training period, so backtests over
  those years are inflated ([arXiv:2504.14765](https://arxiv.org/abs/2504.14765), Profit Mirage
  [arXiv:2510.07920](https://arxiv.org/abs/2510.07920)). Mitigations: judge mainly on data after the
  model's training cutoff, forward paper trading, and plans that never mention specific dates or prices.
- **Generated code often implements the wrong strategy.** QuantCode-Bench (2026) found that the main
  failures are wrong trading logic (e.g. `rsi < 30` instead of "RSI *crosses* below 30"), not syntax errors
  ([arXiv:2604.15151](https://arxiv.org/abs/2604.15151)). Declarative plans with named operators like
  `crosses_below` avoid this.
- **Validated tools beat free-form output.** In the same 2026 study, a JSON-emitting critic without
  validation had 60/60 proposals rejected. Through validated tool calls, 80–99% were valid.
- **Ideas from the web are crowded.** Published strategies are known to everyone (and every LLM), so they
  decay quickly ([AlphaAgent, arXiv:2502.16789](https://arxiv.org/abs/2502.16789)). Use web research for
  *hypotheses and building blocks*, and push the agent toward originality and simplicity.
- **Where it works best:** LLM-guided search does better on *mechanical* problems with clear scoring
  (execution, position sizing, risk overlays, regime filters) than on pure price prediction (MadEvolve,
  [arXiv:2605.23007](https://arxiv.org/abs/2605.23007)). Expect the agent's best contributions to be
  risk management and filters on top of simple signals.
- **What professional firms build:** Man Group's AlphaGPT uses an idea → implement → evaluate pipeline, with
  AI signals held to the same statistical thresholds as human research. Two Sigma: the bottleneck moves
  from "more ideas" to "**evaluating ideas faster**."

**Practical consequence:** "keep iterating until the strategy is successful" will always eventually find
something that *looks* successful. The loop must have a fixed success bar, a trial budget, and an honest
"no edge found" exit. A run that ends with "no edge found" has worked correctly.

### Claude Code vs Jev — the decision

**Use both, for different jobs. Claude does the thinking and writing; Jev does the fast yes/no checks.**

| | Claude (Claude Code / Agent SDK) | Jev (TypeSafe) |
|---|---|---|
| What it is | Full agent: reasons, searches the web, writes plans and code, explains results | A "decision model": you send `state` + typed questions, it returns probabilities. **It cannot write text or code** |
| Speed | Seconds to minutes per step | ~150–500 ms per call (community/vendor figures), many questions batched in one call |
| Cost | Normal model pricing | Very cheap (reported ~$0.042 per million input tokens, output free; check current pricing) |
| Role here | Researcher, strategy designer, critic, report writer | Cheap gates: screen web sources, flag duplicate ideas, triage results, "is this run really done?" checks, prompt-injection screening of scraped pages |

**Jev cannot replace Claude Code's model.** Claude Code speaks the Anthropic Messages API and Jev has no
chat/completions endpoint. Jev plugs in through its official skill, hooks, MCP, or direct API calls from
our loop. Keep arithmetic, dates, and counting in our own code (TypeSafe documents these as unreliable for Jev).

**Is Claude fast enough?** The LLM isn't the bottleneck if the loop is designed well:
- Backtests run in milliseconds (vectorbt). Parameter tuning is Optuna's job, not the LLM's.
- One agent iteration (read results → propose a new plan) takes ~10–60 s. That's fine: the LLM should
  make *few, thoughtful* proposals, and Optuna/vectorbt should do the high-volume work. Fewer LLM
  proposals also keeps the trial count, and so the DSR bar, lower.
- Run several hypotheses **in parallel** with subagents (e.g. one each for trend, mean reversion, volatility breakout).
- Use Jev where many small judgments are needed quickly (screening 50 web sources, triaging 200 sweep results).

### Architecture

```
┌───────────────────────── Research Orchestrator (Claude Agent SDK, Python) ─────────────────────────┐
│                                                                                                     │
│  1. Scout (web)  ──►  2. Designer  ──►  3. Lab run  ──►  4. Critic  ──►  5. Decide                   │
│  WebSearch/fetch      writes plan       MCP tools        reads metrics     refine / new idea /        │
│  + Jev source gate    JSON (validated)  vectorbt+Optuna  + Jev triage      holdout check / stop       │
│                                                                                                     │
└──────────────┬──────────────────────────────┬───────────────────────────────┬──────────────────────┘
               │                              │                               │
        Idea library (SQLite)        strategy-lab MCP server            Research journal (Markdown)
        sources, hypotheses,         validate_plan · run_backtest ·     what was tried, why it failed,
        dedupe hashes                run_sweep · walk_forward ·         lessons learned
                                     get_ledger · holdout_check (gated)
```

**Why an MCP server is the centre:** the same `strategy-lab` tools work from:
1. **Interactive Claude Code**: you chat ("try a volatility-breakout idea on ETH 1h") and watch it work.
2. **Headless `claude -p`**: scripted or scheduled runs (e.g. a nightly research job).
3. **Agent SDK program**: the full autonomous loop with budgets, subagents, and logging.

Start with (1). Graduate to (3) once the tools are trustworthy.

### The research loop in detail

0. **Prioritized hypothesis families** (from the quant research): risk overlays and volatility targeting →
   long/flat trend filters → regime filters (hedgeFund features) → cross-sectional momentum across 10–15
   coins → carry/funding (only once futures data is available). Agents start at the top of this list.
   **Seed (first runs):** import popular open-source Freqtrade strategies (official repo + top Freqle
   entries) and translate them into plans. Running them through the pipeline calibrates the tests and gives the agent a library of known failures to learn from.
1. **Scout:** search the web (academic papers, quant blogs, forums) for strategy ideas for the target market.
   Jev screens each source in one batched call (`is_specific_rule`, `has_evidence`, `is_marketing_hype`,
   `contains_injected_instructions`). Claude turns the survivors into **hypotheses** with an economic
   rationale ("trend persistence after volatility compression"). Store them in the idea library with source links.
2. **Dedupe:** Jev checks the new hypothesis against the library ("same as an idea already tested?"), so effort isn't spent re-testing.
3. **Design:** Claude writes a plan JSON using only registry features. The validator returns errors and
   Claude fixes them. **Simplicity limits:** max ~4 features, max ~6 tunable parameters (AlphaAgent-style
   complexity control).
4. **Lab run (design window only):** quick backtest → Optuna search over Claude's chosen ranges → walk-forward validation. Everything goes in the ledger.
5. **Critic:** a separate Claude subagent (fresh context, sceptical brief) reviews the metrics: Is the edge
   concentrated in a few trades? Does it only work in one regime? Is it just leveraged buy-and-hold? Is the
   parameter surface smooth or a lucky spike? Jev does fast triage on large sweep result sets.
6. **Decide:**
   - Refine (change the logic, not just the parameters) → back to step 3, with lessons written to the journal.
   - Abandon → record why, move to the next hypothesis.
   - Promising → **holdout check** (budget-limited). Pass → candidate. Fail → the hypothesis is dead (no re-tuning on the holdout).
   - Budget exhausted → write the "no edge found" report.
7. **Report:** a Markdown summary per run: ideas tried, failures and reasons, candidates with full stats, and the ledger size used for DSR.

### Success criteria (defined *before* a run, never changed mid-run)

A strategy is a **candidate** only if **all** of these hold (starting thresholds; tune them to your risk appetite):
- [ ] Walk-forward out-of-sample Sharpe ≥ 1.0 *after* fees and slippage
- [ ] **Deflated Sharpe Ratio ≥ 0.95**, computed with the run's full trial count
- [ ] **PBO ≤ 0.2**
- [ ] Beats buy-and-hold **and volatility-targeted buy-and-hold** on risk-adjusted return, and beats the random-entry null
- [ ] Survives the adversarial checks: edge decays gradually under signal delay, stays positive at ×2 costs
- [ ] Max drawdown ≤ 25%; at least ~100 trades (enough to mean something)
- [ ] Profitable in at least 2 of 3 market regimes, or explicitly regime-gated
- [ ] Parameter robustness: neighbouring parameter values (±20%) stay profitable
- [ ] Passes the locked holdout, then matches in the event-driven cross-check engine

A candidate becomes **"successful"** only after **forward paper trading** (Binance Spot Testnet or freqtrade
dry-run) for at least 4–8 weeks with results consistent with its backtest. This is the only test the LLM
can't have memorized.

### Guardrails

- **Budgets per run:** max hypotheses, max LLM proposals, max total backtests, max holdout checks, max $ spend.
- **No live trading by agents.** Agents never get exchange API keys with trade permission. Paper trading only, and any real-money step is a manual decision by you.
- **Agents can't edit the lab.** The engine, registry, stats, and success criteria are read-only to agents (enforce this with Claude Code permissions/hooks), so an agent can't make tests easier to pass.
- **Web content is untrusted:** scraped pages are data, never instructions. Jev or Claude screens them for injected instructions before they reach the designer.
- **New registry features are added by a human** (or proposed by the agent as a PR you review), with look-ahead tests.

### Implementation steps

1. Build the `strategy-lab` **MCP server** on top of Phase 7 (`validate_plan`, `list_features`,
   `run_backtest`, `run_sweep`, `walk_forward`, `get_ledger`, `holdout_check` with a hard budget, `get_report`).
2. Register it with Claude Code (`claude mcp add strategy-lab -- python -m lab.mcp_server`) and do manual
   research sessions to shake out tool bugs and prompt wording.
3. Write a **project skill / CLAUDE.md section** for strategy research: the success criteria, the loop,
   the anti-overfitting rules, and how to write the journal. Start from hedgeFund's `honest-backtesting`
   skill, `quant-researcher` / `quant-reviewer` agents, and `trading-safety` / `no-lookahead` rules in
   `../hedgeFund/.claude/`, and adapt them for crypto and intraday bars.
4. Add **Jev**: install the official skill (`claude plugin marketplace add typesafe-ai/skills` →
   `claude plugin install typesafe@typesafe-ai`), get a `TYPESAFE_API_KEY`, and implement the four gates
   (source screen, dedupe, sweep triage, done-check). Make every gate **fail open**: if Jev is down, fall back to Claude, never block.
5. Build the **Agent SDK orchestrator** (`agents/research_loop.py`) with Scout, Designer, and Critic
   subagents, budgets, and resumable state. Run it headless for long runs.
6. Add a **Research page** in the web UI: live run progress, idea library, ledger, journal, candidates
   (click a candidate → see it on the chart with trade markers).
7. **Paper-trading runner** for candidates (freqtrade dry-run or Binance testnet) with a weekly
   backtest-vs-live comparison.
8. Optional: schedule nightly research runs (`/schedule` cloud routine or cron + `claude -p`).

### Checklist
- [x] `strategy-lab` MCP server with all tools + tests
- [x] Holdout check enforces its budget server-side (agents can't bypass it)
- [x] Research skill / CLAUDE.md instructions
- [x] Manual interactive research session completed end-to-end
- [x] Jev gates (source screen, dedupe, sweep triage, done-check) with fail-open behaviour
- [x] Agent SDK orchestrator with Scout / Designer / Critic subagents
- [x] Per-run budgets (proposals, backtests, holdout checks, $)
- [x] Lab code and success criteria protected from agent edits (permissions/hooks)
- [x] Idea library + dedupe
- [x] Seed importer: Freqtrade strategies → plans, run through the honest pipeline as a calibration baseline
- [x] Research journal + end-of-run report (including "no edge found"), written Obsidian-friendly: YAML frontmatter (run ID, idea IDs, date, outcome) and `[[links]]` to idea and ledger run IDs; never records holdout results, and scout-sourced text is marked untrusted
- [x] Research page in the UI (candidate → chart browser-checked 2026-10-25 with a temporary stand-in candidate, since no real one exists yet)
- [x] Paper-trading runner + forward-vs-backtest comparison
- [x] Prompt-injection screening for scraped content

**Done when:** an autonomous run on BTCUSDT/ETHUSDT finishes within budget and produces either a candidate
that passes every success criterion (and then enters paper trading) or a clear, well-documented "no edge
found" report, with every trial in the ledger.

### Costs and practical notes

- **Claude usage:** a research run with dozens of proposals plus web research can use a lot of tokens. Use
  a strong model (Opus 5.5 / Sonnet 5.5) for Designer and Critic, and Haiku 5.5 for summarising web pages.
  Community reports suggest headless runs use subscription limits faster than interactive use. For long
  autonomous runs, an API key with a spend cap may be more predictable. Check Anthropic's current plan
  terms for Agent SDK use.
- **Jev:** cheap per call, but the economics depend on batching many questions into one call. Install the
  official skill so the agent learns to do that. Jev, its skill, and the community plugins are new
  (Sept–Oct 2026), so pin versions and re-check the docs (`docs.typesafe.ai`) when implementing.
- **Expect most runs to find nothing.** That's the honest base rate. Value comes from quickly ruling out bad
  ideas, and from occasional robust risk overlays.

---

## Phase 9 — Polish & Desktop App (Optional)

### Checklist
- [ ] Multi-chart layouts (1, 2, 4 charts) with optional symbol/crosshair sync
- [ ] **Bar replay**: step or play history forward bar by bar (also a manual look-ahead check for indicators)
- [ ] **Manual paper trading** panel: pretend balance, market/limit orders, positions, P&L, on live or replayed prices
- [ ] Seconds and custom timeframes (aggregate from trades/1m data)
- [x] Seasonals view (monthly/weekday return tables per symbol)
- [ ] Regime ribbon overlay from hedgeFund's engine (filtered, never back-dated)
- [x] Keyboard shortcuts (timeframe keys, Esc to cancel drawing, Delete to remove)
- [x] Light/dark theme toggle
- [x] Settings page (default provider, colors, timezone)
- [x] Screenshot/export chart as PNG (`chart.takeScreenshot()`)
- [x] Add a second provider (Coinbase) to prove the provider abstraction
- [ ] Wrap in **Tauri** (`npm create tauri-app`) with the server as a sidecar
- [ ] App icon + menu bar; launch at login (optional)

---

## Testing Strategy

- **Unit (Vitest):** indicator math, data mappers (Binance kline → Candle).
- **Unit (pytest):** lab engine accounting, look-ahead tests for every registry feature (shifting future data must not change past signals), DSR/PBO against published examples.
- **Integration:** provider against live Binance REST (marked slow/optional), server cache-fill logic.
- **Manual checks per phase:** the "Done when" line of each phase.

---

## Risks & Gotchas

| Risk | Mitigation |
|---|---|
| Binance blocked in your region | Use Binance.US or Coinbase via the provider interface |
| Rate limits (HTTP 429/418) | Cache in SQLite, back off on `Retry-After`, batch requests |
| WebSocket drops silently | Heartbeat/timeout detection, reconnect + REST gap fill |
| Time zone confusion | Store UNIX seconds UTC everywhere; format only in the UI |
| Indicator lag on long histories | Incremental tail recompute; compute in a Web Worker if needed |
| Drawing tools are the hardest UI piece | Start with horizontal lines, add tools one at a time |
| Look-ahead bias in backtests | Signals use only closed candles; fill at next open; look-ahead-safe feature registry |
| Agents overfit by searching too much | Trial ledger + Deflated Sharpe + PBO + locked holdout with a budget |
| LLM memorized historical prices | Weight post-training-cutoff data; forward paper trading before calling anything "successful" |
| Agent "fixes" the tests to pass | Lab code and success criteria read-only to agents |
| Prompt injection from scraped web pages | Treat as data; screen with Jev/Claude before use |
| Runaway API spend | Per-run budgets and spend caps |
| Jev ecosystem is very new | Fail-open gates; pin versions; Claude fallback |
| Equity-style assumptions on crypto (252-day year, survivorship, forward-filled gaps) | 365-day annualization, point-in-time universe, gaps marked untradeable |
| hedgeFund code assumes daily bars | Audit before reuse; wrap or extend for intraday |

---

## Milestone Summary

| Phase | Outcome | Rough size |
|---|---|---|
| 0 | App runs locally | < 1 hour |
| 1 | Historical candlestick chart | Small |
| 2 | Live real-time chart | Small |
| 3 | Timeframes, search, watchlist, history | Medium |
| 4 | Indicators | Medium |
| 5 | Local backend + cache | Medium |
| 6 | Drawing tools + alerts | Large |
| 7 | Backtesting lab (Python, honest stats) | Large |
| 8 | AI strategy research agents (Claude + Jev) | Large |
| 9 | Polish + desktop app | Medium |

**First usable version = end of Phase 3.**
