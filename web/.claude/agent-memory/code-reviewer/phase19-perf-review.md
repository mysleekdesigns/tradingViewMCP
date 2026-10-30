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
