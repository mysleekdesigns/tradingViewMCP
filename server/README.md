# server/ — local backend (Phase 5)

Fastify + `@fastify/websocket` + `better-sqlite3`, TypeScript, ESM. Single-user local app:
binds to `127.0.0.1` only. Market data is public (Binance global spot via Binance's
market-data-only hosts); no API keys anywhere.

- Port: `TV_PORT` env, default `8787`. Host is always `127.0.0.1`.
- Database: `TV_DB_PATH` env, default `server/data/app.db` (gitignored). Tests use `:memory:`.
- Market data venue: `TV_MARKET_VENUE` env, `binance` (default) or `binance-us`. See "Market
  data venue" below.
- Alerts: `TV_DESKTOP_NOTIFY` (`auto` | `always` | `off`, default `auto`) and optional
  `TV_ALERT_WEBHOOK_URL` (secret; environment only). See "Alerts" below.
- Run: `npm run dev` (tsx watch). From the repo root, `npm run dev` starts server + web together.
- The Vite dev server proxies `/api` and `/ws` to `http://127.0.0.1:8787`, so the browser uses
  same-origin relative URLs.
- **Host check (DNS-rebinding guard).** Every request, REST and WebSocket upgrade alike, must
  carry a `Host` header whose hostname is `localhost`, `127.0.0.1` or `[::1]` (any port; the
  Vite proxy forwards `Host: localhost:5173` unchanged because `changeOrigin` is off). Anything
  else, including a missing `Host`, gets `403 {"error":"http","message":"host not allowed"}`
  before routing, so a page whose domain re-resolves to 127.0.0.1 cannot read or change data.
  Use `http://localhost:…` or `http://127.0.0.1:…`, never a custom hostname.

## Market data venue

All upstream hosts are named once, in `src/venue.ts`:

| `TV_MARKET_VENUE`   | REST                              | WebSocket                          |
| ------------------- | --------------------------------- | ---------------------------------- |
| `binance` (default) | `https://data-api.binance.vision` | `wss://data-stream.binance.vision` |
| `binance-us`        | `https://api.binance.us`          | `wss://stream.binance.us:9443`     |

`binance` uses Binance's documented
[market-data-only domains](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only):
the same `/api/v3` payloads and streams as Binance.com (global spot), reachable where
`api.binance.com` answers 451. It is the same venue as the lab's `data.binance.vision` archive.
Binance.US was too thin to chart (most 1m bars on mid-cap pairs had no trades).

- **Rate limits** (read live from `exchangeInfo.rateLimits` on 2026-10-24): `REQUEST_WEIGHT`
  6000 per minute; the limiter keeps 20% headroom, follows `X-MBX-USED-WEIGHT-1M`, honours
  `Retry-After` on 429 and stops calling on 418. Measured weights: klines 2, exchangeInfo 20,
  ticker/24hr (≤ 20 symbols) 2. `exchangeInfo` is requested with `showPermissionSets=false`
  (17.8 MB → 6.8 MB). Symbols with `isSpotTradingAllowed: false` (leveraged tokens) are
  dropped from `/api/symbols`.
- **Streams** follow Binance.com rules: connections close at 24 h (the hub reconnects with
  backoff), the server pings and Node answers pongs, one stream per upstream socket.
  `LIST_SUBSCRIPTIONS` is answered on both hosts.
- **Cache never mixes venues.** `kv_cache` key `market_venue` records the venue the cached
  market data came from. Once the server is listening (a start that fails, e.g. port in use,
  never touches the cache), if the marker is absent or differs from `TV_MARKET_VENUE`, one
  transaction deletes every row of `candles`, `candle_ranges` and `kv_cache` (symbol list),
  then records the venue; the result is logged as `market data venue`. Watchlists, layouts,
  drawings, alerts and alert history are kept. Same venue → the cache is kept. After a change
  the server loads the new symbol list and logs once (warn) the active-alert and watchlist
  symbols the new venue does not list (names only; nothing is deleted).
- **One server per database.** Startup creates `<TV_DB_PATH>.lock` (exclusive create, holding
  the pid) and removes it on shutdown. A second server on the same database refuses to start
  with a message naming the running pid; a lock left by a dead process is taken over.

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

- `GET /api/health` →
  `200 { "ok": true, "db": true, "upstreamSockets": number, "venue": { "id": "binance" | "binance-us", "name": string } }`
  (the web app labels the chart and the Strategy Tester data note from `venue`)
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
- `GET /api/symbols` → `SymbolInfo[]` (all statuses; non-spot leveraged tokens dropped).
  Cached in SQLite (`kv_cache`), TTL 1 h; a failed refresh serves the stale copy if one exists.
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
- Upstream `live` means the exchange socket is open (the stream is subscribed by URL), not
  that a push has arrived: a kline stream can go quiet (Binance global pushes every ~2 s, but a
  halted pair is silent; Binance.US pushes only on trades, minutes apart). After 25 s of
  upstream silence the server sends `{"method":"LIST_SUBSCRIPTIONS","id":n}` (repeated while
  silent); its reply counts as
  traffic, so a quiet but healthy socket is kept and only one that answers nothing for 60 s is
  reconnected. Backoff resets only once a message arrives.
- When the last client leaves, the upstream socket is kept for a ~5 s grace period so a quick
  reconnect reuses it (and its backoff state).
- Upgrades are accepted only with no `Origin` header or an Origin on `localhost` / `127.0.0.1`
  (else close `1008`). At most 50 distinct upstream streams; a new one beyond that closes `1013`.
- A client whose send buffer exceeds 1 MB is terminated (the browser sees close `1006`).
- Clients never send anything; any client message is ignored.

### Alerts (Phase 6)

Notification only: alerts never place, cancel, or modify orders, and use no exchange keys.
Shapes are exactly `web/src/data/alertTypes.ts` (`Alert`, `NewAlert`, `AlertPatch`, `AlertEvent`,
`AlertStreamMessage`); the server copy is `server/src/alerts/types.ts`. Times are UNIX seconds.
Errors use the table above (`error: "http"`): 400 validation, 404 unknown alert, 409 limit.

- `GET /api/alerts[?symbol=SYM]` → `Alert[]`, newest first (`createdAt` desc). Bad symbol → 400.
- `POST /api/alerts` body `NewAlert` → `201 Alert`. `id` is a server UUID; `active: true`;
  `lastTriggeredAt: null`. Validation: `symbol` trimmed + uppercased, `^[A-Z0-9]{2,30}$`;
  `condition` ∈ `crosses_above | crosses_below | greater | less`; `price` a finite number > 0;
  `frequency` ∈ `once | every`; `note` optional string, trimmed, ≤ 200 chars (default `''`).
  Unknown body fields → 400. More than 500 alerts in total → 409.
- `PATCH /api/alerts/:id` body `AlertPatch` (any non-empty subset of `condition`, `price`,
  `frequency`, `active`, `note`; unknown keys or `{}` → 400) → `Alert`; 404 if absent.
  `active: true` re-arms the alert; changing `price` or `condition` also resets its state
  (an explicit reset starts armed, so a true `greater`/`less` fires at once, cooldown allowing).
- `DELETE /api/alerts/:id` → `204`, or 404.
- `GET /api/alerts/events?limit=100` (1..1000, default 100) → `AlertEvent[]`, newest first.
  `DELETE /api/alerts/events` → `204` (clears history). History has no foreign key to alerts,
  so it survives deleting an alert, and is pruned to the newest 1000 events on insert.
- `GET /ws/alerts` (upgrade; same Origin rule as the other sockets, else close `1008`).
  Server → client `AlertStreamMessage`:
  - `{ "type": "heartbeat" }` on attach and every 20 s (plus a protocol ping).
  - `{ "type": "triggered", "event": AlertEvent, "alert": Alert }` per trigger (`alert` is the
    state after the trigger, e.g. `active: false` for a fired `once` alert), followed by one
    `{ "type": "changed" }` for that batch of triggers.
  - `{ "type": "changed" }` after every successful REST mutation (any tab): refetch.
  - Clients never send; messages are ignored. A client whose send buffer exceeds 1 MB is
    terminated.

**Evaluation.** Alerts fire on the live last-traded price (`Ticker.last` from the shared
`!miniTicker@arr` upstream socket), not on bar close. The engine holds that stream only while at
least one active alert exists. Per alert it keeps `{ prev, armed }` from its own first tick
(an alert never compares against a price seen before it was created or re-armed):

| condition       | fires when                                                          |
| --------------- | ------------------------------------------------------------------- |
| `crosses_above` | `prev < level && cur >= level` (never on the first tick)            |
| `crosses_below` | `prev > level && cur <= level` (never on the first tick)            |
| `greater`       | `cur > level` while armed (first tick can fire); re-arms when false |
| `less`          | `cur < level` while armed (first tick can fire); re-arms when false |

State lives in memory. After a server restart (including every `tsx watch` reload) an alert
that has fired before (`lastTriggeredAt !== null`) and was not explicitly reset starts
**disarmed**: a `greater`/`less` whose condition is still true does not fire again until it has
been seen false once. Alerts that never fired, and explicit resets (above), start armed.

`once` deactivates after firing. `every` stays active with a 60 s cooldown per alert (seeded
from `lastTriggeredAt`, so restarts and edits respect it): a cross inside the cooldown is
dropped; a `greater`/`less` that is true inside it fires when the cooldown ends. Non-finite or
≤ 0 prices are ignored. A trigger is one SQLite transaction (event row, `last_triggered_at`,
`active = 0` for `once`). Message: `BTCUSDT crossing up 65,000 (last 65,012.5)`.

**Notifications** (after the DB commit, fire-and-forget; one failing never blocks another or
the WebSocket push). One upstream ticker batch can fire several alerts: the WebSocket still
gets one `triggered` per alert, but desktop and webhook get **one notification per batch**:

- Browser: the web app shows a Notification on `triggered` while it is open.
- Desktop (macOS): `osascript -e 'display notification …'` (no shell, 5 s timeout).
  `TV_DESKTOP_NOTIFY=auto|always|off` (default `auto` = only while no `/ws/alerts` client is
  attached, so an open browser does not double-notify). One trigger: title `Alert: SYM`, body
  the message (plus `Note: …`). Several: title `Alerts: N triggered`, body the first 5
  messages then `+M more`.
- Webhook (optional): `TV_ALERT_WEBHOOK_URL` (https, or http to localhost). At most one POST
  per batch, JSON `{ text, content, event, alert, allowed_mentions: { parse: [] } }` (`text`
  for Slack, `content` for Discord; `allowed_mentions` stops a note such as `@everyone` pinging
  a Discord channel and is always forced by the notifier). With several triggers, `event` /
  `alert` are the first one, `events: AlertEvent[]` lists all of them, and `text`/`content`
  join each trigger's text with newlines (kept under 1900 chars with a final `+M more`). 5 s
  timeout, at most one retry on a network error or 5xx. The URL is a secret: read from the environment only,
  never logged, stored, or returned.

## Layout

```
server/
├── src/
│   ├── index.ts            # entry: config, notifiers, startServer, signal handling
│   ├── app.ts              # buildApp(deps) → FastifyInstance; owns the shared upstream hub
│   ├── config.ts           # env parsing (port, db path, venue, desktop notify mode, webhook URL)
│   ├── startup.ts          # lock → open DB → listen → venue check → orphan report
│   ├── dbLock.ts           # <db>.lock: one server per database file
│   ├── venue.ts            # venue hosts, cache check (market_venue), orphaned symbols
│   ├── types.ts            # Candle, SymbolInfo, Ticker, Interval, INTERVAL_SECONDS
│   ├── db/                 # openDb(path) + numbered migrations (2 = alerts)
│   ├── upstream/           # Binance REST client (rate limit + retry), upstream sockets
│   ├── alerts/             # types, evaluate (pure), store (SQLite), engine, notify
│   └── routes/             # candles, symbols, stream, persistence, alerts
└── test/
```
