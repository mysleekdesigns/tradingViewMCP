---
name: review-watchlist
description: Recurring risk areas found in web/ reviews (live data, gap fill, rate limits, history paging) to re-check on later phases
metadata:
  type: project
---

Status after the Phase 3 review (2026-10-24). Re-check open items when reviewing Phase 4+:

Fixed in Phase 3 (drop if they stay fixed):
- Gap-fill hole: `onFetched` now replaces the window when the fresh page does not overlap (`overlapsStart`), cancelling any older page. Cost: loaded history is dropped after a long outage.
- retryBlocked bypass on gap fill / auto reload: `requestGapFill`, `scheduleGapRetry`, and the error-state reconnect branch all check it now.

Still open:
- `loadOlder` has no backoff after a non-429 failure (network, 5xx); the left-edge range listener retries on the next scroll event. A 429 with no `Retry-After` header also gives no block (retryAfterSeconds returns 0).
- WS idle-stale timeout no longer counts as a failure, but it still reconnects every 60 s on a quiet stream, and each reconnect triggers a REST gap fill.
- Per tick is O(n): `applyLiveCandle` copy, `planSeriesSync` identity walk, and in Heikin-Ashi mode `heikinAshiFrom` slices the whole prefix. No cap on loaded history.

Confirmed conventions (do not re-flag):
- Provider API takes `endTime` in UNIX seconds and converts to ms once (`binance.ts` getCandles); callers pass `oldest.time - 1`.
- Binance.US REST weight limit is 6000/min (checked live); default watchlist symbols are all listed.
- LWC keeps the view on prepend by itself (right-anchored rightOffset), so Chart's explicit `setVisibleLogicalRange(range + count)` is a no-op safety, not a double shift.

**Why:** these are the resilience/perf gaps most likely to come back as history length and symbol count grow.
**How to apply:** in later web/ reviews, grep `liveCandles.ts` / `binanceStream.ts` / `Chart.tsx` for these paths before writing new findings, and move items to "Fixed" once fixed.
