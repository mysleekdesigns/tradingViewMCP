// One server per database file. A second server on the same TV_DB_PATH would share the
// market-data cache while possibly using another venue (and could relabel or clear it under the
// first one), so it refuses to start instead.
import { closeSync, openSync, readFileSync, statSync, unlinkSync, writeSync } from 'node:fs'

export interface DbLock {
  /** The lock file: `<dbPath>.lock`, holding the owner's pid. */
  readonly path: string
  /** Removes the lock file if it is still ours. Idempotent; safe in an `exit` handler. */
  release(): void
}

export class DbLockedError extends Error {
  readonly pid: number | null
  constructor(message: string, pid: number | null) {
    super(message)
    this.name = 'DbLockedError'
    this.pid = pid
  }
}

/** A lock file without a readable pid younger than this may still be being written. */
export const UNREADABLE_LOCK_GRACE_MS = 10_000

export function lockPathFor(dbPath: string): string {
  return `${dbPath}.lock`
}

/** True when a process with this pid exists (EPERM: exists but owned by another user). */
export function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM'
  }
}

function errCode(err: unknown): string | undefined {
  return (err as NodeJS.ErrnoException | undefined)?.code
}

function readPid(path: string): number | null {
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch {
    return null
  }
  const pid = Number(text.trim())
  return Number.isInteger(pid) && pid > 0 ? pid : null
}

export interface AcquireOptions {
  pid?: number
  isPidAlive?: (pid: number) => boolean
  now?: () => number
  /** Waits before the ownership re-check. Default: the next event-loop turn (setImmediate). */
  tick?: () => Promise<void>
}

const nextTick = (): Promise<void> => new Promise((resolve) => setImmediate(resolve))

/**
 * Creates `<dbPath>.lock` exclusively (`wx`) with this process's pid. If it exists and names a
 * live process, rejects with DbLockedError saying what to do. A lock naming a dead process (a
 * crash or `kill -9`) is stale: it is removed and taken over. After creating it, the lock is
 * re-read once on the next tick: if another process replaced it meanwhile (two servers taking
 * over the same stale lock), this one rejects and leaves the other's lock alone. `:memory:`
 * needs no lock.
 */
export async function acquireDbLock(dbPath: string, opts: AcquireOptions = {}): Promise<DbLock> {
  const lock = createLock(dbPath, opts)
  if (lock.path === '') return lock
  await (opts.tick ?? nextTick)()
  const owner = readPid(lock.path)
  const ownPid = opts.pid ?? process.pid
  if (owner !== ownPid) {
    lock.release() // a no-op unless the file is ours
    throw new DbLockedError(
      `Another server${owner === null ? '' : ` (pid ${owner})`} took the lock ${lock.path} for ` +
        `${dbPath} at the same moment. Stop it first, or set TV_DB_PATH to a different database.`,
      owner,
    )
  }
  return lock
}

function createLock(dbPath: string, opts: AcquireOptions): DbLock {
  const ownPid = opts.pid ?? process.pid
  const alive = opts.isPidAlive ?? isPidAlive
  const now = opts.now ?? Date.now
  if (dbPath === ':memory:' || dbPath === '') return { path: '', release: () => {} }
  const path = lockPathFor(dbPath)

  for (let attempt = 0; attempt < 2; attempt++) {
    let fd: number
    try {
      fd = openSync(path, 'wx')
    } catch (err) {
      if (errCode(err) !== 'EEXIST') throw err
      const pid = readPid(path)
      if (pid === null) {
        let ageMs: number
        try {
          ageMs = now() - statSync(path).mtimeMs
        } catch {
          continue // vanished meanwhile: try again
        }
        if (ageMs < UNREADABLE_LOCK_GRACE_MS) {
          throw new DbLockedError(
            `Another server is starting on ${dbPath} (lock ${path}). Try again in a few seconds.`,
            null,
          )
        }
      } else if (alive(pid)) {
        throw new DbLockedError(
          `Another server (pid ${pid}) is already using ${dbPath}. Stop it first, or set ` +
            `TV_DB_PATH to a different database. If no server is running, delete ${path}.`,
          pid,
        )
      }
      // Stale (dead pid, or unreadable and old): remove and retry once, unless another process
      // replaced it since we read it.
      if (readPid(path) !== pid) continue
      try {
        unlinkSync(path)
      } catch (e) {
        if (errCode(e) !== 'ENOENT') throw e
      }
      continue
    }
    try {
      writeSync(fd, `${ownPid}\n`)
    } finally {
      closeSync(fd)
    }
    let released = false
    return {
      path,
      release() {
        if (released) return
        released = true
        // Only remove the file if it is still ours (never another server's fresh lock).
        if (readPid(path) !== ownPid) return
        try {
          unlinkSync(path)
        } catch {
          // Already gone.
        }
      },
    }
  }
  throw new DbLockedError(`Could not take the lock ${path} for ${dbPath}; try again.`, null)
}
