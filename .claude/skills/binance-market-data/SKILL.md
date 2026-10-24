---
name: binance-market-data
description: Reference for the public market-data APIs this app uses - Binance / Binance.US REST klines, exchangeInfo, 24h tickers, kline and miniTicker WebSocket payloads, rate limits, the data.binance.vision archive, and Coinbase Exchange differences. Use when implementing or reviewing a MarketDataProvider, the server cache/fan-out, or the lab's archive downloader.
user-invocable: false
---

# Market data reference

Public market data needs **no API key**. Never add signed endpoints. Rate-limit weights and limits change; read them from `exchangeInfo.rateLimits` and the `X-MBX-USED-WEIGHT-1M` response header rather than hard-coding them.

## Hosts (pick by region, configure in one place)

| Provider | REST | WebSocket |
|---|---|---|
| Binance | `https://api.binance.com` | `wss://stream.binance.com:9443` (market-data-only alt: `wss://data-stream.binance.vision`) |
| Binance.US | `https://api.binance.us` | `wss://stream.binance.us:9443` |
| Coinbase Exchange | `https://api.exchange.coinbase.com` | `wss://ws-feed.exchange.coinbase.com` |

`api.binance.com` blocks US IPs (HTTP 451 / geo error). Show a clear "provider blocked in your region" error, not a generic failure.

## Binance REST

`GET /api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000[&startTime=ms][&endTime=ms]`. Max 1000 per call. Each row:

| idx | field | idx | field |
|---|---|---|---|
| 0 | open time (ms) | 6 | close time (ms) |
| 1–4 | open, high, low, close (**strings**) | 7 | quote volume |
| 5 | base volume (string) | 8 | trade count |
| | | 9, 10 | taker buy base / quote |

```ts
const toCandle = (k: unknown[]): Candle => ({
  time: Math.floor(Number(k[0]) / 1000), open: Number(k[1]), high: Number(k[2]),
  low: Number(k[3]), close: Number(k[4]), volume: Number(k[5]),
});
```
The **last row is usually the still-forming candle** (close time > now). Older history: `endTime = oldest.time * 1000 - 1`.

- `GET /api/v3/exchangeInfo`: `symbols[]` with `status`, `baseAsset`, `quoteAsset`, `filters` (tick size for price formatting). Heavy; cache it.
- `GET /api/v3/ticker/24hr`: all symbols without `symbol` param is heavy; prefer the WS stream for the watchlist.
- Errors: **429** → wait `Retry-After` seconds, back off. **418** → IP banned for escalating periods; stop calling and surface it. Body `{ code, msg }` (e.g. `-1121` invalid symbol).

## Binance WebSocket

- Single stream: `/ws/btcusdt@kline_1m` (symbol **lowercase**). Combined: `/stream?streams=btcusdt@kline_1m/ethusdt@kline_1m`, messages wrapped as `{ stream, data }`.
- Kline message: `{ e: 'kline', E, s, k: { t, T, s, i, o, c, h, l, v, n, x, q, ... } }`. `k.t` is open time ms; `k.x === true` means closed.
- All-market mini tickers: `!miniTicker@arr` → array of `{ s, c (close), o (open 24h), h, l, v, q }`. % change = `(c - o) / o`.
- Server pings every few minutes; browsers answer pongs automatically, Node `ws` does too. Connections drop at 24h, so always reconnect.
- Limits: a cap on incoming messages per second per connection and on streams per connection; batch subscriptions on one combined connection (this is what the server fan-out is for).
- Intervals: `1s 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M` (`1s` is Binance-only).

## Archive (lab downloader, Phase 7)

`https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip` (+ `.CHECKSUM` with SHA-256; verify it). Daily files under `.../daily/klines/...` for the current month. CSV, no header, same columns as REST. **Timestamp units changed for newer spot files (microseconds instead of milliseconds)**: detect by magnitude (> 1e14 → µs) and normalise, and test both.

## Coinbase Exchange differences

- Product ids are `BTC-USD`, not `BTCUSDT`. Map symbols in the provider.
- `GET /products/{id}/candles?granularity=<s>&start=<iso>&end=<iso>`: granularity only `60, 300, 900, 3600, 21600, 86400`; max 300 rows; rows are `[time(s), low, high, open, close, volume]` (**low/high come before open**); returned **newest first**. Sort ascending.
- No 4h/1w granularity: aggregate from 1h / 1d.
- The Exchange WS feed has no kline channel: subscribe to `ticker` or `matches` and build the forming candle yourself.
