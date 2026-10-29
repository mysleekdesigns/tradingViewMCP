---
name: phase16-market-structure-review
description: Phase 16 market structure overlay review rounds 1-3 - closedThrough/hole cap/maybeRecover design, open repairPending stall + provisional-through-aggregation gap, confirmed conventions
metadata:
  type: project
---

Round 1 (2026-10-29): W1 reconnect partial bar pushed as closed; W2 LWC 5.2.1 markers primitive leak.
Round 2 (2026-10-29): both CLOSED. `unconfirmed` set + `closedThrough`; mux one primitive per series.
Round 3 (2026-10-29): round-2 W (no recovery after give-up) CLOSED by `maybeRecover` + hole cap
(`holeCap`/`holeCapToken`) + Coinbase provisional closes. tsc + 288 related tests green.

Open after round 3 (check first next round):
- W: `maybeRecover` bails on `repairPending`, but a gap fill that fails with retries exhausted
  (recovery fetch, or the 3rd normal retry) never clears `repairPending` (onFetchFailed only
  schedules a retry that gives up). An onRepair landing during that fetch -> no request ever again,
  closedThrough frozen behind the cap. Reproduced in a scratch test (0 requests over 6 bars).
- W: `timeframeProvider.subscribeAggregated` onBase is `(c, closed)`: Coinbase provisional flag is
  dropped, aggregated custom TFs close (and mark done) on a provisional base close; settled
  correction is then ignored (st.done). closedThrough contract not honoured there.
- Nit: replace-when-window-starts-after-cap clause is effectively unreachable with limit >= 100s.

Confirmed conventions (do not re-flag):
- Replay never shows the forming bar; `structureClosedThrough` caps replay at live closedThrough.
- `unconfirmed` only holds times on screen; pruned on replace. Bounded (usually 1).
- Identical-values closed/settled tick -> one extra snapshot (only closedThrough moves), no
  series.update (applyLiveCandle returns the same ref). Intended.
- Coinbase settle worst case ~17 s (1.5 delay + 5 timeout + 5 retry + 5 timeout) < 1m bar, so
  provisional bars never look "stuck" to maybeRecover in normal operation.
- maybeRecover: one request per max(bar, 30 s), only on first tick of a new bar, sets retries
  exhausted so no timer chain; Retry-After/in-flight/scheduled retry all suppress it.
- Effect cleanup order and mux detach order as in round 2; only liveCandles consumes `closed`.

**Why:** the give-up/queued-repair paths are where unit tests stop and the overlay silently stalls.
**How to apply:** next round read onFetchFailed + maybeRecover guards and subscribeAggregated onBase
first. Related: [[review-watchlist]], [[phase14-chart-upgrades-review]].
