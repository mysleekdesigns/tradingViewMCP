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
