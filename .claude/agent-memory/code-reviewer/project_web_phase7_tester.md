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

Open gaps (recheck next review):
- App history auto-load effect stalls after 2 consecutive failed pages or a Retry-After > 3 s (single 3 s retry vs 4-8 s cooldown; retryBlocked not in deps); spinner forever.
- historyPlan ignores window.end: at 1m/5m/15m the 20k-bar cap never reaches the design window (window ends 2025-10-01), so "Load full history" loads 19.5k holdout-period bars and shows 0 trades.
- focusRange is never cleared: re-applied when Chart remounts; and Zoom/trade focus outside loaded bars clamps to a 1-bar range.
- BacktestPanel (incl. full TradesTab) re-renders on every live tick (not memoised).
- Root `npm run dev` uses --kill-others-on-fail, so a lab exit (uv missing, port 8788 busy) kills server+web.
- Trades rows not keyboard-operable; resize separator lacks aria-valuemin/max.

**Why:** saves re-deriving the overlay/pane lifecycle and contract mapping next review.
**How to apply:** start later tester/lab-UI reviews by re-checking these gaps. Older web/server memories live in the repo-root `.claude/agent-memory/code-reviewer/` (see [[web-phase6-drawings-alerts]], [[web-conventions]]).
