---
name: web-phase6-drawings-alerts
description: Phase 6 web drawings/alerts (controller, TimeIndex, primitive, drawings store, alerts store) - confirmed design not to re-flag and open gaps from the 2026-10-24 review
metadata:
  type: project
---

Confirmed design (Phase 6 review, 2026-10-24; verified in code):
- Chart unmounts on every symbol/interval change (useLiveCandles shows `initialSnapshot` = loading), so controller/adapter state never crosses symbols. Chart-type switch keeps the chart and re-creates series + DrawingsPrimitive + alert price lines in the series effect.
- Effect cleanup order: chart effect is declared first, so on unmount `chart.remove()` runs before the series cleanup; the series cleanup uses `alive = chartRef.current === chart` and skips detach/removePriceLine on a dead chart. Not a leak.
- LWC 5.2.1 uses mouse/touch events, not pointer events. Chart cancels the consumed `pointerdown` (capture on container) which suppresses compat mousedown/move/up, so LWC does not pan; handleScroll/handleScale are also toggled off via onBusyChange. Pane hover cursor comes from the primitive's `hitTest` (LWC calls it on mousemove), not from Chart's pointermove (that only runs while busy).
- `pane.getHTMLElement()` is the pane `<tr>` (left axis td + pane td + right axis td), so Chart's `toPanePoint` subtracting the left price-scale width is correct.
- TimeIndex: fractional logical by binary search + linear interpolation, extrapolation by interval step past either end; setBars is O(1) per tick. Indicators emit no forward-shifted points, so LWC logical index == candle index.
- ChartContextMenu/AlertDialog React onKeyDown stopPropagation does stop the window keydown listeners (React root is below window). Watchlist row Delete calls preventDefault so Chart's Delete handler (checks defaultPrevented) does not double-delete.
- Alerts stream and ServerProvider pass `fatalCloseCodes: [1008]` (the Phase 5 1008 reconnect loop is fixed). Browser notifications use `tag: alert-<id>` so tabs collapse.

Fixed after the first Phase 6 review (re-verified 2026-10-24, do not re-flag):
- Unsynced drawing loss: per-symbol pending set `tv.drawings.v1.<SYM>.pending` {upserts<=500, removed<=1000}, version-acked per id, fed into reconcileDrawings on load; dispose/pagehide send keepalive writes (skips ids whose latest version is already in flight); adoptMirror keeps this tab's dirty ids on top and does not write back (no tab echo); 4xx except 408/429 not retried. Scratch repro now passes (ids [a,b], price 150).
- Toast-over-modal: AlertDialogState.symbol is fixed at open.
- Ghosts: onCreate/onChange return boolean; controller drops pending on false; drags/creation reject price <= 0.
- Key scope: `chartOwnsKeyTarget` (state/chartTools.ts) = body or inside chart container, minus inputs/dialog/alertdialog/menu/listbox. Controller blurs outside focus on a consumed press; all dialogs have full-screen modal backdrops, so the blur cannot steal dialog focus.

Still open (2026-10-24 re-review):
- Escape is gated by chartOwnsKeyTarget too: after clicking a drawing-rail tool button (Chrome focuses it), Escape does nothing, and App ignores the 'cursor' shortcut command.
- Shared pending set across tabs: tab B's ack can drop an id tab A re-edited; only matters if A's newer write never lands. Narrow; note-level.
- A permanent 4xx drops the id from pending, so the next load lets the server list win (the drawing vanishes despite the "stays local only" log).

**Why:** saves re-deriving the event-consumption and lifecycle reasoning next review.
**How to apply:** start later web reviews of drawings/alerts by re-checking the open gaps; related [[web-phase5-server-sync]], [[review-watchlist]].
