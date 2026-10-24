---
name: review-conventions
description: Confirmed web/ conventions and recurring review points (fonts in LWC canvas, quote 24h basis, refresh/Retry-After state) as of Phase 1
metadata:
  type: project
---

Confirmed conventions in web/ (Phase 1, reviewed 2026-10-24):
- LWC v5 resets its text-width cache only when layout.fontFamily/fontSize *string* changes, so re-applying the same font after fonts load is a no-op. The project starts on a system-only font string and switches to IBM Plex after document.fonts.load. Do not flag this switch as unnecessary.
- Header quote: intraday intervals measure against price at last.time - 24h (bar open if exact, else prior close), falling back to prev close with basis label. formatChange/formatDiff round before signing (no "-0.00").
- Failed refresh keeps the ready chart (state stays 'ready', error goes to the status bar). Retry-After lives in a seconds state that drives one setTimeout.

Recurring things to check:
- Direction colour (from pct rounded to 2dp) can disagree with the absolute change string on high-priced pairs (BTC +0.50 shows flat).
- Live regions (role=status) mounted already holding their text may not be announced.

**Why:** saves re-deriving LWC internals and intended semantics each review.
**How to apply:** verify that these still hold in the code before relying on them; when they do, skip re-litigating them.
