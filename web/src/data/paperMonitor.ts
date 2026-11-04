/**
 * Client for the local server's paper monitor (`GET /api/paper/monitor`): one read-only row per
 * Freqtrade dry-run test. Nothing here starts, stops or changes a test: the only request is that
 * GET. No keys, no exchange calls, no trading endpoints.
 *
 * The parser is tolerant where the contract allows null and fails closed on what matters: a row
 * it cannot read stays in the list with an error (a dropped row could hide a halted test), and
 * any non-null `halt` reads as halted, however malformed.
 */

import { createLocalServerRequest, type LocalServerClientOptions } from './drawingsApi'
import { isRecord, numberOr, numberOrNull as num, stringOr as str } from './guards'
import { PersistenceError } from './persistence'

export const PAPER_MONITOR_PATH = '/api/paper/monitor'
/** The server reads every dry-run DB per request: more room than the persistence calls. */
export const PAPER_MONITOR_TIMEOUT_MS = 10_000

export const PAPER_MONITOR_OFFLINE_MESSAGE =
  'Paper test monitor unavailable: the local server is not running (`npm run dev` in server/).'
export const PAPER_MONITOR_NO_SERVER_MESSAGE =
  'Paper test monitor needs the local server. This data source reads the exchange directly.'
const UNREADABLE_ROW = 'The server sent an unreadable row for this test.'

/**
 * Where UNIX seconds stop. 1e11 s is the year 5138, while read as milliseconds it is 1973, so
 * anything from here up is a time in the wrong unit: it would print a garbled date, and past
 * 8.64e12 `toISOString` throws.
 */
const MAX_UNIX_SECONDS = 1e11

/** `v` as a time in UNIX seconds that is safe to format, else null (0 is the server's "unread"). */
export function unixSeconds(v: unknown): number | null {
  const n = num(v)
  return n !== null && n > 0 && n < MAX_UNIX_SECONDS ? n : null
}

export interface PaperMonitorStake {
  last: number | null
  riskPerTradePct: number | null
  maxTotalExposurePct: number | null
  /** Open trades' stake, percent of the wallet. Null without a wallet or a readable DB. */
  openExposurePct: number | null
}

export interface PaperMonitorBreaker {
  maxDailyLossPct: number
  maxDrawdownPct: number
}

export interface PaperMonitorHalt {
  reason: string
  /** Null when the lock could not be read (the server then sends no time): still halted. */
  threshold: number | null
  value: number | null
  time: string
}

/** The pre-registered backtest range over the whole test, up to `decisionEnd`. */
export interface PaperMonitorExpected {
  tradesLo: number
  tradesHi: number
  /** Fractions of bars in a position (0.25 = 25%), as the protocol stores them. */
  exposureLo: number
  exposureHi: number
  verdict: string | null
  asof: number | null
}

/** A running protection lock. A row that has one is locked, however malformed the fields. */
export interface PaperMonitorProtectionLock {
  /** ISO 8601 UTC; '' when unreadable. */
  until: string
  /** '' when freqtrade stored none. */
  reason: string
}

/** Which sources were read. False means the fields taken from it are not facts: show a dash. */
export interface PaperMonitorRead {
  protocol: boolean
  trades: boolean
}

/** A block that is present but unusable: unknown, which is not the same as not configured. */
export interface PaperMonitorMalformed {
  protections: boolean
  circuitBreaker: boolean
}

export interface PaperMonitorTest {
  /** The folder name, unique in the list: `name` is the protocol's candidate and may repeat. */
  id: string
  name: string
  symbol: string
  interval: string
  /** UNIX seconds; null when unread or out of range (`unixSeconds`). */
  start: number | null
  decisionEnd: number | null
  wallet: number | null
  closedTrades: number
  openTrades: number
  netPnl: number
  /** Percent of the wallet (12.5 = 12.5%), like every `Pct` field here. */
  netPnlPct: number | null
  /** From the peak of realized equity, >= 0. Meaningless (0) without a wallet. */
  drawdownPct: number
  /** Closed trades only: open positions are not marked to market. */
  pnlBasis: 'realized'
  stake: PaperMonitorStake
  /** Configured in the protocol. Whether one is firing is `protectionLock`. */
  protections: boolean
  protectionLock: PaperMonitorProtectionLock | null
  circuitBreaker: PaperMonitorBreaker | null
  halt: PaperMonitorHalt | null
  expected: PaperMonitorExpected | null
  read: PaperMonitorRead
  malformed: PaperMonitorMalformed
  /** Part of this test could not be read: `read` and `malformed` say which figures to trust. */
  error: string | null
}

export interface PaperMonitorResponse {
  generatedAt: number
  tests: PaperMonitorTest[]
}

export interface PaperMonitorClient {
  /** Rejects with a PersistenceError; `paperMonitorErrorMessage` words it for the panel. */
  fetchMonitor(signal?: AbortSignal): Promise<PaperMonitorResponse>
}

function unreadableAnswer(): never {
  throw new PersistenceError('parse', 'The local server sent an unreadable paper monitor answer')
}

function parseStake(v: unknown): PaperMonitorStake {
  const s = isRecord(v) ? v : {}
  return {
    last: num(s.last),
    riskPerTradePct: num(s.riskPerTradePct),
    maxTotalExposurePct: num(s.maxTotalExposurePct),
    openExposurePct: num(s.openExposurePct),
  }
}

function parseHalt(v: unknown): PaperMonitorHalt | null {
  if (v === null || v === undefined) return null
  const h = isRecord(v) ? v : {}
  const time = str(h.time)
  // An unreadable halt.lock arrives with zero figures and no time: those zeros are not readings.
  return {
    reason: str(h.reason).trim() || 'unknown',
    threshold: time === '' ? null : num(h.threshold),
    value: time === '' ? null : num(h.value),
    time,
  }
}

function parseProtectionLock(v: unknown): PaperMonitorProtectionLock | null {
  if (v === null || v === undefined) return null
  const lock = isRecord(v) ? v : {}
  return { until: str(lock.until), reason: str(lock.reason) }
}

function parseExpected(v: unknown): PaperMonitorExpected | null {
  if (!isRecord(v)) return null
  const tradesLo = num(v.tradesLo)
  const tradesHi = num(v.tradesHi)
  const exposureLo = num(v.exposureLo)
  const exposureHi = num(v.exposureHi)
  if (tradesLo === null || tradesHi === null || exposureLo === null || exposureHi === null) {
    return null
  }
  return {
    tradesLo,
    tradesHi,
    exposureLo,
    exposureHi,
    verdict: typeof v.verdict === 'string' && v.verdict !== '' ? v.verdict : null,
    asof: unixSeconds(v.asof),
  }
}

/** A time field the row could not use: 0 is not one, it is how the server says "unread". */
const badTime = (wire: unknown, time: number | null) => wire !== 0 && time === null

/**
 * One row, always: a malformed one comes back with `error` set, zeroed figures and `read` false.
 * `read` and `malformed` fail closed, so a row without them shows dashes rather than Off or None.
 */
function parseTest(v: unknown): PaperMonitorTest {
  const t = isRecord(v) ? v : {}
  const start = unixSeconds(t.start)
  const decisionEnd = unixSeconds(t.decisionEnd)
  const closedTrades = num(t.closedTrades)
  const openTrades = num(t.openTrades)
  const netPnl = num(t.netPnl)
  const drawdownPct = num(t.drawdownPct)
  const breaker = isRecord(t.circuitBreaker) ? t.circuitBreaker : null
  const maxDailyLossPct = num(breaker?.maxDailyLossPct)
  const maxDrawdownPct = num(breaker?.maxDrawdownPct)
  const breakerBroken =
    t.circuitBreaker !== null &&
    t.circuitBreaker !== undefined &&
    (maxDailyLossPct === null || maxDrawdownPct === null)
  const figuresUsable =
    closedTrades !== null &&
    openTrades !== null &&
    netPnl !== null &&
    drawdownPct !== null &&
    t.pnlBasis === 'realized'
  const read = isRecord(t.read) ? t.read : {}
  const malformed = isRecord(t.malformed) ? t.malformed : null
  const unreadable =
    !isRecord(v) ||
    typeof t.id !== 'string' ||
    t.id === '' ||
    typeof t.name !== 'string' ||
    t.name === '' ||
    badTime(t.start, start) ||
    badTime(t.decisionEnd, decisionEnd) ||
    !figuresUsable ||
    breakerBroken ||
    !isRecord(t.read) ||
    malformed === null
  const error = typeof t.error === 'string' && t.error !== '' ? t.error : null
  return {
    id: str(t.id),
    name: str(t.name) || 'Unnamed test',
    symbol: str(t.symbol),
    interval: str(t.interval),
    start,
    decisionEnd,
    wallet: num(t.wallet),
    closedTrades: numberOr(closedTrades, 0),
    openTrades: numberOr(openTrades, 0),
    netPnl: numberOr(netPnl, 0),
    netPnlPct: num(t.netPnlPct),
    drawdownPct: numberOr(drawdownPct, 0),
    pnlBasis: 'realized',
    stake: parseStake(t.stake),
    protections: t.protections === true,
    protectionLock: parseProtectionLock(t.protectionLock),
    circuitBreaker:
      maxDailyLossPct !== null && maxDrawdownPct !== null
        ? { maxDailyLossPct, maxDrawdownPct }
        : null,
    halt: parseHalt(t.halt),
    expected: parseExpected(t.expected),
    read: { protocol: read.protocol === true, trades: read.trades === true && figuresUsable },
    malformed: {
      protections: typeof t.protections !== 'boolean' || (malformed?.protections ?? true) !== false,
      circuitBreaker: breakerBroken || (malformed?.circuitBreaker ?? true) !== false,
    },
    error: error ?? (unreadable ? UNREADABLE_ROW : null),
  }
}

/**
 * Rows are keyed by id, and a repeated key could hide a row (and with it a halted test), so a
 * missing or repeated id gets a stand-in. A folder name cannot contain '/', so no real id matches.
 */
function withUniqueIds(tests: PaperMonitorTest[]): PaperMonitorTest[] {
  const seen = new Set<string>()
  return tests.map((test, index) => {
    const id = test.id === '' || seen.has(test.id) ? `/row-${index}` : test.id
    seen.add(id)
    return { ...test, id }
  })
}

/** Strict on the envelope; rows are read one by one (`parseTest`). */
export function parsePaperMonitorResponse(v: unknown): PaperMonitorResponse {
  if (!isRecord(v) || !Array.isArray(v.tests)) unreadableAnswer()
  const generatedAt = unixSeconds(v.generatedAt)
  if (generatedAt === null) unreadableAnswer()
  return { generatedAt, tests: withUniqueIds(v.tests.map(parseTest)) }
}

/** The fetch error as the panel shows it; a stopped server gets its own message. */
export function paperMonitorErrorMessage(err: unknown): string {
  if (!(err instanceof PersistenceError)) {
    return err instanceof Error ? err.message : String(err)
  }
  if (err.kind === 'network') return PAPER_MONITOR_OFFLINE_MESSAGE
  const status = err.status
  // The dev proxy answers a bare 500 when nothing listens on 8787.
  const proxyFailure = status === 500 && err.message === `Server error (HTTP ${status})`
  if (proxyFailure || status === 502 || status === 503 || status === 504) {
    return PAPER_MONITOR_OFFLINE_MESSAGE
  }
  if (status === 404)
    return 'This server has no paper monitor yet. Restart it to pick up the route.'
  return err.message
}

export function createPaperMonitorClient(opts: LocalServerClientOptions = {}): PaperMonitorClient {
  const request = createLocalServerRequest({ timeoutMs: PAPER_MONITOR_TIMEOUT_MS, ...opts })
  return {
    async fetchMonitor(signal) {
      const { body } = await request(PAPER_MONITOR_PATH, { method: 'GET', signal })
      return parsePaperMonitorResponse(body)
    },
  }
}
