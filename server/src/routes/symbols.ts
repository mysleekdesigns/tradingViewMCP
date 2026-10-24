import type { FastifyInstance } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { restClientFor, type RestClient } from '../upstream/rest.ts'
import { ApiError, type SymbolInfo, type Ticker } from '../types.ts'
import { normalizeSymbol } from './candles.ts'

export const SYMBOLS_KEY = 'symbols:v1'
export const SYMBOLS_TTL_SEC = 3600
/** Matches the watchlist cap. */
export const MAX_TICKER_SYMBOLS = 200
/** Most single-symbol ticker calls one /api/tickers request may make in the -1121 fallback. */
export const MAX_SINGLE_TICKER_CALLS = 20
/** How long a symbol upstream rejected with -1121 is skipped. */
export const BAD_SYMBOL_TTL_MS = 10 * 60_000

interface KvRow {
  value: string
  updated_at: number
}

// GET /api/symbols (cached exchangeInfo) and GET /api/tickers?symbols=A,B. See server/README.md.
export default async function symbolsRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const client: RestClient = restClientFor(opts)
  const selectKv = opts.db.prepare<[string], KvRow>(
    'SELECT value, updated_at FROM kv_cache WHERE key = ?',
  )
  const upsertKv = opts.db.prepare<[string, string, number]>(
    `INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
  )
  const nowSec = (): number => Math.floor(opts.now() / 1000)

  let refreshing: Promise<string> | null = null
  /** Symbol → epoch ms until which it is skipped (upstream said -1121 for it alone). */
  const badSymbols = new Map<string, number>()

  function isNegCached(sym: string): boolean {
    const until = badSymbols.get(sym)
    if (until === undefined) return false
    if (opts.now() < until) return true
    badSymbols.delete(sym)
    return false
  }

  /** Fetch exchangeInfo and store it; concurrent callers share one upstream call. */
  function refresh(): Promise<string> {
    if (refreshing === null) {
      refreshing = client
        .exchangeInfo()
        .then((info) => {
          const json = JSON.stringify(info.symbols)
          upsertKv.run(SYMBOLS_KEY, json, nowSec())
          return json
        })
        .finally(() => {
          refreshing = null
        })
    }
    return refreshing
  }

  /** The symbol list as JSON text: fresh cache, else refresh, else the stale copy, else throw. */
  async function symbolsJson(): Promise<string> {
    const row = selectKv.get(SYMBOLS_KEY)
    if (row !== undefined && nowSec() - row.updated_at < SYMBOLS_TTL_SEC) return row.value
    try {
      return await refresh()
    } catch (err) {
      if (row !== undefined) return row.value
      throw err
    }
  }

  function tradingSet(json: string): Set<string> {
    const list = JSON.parse(json) as SymbolInfo[]
    return new Set(list.filter((s) => s.status === 'TRADING').map((s) => s.symbol))
  }

  /** Symbols with status TRADING (refreshing the list if needed). */
  async function tradingSymbols(): Promise<Set<string>> {
    return tradingSet(await symbolsJson())
  }

  /** TRADING symbols from the stored list (any age) without an upstream call; null if none. */
  function cachedTradingSymbols(): Set<string> | null {
    const row = selectKv.get(SYMBOLS_KEY)
    return row === undefined ? null : tradingSet(row.value)
  }

  app.get('/api/symbols', async (_request, reply) => {
    const json = await symbolsJson()
    return reply.type('application/json; charset=utf-8').send(json)
  })

  app.get('/api/tickers', async (request): Promise<Ticker[]> => {
    const q = (request.query ?? {}) as Record<string, unknown>
    const raw = q.symbols
    if (raw !== undefined && typeof raw !== 'string') {
      throw new ApiError('http', 'symbols must be a comma-separated list', 400)
    }
    const wanted: string[] = []
    for (const part of (raw ?? '').split(',')) {
      if (part.trim() === '') continue
      const sym = normalizeSymbol(part)
      // Invalid syntax cannot be a listed symbol; it is omitted like any unknown symbol.
      if (sym !== null && !wanted.includes(sym)) wanted.push(sym)
    }
    if (wanted.length > MAX_TICKER_SYMBOLS) {
      throw new ApiError('http', `at most ${MAX_TICKER_SYMBOLS} symbols`, 400)
    }
    if (wanted.length === 0) return []

    // Skip symbols upstream recently rejected, and (when a symbol list is stored) anything not
    // TRADING, before the batch call: one unknown symbol fails the whole batch with -1121.
    const cached = cachedTradingSymbols()
    const ask = wanted.filter((s) => !isNegCached(s) && (cached === null || cached.has(s)))
    if (ask.length === 0) return []

    let tickers: Ticker[]
    try {
      tickers = await client.tickers24hr(ask)
    } catch (err) {
      // Upstream fails the WHOLE request with -1121 if any symbol is unknown. Drop unknowns using
      // the (refreshed) symbol list and retry once; if that still fails, ask one symbol at a time,
      // at most MAX_SINGLE_TICKER_CALLS, and remember the ones rejected individually.
      if (!(err instanceof ApiError) || err.kind !== 'bad-symbol') throw err
      const trading = await tradingSymbols()
      const listed = ask.filter((s) => trading.has(s))
      if (listed.length === 0) return []
      try {
        tickers = await client.tickers24hr(listed)
      } catch (err2) {
        if (!(err2 instanceof ApiError) || err2.kind !== 'bad-symbol') throw err2
        const each = await Promise.all(
          listed.slice(0, MAX_SINGLE_TICKER_CALLS).map((s) =>
            client.tickers24hr([s]).catch((e: unknown) => {
              if (e instanceof ApiError && e.kind === 'bad-symbol') {
                badSymbols.set(s, opts.now() + BAD_SYMBOL_TTL_MS)
                return []
              }
              throw e
            }),
          ),
        )
        tickers = each.flat()
      }
    }

    const bySymbol = new Map<string, Ticker>()
    for (const t of tickers) bySymbol.set(t.symbol.toUpperCase(), t)
    const out: Ticker[] = []
    for (const sym of wanted) {
      const t = bySymbol.get(sym)
      if (t !== undefined) out.push(t)
    }
    return out
  })
}
