---
name: overlay-review-2026-10-29
description: Phase 15 risk-overlay review r1-r3 (uncommitted on 7680942) - r3 closed all r2 items; residuals: edge sweep of a REWRITE ported to overlay, near-BH overlays alike via common exposure
metadata:
  type: project
---

Phase 15 r1 (2026-10-29, uncommitted on 7680942). Re-check the open items before re-flagging.

Verified sound (do not re-flag unless code changes):
- Overlay compile: exits = flat_when (NaN through not/or/and -> invalid -> False), entries = ~exits, both False in warm-up; exposure at t+1 = 0 iff flat_when valid-True at close t; plan-level perturbation test covers 3-feature plan incl. derivatives.
- vol_rank / premium_mean_rank: pandas expanding rank(method=max, pct) with min 20 valid; warmup p+19 / p+18 correct; perturbation + truncation tests. Design and holdout both compile on history from the SAME file start (loader from data start), so design-bar ranks are identical in both views.
- Overlay and BH scored on identical bars (_decision_window = max(window start, warmup)); bar 0 flat for both; derivatives overlays start at the shared scored start.
- CDaR closed form = CUZ / Rockafellar-Uryasev discrete with fractional tail. break_even walk stops at first failure. 365-day bars_per_year everywhere; block = ceil(days*bars/day).
- Bonferroni alpha/N on both rows is right: per plan it is an intersection-union test (no /2 needed), /N is the FWER across plans.
- Golden: test_report_golden passes against a `git archive HEAD` copy (needs agents/config too) and on the working tree, so the fixture is genuinely pre-Phase-15.
- check_question exact-match on kind, same as _validate dispatch; logic_hash payload excludes kind, so an overlay and the equivalent signal plan share a logic hash (holdout death across questions works).

Open at r1:
- C: lab/config/overlay_criteria.toml is NOT under the lab lock (protect-files.sh / guard-bash.sh only match success_criteria*); start_run's refusal prints the cp command.
- W: N_overlay counts logic hashes (params+literals stripped) while DSR N counts plan_hash; budget allows 6 proposals for 3 hypotheses, pbo row auto-passes "nothing selected in-sample". Journal text wrongly says "like the DSR's N".
- W (contract): overlay trials excluded from edge N -> stateless timing rules explored in overlay runs escape edge N when ported (same logic hash).
- W (contract): CDaR95 bar-level on BTC 1d design = 123 bars, ALL from the 2021-11 episode (probed) -> one-episode statistic.
- N: percentile bootstrap bias d* - dhat ~ +0.04 (0.3-0.8 SD) on sma200/vol_rank; direction conservative.

Probe scripts: scratchpad qr15/probe_boot.py, probe_tail.py. sma200 1d: exposure 0.63, CDaR ov 0.663 / bh 0.733 / matched 0.543 (fails), Sharpe 0.94 vs 1.12, SE of gap 0.28.

r2 (2026-10-29, same day, still uncommitted). Owner decisions: overlay trials COUNT in edge N, excluded from V[SR] pools (ledger _OVERLAY_RUNS_SQL); new row min_episodes_improved (share 0.5 strict).
Verified r2: refusal before any trial in run_backtest/adversarial/crosscheck/holdout (adversarial base trial is source "api" so it burns the logic); error rows count; renames/alias/feature order canonical; unused feature rejected by validator; other interval refused (interval not in logic hash); other symbol = new logic by design. Episode window r[peak+1 : recovery+1] from own equity at peak, correct. Strict majority + _need correct. Underpowered forces fail in beat/sharpe/break-even/wf; equality alpha/N == 1/(1+B) passes correctly. Edge N SQL unchanged (includes overlays); golden byte-identical. 344 lab + 26 agents tests pass.
Open at r2: C hook (overlay_criteria.toml; guard-bash.sh also lacks research|search); W untuned rule syntactic (equivalent rewrites with new params accepted -> pbo auto-pass false); W edge-run sweep of the signal twin ported to overlay, not in N_overlay; W disclose dev probes (sma200/vol_rank30>0.9 on BTC 1d, probe_boot.py also supports 4h sma1200/vol_rank180) in seen-data declaration. Notes: PLAN.md "counted by logic like the DSR's N" still there; per-run lock lets concurrent overlay runs race the untuned check.
Probe scripts r2: scratchpad qr15r2/test_probe_untuned.py, test_probe_adv.py, test_probe_edge_port.py (run from lab/ with PYTHONPATH=. UV_CACHE_DIR=$TMPDIR/uvcache uv run --frozen pytest -s).

r3 (2026-10-29, closure). All r2 items closed: hooks now cover overlay_criteria* and research|search; check_overlay_untuned(ledger, plan) rules 1-3 (logic hash, same feature multiset + param_key incl. interval across overlay subs, same logic >1 or other param set in non-overlay runs incl. neighbourhood/optuna); pbo_max fails when >1 overlay submission in the behaviour family (symmetric, both fail); overlay_lock = lab-wide flock db_path.parent/locks, after run_lock, in run_backtest/adversarial/crosscheck/holdout; journal seen-data declaration lists the sma200/vol_rank30 probes and unconfirmed 4h variants.
Probes r3: scratchpad qr15r3/test_probe_r3.py. Literal rewrite same params (close/ma < 0.97) accepted -> pbo fails both (closed by pbo). Residual: edge run_sweep of a REWRITE (close/m >= 1, different logic hash) then overlay at the swept-best period -> accepted, pbo pass "nothing swept", N_overlay 1, rewrite NOT in overlay family (families compare base trials, not optuna trials). Not live in real ledger (no edge optuna/sma/vol_rank/premium trials on BTC 1d). Accepted residual; holdout still one-shot.
Rule 2 ignores symbol (param_key has interval, no symbol): same features other params on ETH refused - over-refusal, fail closed.
Real run r-20261029-173704-59d618: premium + vol_rank overlays 95-99% invested correlate >0.8 through common BH exposure -> pbo fails both. Judged a Note (fails closed, power only; return-correlation family cannot separate near-BH overlays; a fix would correlate active returns r_ov - r_bh or flat-bar overlap). Same artefact can share a holdout across near-BH overlays/edge plans.
