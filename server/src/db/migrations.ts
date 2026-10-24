import type { Db } from './index.ts'

export interface Migration {
  version: number
  name: string
  sql: string
}

// Append-only. Never edit a migration once it has been applied; add a new one instead.
export const MIGRATIONS: readonly Migration[] = [
  {
    version: 1,
    name: 'init',
    sql: `
      -- Closed bars only. time = bar open time, UNIX seconds.
      CREATE TABLE candles (
        symbol   TEXT    NOT NULL,
        interval TEXT    NOT NULL,
        time     INTEGER NOT NULL,
        open     REAL    NOT NULL,
        high     REAL    NOT NULL,
        low      REAL    NOT NULL,
        close    REAL    NOT NULL,
        volume   REAL    NOT NULL,
        PRIMARY KEY (symbol, interval, time)
      ) WITHOUT ROWID;

      -- Inclusive open-time ranges [start, end] (UNIX seconds) of closed bars already fetched
      -- upstream, so genuine exchange gaps inside a range are not refetched forever.
      CREATE TABLE candle_ranges (
        symbol   TEXT    NOT NULL,
        interval TEXT    NOT NULL,
        start    INTEGER NOT NULL,
        end      INTEGER NOT NULL,
        PRIMARY KEY (symbol, interval, start)
      ) WITHOUT ROWID;

      -- Generic JSON cache (e.g. symbol list). updated_at = UNIX seconds.
      CREATE TABLE kv_cache (
        key        TEXT    PRIMARY KEY,
        value      TEXT    NOT NULL,
        updated_at INTEGER NOT NULL
      );

      -- items = JSON array of symbols. updated_at = UNIX seconds.
      CREATE TABLE watchlists (
        id         TEXT    PRIMARY KEY,
        items      TEXT    NOT NULL,
        updated_at INTEGER NOT NULL
      );

      -- data = JSON object. updated_at = UNIX seconds.
      CREATE TABLE layouts (
        id         TEXT    PRIMARY KEY,
        data       TEXT    NOT NULL,
        updated_at INTEGER NOT NULL
      );

      -- data = JSON object. updated_at = UNIX seconds.
      CREATE TABLE drawings (
        id         TEXT    PRIMARY KEY,
        symbol     TEXT    NOT NULL,
        data       TEXT    NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE INDEX drawings_symbol ON drawings (symbol);
    `,
  },
  {
    version: 2,
    name: 'alerts',
    sql: `
      -- Price alerts (notification only). Times are UNIX seconds; active is 0/1.
      CREATE TABLE alerts (
        id                TEXT    PRIMARY KEY,
        symbol            TEXT    NOT NULL,
        condition         TEXT    NOT NULL,
        price             REAL    NOT NULL,
        frequency         TEXT    NOT NULL,
        active            INTEGER NOT NULL,
        note              TEXT    NOT NULL DEFAULT '',
        created_at        INTEGER,
        updated_at        INTEGER,
        last_triggered_at INTEGER NULL
      );
      CREATE INDEX alerts_active ON alerts (active);

      -- Trigger history. No foreign key to alerts, so history survives deleting an alert.
      CREATE TABLE alert_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id     TEXT    NOT NULL,
        symbol       TEXT,
        condition    TEXT,
        level        REAL,
        price        REAL,
        triggered_at INTEGER,
        message      TEXT
      );
    `,
  },
]

/**
 * Applies every pending migration, each in its own transaction, and records it in
 * `schema_migrations`. Idempotent. Returns the current schema version.
 */
export function migrate(db: Db): number {
  db.exec(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      version    INTEGER PRIMARY KEY,
      name       TEXT    NOT NULL,
      applied_at INTEGER NOT NULL
    )
  `)
  const currentStmt = db.prepare<[], { v: number | null }>(
    'SELECT MAX(version) AS v FROM schema_migrations',
  )
  const record = db.prepare<[number, string, number]>(
    'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
  )
  let current = currentStmt.get()?.v ?? 0
  const pending = [...MIGRATIONS].sort((a, b) => a.version - b.version)
  for (const m of pending) {
    if (m.version <= current) continue
    db.transaction(() => {
      db.exec(m.sql)
      record.run(m.version, m.name, Math.floor(Date.now() / 1000))
    })()
    current = m.version
  }
  return current
}
