---
name: review-conventions
description: Recurring things to check in this repo's forward/paper-test and pre-registration code (hash chains, frozen analyses, stopping rules, input readers, permutation dependence, stratified-test centring, git anchors)
metadata:
  type: feedback
---

Recurring checks that found real issues in this repo:

- Pre-registrations: check that the evaluation time / stopping rule is frozen, not just metric,
  test and min sample. An `asof` CLI flag without a frozen final asof = optional stopping.
- Hash-chained logs: an unkeyed chain detects nothing against a deliberate rewrite of a suffix;
  ask where the head is anchored (git-tracked file? report file?). `agents/data/` is gitignored.
  A local heads file is only as good as git: check whether anything compares it with HEAD and
  whether a header-only heads file passes.
- "Version" integers that pin formulas (SNAPSHOT_VERSION etc.) do not pin code; suggest a
  fingerprint of output on a fixed synthetic input stored in genesis.
- LLM model ids like "jev-latest" are floating aliases; check the per-response model is pinned
  AND that a missing served id is not silently replaced by the requested alias.
- Pooled trades across correlated coins judged in one batch are not iid for a permutation test;
  after same-bar clustering, also ask about cross-bar regime dependence (persistent features,
  overlapping holding windows).
- "Reproduces from X alone" reports: check every input reader fails closed in the FINAL report
  (a missing/locked DB that becomes [] plus a stdout note silently drops data).
- testing.md says permutation nulls with N >= 1000 are slow/excluded; the intent is runtime. Tiny
  closed-form permutation tests on frozen n run in ms; recommend keeping them default and asking
  the owner to clarify the rule rather than lowering n.
- Stratified permutation tests: check the statistic is CENTRED by its conditional null mean.
  Permuting within strata but scoring the pooled difference leaves fixed strata / unequal
  label shares as an offset, so a "two-sided" test turns one-sided and blind in one direction.
  Build a toy with opposite regime tilt (take share high in the bad block) to demonstrate.
- A git-anchor check that only warns when the file is uncommitted, or only compares with HEAD,
  is not an anchor for the FINAL read; ask for fail-closed at the final report.
- Frozen evaluation times: check the report CLI caps a user-supplied `--asof` at the wall clock.
  A future asof turns an early peek into the "final" report (Phase 17 r4 Critical).
- After stratifying, check the minimum-sample gate counts informative (both-label) strata, not
  raw trades; otherwise a powerless test reads as "no information" instead of "inconclusive".
- To verify a Monte Carlo permutation test, compare its mean p over ~20 seeds with an exact
  enumeration on a 6-cluster toy case (done for Phase 17 r2; unbiased).

**Why:** these were the gaps in the Phase 17 reviews; the look-ahead path itself was clean.
**How to apply:** run through this list on any forward-test, paper-trade or journal-freeze change.
See [[phase17-shadow-review]].
