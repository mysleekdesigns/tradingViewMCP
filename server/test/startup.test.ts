// startServer ordering: lock → listen → venue check → orphan report. Real temp DB files and
// loopback ports; upstream fetch and WebSocket are fakes (offline).
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer, type Server } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Config } from '../src/config.ts'
import { openDb } from '../src/db/index.ts'
import { DbLockedError, lockPathFor } from '../src/dbLock.ts'
import { startServer, type Started } from '../src/startup.ts'
import { VENUE_KEY, VENUES, type VenueId } from '../src/venue.ts'

class FakeUpstream {
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  send(): void {}
  close(): void {}
}

const symbol = (s: string) => ({
  symbol: s,
  status: 'TRADING',
  baseAsset: s.replace(/USDT$/, ''),
  quoteAsset: 'USDT',
  filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.01' }],
})

/** Upstream that lists only BTCUSDT and SUIUSDT. */
const fakeFetch = (async () =>
  new Response(JSON.stringify({ symbols: [symbol('BTCUSDT'), symbol('SUIUSDT')] }), {
    status: 200,
  })) as typeof fetch

let dir: string
let dbPath: string
const running: Started[] = []

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tv-start-'))
  dbPath = join(dir, 'app.db')
})
afterEach(async () => {
  for (const s of running.splice(0)) await s.close()
  rmSync(dir, { recursive: true, force: true })
})

function config(venue: VenueId, port = 0): Config {
  return { port, dbPath, desktopNotify: 'off', venue: VENUES[venue] }
}

function start(venue: VenueId, extra: { port?: number; fetch?: typeof fetch } = {}) {
  return startServer({
    config: config(venue, extra.port),
    deps: {
      fetch: extra.fetch ?? fakeFetch,
      WebSocketImpl: FakeUpstream as unknown as typeof WebSocket,
    },
    nowSec: () => 1_792_800_000,
  })
}

/** A DB marked `binance-us` with cached bars and user data naming listed and unlisted symbols. */
function seedUsDb(): void {
  const db = openDb(dbPath)
  const kv = db.prepare('INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, 0)')
  kv.run(VENUE_KEY, 'binance-us')
  kv.run('symbols:v1', '[]')
  db.prepare(
    `INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)
     VALUES ('SUIUSDT', '1m', 60, 1, 1, 1, 1, 0)`,
  ).run()
  db.prepare(`INSERT INTO watchlists (id, items, updated_at) VALUES ('default', ?, 0)`).run(
    JSON.stringify(['BTCUSDT', 'OLDUSDT', 'GHOSTUSDT']),
  )
  const alert = db.prepare(
    `INSERT INTO alerts (id, symbol, condition, price, frequency, active, note, created_at,
       updated_at) VALUES (?, ?, 'greater', 1, 'once', ?, '', 0, 0)`,
  )
  alert.run('a1', 'GHOSTUSDT', 1)
  alert.run('a2', 'SUIUSDT', 1)
  alert.run('a3', 'GONEUSDT', 0) // inactive: not reported
  db.close()
}

function readState(): { venue: string | undefined; candles: number } {
  const db = openDb(dbPath)
  try {
    const venue = db
      .prepare<[string], { value: string }>('SELECT value FROM kv_cache WHERE key = ?')
      .get(VENUE_KEY)?.value
    const candles = db.prepare<[], { n: number }>('SELECT COUNT(*) AS n FROM candles').get()?.n
    return { venue, candles: candles ?? -1 }
  } finally {
    db.close()
  }
}

function occupyPort(): Promise<{ server: Server; port: number }> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address()
      if (addr === null || typeof addr === 'string') return reject(new Error('no port'))
      resolve({ server, port: addr.port })
    })
  })
}

describe('startServer', () => {
  it('a failed listen leaves the venue marker and cache untouched and frees the lock', async () => {
    seedUsDb()
    const { server, port } = await occupyPort()
    try {
      await expect(start('binance', { port })).rejects.toMatchObject({ code: 'EADDRINUSE' })
    } finally {
      server.close()
    }
    expect(readState()).toEqual({ venue: 'binance-us', candles: 1 })
    expect(existsSync(lockPathFor(dbPath))).toBe(false)
  })

  it('refuses a second server on the same DB without touching its cache', async () => {
    const first = await start('binance')
    running.push(first)
    first.db
      .prepare(
        `INSERT INTO candles (symbol, interval, time, open, high, low, close, volume)
         VALUES ('BTCUSDT', '1h', 3600, 1, 1, 1, 1, 1)`,
      )
      .run()
    await expect(start('binance-us')).rejects.toBeInstanceOf(DbLockedError)
    expect(readFileSync(lockPathFor(dbPath), 'utf8').trim()).toBe(String(process.pid))
    const marker = first.db
      .prepare<[string], { value: string }>('SELECT value FROM kv_cache WHERE key = ?')
      .get(VENUE_KEY)
    expect(marker?.value).toBe('binance')
    expect(first.db.prepare('SELECT COUNT(*) AS n FROM candles').get()).toEqual({ n: 1 })
  })

  it('recovers a stale lock left by a dead process', async () => {
    writeFileSync(lockPathFor(dbPath), '999999\n')
    const s = await startServer({
      config: config('binance'),
      deps: { fetch: fakeFetch, WebSocketImpl: FakeUpstream as unknown as typeof WebSocket },
      isPidAlive: () => false,
    })
    running.push(s)
    expect(readFileSync(lockPathFor(dbPath), 'utf8').trim()).toBe(String(process.pid))
  })

  it('runs the venue check after listening and removes the lock on close', async () => {
    seedUsDb()
    const s = await start('binance')
    expect(s.app.server.listening).toBe(true)
    expect(s.venueCheck).toMatchObject({ previous: 'binance-us', changed: true })
    const health = await s.app.inject({ url: '/api/health' })
    expect(health.json()).toMatchObject({ venue: { id: 'binance', name: 'Binance' } })
    await s.orphanCheck
    await s.close()
    expect(existsSync(lockPathFor(dbPath))).toBe(false)
    expect(readState()).toEqual({ venue: 'binance', candles: 0 })
  })

  it('after a venue change, warns once with active-alert and watchlist symbols not listed', async () => {
    seedUsDb()
    const s = await start('binance')
    running.push(s)
    const warn = vi.spyOn(s.app.log, 'warn')
    const report = await s.orphanCheck
    expect(report).toEqual({ alerts: ['GHOSTUSDT'], watchlist: ['GHOSTUSDT', 'OLDUSDT'] })
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0]?.[0]).toEqual({
      venue: 'Binance',
      alerts: ['GHOSTUSDT'],
      watchlist: ['GHOSTUSDT', 'OLDUSDT'],
    })
  })

  it('skips the orphan check when the venue is unchanged', async () => {
    const first = await start('binance')
    await first.close()
    const calls: string[] = []
    const s = await start('binance', {
      fetch: (async (u: string | URL | Request) => {
        calls.push(String(u))
        return fakeFetch(u)
      }) as typeof fetch,
    })
    running.push(s)
    expect(s.venueCheck.changed).toBe(false)
    expect(await s.orphanCheck).toBeNull()
    expect(calls).toEqual([])
  })
})
