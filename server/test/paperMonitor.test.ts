import { createHash } from 'node:crypto'
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import Database from 'better-sqlite3'
import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { DEFAULT_PAPER_DIR, loadPaperDir } from '../src/config.ts'
import { openDb, type Db } from '../src/db/index.ts'
import {
  DB_BUSY_TIMEOUT_MS,
  realizedDrawdownPct,
  type PaperMonitorResponse,
  type PaperMonitorTest,
} from '../src/routes/paperMonitor.ts'

const NOW_MS = Date.UTC(2026, 10, 4, 12, 0, 30)

interface TradeFixture {
  is_open: 0 | 1
  close_profit_abs: number | null
  stake_amount: number
  open_date: string
  close_date: string | null
}

/** Same column types as freqtrade's table, trimmed to what the route reads. */
const CREATE_TRADES = `CREATE TABLE trades (
  id INTEGER PRIMARY KEY,
  is_open BOOLEAN NOT NULL,
  close_profit_abs FLOAT,
  stake_amount FLOAT NOT NULL,
  open_date DATETIME NOT NULL,
  close_date DATETIME
)`
const INSERT_TRADE = `INSERT INTO trades (is_open, close_profit_abs, stake_amount, open_date, close_date)
  VALUES (@is_open, @close_profit_abs, @stake_amount, @open_date, @close_date)`

interface LockFixture {
  reason: string | null
  lock_end_time: string
  active?: 0 | 1
}

/** Freqtrade's real table, as read from a dry-run DB. */
const CREATE_PAIRLOCKS = `CREATE TABLE pairlocks (
  id INTEGER NOT NULL,
  pair VARCHAR(25) NOT NULL,
  side VARCHAR(25) NOT NULL,
  reason VARCHAR(255),
  lock_time DATETIME NOT NULL,
  lock_end_time DATETIME NOT NULL,
  active BOOLEAN NOT NULL,
  PRIMARY KEY (id)
)`
const INSERT_PAIRLOCK = `INSERT INTO pairlocks (pair, side, reason, lock_time, lock_end_time, active)
  VALUES ('ONDO/USDT', 'long', @reason, '2026-11-04 08:00:06.506631', @lock_end_time, @active)`

function closed(pnl: number | null, closeDate: string, stake = 100): TradeFixture {
  return {
    is_open: 0,
    close_profit_abs: pnl,
    stake_amount: stake,
    open_date: '2026-10-30 00:00:00.000000',
    close_date: closeDate,
  }
}

function open(stake: number, openDate: string): TradeFixture {
  return {
    is_open: 1,
    close_profit_abs: null,
    stake_amount: stake,
    open_date: openDate,
    close_date: null,
  }
}

/** The shape of the seven current HC protocols: no `risk_controls`, no `sizing`. */
function v7Protocol(candidate: string, extra: Record<string, unknown> = {}) {
  return {
    candidate,
    symbol: 'ONDOUSDT',
    interval: '4h',
    start: 1_793_318_400,
    decision_end: 1_798_156_800,
    trades: { lo: 0, hi: 26, expected: 6.5 },
    exposure: { lo: 0, hi: 0.824, expected: 0.18 },
    version: 7,
    ...extra,
  }
}

describe('paper monitor route', () => {
  let dir: string
  let db: Db
  let app: FastifyInstance

  beforeEach(async () => {
    dir = mkdtempSync(join(tmpdir(), 'paper-monitor-'))
    db = openDb(':memory:')
    app = await buildApp({ db, paperDir: dir, now: () => NOW_MS })
  })

  afterEach(async () => {
    await app.close()
    db.close()
    rmSync(dir, { recursive: true, force: true })
  })

  function folder(name: string): string {
    const path = join(dir, name)
    mkdirSync(path, { recursive: true })
    return path
  }

  function writeJson(path: string, value: unknown): void {
    writeFileSync(path, JSON.stringify(value))
  }

  function writeDb(path: string, trades: TradeFixture[], locks?: LockFixture[]): void {
    const conn = new Database(path)
    conn.exec(CREATE_TRADES)
    const insert = conn.prepare(INSERT_TRADE)
    for (const trade of trades) insert.run(trade)
    if (locks) {
      conn.exec(CREATE_PAIRLOCKS)
      const insertLock = conn.prepare(INSERT_PAIRLOCK)
      for (const lock of locks) insertLock.run({ active: 1, ...lock })
    }
    conn.close()
  }

  /** A paper test folder holding protocol.json and config.json. */
  function paperTest(
    name: string,
    opts: {
      protocol?: Record<string, unknown>
      wallet?: number
      trades?: TradeFixture[]
      locks?: LockFixture[]
    } = {},
  ): string {
    const path = folder(name)
    writeJson(join(path, 'protocol.json'), v7Protocol(name, opts.protocol))
    writeJson(join(path, 'config.json'), { dry_run_wallet: opts.wallet ?? 1000 })
    if (opts.trades) writeDb(join(path, 'dryrun.sqlite'), opts.trades, opts.locks)
    return path
  }

  async function monitor(url = '/api/paper/monitor'): Promise<PaperMonitorResponse> {
    const res = await app.inject({ method: 'GET', url })
    expect(res.statusCode).toBe(200)
    return res.json<PaperMonitorResponse>()
  }

  async function onlyTest(): Promise<PaperMonitorTest> {
    const { tests } = await monitor()
    expect(tests).toHaveLength(1)
    return tests[0] as PaperMonitorTest
  }

  function sha(path: string): string {
    return createHash('sha256').update(readFileSync(path)).digest('hex')
  }

  it('reports generatedAt in UNIX seconds and no tests for an empty dir', async () => {
    expect(await monitor()).toEqual({ generatedAt: Math.floor(NOW_MS / 1000), tests: [] })
  })

  it('reports no tests when the paper dir does not exist', async () => {
    rmSync(dir, { recursive: true })
    expect((await monitor()).tests).toEqual([])
  })

  it('reads a v7 protocol: optional blocks absent mean null or false, no DB means zero trades', async () => {
    paperTest('hc-a')
    expect(await onlyTest()).toEqual({
      id: 'hc-a',
      name: 'hc-a',
      symbol: 'ONDOUSDT',
      interval: '4h',
      start: 1_793_318_400,
      decisionEnd: 1_798_156_800,
      wallet: 1000,
      closedTrades: 0,
      openTrades: 0,
      netPnl: 0,
      netPnlPct: 0,
      drawdownPct: 0,
      pnlBasis: 'realized',
      stake: { last: null, riskPerTradePct: null, maxTotalExposurePct: null, openExposurePct: 0 },
      protections: false,
      protectionLock: null,
      circuitBreaker: null,
      halt: null,
      expected: {
        tradesLo: 0,
        tradesHi: 26,
        exposureLo: 0,
        exposureHi: 0.824,
        verdict: null,
        asof: null,
      },
      read: { protocol: true, trades: true },
      malformed: { protections: false, circuitBreaker: false },
      error: null,
    })
  })

  it('reads the risk_controls and sizing blocks', async () => {
    paperTest('hc-b', {
      protocol: {
        risk_controls: {
          protections: { MaxDrawdown: { max_allowed_drawdown: 0.15 } },
          circuit_breaker: { max_daily_loss_pct: 3, max_drawdown_pct: 12.5, wallet: 1000 },
        },
        sizing: { risk_per_trade_pct: 0.5, max_total_exposure_pct: 30, min_stake: 10 },
      },
    })
    const test = await onlyTest()
    expect(test.protections).toBe(true)
    expect(test.circuitBreaker).toEqual({ maxDailyLossPct: 3, maxDrawdownPct: 12.5 })
    expect(test.stake).toEqual({
      last: null,
      riskPerTradePct: 0.5,
      maxTotalExposurePct: 30,
      openExposurePct: 0,
    })
    expect(test.error).toBeNull()
  })

  it('treats null blocks as not configured, not as an error', async () => {
    paperTest('hc-c', { protocol: { risk_controls: { protections: null, circuit_breaker: null } } })
    const test = await onlyTest()
    expect(test.protections).toBe(false)
    expect(test.circuitBreaker).toBeNull()
    expect(test.malformed).toEqual({ protections: false, circuitBreaker: false })
    expect(test.error).toBeNull()
  })

  it('flags a malformed circuit_breaker instead of showing it as not configured', async () => {
    paperTest('hc-d', {
      protocol: { risk_controls: { circuit_breaker: { max_daily_loss_pct: 'three' } } },
    })
    const test = await onlyTest()
    expect(test.circuitBreaker).toBeNull()
    expect(test.malformed).toEqual({ protections: false, circuitBreaker: true })
    expect(test.error).toContain('circuit_breaker')
  })

  it.each([
    ['a string', 'yes'],
    ['a number', 3],
    ['true', true],
  ])('flags protections that are %s instead of showing them as off', async (_label, block) => {
    paperTest('hc-d2', { protocol: { risk_controls: { protections: block } } })
    const test = await onlyTest()
    expect(test.protections).toBe(false)
    expect(test.malformed).toEqual({ protections: true, circuitBreaker: false })
    expect(test.error).toContain('protections')
  })

  it('flags both blocks when risk_controls itself is not an object', async () => {
    paperTest('hc-d3', { protocol: { risk_controls: 'on' } })
    const test = await onlyTest()
    expect(test.malformed).toEqual({ protections: true, circuitBreaker: true })
    expect(test.error).toContain('risk_controls')
  })

  it('counts an empty protections block as off, not malformed', async () => {
    paperTest('hc-d4', { protocol: { risk_controls: { protections: {} } } })
    const test = await onlyTest()
    expect(test.protections).toBe(false)
    expect(test.malformed.protections).toBe(false)
    expect(test.error).toBeNull()
  })

  it('sends the folder name as id, unique even when two tests share a candidate', async () => {
    paperTest('folder-one', { protocol: { candidate: 'same' } })
    paperTest('folder-two', { protocol: { candidate: 'same' } })
    const { tests } = await monitor()
    expect(tests.map((t) => t.name)).toEqual(['same', 'same'])
    expect(tests.map((t) => t.id)).toEqual(['folder-one', 'folder-two'])
  })

  it('sums closed trades, counts open ones and takes the last stake by open date', async () => {
    paperTest('hc-e', {
      trades: [
        closed(10, '2026-11-01 00:00:01.000000', 100),
        open(95.5, '2026-11-03 00:00:00.000000'),
        closed(-4, '2026-11-02 00:00:02.000000', 100),
      ],
    })
    const test = await onlyTest()
    expect(test.closedTrades).toBe(2)
    expect(test.openTrades).toBe(1)
    expect(test.netPnl).toBe(6)
    expect(test.netPnlPct).toBeCloseTo(0.6, 12)
    expect(test.stake.last).toBe(95.5)
    expect(test.pnlBasis).toBe('realized')
  })

  it('computes realised drawdown in close_date order, not row order', async () => {
    // By close date: +100 (1100 peak), -220 (880), +44 (924). Worst fall 220 / 1100 = 20%.
    // In row order the same trades give -220, +44, +100 and a 22% fall from the 1000 start.
    paperTest('hc-f', {
      trades: [
        closed(-220, '2026-11-02 00:00:02.000000'),
        closed(44, '2026-11-03 00:00:03.000000'),
        closed(100, '2026-11-01 00:00:01.000000'),
      ],
    })
    const test = await onlyTest()
    expect(test.drawdownPct).toBeCloseTo(20, 9)
    expect(test.netPnl).toBe(-76)
  })

  it('realizedDrawdownPct matches hand-calculated values', () => {
    // Tolerance 1e-9: float rounding only.
    expect(realizedDrawdownPct(1000, [])).toBe(0)
    expect(realizedDrawdownPct(1000, [50, 50])).toBe(0)
    expect(realizedDrawdownPct(1000, [-100])).toBeCloseTo(10, 9)
    expect(realizedDrawdownPct(1000, [-100, -100])).toBeCloseTo(20, 9)
    // New peak 1200, then 900: 300 / 1200 = 25%; the earlier 10% does not win.
    expect(realizedDrawdownPct(1000, [-100, 300, -300])).toBeCloseTo(25, 9)
    // The recovery does not erase the worst point.
    expect(realizedDrawdownPct(1000, [-500, 500])).toBeCloseTo(50, 9)
  })

  it('flags an unknown wallet when closed trades need one', async () => {
    const path = paperTest('hc-g', { trades: [closed(-5, '2026-11-01 00:00:01.000000')] })
    writeJson(join(path, 'config.json'), { dry_run: true })
    const test = await onlyTest()
    expect(test.wallet).toBeNull()
    expect(test.netPnl).toBe(-5)
    expect(test.netPnlPct).toBeNull()
    expect(test.drawdownPct).toBe(0)
    expect(test.error).toContain('dry_run_wallet')
    expect(test.read).toEqual({ protocol: true, trades: true })
    expect(test.stake.openExposurePct).toBeNull()
  })

  it('reports open stake as a percent of the wallet, ignoring closed trades', async () => {
    paperTest('hc-g2', {
      wallet: 2000,
      trades: [
        closed(5, '2026-11-01 00:00:01.000000', 400),
        open(100, '2026-11-02 00:00:00.000000'),
        open(150, '2026-11-03 00:00:00.000000'),
      ],
    })
    expect((await onlyTest()).stake.openExposurePct).toBeCloseTo(12.5, 12)
  })

  it('reports zero exposure with a wallet and no open trade', async () => {
    paperTest('hc-g3', { trades: [closed(5, '2026-11-01 00:00:01.000000')] })
    expect((await onlyTest()).stake.openExposurePct).toBe(0)
  })

  describe('protection lock', () => {
    const FUTURE = '2026-11-04 16:00:00.000000'
    const PAST = '2026-11-04 08:00:00.000000'
    const COOLDOWN = 'Cooldown period for 2 candles.'

    async function lockOf(locks?: LockFixture[]): Promise<PaperMonitorTest['protectionLock']> {
      paperTest('hc-lock', { trades: [], locks })
      return (await onlyTest()).protectionLock
    }

    it('reports a protection lock that ends in the future, as an ISO UTC time', async () => {
      expect(await lockOf([{ reason: COOLDOWN, lock_end_time: FUTURE }])).toEqual({
        until: '2026-11-04T16:00:00Z',
        reason: COOLDOWN,
      })
    })

    it('ignores a lock that has ended, though freqtrade leaves it active', async () => {
      expect(await lockOf([{ reason: COOLDOWN, lock_end_time: PAST }])).toBeNull()
    })

    it('counts a lock that ends this instant as over', async () => {
      const lockEnd = new Date(NOW_MS).toISOString().slice(0, 23).replace('T', ' ')
      expect(await lockOf([{ reason: COOLDOWN, lock_end_time: `${lockEnd}000` }])).toBeNull()
    })

    it('ignores a lock freqtrade has released', async () => {
      expect(await lockOf([{ reason: COOLDOWN, lock_end_time: FUTURE, active: 0 }])).toBeNull()
    })

    it("ignores freqtrade's post-exit Auto lock, which is no protection", async () => {
      expect(await lockOf([{ reason: 'Auto lock', lock_end_time: FUTURE }])).toBeNull()
    })

    it('takes the lock that ends last when several are running', async () => {
      const lock = await lockOf([
        { reason: 'Auto lock', lock_end_time: '2026-11-05 00:00:00.000000' },
        { reason: COOLDOWN, lock_end_time: FUTURE },
        { reason: 'Max drawdown', lock_end_time: '2026-11-04 20:00:00.000000' },
      ])
      expect(lock).toEqual({ until: '2026-11-04T20:00:00Z', reason: 'Max drawdown' })
    })

    it('keeps a lock that has no reason text: unknown is not the post-exit lock', async () => {
      expect(await lockOf([{ reason: null, lock_end_time: FUTURE }])).toEqual({
        until: '2026-11-04T16:00:00Z',
        reason: '',
      })
    })

    it('gives null for a DB with no pairlocks table, with no error', async () => {
      paperTest('hc-lock', { trades: [] })
      const test = await onlyTest()
      expect(test.protectionLock).toBeNull()
      expect(test.error).toBeNull()
    })

    it('gives null without a DB', async () => {
      paperTest('hc-lock')
      expect((await onlyTest()).protectionLock).toBeNull()
    })

    it('leaves the DB bytes unchanged', async () => {
      const path = paperTest('hc-lock', {
        trades: [],
        locks: [{ reason: COOLDOWN, lock_end_time: FUTURE }],
      })
      const before = sha(join(path, 'dryrun.sqlite'))
      expect((await onlyTest()).protectionLock).not.toBeNull()
      expect(sha(join(path, 'dryrun.sqlite'))).toBe(before)
    })
  })

  it('reports a halt.lock', async () => {
    const path = paperTest('hc-h')
    writeJson(join(path, 'halt.lock'), {
      reason: 'daily loss',
      threshold: 3,
      value: 3.4,
      time: '2026-11-04T10:00:00+00:00',
    })
    expect((await onlyTest()).halt).toEqual({
      reason: 'daily loss',
      threshold: 3,
      value: 3.4,
      time: '2026-11-04T10:00:00+00:00',
    })
  })

  it.each([
    ['not JSON', '{oops'],
    ['empty', ''],
    ['the wrong JSON type', '[1, 2]'],
    ['missing keys', '{"reason": "x"}'],
    ['a non-numeric threshold', '{"reason":"x","threshold":"3","value":1,"time":"t"}'],
  ])('still shows halted when halt.lock is %s', async (_label, content) => {
    const path = paperTest('hc-i')
    writeFileSync(join(path, 'halt.lock'), content)
    expect((await onlyTest()).halt).toEqual({
      reason: 'unreadable halt.lock',
      threshold: 0,
      value: 0,
      time: '',
    })
  })

  it('still shows halted when halt.lock cannot be read as a file', async () => {
    const path = paperTest('hc-j')
    mkdirSync(join(path, 'halt.lock'))
    expect((await onlyTest()).halt?.reason).toBe('unreadable halt.lock')
  })

  it('leaves halt.lock in place after a request', async () => {
    const path = paperTest('hc-k')
    const lock = join(path, 'halt.lock')
    writeFileSync(lock, '{oops')
    const before = sha(lock)
    await monitor()
    expect(sha(lock)).toBe(before)
  })

  it('fills verdict and asof from the newest comparison by filename', async () => {
    const path = paperTest('hc-l')
    writeJson(join(path, 'comparison-2026-11-03.json'), {
      period_end: 1_793_700_000,
      protocol_verdict: { verdict: 'fail' },
    })
    writeJson(join(path, 'comparison-2026-11-04.json'), {
      period_end: 1_793_793_600,
      verdict: 'insufficient_data',
      protocol_verdict: { verdict: 'continue', headline: 'continue: 5.5 of 56.0 days' },
    })
    writeJson(join(path, 'comparison-latest.json'), { protocol_verdict: { verdict: 'pass' } })
    const test = await onlyTest()
    expect(test.expected?.verdict).toBe('continue')
    expect(test.expected?.asof).toBe(1_793_793_600)
  })

  it('leaves verdict null when the comparison has no protocol_verdict', async () => {
    const path = paperTest('hc-m')
    writeJson(join(path, 'comparison-2026-11-04.json'), { period_end: 1_793_793_600 })
    const test = await onlyTest()
    expect(test.expected?.verdict).toBeNull()
    expect(test.expected?.asof).toBe(1_793_793_600)
    expect(test.error).toBeNull()
  })

  it('keeps the bands and sets error when the newest comparison is corrupt', async () => {
    const path = paperTest('hc-n')
    writeFileSync(join(path, 'comparison-2026-11-04.json'), '{oops')
    const test = await onlyTest()
    expect(test.expected?.tradesHi).toBe(26)
    expect(test.expected?.verdict).toBeNull()
    expect(test.error).toContain('comparison-2026-11-04.json')
    expect(test.read).toEqual({ protocol: true, trades: true })
  })

  it('gives expected null when the protocol has no bands', async () => {
    paperTest('hc-o', { protocol: { trades: undefined, exposure: undefined } })
    expect((await onlyTest()).expected).toBeNull()
  })

  it('a corrupt DB fails only its own test and keeps that test readable', async () => {
    paperTest('hc-good', { trades: [closed(7, '2026-11-01 00:00:01.000000')] })
    const bad = paperTest('hc-bad')
    writeFileSync(
      join(bad, 'dryrun.sqlite'),
      'this is not a sqlite file, not even close'.repeat(80),
    )
    writeJson(join(bad, 'halt.lock'), { reason: 'x', threshold: 1, value: 2, time: 't' })

    const { tests } = await monitor()
    expect(tests.map((t) => t.name)).toEqual(['hc-bad', 'hc-good'])
    const [badTest, goodTest] = tests as [PaperMonitorTest, PaperMonitorTest]
    expect(badTest.error).toMatch(/^dryrun\.sqlite: /)
    expect(badTest.closedTrades).toBe(0)
    expect(badTest.read).toEqual({ protocol: true, trades: false })
    expect(badTest.stake.openExposurePct).toBeNull()
    expect(badTest.protectionLock).toBeNull()
    expect(badTest.halt?.reason).toBe('x')
    expect(badTest.expected?.tradesHi).toBe(26)
    expect(goodTest.error).toBeNull()
    expect(goodTest.netPnl).toBe(7)
  })

  it('a DB locked by a writer gives that test an error within the busy timeout', async () => {
    paperTest('hc-good', { trades: [closed(7, '2026-11-01 00:00:01.000000')] })
    const locked = join(paperTest('hc-locked', { trades: [] }), 'dryrun.sqlite')
    const writer = new Database(locked)
    writer.exec('BEGIN EXCLUSIVE')
    try {
      const startedAt = performance.now()
      const { tests } = await monitor()
      expect(performance.now() - startedAt).toBeLessThan(DB_BUSY_TIMEOUT_MS + 2000)
      const [goodTest, lockedTest] = tests as [PaperMonitorTest, PaperMonitorTest]
      expect(lockedTest.error).toMatch(/^dryrun\.sqlite: /)
      // One wait only: a locked DB must not be opened a second time for its locks.
      expect(lockedTest.error).not.toContain('pairlocks')
      expect(goodTest.error).toBeNull()
    } finally {
      writer.exec('ROLLBACK')
      writer.close()
    }
  })

  it('a corrupt protocol.json still returns the folder, its trades and its halt', async () => {
    paperTest('hc-good')
    const bad = paperTest('hc-bad-protocol', {
      trades: [closed(3, '2026-11-01 00:00:01.000000')],
    })
    writeFileSync(join(bad, 'protocol.json'), '{oops')
    writeFileSync(join(bad, 'halt.lock'), 'garbage')

    const { tests } = await monitor()
    const badTest = tests.find((t) => t.name === 'hc-bad-protocol')
    expect(tests).toHaveLength(2)
    expect(badTest?.error).toContain('protocol.json')
    expect(badTest?.id).toBe('hc-bad-protocol')
    expect(badTest?.read).toEqual({ protocol: false, trades: true })
    expect(badTest?.symbol).toBe('')
    expect(badTest?.netPnl).toBe(3)
    expect(badTest?.halt?.reason).toBe('unreadable halt.lock')
    expect(badTest?.expected).toBeNull()
  })

  it('does not count a valid JSON object that is no protocol as read', async () => {
    const path = paperTest('hc-empty-protocol')
    writeJson(join(path, 'protocol.json'), { version: 8 })
    const test = await onlyTest()
    expect(test.read.protocol).toBe(false)
    expect(test.error).toContain('protocol.json lacks')
  })

  it('skips jev-shadow, folders without a protocol, loose files and nested protocols', async () => {
    paperTest('hc-a')
    writeFileSync(join(folder('jev-shadow'), 'decisions.jsonl'), '{}\n')
    folder('no-protocol')
    writeJson(join(folder('parent/child'), 'protocol.json'), v7Protocol('nested'))
    writeJson(join(dir, 'protocol.json'), v7Protocol('loose'))
    expect((await monitor()).tests.map((t) => t.name)).toEqual(['hc-a'])
  })

  it('does not follow a symlink out of the paper dir', async () => {
    const outside = mkdtempSync(join(tmpdir(), 'paper-monitor-outside-'))
    try {
      writeJson(join(outside, 'protocol.json'), v7Protocol('outside'))
      symlinkSync(outside, join(dir, 'linked'))
      expect((await monitor()).tests).toEqual([])
    } finally {
      rmSync(outside, { recursive: true, force: true })
    }
  })

  it('leaves the DB bytes unchanged after a request', async () => {
    const path = paperTest('hc-a', { trades: [closed(1, '2026-11-01 00:00:01.000000')] })
    const dbPath = join(path, 'dryrun.sqlite')
    const before = sha(dbPath)
    expect((await onlyTest()).closedTrades).toBe(1)
    expect(sha(dbPath)).toBe(before)
    expect(readdirSync(path).sort()).toEqual(['config.json', 'dryrun.sqlite', 'protocol.json'])
  })

  it('reads rows a live bot holds in the WAL without changing the main DB file', async () => {
    const dbPath = join(paperTest('hc-live'), 'dryrun.sqlite')
    const bot = new Database(dbPath)
    try {
      bot.pragma('journal_mode = WAL')
      bot.pragma('wal_autocheckpoint = 0')
      bot.exec(CREATE_TRADES)
      bot.pragma('wal_checkpoint(TRUNCATE)')
      const before = sha(dbPath)

      bot.prepare(INSERT_TRADE).run(closed(-12.5, '2026-11-01 00:00:01.000000'))
      const test = await onlyTest()

      expect(test.closedTrades).toBe(1)
      expect(test.netPnl).toBe(-12.5)
      expect(sha(dbPath)).toBe(before)
      // The bot still writes after the reader has gone.
      bot.prepare(INSERT_TRADE).run(open(50, '2026-11-02 00:00:00.000000'))
      expect((await onlyTest()).openTrades).toBe(1)
    } finally {
      bot.close()
    }
  })

  it('reads a WAL-mode DB after the bot stopped, main file unchanged', async () => {
    const dbPath = join(paperTest('hc-stopped'), 'dryrun.sqlite')
    const bot = new Database(dbPath)
    bot.pragma('journal_mode = WAL')
    bot.exec(CREATE_TRADES)
    bot.prepare(INSERT_TRADE).run(closed(2, '2026-11-01 00:00:01.000000'))
    bot.close()
    const before = sha(dbPath)

    const test = await onlyTest()

    expect(test.closedTrades).toBe(1)
    expect(test.error).toBeNull()
    expect(sha(dbPath)).toBe(before)
  })

  it('ignores a path in the query string', async () => {
    paperTest('hc-a')
    const other = mkdtempSync(join(tmpdir(), 'paper-monitor-other-'))
    try {
      const { tests } = await monitor(`/api/paper/monitor?dir=${encodeURIComponent(other)}`)
      expect(tests.map((t) => t.name)).toEqual(['hc-a'])
    } finally {
      rmSync(other, { recursive: true, force: true })
    }
  })

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'] as const)('has no %s method', async (method) => {
    const res = await app.inject({ method, url: '/api/paper/monitor' })
    expect(res.statusCode).toBe(404)
  })
})

describe('loadPaperDir', () => {
  it('defaults to agents/data/paper in the repo', () => {
    expect(DEFAULT_PAPER_DIR).toBe(resolve(import.meta.dirname, '..', '..', 'agents/data/paper'))
    expect(loadPaperDir({})).toBe(DEFAULT_PAPER_DIR)
    expect(loadPaperDir({ PAPER_DIR: '' })).toBe(DEFAULT_PAPER_DIR)
  })

  it('takes PAPER_DIR and resolves it', () => {
    expect(loadPaperDir({ PAPER_DIR: '/x/y/../paper' })).toBe('/x/paper')
    expect(loadPaperDir({ PAPER_DIR: 'rel/paper' })).toBe(resolve('rel/paper'))
  })
})
