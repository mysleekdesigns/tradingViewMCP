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
- [x] Multi-chart layouts (1, 2, 4 charts) with optional symbol/crosshair sync
- [x] **Bar replay**: step or play history forward bar by bar (also a manual look-ahead check for indicators)
- [x] **Manual paper trading** panel: pretend balance, market/limit orders, positions, P&L, on live or replayed prices
- [x] Seconds and custom timeframes (aggregate from trades/1m data)
- [x] Seasonals view (monthly/weekday return tables per symbol)
- [x] Regime ribbon overlay from hedgeFund's engine (filtered, never back-dated)
- [x] Keyboard shortcuts (timeframe keys, Esc to cancel drawing, Delete to remove)
- [x] Light/dark theme toggle
- [x] Settings page (default provider, colors, timezone)
- [x] Screenshot/export chart as PNG (`chart.takeScreenshot()`)
- [x] Add a second provider (Coinbase) to prove the provider abstraction
- [ ] Wrap in **Tauri** (`npm create tauri-app`) with the server as a sidecar — **deferred by the owner 2026-10-26** (needs the Rust toolchain; not planned for now)
- [ ] App icon + menu bar; launch at login (optional) — **deferred by the owner 2026-10-26**

---

## Phase 10 — Multi-Coin Research (cross-sectional momentum, H1)

Why: every single-symbol research run so far ended "no edge found", and the literature review
(`agents/research/reports/Crypto strategy edge research.md`) says the best-supported effect left to
test is cross-sectional momentum among liquid coins (H1: long-only top quintile of the top 20–30
USDT coins by trailing volume, ranked on 28-day return, weekly rebalance). That needs a
survivorship-free, point-in-time multi-coin universe and a portfolio backtest. Items marked
**(owner)** touch lab paths that are read-only while `.claude/lab.lock` exists.

### Checklist
- [x] Point-in-time universe data layer (`lab/lab/data/listing.py`, `universe.py`, `universe_build.py`): every archive symbol including delisted ones, stablecoin/fiat/leveraged exclusions from `lab.toml [universe]`, one year of seasoning restarted after a relisting gap, trailing dollar-volume rank, perturbation-tested
- [x] Daily klines for every selected USDT coin downloaded and the universe table built (`uv run python -m lab.data.universe_build symbols|download|build`), manifest reviewed
- [x] **(owner)** Portfolio backtest in `lab/lab/engine/`: target weights at t's close filled at t+1's open, per-asset costs on traded value, a held coin that stops printing exits at its last close (delisting return), cash when fewer coins qualify
- [x] **(owner)** Cross-sectional features in `lab/lab/registry/` (trailing return rank within the eligible set) with perturbation tests
- [x] **(owner)** Portfolio benchmarks: equal-weight universe, vol-targeted BTC, random-selection null matched on dates, breadth, exposure, turnover, weighting and volatility tercile; `[universe]` added to the research-run fingerprint
- [x] Portfolio plan type through `validate_plan` / `run_backtest` / `walk_forward` / ledger families and success criteria (portfolio benchmarks in `get_report`, universe manifest sha256 frozen into the run, all benchmarks scored from the strategy's first decision row, segment-end exits listed), then one pre-registered H1 run

### Safeguards before the H1 run (added 2026-10-26)

Why: an open February 2026 study of 20-day cross-sectional crypto momentum on 25 liquid coins
(2020–2026, 20 bps one-way costs) found significant alpha in 2020–22, none in 2023–24, and losses
in 2025–26, with cross-sectional dispersion down 38% after 2022; its 1-day reversal had a t-stat of
4.6 out of sample and still lost 28% a year to 138% daily turnover. Turnover and regime are the
first-order risks for H1, so they are measured and controlled before the run, not after
(`agents/research/reports/Project improvement research.md`, section 3).

- [x] **(owner)** Cross-sectional dispersion feature in `lab/lab/registry/cross_section.py` (cross-sectional std of trailing returns among eligible coins at row t, perturbation-tested) and logged per rebalance in the portfolio run so the critic can see whether any edge lives only in high-dispersion periods; optional dispersion gate in the portfolio plan type
- [x] **(owner)** Liquidity-aware weights: cap each coin's target weight by its share of trailing dollar volume (`lab.toml [universe]` setting, frozen into the run fingerprint)
- [x] **(owner)** Rank banding: a coin enters the top quintile only when its rank crosses the boundary by a margin and leaves only when it falls below a wider one (hysteresis), so weekly rebalances do not churn the edge of the quintile
- [x] **(owner)** Participation cost term in `lab/lab/engine/costs.py` for portfolio runs: square-root market impact on traded value divided by the coin's trailing dollar volume, added to the existing fee + volatility-scaled slippage; BTC/ETH single-symbol costs unchanged
- [x] H1 report decomposes returns into alpha and beta to BTC (full period and rolling 60-day), since a long-only top quintile carries most of BTC's beta; the journal says "no edge found" if the alpha is not significant after costs

**Done when:** H1 has been run once, end to end, through the same ledgered pipeline and success
criteria as single-symbol plans, with the dispersion, liquidity and banding safeguards in place,
and the outcome (most likely "no edge found") is written up in the journal.

---

## Phase 11 — Lab Statistics v2 (owner, lab lock)

Why: every item here changes how a result is judged, so every item is owner-only while
`.claude/lab.lock` exists. The lab's Deflated Sharpe Ratio counts *nominal* trials: after three
research runs N is 2,479 plans, which puts the bar near Sharpe 1.7, above anything published for
crypto. A 2026 review of about 92,500 fully costed strategies (50,000 crypto across 20 pairs) found
the 50,000 crypto trials were worth about 434 independent bets once trial correlation was counted,
and the single best strategy had a DSR of 0.029. Counting *effective* trials keeps the bar honest
and reachable. The MIT library `purgedcv` implements effective-N, CPCV paths, PBO, PSR, minimum
backtest length and an Optuna trial recorder, and is a second implementation to test our own
statistics against (report section 1).

### Checklist
- [x] **(owner)** `effective_n_trials` in `lab/lab/stats/`: eigenvalue participation ratio of the design-window return correlation matrix of ledgered plans, per run and across runs, with unit tests on constructed cases (identical plans → 1; independent plans → N) — `lab/lab/stats/effective_n.py` + `lab/lab/research/trial_count.py` (daily grid, unknown pairs independent). Real ledger: 8.78 vs nominal 4,198; a time-shuffled null scores ~1,227, so it is a **diagnostic only** (see next item)
- [x] **(owner)** `get_report` shows nominal N and effective N side by side; `success_criteria.toml` gains a `dsr_trial_count = "nominal" | "effective"` key that is frozen into the ledger at `start_run` (the current runs stay on nominal; the choice is a pre-run decision, never changed mid-run) — key added and frozen at `start_run` (absent = nominal), but **only "nominal" is accepted**: quant review (Critical) showed the participation-ratio N double-counts the shared market factor (24% vs 0.5% DSR false passes on a one-factor null) and falls as trials are added; effective N is shown as `effective_diagnostic` beside its shuffled null. A factor-robust, calibrated estimator v2 is needed before "effective" can be offered
- [x] **(owner)** Minimum backtest length (MinBTL) and minimum track record length (from the Probabilistic Sharpe Ratio) in `get_report`, so a plan with too few bars is rejected for that stated reason — in `get_report.statistics`; when `dsr_min` fails, its note names the binding length ("design window too short ..."). Not a separate criterion row: a MinTRL shortfall always fails DSR (same inversion); a plan that passed DSR below MinBTL would not be rejected by MinBTL alone
- [x] **(owner)** Combinatorial Purged Cross-Validation (CPCV) in `lab/lab/engine/splits.py` as an *additional* validator beside walk-forward: C(N,K) purged, embargoed folds reconstructed into several out-of-sample paths, reported as a distribution of OOS Sharpe per plan; every path's backtests are ledgered; DSR still deflates by configurations searched, not paths — splits/purge/embargo/path assignment in `engine/splits.py`, path Sharpes in `stats/cpcv.py`; built from the behaviour family's already-ledgered sweep trials (no new backtests, N unchanged), reported twice: all trials (labelled an upper bound, TPE saw the test bars) and the random start-up trials
- [x] **(owner)** Second-implementation tests: `purgedcv` added as a dev dependency and `tests/test_stats_crosscheck.py` compares our DSR, PSR, expected-max-Sharpe and CSCV PBO against it on fixed inputs (tolerance documented), the way engine accounting is tested against hand examples — `purgedcv==0.1.11`; also MinTRL, MinBTL and CPCV splits/paths; mutation probes confirm the comparisons catch a selection leak and a raw/excess kurtosis swap
- [x] **(owner)** Run fingerprint (`lab/lab/research/runs.py::lab_values`) records `optuna.__version__`, `vectorbt.__version__`, and the explicit sampler settings (Optuna 5.0 turned multivariate TPE and constant liar on by default), so a rerun months later is comparable — recorded as `config["software"]` beside `lab_values` (inside it would change every existing run's fingerprint and lock them out of `get_report`); sampler built with explicit settings (`make_tpe_sampler`), a mismatch refuses the run; lockfile parity test in agents
- [x] Quant review of the whole phase, then `/run-gate 11` — 2 quant review rounds (round 1: 1 Critical, 4 Warnings fixed); gate PASS: lab 2258 passed, agents 680 passed, ruff clean

**Done when:** `get_report` for an existing ledgered plan shows nominal N, effective N, MinBTL,
MinTRL and a CPCV path distribution; the cross-check tests against `purgedcv` pass; and a research
run started after the change freezes its `dsr_trial_count` choice in the ledger.

---

## Phase 12 — Derivatives Data as Risk Filters

Why: the strategy report's hypothesis H3 (funding and basis as a crash-risk or exposure filter on
spot long-only plans) was parked because it seemed to need futures access. It does not for
research: Binance's public archive (`data.binance.vision`, verified 2026-10-26) holds USDⓈ-M
`fundingRate`, `premiumIndexKlines`, `markPriceKlines`, `indexPriceKlines`, `bookTicker` and
`aggTrades` (monthly) and daily `metrics` (open interest, OI value, top-trader and global
long/short ratios, taker buy/sell ratio at 5-minute resolution) from 2020-09-01, each zip with a
`.CHECKSUM`. No futures account and no non-US IP is needed. Live futures streams for US IPs are a
separate question; nothing in the chart depends on them here (report section 2).

### Checklist
- [x] Archive downloader for `futures/um` `fundingRate`, `premiumIndexKlines`, `markPriceKlines`, `indexPriceKlines` (monthly) and `metrics` (daily) → Parquet under `lab/data/derivatives/` (gitignored), checksums verified, manifest with first/last timestamps per series, gaps recorded as gaps (never forward-filled); extend `lab/lab/data/archive.py` rather than copying it
- [x] Alignment module: each derivatives series aligned to the spot bar's *close* using the archive's own timestamps (a funding settlement or OI row is visible only at bars whose close is at or after its timestamp), unit-tested with a shifted-timestamp case
- [x] **(owner)** Registry features: `funding_rate`, `funding_zscore` (trailing window), `premium_index`, `basis_pct` (mark vs index), `oi_change` (trailing % change), `long_short_ratio`, each with the known-value test and the perturbation (no-look-ahead) test; feature docs say "exposure or crash-risk filter, not a direction signal"
- [x] `validate_plan` accepts the new features on spot long-only plans as gating clauses (e.g. `funding_zscore < 2`), and the derivatives manifest sha256 joins the research-run fingerprint — frozen per run as a per-series derivatives snapshot beside the lab.toml fingerprint (so in-flight runs keep theirs); derivatives plans share one scored start per symbol/interval, and the report adds an ungated-baseline benchmark
- [x] Research page / chart: optional funding and open-interest panes from the lab's Parquet (historical only; no live futures stream) — `POST /lab/derivatives` + header toggle "Funding / OI"; owner browser-checked 2026-10-27
- [x] One pre-registered H3 run through the normal pipeline (criteria and budget written first, `strategy-critic` review, at most the budgeted holdout check), written up in the journal whatever the outcome — run r-20261028-103056-807dd5: no edge found (funding z < 2 entry gate inert, ~5% of entries; both plans fail 9 criteria; holdout unspent)

**Done when:** BTCUSDT funding, premium index and open interest are downloadable, aligned to spot
bars with the perturbation test passing, selectable in a plan, and H3 has been run once end to end
with its result (most likely "no edge found") in the journal.

---

## Phase 13 — Research Agents v2

Why: the Agent SDK (pinned 0.2.174) added structured outputs, nested background subagents with
depth/concurrency caps, `defer` and `updatedToolOutput` hooks, and tool search on by default; some
defaults changed twice in autumn 2026, so caps must be set explicitly. A 2026 study of 856 MCP tools
found 97% of tool descriptions deficient (56% without a stated purpose) and measured a median
+5.85-point task-success gain from adding purpose, guidelines, limitations and parameter
explanations, with compact variants keeping most of the gain. The "Agentic Trading" survey (77
studies) found only 2 of 19 closed-loop studies reported time-consistent splits and 1 of 19 a cost
model, and proposes a seven-item reporting checklist. The forward test also needs a frozen
acceptance rule so "consistent with the backtest" is a number (report sections 4 and 5).

### Checklist
- [x] Structured outputs: Designer returns plan JSON against the plan schema and Critic returns a `{verdict, reasons, evidence}` schema via `output_format={"type": "json_schema", ...}` (draft-07); invalid outputs are retried by the SDK and counted toward the proposal budget; dry-run test covers a deliberately invalid output
- [x] Subagent caps set explicitly in `agents/research/sdk_runner.py`: `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` small, `permission_mode` explicit (already), and a note that `max_budget_usd` counts subagent spend; the dry run asserts the `init` message lists every `strategy-lab` tool (tool search is on by default; set `ENABLE_TOOL_SEARCH=false` for the Designer if any tool is missing)
- [x] Prompt-injection screening moved into a `PostToolUse` hook (`updatedToolOutput`) on the Scout's web tools, so scraped text is screened before the model sees it; Python heuristics and Jev stay as the screen, fail-open as today
- [x] `strategy-lab` MCP tool description audit against the rubric (purpose, when to use, when not to, each parameter, one short example, limits), kept compact; `tests/test_mcp_descriptions.py` checks every tool has each component
- [x] Reporting checklist header in the journal's end-of-run report: data and universe, time split and leakage, action semantics, execution and costs, leakage audit, artifacts, adaptation; each line names the ledger/run fields that prove it
- [x] Per-run ledger hash chain: each trial's record hashes the previous trial's hash; `get_ledger` can verify the chain (tamper evidence for the journal's "every trial in the ledger")
- [x] Idea library failure tags: when a plan is abandoned, the Critic's reason is stored as a tag (`cost_bound`, `regime_only`, `few_trades`, `beta_only`, `parameter_spike`, ...), and the Designer prompt includes the failure tags of the idea's family before proposing
- [x] Pre-registered paper-trade protocol in `agents/paper_trade.py prepare`: writes to the journal, before the first dry-run trade, the expected trade count and exposure with tolerances, a per-trade implementation-shortfall tolerance against the lab's modelled fill, and a minimum-track-record stopping rule from the candidate's claimed Sharpe; `compare` evaluates against that frozen protocol and declares pass / fail / continue; freqtrade `--dry-run` stays the venue (the Binance Spot Testnet retires endpoints aggressively and wipes accounts on reset) — protocol v7: ledger-linked (research-run trial, behaviour-family holdout status frozen), one joint irregular-fill cap on entry/exit timing, write-once verdict, frozen candle source and `WARM_FROM`/`START` in the generated strategy
- [x] Code review of `agents/` and quant review of the protocol arithmetic, then `/run-gate 13` — 5 quant rounds and 4 code rounds (owner approved rounds 4–5); every Critical and Warning fixed; gate PASS (agents 653, lab 2002)

**Done when:** a `--dry-run` research run completes with structured Designer/Critic outputs, the
tool list assertion and the hash-chain verification passing, the journal report carries the
checklist header, and `paper_trade.py prepare` writes a frozen protocol that `compare` evaluates.

---

## Phase 14 — Chart Upgrades (Lightweight Charts 5.2)

Why: the app is on Lightweight Charts 5.2.1 but uses none of the features added since 5.0:
series hit testing (5.2, `hoveredItem` / `hoveredTarget`, `hoveredSeriesOnTop`), data conflation
for very long datasets (5.1, `enableConflation`, off by default), `takeScreenshot({ addTopLayer,
includeCrosshair })` (5.0.9), and the pane API additions (5.0.8). TradingView published official
plugin packages on 2026-09-16 (`@tradingview/lwc-toolkit`, `lwc-plugin-*`, `create-lwc-plugin`)
and an Agent Skill for AI assistants (5.2.1). The MIT library `lightweight-charts-drawing` (v0.2)
ships 86 TradingView-style drawing tools with export/import JSON and leaves undo, storage and
toolbars to the host; its 0.x API broke between 0.1 and 0.2, so it is evaluated, not assumed
(report section 6).

### Checklist
- [x] PNG export includes drawings: check whether `web/src/chart/screenshot.ts` loses top-layer primitives today; if so pass `addTopLayer: true` (and optionally `includeCrosshair`) and add a test — it did not lose them: export already called `takeScreenshot(true, false)`; now pinned by `captureChart` (`web/src/chart/screenshot.ts`) and its test
- [x] Data conflation enabled for long histories and the seconds/custom timeframes (`enableConflation`, threshold tuned), with a no-regression test on indicator values (conflation is render-only; indicators still compute on every bar) — `web/src/chart/conflation.ts` (on from 20k bars or for seconds/custom timeframes, precompute from 50k, `minBarSpacing` lowered so merging happens on 2x screens); chart history cap raised to `CHART_MAX_BARS` 150k; no-regression test `conflation.indicators.test.ts` (120k bars)
- [x] Series hit testing from 5.2 for the indicator legend and drawing selection (`hoveredItem` / `hoveredTarget`), replacing manual nearest-series lookups where present; `hoveredSeriesOnTop` on — the 5.2.1 typings name it `hoveredInfo` (the release notes' `hoveredItem`/`hoveredTarget` are stale); `web/src/chart/hover.ts`, legend highlight + line width, drawing hover handles, `drawing:<id>` / `indicator:<uid>` primitive ids. No manual nearest-series lookup existed
- [x] Official plugin packages adopted where they replace hand-written code: `lwc-plugin-vertical-line`, `lwc-plugin-anchored-text`, `lwc-plugin-accessibility`; the repo's Lightweight Charts Agent Skill merged into `.claude/skills/lightweight-charts-v5` — accessibility 1.0.0 adopted (new capability; H/T/Escape rebound to `,`/`.`/none); vertical-line and anchored-text not adopted (nothing hand-written to replace; anchored-text is not on npm); reasons in `web/README.md`
- [x] Evaluate `lightweight-charts-drawing` in a branch: load the existing persisted drawings through its `importJSON`, compare behaviour with the four hand-built tools, and decide (adopt, adopt beside, or reject) with the reasons written in `web/README.md` — decision: reject for now (63 KB gzipped vs our 7.7 KB, 0.x API churn, two input layers), written in `web/README.md`. Evaluated on a throwaway worktree branch (0.6.2, 13 tests passing there, five kinds incl. hray); the branch was discarded, so the README is the record
- [x] Indicators via `/add-indicator`: volume profile (fixed range, ported from the plugin example) and anchored VWAP, each with the known-value and no-look-ahead tests — `web/src/indicators/{anchoredVwap,volumeProfile}.ts`, renderer `web/src/chart/volumeProfileLayer.ts` (ported, Apache-2.0), right-click "from this bar", UTC time inputs; anchors before the loaded history draw nothing and say so
- [x] Code review, `npx tsc -b`, `npx vitest run`, `npm run lint`, then `/run-gate 14`; browser checks listed for the owner — code + quant review 2 rounds (round 1: 1 Critical, 6 Warnings fixed); gate PASS: tsc, lint, build, vitest 2277 passed; owner browser-checked 2026-10-29 (PNG export with drawings, 100k+ bar 1m zoom with conflation, hover highlighting, volume profile and anchored VWAP, accessibility keys)

**Done when:** a PNG export shows the drawings, a 1m chart with 100k+ bars zooms out smoothly with
conflation on, hovering an indicator highlights it through hit testing, and the volume profile and
anchored VWAP render with passing tests.

---

## Phase 15 — Risk-Overlay Question (owner, lab lock)

Why: four research runs and about 4,200 plans asked one question, "is there an edge?", and every
answer was "no edge found". The strategy report predicted this. A DSR bar near Sharpe 1.7 is above
the best credible crypto results, and almost every surviving published result is a *risk* result
(smaller drawdowns, crash avoidance), not a return result ("Crypto strategy edge research",
Conclusion). This phase adds a second question, pre-registered in its own criteria file before
any run: **does one slow overlay on buy-and-hold cut drawdown, without a significant Sharpe loss,
with a break-even cost of at least twice the base cost?** A pass is reported as a *risk overlay*,
never as an edge. The edge question, its criteria and its bar are unchanged.

Traps this design must close:
- **Holding less always cuts drawdown.** 50% BTC has about half of BTC's drawdown and the same
  Sharpe. So the overlay is compared with buy-and-hold *scaled to the overlay's own average
  exposure*, as well as with plain buy-and-hold. Beating plain buy-and-hold on drawdown proves
  nothing by itself.
- **Max drawdown is one number from one path.** The design window (2019-01 to 2025-10) holds only a
  handful of large BTC drawdowns. Criteria use conditional drawdown at risk (CDaR, the mean of the
  worst 5% of drawdowns) and paired stationary-bootstrap tests. When there are too few drawdown
  episodes, the result is "not testable", not a pass.
- **The design window has been seen.** Edge runs on 2026-10-25 and 2026-10-26 already looked at
  trend filters, volatility gating and regime filters there. So overlays come from the literature
  with their *published* parameters, untuned, and each declares its family links to earlier
  plans. The locked holdout (from 2025-10-01, unspent) and the forward paper test are the real
  tests.
- **Multiple testing.** The significance level is divided by N_overlay, the number of distinct
  overlay logics across every overlay run (Bonferroni), and each logic may be tried with one
  published parameter set only (the lab refuses re-parameterised copies). A new run never
  resets it.

**Owner decisions** (confirmed 2026-10-29): the criteria values below as proposed; minimum
average exposure 0.5; exposure 0 or 1 only (no half exposure, so no engine change); the slate of
three literature overlays below. Added during the build (2026-10-29): budget 3 hypotheses /
6 proposals / 600 backtests / 1 holdout check / $10; overlays are untuned (no `run_sweep` or
`walk_forward` in overlay runs); bootstrap mean block 60 days; overlay trials count in the edge
DSR's N but stay out of its V[SR] pools; a `min_episodes_improved` row (the overlay beats
exposure-matched buy-and-hold inside a strict majority of the deep buy-and-hold episodes); the
fold check's drawdown rows are "not testable" when the folds hold too few episodes; the holdout
check uses point estimates and fails when buy-and-hold's holdout max drawdown is under 30%.

### Checklist
- [x] **(owner)** Question and criteria file: — `lab/lab/research/{contract,runs}.py`, `start_run(question=...)`; owner installed `lab/config/overlay_criteria.toml` (15 keys incl. `bootstrap_mean_block_days = 60` and `min_episodes_improved_share = 0.5`); edge report pinned by `lab/tests/test_report_golden.py` (fixture captured at 7680942, also passes against a pristine HEAD copy)
  - `start_run` takes `question = "edge" | "risk_overlay"` (default `"edge"`) and freezes it with the criteria sha256.
  - An overlay run reads `lab/config/overlay_criteria.toml`, which the owner installs from `agents/config/overlay_criteria.proposed.toml`. No file means no overlay research.
  - An overlay run's report has only overlay rows, and its wording never says "edge". An edge run's report is unchanged, which a test checks.
  - Proposed rows:
    - `cdar_vs_buy_hold_max = 0.75`: the overlay's CDaR95 is at most 75% of buy-and-hold's.
    - `beat_exposure_matched = true`: the overlay's CDaR95 is below that of buy-and-hold scaled to the overlay's average exposure, paired-bootstrap p ≤ α / N_overlay.
    - `sharpe_non_inferiority = 0.2`: the one-sided 95% lower bound of Sharpe(overlay) − Sharpe(buy-and-hold), from a paired stationary bootstrap annualised with 365 days, is at least −0.2.
    - `break_even_cost_multiple_min = 2.0`: the drawdown and Sharpe rows still hold at twice the costs.
    - `min_drawdown_episodes = 3`: buy-and-hold has at least 3 separate drawdowns deeper than 30% in the scored window; otherwise "not testable".
    - `min_avg_exposure = 0.5`: an overlay, not a timing strategy.
    - `alpha = 0.05`.
    - `wf_rows_hold = true`: the rows also hold on the stitched walk-forward test folds.
    - `pbo_max = 0.2` on the overlay objective, if anything is swept.
    - holdout pass and cross-check match, as for edge plans.
- [x] **(owner)** Statistics in `lab/lab/stats/`: — `stats/drawdown.py` (CUZ CDaR, episodes, constant-mix exposure-matched benchmark) and `stats/paired.py`; checked against `scipy.optimize.linprog`, brute force and vectorbt drawdowns
  - CDaR, drawdown episodes (peak to recovery) and the exposure-matched benchmark;
  - paired stationary-bootstrap tests for the CDaR difference and the Sharpe difference, reusing `stats/bootstrap.py` with an explicit seed;
  - the break-even cost multiple.
  - Each gets known-value tests on hand-built 5–20 bar paths, and a second-implementation check where a library has one.
- [x] Overlay plan kind (`{"kind": "overlay", ...}`): — `Plan.kind`, validator/schema/compile overlay branch, new features `vol_rank` and `premium_mean_rank`; perturbation test in `tests/test_plan_overlay.py`; NaN in `flat_when` keeps the overlay invested
  - Base is buy-and-hold of one symbol. A risk rule decided on bar t's close sets the exposure for bar t+1's open to 1 or 0.
  - Unlike edge plans, derivatives features may drive the flat signal, since crash-risk filtering is their stated role.
  - Registry features only; ≤ 3 features and ≤ 3 tunable parameters.
  - It ships with the perturbation (no-look-ahead) test.
  - Exposure is 0 or 1 only (owner decision); half exposure would need an engine change and is out of scope.
- [x] **(owner)** `get_report`, `get_budget` and `holdout_check` for overlay runs: — `research/overlay.py`, `research/overlay_criteria.py`; holdout uses point estimates and fails when buy-and-hold's holdout max drawdown is under 30%; one published parameter set per overlay logic is enforced server-side (rewrites with other params and logics tuned in edge runs are refused)
  - `holdout_check` stays pass/fail only, judged on the frozen overlay rows.
  - Holdout death is keyed on logic across both questions: a logic whose holdout was spent under either question is never checked again.
  - N_overlay is counted across every overlay run and shown in the report.
- [x] Pre-registration: the overlay journal template states the following before the first backtest: — `Journal.create_overlay` / `finish_overlay` in `agents/research/journal.py`; overlay checks in `agents/prompts/critic.md` and `.claude/agents/strategy-critic.md`
  - the question, criteria and budget;
  - the slate, each overlay with its source and published parameters;
  - each overlay's family links to earlier ledgered plans.
  - The `strategy-critic` prompt gains the overlay checks: the exposure-matched comparison, the episode count, and the seen-data declaration.
- [x] One pre-registered overlay run through the normal pipeline: — run r-20261029-173704-59d618, journal `agents/journal/2026-10-29-btcusdt-btcusdt-risk-overlay-slate-phase-15.md`: **no overlay benefit** (sma200 CDaR 0.663 vs exposure-matched 0.543; premium and vol_rank gates almost never fire, CDaR ratio 1.00); critic: abandon all three; holdout unspent (0/1); N_overlay 3; unledgered review probes disclosed
  - a slate of at most 3 overlays with published parameters, e.g. flat while BTC closes below its 200-day moving average; flat while the 7-day mean premium index is in the top decile of its expanding history (BIS WP 1087); flat in a high realised-volatility regime;
  - `strategy-critic` review, and at most the budgeted holdout check;
  - written up in the journal whatever the outcome. The likely outcome is "no overlay benefit" once compared with exposure-matched buy-and-hold.
- [x] Quant review of the whole phase, then `/run-gate 15` — 3 quant rounds (round 1: 1 Critical, 4 Warnings; round 2: 3 Warnings; all fixed or closed, one accepted residual: an edge sweep on a *rewritten* logic can pick an overlay's params, not live in the ledger); gate PASS: lab 2597 passed, agents 721 passed, ruff clean

Any overlay that passes still needs 4–8 weeks of freqtrade `--dry-run` under the Phase 13 paper
protocol before it is called a working risk overlay. That takes weeks, so it is not part of
**Done when**.

**Done when:** an overlay run starts with its own frozen criteria file and question. `get_report`
shows the overlay rows: CDaR against buy-and-hold and against exposure-matched buy-and-hold,
Sharpe non-inferiority, break-even cost, episode count and N_overlay. The statistics pass
hand-built known-value tests. An edge run's report is byte-identical to before. One
pre-registered overlay run is in the journal.

---

## Phase 16 — Market Structure Overlay (chart)

Why: price action traders read the chart as swing points, trend breaks and flip zones, and the
chart cannot draw any of them today. A video that turned price action into rules and tested
about 101k variants on NQ/ES futures found the 4h change of character (ChoCh) the strongest
setup and old-resistance-turned-support ("flip zones", first retest) the strongest level idea
("Price action strategies research", `agents/research/reports/`). Those claims are unverified
and come from another asset class. This phase is a **visual tool only**: it makes no edge claim,
adds no backtest, and does not touch `lab/`. It needs only Phase 14 and can run alongside Phase 15.
It also lets the owner check by eye that the swing and ChoCh definitions match how price action
traders draw them, before any lab feature reuses them.

Traps this design must close:
- **Repainting.** A swing pivot at bar `p` is only known after `right` more bars close. Every
  output at bar `t` uses only bars `≤ t`. A swing level starts at its confirmation bar `p + right`
  (a small dot may mark the pivot bar), and BOS/ChoCh markers sit on the bar whose close breaks the
  level. Nothing is back-dated.
- **The forming candle.** Events and pivots come from closed bars only. The forming bar (last REST
  row, or a WS kline with `x: false`) never creates, moves or removes a marker or zone.
- **Hidden definitions.** Each rule below is written out in full in `web/README.md`, ties included,
  so a later lab feature can copy it exactly and be checked against the same fixture.

Definitions (inputs in brackets, defaults set before building):
- **Swing high/low**: fractal pivot. The high of bar `p` is strictly above the `left` bars before
  it and at or above the `right` bars after it (mirrored for lows) [`left` 5, `right` 5].
- **Structure**: `up` after a close above the last confirmed swing high, `down` after a close
  below the last confirmed swing low, `none` until the first break. Closes, not wicks.
- **BOS**: a close through the last confirmed swing in the direction of the current structure.
  **ChoCh**: the first close through the last confirmed swing *against* it, which flips the
  structure. Each swing level can be broken once.
- **Flip zone**: a swing high broken by a close becomes a support zone (a swing low becomes a
  resistance zone). The band runs from the pivot bar's wick to its body edge. A **first retest**
  is the first later closed bar that trades into the zone. A close through the far side of the
  zone invalidates it [max active zones 10].
- Touch counts are not shown or scored (the source found that more touches did not make a level stronger).

### Checklist
- [x] Pure compute `web/src/indicators/marketStructure.ts`: confirmed swings, structure state, BOS/ChoCh events and flip zones (created, first retest, invalidated) in one O(n) pass over closed bars, as typed output (points, events, zones); inputs `left`, `right`, `maxZones`
- [x] Indicator types and the registry extended for event and zone outputs beside `line` / `histogram` (`web/src/indicators/types.ts`, `registry.ts`); the existing indicators are unchanged, which their current tests show
- [x] Rendering: BOS/ChoCh and retest markers through `createSeriesMarkers` (merged with any backtest trade markers, not replacing them), swing levels and flip zones as a series primitive in the style of `volumeProfileLayer.ts`, drawn under the drawings, themed for light and dark, with per-part toggles (swings, BOS, ChoCh, zones) in the settings dialog
- [x] Live updates: incremental tail recompute on each closed bar matches a full recompute (test); the forming bar changes nothing (test); a 100k-bar history computes and renders without a visible stall
- [x] Tests: known values on a hand-built candle fixture where every pivot, BOS, ChoCh, zone, retest and invalidation is worked out by hand, including ties and an equal-high pair; a no-look-ahead perturbation test (changing bars after `t` leaves every output at or before `t` unchanged); a test that no marker's time is earlier than its confirmation bar
- [x] Definitions written out in `web/README.md` (rules, ties, defaults, and "visual only, no edge claim"), with the fixture marked as the parity source for any future lab feature
- [x] Code review and quant review (look-ahead focus), `npx tsc -b`, `npx vitest run`, `npm run lint`, then `/run-gate 16`; browser checks listed for the owner

Not in this phase: alerts on ChoCh (the server would need its own port of the rules) and any lab
work. Testing ChoCh as a strategy needs an owner lab phase (a causal swing feature in
`lab/lab/registry/` and a structure-based stop with an R-multiple target in the engine), then a
pre-registered H4 run under the unchanged edge criteria (report section 4).

**Done when:** on BTCUSDT 4h the overlay draws confirmed swing points, BOS and ChoCh markers, and
flip zones with first retests; markers never appear before their confirmation bar, including while
a candle is forming; and the known-value, no-look-ahead, forming-bar and incremental-vs-full tests
pass.

---

## Phase 17 — Jev Trade-Time Judge (forward only)

Why: today Jev only judges research artefacts (sources, duplicates, sweeps, "done?"); it never sees
a trade signal. A video that used Jev as a take/skip judge on rule-generated setups claimed a
Jev-filtered opening-range bot beat its baseline ("Jev and Claude Code trading bots research",
`agents/research/reports/`). That claim is not evidence: the backtest asked a language model about
past sessions its training data very likely covers, and the filter was tuned on the same history.
**An LLM decision layer cannot be backtested honestly here**, so this phase tests the idea the only
honest way: forward, on bars after the model was trained, beside the seven HC exploratory paper bots
(BTC, ETH, SOL, NEAR, AAVE, SUI, ONDO, 4h; test window 2026-10-30 to 2026-12-25, Phase 8). It works
only in `agents/` and makes no edge claim. A late start only means fewer judged trades; the start time
is recorded in the pre-registration.

Traps this design must close:
- **Steering the frozen tests.** The HC strategies and protocols are frozen, and `compare` refuses a
  changed strategy file. The judge is a separate process that **watches, never steers**: it writes no
  strategy, config, protocol or journal file of the HC bots and cannot change their verdicts.
- **Hindsight.** Each verdict is written within 15 minutes of the fill bar's open, from a snapshot of
  bars closed at the signal bar, before the trade's outcome exists.
  Nothing is ever judged after the fact, and no row is edited later.
- **Memorization.** The snapshot is blind: rounded ratios only, no symbol, no dates, no prices.
- **Silent gaps.** Jev down, a timeout or a bad answer is logged as `unavailable` (fail open), never as
  a skip. A missed bar is logged as missed, never filled in later.
- **Small sample.** Expect roughly 30–80 judged trades over 8 weeks. Only a large effect can show,
  so "inconclusive" and "no information" are correct outcomes, written up as carefully as a positive.

### Checklist
- [x] Shadow process `uv run python paper_trade.py shadow`: after each 4h close, recompute every registered HC candidate's entry signal from closed candles with the lab's `compile_plan` (read-only import, same candles, warm-up and data source as `compare`); no effect on the bots (test: the HC strategy, config, protocol and journal files are byte-identical before and after a run)
- [x] Blind snapshot built in Python (rounded ratios only: 1/6/42-bar returns, ATR %, distance to EMAs, RSI, volume ratio, BTC 4h trend; no symbol, dates or prices) and one batched Jev call per closed bar: `choice` take/skip, `noul` fake-out risk, `score` confidence; `TYPESAFE_API_KEY` from env only
- [x] Append-only, hash-chained `agents/data/paper/jev-shadow/decisions.jsonl` (signal bar, candidate, snapshot sha256, verdict, model id, latency, written-at time); every verdict row written before its fill bar's open + 15 min grace (owner decision 2026-10-29: the signal needs bar t's close, which is the instant the fill bar opens; the snapshot holds only bars closed at t), a later one logged as missed; Jev unavailable or a missed bar logged as such (tests: chain verification fails on any edited or deleted row; no verdict row's written-at is at or after its fill bar's open + grace)
- [x] Pre-registered analysis in a journal entry written before the first decision: start time, metric (mean net trade return of Jev-take vs Jev-skip trades, matched to the paper fills `compare` reads), a permutation test, and a minimum sample (at least 30 judged trades and 8 per arm, else "inconclusive"); never changed afterwards
- [x] `paper_trade.py shadow-report`: take vs skip returns matched to paper fills, `unavailable` and missed counts, "inconclusive" below the minimum sample; reproduces from the JSONL and dry-run DBs alone (test on a fixture; the pre-registration and the git-tracked `jev-shadow-heads.md` are checked, not read for numbers); before the frozen `evaluate_at` (latest protocol decision end + 14 days) every report is an interim look with no outcome or p-value
- [x] Rule written into `.claude/rules/research-agents.md` and the `honest-backtesting` checklist: LLM decision layers are evaluated forward only, never inside lab backtests
- [ ] (optional, after the judge has data) Decision stream panel beside the paper ticket: per candidate, each signal with Jev's verdict, confidence and fake-out score, the fill and (once closed) the trade return; read-only server endpoint over the JSONL and dry-run DBs
- [x] Quant review (look-ahead and pre-registration focus), code review for the panel, `uv run pytest -q`, `uv run ruff check .`, then `/run-gate 17`

Not in this phase: any order path, Alpaca, shorts, or acting on a Jev verdict. A positive result
would justify only a second pre-registered forward test, never trading.

**Done when:** the shadow judge has logged every HC signal since its recorded start with no missing
bar it could have judged (or each one logged as missed), the hash chain verifies, and shadow-report
reproduces from the JSONL and dry-run DBs alone.

---

## Phase 18 — Session Ranges Overlay (chart)

Why: the same video's futures bot found most of its profit in the Asia session. Crypto trades 24/7
but liquidity is still uneven across Asia, London and New York hours. That is an unverified claim
from another asset class. This phase is a **visual tool only**, like Phase 16: it lets the owner look
at session structure on the chart before any lab budget is spent on it. It touches only `web/`.

Definitions (UTC, defaults set before building): Asia 00:00–08:00, London 07:00–16:00, New York
13:00–22:00 (overlaps allowed), plus an optional UTC-day opening range over the first N hours
[N = 1]. Each box spans its session's high and low from closed bars only. Shown only on intervals
below 1d.

### Checklist
- [x] Pure compute `web/src/indicators/sessionRanges.ts`: per-session high/low boxes and the UTC opening range in one pass over closed bars; inputs for the session hours and N
- [x] Rendering as a series primitive under the drawings, themed for light and dark, with per-session toggles in the settings dialog (via `/add-indicator`)
- [x] Tests: known values on a hand-built fixture across a UTC midnight; a no-look-ahead perturbation test; the forming bar never moves a box's high or low; incremental tail recompute matches a full recompute
- [x] Definitions written in `web/README.md` ("visual only, no edge claim"); code review, `npx tsc -b`, `npx vitest run`, `npm run lint`, then `/run-gate 18`; browser checks listed for the owner

Not in this phase: a lab opening-range breakout hypothesis. That needs owner registry features
(hour-of-day, session high/low over the first N bars of a UTC day) under the lab lock, and its prior
is weak (no opening auction in crypto, short-interval ports died on costs, DSR N is already 4685).
Consider it only if this overlay makes the session structure look real.

**Done when:** on BTCUSDT 1h the overlay draws Asia, London and New York boxes and the UTC opening
range from closed bars, nothing moves while a candle is forming, and the known-value, no-look-ahead
and incremental-vs-full tests pass.

---

## Phase 19 — Web Performance & DRY (chart, data, server, bundle)

Why: the 2026-10-30 audit (`agents/research/reports/Web performance research.md`) found no single
hot spot but a layer of steady-state overhead that grows with history length, panel count and tick
rate, plus a startup payload shipped as one 1.02 MB chunk. The tick path already uses
`series.update`, incremental indicator checkpoints, `BarLineage` prefix checks and `barsKey` gating;
this phase removes the remaining per-tick scans and root re-renders, cleans the per-frame path in the
primitives, trims history-load and network cost, splits the bundle, and consolidates the duplicate
code those fixes touch. Touches `web/` and `server/` only; nothing in `lab/`.

Rules for this phase: measure before and after every wave with the protocol in report section 7
(Chrome Performance trace on BTCUSDT 1m with 2 indicators and 1 drawing, single and 4-panel; React
Profiler highlight-updates; allocation sampling; server upstream-call log; `vite build` chunk sizes).
A wave is not done on "looks faster"; it is done when the trace shows the change. Every indicator or
layer rewrite keeps its known-value, no-look-ahead and incremental-vs-full tests green. No behaviour
change is allowed in this phase: same pixels, same numbers, same markers.

### Checklist

Wave 1 — per-tick waste (highest payoff)
- [ ] Baseline traces recorded and saved under `agents/research/research_notes/` before any change (single and 4-panel, Binance 1m, plus Coinbase BTC-USD 1m)
- [ ] Tail detection by lineage/version instead of full scans: `data/candleStore.ts` `applyLiveCandle`, `data/plottable.ts` `every(isTradedBar)`, `chart/seriesSync.ts` `tailMatch`; volume profile layer takes the shared `BarLineage` like structure and sessions; per-tick scripting time independent of history length in the trace
- [ ] Live snapshots out of App state: `lives` becomes a small external store read by the quote, StatusBar and paper feed only; status/closedThrough/error stay in React state; `indicatorSeries` stops emitting on 'update' plans; `useLiveCandles` returns a stable object; keydown handler reads `lastClose` from a ref; React Profiler shows no shell commit (Header, SidebarRail, StatusBar, DrawingToolbar, Watchlist) on a forming-bar tick
- [ ] `memo()` the shell: Header (hoisted callbacks, memoised LayoutMenu element), Watchlist and `WatchlistRow` (symbol-keyed stable callbacks, cached `Intl.NumberFormat`), StatusBar (cached clock formatter per timezone), SidebarRail, AlertsPanel props hoisted
- [ ] Coinbase forming-bar emits coalesced per animation frame (or 100–250 ms), same pattern as the existing ticker flush in `coinbaseStream.ts`
- [ ] Ticker stream filtered before object construction: direct path checks `raw.s` against the watched set in `binanceTicker.ts`; server `/ws/tickers` clients send their watched symbols and the hub filters before serialising; `/ws/tickers` bytes per second drop by the watched-to-total ratio
- [ ] Small per-tick allocations removed: `volumePaletteOf` per point, `inputsKey` string and param/toggle objects per tick in the three layers, `uidOfSeries` linear scan replaced by a `Map`, aggregated-timeframe bucket kept as running OHLCV in `BucketState`

Wave 2 — per-frame path (crosshair, scroll, zoom, drawing drag)
- [ ] Drawings primitive (`chart/drawings/primitives.ts`): rgba strings, scaled dash arrays, fib label strings and handle points precomputed into `RenderItem` in `updateAllViews`; `draw()` is canvas calls only; trace with the mouse moving shows no string or `Intl` work per paint
- [ ] Geometry signature cache for market structure, volume profile and session layers (visible logical range, width, barSpacing, two reference `priceToCoordinate` values, view version); unchanged view returns cached geometry; volume profile culls off-screen before building rows, caches `maxVolume` and value-area flags on `setProfile`, and derives barSpacing from `logicalToCoordinate` instead of `timeScale().options()`
- [ ] Drawings hit test uses the frame's cached shapes instead of re-projecting every drawing per mouse move; selected drawing projected once
- [ ] Legend rows memoised on `[indicatorSnapshot.version, hoveredTime, lastBarTime]` and partitioned once; dash arrays hoisted once per draw and strokes batched by colour in the three layers

Wave 3 — history load, bar close, network
- [ ] Rolling-window indicator primitives (`indicators/primitives.ts`) use a ring buffer with running sum and sum-of-squares (periodic re-sum for drift); 'prepend' computes only the new head and splices; perturbation and known-value tests unchanged; a 1000-bar prepend on a 150k history no longer resets every runner
- [ ] Prepend/gap merge by binary-search splice of two sorted arrays (no Map + sort); one plotted-times `Set` and one `barsKey` shared by `Chart.tsx`, `backtestOverlay` and `derivativesPanes`; `series.update()` for the newest point when only the tail grew; volume `setData` gated on up/down colour actually changing; same-kind chart-type switches use `applyOptions` + `setData` without removing the series
- [ ] Server: candles route serves the forming bar from the hub's last kline so a warm-cache `/api/candles` makes zero upstream calls (verified in the server log); `@fastify/compress` (brotli/gzip); `Cache-Control: immutable` + ETag on closed-history pages (`endTime` set)
- [ ] Ref-counted live-candle controller cache keyed on provider/symbol/interval so two panels on the same market share one socket and one REST load; Coinbase REST pages fetched in parallel within the 125 ms throttle budget

Wave 4 — startup and bundle
- [ ] `TesterIcon` and `ResearchIcon` moved out of `BacktestPanel`/`ResearchPage` into a small icons module; `React.lazy` + `Suspense` for ResearchPage, BacktestPanel, PaperTradingPanel, SeasonalsView and SettingsDialog (their CSS splits with them); vendor groups via `build.rolldownOptions.output.codeSplitting.groups` for react/react-dom, lightweight-charts and lwc-plugin-accessibility; main chunk under ~150 KB gzip and vendor hashes stable across an app-only change
- [ ] Fonts: four weights (sans 400/600, mono 400/500) or `@fontsource-variable` packages; `<link rel="preload" as="font">` for the two body weights in `index.html`; `server.warmup` for `App.tsx` and `Chart.tsx` in dev
- [ ] React Compiler: `eslint-plugin-react-compiler` added and violations fixed (setState-during-render sites in `App.tsx` and `useReplay.ts`, over-specified deps in `state/drawings.ts`, render-computed `slots` closure); compiler enabled on `@vitejs/plugin-react` v6 (`react({ compiler: true })` with `oxc-transform-react`, or `@rolldown/plugin-babel` + `reactCompilerPreset()`) in `annotation` mode on the shell first, then `all`; existing `useMemo`/`useCallback` left in place until profiled

Wave 5 — DRY consolidation (interleave with waves 1–4 where a fix touches the duplicate)
- [ ] `web/src/data/http.ts` (one typed request helper: fetch, JSON, abort, Retry-After, error mapping) and `web/src/data/guards.ts` (isRecord/finite/nonEmptyString/toFiniteNumber/dedupeByTime/candle equality) replace the copies in `binance.ts`, `coinbase.ts`, `server.ts`, `persistence.ts`, `labApi.ts`, `researchApi.ts`, `drawingsApi.ts`
- [ ] `chart/layers/base.ts`: shared primitive base (uid, visible, attach/detach, paneViews, updateAllViews, render-target types), shared layer host (entries map, keep-set sweep, setColors fan-out), shared coords adapter, lineage prefix memo inside `BarLineage`; `chart/util/search.ts` with one `lowerBound`; one luminance function (pick Rec.709 or Rec.601, used everywhere); one candle→point mapper set
- [ ] State/UI: one `storedValue<T>` + `useStoredState` for the six localStorage pairs and eight persist-on-change effects; `useModalKeys` extended to the twelve hand-rolled Escape handlers; a `useMenu` hook for TimeframePicker, ChartTypeMenu and LayoutMenu; a `useController(factory, deps)` hook for the seven controller-in-state hooks
- [ ] `components/primitives.css`: icon button, tab with accent underline, dialog shell/head/foot, `:focus-visible` ring, thin scrollbar, panel head/title/empty chrome; the seven-plus copies removed from the component stylesheets; visual diff shows no change
- [ ] After each wave: code review (`code-reviewer`; `quant-reviewer` for anything under `web/src/indicators/`), `npx tsc -b`, `npx vitest run`, `npm run lint` in `web/`; `npx tsc --noEmit`, `npx vitest run` in `server/`; then `/run-gate 19`; browser checks listed for the owner

Not in this phase: a Web Worker for indicators (only worth it for full recomputes on very long
histories once the primitives are O(1) per bar; revisit if the post-wave-3 trace still shows
`runner.reset` as a long task), compact kline arrays on the wire (client and server change; measure
after compression first), and any change to what the chart shows.

**Done when:** on BTCUSDT 1m with two indicators, one drawing and a 4-panel layout, a 10 s Chrome
Performance trace shows no React commit outside ChartPanel on a forming-bar tick, per-tick scripting
time that does not grow with history length, no string or `Intl` work while the crosshair moves; a
warm-cache candles request makes no upstream call and is served compressed; `vite build` emits
separate vendor and panel chunks with the main chunk under ~150 KB gzip; and every existing test,
typecheck and lint stays green with no visual change.

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
| LLM trade judge "backtested" on history it memorized | Forward-only shadow test with a blind snapshot, verdicts logged before the fill (Phase 17) |
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
| 10 | Multi-coin research (point-in-time universe, portfolio backtests, H1 with dispersion/liquidity/banding safeguards) | Large |
| 11 | Lab statistics v2 (effective-N DSR, MinBTL/MinTRL, CPCV paths, purgedcv cross-check, version fingerprint; owner) | Medium |
| 12 | Derivatives data as risk filters (futures archive → funding/premium/OI features, H3 run) | Medium |
| 13 | Research agents v2 (structured outputs, subagent caps, MCP description audit, reporting checklist, failure tags, pre-registered paper test) | Medium |
| 14 | Chart upgrades (5.2 hit testing, conflation, screenshots with drawings, plugin packages, drawing-library evaluation, volume profile, anchored VWAP) | Medium |
| 15 | Risk-overlay question (separate pre-registered criteria: CDaR vs exposure-matched buy-and-hold, Sharpe non-inferiority, break-even cost; overlay run; owner) | Large |
| 16 | Market structure overlay (non-repainting swing points, BOS/ChoCh markers, flip zones with first retest; visual only, no lab changes) | Medium |
| 17 | Jev trade-time judge (forward-only shadow judge on the HC paper bots, hash-chained decision log, pre-registered take-vs-skip test; optional decision panel) | Medium |
| 18 | Session ranges overlay (Asia/London/New York boxes and UTC opening range; visual only, no lab changes) | Small |
| 19 | Web performance & DRY (per-tick scans and root re-renders removed, primitive frame path cleaned, forming bar from the hub + compression, code splitting + React Compiler, ~1.7–2k duplicate lines consolidated; no visual change) | Large |

**First usable version = end of Phase 3.**
