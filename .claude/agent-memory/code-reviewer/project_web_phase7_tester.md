---
name: web-phase7-strategy-tester
description: Phase 7 web Strategy Tester (BacktestPanel, labApi, BacktestOverlay) - confirmed design not to re-flag and open gaps from the 2026-10-24 review
metadata:
  type: project
---

Confirmed design (Phase 7 review, 2026-10-24; verified in code):
- labApi matches lab/lab/api.py + report.py: routes under /lab, 422 `{errors:[{path,message}]}`, 503 `{detail}` = no-data, Vite proxy answers 502 when 8788 is down (Vite 8 proxyMiddleware) -> offline. Percentile is 0-100; fold index is 0-based (UI adds 1); window.end is exclusive (holdout_start 2025-10-01); ledger newest first.
- BacktestOverlay: markers/equity filtered to loaded candle times (never stretch the time scale); equity pane removed (suspendPane) before indicator pane changes and re-added last; LWC 5.2.1 removeSeries auto-drops an empty pane (_cleanupIfPaneIsEmpty). Unmount: detachChart(false)/detachSeries(false) skip a dead chart. Chart-type switch re-attaches markers. StrictMode-safe.
- Runs are deliberately not aborted when the dock closes (report waits in App state).
- Lab plan template validates for every chart interval except 1w (lab has no 1w).

Auto history + zoom (added 2026-10-24, second review): autoHistoryStep state machine in backtestView.ts, keyed run_id#trial_id@symbol:interval, kept in a ref in App (StrictMode double effect dispatches 'start' once). Completion is detected via historyActive (firstBarTime > target) going false; stop/fail -> no zoom. Effect deps change on candleCount/firstBarTime, not per tick. History target survives a market switch, so returning mid-load resumes it (pre-existing manual semantics).

Open gaps (recheck next review):
- autoHistoryStep new-key branch uses `plan.needed && !exhausted`, not `!historyComplete(...)`: when the chart already holds >= 20k raw bars (after an earlier full load), plan is partial/bars 0/target == firstBar, 'start' never makes historyActive true, state stuck 'starting', no zoom for every later report on that market. Same family: a capped load ending with < 20k bars (exchange gaps) is not "complete" -> no zoom. Reproduced with a script.
- History chip and panel note are both role="status" with the same per-page-changing text (double, chatty announcements).
- Probably fixed (seen in code 2026-10-24, not re-verified in depth): retry loop is now setInterval + retryBlocked in deps; unreachable windows are skipped; focusDone clears focusRange; testerLink memoised.
- Root `npm run dev` uses --kill-others-on-fail, so a lab exit kills server+web.
- Trades rows not keyboard-operable; resize separator lacks aria-valuemin/max.

**Why:** saves re-deriving the overlay/pane lifecycle and contract mapping next review.
**How to apply:** start later tester/lab-UI reviews by re-checking these gaps. Older web/server memories live in the repo-root `.claude/agent-memory/code-reviewer/` (see [[web-phase6-drawings-alerts]], [[web-conventions]]).
