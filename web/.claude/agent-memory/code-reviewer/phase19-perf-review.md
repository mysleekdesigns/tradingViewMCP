---
name: phase19-perf-review
description: Phase 19 wave 2 (per-frame path) review 2026-10-30 - view-signature cache proofs, LWC 5.2.1 invalidation facts, open nits
metadata:
  type: project
---

Wave 2 round 1 (2026-10-30): sound, no Critical/Warning. tsc -b, eslint src/chart, 674 chart tests green.

LWC 5.2.1 facts checked in node_modules (reuse, do not re-derive):
- Primitives' `updateAllViews` runs only for invalidation > Cursor (pane-widget paint ->
  recalculatePriceScales). A crosshair-only move repaints the top canvas: zOrder 'top' renderers
  (drawings) re-run draw() with no updateAllViews. Hover-id changes and drags go through
  requestUpdate (Light), so all primitives update then.
- `indexToCoordinate` = width - (baseIndex + rightOffset - i + 0.5) * barSpacing - 1, and returns 0
  for a NON-INTEGER index (isInteger check). Fractional logicals must be interpolated by the caller.
- `timeScale().options().barSpacing` is the live `_barSpacing` (not the initial option).
  |x(1) - x(0)| differs from it by up to ~1e-9 px (float), almost always non-bit-equal.
- Prepend: x(logical 0) moves (baseIndex grows, rightOffset kept). Append while scrolled back:
  rightOffset compensated, x0 unchanged, visible time range unchanged. Same-count window shift
  (drop first + append) keeps x0 but changes the visible time range.

Signature-cache proof to reuse: every price-scale mode is affine (linear, %, indexed) or log in
price for fixed range/height/base, so y of two distinct live-measured ref prices fingerprints it.
Time -> x needs index mapping too: structure/session add visible-range equality (catches window
shift / scrolled-back gap fill), VP measures xFrom/xTo every frame, drawings use TimeIndex/bind
version. Skip-redundant-style-set code tracks state from null/NaN per draw call, inside save/restore.

Open nits: VP barSpacing now from logicalToCoordinate (~1e-9 drift vs options; pixel flip only
at exact .5); hline/hray price labels still format per updateAllViews (drag/tick, not crosshair).

**Why:** later Phase 19 waves and any new overlay will reuse viewSignature and these LWC facts.
**How to apply:** for a new cached layer, check which inputs are NOT live-measured (index
mapping, view data) and that each has a version bump or a measured proxy. Related:
[[phase18-session-ranges-review]], [[review-watchlist]].

Wave 5 (DRY) review 2026-10-31: sound, no Critical/Warning (tsc -b, lint, 3438 tests green).
Conventions now in place (do not re-flag):
- data/http.ts has TWO cores on purpose: `requestMarketJson` (AbortSignal.timeout + any; caller
  abort wins; ServerProvider adds `callerAbortWins` pre-check + abort-over-parse) and
  `fetchWithin` (setTimeout controller; timeout wins over caller abort; `read` runs before the
  timer clears, so body reads stay under the deadline). Per-client error classes via `failures`.
- `useController(factory, deps, key=deps)`: emit is ignored after cleanup (old hooks had no
  guard; emits DURING dispose still land). useAlerts key [client], deps [client, WebSocketImpl].
- chart/layers/base.ts: VP overrides `invalidate()` (resets frame cache); base detached() nulls
  geom then invalidates. LayerHost.applyColors compares colorKeys, same lists as old layers.
- util/color luminance = Rec.601 with anchored '#rrggbb'; all inputs are validated 6-hex.
- CSS: primitives.css is @import-ed first in index.css. Cascade check method that worked: diff old
  vs new parsed rules per file, confirm every removed (selector, prop, value) resolves the same in
  primitives, then look for equal-specificity competitors on co-occurring classes that sat BEFORE
  the old position (only those can flip). None found in wave 5.
Nit left: data/, state/ and indicators/ import chart/util/search (layering inversion).

Post-wave-5 tick tweaks review 2026-11-01 (HA in place, spliceTail, rememberPlotted): sound.
Fuzzed plottableCandles vs raw.filter (stale prev, older/sibling raws, merges, double ticks) and
heikinAshiFromInPlace vs heikinAshi: 0 mismatches. Facts to reuse:
- haRef (Chart.tsx) is the ONLY holder of HA arrays; every assignment is a fresh heikinAshi() or
  the in-place tail. 'update' plans have from >= prev.length-1, so series.update never throws.
- plottedOf is read only via filteredFromLineage (parent of raw); plottableCandles is called only
  from liveCandles ready(), once per new raw, in order -> grandparent pruning never hits the
  common path. Bursts (pending ticks after REST) always missed, pre-existing.
- Aliasing pitfall: tests that compare `tail.ha[i]` to `prev.ha[i]` after an in-place call are
  vacuous (same array). Save the element before the call.
Method that worked: bundle a scratch fuzz with web/node_modules/.bin/rolldown (no esbuild/tsx).
