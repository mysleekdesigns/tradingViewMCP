---
paths:
  - "web/src/data/**"
  - "server/src/**"
---

# Market data providers

- Binance klines are arrays of strings. Parse every price/volume with `Number()` and convert open time ms → seconds (`Math.floor(t / 1000)`). Validate the array length; reject malformed rows with a clear error.
- Only a kline with `k.x === true` is closed. Anything used for signals, alerts on close, or backtests uses closed candles only; the forming candle is display-only.
- `api.binance.com` blocks US IPs. The provider is chosen by config (Binance / Binance.US / Coinbase); never hard-code a host in more than one place.
- Respect rate limits: back off on HTTP 429 using `Retry-After`; treat 418 as an IP ban and stop calling. Cache before re-fetching. Paginate history with `endTime`, max 1000 candles per request.
- WebSockets: heartbeat/timeout detection, exponential backoff reconnect, then REST gap-fill of recent candles. Expose status as `live | reconnecting | offline`.
- Tests never hit the network by default. Live-API integration tests are marked slow/optional and skipped in the normal run.
- Endpoint and payload reference: the `binance-market-data` skill.
