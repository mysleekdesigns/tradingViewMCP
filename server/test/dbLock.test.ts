import { existsSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  acquireDbLock,
  DbLockedError,
  lockPathFor,
  UNREADABLE_LOCK_GRACE_MS,
} from '../src/dbLock.ts'

let dir: string
let dbPath: string
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tv-lock-'))
  dbPath = join(dir, 'app.db')
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

describe('acquireDbLock', () => {
  it('writes our pid to <db>.lock and removes it on release', () => {
    const lock = acquireDbLock(dbPath, { pid: 4242 })
    expect(lock.path).toBe(lockPathFor(dbPath))
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('4242')
    lock.release()
    expect(existsSync(lock.path)).toBe(false)
    lock.release() // idempotent
  })

  it('refuses a second holder while the first pid is alive', () => {
    const first = acquireDbLock(dbPath, { pid: 1001, isPidAlive: () => true })
    let err: unknown
    try {
      acquireDbLock(dbPath, { pid: 1002, isPidAlive: () => true })
    } catch (e) {
      err = e
    }
    expect(err).toBeInstanceOf(DbLockedError)
    expect((err as DbLockedError).pid).toBe(1001)
    expect((err as Error).message).toContain('pid 1001')
    expect((err as Error).message).toContain('TV_DB_PATH')
    expect(readFileSync(first.path, 'utf8').trim()).toBe('1001')
    first.release()
  })

  it('takes over a stale lock whose pid is dead', () => {
    writeFileSync(lockPathFor(dbPath), '999999\n')
    const lock = acquireDbLock(dbPath, { pid: 77, isPidAlive: (pid) => pid !== 999999 })
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('77')
    lock.release()
  })

  it('treats a fresh unreadable lock as held and an old one as stale', () => {
    const path = lockPathFor(dbPath)
    writeFileSync(path, '')
    const t = Date.now()
    expect(() => acquireDbLock(dbPath, { now: () => t })).toThrow(DbLockedError)
    const old = (t - UNREADABLE_LOCK_GRACE_MS - 1000) / 1000
    utimesSync(path, old, old)
    const lock = acquireDbLock(dbPath, { pid: 5, now: () => t })
    expect(readFileSync(path, 'utf8').trim()).toBe('5')
    lock.release()
  })

  it('release leaves a lock that another process now owns', () => {
    const lock = acquireDbLock(dbPath, { pid: 10 })
    writeFileSync(lock.path, '11\n')
    lock.release()
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('11')
  })

  it('needs no lock for :memory:', () => {
    const a = acquireDbLock(':memory:')
    const b = acquireDbLock(':memory:')
    a.release()
    b.release()
  })
})
