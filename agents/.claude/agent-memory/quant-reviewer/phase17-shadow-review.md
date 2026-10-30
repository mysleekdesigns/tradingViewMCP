---
name: phase17-shadow-review
description: Phase 17 Jev shadow judge reviews (r1-r4, 2026-10-29) - what is resolved, what stays open before genesis
metadata:
  type: project
---

Phase 17 Jev shadow judge (agents/research/paper/shadow*.py). No genesis existed at r1, r2 or r3
(no agents/data/paper/jev-shadow, no *-multi-jev-shadow.md), so everything is still fixable.

r1 (2026-10-29) raised C1 no frozen eval time, C2 unkeyed chain, W1 iid permutation, W2 formulas
unpinned, W3 floating model alias, W4 BTC identifiable, W5 no integrated perturbation test,
W6 warm-up == snapshot minimum. r2 resolved most; open after r2: final report fails open on an
unreadable DB, cross-bar regime dependence, served model fell back to alias, heads-file gaps.

r3 (2026-10-29, 200 tests green): all r2 items fixed in code (FinalReportRefusedError, served id
or None + requested_model, tip coverage + single-append `recovered`, git show HEAD prefix check,
genesis env). Stratified permutation is unbiased vs exact enumeration (6-stratum toy: exact
0.472, MC mean 0.471).
Open after r3 (Warning, fix before genesis): the stratified test permutes within (block, size)
strata but still uses the UNCENTRED pooled |mean(take)-mean(skip)|. Fixed strata (one cluster or
one label) and unequal take shares across weeks shift the null centre c away from 0, so the
"two-sided" test becomes one-sided in the direction of Jev's regime tilt and blind to a
within-week effect of the other sign up to |2c| (demo: within-week take +2pp, pooled -5pp,
p = 1.0; centred stat p = 0.021). Headline diff is confounded too. Fix: centre d by its exact
null mean E0[take_sum] = sum_s (n_take_s / n_s) * sum_s, take the sign from the centred value.
Suggestions: final report should require the heads file committed (git check is warning-only),
check committed history not just HEAD, flag pin == requested alias.

r4 (2026-10-29, 213 tests green, still no genesis): W-r3-1 closed. T = (S - E0[S]) / W is exact
(wreath-product enumeration with size-2/3 clusters + single-cluster mixed stratum: exact mean of S
== centre, T == weighted within-stratum diff, MC p 0.00235 vs exact 0.00231, sign-symmetric,
shift-invariant per block). S1 final_git_problems fails closed (missing, uncommitted, tip not
anchored, git unavailable, non-append-only history). S2 refusals all precede heads writes. S3 alias
echo -> no served id (exact string only).
NEW Critical r4: cli_shadow_report never caps --asof at the wall clock, so `--asof <after
evaluate_at> --write` before evaluate_at builds a FINAL report (outcome + p-value) from partial data
and squats report-<evaluate_at>.md (demonstrated on the cli_world fixture). Code-only fix, does not
touch frozen content. Warning r4: min-sample gate counts all trades, not trades in informative
strata, so a powerless read is labelled "no information" (decide before genesis: frozen analysis).
Note: informative-strata count/text ignores single-cluster mixed strata that do carry weight.

**Why:** the analysis freezes at genesis, so fixes must land before the first `shadow` run.
**How to apply:** on re-review, first check whether genesis exists; if it does, these are
reportable limitations, not fixable code. See [[review-conventions]].
