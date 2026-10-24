import { afterEach, describe, expect, it } from 'vitest'
import { openDb, type Db } from '../src/db/index.ts'
import { MIGRATIONS, migrate } from '../src/db/migrations.ts'

const TABLES = ['candles', 'candle_ranges', 'kv_cache', 'watchlists', 'layouts', 'drawings']

function tableNames(db: Db): string[] {
  return db
    .prepare<[], { name: string }>("SELECT name FROM sqlite_master WHERE type = 'table'")
    .all()
    .map((r) => r.name)
}

function columns(db: Db, table: string): string[] {
  return db
    .prepare<[string], { name: string }>('SELECT name FROM pragma_table_info(?)')
    .all(table)
    .map((r) => r.name)
}

describe('migrations', () => {
  let db: Db | undefined
  afterEach(() => db?.close())

  it('creates every table on :memory:', () => {
    db = openDb(':memory:')
    const names = tableNames(db)
    for (const t of [...TABLES, 'schema_migrations']) expect(names).toContain(t)
    expect(columns(db, 'candles')).toEqual([
      'symbol',
      'interval',
      'time',
      'open',
      'high',
      'low',
      'close',
      'volume',
    ])
    expect(columns(db, 'candle_ranges')).toEqual(['symbol', 'interval', 'start', 'end'])
    expect(columns(db, 'drawings')).toEqual(['id', 'symbol', 'data', 'updated_at'])
    const idx = db
      .prepare<[], { name: string }>("SELECT name FROM pragma_index_list('drawings')")
      .all()
      .map((r) => r.name)
    expect(idx).toContain('drawings_symbol')
  })

  it('keys candles by (symbol, interval, time)', () => {
    db = openDb(':memory:')
    const ins = db.prepare(
      'INSERT INTO candles (symbol, interval, time, open, high, low, close, volume) VALUES (?, ?, ?, 1, 2, 0.5, 1.5, 10)',
    )
    ins.run('BTCUSDT', '1h', 3600)
    ins.run('BTCUSDT', '4h', 3600)
    expect(() => ins.run('BTCUSDT', '1h', 3600)).toThrow(/UNIQUE|PRIMARY KEY/)
  })

  it('records the version and is idempotent', () => {
    db = openDb(':memory:')
    const latest = Math.max(...MIGRATIONS.map((m) => m.version))
    expect(migrate(db)).toBe(latest)
    expect(migrate(db)).toBe(latest)
    const rows = db
      .prepare<[], { version: number; name: string; applied_at: number }>(
        'SELECT version, name, applied_at FROM schema_migrations ORDER BY version',
      )
      .all()
    expect(rows.map((r) => r.version)).toEqual(MIGRATIONS.map((m) => m.version))
    expect(rows[0]?.name).toBe('init')
    // applied_at is UNIX seconds, not ms.
    expect(rows[0]?.applied_at).toBeLessThan(1e11)
    expect(rows[0]?.applied_at).toBeGreaterThan(1.7e9)
  })

  it('migrates a fresh database from version 0', () => {
    db = openDb(':memory:')
    db.close()
    const Database = db.constructor as new (p: string) => Db
    db = new Database(':memory:')
    expect(tableNames(db)).toEqual([])
    expect(migrate(db)).toBe(1)
    for (const t of TABLES) expect(tableNames(db)).toContain(t)
  })

  it('enables foreign keys', () => {
    db = openDb(':memory:')
    expect(db.pragma('foreign_keys', { simple: true })).toBe(1)
  })
})
