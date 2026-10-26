---
name: recurring-issues
description: Statistical/accounting traps found in lab/ and agents/ reviews (Phase 7 stats traps; Phase 8 run/interval laundering, V[SR] scope, logic_hash/family evasion, WF lineage bypass incl. literals, PBO floor; round 4 price-literal time switch, no-op+retune); journal guard wording traps - re-check on every lab/agents review
metadata:
  type: project
---

Found in the Phase 7 review (2026-10-24). Re-check each one whenever `lab/` or `agents/` changes, because they are easy to reintroduce.

1. **Permutation null drift.** `adversarial_checks_signals` permuted the WHOLE `hist` (2018 warm-up plus the 2019-2025 window). That moves the 2018 bear bars into the window. On real BTC 4h the window log-return fell from 3.43 (real) to 1.80 (mean over permuted paths). The EMA 20/50 p-value was 0.049 this way and 0.195 when only the window was permuted. Fix: keep the warm-up rows as they are and permute only from the window start (Masters' method). Tests used driftless random walks, so they could not catch this.
   **How to apply:** any null that permutes or resamples must keep the evaluated window's drift. Test it on a trending synthetic series.
2. **Short equity can go below zero.** vectorbt `shortonly` does not liquidate. The engine raises ("equity exhausted") only if the cover fails at the exit. Equity that goes negative mid-trade and then recovers produces nonsense per-bar returns (for example -1.4 on a bar where equity rose) and a max drawdown below -100%. In the real sign flip, MDD was -1.009 and Sharpe -0.12 on a -99.96% loss.
   **How to apply:** check that any short or leveraged path treats value <= 0 as blown.
3. **Walk-forward rows are not OOS.** `runner._walk_forward` slices one fixed plan into test folds and never re-optimises on the train folds. They cannot serve as the Phase 8 "walk-forward OOS Sharpe".
4. **DSR N is scoped to a client-chosen run_id** (`RunBody.run_id`). A new run_id per request resets N. Phase 8 must assign run_id server-side.
5. **PBO comes from the 8 ±20% neighbours plus the base plan**, not from the run's real search matrix. It measures parameter stability, not selection overfitting. The runner's warning thresholds are hard-coded (PBO 0.5, not the Phase 8 limit of 0.2).
6. **DSR V[SR_n] mixes windows.** `selection_sharpe_variance` takes the latest per-plan Sharpe across all of the run's windows. Optuna WF test rows are `source="optuna"`, so they overwrite a plan's design Sharpe with a 6-month test Sharpe. This is conservative but inconsistent. Scope it by window.
7. **Cross-check stop slippage.** nautilus does not slip stop fills, so `agree` is False for any stop plan with slippage > 0, and that difference hides any real stop-mechanics bug. Verified on 2026-10-24: with zero slippage the 3% stop agrees exactly (74 stops), and the sum of log(vb_exit/nt_exit) over the stop exits equals the whole equity gap. Fix: add a zero-slippage mechanics pass plus a slippage attribution.
8. **Cost model is not in the ledger rows.** If config costs change (vol_mult 0.5 -> 0.1 plus a cap, 2026-10-24), the trials in one run_id mix cost models without any trace.
9. **Config fields the engine ignores.** `slippage_cap` was added to CostModel and lab.toml, but `slippage_series` did not apply it, which leaves a dead knob. Grep each new config key through to its use.

## Phase 8 review (2026-10-24), research layer

10. **Run laundering (follows from #4).** `start_run` (MCP) is unlimited when the server is not pinned (`.mcp.json` has no env). DSR N, the proposal budget, the holdout budget, and the "already holdout-checked" guard are all scoped to one run. Probe: a fresh run with only EMA 20/50 BTC 4h gave DSR 0.976 (pass) at N=9. So: search in run A, then re-submit the winner alone in run B.
    **How to apply:** for any run-scoped statistic or guard, ask whether the agent can mint a new scope.
11. **Degenerate walk-forward space.** `resolve_space` accepts low==high, and evidence is latest-wins. Sweep on the full design window, submit `best_plan`, then call walk_forward with a zero-width space: the "OOS" folds are in-sample for the selection, and N only goes up by 1 (probe: 9 folds, 1 distinct plan). Check that WF space width is enforced and that repeated WF calls are not cherry-picked.
12. **plan_hash includes cosmetics.** It hashes name, description, raw expression whitespace, and alias names. The holdout "one check per plan / dead hypothesis" rule is keyed on it, so a renamed copy dodges the rule.
13. **Lab lock coverage.** `protect-files.sh` locks only engine/registry/stats/success_criteria*. These are not locked: lab/lab/research/ (criteria.py and holdout.py judge the bar), mcp_server.py, ledger.py, runner.py, search/, evaluate/, and lab/config/lab.toml (costs, holdout_start, holdout_budget). Re-check whenever trust-critical code moves.
14. **Holdout metrics exposed.** Holdout trials are ordinary ledger rows (`window_kind="holdout"`, `source="manual"`). get_ledger filters them out, but Phase 7's `/lab/ledger` and the Research page's Ledger tab show their Sharpe, return, and MDD.
15. **$ budget fails open.** In sdk_runner, `total_cost_usd or 0.0` charges $0, and an SDK exception also charges $0.
16. **Crosscheck evidence is never written.** The orchestrator and UI still call a plan a candidate that "passed every success criterion".

## Phase 8 round-2 re-review (2026-10-24)

Fixed since round 1: global DSR N (`count_distinct_plans_research`), holdout refused per `logic_hash` in any run (inside BEGIN IMMEDIATE), WF width/lineage check, WF = min over the logic's WFs, flock per run, lab.toml fingerprint, holdout rows hidden from /lab/ledger, $ unknown charged at cap, candidate needs crosscheck matched.
Holes that remain (re-check):
17. **V[SR] is still run-scoped.** N is global, but V comes only from this run. In a fresh run V is just base + 8 neighbours. Probe on BTC 4h EMA: V fell 8.5x (1.98e-5 to 2.33e-6), and DSR at N=2000 went from 0.977 to 0.9994. One of 60 sweep trials flipped from fail to pass at N=77.
18. **logic_hash is syntactic, so it is easy to dodge.** A no-op clause (`or ema_fast < 0`, `and rsi > -1`), a stop_loss_pct key, or a different interval gives a new hash. That buys a second holdout look (in a new run) and escapes WF-min and lineage. It also collides the other way: swapped periods (trend vs counter-trend) and rsi<30 vs rsi<70 share a hash. Suggest a behavioural key (design-window position or returns correlation).
19. **WF lineage only looks at same-logic sweeps in the same run.** It can be bypassed by sweeping in another run, sweeping a no-op variant, picking params by hand through run_backtest/neighbour hill-climbing, or using the Phase 7 API. Probe: a narrow ±20% WF around the full-window optimum gave OOS Sharpe 1.11 (passes 1.0), while the honest wide space gave 0.41.
20. The Phase 7 `/lab/backtest` accepts research run ids (RUN_ID_PATTERN matches `r-...`). That bypasses the guard, the fingerprint, and the flock.
21. The journal run summary passes `ledger.count(run_id)` (raw per-run rows) but labels it the global distinct-plan DSR N.

## Phase 8 round-3 re-review (2026-10-24)

Closed (probed): #17 (V = max(run V, research-wide V); fresh-run winner now gets V 1.98e-5 not 2.33e-6), #18 single-change dodges (no-op clause, never-hit stop, interval alone: family/logic catch them, corr 0.95-1.0), #19 same-interval sweep-elsewhere + narrow WF (refused), #20 (/lab/backtest 422 on research ids), #21.
Still open:
22. **Composite dodge: no-op clause + another interval.** Family correlation only compares trials on the SAME interval/window, logic hash differs because of the no-op. Probe: 4h EMA 29/150 holdout-checked; 1h EMA 116/600 `or a < 0` -> family = own logic only, `family_holdout_conflict` None (second holdout look), WF lineage = own neighbours only (narrow WF), V from 9 fresh-interval Sharpes. Fix idea: aggregate finer returns to the coarser grid BY TIMESTAMP (position alignment gave corr 0.03 because of 39 missing 1h bars) and correlate across intervals.
23. **Numeric literals are invisible to WF lineage.** logic_hash strips literals ("tuned numbers") but tunable_params has only feature params + risk keys, so hand-tuning `rsi < 20..45` via run_backtest leaves tried_ranges untouched and a WF over r.period passes lineage. Fix: refuse WF when same-logic family plans used other literal values (or make literals tunable).
24. **V scope by interval is launderable.** Same-logic winner moved 4h->1h (periods x4) in a fresh run: V 4.9e-7 from base+8 neighbours, DSR 0.9997 vs 0.975 at the 4h-scaled V (did not cross 0.95 here). Fix: min commensurable-plan count or pool annualised SR variance across intervals.
25. **PBO sweep basis has no size floor.** A 2-trial sweep with one dominated column gives PBO 0.0, so "PBO requires a sweep" = neighbourhood PBO when the agent hand-picks instead of sweeping.
26. Correlation data (BTC 4h, ~56% exposure long-only MA crosses): same-logic param shifts 0.85-0.99, EMA vs SMA same periods 0.88 (escapes 0.9 family), RSI MR vs EMA <= 0.34. 0.9 does not starve distinct ideas; it is evadable by feature swaps.

## Phase 8 round-4 re-review (2026-10-24)

Closed (probed): #22 (timestamp-aligned cross-interval corr: 1h x4 + no-op vs 4h = 0.997, refused; SMA swap + 1h + no-op 0.927 refused), #23 (literal_vector: 6 RSI thresholds refuse WF), #24 (pooled cross-interval V 4.3e-6 vs run 4.9e-7), #25 (min_pbo_trials 16), 'ui' trials in N/family/lineage, orchestrator cross-check before holdout.
Still open:
27. **Price-level literal = time switch (holdout reuse).** Validator accepts `close > 50000` (also volume/atr/macd/ema vs literal). "No prices" is prompt-only (designer.md, critic). Probe: 29/150 checked; `crosses_above(a,b) and close > 50000` at 24/120 has design corr 0.38 (allowed) but HOLDOUT corr 0.895 with the checked plan (BTC holdout min 58k). Fix: dimensional check in lab/lab/plan (price/volume-scale expr vs nonzero literal -> reject).
28. **No-op + far re-tune.** Logic-hash rule ("re-tuning is not new") collapses to the corr>=0.8 rule once a no-op is added: EMA 5/20, 8/30, 10/40 (+`or a < 0`) vs checked 29/150 (corr 0.67-0.76) all ALLOWED. Bounded by the family threshold. Fix: re-instantiate each checked logic at the candidate's params (o: tokens) and correlate, or strip BoolOp operands/risk keys and re-hash.
29. **Literal poison over a broad family.** wf_lineage_check refuses WF when ANY family logic has >1 literal vector; at 0.8 EMA/SMA/ROC trend rules merge (0.88-0.89), so one hand-tuned `roc > 0`/`roc > 1` blocks WF for every BTC trend follower, any interval, for the ledger's life. MCP text says "for that logic". Conservative, but starvation.
30. Pooled V is scoped by exact costs_json; an owner cost change resets V to new-cost trials while N stays global (min_variance_plans met by 2 two-param backtests' neighbourhoods).

Related: [[lab-conventions]]

## Journal leak guard (2026-10-25)

31. **Guard placeholder/rewording traps.** `research.journal.guard_holdout` is a lexical regex (holdout|held out within 80 chars of a metric name or decimal, both directions). Any orchestrator text that names the holdout near criterion names (pbo, dsr, sharpe...) gets withheld, and a placeholder containing the word re-trips it (live run r-20261025-135224-41f19f lost 5 whole entries). Fixed by a neutral placeholder + second fail-closed pass. Pre-holdout notes now say "locked-data check" (a synonym the guard does not see): only safe because those branches run before `run_holdout_check` and print criterion names/statuses only. `_safe_text` splits on `[.;]\s+`, so "(incl. the" splits mid-sentence and can leave misleading fragments.
    **How to apply:** whenever journal wording or the guard changes, run the text through the REAL guard (tests must not use a looser fake) and check the "locked-data" wording never appears after a holdout check.

## Phase 9 wave 2 (2026-10-26): web replay, manual paper trading, regime ribbon

32. **Replay step-back = hindsight trading.** `replayFeedStep` returns NONE on a step back, and the replay paper account is reset only on start, re-pick elsewhere, or exit. Step forward 20 bars, step back 20, market buy at the old close, step forward, sell: +197.7 on a monotone-up probe series. TradingView has no step-back while trading. Fix: reset on any cursor decrease, or refuse orders while cursor < max cursor seen.
33. **Resting limit gets price improvement.** `sweep` fills a buy at min(limit, price/open). That is right for a marketable limit or a bar gap, but wrong for a resting limit crossed between sampled live quotes or across a reconnect: limit 60000 placed at 60500, next quote 59000, fill at 59000. Fix: a resting order (non-marketable at placement) fills at its limit.
34. **Live quote time = snapshot `lastUpdate`.** That value is bumped by loadOlder and refresh, not only by price changes, so a stale or offline panel's last close is re-fed as a fresh tick and can win `paperMarks`.
    **How to apply:** on any paper or replay change, probe P&L with a throwaway vitest file that writes to the scratchpad with appendFileSync. Console output is suppressed in this vitest config.

## Phase 9 wave 2 round 2 (2026-10-26)

Closed (probed): #32 (ReplayBook highWater: orders refused while cursor < mark, bars <= mark never re-fed, jump across the mark fills once), #33 (marketability judged at placement; resting limits fill at exactly the limit incl. gaps), #34 (priceStamp = wall-clock time the last bar's time/close changed; history load/refresh keep it; only status 'live' panels feed/mark).
Still open:
35. **Pick -> cancel = hindsight in the same replay account.** While picking, replay.visible is null, so the chart shows the full live history (all bars past the cursor). `replayFeedStep` treats picking -> active at the same cursor as "resumed" (NONE): cancel (or Esc, or re-picking the cursor bar) keeps the account AND its highWater, so the user trades on bars they just saw. Probe: start 100 back, pick, cancel, buy, +50 bars, sell -> +1702 on the monotone probe series.
    **How to apply:** any mode that shows the live chart mid-replay must end the replay account (reset) or keep the replay view; check every path back to 'active'.
    **Closed in round 3 (2026-10-26, probed):** replayVisible keeps the prefix <= cursor whenever cursorTime is set (re-pick included), pickAt caps a re-pick at the cursor, cancel/re-pick-at-cursor resumes the same account (nothing new was shown), an earlier pick resets at that bar, reconcile exits (reset) on a disjoint reload or empty source, pickAt(NaN) exits. Header quote, backtest overlay, regime source, legend, indicators all follow `replay.visible` during a re-pick.
36. Re-feed after reconnect/hide-show is benign: a quote whose stamp is unchanged is fed again when its panel returns to the live set, but a resting limit fills only at its limit, and only on prices the market really traded, so the fill is conservative (never better than the limit).

## Phase 9 wave 2 round 3 (2026-10-26)

No Critical/Warning left in replay/paper/aggregation. Accepted design (do not re-flag as Critical): outside the replay panel the app still shows live data (watchlist, other panels, Seasonals, Strategy Tester trades, alert dialog prefilled with the live last close, and the paper ticket flips to the LIVE account + live price during a re-pick because `activeReplaying` needs status 'active'). None of it feeds the replay account; TradingView parity. Note-level only.
