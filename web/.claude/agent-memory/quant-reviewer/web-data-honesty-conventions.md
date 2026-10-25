---
name: web-data-honesty-conventions
description: Confirmed conventions and residual weak spots in web/ data code (Coinbase aggregation and live-bar close, seasonals noise screen, lab-overlay venue gating) from the Phase 9 wave 1 reviews (rounds 1-4)
metadata:
  type: project
---

Confirmed sound (Phase 9 wave 1, rounds 1-4, 2026-10-25):
- Coinbase 4h = 1h rows on UTC 00/04 grid (origin 0); 1w = 1d rows on Monday 00:00 UTC (origin 4*86400). Windows bucket-aligned, empty buckets omitted, no forward fill. Stall backfill asks endTime = start of last skipped bucket; compEnd covers all its components, so backfilled bars are whole buckets.
- `pageEndParam` gives recent pages an unaligned per-second `end` (CDN cache MISS); only pages older than bucket + 300 s use the aligned, cacheable URL.
- getCandles step-back for empty windows returns real rows only; `[]` = history exhausted. Live read-backs use stepBack=false.
- `computeSeasonals` drops bars with time + 86400 > nowSec; `loadDailyHistory({nowSec})` drops forming rows while paging.
- `labOnChart(venue)` = venue?.id === 'binance'; gates markers, history planning, show-on-chart, candidate switching.
- Round 3: live close is trusted only when `closeLive` (a trade on the CURRENT subscription set it) and the feed had no step over MAX_FEED_GAP_S (3 s) since; resetSubscription on every socket/subscribe; second read-back whenever !(fromOpen && coveredEnd). Round-2 probes P1-P3 now give REST close 105 (they live as tests P1-P3 in coinbaseStream.test.ts). A lagging first read-back can show a stale close for ~5 s until the second read-back fixes it (accepted).
- Seasonals: t(0.975, n-1) table df 1-30 and A&S 26.7.5 Cornish-Fisher above (checked vs scipy, error < 5e-8). Tint only when |mean - pooled| > t*SE and n >= SMALL_N (10 months / 30 days). Pooled mean includes the cell itself: per-cell rate ~4.3% instead of 5% (conservative, fine). Caption "~40% months / ~20% weekdays" checked by my own MC (normal, t3, vol mixture): months 0.34-0.40, weekdays 0.20-0.25 (0.25 only at n=30, t3 + vol mix). With ~9 complete months per column (Binance BTC since 2017-08) no month can be tinted.
- candleStore applyLiveCandle identical-bar no-op is safe: liveCandles.trackRollover records the closed flag before the no-op.

Residual weak spots to re-check:
- RESOLVED round 4: Mean-row and weekday tint now follow `deviation(s, pooled, minN)` (sign/size vs pooled mean); weekday bar side = raw mean sign, colour = deviation (--above/--below) or grey --noise; scale >= |ref|+hw so no clamping and 'bar ends inside band' == !isNotable. `noneComparable` drives 'no month/weekday is compared' notices. Residual: Median rows still coloured by sign vs zero (Note only).
- REST-only read-back of an unseeded bucket: a missing row (`undefined`) is read as "quiet" with no second read-back, so a lagging row leaves the consumer's partial bar in place. Needs seed failing twice; Note only.
- Gap detection is by feed time only; dropped `match` messages with heartbeats still arriving would go unseen (heartbeat last_trade_id could detect it).

**Why:** these are the places where the web UI can show partial or other-venue data as final or comparable, or show noise as signal.
**How to apply:** on any later change to web/src/data/coinbase*, seasonals, or App lab-overlay wiring, check these first. Probe stream logic with a scratch vitest config (root in the scratchpad, harness copied from coinbaseStream.test.ts) rather than editing source; MC with lab/.venv python (has numpy/scipy), run with -I.
