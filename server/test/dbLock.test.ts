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

async function rejection(p: Promise<unknown>): Promise<DbLockedError> {
  const err = await p.then(
    () => {
      throw new Error('expected rejection')
    },
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(DbLockedError)
  return err as DbLockedError
}

describe('acquireDbLock', () => {
  it('writes our pid to <db>.lock and removes it on release', async () => {
    const lock = await acquireDbLock(dbPath, { pid: 4242 })
    expect(lock.path).toBe(lockPathFor(dbPath))
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('4242')
    lock.release()
    expect(existsSync(lock.path)).toBe(false)
    lock.release() // idempotent
  })

  it('refuses a second holder while the first pid is alive', async () => {
    const first = await acquireDbLock(dbPath, { pid: 1001, isPidAlive: () => true })
    const err = await rejection(acquireDbLock(dbPath, { pid: 1002, isPidAlive: () => true }))
    expect(err.pid).toBe(1001)
    expect(err.message).toContain('pid 1001')
    expect(err.message).toContain('TV_DB_PATH')
    expect(readFileSync(first.path, 'utf8').trim()).toBe('1001')
    first.release()
  })

  it('takes over a stale lock whose pid is dead', async () => {
    writeFileSync(lockPathFor(dbPath), '999999\n')
    const lock = await acquireDbLock(dbPath, { pid: 77, isPidAlive: (pid) => pid !== 999999 })
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('77')
    lock.release()
  })

  it('fails when another process replaced the lock right after we took it', async () => {
    // Two servers took over the same stale lock; the other one's file won.
    writeFileSync(lockPathFor(dbPath), '999999\n')
    const err = await rejection(
      acquireDbLock(dbPath, {
        pid: 77,
        isPidAlive: (pid) => pid !== 999999,
        tick: async () => {
          writeFileSync(lockPathFor(dbPath), '88\n')
        },
      }),
    )
    expect(err.pid).toBe(88)
    expect(err.message).toContain('at the same moment')
    // The winner's lock is left in place.
    expect(readFileSync(lockPathFor(dbPath), 'utf8').trim()).toBe('88')
  })

  it('re-checks ownership on the next tick by default', async () => {
    const p = acquireDbLock(dbPath, { pid: 31 })
    // Runs before the default setImmediate tick: replace the freshly created lock.
    queueMicrotask(() => writeFileSync(lockPathFor(dbPath), '32\n'))
    const err = await rejection(p)
    expect(err.pid).toBe(32)
  })

  it('treats a fresh unreadable lock as held and an old one as stale', async () => {
    const path = lockPathFor(dbPath)
    writeFileSync(path, '')
    const t = Date.now()
    await rejection(acquireDbLock(dbPath, { now: () => t }))
    const old = (t - UNREADABLE_LOCK_GRACE_MS - 1000) / 1000
    utimesSync(path, old, old)
    const lock = await acquireDbLock(dbPath, { pid: 5, now: () => t })
    expect(readFileSync(path, 'utf8').trim()).toBe('5')
    lock.release()
  })

  it('release leaves a lock that another process now owns', async () => {
    const lock = await acquireDbLock(dbPath, { pid: 10 })
    writeFileSync(lock.path, '11\n')
    lock.release()
    expect(readFileSync(lock.path, 'utf8').trim()).toBe('11')
  })

  it('needs no lock for :memory:', async () => {
    const a = await acquireDbLock(':memory:')
    const b = await acquireDbLock(':memory:')
    a.release()
    b.release()
  })
})
