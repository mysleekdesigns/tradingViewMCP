---
name: phase20-hmm-review-2026-11-04
description: Phase 20 Parts B, C, G lab review (causal HMM, persist, episodes): no Critical, causality verified, open warnings on semantic label switching, cache and freeze coverage, flat warm-up head, crossing gates
metadata:
  type: project
---

Reviewed uncommitted on 2026-11-04, before the Part D run. No Critical. Check the code before repeating any open item.

Verified, do not re-flag unless the code changes: prefix property and future rewrites (780 synthetic checks, gaps at cuts, blocks byte-equal); design load is `[start, holdout_start)` and never fits a holdout close; `overlay_block`, holdout view and `wf_fixed` start at the first decision; plan_hash, logic_hash and literal_vector identical on 5,552 ledger and paper plans between HEAD and the tree (script compares an exported `git archive` copy via PYTHONPATH); lab fingerprint identical under old and new lab.toml; seven paper plans hash to their meta.json; `lab.plan` imports in ~/.venvs/freqtrade with no hmmlearn.

Second pass the same day: W2 to W7 closed in code, each confirmed by probe or test (`hmm_stack`, `require_frozen_hmm`, `hmm_scored_start`, `_regime_change_reads`, `observed`, `HMM_UNAVAILABLE_ATTR`, `MAX_HMM_OVERLAY_LOGICS`). Family comparison with a longer-window overlay runs by timestamp on shared bars (`aligned_correlation`), so the HMM warm-up head no longer dilutes it. Hashes still identical to HEAD.

Still open after the second pass:
- W1 semantics: mean-return order is noise when state means are close. Real settings on synthetic two-vol regimes: 6 to 8 flips over 80 refits, state 0 is the calm state in about 30% of fits with equal means. `bear_high_vol` / `hmm_diagnostics` now report it and matched my probe exactly, but mean ordering is unchanged. The owner must decide before Part D; changing it after seeing the diagnostic is a new hypothesis.
- `_regime_change_reads` also refuses `not persist(b > 0.5, 3)`, a level gate and the entry twin of the Part D overlay rule. It fails closed, so honesty is unaffected. Only the pair `persist(c, j) and not persist(c, k)` needs refusing.

The permutation null is an information-only verdict, not a criterion: grep `permut` in `lab/research/criteria.py` finds nothing.

**Why:** Part D is one pre-registered shot, so a model property found after it cannot be fixed without a new hypothesis.

**How to apply:** for any regime model, test label meaning on near-equal means, not only separated ones. In the sandbox the spawn pool fails with PermissionError and falls back to serial, so pool equality is unproven by a direct probe; only the repo's pickling test covers it. Never run the HMM on real data before Part D. Related: [[overlay-review-2026-10-29]], [[derivatives-review-2026-10-27]], [[recurring-issues]].
