---
name: server-conventions
description: Phase 5 server (Fastify/SQLite) confirmed design choices not to re-flag, and open gaps found in the first review (2026-10-24)
metadata:
  type: project
---

Confirmed design (deliberate; do not re-flag):
- Candle cache stores closed bars only, with a 2 s settle (SETTLE_SEC), and records fetched ranges in `candle_ranges` so genuine exchange gaps are not refetched. Forming bar is fetched fresh via REST on every default /api/candles call.
- 1w bars align to Monday: WEEK_OFFSET_SEC = 345600 in server/src/time.ts.
- One RestClient (one limiter) per app via `restClientFor` WeakMap keyed by db; 80 % of the exchangeInfo weight limit (6000 on Binance.US); MAX_IN_FLIGHT 4 with slot hand-off; 429 → pausedUntil, 418 → bannedUntil; retries only on network/5xx (MAX_RETRIES 2).
- Hub: one upstream WS per `kline:SYM:iv` / `tickers` key, torn down on last release, 60 s stale timer, backoff 1 s..30 s equal jitter. Host fixed to 127.0.0.1 in index.ts (not configurable).

Open gaps from the 2026-10-24 review (check whether fixed before re-reporting):
- /api/candles is a time window [E-(limit-1)I, E], not Binance's "last `limit` bars <= endTime"; a gap of >= limit bars returns [] and the web marks history exhausted.
- Default request fails outright on 429/418 even when the cache could serve closed bars.
- Browser cannot see WS ping frames; no app-level heartbeat, so quiet/reconnecting upstream makes clients reconnect, which tears down the hub entry and resets backoff.
- Weight header can come from the previous minute window; ticker per-symbol fallback can fan out to 200 calls; no Origin check or cap on distinct upstream keys.

**Why:** Saves re-deriving the cache/limiter design next review.
**How to apply:** Start Phase 5/6 server reviews by checking these gaps; Phase 6 alerts should consume only `closed: true` hub klines and compare prev vs current. Related: [[web-phase5-sync]].
