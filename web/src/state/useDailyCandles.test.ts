// The daily candle lifecycle, driven through the hook harness and fake timers (no DOM).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { CandleSource } from '../data/timeframeProvider'
import type { Candle } from '../types'
import { fakeReactHooks, mountHook } from './hookHarness.testkit'
import {
  DAILY_CANDLE_LIMIT,
  DAILY_REFRESH_SETTLE_MS,
  closedDailyCandles,
  msToNextDailyRefresh,
  useDailyCandles,
} from './useDailyCandles'

vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...fakeReactHooks,
}))

type DailyProvider = Pick<MarketDataProvider, 'getCandles'>

const DAY = 86_400
const DAY_MS = DAY * 1000
const SETTLE = DAILY_REFRESH_SETTLE_MS
/** The documented wait after each try of one load. */
const RETRIES = [15_000, 30_000, 60_000, 120_000, 300_000]
/** The documented wait after those, until the next refresh. */
const SLOW_RETRY = 1_800_000

/** 2024-01-10 00:00 UTC, in seconds. */
const D10 = Date.UTC(2024, 0, 10) / 1000
/** 12:00 UTC on 2024-01-10: the 01-09 candle is the last closed one, 01-10 is forming. */
const NOON_MS = (D10 + DAY / 2) * 1000
/** Ms from NOON_MS to the refresh after 2024-01-11 00:00 UTC. */
const TO_REFRESH = DAY_MS / 2 + SETTLE
/** Rows through 01-09 only: what a refresh after 01-10 closed reads while that day is late. */
const withoutNewDay = () => days(D10 - DAY, 59)

function candle(time: number, close = 100): Candle {
  return { time, open: 100, high: 110, low: 90, close, volume: 5 }
}

/** `n` consecutive daily candles ending with the one opening at `last`. */
function days(last: number, n: number): Candle[] {
  return Array.from({ length: n }, (_, i) => candle(last - (n - 1 - i) * DAY))
}

/** A provider whose requests stay pending until the test settles them. */
function pendingProvider() {
  const calls: {
    symbol: string
    args: unknown[]
    signal: AbortSignal | undefined
    resolve(rows: Candle[]): void
    reject(e: unknown): void
  }[] = []
  const provider: DailyProvider = {
    getCandles: (symbol, ...args) =>
      new Promise((resolve, reject) => {
        calls.push({ symbol, args, signal: args[3]?.signal, resolve, reject })
      }),
  }
  return { provider, calls }
}

interface Props {
  provider: DailyProvider
  symbol: string
  enabled: boolean
}
const useDaily = (p: Props) => useDailyCandles(p.provider, p.symbol, p.enabled)

/** Let a settled request's callbacks run. */
const settle = () => vi.advanceTimersByTimeAsync(0)

/** Mounted, with the first load answered: 01-09 closed and 01-10 forming. */
async function loaded(opts: { strict?: boolean } = {}) {
  const { provider, calls } = pendingProvider()
  const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true }, opts)
  const firstLoad = calls[calls.length - 1]
  firstLoad.resolve(days(D10, 60))
  await settle()
  hook.flush()
  return { provider, calls, hook }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOON_MS)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('DAILY_CANDLE_LIMIT', () => {
  it('covers the SMA(50) + ATR(14) warm-up and the 1000-bar provider cap', () => {
    expect(DAILY_CANDLE_LIMIT).toBeGreaterThanOrEqual(50 + 14)
    expect(DAILY_CANDLE_LIMIT).toBeLessThanOrEqual(1000)
  })
})

describe('closedDailyCandles', () => {
  it('keeps a row at the exact boundary and drops it one second earlier', () => {
    const rows = [candle(D10 - DAY), candle(D10), candle(D10 + DAY)]
    expect(closedDailyCandles(rows, D10 + DAY).map((c) => c.time)).toEqual([D10 - DAY, D10])
    expect(closedDailyCandles(rows, D10 + DAY - 1).map((c) => c.time)).toEqual([D10 - DAY])
  })

  it('never trusts an answer’s last row, whatever the clock says', () => {
    const rows = [candle(D10 - DAY), candle(D10)]
    expect(closedDailyCandles(rows, D10 + 100 * DAY).map((c) => c.time)).toEqual([D10 - DAY])
  })

  it('returns nothing for an empty list or a lone forming row, and leaves the input alone', () => {
    expect(closedDailyCandles([], D10)).toEqual([])
    const rows = [candle(D10)]
    expect(closedDailyCandles(rows, D10 + 5)).toEqual([])
    expect(rows).toHaveLength(1)
  })
})

describe('msToNextDailyRefresh', () => {
  const MIDNIGHT = D10 * 1000

  it('counts to the next UTC midnight plus the settle delay', () => {
    expect(msToNextDailyRefresh(NOON_MS)).toBe(TO_REFRESH)
    expect(msToNextDailyRefresh(MIDNIGHT - 1)).toBe(1 + SETTLE)
  })

  it('still counts a midnight that has just passed until its refresh instant', () => {
    expect(msToNextDailyRefresh(MIDNIGHT)).toBe(SETTLE)
    expect(msToNextDailyRefresh(MIDNIGHT + SETTLE - 1)).toBe(1)
    expect(msToNextDailyRefresh(MIDNIGHT + SETTLE)).toBe(DAY_MS)
  })
})

describe('useDailyCandles', () => {
  it('asks the provider for 1d candles and never exposes the forming row', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
    expect(calls).toHaveLength(1)
    expect(calls[0].symbol).toBe('BTCUSDT')
    expect(calls[0].args.slice(0, 3)).toEqual(['1d', DAILY_CANDLE_LIMIT, undefined])

    calls[0].resolve(days(D10, 60))
    await settle()
    hook.flush()

    expect(hook.result.loading).toBe(false)
    expect(hook.result.error).toBeNull()
    expect(hook.result.candles).toHaveLength(59)
    expect(hook.result.candles?.at(-1)?.time).toBe(D10 - DAY)
  })

  it('takes the chart’s CandleSource as its provider', () => {
    const source = { getCandles: () => Promise.resolve([]) } as unknown as CandleSource
    const hook = mountHook(useDaily, { provider: source, symbol: 'BTCUSDT', enabled: false })
    expect(hook.result.candles).toBeNull()
  })

  it('makes no request and arms no timer while disabled', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: false })
    await vi.advanceTimersByTimeAsync(2 * DAY_MS)
    expect(calls).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
    expect(hook.result).toEqual({ candles: null, error: null, loading: false })
  })

  it('drops the candles when disabled and fetches again when re-enabled', async () => {
    const { provider, calls, hook } = await loaded()
    expect(hook.result.candles).not.toBeNull()

    hook.rerender({ provider, symbol: 'BTCUSDT', enabled: false })
    expect(hook.result).toEqual({ candles: null, error: null, loading: false })
    expect(vi.getTimerCount()).toBe(0)
    hook.flush()

    hook.rerender({ provider, symbol: 'BTCUSDT', enabled: true })
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
    expect(calls).toHaveLength(2)
    calls[1].resolve(days(D10, 60))
    await settle()
    hook.flush()
    expect(hook.result.candles).toHaveLength(59)
  })

  it('returns null in the very render a symbol changes and aborts the old request', async () => {
    const { provider, calls, hook } = await loaded()
    const btc = hook.result.candles
    expect(btc).not.toBeNull()

    hook.rerender({ provider, symbol: 'ETHUSDT', enabled: true })
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
    expect(calls).toHaveLength(2)
    expect(calls[1].symbol).toBe('ETHUSDT')

    const eth = days(D10, 10).map((c) => ({ ...c, close: 7 }))
    calls[1].resolve(eth)
    await settle()
    hook.flush()
    expect(hook.result.candles).toHaveLength(9)
    expect(hook.result.candles?.[0].close).toBe(7)
  })

  it('ignores a late answer for the old symbol, even if the provider ignores the abort', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })

    hook.rerender({ provider, symbol: 'ETHUSDT', enabled: true })
    expect(calls[0].signal?.aborted).toBe(true)
    expect(calls[1].signal?.aborted).toBe(false)

    calls[0].resolve(days(D10, 60))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
  })

  it('does not bring back old candles when returning to the first symbol', async () => {
    const { provider, calls, hook } = await loaded()
    hook.rerender({ provider, symbol: 'ETHUSDT', enabled: true })
    hook.flush()
    hook.rerender({ provider, symbol: 'BTCUSDT', enabled: true })
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
    expect(calls).toHaveLength(3)
  })

  it('treats a new provider like a new market: another venue’s bars are never shown', async () => {
    const { provider, hook } = await loaded()
    const other = pendingProvider()
    hook.rerender({ provider: other.provider, symbol: 'BTCUSDT', enabled: true })
    expect(hook.result).toEqual({ candles: null, error: null, loading: true })
    expect(other.calls).toHaveLength(1)
    hook.rerender({ provider, symbol: 'BTCUSDT', enabled: true })
    expect(hook.result.candles).toBeNull()
  })

  it('refreshes at UTC midnight plus the settle delay with the day that just closed', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles

    await vi.advanceTimersByTimeAsync(TO_REFRESH - 1)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(2)

    calls[1].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles).not.toBe(before)
    expect(hook.result.candles).toHaveLength(60)
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    expect(hook.result.error).toBeNull()
    expect(hook.result.loading).toBe(false)

    await vi.advanceTimersByTimeAsync(DAY_MS)
    expect(calls).toHaveLength(3)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not pass a row that ended while the answer travelled as closed', async () => {
    // Sent one second before the 01-10 row counts as closed, answered after.
    vi.setSystemTime((D10 + DAY) * 1000 + SETTLE - 1000)
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    await vi.advanceTimersByTimeAsync(5000)
    calls[0].resolve(days(D10, 60))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10 - DAY)
  })

  it('waits for the settle delay when mounted just after midnight', async () => {
    vi.setSystemTime((D10 + DAY) * 1000 + 3000)
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    calls[0].resolve(days(D10, 60))
    await settle()
    hook.flush()
    // The 01-10 row has ended but is not settled; the refresh 7 s later is the next step.
    expect(hook.result.candles?.at(-1)?.time).toBe(D10 - DAY)
    await vi.advanceTimersByTimeAsync(SETTLE - 3000)
    expect(calls).toHaveLength(2)
    calls[1].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
  })

  it('refreshes soon after waking when timers stalled through midnight', async () => {
    const { calls } = await loaded()
    await vi.advanceTimersByTimeAsync(1000)
    // The clock jumps past the refresh; the pending timer has not advanced with it.
    vi.setSystemTime(NOON_MS + 2 * DAY_MS)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(calls).toHaveLength(2)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries a refresh without the new day on the stated delays, then slowly', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    expect(calls).toHaveLength(2)

    for (const [i, wait] of RETRIES.entries()) {
      calls[calls.length - 1].resolve(withoutNewDay())
      await settle()
      hook.flush()
      expect(hook.result.candles).toBe(before)
      expect(hook.result.error).toBeNull()
      await vi.advanceTimersByTimeAsync(wait - 1)
      expect(calls).toHaveLength(2 + i)
      await vi.advanceTimersByTimeAsync(1)
      expect(calls).toHaveLength(3 + i)
    }

    calls[calls.length - 1].resolve(withoutNewDay())
    await settle()
    hook.flush()
    expect(calls).toHaveLength(2 + RETRIES.length)
    expect(hook.result.candles).toBe(before)
    expect(hook.result.error).toMatch(/not available yet/)

    await vi.advanceTimersByTimeAsync(SLOW_RETRY - 1)
    expect(calls).toHaveLength(2 + RETRIES.length)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(3 + RETRIES.length)

    // The late day arrives: the note clears and the next request is the next refresh.
    calls[calls.length - 1].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    expect(hook.result.error).toBeNull()
    await vi.advanceTimersByTimeAsync(10 * SLOW_RETRY)
    expect(calls).toHaveLength(3 + RETRIES.length)
  })

  it('never lets a slow try delay the next refresh, which starts the fast tries again', async () => {
    const { calls, hook } = await loaded()
    const firstRefresh = NOON_MS + TO_REFRESH
    const nextRefresh = firstRefresh + DAY_MS
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    const stamps: number[] = []
    while (Date.now() < nextRefresh) {
      calls[calls.length - 1].reject(new MarketDataError('network', 'offline'))
      const before = calls.length
      await settle()
      while (calls.length === before) await vi.advanceTimersByTimeAsync(1000)
      stamps.push(Date.now())
    }
    expect(stamps.at(-1)).toBe(nextRefresh)
    const spent = RETRIES.reduce((a, b) => a + b, 0)
    expect(stamps[RETRIES.length]).toBe(firstRefresh + spent + SLOW_RETRY)
    expect(calls.length).toBeLessThanOrEqual(2 + RETRIES.length + 48)

    calls[calls.length - 1].reject(new MarketDataError('network', 'offline'))
    await settle()
    hook.flush()
    await vi.advanceTimersByTimeAsync(RETRIES[0])
    expect(Date.now()).toBe(nextRefresh + RETRIES[0])
    expect(stamps.length + 2).toBe(calls.length - 1)
  })

  it('retries when a fast clock refreshes before the day has really ended', async () => {
    const { calls, hook } = await loaded()
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    expect(calls).toHaveLength(2)
    // The local clock says the day is over; the venue's last row is still 01-10, forming.
    calls[1].resolve(days(D10, 60).map((c, i) => (i === 59 ? candle(c.time, 123) : c)))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10 - DAY)
    await vi.advanceTimersByTimeAsync(RETRIES[0])
    expect(calls).toHaveLength(3)
    calls[2].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    expect(hook.result.candles?.at(-1)?.close).toBe(100)
  })

  it('does not skip a refresh instant that passed while the answer travelled', async () => {
    // Sent at 00:00:09, 1 s before the refresh instant, answered at 00:00:11.
    vi.setSystemTime((D10 + DAY) * 1000 + SETTLE - 1000)
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    await vi.advanceTimersByTimeAsync(2000)
    calls[0].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    // The 01-10 row was read before the cutoff passed it, so it is not closed yet.
    expect(hook.result.candles?.at(-1)?.time).toBe(D10 - DAY)
    expect(calls).toHaveLength(2)
    calls[1].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    expect(vi.getTimerCount()).toBe(1)
  })

  it('stops retrying as soon as the new day arrives', async () => {
    const { calls, hook } = await loaded()
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    calls[1].resolve(withoutNewDay())
    await vi.advanceTimersByTimeAsync(RETRIES[0])
    expect(calls).toHaveLength(3)
    calls[2].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    await vi.advanceTimersByTimeAsync(RETRIES[1])
    expect(calls).toHaveLength(3)
  })

  it('keeps the last good candles through a failed refresh, sets error, then recovers', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)

    calls[1].reject(new MarketDataError('network', 'offline'))
    await settle()
    hook.flush()
    expect(hook.result.candles).toBe(before)
    expect(hook.result.error).toBe('offline')
    expect(hook.result.loading).toBe(false)

    await vi.advanceTimersByTimeAsync(RETRIES[0])
    expect(calls).toHaveLength(3)
    calls[2].resolve(days(D10 + DAY, 61))
    await settle()
    hook.flush()
    expect(hook.result.candles?.at(-1)?.time).toBe(D10)
    expect(hook.result.error).toBeNull()
  })

  it('slows the retries of a refresh that keeps failing', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    for (const wait of RETRIES) {
      calls[calls.length - 1].reject(new MarketDataError('network', 'offline'))
      await vi.advanceTimersByTimeAsync(wait)
    }
    expect(calls).toHaveLength(2 + RETRIES.length)
    calls[calls.length - 1].reject(new MarketDataError('network', 'still offline'))
    await settle()
    hook.flush()
    await vi.advanceTimersByTimeAsync(SLOW_RETRY - 1)
    expect(calls).toHaveLength(2 + RETRIES.length)
    expect(hook.result.candles).toBe(before)
    expect(hook.result.error).toBe('still offline')
    expect(vi.getTimerCount()).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(3 + RETRIES.length)
  })

  it('keeps known days when a refresh answers with less than it had', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    calls[1].resolve([])
    await settle()
    hook.flush()
    expect(hook.result.candles).toBe(before)
  })

  it('sets error and leaves candles null when the first load fails, and retries', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    calls[0].reject(new Error('boom'))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({ candles: null, error: 'boom', loading: false })

    await vi.advanceTimersByTimeAsync(RETRIES[0])
    calls[1].resolve(days(D10, 60))
    await settle()
    hook.flush()
    expect(hook.result.candles).toHaveLength(59)
    expect(hook.result.error).toBeNull()
  })

  it('waits at least a venue’s Retry-After', async () => {
    const { provider, calls } = pendingProvider()
    mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    calls[0].reject(new MarketDataError('rate-limited', 'slow down', 429, 120))
    await vi.advanceTimersByTimeAsync(119_999)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(2)
  })

  it.each(['banned', 'bad-symbol', 'region-blocked'] as const)(
    'does not retry a %s error before the next refresh',
    async (kind) => {
      const { provider, calls } = pendingProvider()
      const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
      calls[0].reject(new MarketDataError(kind, 'no'))
      await settle()
      hook.flush()
      expect(hook.result.error).toBe('no')
      await vi.advanceTimersByTimeAsync(TO_REFRESH - 1)
      expect(calls).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(1)
      expect(calls).toHaveLength(2)
    },
  )

  it('keeps the same array when a refresh brings the same closed candles', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    // The new day is late; the answer repeats the closed days in a new array.
    calls[1].resolve(withoutNewDay())
    await settle()
    hook.flush()
    expect(hook.result.candles).toBe(before)
  })

  it('takes a corrected closed candle as a new array', async () => {
    const { calls, hook } = await loaded()
    const before = hook.result.candles
    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    const corrected = withoutNewDay()
    corrected[corrected.length - 1] = candle(D10 - DAY, 123)
    calls[1].resolve([...corrected, candle(D10)])
    await settle()
    hook.flush()
    expect(hook.result.candles).not.toBe(before)
    expect(hook.result.candles?.at(-1)?.close).toBe(123)
  })

  it('reports an empty list, not null, when the venue answers with no closed day', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'NEWUSDT', enabled: true })
    calls[0].resolve([candle(D10)])
    await settle()
    hook.flush()
    expect(hook.result).toEqual({ candles: [], error: null, loading: false })
  })

  it('aborts and clears timers on unmount, and ignores a late answer', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(useDaily, { provider, symbol: 'BTCUSDT', enabled: true })
    hook.unmount()
    expect(calls[0].signal?.aborted).toBe(true)
    calls[0].resolve(days(D10, 60))
    await settle()
    expect(vi.getTimerCount()).toBe(0)

    const second = await loaded()
    expect(vi.getTimerCount()).toBe(1)
    second.hook.unmount()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(2 * DAY_MS)
    expect(second.calls).toHaveLength(1)
  })

  it('clears the timer on a symbol change and on disable', async () => {
    const { provider, calls, hook } = await loaded()
    expect(vi.getTimerCount()).toBe(1)
    hook.rerender({ provider, symbol: 'ETHUSDT', enabled: true })
    expect(vi.getTimerCount()).toBe(0)
    calls[1].resolve(days(D10, 60))
    await settle()
    expect(vi.getTimerCount()).toBe(1)
    hook.rerender({ provider, symbol: 'ETHUSDT', enabled: false })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaves one request and one timer under StrictMode’s double effects', async () => {
    const { provider, calls } = pendingProvider()
    const hook = mountHook(
      useDaily,
      { provider, symbol: 'BTCUSDT', enabled: true },
      { strict: true },
    )
    expect(calls).toHaveLength(2)
    expect(calls[0].signal?.aborted).toBe(true)
    expect(calls[1].signal?.aborted).toBe(false)

    calls[0].resolve(days(D10, 60))
    calls[1].resolve(days(D10, 60))
    await settle()
    hook.flush()
    expect(hook.result.candles).toHaveLength(59)
    expect(vi.getTimerCount()).toBe(1)

    await vi.advanceTimersByTimeAsync(TO_REFRESH)
    expect(calls).toHaveLength(3)
    expect(vi.getTimerCount()).toBe(0)
  })
})
