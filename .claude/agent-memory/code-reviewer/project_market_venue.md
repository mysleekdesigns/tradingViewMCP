---
name: market-venue
description: Market-data venue switched (2026-10-24) from Binance.US to Binance market-data-only hosts via TV_MARKET_VENUE; verified live facts and open gaps from that review
metadata:
  type: project
---

Default venue is `binance` (REST data-api.binance.vision, WS data-stream.binance.vision); `binance-us` still selectable on the server only via `TV_MARKET_VENUE`. Hosts live in server/src/venue.ts and web/src/data/venue.ts (one copy per package, deliberate).

Verified live 2026-10-24 (do not re-measure unless something changes):
- exchangeInfo?showPermissionSets=false: 6.8 MB raw but ~147 KB gzipped (fetch sends accept-encoding), 3.5 s uncompressed; REQUEST_WEIGHT 6000/min; used-weight 20; CORS `*` (direct mode fine). 3736 symbols, 1383 TRADING; all 40 `isSpotTradingAllowed:false` entries are non-TRADING. `permissionSets` still present as `[]`.
- WS raw `/ws/<stream>` answers `LIST_SUBSCRIPTIONS` (`{"result":[...],"id":n}`); kline pushes ~2 s; `!miniTicker@arr` ~65 entries/s (~11 KB/s), not a perf concern.
- ensureVenue: one transaction, prepared statements, only candles/candle_ranges/kv_cache; kv_cache holds only `symbols:v1` + `market_venue`.

Open gaps from that review (check before re-reporting):
- Web `VENUE_NAME`/`VENUE_NOTE` are hard-coded "Binance"/"same data"; server venue not exposed (/api/health has no venue), so `binance-us` mislabels the UI and the backtest note.
- ensureVenue runs before listen: a second instance (EADDRINUSE, or another port on the same app.db) clears/flips the marker under a running server, which keeps writing the old venue's bars.
- lab/lab/data/store.py SQLite top-up does not check `market_venue`.

**Why:** Saves re-verifying upstream limits/sizes and the cache-clear design next review.
**How to apply:** Venue/upstream reviews start here; related [[server-conventions]], [[web-no-trade-bars]].
