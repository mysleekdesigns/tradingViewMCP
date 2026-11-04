---
name: key-levels-review-2026-11-04
description: Phase 21 key levels quant review: engine causal, three open warnings (sparse perturbation cuts, time-only closed daily test, stale right-edge label)
metadata:
  type: project
---

Phase 21 reviewed uncommitted on 2026-11-04. No look-ahead in `KeyLevelsEngine`: 4242 bar cuts × 3 gap rates, truncated, full and wild-future dailies all bit-identical. ATR matched Python Fractions.

Open warnings at review time (check the code before repeating them):
- `keyLevels.test.ts` perturbation cuts fall Thu/Fri and one Tue with no later daily, so a PWH leak from a later day of the current week survives all 4 test files. Only a Monday or Tuesday cut with later dailies catches it.
- `closedDailyCandles` tests time only against the local clock. A clock >10 s fast makes every midnight refresh read a forming row as closed; a stale body is also accepted, with no refetch for a day. The server already requires a following bar.
- `keyLevelGeometry` labels the latest visible step, so when today has no step the right edge shows an older PDH/PDL with no line and no note. `dailyError` beside candles is dropped by the layer.

**Why:** the mutation trick found what the green tests hid: value-level perturbation at a few cuts cannot see a day-of-week dependent read.

**How to apply:** for calendar-keyed indicators, mutate the read window and require cuts across all 7 weekdays. Accepted by design, do not re-flag: a week with any missing day gets no PW step; a gap restarts ATR and SMA. Related: [[indicator-conventions]], [[recurring-issues]].
