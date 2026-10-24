---
paths:
  - "server/**"
---

# Local backend (Fastify + SQLite)

- Fastify + `@fastify/websocket` + `better-sqlite3`, TypeScript. Bind to `127.0.0.1` only; this is a single-user local app (PLAN.md Non-goals).
- Schema changes go through numbered migrations; never edit an applied migration. The candle table key is `(symbol, interval, time)`.
- `/api/candles` serves from SQLite and fetches only the missing ranges upstream. One upstream socket per symbol/interval, fanned out to every browser client; close upstream when the last client leaves.
- Alerts are evaluated server-side on the live stream so they fire with the browser closed. `crosses_above`/`crosses_below` compare the previous and current value, not just the current one.
- Use prepared statements; never build SQL with string concatenation.
- `*.db` files are runtime data. Never commit them or edit them by hand.
