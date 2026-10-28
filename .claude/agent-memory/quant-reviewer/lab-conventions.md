---
name: lab-conventions
description: Lab engine/stat conventions verified as correct in the Phase 7 review, plus how to run probes in the sandbox
metadata:
  type: project
---

Checked by hand-built probes on 2026-10-24. Do not re-flag these unless the code changes.

- Engine two-row trick (row 2j = open, row 2j+1 = rest of bar). Verified:
  - stops are checked on the entry bar
  - a gap through the stop fills at the open with slippage applied
  - when SL and TP hit in the same bar, SL wins
  - an exit signal at the open runs before the intrabar stop
  - stops are skipped on untradable bars and checked at the next tradable open
  - a favourable TP gap fills at the open
  - fee and slippage are charged per side on traded value
- Fills happen at the next tradable bar's open. The `delay` parameter counts rows.
- Annualisation is 365 days throughout: `bars_per_year`, with `periods_per_year` passed explicitly to every hedgeFund metric (hedgeFund defaults to 252).
- DSR matches Bailey & López de Prado 2014 (0.9004 for N=100 and 0.9505 for N=46 are tested). Permutation p-value is `(count + 1) / (N + 1)` (from hedgeFund). PBO/CSCV ranks follow the paper.
- holdout_start = 2025-10-01. The runner loads with end=holdout_start, calls `design_view`, then `assert_no_holdout`.
- Data: missing bars are absent rows. tradable = volume > 0. No ffill anywhere.
- Probing: run `export UV_CACHE_DIR=$TMPDIR/uv-cache` first. Real data is at lab/data/klines/BTCUSDT/4h (2018-01..2026-09). A full report plus the adversarial run with 20 permutation paths takes about 7 s.
- Search/WF (verified on 2026-10-24): every Optuna trial is ledgered, errors included. Selection reads train windows only (`history_through(train)`), and a perturbation test covers it. The holdout is guarded by `assert_no_holdout` plus `window.end <= holdout_start`. The guard trusts the caller's `cfg`, so Phase 8 must pin `cfg` server-side.
- nautilus cross-check timing: the bar ts is at close and the open is a trade tick 1 ns after the open. The replay strategy only reads signal rows < the current row, so the close cannot leak into the fill. The no-stop EMA 20/50 run agrees exactly (137 trades).
- Files can change mid-review because implementers run in parallel. Check mtimes and config values before quoting numbers.

Phase 8 research layer (verified sound on 2026-10-24; do not re-flag unless the code changes):

- `criteria.regime_breakdown` assigns bar t's return to the label at t-1, using the frozen 90d/±10% definition on the base design trial's returns.
- The DSR row re-deflates the stored design-trial returns with the current N. V is scoped by interval, window, and costs. With N > 1 and no estimable V it reads "missing".
- Missing evidence is never a pass. A backtest evidence trial_id is checked against run_id, plan_hash, and status.
- Evidence is written only by server code, keyed by the hash of the submitted plan.
- Holdout eval: data up to the last closed bar (now floored to the interval), `evaluate_on_window` (next-bar open, warm-up from the full history), 365-day metrics. The check is spent under BEGIN IMMEDIATE before any holdout bar is evaluated.
- In autonomous SDK roles the designer has no built-in tools, a pinned server, and holdout and start_run disabled. The critic has read-only tools and strict_mcp_config.
- The seed JSONs match the freqtrade sources (checked against raw GitHub): Fisher-RSI to RSI conversions are exact, and deviations are disclosed.
- `paper/compare.py` has no annualisation. The replay warms up from `startup_candle_count`.
- Probe recipe: call `start_research_run` + `backtest_evidence` + `evaluate_plan` directly with `load_research_config(agents/config/success_criteria.proposed.toml)` and a tmp ledger. About 20 s on BTC 4h.
- Round 2 (2026-10-24), verified: the cross-check runs two passes (configured + zero-slippage mechanics), which closes recurring #7. Holdout min trades = max(5, ceil(min_trades*holdout_bars/design_bars)). PBO = max(neighbourhood, sweep CSCV of the logic). Unknown SDK cost is charged at the cap, and the reservation is persisted for --resume.
- Hook quirk: any Bash command whose text contains `lab/lab/registry` or `success_criteria.toml` is blocked by the lab-lock hook, even for reads. Use Read, or introspect via Python (`from lab.registry import REGISTRY`).
- Round 3 (2026-10-24), verified: family code reads design-window trials only (no holdout metrics; holdout_checked reads only the check's plan_json); correlation needs equal-length arrays from the exact same symbol/interval/window; record_holdout_check re-checks the family's logic+plan hashes under BEGIN IMMEDIATE (family itself computed outside the txn); neighbourhood grid uncapped with cap 25 = base + 6x4; journal DSR N global. behaviour_family costs ~1.7 s per 3000 4h trials (decodes every blob, even same-logic rows); holdout_check computes it twice.
- Probe scripts live in the scratchpad: probe_v.py (V laundering), probe_wf.py, probe_wf_mcp.py (narrow WF via MCP), probe_r3.py / probe_r3b.py (interval composite dodge, literal tuning, correlation table, perf). Run them with `uv run python <script> ../agents/config/success_criteria.proposed.toml` from lab/.

- Round 4 (2026-10-24), verified: family alignment loads with end=holdout_start + design_view (no holdout read); _to_grid drops gap bars and incomplete buckets, lcm grid, all intervals epoch-aligned (no 1w/3d/1M). "uncertain" fail-closed only blocks same-feature-multiset logics that were already holdout-checked (RSI not refused with 1h loader broken). min_variance_plans counts neighbours: one 2-param plan = 9 plans (DSR missing), a second plan or any sweep clears it; PBO needs a >=16 sweep anyway, so the floor never blocks an otherwise-passing first run. Orchestrator: crosscheck -> pre_holdout_passed -> holdout; family/uncertain refusals classify as logic_spent (dead). Probes: scratchpad qr4/probe_q4*.py.
- Paper runner (agents/research/paper/freqtrade.py), verified 2026-10-25: `check_paper_config` is a deny-list scan for credential keys anywhere (key/secret only as ""), plus a dry_run-is-True check. It never relied on `api_server`/`telegram` blocks being present, so leaving those blocks out (freqtrade 2026.9 schema requires a token/listen address when they exist) does not weaken it. `_child_env` strips only FREQTRADE__EXCHANGE__*, so FREQTRADE__API_SERVER__/TELEGRAM__ env vars still pass through by design. The public_api_host override is binance-only in practice. ccxt 4.5.90 reads `options.fetchMarkets` as either a dict with `types` or a list. `exchange_options` replaces `ccxt_config` wholesale rather than merging it.
- Sandbox quirk: `uv run` fails because the ~/.cache/uv sandbox is denied. Use `agents/.venv/bin/python -m pytest` and `.venv/bin/ruff` directly. The freqtrade venv is at ~/.venvs/freqtrade.

Related: [[recurring-issues]]

Phase 9 (verified on 2026-10-26):
- `POST /lab/regime` is display-only (no ledger). `regime_labels` = `pct_change(window)` over ROWS. The ribbon sends `load.bars` (zero-volume bars dropped), so on thin pairs its window differs from the lab's regime_state, which keeps zero-volume rows. That is a consistency gap, not look-ahead.
- Web: regime and replay both treat the last row of `load.bars` as forming. Replay passes `formingLast=false`. Stale points are filtered to plotted times. The Chart applies 'set' on a shrink and 'update' from prev.length on a grow. The runner property tests cover replay.
- The paper engine's realized+unrealized = equity - start identity holds (max error 3e-11 over 5000 random ops). Reserves keep cash >= 0.
- Aggregation is never forward-filled. The partial oldest bucket is dropped. d/w are anchored to Monday 1970-01-05 (3d opens at 86400 mod 259200); s/m/h are anchored to the epoch.
- Phase 9 round 2 (2026-10-26), verified: regimeSourceBars live = raw minus forming last row; replay = raw rows <= the last visible (closed) bar, zero-volume rows included (closes N2); a zero-volume forming bar is excluded too. Lab /lab/regime labels are causal by rows, so useRegime keeping the previous series' points while loading leaks nothing (Chart filters to plotted times). Replay market orders fill at the cursor bar's close (TV-style, not t+1 open); orders are stamped with the bar's open time, so only later bars fill limits. The reset form keeps highWater. Live staleness: stream stale timeout 60 s reconnects silently with status 'live'.
- Probe recipe (web): throwaway `web/src/state/zzqprobe.test.ts` that drives replay.ts pure functions + replayFeedStep/feedReplayBar/placeReplayOrder like App's effect, logging with appendFileSync to the scratchpad; run `npx vitest run <file>` from web/; delete it afterwards.
- Phase 9 round 3 (2026-10-26), verified: timeframeProvider never emits a bucket that is not `complete && seeding===null`; mid-bucket entry and post-drop buckets are REST-seeded first, failed ended buckets go to onRepair (never emitted closed); REST aggregation drops the partial oldest bucket unless history starts there, parallel pages spaced BASE_PAGE*base never leave holes. regimeSourceBars' `raw` fallback (whole raw incl. forming row, formingLast false) is unreachable from replay because replay always drops load.bars' last row. Ribbon points filtered to appliedRef times.


## Universe / data layer (added 2026-10-26)


- Run fingerprint = sha256 over a *selective* `lab_values(cfg)` in lab/lab/research/runs.py, not the raw lab.toml text; adding a new lab.toml section does not invalidate frozen runs unless added there.
- Lab lock covers lab/lab/engine, registry, stats and config/success_criteria* only; lab/config/lab.toml and lab/lab/data are NOT locked.
- Candle convention (lab/lab/types.py validate_candles): UTC index, missing bars are absent rows (never NaN/ffill), zero-volume bars kept with tradable=False.
- Perturbation helper: tests/synthetic.py perturb_after(frame, pos).
- Archive download (lab/lab/data/archive.py): 404 month -> skipped; missing .CHECKSUM or sha mismatch -> ValueError aborts the remaining months of that symbol (earlier months stay on disk).
- Offline test run: `cd lab && .venv/bin/python -m pytest -q` (~35 s, 1146 tests as of 2026-10-26).

**Why:** saves re-deriving these on each review.
**How to apply:** verify quickly (grep) before relying on any item; code may have moved.

Phase 10 portfolio engine (lab/lab/engine/portfolio.py), verified 2026-10-26:
- Hand example (60/40, fee 0.1%, flat slip 0.05%) matches to the cent; budget includes fee (units = b/(fill(1+fee))), cash -= b exactly. 150-universe fuzz (holes, untradable, delistings, band 0/0.02): order-replayed cash never < 0, fees == sum(order fees), no units after a delisting, net_returns_for bit-equal to run_portfolio.
- Forced delisting exit at the symbol's last bar close (slipped, fee) and cancel of same-close decisions: honest; a buy filling at the last bar's open pays the round trip. Mixed ms/us/ns indexes union/reindex fine (pandas 3). BTC vol-target reindex fill 0 preserves compounded return.
- Cross-section perturbation tests have teeth (price shock, deleted bars, truncation, late listing, sanity test that later outputs change). Segment breaks in universe require a hole (relist_gap_days 30 or hole + 5x jump), eligibility needs 365 d since segment start, so a 28-row lookback across a break is always masked.
- Phase 10 wave 1 verified 2026-10-26: impact_inputs causal (adv/sigma at row j from rows < j, per segment; bar j's own volume/close do not move it); per-bar ADV + per-bar sigma is equivalent to daily under sqrt-time; NaN liquidity charged cap; gross run pays no impact; net_returns_for == run_portfolio with impact. Banding (buffer rule, state = previous decision), gate (resets state, all cash) and liquidity_share pass prefix-perturbation tests with teeth. alpha_beta HAC matches a hand Newey-West sandwich exactly; size 4-6% under heteroskedastic AR(0.3) errors; 365-day bars_per_year. Portfolio plans allow 9 tunables (documented deviation from 6).

Phase 10 wave 2 portfolio pipeline (lab/lab/portfolio_runner.py, research/portfolio*.py), verified 2026-10-26:
- Perturbing every candle at/after holdout_start (+ a holdout-only delisting, universe rebuilt) leaves run_backtest/adversarial/sweep/walk_forward/crosscheck bit-identical (scratchpad p10w2/probe_leak.py). Ranks read with parquet filter time < end; loader sliced < end; WF train/test use `data.through(window.end)`.
- Every benchmark and the null scored from `_first_decision` = first INVESTABLE schedule row (>= min_names sizable names in the ungated book; gate-closed cash rows count, round 2); ledger rows keep whole-window returns (zeros before) so DSR/V/PBO/family are commensurable. Null gets members, vol_panel, inverse_vol, max_weight, liquidity share+mult, impact, costs. p = (beat+1)/(n_paths+1); percentile over finite paths. alpha p two-sided (conservative with alpha>0).
- Ledger: base "api", 4 benchmark rows, neighbourhood (cap 37 = base + 9x4), Optuna incl. pruned infeasible as error rows (base plan_hash), WF train ("walk_forward_train") + test ("optuna", window_kind walk_forward_test), adversarial, holdout "manual". costs_json has "impact" key so V pools portfolio plans only. Crosscheck records evidence only (same as single-symbol).
- Crosscheck replay: independent accounting loop, but shares slippage_series/impact_inputs/impact_fraction AND the strategy's weights, so it cannot catch signal look-ahead or a cost-model bug. Tolerance 1e-8 relative.
- Fingerprint: lab_values "universe" now nests liquidity_cap_mult + "portfolio" (all pre-existing runs refuse). Manifest sha256 (covers parquet sha) frozen at start_run, checked by every portfolio tool + get_report + holdout. Portfolio holdout: same guard order as single-symbol (logic, family, crosscheck, pre-holdout, then spend under txn).
- Round 2 conventions: rebalance.offset pinned 0 server-side (schema const 0); delay check = k*every rows of stale inputs on the same rebalance rows; schedule phase = position from the panel's first row (data start, not window start), so folds/holdout share it; stale rank table cuts the design window and refuses the holdout.
- Fail closed without [portfolio] criteria table (3 rows missing). min_trades = filled rebalances (~1 per decision with min_rebalance 0, so 100 is weak).
- Phase 10 H4 null verified 2026-10-26: only `research/portfolio_criteria.py` reads `benchmarks.random_selection.percentile`; `vol_only_sidebar` is nested, not ledgered, not read by agents/. Default-off/vol-only null bit-identical to a5fa079 (qr/bitid.py, 4000 cases incl. RNG state). Kept targets the survivors cannot match go to the fresh band draw (need = k - kept matched, turnover unchanged). The lab-lock hook blocks shell commands whose text names `lab/registry` etc. even for reads: use the Read tool or python `inspect`.

Phase 11 (2026-10-28) verified conventions:
- Software record lives in `config["software"]` BESIDE `config["lab"]` (not inside hashed lab_values, so pre-Phase-11 runs stay readable); runs without the key are not checked. Lab's own code version is NOT recorded.
- `make_tpe_sampler(seed)` = TPESampler(seed, multivariate=True, constant_liar=True, n_startup_trials=10, n_ei_candidates=24, group=False); identical to 5.0's `TPESampler(seed=seed)` for single-objective (multivariate=None -> not multi-objective -> True). Deprecated knobs (gamma, weights, prior_weight, consider_endpoints/magic_clip) not recorded; version string covers them.
- purgedcv 0.1.11 conventions: SR ddof=0, skew/kurt bias=False (raw kurt), annualised V / bars_per_year, MinTRL 1+ceil, PBO input (n_configs, n_obs), logit < 0, CPCV purge by label overlap both sides. Measured return-level gap at n=2000, 50 seeds: PSR 1.3e-4, DSR 3.7e-4 (band 1e-3). Its effective_n_trials is a different estimator (autocorrelation time).

Phase 11 part 1 (2026-10-28) verified:
- MinBTL = expected_max_z(N)^2 / SR_annual^2 years (Bailey-Borwein-LdP-Zhu 2014); paper example N=45, SR 1 -> 4.998 y. Report: target = plan's observed design net Sharpe x sqrt(bars_per_year 365-day), N = the DSR's N.
- MinTRL at SR0 (DSR's deflated benchmark) and confidence = dsr_min: DSR >= c <=> n >= MinTRL is exact (same moments, same n-1); length reason appended only to non-pass dsr_min rows, never changes status.
- CPCV: contiguous groups (remainder to first groups), purge before / embargo (ceil 1% T) after each merged test block, AFML path rule; best-in-train per split on train mask only (hidden-bar perturbation test is strong). Reads ledgered sweep trials, adds no trials, DSR N unchanged.
- frozen_criteria: absent dsr_trial_count -> "nominal"; 11 real runs, none had the key on 2026-10-28; installed lab/config/success_criteria.toml has no key.
- trial_count: plan set == count_distinct_plans_research (SELECTION_SOURCES); window_kind preference design > WF train > WF test > custom; holdout excluded by kind only (no date guard). Real ledger kinds: design / walk_forward_train / walk_forward_test only. Global compute 9.7 s at M=4,198, T=2,464 days.

Phase 11 round 2 (2026-10-28) verified:
- holdout_check calls evaluate_plan (so plan_statistics) BEFORE record_holdout_check: any exception in the statistics block refuses without spending. Only the effective-N part is try-wrapped; CPCV/MinBTL/MinTRL failures would break get_report (fail closed).
- Optuna TPE 5.0 counts COMPLETE + PRUNED trials toward n_startup_trials; lab objectives never raise except TrialPruned (portfolio infeasible), one ledger row per trial; MCP guard+run hold a per-run lock, so same-run sweeps never interleave.
- plan_return_trials guard `window_end <= holdout_start` is right: Window is half-open [start, end).
