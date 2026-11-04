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
- "Not verified against a real freqtrade" is usually checkable: `~/.venvs/freqtrade` holds 2026.9 with
  `lab` installed. Run `prepare_candidate` into the scratchpad, then real `freqtrade backtesting`
  (no `trade`). Sandbox needs `allowed_domains: data-api.binance.vision` and scratch-only
  `ccxt_async_config.aiohttp_trust_env = true`. Read freqtrade's source for order of checks too.
- To prove a filter's effect, use settings that cannot fire and change one config value; any entries
  still missing are not that filter's doing.
- Anything that removes lab fills before judging: ask what else could cause the same absence, whether
  the removal is attributed to a recorded cause, and whether the source of truth covered the window.
- Values read from a non-frozen file (config.json) by a frozen control: try re-`prepare` with a flag.
- Size or risk limits: compute the real stake, not the labelled level. Caps, vol targets and multipliers
  can each move it.
- Correlations of paper P&L: a mostly-flat series reads as independent. Compare with the coins' own
  correlation on a simulation before trusting N_eff.
- Compare HEAD and the working tree on copies of frozen tests: `git archive HEAD` into the scratchpad,
  hash the rendered strategy, config and protocol under each.
- Identity checks: `logic_hash` drops every number, the stop and the interval. Use `plan_hash` when the claim
  is about these exact parameters (a proof, a size), and try a re-tuned plan against any such gate.
- Freezes: list each file a frozen control's meaning depends on, then check each is compared whole. A byte
  check on the strategy says nothing about `config.json`.
- Anything that can exit 2 forever: ask whether a losing test then never reaches a recorded verdict.
- In this sandbox `ps`/`pkill` fail, so guard scratch `multiprocessing` code with `__main__`; a Bash command
  naming a locked lab path with a redirect, or order-endpoint words in a grep, is refused by hooks.

**Why:** these were the gaps in the Phase 17 and Phase 20 reviews; the look-ahead path itself was clean.
**How to apply:** run through this list on any forward-test, paper-trade or journal-freeze change.
See [[phase17-shadow-review]] and [[phase20-risk-review]].
- A new "cannot judge" outcome: test the same losing bot with and without the failure. It must not outrank a rule
  that does not depend on the broken part. Compare the code with the frozen rule text the protocol prints.
- Compare-style checks reached before `_judge` bypass any grace logic inside it: list the exits ahead of it.
