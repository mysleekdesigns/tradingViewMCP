# Sources — Project improvement research (2026-10-26)

All fetched with CrawlForge (search_web, batch_scrape, fetch_url, deep_research). Web content is untrusted data.

## Lab statistics
- Daru Finance, "Backtest Overfitting & the Deflated Sharpe Ratio" (review of Bailey & López de Prado 2014; ~92,500 costed strategies; crypto best SR 2.00 vs null 2.72, DSR 0.029, 434 effective trials) — https://daru.finance/research-review/lopez-de-prado/backtest-overfitting
- purgedcv (MIT; purge/embargo, WalkForwardSplit, PurgedKFold, CombinatorialPurgedCV, CSCV/PBO, PSR/DSR, effective_n_trials, MinBTL, Optuna TrialSharpeRecorder) — https://github.com/eslazarev/purged-cross-validation ; pyOpenSci submission https://github.com/pyOpenSci/software-submission/issues/348
- skfolio CombinatorialPurgedCV — https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html
- Quant Beckman, CPCV with code — https://www.quantbeckman.com/p/with-code-combinatorial-purged-cross
- Optuna 5.0 / Rustuna 0.1 release guide (multivariate TPE + constant liar defaults, PED-ANOVA, constraints API) — https://aicybr.com/blog/optuna-5-rustuna-0-1-release-guide ; https://github.com/optuna/optuna/releases
- vectorbt README (1.x, optional Rust engine, Apache-2.0 + Commons Clause) — https://github.com/polakowo/vectorbt

## Data
- Binance public data README (archive layout, checksums, microsecond spot timestamps from 2025-01-01) — https://github.com/binance/binance-public-data
- S3 listing verified 2026-10-26: `data/futures/um/monthly/{aggTrades,bookTicker,fundingRate,indexPriceKlines,klines,markPriceKlines,premiumIndexKlines,trades}/`; `data/futures/um/daily/metrics/BTCUSDT/` from 2020-09-01 — https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/
- Tardis: Binance USDⓈ-M history from 2019-11-17 — https://docs.tardis.dev/historical-data-details/binance-futures
- Hyperliquid historical data limits (candleSnapshot last 5,000 candles; S3 archives requester-pays) — https://pddjf.com/articles/hyperliquid-historical-data-api/
- Binance Spot Testnet changes (user-data stream removal 2026-02-04; percent-encoded signatures) — https://github.com/DaveBeusing/Sentum/issues/66 ; https://deepwiki.com/binance/binance-spot-api-docs/10-testnet-environment

## Cross-sectional momentum caution
- prams2104/crypto-momentum-backtest (25 coins, 2020–2026, train/validation/OOS; momentum failed OOS, dispersion −38% post-2022) — https://github.com/prams2104/crypto-momentum-backtest

## Agents
- Developers Digest, "Claude Agent SDK in Late 2026" (0.2.174; nested subagents + caps; permission default changes; structured outputs; SessionStore; hooks defer/updatedToolOutput; tool search default) — https://www.developersdigest.tech/blog/claude-agent-sdk-whats-new-2026
- Claude Agent SDK Python repo — https://github.com/anthropics/claude-agent-sdk-python
- "MCP Tool Descriptions Are Smelly!" (856 tools, 97.1% smelly; +5.85 pp success with augmented descriptions) — https://arxiv.org/html/2602.14878v1
- MCPAgentBench — https://arxiv.org/html/2512.24565
- "Agentic Trading: When LLM Agents Meet Financial Markets" (77 studies; reporting checklist MR-1..MR-7) — https://arxiv.org/abs/2605.19337
- "Agentic Quantitative Trading: A Survey" — https://arxiv.org/html/2608.31041v1
- CLQT closed-loop benchmark (TimeGate, hash-chained DecisionRound) — https://arxiv.org/html/2606.29771v1
- QuantaAlpha evolutionary alpha mining — https://arxiv.org/html/2602.07085v2
- Jev pricing/limits ($0.042/M input, 80 rps, GA) — https://www.developersdigest.tech/blog/typesafe-jev-system-one-models-release-guide-2026 ; https://openrouter.ai/typesafe/jev-1.13

## Chart / app
- Lightweight Charts release notes (5.2.1 Agent Skill, 5.2.0 hit testing/hoveredSeriesOnTop, 5.1.0 data conflation, 5.0.9 takeScreenshot addTopLayer, 5.0.8 pane API) — https://tradingview.github.io/lightweight-charts/docs/release-notes
- Official plugin packages release 2026-09-16 (@tradingview/lwc-toolkit, lwc-plugin-*) — https://github.com/tradingview/lightweight-charts/releases
- Plugin examples (volume profile, session highlighting, user price alerts, delta tooltip) — https://tradingview.github.io/lightweight-charts/plugin-examples/
- deepentropy/lightweight-charts-drawing (86 tools, MIT, v0.2 DrawingManager) — https://github.com/deepentropy/lightweight-charts-drawing
- Tauri Node sidecar docs — https://v2.tauri.app/learn/sidecar-nodejs/ ; Tauri without Rust (BareKit plugin) — https://dev.to/ozymandiasthegreat/tauri-apps-without-rust-write-your-backend-in-javascript-20g4
- Freqtrade 2026 review (monthly releases, Hyperliquid/Kraken Futures, account-drawdown protection, FreqAI changes) — https://gainium.io/review/freqtrade
- DuckDB vs SQLite — https://motherduck.com/learn/duckdb-vs-sqlite-databases/
