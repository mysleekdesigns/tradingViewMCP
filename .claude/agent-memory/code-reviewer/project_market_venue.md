---
name: market-venue
description: Market-data venue (TV_MARKET_VENUE binance|binance-us), DB lock + startup order, live-verified upstream facts, and open gaps from the 2026-10-24 reviews
metadata:
  type: project
---

Default venue is `binance` (REST data-api.binance.vision, WS data-stream.binance.vision); `binance-us` selectable on the server only via `TV_MARKET_VENUE`. Hosts live in server/src/venue.ts and web/src/data/venue.ts (one copy per package, deliberate).

Verified live 2026-10-24 (do not re-measure unless something changes):
- exchangeInfo?showPermissionSets=false: 6.8 MB raw but ~147 KB gzipped, 3.5 s uncompressed; REQUEST_WEIGHT 6000/min; CORS `*` (direct mode fine). All `isSpotTradingAllowed:false` entries are non-TRADING.
- WS raw `/ws/<stream>` answers `LIST_SUBSCRIPTIONS`; kline pushes ~2 s; `!miniTicker@arr` ~11 KB/s, not a perf concern.

Confirmed design (second review, 2026-10-24; do not re-flag):
- startServer order: lock -> openDb -> buildApp -> listen -> ensureVenue (sync, no await after listen, so before any request) -> orphan warn via app.inject('/api/symbols'). Failure before/at listen closes app+db and releases the lock; cache untouched. Hub/alert engine never read or write candles/kv_cache, so engine.sync() during buildApp is safe.
- `<db>.lock` (wx + pid). `tsx watch` (4.23.x) SIGTERMs the child, awaits its `exit` (SIGKILL after 5 s), then spawns: restarts are safe (released by SIGTERM handler, or dead pid -> stale takeover). Node fires `exit` on uncaught exceptions, so the exit-handler release covers crashes; SIGHUP/SIGKILL leave a dead pid (taken over).
- Web: ERROR_SOURCE is stable so the venue arriving later never restarts the candle stream; getVenue retry effect aborts + clears its timer (StrictMode-safe). venueNote claims "same data" only for id `binance`.
- Earlier gaps fixed: web labels from /api/health venue; pre-listen cache clear; lab store.py venue check.

Open gaps (recheck before re-reporting):
- Lock stale takeover is check-then-unlink (two simultaneous starts on a stale lock can both win); pid reuse after reboot refuses with a "delete the lock" message.
- Web caches the venue for the page's life; a server restarted with another TV_MARKET_VENUE keeps the old label until reload.

**Why:** saves re-verifying upstream limits, startup order and tsx-watch lock behaviour.
**How to apply:** venue/upstream/startup reviews start here; related [[server-conventions]], [[web-no-trade-bars]], [[web-phase7-strategy-tester]].
