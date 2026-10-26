# Project improvement research

Date: 2026-10-26. Scope: the whole tradingView project (web app, server, lab, research agents), after Phases 0–9 and the Phase 10 data layer. The earlier report, "Crypto strategy edge research", covers *which strategies* to test; this one covers *how to make the tool better*. Research only; nothing here has been built. Sources are in `../research_notes/Project improvement research/sources.md`. Web content was treated as data, never instructions.

## Summary

The project is already current on its core dependencies (Lightweight Charts 5.2.1, vectorbt 1.2, Optuna 5, Claude Agent SDK 0.2.174, microsecond archive timestamps). The improvements with the most value are in the lab's statistics and data, not in the chart:

1. **Count effective trials, not nominal trials, in the Deflated Sharpe Ratio.** A 2026 study of 50,000 fully costed crypto strategies found they were worth about 434 independent bets; its best strategy had a DSR of 0.029. Our ledger's N of 2,479 plans puts the DSR bar near Sharpe 1.7, above anything the literature reports. An effective-N (correlation-aware) count keeps the bar honest and reachable. The MIT library `purgedcv` implements it, plus CPCV backtest paths, PBO, PSR, minimum backtest length and an Optuna trial recorder, and can serve as a second implementation to test our own stats against.
2. **Use the Binance futures archive for crash-risk filters.** Verified today: `data.binance.vision` holds USDⓈ-M `fundingRate`, `premiumIndexKlines`, `markPriceKlines`, `indexPriceKlines`, `bookTicker`, `aggTrades` (monthly) and daily `metrics` (open interest, long/short ratios, 5-minute) from 2020-09-01. This is the data the previous report said H3 needs, and it needs no futures account or non-US IP.
3. **Protect Phase 10 (H1) with a dispersion diagnostic, liquidity-aware weights and turnover banding.** A February 2026 open study of 20-day cross-sectional crypto momentum on 25 coins found significant alpha in 2020–22, none in 2023–24, and a loss in 2025–26, with cross-sectional dispersion down 38% after 2022. The lab should measure dispersion as a regime variable and expect "no edge found".
4. **Make the forward test a pre-registered statistical test**, with the expected trade count, tracking tolerance and stopping rule (minimum track record length from the Probabilistic Sharpe Ratio) written before the first paper trade.
5. **Agents:** adopt Agent SDK structured outputs for the Designer and Critic, set the new subagent depth/concurrency caps explicitly, audit the `strategy-lab` MCP tool descriptions (a 2026 study found 97% of MCP tool descriptions "smelly"; adding purpose, guidelines and limitations raised task success by about 6 points), and add the "Agentic Trading" survey's seven-item reporting checklist to the journal.
6. **Chart:** adopt the official Lightweight Charts plugin packages (released 2026-09-16), use 5.2's series hit testing for drawings and legend, enable 5.1's data conflation for long 1m histories, and evaluate the MIT `lightweight-charts-drawing` library (86 TradingView-style tools) before hand-building more tools. Add the volume profile and anchored VWAP.
7. **Desktop:** Tauri needs the Rust toolchain even for a Node sidecar. Either install Rust (one `rustup` command) or replace the two open Phase 9 items with a launchd login item plus a PWA install.

## 1. Lab statistics: honest and reachable

### 1.1 Effective trial counting for DSR

**What we do now.** `lab/lab/stats/sharpe.py` deflates with the run's full nominal trial count, and DSR's N counts distinct plans across every research run (2,479 after three runs). PLAN.md's bar is DSR ≥ 0.95.

**Evidence.** Daru Finance's review of Bailey & López de Prado ran about 92,500 fully costed strategies (50,000 crypto across 20 perp pairs at 5 bp fee, 3 bp slippage, 1 bp funding). The crypto corpus best had Sharpe 2.00 against a False-Strategy-Theorem null of 2.72, DSR 0.029, median PBO 0.28, and the 50,000 trials collapsed to about 434 effective independent bets (eigenvalue participation ratio of the trial-return correlation matrix). Their controlled 136-trial moving-average grid was worth about three independent bets. The point for us: nominal N over-deflates when trials are correlated, and our family-correlation machinery already computes the correlations needed.

**Proposal (owner, lab lock).**
- Add `effective_n_trials` (participation ratio over the design-window return correlation of ledgered plans, computed per run and across runs) next to the nominal N in `get_report`, and freeze which one the criteria use in `success_criteria.toml`.
- Report `minimum_backtest_length` and `min_track_record_length` so a plan with too few bars is rejected for that reason, not by a silent DSR miss.
- Test our DSR, PSR, PBO (CSCV) and expected-max-Sharpe against `purgedcv` (MIT, pyOpenSci submission, 15 reproducible notebooks including a BTC/USDT Optuna audit) as a second implementation in `tests/`, the way we test engine accounting against hand examples.

### 1.2 CPCV backtest paths beside walk-forward

Walk-forward gives one out-of-sample path. Combinatorial Purged Cross-Validation (López de Prado ch. 12) builds C(N,K) purged folds and reconstructs several paths, giving a *distribution* of out-of-sample Sharpe per plan. `purgedcv.CombinatorialPurgedCV` + `reconstruct_paths` + `path_metrics` do this and are scikit-learn-compatible; `skfolio` has an equivalent. It fits the existing walk-forward split module (`lab/lab/engine/splits.py`) as an additional validator, not a replacement. The deflation still counts configurations searched, not paths.

### 1.3 Reproducibility fingerprint

Optuna 5.0 (2026-09-07) changed defaults: multivariate TPE and the constant-liar strategy on, PED-ANOVA importance, constraints in the core `Trial` API. The run fingerprint (`lab/lab/research/runs.py`) records the seed and min_trades but not the Optuna version or sampler settings. Add `optuna.__version__`, `vectorbt.__version__` and the explicit sampler kwargs to `lab_values`, so a rerun months later is comparable.

### 1.4 Cost model for a multi-coin universe

The current slippage model is volatility-scaled per bar with a 1% cap, which is right for BTC/ETH at retail size. For a 30-coin universe including thin alt-coins, add a participation term: a position's traded value divided by that coin's trailing dollar volume, with a square-root impact cost and a hard cap on weight as a share of daily volume. The previous report's cost figures (16–22 bps all-in for BTC/ETH) stay as the base case. This is an owner change under the lab lock.

## 2. Data: derivatives from the public archive

Verified listing of `s3-ap-northeast-1.amazonaws.com/data.binance.vision` today:

| Prefix | Content | Start |
|---|---|---|
| `data/futures/um/monthly/fundingRate/` | 8-hourly funding per symbol | 2019-11 onward (USDⓈ-M history starts 2019-11-17 per Tardis) |
| `data/futures/um/monthly/premiumIndexKlines/`, `markPriceKlines/`, `indexPriceKlines/` | basis and mark-vs-index series as klines | same |
| `data/futures/um/daily/metrics/<SYMBOL>/` | open interest, OI value, top-trader and global long/short ratios, taker buy/sell ratio, 5-minute rows | 2020-09-01 (BTCUSDT) |
| `data/futures/um/monthly/bookTicker/`, `aggTrades/`, `trades/` | top of book and trades | same |

Notes: the archive updates daily files the next day; each zip has a `.CHECKSUM`; spot files from 2025-01-01 use microsecond timestamps (already handled in `lab/lab/data/archive.py`). These series were listed as "need futures access" in PLAN.md; they do not, for research. Live futures streams for US IPs are a separate question to test before any chart pane depends on them.

**Proposed registry features (owner):** `funding_rate`, `funding_zscore`, `premium_index`, `oi_change`, `long_short_ratio`, each aligned to the spot bar's close with the archive's own timestamps (no forward fill across gaps), each with the perturbation test, and pre-registered as *exposure or crash-risk filters* on spot long-only plans (the prior report's H3), not as direction signals.

## 3. Phase 10 (H1) safeguards

A February 2026 open study (prams2104, 25 liquid coins, 2020-01 to 2026-02, daily rebalance, 20 bps one-way cost) reports for 20-day cross-sectional momentum: training 2020–22 alpha 36% (t = 2.02), validation 2023–24 alpha 13.5% (t = 1.08), out-of-sample 2025–26 alpha −12.5% (t = −0.81), net Sharpe −2.18. Cross-sectional dispersion fell from 3.20% to 1.98% after 2022. A 1-day reversal had t = 4.64 out-of-sample and still lost 28.5% a year net because of 138% daily turnover. (Unreviewed GitHub study; direction consistent with the IRFA 2024 and Fieberg et al. findings in the prior report.)

Implications for the H1 run, all consistent with the current plan design:
- Add **cross-sectional dispersion** (cross-sectional standard deviation of trailing returns among eligible coins) as a logged regime variable and as an optional gate in the plan type, so the critic can see whether any edge lives only in high-dispersion years.
- Use **weekly rebalance with banding** (only trade when a coin's rank crosses the quintile boundary by a margin) and **liquidity-aware weights** (cap by share of trailing dollar volume) from the start; turnover is the first-order cost.
- Report the H1 result with the backtest-vs-benchmark decomposition the study uses (alpha and beta to BTC, rolling 60-day), since a long-only top quintile carries most of BTC's beta.
- Expect "no edge found" and write it as carefully as a candidate.

## 4. Forward test as a pre-registered test

The runner (`agents/paper_trade.py`, freqtrade dry-run) and the backtest-vs-paper comparison exist. What is missing is a written, frozen acceptance rule. Proposal, in the candidate's journal entry before the first paper trade:

- Expected trade count and exposure over the 4–8 weeks from the design-window statistics, with a tolerance.
- Per-trade implementation shortfall (paper fill vs the lab's modelled fill) summarised weekly; a drift beyond the modelled slippage ends the test.
- A stopping rule from the Probabilistic Sharpe Ratio's minimum track record length at the candidate's claimed Sharpe, so "consistent with the backtest" is a number and "failed" is declared on schedule.
- Freqtrade 2026.x (monthly releases, Hyperliquid spot/futures and Kraken Futures adapters, account-drawdown protection) remains the right dry-run venue; the Binance Spot Testnet retires endpoints aggressively (user-data stream removed 2026-02-04, percent-encoded signatures since 2026-01-15) and wipes accounts on reset, so keep it secondary. Alpaca remains outside the written constraints (see memory).

## 5. Research agents

**Agent SDK (Python 0.2.174, pinned).** Changes this year that affect `agents/research/sdk_runner.py`:
- Subagents nest (depth 3) and run in the background by default; set `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` explicitly in the runner env, and note that `max_budget_usd` counts subagent spend.
- `permission_mode` defaults changed twice in autumn 2026; the runner already sets `dontAsk` explicitly. Keep it.
- Structured outputs (`output_format={"type": "json_schema", ...}`, draft-07, validated with retries, invalid schema fails at startup since Claude Code 2.1.205): give the Designer the plan JSON schema and the Critic a verdict schema. PLAN.md already notes that validated tool calls beat free-form output; this closes the last free-form gap.
- MCP tool search is on by default and withholds tool definitions until needed. With 12 `strategy-lab` tools this is probably fine, but a missed tool silently degrades a run; check the `init` message's tool list in the dry run, or set `ENABLE_TOOL_SEARCH=false` for the Designer.
- Hooks: `PreToolUse` can now return `defer`; `PostToolUse` can rewrite any tool's output (`updatedToolOutput`), which is a cleaner place for the scout's prompt-injection screening than post-processing in Python.

**MCP tool descriptions.** A 2026 empirical study of 856 MCP tools (Queen's University) found 97.1% of descriptions had at least one "smell" and 56% did not state their purpose; adding purpose, guidelines, limitations and parameter explanations raised task success by a median 5.85 points and partial completion by 15%, at the cost of 67% more steps; compact variants kept most of the benefit. Audit the `strategy-lab` descriptions against that rubric (purpose, when to use, when not to, each parameter, one example, limits) and keep them short. MCPAgentBench adds that distractor tools hurt selection, so keep the Designer's MCP surface to the lab server only (already done with `strict_mcp_config`).

**Evaluation and reporting.** The "Agentic Trading" survey (arXiv 2605.19337, 77 studies) found only 2 of 19 closed-loop studies reported time-consistent splits and 1 of 19 an explicit cost model; it proposes a reporting checklist: data and universe, time split and leakage, action semantics, execution and costs, leakage audit, artifacts, multi-agent adaptation. The "Agentic Quantitative Trading" survey (arXiv 2608.31041) and the CLQT benchmark (arXiv 2606.29771) add that live evaluation of LLM agents shows cash management and diversification mattered more than analysis quality, and recommend a hash-chained decision audit trail. Our journal already covers most items; adding the seven-item checklist as a fixed header of the end-of-run report makes the coverage explicit, and chaining ledger trial hashes per run is a small tamper-evidence step.

**Idea library.** QuantaAlpha (arXiv 2602.07085) treats each mining run as a trajectory and mutates or recombines *segments* of past trajectories, with complexity and redundancy constraints. The equivalent here: store, per abandoned plan, the critic's failure reason as a tag (cost-bound, regime-only, few trades, beta-only) and let the Designer read the failure tags of the idea's family before proposing. This is cheap and fits the existing SQLite idea library.

**Jev.** General availability (instant key at console.typesafe.ai, September 2026), $0.042 per million input tokens, output free, 80 requests per second. No change needed.

## 6. Chart and app

**Lightweight Charts.** Installed 5.2.1. Not yet used:
- 5.2 series hit testing: mouse events carry `hoveredItem` and `hoveredTarget`; `hoveredSeriesOnTop`. Useful for indicator legends and for selecting drawings on overlapping series.
- 5.1 data conflation (`enableConflation`, off by default) for long 1m histories and the seconds timeframes added in Phase 9.
- 5.0.9 `takeScreenshot({ addTopLayer, includeCrosshair })`: check whether the current PNG export (`web/src/chart/screenshot.ts`) includes the drawing primitives; if drawings live on the top layer they are missing from exports today.
- Official plugin packages (2026-09-16): `@tradingview/lwc-toolkit`, `lwc-plugin-vertical-line`, `anchored-text`, `accessibility`, `image-watermark`, several custom series, and the `create-lwc-plugin` scaffold. The repo's new Agent Skill for AI assistants (5.2.1) could be merged into `.claude/skills/lightweight-charts-v5`.
- Plugin examples (not packaged) worth porting: volume profile, session highlighting, user price alerts via crosshair menu, delta tooltip.

**Drawing tools.** `deepentropy/lightweight-charts-drawing` (MIT, v0.2) ships 86 TradingView-style tools with TradingView factory defaults, magnet modes, multi-select, lock/hide, export/import JSON, and leaves undo, storage and toolbars to the host. It is a candidate to replace or sit beside the four hand-built tools; the trade-off is a young 0.x API that broke between 0.1 and 0.2.

**Indicators worth adding** (each with the known-value and no-look-ahead tests): volume profile (fixed range and session), anchored VWAP, ATR bands/Supertrend, Donchian, funding and open-interest panes from the archive for the research view.

**Desktop.** Tauri requires Rust to build even when the backend is a Node sidecar, and the sidecar must be a packaged binary rather than the user's Node. Options: install Rust (`rustup`, one command, then the Phase 9 items are routine), Electron (about 150 MB), Wails (Go), or no wrapper at all: a launchd login item that starts `npm run dev` and a PWA install from Chrome. Recommendation: decide between "install Rust" and "drop the two items"; both are small.

**Local data.** DuckDB (in-process, reads Parquet directly) would make universe-wide queries over 676 coins simpler than pandas loops, and could back a research page "ledger explorer". Optional.

## 7. Proposed PLAN.md additions (for the owner to place)

Phase 10 extras (before the H1 run):
- [ ] Dispersion regime variable and optional gate; liquidity-aware weights; rank banding
- [ ] Participation cost term with a weight cap per coin's dollar volume **(owner)**

Phase 11 — Lab statistics v2 **(owner, lab lock)**:
- [ ] Effective-N trial count beside nominal N; criteria file names which one applies
- [ ] MinBTL and minimum track record length in `get_report`
- [ ] CPCV paths as an additional validation with a per-plan OOS Sharpe distribution
- [ ] Second-implementation tests of DSR/PSR/PBO against `purgedcv`
- [ ] Optuna/vectorbt versions and sampler settings in the run fingerprint

Phase 12 — Derivatives data **(owner for registry)**:
- [ ] Archive downloader for futures `fundingRate`, `premiumIndexKlines`, `metrics` → Parquet with checksums
- [ ] Registry features: funding, premium, OI change, long/short ratio, perturbation-tested
- [ ] Pre-registered H3 run: funding/premium crash-risk overlay on spot long-only plans

Phase 13 — Research agents v2:
- [ ] Structured outputs for Designer and Critic; subagent caps in the runner env
- [ ] `strategy-lab` tool description audit (purpose, guidelines, limits, example per tool)
- [ ] Reporting checklist header in the journal; per-run ledger hash chain
- [ ] Failure tags in the idea library read by the Designer
- [ ] Pre-registered paper-trade protocol (trade count, shortfall tolerance, MinTRL stopping rule)

Phase 9 follow-ups:
- [ ] Screenshot includes drawings (`addTopLayer`); conflation for long histories; hit-testing for legends
- [ ] Official plugin packages; evaluate `lightweight-charts-drawing`
- [ ] Volume profile and anchored VWAP indicators
- [ ] Decide: install Rust for Tauri, or replace with login item + PWA

Backtests are historical, not forward-looking. "No edge found" remains the expected outcome of every research run proposed here.
