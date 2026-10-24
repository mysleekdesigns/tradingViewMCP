import Database from 'better-sqlite3'
import { migrate } from './migrations.ts'

export type Db = Database.Database

/** Opens (or creates) the database, enables WAL for files and foreign keys, and migrates it. */
export function openDb(path: string): Db {
  const db = new Database(path)
  if (path !== ':memory:' && path !== '') db.pragma('journal_mode = WAL')
  db.pragma('foreign_keys = ON')
  migrate(db)
  return db
}
