import { describe, expect, it, vi } from 'vitest'
import type { FetchLike } from './http'
import {
  PAPER_MONITOR_OFFLINE_MESSAGE,
  PAPER_MONITOR_PATH,
  createPaperMonitorClient,
  paperMonitorErrorMessage,
  parsePaperMonitorResponse,
  unixSeconds,
} from './paperMonitor'
import { PersistenceError } from './persistence'

/** One row as the server sends it for a current HC test. */
function wireTest(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'hc-macd-rsi-btc-4h-exploratory',
    name: 'hc-macd-rsi-btc-4h-exploratory',
    symbol: 'BTCUSDT',
    interval: '4h',
    start: 1_793_318_400,
    decisionEnd: 1_798_156_800,
    wallet: 1000,
    closedTrades: 3,
    openTrades: 1,
    netPnl: -12.5,
    netPnlPct: -1.25,
    drawdownPct: 2.5,
    pnlBasis: 'realized',
    stake: { last: 100, riskPerTradePct: null, maxTotalExposurePct: null, openExposurePct: 10 },
    protections: false,
    protectionLock: null,
    circuitBreaker: null,
    halt: null,
    expected: {
      tradesLo: 2,
      tradesHi: 14,
      exposureLo: 0.036,
      exposureHi: 0.475,
      verdict: 'continue',
      asof: 1_793_793_600,
    },
    read: { protocol: true, trades: true },
    malformed: { protections: false, circuitBreaker: false },
    error: null,
    ...over,
  }
}

const parseOne = (row: unknown) =>
  parsePaperMonitorResponse({ generatedAt: 1_793_800_000, tests: [row] }).tests[0]

describe('parsePaperMonitorResponse', () => {
  it('passes a well-formed row through unchanged', () => {
    const row = wireTest({
      stake: { last: 50, riskPerTradePct: 1, maxTotalExposurePct: 80, openExposurePct: 5 },
      protections: true,
      protectionLock: { until: '2026-11-05T04:00:00Z', reason: 'Cooldown period for 2 candles.' },
      circuitBreaker: { maxDailyLossPct: 3, maxDrawdownPct: 10 },
      halt: { reason: 'max_drawdown', threshold: 10, value: 11.5, time: '2026-11-03T14:00:00Z' },
    })
    expect(parseOne(row)).toEqual(row)
  })

  it('rejects an answer whose envelope is not the contract', () => {
    for (const bad of [null, [], {}, { generatedAt: 1 }, { generatedAt: 'x', tests: [] }]) {
      expect(() => parsePaperMonitorResponse(bad)).toThrow(PersistenceError)
    }
  })

  it('keeps a malformed row in the list with an error instead of dropping it', () => {
    const res = parsePaperMonitorResponse({
      generatedAt: 1,
      tests: ['nonsense', wireTest({ netPnl: 'lots' }), wireTest({ pnlBasis: 'marked' })],
    })
    expect(res.tests).toHaveLength(3)
    for (const t of res.tests) expect(t.error).toMatch(/unreadable/)
  })

  it('keeps the server error text of a test it could only partly read', () => {
    expect(parseOne(wireTest({ error: 'dryrun.sqlite: database is locked' })).error).toBe(
      'dryrun.sqlite: database is locked',
    )
  })

  it('reads any non-null halt as halted, however malformed', () => {
    expect(parseOne(wireTest({ halt: true })).halt).toEqual({
      reason: 'unknown',
      threshold: null,
      value: null,
      time: '',
    })
    expect(parseOne(wireTest({ halt: {} })).halt).not.toBeNull()
    expect(parseOne(wireTest({ halt: null })).halt).toBeNull()
  })

  it('does not take the zeros of an unreadable halt.lock for readings', () => {
    const halt = { reason: 'unreadable halt.lock', threshold: 0, value: 0, time: '' }
    expect(parseOne(wireTest({ halt })).halt).toEqual({ ...halt, threshold: null, value: null })
  })

  it('flags a breaker it cannot read: that is not the same as no breaker', () => {
    const t = parseOne(wireTest({ circuitBreaker: { maxDailyLossPct: 3 } }))
    expect(t.circuitBreaker).toBeNull()
    expect(t.malformed.circuitBreaker).toBe(true)
    expect(t.error).toMatch(/unreadable/)
  })

  it('reads a row without read flags as read from nothing', () => {
    const t = parseOne(wireTest({ read: undefined }))
    expect(t.read).toEqual({ protocol: false, trades: false })
    expect(t.error).toMatch(/unreadable/)
  })

  it('reads only a literal true as read', () => {
    const t = parseOne(wireTest({ read: { protocol: 1, trades: 'yes' } }))
    expect(t.read).toEqual({ protocol: false, trades: false })
  })

  it('does not call figures read when a row sends them broken', () => {
    expect(parseOne(wireTest({ netPnl: 'lots' })).read.trades).toBe(false)
    expect(parseOne(wireTest({ closedTrades: null })).read.trades).toBe(false)
    expect(parseOne(wireTest({ pnlBasis: 'marked' })).read.trades).toBe(false)
  })

  it('keeps the read flags of a row the server could only partly read', () => {
    const t = parseOne(
      wireTest({ read: { protocol: false, trades: true }, error: 'protocol.json is not valid' }),
    )
    expect(t.read).toEqual({ protocol: false, trades: true })
  })

  it('takes the malformed flags from the server', () => {
    const t = parseOne(wireTest({ malformed: { protections: true, circuitBreaker: false } }))
    expect(t.malformed).toEqual({ protections: true, circuitBreaker: false })
  })

  it('reads missing malformed flags or a non-boolean protections as unknown', () => {
    expect(parseOne(wireTest({ malformed: undefined })).malformed).toEqual({
      protections: true,
      circuitBreaker: true,
    })
    const t = parseOne(wireTest({ protections: 'on' }))
    expect(t.protections).toBe(false)
    expect(t.malformed.protections).toBe(true)
  })

  it('reads any non-null protection lock as locked, however malformed', () => {
    expect(parseOne(wireTest({ protectionLock: true })).protectionLock).toEqual({
      until: '',
      reason: '',
    })
    expect(parseOne(wireTest({ protectionLock: undefined })).protectionLock).toBeNull()
  })

  it('reads a missing openExposurePct as unknown', () => {
    const t = parseOne(wireTest({ stake: { last: 100 } }))
    expect(t.stake.openExposurePct).toBeNull()
  })

  it.each([
    ['beyond what Date can hold', 9e12],
    ['milliseconds', 1_798_156_800_000],
    ['negative', -1],
  ])('treats a decision end that is %s as null, and flags the row', (_label, value) => {
    const t = parseOne(wireTest({ decisionEnd: value }))
    expect(t.decisionEnd).toBeNull()
    expect(t.error).toMatch(/unreadable/)
  })

  it('treats a bad start or asof as null', () => {
    const t = parseOne(
      wireTest({
        start: 1_793_318_400_000,
        expected: {
          tradesLo: 2,
          tradesHi: 14,
          exposureLo: 0,
          exposureHi: 1,
          verdict: 'continue',
          asof: 1_793_793_600_000,
        },
      }),
    )
    expect(t.start).toBeNull()
    expect(t.expected?.asof).toBeNull()
  })

  it('reads the zero the server sends for an unread protocol as null, without a parse error', () => {
    const t = parseOne(wireTest({ start: 0, decisionEnd: 0 }))
    expect(t.start).toBeNull()
    expect(t.decisionEnd).toBeNull()
    expect(t.error).toBeNull()
  })

  it('rejects an answer whose own time is out of range', () => {
    expect(() => parsePaperMonitorResponse({ generatedAt: 9e12, tests: [] })).toThrow(
      PersistenceError,
    )
    expect(() => parsePaperMonitorResponse({ generatedAt: 1_793_800_000_000, tests: [] })).toThrow(
      PersistenceError,
    )
  })

  it('gives every row a unique id, even with the id missing or repeated', () => {
    const res = parsePaperMonitorResponse({
      generatedAt: 1,
      tests: [
        wireTest({ id: 'a' }),
        wireTest({ id: 'a' }),
        wireTest({ id: undefined }),
        wireTest({ id: undefined }),
      ],
    })
    const ids = res.tests.map((t) => t.id)
    expect(ids[0]).toBe('a')
    expect(new Set(ids).size).toBe(4)
    expect(res.tests[2].error).toMatch(/unreadable/)
  })

  it('reads a missing stake block as dashes and an incomplete expected range as none', () => {
    const t = parseOne(wireTest({ stake: undefined, expected: { tradesLo: 2 } }))
    expect(t.stake).toEqual({
      last: null,
      riskPerTradePct: null,
      maxTotalExposurePct: null,
      openExposurePct: null,
    })
    expect(t.expected).toBeNull()
    expect(t.error).toBeNull()
  })
})

describe('unixSeconds', () => {
  it('accepts a real time in seconds', () => {
    expect(unixSeconds(1_793_800_800)).toBe(1_793_800_800)
    expect(unixSeconds(1)).toBe(1)
  })

  it('refuses what is not a time in seconds', () => {
    for (const bad of [0, -1, 1e11, 1_793_800_800_000, 9e12, NaN, Infinity, '1793800800', null]) {
      expect(unixSeconds(bad)).toBeNull()
    }
  })
})

describe('paperMonitorErrorMessage', () => {
  it('words every sign of a stopped server the same way', () => {
    const stopped = [
      new PersistenceError('network', 'Cannot reach the local server'),
      // Vite's proxy with nothing on 8787.
      new PersistenceError('http', 'Server error (HTTP 500)', 500),
      new PersistenceError('http', 'Server error (HTTP 502)', 502),
      new PersistenceError('http', 'Server error (HTTP 504)', 504),
    ]
    for (const err of stopped) {
      expect(paperMonitorErrorMessage(err)).toBe(PAPER_MONITOR_OFFLINE_MESSAGE)
    }
  })

  it('keeps a message the server wrote itself', () => {
    const err = new PersistenceError('http', 'PAPER_DIR is not readable', 500)
    expect(paperMonitorErrorMessage(err)).toBe('PAPER_DIR is not readable')
  })

  it('says so when a running server predates the route', () => {
    const err = new PersistenceError('http', 'Server error (HTTP 404)', 404)
    expect(paperMonitorErrorMessage(err)).toMatch(/Restart/)
  })
})

describe('createPaperMonitorClient', () => {
  it('sends one GET to the monitor path and nothing else', async () => {
    const fetchImpl = vi.fn<FetchLike>(() =>
      Promise.resolve(new Response(JSON.stringify({ generatedAt: 5, tests: [] }))),
    )
    const res = await createPaperMonitorClient({ fetchImpl }).fetchMonitor()
    expect(res).toEqual({ generatedAt: 5, tests: [] })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe(PAPER_MONITOR_PATH)
    expect(url).toBe('/api/paper/monitor')
    expect(init?.method).toBe('GET')
    expect(init?.body).toBeUndefined()
  })

  it('offers reads only: the client has no other method', () => {
    const client = createPaperMonitorClient({ fetchImpl: () => Promise.reject(new Error('no')) })
    expect(Object.keys(client)).toEqual(['fetchMonitor'])
  })
})
