---
name: phase12-derivatives-review
description: Phase 12 web review (funding / OI panes from POST /lab/derivatives) - confirmed pane-order/lifecycle conventions and open items (replay refetch, no retry on transient lab errors)
metadata:
  type: project
---

Reviewed 2026-10-27 (uncommitted at the time). No criticals. tsc/vitest (2082)/lint green.

Open after the review (re-check in later rounds):
- useDerivatives effect deps include `formingLast`: entering replay directly (start N back /
  replay-from-here) re-runs the effect with an unchanged key and narrows coverage to the cursor,
  so playback past it refetches per step; the 250 ms debounce / 60 s gap timer is reset every step
  and never fires at 5-10 bars/s, so the panes stall until pause. Same structure as useRegime.
- Non-sticky errors (offline, timeout, http, parse) are stored as covering the window, so they are
  retried only when the next bar closes (a day on 1D) or on toggle; the chip tells the user to start
  the lab, which alone does nothing. useRegime has the same no-retry shape.
- beyondArchive coverage: freshly downloaded archive data appears only after toggle off/on,
  older-history prepend, or market change (documented in the hook).

Confirmed conventions (do not re-flag):
- Pane order: indicator effect suspends equity + derivatives, syncs, resumes derivatives then
  equity; the derivatives data effect suspends/resumes equity only when creating. Chart-create
  attach may build derivatives/equity in the wrong order (StrictMode remount), but paneKeyRef is
  reset to null so the indicator effect always re-orders on (re)mount.
- One series per derivatives pane; removal order OI then funding; zero price line dies with the
  funding series. Stretch captured on suspend/remove, not on detachChart(false).
- Values are point-in-time per (time, bar_seconds), so a request with stale candles after a
  symbol/timeframe switch still yields correct values for those times; stale answers are dropped
  by abort + `series` check.
- Replay safety: Chart filters points to plotted bar times (appliedRef) and the legend reads at
  or before the last plotted bar, so a live overlay reused in replay shows nothing right of the
  cursor.
- No React Compiler in this repo (eslint react-hooks only), so render-time reads of mutable
  managers (paneIndices(), equityPaneIndex()) driven by useSyncExternalStore versions are fine.

**Why:** these are the derivatives-pane risks most likely to resurface when Phase 14 touches panes.
**How to apply:** grep useDerivatives.ts deps and derivativesCovers before re-flagging; related
[[review-watchlist]], [[phase9-review-notes]].
