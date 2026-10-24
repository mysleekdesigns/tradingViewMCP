// Market-data venue: host presets, TV_MARKET_VENUE config, the venue-change cache clear, and the
// Binance global specifics (spot-only symbols, slimmer exchangeInfo query). Offline only.
import { afterEach, describe, expect, it } from 'vitest'
import { buildApp, DEFAULT_REST_BASE, DEFAULT_WS_BASE } from '../src/app.ts'
import { loadConfig } from '../src/config.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { parseExchangeInfo } from '../src/upstream/parse.ts'
import { createRestClient } from '../src/upstream/rest.ts'
import {
  DEFAULT_VENUE_ID,
  ensureVenue,
  orphanedUserSymbols,
  VENUE_KEY,
  VENUES,
} from '../src/venue.ts'

const NOW = 1_792_800_000

const COUNT_SQL: Record<string, string> = {
  candles: 'SELECT COUNT(*) AS n FROM candles',
  candle_ranges: 'SELECT COUNT(*) AS n FROM candle_ranges',
  kv_cache: 'SELECT COUNT(*) AS n FROM kv_cache',
  watchlists: 'SELECT COUNT(*) AS n FROM watchlists',
  layouts: 'SELECT COUNT(*) AS n FROM layouts',
  drawings: 'SELECT COUNT(*) AS n FROM drawings',
  alerts: 'SELECT COUNT(*) AS n FROM alerts',
  alert_events: 'SELECT COUNT(*) AS n FROM alert_events',
}

function count(db: Db, table: string): number {
  const sql = COUNT_SQL[table]
  if (sql === undefined) throw new Error(`no count query for ${table}`)
  return db.prepare<[], { n: number }>(sql).get()?.n ?? -1
}

function kv(db: Db, key: string): string | undefined {
  return db
    .prepare<[string], { value: string }>('SELECT value FROM kv_cache WHERE key = ?')
    .get(key)?.value
}

/** A database holding cached market data plus one row of every kind of user data. */
function seeded(): Db {
  const db = openDb(':memory:')
  db.prepare(
    `INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)
     VALUES ('SUIUSDT', '1m', 60, 1, 1, 1, 1, 0), ('SUIUSDT', '1m', 120, 1, 2, 1, 2, 5)`,
  ).run()
  db.prepare(
    `INSERT INTO candle_ranges (symbol, interval, start, end) VALUES ('SUIUSDT', '1m', 60, 120)`,
  ).run()
  db.prepare(`INSERT INTO kv_cache (key, value, updated_at) VALUES ('symbols:v1', '[]', ?)`).run(
    NOW,
  )
  db.prepare(
    `INSERT INTO watchlists (id, items, updated_at) VALUES ('default', '["SUIUSDT"]', ?)`,
  ).run(NOW)
  db.prepare(`INSERT INTO layouts (id, data, updated_at) VALUES ('default', '{}', ?)`).run(NOW)
  db.prepare(
    `INSERT INTO drawings (id, symbol, data, updated_at) VALUES ('d1', 'SUIUSDT', '{}', ?)`,
  ).run(NOW)
  db.prepare(
    `INSERT INTO alerts (id, symbol, condition, price, frequency, active, note, created_at,
       updated_at) VALUES ('a1', 'SUIUSDT', 'greater', 2, 'once', 1, '', ?, ?)`,
  ).run(NOW, NOW)
  db.prepare(
    `INSERT INTO alert_events (alert_id, symbol, condition, level, price, triggered_at, message)
     VALUES ('a1', 'SUIUSDT', 'greater', 2, 2.1, ?, 'hit')`,
  ).run(NOW)
  return db
}

const USER_TABLES = ['watchlists', 'layouts', 'drawings', 'alerts', 'alert_events']

describe('venue presets', () => {
  it('defaults to the Binance market-data-only hosts', () => {
    expect(DEFAULT_VENUE_ID).toBe('binance')
    expect(VENUES.binance.restBase).toBe('https://data-api.binance.vision')
    expect(VENUES.binance.wsBase).toBe('wss://data-stream.binance.vision')
    expect(DEFAULT_REST_BASE).toBe('https://data-api.binance.vision')
    expect(DEFAULT_WS_BASE).toBe('wss://data-stream.binance.vision')
  })

  it('keeps Binance.US selectable', () => {
    expect(VENUES['binance-us'].restBase).toBe('https://api.binance.us')
    expect(VENUES['binance-us'].wsBase).toBe('wss://stream.binance.us:9443')
  })
})

describe('TV_MARKET_VENUE', () => {
  it('defaults to binance', () => {
    expect(loadConfig({}).venue).toEqual(VENUES.binance)
    expect(loadConfig({ TV_MARKET_VENUE: '' }).venue.id).toBe('binance')
  })

  it('selects binance-us case-insensitively', () => {
    expect(loadConfig({ TV_MARKET_VENUE: ' Binance-US ' }).venue).toEqual(VENUES['binance-us'])
  })

  it('rejects an unknown venue', () => {
    expect(() => loadConfig({ TV_MARKET_VENUE: 'coinbase' })).toThrow(/TV_MARKET_VENUE/)
    expect(() => loadConfig({ TV_MARKET_VENUE: 'toString' })).toThrow(/TV_MARKET_VENUE/)
  })
})

describe('ensureVenue', () => {
  let db: Db | undefined
  afterEach(() => db?.close())

  it('clears cached market data when no venue is recorded, keeping user data', () => {
    db = seeded()
    const res = ensureVenue(db, 'binance', NOW)
    expect(res).toEqual({
      previous: null,
      changed: true,
      removed: { candles: 2, ranges: 1, kv: 1 },
    })
    expect(count(db, 'candles')).toBe(0)
    expect(count(db, 'candle_ranges')).toBe(0)
    expect(kv(db, 'symbols:v1')).toBeUndefined()
    expect(kv(db, VENUE_KEY)).toBe('binance')
    for (const t of USER_TABLES) expect(count(db, t)).toBe(1)
  })

  it('clears cached market data when the recorded venue differs, keeping user data', () => {
    db = seeded()
    db.prepare('INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, ?)').run(
      VENUE_KEY,
      'binance-us',
      NOW - 10,
    )
    const res = ensureVenue(db, 'binance', NOW)
    expect(res.previous).toBe('binance-us')
    expect(res.changed).toBe(true)
    expect(res.removed).toEqual({ candles: 2, ranges: 1, kv: 1 })
    expect(count(db, 'candles')).toBe(0)
    expect(count(db, 'candle_ranges')).toBe(0)
    expect(count(db, 'kv_cache')).toBe(1)
    expect(kv(db, VENUE_KEY)).toBe('binance')
    for (const t of USER_TABLES) expect(count(db, t)).toBe(1)
  })

  it('keeps the cache when the recorded venue matches', () => {
    db = seeded()
    ensureVenue(db, 'binance', NOW)
    db.prepare(
      `INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)
       VALUES ('BTCUSDT', '1h', 3600, 1, 1, 1, 1, 1)`,
    ).run()
    db.prepare(`INSERT INTO kv_cache (key, value, updated_at) VALUES ('symbols:v1', '[]', ?)`).run(
      NOW,
    )
    const res = ensureVenue(db, 'binance', NOW + 60)
    expect(res).toEqual({
      previous: 'binance',
      changed: false,
      removed: { candles: 0, ranges: 0, kv: 0 },
    })
    expect(count(db, 'candles')).toBe(1)
    expect(kv(db, 'symbols:v1')).toBe('[]')
  })

  it('records the venue on a fresh database', () => {
    db = openDb(':memory:')
    expect(ensureVenue(db, 'binance-us', NOW)).toEqual({
      previous: null,
      changed: true,
      removed: { candles: 0, ranges: 0, kv: 0 },
    })
    expect(kv(db, VENUE_KEY)).toBe('binance-us')
  })

  it('switching back clears again', () => {
    db = seeded()
    ensureVenue(db, 'binance-us', NOW)
    db.prepare(
      `INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)
       VALUES ('SUIUSDT', '1m', 60, 1, 1, 1, 1, 0)`,
    ).run()
    const res = ensureVenue(db, 'binance', NOW)
    expect(res.previous).toBe('binance-us')
    expect(res.removed.candles).toBe(1)
    expect(kv(db, VENUE_KEY)).toBe('binance')
  })
})

describe('Binance global exchangeInfo', () => {
  const spot = {
    symbol: 'SUIUSDT',
    status: 'TRADING',
    baseAsset: 'SUI',
    quoteAsset: 'USDT',
    isSpotTradingAllowed: true,
    filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.00010000' }],
  }

  it('skips symbols that are not spot pairs (leveraged tokens)', () => {
    const info = parseExchangeInfo({
      symbols: [
        spot,
        { ...spot, symbol: 'BTCUPUSDT', baseAsset: 'BTCUP', isSpotTradingAllowed: false },
      ],
    })
    expect(info.symbols.map((s) => s.symbol)).toEqual(['SUIUSDT'])
  })

  it('keeps symbols without the isSpotTradingAllowed field (Binance.US-shaped)', () => {
    const { isSpotTradingAllowed: _omit, ...usShaped } = spot
    void _omit
    expect(parseExchangeInfo({ symbols: [usShaped] }).symbols).toHaveLength(1)
  })

  let db: Db | undefined
  afterEach(() => db?.close())

  it('requests exchangeInfo without permission sets', async () => {
    const urls: URL[] = []
    const client = createRestClient({
      fetch: (async (input: string | URL | Request) => {
        urls.push(new URL(String(input)))
        return new Response(JSON.stringify({ symbols: [spot] }), { status: 200 })
      }) as typeof fetch,
      now: () => NOW * 1000,
      restBase: VENUES.binance.restBase,
    })
    await client.exchangeInfo()
    expect(urls).toHaveLength(1)
    expect(urls[0]?.origin).toBe('https://data-api.binance.vision')
    expect(urls[0]?.pathname).toBe('/api/v3/exchangeInfo')
    expect(urls[0]?.searchParams.get('showPermissionSets')).toBe('false')
  })

  it('buildApp without restBase calls the vision host', async () => {
    db = openDb(':memory:')
    const origins: string[] = []
    const app = await buildApp({
      db,
      fetch: (async (input: string | URL | Request) => {
        origins.push(new URL(String(input)).origin)
        return new Response(JSON.stringify({ symbols: [spot] }), { status: 200 })
      }) as typeof fetch,
      WebSocketImpl: class {} as unknown as typeof WebSocket,
      now: () => NOW * 1000,
    })
    try {
      const res = await app.inject({
        method: 'GET',
        url: '/api/symbols',
        headers: { host: 'localhost' },
      })
      expect(res.statusCode).toBe(200)
      expect(origins).toEqual(['https://data-api.binance.vision'])
    } finally {
      await app.close()
    }
  })
})

describe('orphanedUserSymbols', () => {
  let db: Db | undefined
  afterEach(() => db?.close())

  it('lists active-alert and watchlist symbols the venue does not list, skipping bad rows', () => {
    db = seeded() // watchlist ["SUIUSDT"], active alert on SUIUSDT
    db.prepare(
      `INSERT INTO watchlists (id, items, updated_at) VALUES ('broken', 'not json', 0)`,
    ).run()
    db.prepare(`INSERT INTO watchlists (id, items, updated_at) VALUES ('obj', '{"a":1}', 0)`).run()
    expect(orphanedUserSymbols(db, new Set(['BTCUSDT']))).toEqual({
      alerts: ['SUIUSDT'],
      watchlist: ['SUIUSDT'],
    })
    expect(orphanedUserSymbols(db, new Set(['SUIUSDT']))).toEqual({ alerts: [], watchlist: [] })
  })
})
