---
name: review-watchlist
description: Recurring risk areas found in web/ reviews (live data, gap fill, rate limits, history paging) to re-check on later phases
metadata:
  type: project
---

Status after the Phase 4 review (2026-10-24). Re-check open items when reviewing Phase 4+:

Fixed in Phase 3 (drop if they stay fixed):
- Gap-fill hole: `onFetched` now replaces the window when the fresh page does not overlap (`overlapsStart`), cancelling any older page. Cost: loaded history is dropped after a long outage.
- retryBlocked bypass on gap fill / auto reload: `requestGapFill`, `scheduleGapRetry`, and the error-state reconnect branch all check it now.

- loadOlder backoff (fixed after the Phase 3 review): escalating cooldown after any non-aborted failure, skipped while offline; 429/418 without Retry-After now block with defaults (60 s / 120 s) on every path.
- Idle-stale churn (fixed after the Phase 3 review): an idle stale reconnect on an opened socket stays 'live'; the post-reconnect gap fill is skipped unless a bar boundary passed; a rollover without a closed tick on the previous bar triggers one gap fill.

Still open:
- Per tick is O(n): `applyLiveCandle` copy, `planSeriesSync` identity walk, and in Heikin-Ashi mode `heikinAshiFrom` slices the whole prefix. No cap on loaded history.

Open after the Phase 4 review:
- `IndicatorSeriesManager` recolor path applies only `color`; line series keep the old `crosshairMarkerBackgroundColor`.
- Pane-legend measure effect: ResizeObserver callback overwrites the pending rAF id (`frame = 0`), so cleanup can miss one rAF.
- Any pane add re-applies stretch factors, discarding user-dragged pane heights.

Confirmed conventions (do not re-flag):
- LWC is 5.2.1: removeSeries auto-removes an empty pane (unless preserveEmptyPane); addSeries clamps paneIndex to panes.length, so panes are built in config order 1..n. `removeEmptyPanes` is a belt-and-braces no-op.
- Indicator path: Chart's candles effect passes the same `planSeriesSync` plan to `indicatorManager.applyPlan`; ticks use `IndicatorRunner.update` (checkpoint replay) + `series.update`, and `setData` only when the runner reports deleted points. Runner mutates its own output arrays in place, but every LWC call gets mapped copies and the legend reads values at render through a `useSyncExternalStore` version counter, so that is safe.
- StrictMode: the chart-create cleanup calls `indicatorManager.detach()` (clears configs), so the remount's sync effect recreates every series.
- Provider API takes `endTime` in UNIX seconds and converts to ms once (`binance.ts` getCandles); callers pass `oldest.time - 1`.
- Binance.US REST weight limit is 6000/min (checked live); default watchlist symbols are all listed.
- LWC keeps the view on prepend by itself (right-anchored rightOffset), so Chart's explicit `setVisibleLogicalRange(range + count)` is a no-op safety, not a double shift.

**Why:** these are the resilience/perf gaps most likely to come back as history length and symbol count grow.
**How to apply:** in later web/ reviews, grep `liveCandles.ts` / `binanceStream.ts` / `Chart.tsx` for these paths before writing new findings, and move items to "Fixed" once fixed.
