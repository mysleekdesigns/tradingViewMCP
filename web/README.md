# web

Vite + React + TypeScript frontend for tradingView (Lightweight Charts v5). Market data is
Binance global spot from Binance's market-data-only hosts (`data-api.binance.vision`,
`data-stream.binance.vision`), through the local server by default; hosts and the venue name
live in `src/data/venue.ts`.

```sh
npm run dev        # http://localhost:5173
npx tsc -b         # typecheck
npm run lint       # ESLint (type-aware)
npx vitest run     # tests
npm run format     # Prettier
```

See `../PLAN.md` for phases and scope.
