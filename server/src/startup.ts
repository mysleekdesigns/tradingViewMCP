// Server startup, separate from index.ts so the order of steps is testable:
// lock the DB file → open DB → build app → listen → venue check (cache clear) → orphan report.
import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import type { FastifyInstance } from 'fastify'
import { buildApp, type AppDeps } from './app.ts'
import type { Config } from './config.ts'
import { acquireDbLock, type DbLock } from './dbLock.ts'
import { openDb, type Db } from './db/index.ts'
import type { SymbolInfo } from './types.ts'
import { ensureVenue, orphanedUserSymbols, type OrphanReport, type VenueCheck } from './venue.ts'

// Single-user local app: always bind to loopback. Not configurable on purpose.
export const HOST = '127.0.0.1'

export interface StartOptions {
  config: Config
  logger?: boolean
  /** Extra app deps (notifiers; fakes in tests). Hosts and venue always come from `config`. */
  deps?: Omit<AppDeps, 'db' | 'restBase' | 'wsBase' | 'venue'>
  /** Lock liveness probe; tests inject one. */
  isPidAlive?: (pid: number) => boolean
  /** UNIX seconds clock for the venue marker. */
  nowSec?: () => number
}

export interface Started {
  app: FastifyInstance
  db: Db
  lock: DbLock
  venueCheck: VenueCheck
  /**
   * Resolves once the orphan check is done: null when the venue did not change (no check) or
   * the symbol list could not be loaded, else the report (also logged at warn level when it
   * names anything). Never rejects.
   */
  orphanCheck: Promise<OrphanReport | null>
  /** Close the app and DB and release the lock. */
  close(): Promise<void>
}

/** Loads the symbol list through the app (so it is cached) and reports orphaned user symbols. */
async function checkOrphans(app: FastifyInstance, db: Db, venueName: string) {
  try {
    const res = await app.inject({ method: 'GET', url: '/api/symbols' })
    if (res.statusCode !== 200) {
      app.log.warn(
        { status: res.statusCode },
        'could not load the symbol list to check alerts and watchlist symbols',
      )
      return null
    }
    const list = res.json<SymbolInfo[]>()
    const report = orphanedUserSymbols(db, new Set(list.map((s) => s.symbol)))
    if (report.alerts.length > 0 || report.watchlist.length > 0) {
      app.log.warn(
        { venue: venueName, alerts: report.alerts, watchlist: report.watchlist },
        `symbols in active alerts or the watchlist are not listed on ${venueName}`,
      )
    }
    return report
  } catch (err) {
    app.log.warn({ err }, 'orphaned-symbol check failed')
    return null
  }
}

/**
 * Starts the server. The venue check (which may clear the market-data cache) runs only after
 * `listen` succeeded, so a start that fails (port in use, DB locked) never touches the cache. It
 * runs synchronously right after `listen` resolves, before any request is handled.
 */
export async function startServer(opts: StartOptions): Promise<Started> {
  const { config } = opts
  const nowSec = opts.nowSec ?? (() => Math.floor(Date.now() / 1000))
  if (config.dbPath !== ':memory:') mkdirSync(dirname(config.dbPath), { recursive: true })

  const lock = await acquireDbLock(
    config.dbPath,
    opts.isPidAlive ? { isPidAlive: opts.isPidAlive } : {},
  )
  let db: Db | undefined
  let app: FastifyInstance | undefined
  try {
    db = openDb(config.dbPath)
    app = await buildApp(
      {
        ...opts.deps,
        db,
        restBase: config.venue.restBase,
        wsBase: config.venue.wsBase,
        venue: { id: config.venue.id, name: config.venue.name },
      },
      { logger: opts.logger ?? false },
    )
    await app.listen({ host: HOST, port: config.port })
  } catch (err) {
    try {
      await app?.close()
    } finally {
      db?.close()
      lock.release()
    }
    throw err
  }

  // No await between listen and here: requests are only handled on a later event-loop turn.
  const venueCheck = ensureVenue(db, config.venue.id, nowSec())
  app.log.info(
    {
      venue: config.venue.id,
      restBase: config.venue.restBase,
      wsBase: config.venue.wsBase,
      previousVenue: venueCheck.previous,
      cacheCleared: venueCheck.changed ? venueCheck.removed : false,
    },
    'market data venue',
  )
  const orphanCheck = venueCheck.changed
    ? checkOrphans(app, db, config.venue.name)
    : Promise.resolve(null)

  const theApp = app
  const theDb = db
  let closed: Promise<void> | null = null
  return {
    app: theApp,
    db: theDb,
    lock,
    venueCheck,
    orphanCheck,
    close() {
      closed ??= (async () => {
        try {
          // A pending orphan check fails harmlessly once the app is closed.
          await theApp.close()
        } finally {
          theDb.close()
          lock.release()
        }
      })()
      return closed
    },
  }
}
