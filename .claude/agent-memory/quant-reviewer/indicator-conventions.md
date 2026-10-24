---
name: indicator-conventions
description: Confirmed conventions and review gaps for web/src/indicators (Phase 4) - stepper/clone/runner design, TV formulas, known weak spots
metadata:
  type: project
---

Phase 4 indicators (reviewed 2026-10-24) use a stepper design: `next(candle)` + deep `clone()`; `IndicatorRunner` checkpoints a clone after bar n-2 and replays the tail. Confirmed conventions: SMA-seeded EMA/RMA, Wilder RSI (avgLoss==0 -> 100, then avgGain==0 -> 0, first value at index `period`), Bollinger population stdev, Stoch HH==LL -> 50 (deliberate deviation from TV's na), VWAP hlc3 reset on floor(time/86400) UTC, no point while session volume 0. Warm-up bars emit no key; `stepInto` drops non-finite values.

**Why:** these are the agreed conventions, so don't re-flag them as bugs; review effort should go to the runner contract and test coverage.

**How to apply:** when reviewing indicator changes, check that (1) perturbation tests use exact equality (toEqual/toStrictEqual), (2) the runner's `from` contract holds. In 2026-10 the `from === n` early return trusted the caller even when n > previous length, which silently dropped appended bars. Also check (3) that runner property tests cover the real registry defs, not only fake defs. A temporary probe test file in src/indicators (deleted afterwards) is a quick way to verify exactness for every INDICATORS entry.

Follow-up 2026-10-24: W1 fixed (guard is now `from === n && n === this.length`) and runner.test.ts has a describe.each(INDICATORS) random tick/append test. Note that Chart's planSeriesSync never produces from === n for an update plan, so W1 was only reachable by other callers. The chart path (src/chart/indicatorSeries.ts) passes the REAL candles to the runner, never Heikin-Ashi bars. Histogram colours use the previous point from the patched runner.output, which matches setData colouring. Verified with a throwaway probe: a fake series that keeps a time->point map, with update() throwing on an older time, run through 5 seeds x 400 random ticks/appends over all INDICATORS. Its state equalled the full recompute, values and colours. Reuse that probe shape for future chart/runner changes.
