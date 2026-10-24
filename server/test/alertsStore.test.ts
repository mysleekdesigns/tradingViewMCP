import Database from 'better-sqlite3'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createAlertStore, MAX_ALERT_EVENTS, type AlertStore } from '../src/alerts/store.ts'
import type { Alert } from '../src/alerts/types.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { MIGRATIONS, migrate } from '../src/db/migrations.ts'

const T0 = 1_700_000_000

function alert(id: string, over: Partial<Alert> = {}): Alert {
  return {
    id,
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    price: 65000,
    frequency: 'once',
    active: true,
    note: '',
    createdAt: T0,
    updatedAt: T0,
    lastTriggeredAt: null,
    ...over,
  }
}

function names(db: Db, type: 'table' | 'index'): string[] {
  return db
    .prepare<[string], { name: string }>('SELECT name FROM sqlite_master WHERE type = ?')
    .all(type)
    .map((r) => r.name)
}

describe('migration 2 (alerts)', () => {
  it('applies on top of an existing version-1 database and is idempotent', () => {
    const db = new Database(':memory:')
    try {
      const v1 = MIGRATIONS.find((m) => m.version === 1)!
      db.exec(`CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)`)
      db.exec(v1.sql)
      db.prepare('INSERT INTO schema_migrations VALUES (1, ?, ?)').run('init', T0)
      db.prepare('INSERT INTO drawings VALUES (?, ?, ?, ?)').run('d1', 'BTCUSDT', '{}', T0)
      expect(names(db, 'table')).not.toContain('alerts')

      const latest = Math.max(...MIGRATIONS.map((m) => m.version))
      expect(latest).toBeGreaterThanOrEqual(2)
      expect(migrate(db)).toBe(latest)
      expect(migrate(db)).toBe(latest)
      expect(names(db, 'table')).toEqual(expect.arrayContaining(['alerts', 'alert_events']))
      expect(names(db, 'index')).toContain('alerts_active')
      // Existing v1 data is untouched.
      expect(db.prepare('SELECT COUNT(*) AS n FROM drawings').get()).toEqual({ n: 1 })
      const versions = db
        .prepare<[], { version: number }>('SELECT version FROM schema_migrations ORDER BY version')
        .all()
        .map((r) => r.version)
      expect(versions).toEqual(MIGRATIONS.map((m) => m.version).sort((a, b) => a - b))
    } finally {
      db.close()
    }
  })

  it('creates the alert columns in order', () => {
    const db = openDb(':memory:')
    try {
      const cols = (t: string): string[] =>
        db
          .prepare<[string], { name: string }>('SELECT name FROM pragma_table_info(?)')
          .all(t)
          .map((r) => r.name)
      expect(cols('alerts')).toEqual([
        'id',
        'symbol',
        'condition',
        'price',
        'frequency',
        'active',
        'note',
        'created_at',
        'updated_at',
        'last_triggered_at',
      ])
      expect(cols('alert_events')).toEqual([
        'id',
        'alert_id',
        'symbol',
        'condition',
        'level',
        'price',
        'triggered_at',
        'message',
      ])
      // No foreign key from events to alerts.
      expect(db.prepare("SELECT * FROM pragma_foreign_key_list('alert_events')").all()).toEqual([])
    } finally {
      db.close()
    }
  })
})

describe('alert store', () => {
  let db: Db
  let store: AlertStore
  beforeEach(() => {
    db = openDb(':memory:')
    store = createAlertStore(db)
  })
  afterEach(() => db.close())

  it('round-trips an alert and lists newest first, filtered by symbol', () => {
    store.insert(alert('a', { createdAt: T0 }))
    store.insert(alert('b', { createdAt: T0 + 10, symbol: 'ETHUSDT', note: 'hi' }))
    store.insert(alert('c', { createdAt: T0 + 10 }))
    expect(store.get('b')).toEqual(
      alert('b', { createdAt: T0 + 10, symbol: 'ETHUSDT', note: 'hi' }),
    )
    // Same created_at: the later insert comes first.
    expect(store.list().map((a) => a.id)).toEqual(['c', 'b', 'a'])
    expect(store.list('BTCUSDT').map((a) => a.id)).toEqual(['c', 'a'])
    expect(store.count()).toBe(3)
    expect(store.get('nope')).toBeUndefined()
  })

  it('updates a subset of fields and the updated time', () => {
    store.insert(alert('a'))
    const u = store.update('a', { price: 70000, active: false }, T0 + 5)
    expect(u).toEqual(alert('a', { price: 70000, active: false, updatedAt: T0 + 5 }))
    expect(store.get('a')).toEqual(u)
    expect(store.listActive()).toEqual([])
    expect(store.update('missing', { price: 1 }, T0)).toBeUndefined()
  })

  it('deletes', () => {
    store.insert(alert('a'))
    expect(store.delete('a')).toBe(true)
    expect(store.delete('a')).toBe(false)
  })

  it('records a once trigger in one go: event, last_triggered_at, deactivation', () => {
    store.insert(alert('a'))
    const r = store.recordTrigger({
      alertId: 'a',
      price: 65012.5,
      triggeredAt: T0 + 60,
      message: 'm',
    })
    expect(r?.event).toEqual({
      id: 1,
      alertId: 'a',
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      level: 65000,
      price: 65012.5,
      triggeredAt: T0 + 60,
      message: 'm',
    })
    expect(r?.alert).toEqual(
      alert('a', { active: false, updatedAt: T0 + 60, lastTriggeredAt: T0 + 60 }),
    )
    // An inactive alert cannot trigger again.
    expect(
      store.recordTrigger({ alertId: 'a', price: 1, triggeredAt: T0, message: 'x' }),
    ).toBeUndefined()
    expect(
      store.recordTrigger({ alertId: 'gone', price: 1, triggeredAt: T0, message: 'x' }),
    ).toBeUndefined()
    expect(store.listEvents(10)).toHaveLength(1)
  })

  it('keeps an every alert active after a trigger', () => {
    store.insert(alert('a', { frequency: 'every' }))
    const r = store.recordTrigger({ alertId: 'a', price: 2, triggeredAt: T0 + 1, message: 'm' })
    expect(r?.alert.active).toBe(true)
    expect(r?.alert.lastTriggeredAt).toBe(T0 + 1)
  })

  it('lists events newest first with a limit, and clears them', () => {
    store.insert(alert('a', { frequency: 'every' }))
    for (let i = 0; i < 5; i++) {
      store.recordTrigger({ alertId: 'a', price: 100 + i, triggeredAt: T0 + i, message: `m${i}` })
    }
    expect(store.listEvents(3).map((e) => e.message)).toEqual(['m4', 'm3', 'm2'])
    store.clearEvents()
    expect(store.listEvents(10)).toEqual([])
  })

  it('history survives deleting the alert', () => {
    store.insert(alert('a'))
    store.recordTrigger({ alertId: 'a', price: 66000, triggeredAt: T0, message: 'm' })
    store.delete('a')
    const ev = store.listEvents(10)
    expect(ev).toHaveLength(1)
    expect(ev[0]?.alertId).toBe('a')
  })

  it(`prunes history to the newest ${MAX_ALERT_EVENTS} events`, () => {
    store.insert(alert('a', { frequency: 'every' }))
    const n = MAX_ALERT_EVENTS + 5
    for (let i = 0; i < n; i++) {
      store.recordTrigger({ alertId: 'a', price: 1 + i, triggeredAt: T0 + i, message: `m${i}` })
    }
    const all = store.listEvents(MAX_ALERT_EVENTS + 100)
    expect(all).toHaveLength(MAX_ALERT_EVENTS)
    expect(all[0]?.message).toBe(`m${n - 1}`)
    expect(all.at(-1)?.message).toBe(`m${n - MAX_ALERT_EVENTS}`)
    const count = db.prepare('SELECT COUNT(*) AS n FROM alert_events').get()
    expect(count).toEqual({ n: MAX_ALERT_EVENTS })
  })
})
