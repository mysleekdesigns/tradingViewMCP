---
name: paper-protocol-review-2026-10-28
description: Phase 13 pre-registered paper-trade protocol review (agents/research/paper/protocol.py, compare.observe_paper, paper_trade.py) plus ledger hash chain; r1 findings, r2 status, open items and probe recipes
metadata:
  type: project
---

Phase 13, uncommitted on 39d1c80. Lab lock ON; ledger.py / mcp_server.py are not locked paths (recurring-issues #13).

## Round 1 (2026-10-28) findings and round-2 status
- C1 pass on missing candles + outcome rewritten: **FIXED r2** (coverage = rows vs expected bars + fill candles; decision with incomplete data = continue; frontmatter outcome write-once, compare short-circuits).
- C2 timing unchecked: **PARTLY FIXED r2**. Entries matched to a lab replay (greedy max matching +/-1 bar, rate matched/max(P,L) >= 0.8, early fail bound (hi-u)/hi is valid). OPEN: exits never enter the verdict (exit+3/+6 bars or exit_reason "roi" pass at the honest rate); lag_bars recorded but unused (one-bar-late bot passes 41% at 30 bps bar vol; early-1 passes like honest with -92 bps "favourable" median; shortfall is one-sided).
- W1 weekly multiplicity: **FIXED** (weekly = alerts; gross 4x tol on weeks + 3-fill windows; union bound printed; Gaussian-conservative ~2.4x, but t3 noise ~1.4x above it; accepted).
- W2 claim: **FIXED** (min WF OOS of plan_hash or owner_supplied; PSR headline reworded; min testable printed). Claim only sets horizon/mode, cannot raise the false-pass rate.
- W3 vacuous bands: **FIXED** (inconclusive upfront; <min_fills judged = inconclusive, acceptable).
- W4 unledgered/re-registrable: **PARTLY**. Needs a design trial, but design_trial_meta accepts source "manual" in ANY run (web UI); no holdout status read; duplicate check reads only gitignored agents/data/paper (delete folder -> re-register; journal not consulted).
- Note 2 flat-start blocks: **FIXED** (owner_entry >= t).
- NEW r2: one permanently missing bar turns a decision-bar fail into continue forever (no terminal state). Bands' honest false-fail unstated: 0-14% (mean ~7%) across design draws on a stationary random walk (overlapping weekly blocks ~13 effective samples). Candle source/warm-up not frozen (api vs archive replay differ for history-dependent features).
- Ledger _ensure_chain r2: plain-read pre-check + re-read inside BEGIN IMMEDIATE is sound; docstring "read-only opens never take the write lock" is false (_migrate takes BEGIN IMMEDIATE per migration on every open).

## Round 3 (2026-10-28, final) status
- FIXED: exits judged by kind (exit+-3/+6, roi mislabels: 60/60 fail); lag rule exists; two-sided vs-open shortfall (favourable -25 bps: 59/60 fail); gap -> final fail when the bot's best case still fails, else continue, then inconclusive at +7 d grace (verified 6.9 d continue / 7.0 d inconclusive); candle source+host+warm-up frozen and enforced in compare; Ledger.open_readonly (mode=ro).
- OPEN C (r3): lag-0 share >= 0.8 pooled over entries+exits is diluted. Honest lag-0 share is 1.0 (the bot runs lab compile_plan on closed candles), yet entries late on 1 in 3 pass 56%, all fills late on 1 in 6 pass 27%, stops a bar late pass 33%; headline still says "(lag 0)". Lab look-ahead shows up as paper LATE, but only EARLY is labelled "possible look-ahead".
- OPEN W: the bootstrap false-fail is NOT a floor (design seeds 102/104/108: boot 5.3/5.9/3.3% vs simulated true 0/0.8/0.8%; seed 100: 1.8 vs 10%), and the journal prints it bare. Non-blocking (fail direction).
- OPEN W: holdout death read by the exact stored logic_hash only (the lab uses family + recomputed logic); a no-op twin of a holdout-dead or paper-failed logic registers; holdout status (passed/unchecked) is not frozen in the protocol/journal, so a paper pass on a never-checked plan reads like a candidate pass. The real ledger has 0 holdout checks (latent).
- Probes: scratchpad p13r3/{band,frac,grace,stops}.py (copy of p13r2/common.py).

**How to apply:** for any forward-test verdict ask (1) coverage required at decision, and a terminal state if data never completes? (2) outcome write-once? (3) are BOTH entry and exit timing (and lag sign) checked? (4) is the total honest false-fail rate (bands included) stated? Probes: scratchpad p13r2/common.py builds a DesignStats from the lab engine on a random walk so honest bots pass (~90%); probe_lag2.py / probe_lowvol.py / probe_band.py / probe_gap_stuck.py. Related: [[recurring-issues]], [[lab-conventions]].

## Round 4 (2026-10-28, owner-approved extra round)
- r3 C (pooled lag-0 floor): **FIXED** for 1-bar shifts. Per-side entry_lag/exit_lag + joint lag_total cap max(1, ceil(0.05 x matched)); late = "possible look-ahead in the lab", early = forming candle. Reproduced: late1of6/late1of5/late1of4/early1of6 0/100, entrylate1of3 3/100, stops a bar late 7/60, honest 94/100.
- r3 W bootstrap wording: **FIXED** (false_fail_text: single-path, either direction, range 0-10%).
- r3 W family/holdout: **FIXED** for dead families (family_holdout_conflict, fail closed on uncertain-only); locked_split_check frozen.
- NEW C (r4): the 0.8 min_match_rate is the same dilution, one level up. A fill shifted >= 2 bars (outside +/-1 tolerance) or a skipped/extra trade is UNMATCHED, not off-lag, and is not in lag_total. Non-monotone: seed 1 (7 lab trades), shift trade 0 by 1 bar -> fail, by 2 bars -> pass "(lag 0)". late2_1of6 27/100, entrylate2_1of5 19/100, drop1of6 77/100 (honest 94). Headline also says "every reason matched". Fix: put unmatched (paper+lab, both sides) into the joint irregular-fill cap.
- NEW W (r4): ledger_link gives "passed" to ANY family member (re-tuned params, twin) when one member passed; lab criteria.py ~934-945 FAILS the holdout row for such a variant ("can never be holdout-checked"). Also ignores failed checks of uncertain same-feature plans when a certain member passed. Latent (0 checks in ledger).
- NEW W (r4, low): prepare --ledger takes any path; protocol does not freeze ledger path/chain head, so a copied/edited ledger can mint locked_split_check=passed.
- Engineering fixes (exit 2 before append_verdict, 429 bounded, comparison recreate only if outcome/asof/sha match) do not change verdicts.
- Probes: scratchpad p13r4/{far.py, one.py} (+ r3 copies).

## Round 5 (2026-10-28, final)
- r4 C (match-rate dilution): **FIXED**. Joint irregular count (unmatched paper+lab entries/exits + off-lag) vs max(1, floor(0.05 x all fills)). Reproduced: late2/late3/entrylate2 0/100, drop1of6/1of5 7/100 (the 7 = dropped trade is the last one, open at the decision bar: costs 1 irregular, inside the allowance; acceptable), honest 94/100.
- r4 W family "passed": **FIXED** (_family_checks, passed only if every check is this plan's own; family_checked_not_this_plan). r4 W --ledger: **FIXED** (passed_other_ledger). Residual: frozen chain_head is the design run's trial chain; holdout outcome lives in research_evidence (unchained).
- Gap: a missing candle can only turn a fail into continue -> inconclusive("data") after grace, never a pass (60/60 probe). 0-trade bot: inconclusive (0 judged fills < min_fills) or band fail, never pass. 4 x hi early cap is a valid bound.
- NEW W (r5): lab replay warm-up = startup_candle_count (max(200, 3x lookback)), but freqtrade dry-run's first fetch is ohlcv_candle_limit (1000 on Binance) candles (exchange.py tail(limit+startup)). For long EMAs the bot's signals differ near crossings: honest timing fail 2.5-4% (ema 3/100: floor 7/200 vs ceil 1/200), labelled "not running the plan". Fail direction, unstated. Fix: trim the bot dataframe to the frozen warm_from (or freeze warmup = candle limit).
- Probes: scratchpad r5q/{far,gap,zero,warm,warm2}.py. Note zsh: pass probe args separately, no "$a" word-split.
- r5 re-check (WARM_FROM fix, protocol v6): **FIXED**. Rendered strategy (freqtrade stubbed, exec'd) gives signals identical to the lab replay; compare refuses a mismatched/missing WARM_FROM line (None != int). 1h slide past WARM_FROM is benign (window >= 1000 + 3L, residual <= e^-6), not a false pass.
- NEW W (r5 re-check): start boundary. Lab replay masks signals to rows >= start (first fill start+1); the bot has no START gate, so a signal at bar start-1 (or a stale one at launch, prepare --launch runs before start) fills at bar `start` or holds a pre-start position: 3-4/200 honest timing fails (ema 3/100, 5/150) in a freqtrade-like per-bar sim (r5q/warm3.py); gating signals to bars >= start gives 0/200 (warm3g.py). Fix: render START, enter_long only where date >= START.
