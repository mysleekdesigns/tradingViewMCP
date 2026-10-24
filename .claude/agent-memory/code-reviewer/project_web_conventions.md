---
name: web-conventions
description: Confirmed web/ conventions (provider, chart lifecycle, time units, LWC font caching) and recurring review gaps seen since Phase 1
metadata:
  type: project
---

Confirmed conventions (Phase 1 review + UI pass, 2026-10-24):
- Direct provider defaults to Binance vision hosts (`BINANCE_REST` in src/data/venue.ts, see [[market-venue]]); `fetchFn`/`baseUrl`/`timeoutMs` injectable. `MarketDataError` kinds include network, bad-symbol, rate-limited (429), banned (418), region-blocked, http, parse, aborted; `retryAfter` in seconds. Requests use `AbortSignal.any([caller, timeout])`; candles sorted and deduped by time.
- Provider API takes/returns UNIX seconds; the ms/s conversion lives only in binance.ts.
- Chart.tsx: chart created in a `[]` effect; data pushed in a `[candles]` effect via refs; watermark (`createTextWatermark`) text updated in a `[symbol, interval]` effect; cleanup unsubscribes, detaches watermark, then `chart.remove()`. StrictMode-safe.
- App.tsx: one fetch effect keyed on `attempt` with `stale` flag + AbortController; Retry-After disables Retry/Refresh via a timer effect.
- UTC formatting helpers live in src/components/quote.ts (`formatUtc`, `formatUtcClock`, toISOString-based).
- LWC v5 text-width caches reset only when the font *string* changes; re-applying the same `fontFamily` repaints glyphs but keeps stale axis-width measurements.

Recurring gaps to recheck:
- Binance `endTime` is inclusive on open time, so Phase 3 pagination must pass `oldest.time - 1` or drop the boundary row.
- Header quote has `aria-live="polite"`; once Phase 2 ticks arrive it will announce every tick.
- Quote 24h basis needs >=1440 bars at 1m; LIMIT 500 shows "—" on 1m.
- 429/418 without Retry-After leaves Retry enabled immediately (no client backoff floor).

**Why:** these are the gaps the Phase 1 reviews found; earlier gaps (no timeout, no dedupe, 429/418 unmapped) were fixed by the UI pass.
**How to apply:** check these first when reviewing Phase 2/3 changes to src/data, src/chart, and src/components.
