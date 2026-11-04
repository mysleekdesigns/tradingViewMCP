---
name: web-phase21-key-levels
description: Phase 21 key levels overlay + useDailyCandles (reviewed 2026-11-04) - confirmed lifecycle/tick design, open findings on partial-day range used and the daily refresh schedule
metadata:
  type: project
---

Confirmed sound (first review 2026-11-04; do not re-flag):
- KeyLevelsLayer follows SessionRangesLayer: same LayerHost attach/detach/sweep, BarLineage prefix check, closed bars only. A forming tick costs O(log n) plus a few small objects; `legendV` never moves on a tick.
- `view.bars` may be an older array on purpose (only open times are read); bar count or newest time changing bumps the version.
- `keyLevelsChanged` -> `emitValues` changes the snapshot, so legendMemo misses; the layout snapshot stays, so Chart itself does not re-render.
- useDailyCandles: at most one request or timer, abort + `signal.aborted` guards, same-render LOADING on provider/symbol change, cleanup `setResult(null)` stops A->B->A resurrecting old candles.
- LWC: primitive 'normal' views are in the main series' own paneViews, so later series (volume, indicator lines) paint over the labels; drawings at 'top' stay above.
- Coinbase newest page is not CDN stale (`pageEndParam` unaligned end), so a late partial 1d row is not a concern there.

Open at that review (recheck next time):
- Range used reads only loaded bars. App `LIMIT` = 500, so 1m charts lose the day after 08:20 UTC and seconds charts always do; the oldest loaded day is partial on every interval.
- `waitForNextDay` schedules from answer time, not send time: a load sent before midnight+10 s and answered after it skips that closed day for ~24 h (probe confirmed).
- Retries end after ~9 min, then nothing until the next midnight; the legend note fires only when `daily === null`, so a missing today is silent.

**Why:** LIMIT and the provider switch at `App.tsx` (`isSecondsTimeframe ? timeframes : TIMEFRAMES`) sit outside the phase's diff and are easy to miss.
**How to apply:** for any new "per UTC day" number on the chart, check it against loaded history length, not just bar correctness. Related [[web-no-trade-bars]], [[web-phase19-wave3]].
