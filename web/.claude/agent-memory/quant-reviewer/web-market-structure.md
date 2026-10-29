---
name: web-market-structure
description: Phase 16 market structure overlay (swings/BOS/ChoCh/flip zones) - engine causal; closedThrough gating + provisional flag + hole cap verified r4; open: silent stale reconnect (Binance) crosses holes / closes partial aggregated buckets
metadata:
  type: project
---

Round 1-2 (2026-10-29): engine is push-only, look-ahead only via back-dated timestamps; the
`marketStructure.rules.fixture.ts` kills every engine mutation tried.

Round 3: `liveCandles.ts` `unconfirmed` set + `closedThroughOf`; `structureBars.ts` caps replay
at live closedThrough. Open then: Coinbase corrected close repaint (W1), hole after failed gap
fill (W2).

Round 4 (2026-10-29, fix round 2): W1/W2 closed. Coinbase stream passes `onCandle(c, true, true)`
for a provisional close, settled by a later unflagged close; controller keeps it unconfirmed.
Hole cap (`setHoleCap`, lowest wins, cleared only by a non-empty REST result with token >
holeCapToken; a window starting after the cap replaces). `maybeRecover` = one gap fill per new
bar while stuck. 39/45 mutants killed; survivors are test gaps or equivalent: provisional lost
in the pre-load pending map, repair cap at `time` vs `time-1`, `>=` token, empty result clears
cap, highest-cap-wins, no-emit-on-reconnect, stop keeps cap.
- NEW open Warning: Binance `openReconnectingSocket` idle-stale reconnect is silent (no status),
  so no gap fill and no hole cap. Native: bar k final, k+1 missed, k+2 ticks -> closedThrough
  crosses the hole (probe P4). Aggregated custom TFs: `subscribeAggregated` keeps `synced`, so
  the stalled bucket closes final with missing base bars and the next one is "complete" from
  mid-bucket (probe P5, `probe3.test.ts`).
- Residuals judged Nit: custom Coinbase TFs drop base corrections (documented in README);
  non-newest REST row / stall-backfill last row on Coinbase treated final (needs out-of-order
  aggregation or a wake within REST lag of a boundary).

Harness: `scratchpad/ct` = copy of web/src + symlinked node_modules (re-copy src each round;
probes in `src/chart/probe2.test.ts`, `probe3.test.ts`), `run2.py <ct> [names]` mutation loop,
run vitest from the web dir with `--config <ct>/vitest.*.mts` (`--silent=false` for console).
Scratch dir is session-specific; recreate if missing.

**Why:** the fixture is the parity source for a future lab ChoCh feature (H4), and the overlay
must never draw from data that will later change.
**How to apply:** on later edits, rerun P1/P2/P4/P5 and the mutation loop first; check every
venue's reconnect path (status pulse or not) and "closed" semantics against the controller's
closed-and-not-provisional = final assumption.
See also [[web-range-indicators]], [[web-data-honesty-conventions]].
