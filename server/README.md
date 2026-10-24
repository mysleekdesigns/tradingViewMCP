# server/ — local backend (Phase 5)

Fastify + `@fastify/websocket` + `better-sqlite3`, TypeScript, ESM. Single-user local app:
binds to `127.0.0.1` only. Market data is public (Binance.US); no API keys anywhere.

- Port: `TV_PORT` env, default `8787`. Host is always `127.0.0.1`.
- Database: `TV_DB_PATH` env, default `server/data/app.db` (gitignored). Tests use `:memory:`.
- Run: `npm run dev` (tsx watch). From the repo root, `npm run dev` starts server + web together.
- The Vite dev server proxies `/api` and `/ws` to `http://127.0.0.1:8787`, so the browser uses
  same-origin relative URLs.

## Contract (shared by server and `web/src/data/server.ts`)

All times are UNIX **seconds** UTC. All prices/volumes are numbers. `Candle`, `SymbolInfo`,
`Ticker` have exactly the shapes of `web/src/types.ts` (the server keeps its own copy in
`server/src/types.ts`).

Intervals accepted: `1m 5m 15m 1h 4h 1d 1w` (fixed lengths 60, 300, 900, 3600, 14400, 86400,
604800 s).

### Errors

Every non-2xx JSON response is `{ "error": ErrorKind, "message": string, "retryAfter"?: number }`
where `ErrorKind` is one of the web `MarketDataErrorKind` values:

| HTTP | error            | when                                                                         |
| ---- | ---------------- | ---------------------------------------------------------------------------- |
| 400  | `bad-symbol`     | unknown/invalid symbol (upstream `-1121`)                                    |
| 400  | `http`           | other bad query params (bad interval, limit, endTime)                        |
| 429  | `rate-limited`   | upstream 429 or local limiter exhausted; `Retry-After` header + `retryAfter` |
| 503  | `banned`         | upstream 418; `Retry-After` header + `retryAfter`                            |
| 503  | `region-blocked` | upstream 451                                                                 |
| 502  | `network`        | upstream unreachable / timed out                                             |
| 502  | `parse`          | upstream returned malformed data                                             |
| 404  | `http`           | persistence item not found                                                   |

### REST

- `GET /api/health` → `200 { "ok": true, "db": true, "upstreamSockets": number }`
- `GET /api/candles?symbol=BTCUSDT&interval=1h&limit=500[&endTime=<sec>]` → `Candle[]`
  - Same semantics as `MarketDataProvider.getCandles`: ascending, unique times, at most `limit`
    (1..1000, default 500) bars whose open time is `<= endTime` (default: now). The last row may
    still be forming. Exchange gaps are skipped (older bars are backfilled to fill the page), so
    a short or empty page means history really starts there.
  - Served from SQLite; only missing ranges are fetched upstream. Only **closed** bars are
    stored: `time + intervalSec + 5 <= now` (5 s settle grace) **and** upstream has already
    returned a later bar, which guards against local clock skew. The forming bar is always
    fetched fresh and never cached. Ranges already fetched (including "no data before here")
    are recorded in `candle_ranges`, so gaps and pre-listing ranges are never refetched.
  - If only the fresh tail fails upstream (`rate-limited`, `banned`, `network`), the cached closed
    bars are returned without the forming bar instead of an error.
- `GET /api/symbols` → `SymbolInfo[]` (all, unfiltered). Cached in SQLite (`kv_cache`), TTL 1 h;
  a failed refresh serves the stale copy if one exists.
- `GET /api/tickers?symbols=BTCUSDT,ETHUSDT` → `Ticker[]` (unknown symbols omitted; empty list →
  `[]` without an upstream call). Not cached.
- `GET /api/watchlists/:id` → `{ "id": string, "items": string[], "updatedAt": number }` or 404.
  `PUT /api/watchlists/:id` body `{ "items": string[] }` (≤ 200 symbols, each
  `^[A-Z0-9][A-Z0-9._-]{0,39}$`) → the stored object.
- `GET /api/layouts/:id` → `{ "id": string, "data": object, "updatedAt": number }` or 404.
  `PUT /api/layouts/:id` body `{ "data": object }` (JSON ≤ 256 KB) → stored object.
- `GET /api/drawings?symbol=BTCUSDT` → `{ "id", "symbol", "data", "updatedAt" }[]`.
  `PUT /api/drawings/:id` body `{ "symbol": string, "data": object }` → stored object.
  `DELETE /api/drawings/:id` → `204` (404 if absent).
- ids: `^[A-Za-z0-9_-]{1,64}$`, otherwise 400. The app uses id `default` for its single
  watchlist and layout.

### WebSocket fan-out

One browser socket per logical stream; the server holds **one upstream socket per
symbol/interval** (and one shared `!miniTicker@arr` socket) no matter how many browser sockets
are attached, and closes it when the last one leaves.

- `GET /ws/klines?symbol=BTCUSDT&interval=1h` (upgrade). Server → client messages:
  - `{ "type": "kline", "candle": Candle, "closed": boolean }`
  - `{ "type": "status", "status": "live" | "reconnecting" }` — upstream state. Sent once on
    attach with the current state, then on every change. After `reconnecting` → `live` the
    client should refetch recent candles (gap fill), as with the direct provider.
  - Invalid symbol/interval: the server closes with code `1008` and a reason.
- `GET /ws/tickers` (upgrade). Server → client:
  - `{ "type": "tickers", "tickers": Ticker[] }` (only symbols that changed in that upstream push)
  - `{ "type": "status", ... }` as above.
- Every 20 s the server re-sends the current `{type:'status'}` as an app-level heartbeat
  (browsers cannot see protocol pings), plus a protocol ping. Clients treat no message for 60 s
  as stale.
- When the last client leaves, the upstream socket is kept for a ~5 s grace period so a quick
  reconnect reuses it (and its backoff state).
- Upgrades are accepted only with no `Origin` header or an Origin on `localhost` / `127.0.0.1`
  (else close `1008`). At most 50 distinct upstream streams; a new one beyond that closes `1013`.
- A client whose send buffer exceeds 1 MB is terminated (the browser sees close `1006`).
- Clients never send anything; any client message is ignored.

## Layout

```
server/
├── src/
│   ├── index.ts            # entry: open DB, build app, listen on 127.0.0.1
│   ├── app.ts              # buildApp({ db, upstream? }) → FastifyInstance (registers plugins)
│   ├── config.ts           # env parsing (port, db path)
│   ├── types.ts            # Candle, SymbolInfo, Ticker, Interval, INTERVAL_SECONDS
│   ├── db/                 # openDb(path) + numbered migrations
│   ├── upstream/           # Binance.US REST client (rate limit + retry), upstream sockets
│   └── routes/             # candles.ts, symbols.ts, stream.ts, persistence.ts
└── test/
```
