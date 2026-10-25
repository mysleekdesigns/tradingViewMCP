---
name: web-phase8-research-page
description: Phase 8 Research page (ResearchPage, researchApi, lab /lab/research/* readers) - confirmed design not to re-flag and open gaps after the round-3 review on 2026-10-24
metadata:
  type: project
---

Confirmed design (verified in code, rounds 1-3, 2026-10-24):
- ResearchPage is an overlay over chart+dock+sidebar (`.app > .research`, grid-row 2/-2, col 2/-2). Chart stays mounted. `dialogOpen` includes `researchOpen`; ResearchPage owns Esc (window listener, skips dialogs/defaultPrevented).
- While open, `main` and `.sidebar` get the React 19 `inert` prop; the dock gets `inert` via DOM in an App effect keyed [researchOpen, dockOpen]. Left drawing rail, header, right rail stay live on purpose.
- useResearchRuns (round 3): `schedule(trigger)` with triggers initial/response/visible. initial+visible always fetch at once (0), response uses runsPollDelay (offline 8 s, fresh live 5 s, stalled-only null, else idle 30 s). Hidden -> null + visibility handler clears the timer. Single-flight (`inflight` guard in schedule and load; load clears timer), refresh = load. StrictMode/unmount safe (disposed, abort, finally->schedule no-ops). Checked: no double timers, no hidden polling.
- Two error boundaries: outer (App, around the whole page, Reload resets tab to Runs) and per-tab (`key={tab}`, onError resets saved tab to Runs, "Try again" re-saves the tab then reset()). No reset loop: re-render only on user click.
- Plan restore persisted in localStorage (`tv.research.planRestore.v1`); App saves `restoreValid ? planRestore : null` so a lapsed offer is removed on first render; offer only valid while editor text === loaded && planEdited. Safe against stale resurrection.
- journalTarget cleared via journalTargetAfterTabChange when leaving Journal.
- Web never sends a research run id: both `labClient.backtest(plan)` callers omit runId (server default "ui"); no web adversarial call. Lab `_run` rejects RESEARCH_RUN_ID_PATTERN or registered run ids with 422.
- 404 mapping, budgetMeters, XSS (no innerHTML, safeHttpUrl) as in round 2.
- Interrupted agent runs stay outcome "running" BY DESIGN (orchestrator `resume()` accepts running/error); so "stalled" can be a permanent state.

Open gaps after round 3 (recheck next review):
- Stalled-only list returns null from runsPollDelay: one interrupted run in history permanently disables the 30 s new-run check (residual of the round-2 idle gap). Fix: return RESEARCH_IDLE_POLL_MS instead of null.
- INLINE_RE code-span alternative: ~48 ms per 8000-char backtick paragraph, ~2.4 s for a 400k hostile journal (round-2 "0.4 s" figure was the bracket case). InlineView is not memoised, so it re-parses on every runs poll. Fix: `(?<!`)(`{1,8})(?!`)` opener (measured O(1) on backtick runs) and/or memo MdBlockView.
- Server /lab/research/runs still recomputes budget_status per run per poll (now every 30 s idle too).

**Why:** saves re-deriving the polling/boundary/restore mapping next review.
**How to apply:** start later Research-page or lab-API reviews by re-checking these gaps. Related: [[web-phase7-strategy-tester]], [[web-conventions]].
