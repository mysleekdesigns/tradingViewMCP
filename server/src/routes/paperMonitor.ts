import { existsSync } from 'node:fs'
import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import Database from 'better-sqlite3'
import type { FastifyInstance } from 'fastify'
import type { RouteOptions } from '../app.ts'

// GET /api/paper/monitor: a read-only view of every paper test folder under the paper dir.
// The route takes no input, so nothing a client sends reaches a path. Each test is read on its
// own: one bad file sets that test's `error` and leaves every other field and test intact.

/**
 * Busy wait on a locked dry-run DB. better-sqlite3 is synchronous, so this blocks the event loop;
 * its 5 s default would stall the whole server on one bad folder during a panel poll.
 */
export const DB_BUSY_TIMEOUT_MS = 200

export interface PaperMonitorStake {
  last: number | null
  riskPerTradePct: number | null
  maxTotalExposurePct: number | null
  /** Sum of the open trades' `stake_amount`, percent of the wallet. Null without a wallet or DB. */
  openExposurePct: number | null
}

export interface PaperMonitorCircuitBreaker {
  maxDailyLossPct: number
  maxDrawdownPct: number
}

export interface PaperMonitorHalt {
  reason: string
  threshold: number
  value: number
  time: string
}

export interface PaperMonitorExpected {
  tradesLo: number
  tradesHi: number
  exposureLo: number
  exposureHi: number
  verdict: string | null
  asof: number | null
}

/** A running protection lock from freqtrade's `pairlocks` table. */
export interface PaperMonitorProtectionLock {
  /** ISO 8601 UTC. */
  until: string
  /** Freqtrade's text; '' when it stored none. */
  reason: string
}

/** Which sources were read. False means the fields taken from it are placeholders, not facts. */
export interface PaperMonitorRead {
  /** protocol.json parsed and holds a test: without one, no block in it can be called absent. */
  protocol: boolean
  /** dryrun.sqlite read, or absent (a test with no DB yet has zero trades). */
  trades: boolean
}

/** A block that is present but unusable: unknown, which is not the same as not configured. */
export interface PaperMonitorMalformed {
  protections: boolean
  circuitBreaker: boolean
}

export interface PaperMonitorTest {
  /** The folder name: unique, unlike `name`, which is `protocol.candidate`. */
  id: string
  name: string
  symbol: string
  interval: string
  /** UNIX seconds, from protocol.json. */
  start: number
  decisionEnd: number
  /** config.json `dry_run_wallet`. */
  wallet: number | null
  closedTrades: number
  openTrades: number
  /** Sum of `close_profit_abs` over closed trades. */
  netPnl: number
  netPnlPct: number | null
  /** From the peak of realised equity (wallet + cumulative closed P&L, by close date), >= 0. */
  drawdownPct: number
  /** Open positions are not marked to market. */
  pnlBasis: 'realized'
  stake: PaperMonitorStake
  /** Configured in the protocol, which says nothing of whether one is firing: `protectionLock`. */
  protections: boolean
  protectionLock: PaperMonitorProtectionLock | null
  circuitBreaker: PaperMonitorCircuitBreaker | null
  halt: PaperMonitorHalt | null
  expected: PaperMonitorExpected | null
  read: PaperMonitorRead
  malformed: PaperMonitorMalformed
  /** This test could not be read in full; the other tests still return. */
  error: string | null
}

export interface PaperMonitorResponse {
  /** UNIX seconds. */
  generatedAt: number
  tests: PaperMonitorTest[]
}

type Json = Record<string, unknown>

interface TradeRow {
  is_open: number
  close_profit_abs: number | null
  stake_amount: number
  last_stake: number | null
}

interface LockRow {
  reason: string | null
  lock_end_time: string
}

/** One statement, so one snapshot: a trade closing mid-read cannot be counted twice or not at all. */
const TRADES_SQL = `SELECT is_open, close_profit_abs, stake_amount,
    (SELECT stake_amount FROM trades ORDER BY open_date DESC, id DESC LIMIT 1) AS last_stake
  FROM trades ORDER BY close_date, id`

/**
 * Freqtrade calls `lock_pair(reason="Auto lock")` after every exit, so a pair cannot re-enter on
 * the same candle (`handle_protections`). That runs with no protection configured, so it says
 * nothing about one. Real protections word their own reason ("Cooldown period for 2 candles."),
 * so the exact text is the rule: anything else counts, a lock with no reason included.
 */
const POST_EXIT_LOCK_REASON = 'Auto lock'

const HAS_PAIRLOCKS_SQL = `SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pairlocks'`

/**
 * The same test as freqtrade's own: `active` alone is not enough, because it stays 1 once a lock
 * has run out. `IS NOT` keeps a NULL reason, which `<>` would drop.
 */
const ACTIVE_LOCK_SQL = `SELECT reason, lock_end_time FROM pairlocks
  WHERE active = 1 AND lock_end_time > ? AND reason IS NOT ?
  ORDER BY lock_end_time DESC LIMIT 1`

const COMPARISON_RE = /^comparison-\d{4}-\d{2}-\d{2}\.json$/

const UNREADABLE_HALT: PaperMonitorHalt = {
  reason: 'unreadable halt.lock',
  threshold: 0,
  value: 0,
  time: '',
}

function isObject(v: unknown): v is Json {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function asObject(v: unknown): Json {
  return isObject(v) ? v : {}
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function isMissing(err: unknown): boolean {
  return isObject(err) && err.code === 'ENOENT'
}

/** The file's text, or null if it does not exist. Any other read failure throws. */
async function readText(path: string): Promise<string | null> {
  try {
    return await readFile(path, 'utf8')
  } catch (err) {
    if (isMissing(err)) return null
    throw err
  }
}

/** The JSON object in `text`, or null if `text` is not one. */
function parseObject(text: string): Json | null {
  try {
    const value: unknown = JSON.parse(text)
    return isObject(value) ? value : null
  } catch {
    return null
  }
}

/**
 * Peak-to-trough fall of realised equity, in percent of the peak. `pnls` are closed-trade P&Ls in
 * close order; the peak starts at the wallet, so a first losing trade already counts.
 */
export function realizedDrawdownPct(wallet: number, pnls: readonly number[]): number {
  let equity = wallet
  let peak = wallet
  let worst = 0
  for (const pnl of pnls) {
    equity += pnl
    peak = Math.max(peak, equity)
    worst = Math.max(worst, ((peak - equity) / peak) * 100)
  }
  return worst
}

/** Runs `read` on `dbPath`, or returns `absent` when there is no DB yet. */
function withDb<T>(dbPath: string, absent: T, read: (db: Database.Database) => T): T {
  if (!existsSync(dbPath)) return absent
  // Read-only and never checkpoints; a live bot's rows in the `-wal` file are still seen.
  const db = new Database(dbPath, {
    readonly: true,
    fileMustExist: true,
    timeout: DB_BUSY_TIMEOUT_MS,
  })
  try {
    return read(db)
  } finally {
    db.close()
  }
}

/** Rows from freqtrade's `trades` table. A missing DB means zero trades. */
function readTrades(dbPath: string): TradeRow[] {
  return withDb<TradeRow[]>(dbPath, [], (db) => db.prepare<[], TradeRow>(TRADES_SQL).all())
}

/** Freqtrade's naive UTC text as ISO 8601; anything else stays as written. */
function freqtradeTimeToIso(text: string): string {
  const match = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})/.exec(text)
  return match === null ? text : `${match[1]}T${match[2]}Z`
}

/** The running protection lock that ends last, or null. Older DBs may have no such table. */
function readProtectionLock(dbPath: string, nowMs: number): PaperMonitorProtectionLock | null {
  // Freqtrade stores `YYYY-MM-DD HH:MM:SS.ffffff`, so the same shape compares as text.
  const now = `${new Date(nowMs).toISOString().slice(0, 23).replace('T', ' ')}000`
  return withDb<PaperMonitorProtectionLock | null>(dbPath, null, (db) => {
    if (db.prepare(HAS_PAIRLOCKS_SQL).get() === undefined) return null
    const row = db
      .prepare<[string, string], LockRow>(ACTIVE_LOCK_SQL)
      .get(now, POST_EXIT_LOCK_REASON)
    if (row === undefined) return null
    return { until: freqtradeTimeToIso(row.lock_end_time), reason: row.reason ?? '' }
  })
}

/** Fail safe: a lock file that exists always reads as halted, however broken its content. */
async function readHalt(folder: string): Promise<PaperMonitorHalt | null> {
  let text: string | null
  try {
    text = await readText(join(folder, 'halt.lock'))
  } catch {
    return UNREADABLE_HALT
  }
  if (text === null) return null
  const lock = parseObject(text)
  const threshold = num(lock?.threshold)
  const value = num(lock?.value)
  const reason = lock?.reason
  const time = lock?.time
  if (typeof reason !== 'string' || typeof time !== 'string') return UNREADABLE_HALT
  if (threshold === null || value === null) return UNREADABLE_HALT
  return { reason, threshold, value, time }
}

/** `dry_run_wallet`, or null when there is none to read (positive numbers only). */
async function readWallet(folder: string): Promise<number | null> {
  try {
    const text = await readText(join(folder, 'config.json'))
    const wallet = num(text === null ? null : parseObject(text)?.dry_run_wallet)
    return wallet !== null && wallet > 0 ? wallet : null
  } catch {
    return null
  }
}

/** `protocol_verdict.verdict` and the data end (`period_end`) of the newest comparison, if any. */
async function readComparison(
  folder: string,
): Promise<Pick<PaperMonitorExpected, 'verdict' | 'asof'>> {
  const names = (await readdir(folder)).filter((n) => COMPARISON_RE.test(n)).sort()
  const newest = names.at(-1)
  if (newest === undefined) return { verdict: null, asof: null }
  const text = await readText(join(folder, newest))
  const comparison = text === null ? null : parseObject(text)
  if (comparison === null) throw new Error(`${newest} is not valid JSON`)
  return {
    verdict: str(asObject(comparison.protocol_verdict).verdict),
    asof: num(comparison.period_end),
  }
}

const isAbsent = (v: unknown) => v === undefined || v === null

/** Freqtrade protections may be an object or a list; an empty one protects nothing. */
function parseProtections(riskControls: Json): { protections: boolean; malformed: boolean } {
  const p = riskControls.protections
  if (isAbsent(p)) return { protections: false, malformed: false }
  if (typeof p !== 'object' || p === null) return { protections: false, malformed: true }
  return { protections: Object.keys(p).length > 0, malformed: false }
}

/** `malformed` is not the same as "no breaker configured", so a plain null cannot say it. */
function parseCircuitBreaker(riskControls: Json): {
  circuitBreaker: PaperMonitorCircuitBreaker | null
  malformed: boolean
} {
  const block = riskControls.circuit_breaker
  if (isAbsent(block)) return { circuitBreaker: null, malformed: false }
  const breaker = asObject(block)
  const maxDailyLossPct = num(breaker.max_daily_loss_pct)
  const maxDrawdownPct = num(breaker.max_drawdown_pct)
  if (maxDailyLossPct === null || maxDrawdownPct === null) {
    return { circuitBreaker: null, malformed: true }
  }
  return { circuitBreaker: { maxDailyLossPct, maxDrawdownPct }, malformed: false }
}

/** The protocol's risk block. Its parts are absent, usable or malformed, never guessed. */
function parseRiskControls(protocol: Json, errors: string[]) {
  const block = protocol.risk_controls
  if (!isAbsent(block) && !isObject(block)) {
    errors.push('protocol.json risk_controls is malformed')
    return {
      protections: false,
      circuitBreaker: null,
      malformed: { protections: true, circuitBreaker: true },
    }
  }
  const riskControls = asObject(block)
  const { protections, malformed: badProtections } = parseProtections(riskControls)
  const { circuitBreaker, malformed: badBreaker } = parseCircuitBreaker(riskControls)
  if (badProtections) errors.push('protocol.json protections is malformed')
  if (badBreaker) errors.push('protocol.json circuit_breaker is malformed')
  return {
    protections,
    circuitBreaker,
    malformed: { protections: badProtections, circuitBreaker: badBreaker },
  }
}

/** The test in `folder`, or null if the folder has no protocol.json and so is not a paper test. */
async function readTest(
  paperDir: string,
  folderName: string,
  nowMs: number,
): Promise<PaperMonitorTest | null> {
  const folder = join(paperDir, folderName)
  const errors: string[] = []

  let protocol: Json = {}
  let protocolRead = false
  try {
    const text = await readText(join(folder, 'protocol.json'))
    if (text === null) return null
    const parsed = parseObject(text)
    if (parsed === null) errors.push('protocol.json is not valid JSON')
    else {
      protocol = parsed
      protocolRead = true
    }
  } catch {
    errors.push('protocol.json could not be read')
  }

  const [wallet, halt] = await Promise.all([readWallet(folder), readHalt(folder)])

  const name = str(protocol.candidate)
  const symbol = str(protocol.symbol)
  const interval = str(protocol.interval)
  const start = num(protocol.start)
  const decisionEnd = num(protocol.decision_end)
  if (errors.length === 0) {
    if (name === null || symbol === null || interval === null) {
      errors.push('protocol.json lacks candidate, symbol or interval')
      protocolRead = false
    }
    if (start === null || decisionEnd === null) {
      errors.push('protocol.json lacks start or decision_end')
      protocolRead = false
    }
  }

  const sizing = asObject(protocol.sizing)
  const { protections, circuitBreaker, malformed } = parseRiskControls(protocol, errors)

  const dbPath = join(folder, 'dryrun.sqlite')
  let rows: TradeRow[] = []
  let tradesRead = true
  try {
    rows = readTrades(dbPath)
  } catch (err) {
    tradesRead = false
    errors.push(`dryrun.sqlite: ${err instanceof Error ? err.message : 'could not be read'}`)
  }
  // Not retried after a failed read: a locked DB would spend the busy timeout a second time.
  let protectionLock: PaperMonitorProtectionLock | null = null
  if (tradesRead) {
    try {
      protectionLock = readProtectionLock(dbPath, nowMs)
    } catch (err) {
      errors.push(`pairlocks: ${err instanceof Error ? err.message : 'could not be read'}`)
    }
  }
  const pnls = rows.filter((r) => r.is_open === 0).map((r) => r.close_profit_abs ?? 0)
  const netPnl = pnls.reduce((sum, pnl) => sum + pnl, 0)
  const openStake = rows.filter((r) => r.is_open !== 0).reduce((s, r) => s + r.stake_amount, 0)
  if (wallet === null && pnls.length > 0) {
    errors.push('config.json has no dry_run_wallet: drawdown not computed')
  }

  const trades = asObject(protocol.trades)
  const exposure = asObject(protocol.exposure)
  const tradesLo = num(trades.lo)
  const tradesHi = num(trades.hi)
  const exposureLo = num(exposure.lo)
  const exposureHi = num(exposure.hi)
  let expected: PaperMonitorExpected | null = null
  if (tradesLo !== null && tradesHi !== null && exposureLo !== null && exposureHi !== null) {
    let comparison: Pick<PaperMonitorExpected, 'verdict' | 'asof'> = { verdict: null, asof: null }
    try {
      comparison = await readComparison(folder)
    } catch (err) {
      errors.push(err instanceof Error ? err.message : 'comparison could not be read')
    }
    expected = { tradesLo, tradesHi, exposureLo, exposureHi, ...comparison }
  }

  return {
    id: folderName,
    name: name ?? folderName,
    symbol: symbol ?? '',
    interval: interval ?? '',
    start: start ?? 0,
    decisionEnd: decisionEnd ?? 0,
    wallet,
    closedTrades: pnls.length,
    openTrades: rows.filter((r) => r.is_open !== 0).length,
    netPnl,
    netPnlPct: wallet === null ? null : (netPnl / wallet) * 100,
    drawdownPct: wallet === null ? 0 : realizedDrawdownPct(wallet, pnls),
    pnlBasis: 'realized',
    stake: {
      last: rows[0]?.last_stake ?? null,
      riskPerTradePct: num(sizing.risk_per_trade_pct),
      maxTotalExposurePct: num(sizing.max_total_exposure_pct),
      openExposurePct: wallet === null || !tradesRead ? null : (openStake / wallet) * 100,
    },
    protections,
    protectionLock,
    circuitBreaker,
    halt,
    expected,
    read: { protocol: protocolRead, trades: tradesRead },
    malformed,
    error: errors.length > 0 ? errors.join('; ') : null,
  }
}

/** Reads every paper test directly under `paperDir`. A missing dir means no tests. */
export async function readPaperMonitor(
  paperDir: string,
  nowMs: number,
): Promise<PaperMonitorResponse> {
  let folders: string[]
  try {
    const entries = await readdir(paperDir, { withFileTypes: true })
    // isDirectory() is false for a symlink, so nothing outside the paper dir is followed.
    folders = entries
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .sort()
  } catch (err) {
    if (!isMissing(err)) throw err
    folders = []
  }
  const tests = await Promise.all(folders.map((folder) => readTest(paperDir, folder, nowMs)))
  return {
    generatedAt: Math.floor(nowMs / 1000),
    tests: tests.filter((t) => t !== null),
  }
}

export default async function paperMonitorRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  app.get('/api/paper/monitor', async () => readPaperMonitor(opts.paperDir, opts.now()))
}
