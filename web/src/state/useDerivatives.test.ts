import { describe, expect, it } from 'vitest'
import type { DerivativesResponse } from '../data/labTypes'
import type { Candle } from '../types'
import {
  DERIVATIVES_PREF_KEY,
  derivativesVenueBlocked,
  loadDerivativesOn,
  saveDerivativesOn,
  type DerivativesStorage,
} from './derivativesPref'
import {
  DERIVATIVES_RETRY_MS,
  MAX_DERIVATIVES_BARS,
  derivativesArchiveEnd,
  derivativesCovers,
  derivativesRequestKey,
  derivativesRequestTimes,
  derivativesRetryDelay,
  derivativesTrigger,
  derivativesUnsupportedReason,
  derivativesWindow,
  isStickyDerivativesError,
  newestBeyondArchive,
  quotedCommand,
  type DerivativesCoverage,
} from './useDerivatives'

const H = 3600
const T0 = 1_704_067_200

function bars(n: number, start = T0, step = H): Candle[] {
  return Array.from({ length: n }, (_, i) => ({
    time: start + i * step,
    open: 1,
    high: 1,
    low: 1,
    close: 1,
    volume: 1,
  }))
}

describe('derivativesUnsupportedReason', () => {
  it('is on for minute to weekly bars on Binance symbols', () => {
    for (const tf of ['1m', '5m', '1h', '4h', '1d', '3d', '1w'] as const) {
      expect(derivativesUnsupportedReason(tf, 'BTCUSDT')).toBeNull()
    }
  })

  it('is off on seconds charts and on bars longer than a week', () => {
    expect(derivativesUnsupportedReason('1s', 'BTCUSDT')).toMatch(/seconds/)
    expect(derivativesUnsupportedReason('30s', 'BTCUSDT')).toMatch(/seconds/)
    expect(derivativesUnsupportedReason('2w', 'BTCUSDT')).toMatch(/1W/)
  })

  it('is off for symbols that are not Binance-style (e.g. Coinbase BTC-USD)', () => {
    expect(derivativesUnsupportedReason('1h', 'BTC-USD')).toMatch(/Binance symbols/)
    expect(derivativesUnsupportedReason('1h', 'BTC/USDT')).toMatch(/Binance symbols/)
    expect(derivativesUnsupportedReason('1h', 'btcusdt')).toMatch(/Binance symbols/)
  })
})

describe('closed bars sent to the lab', () => {
  it('never sends the forming last bar', () => {
    const c = bars(3)
    expect(derivativesRequestTimes(c, true)).toEqual([T0, T0 + H])
    expect(derivativesWindow(c, true)).toEqual({ first: T0, newest: T0 + H })
    // Replay: every bar is closed.
    expect(derivativesRequestTimes(c, false)).toEqual([T0, T0 + H, T0 + 2 * H])
    expect(derivativesWindow(c, false)).toEqual({ first: T0, newest: T0 + 2 * H })
  })

  it('has nothing to send while only the forming bar exists', () => {
    expect(derivativesWindow(bars(1), true)).toBeNull()
    expect(derivativesRequestTimes(bars(1), true)).toEqual([])
    expect(derivativesWindow([], false)).toBeNull()
  })

  it('sends at most the newest MAX_DERIVATIVES_BARS closed bars (the regime cap)', () => {
    expect(MAX_DERIVATIVES_BARS).toBe(20_000)
    const c = bars(MAX_DERIVATIVES_BARS + 6) // 5 extra closed bars + the forming one
    const times = derivativesRequestTimes(c, true)
    expect(times).toHaveLength(MAX_DERIVATIVES_BARS)
    expect(times[0]).toBe(c[5].time)
    expect(times.at(-1)).toBe(c[c.length - 2].time)
    expect(derivativesWindow(c, true)).toEqual({ first: c[5].time, newest: c[c.length - 2].time })
  })
})

function response(
  last: { f: number | null; o: number | null },
  available: { f: number | null; o: number | null },
  time = T0 + 10 * H,
): DerivativesResponse {
  return {
    symbol: 'BTCUSDT',
    bar_seconds: H,
    funding: [
      { time: T0, value: 0.0001 },
      { time, value: last.f },
    ],
    open_interest: [
      { time: T0, value: 1e9 },
      { time, value: last.o },
    ],
    available: {
      funding: { first: T0 - 100 * H, last: available.f },
      open_interest: { first: available.o === null ? null : T0 - 100 * H, last: available.o },
    },
    source: '',
  }
}

describe('newestBeyondArchive', () => {
  it('is true when the newest bar is null in both series and after both archive ends', () => {
    expect(newestBeyondArchive(response({ f: null, o: null }, { f: T0, o: T0 + H }))).toBe(true)
  })

  it('counts a series with nothing downloaded as ended', () => {
    expect(newestBeyondArchive(response({ f: null, o: null }, { f: T0, o: null }))).toBe(true)
  })

  it('is false while either series still has a value at the newest bar', () => {
    expect(newestBeyondArchive(response({ f: 0.0001, o: null }, { f: T0, o: T0 }))).toBe(false)
    expect(newestBeyondArchive(response({ f: null, o: 5e9 }, { f: T0, o: T0 }))).toBe(false)
  })

  it('is false for a gap inside the archive range (later bars may have data)', () => {
    const inside = T0 + 10 * H
    expect(
      newestBeyondArchive(response({ f: null, o: null }, { f: inside + H, o: inside + H })),
    ).toBe(false)
    // Exactly at the archive end is not past it.
    expect(newestBeyondArchive(response({ f: null, o: null }, { f: inside, o: T0 }))).toBe(false)
  })

  it('is false for an empty answer', () => {
    const empty = { ...response({ f: null, o: null }, { f: null, o: null }) }
    empty.funding = []
    empty.open_interest = []
    expect(newestBeyondArchive(empty)).toBe(false)
  })
})

describe('derivativesCovers (the refetch rule)', () => {
  const cov = (over: Partial<DerivativesCoverage> = {}): DerivativesCoverage => ({
    first: T0,
    newest: T0 + 10 * H,
    beyondArchive: false,
    sticky: false,
    failed: false,
    ...over,
  })

  it('needs a first request', () => {
    expect(derivativesCovers(null, { first: T0, newest: T0 })).toBe(false)
  })

  it('covers live ticks (same closed bars) and a replay step back', () => {
    expect(derivativesCovers(cov(), { first: T0, newest: T0 + 10 * H })).toBe(true)
    expect(derivativesCovers(cov(), { first: T0, newest: T0 + 4 * H })).toBe(true)
  })

  it('asks again when older history is prepended, whatever else holds', () => {
    const win = { first: T0 - 500 * H, newest: T0 + 10 * H }
    expect(derivativesCovers(cov(), win)).toBe(false)
    expect(derivativesCovers(cov({ beyondArchive: true }), win)).toBe(false)
    expect(derivativesCovers(cov({ sticky: true }), win)).toBe(false)
  })

  it('asks again on a bar close while the archive still reached the newest bar sent', () => {
    expect(derivativesCovers(cov(), { first: T0, newest: T0 + 11 * H })).toBe(false)
  })

  it('does not ask again for new bars once the newest bar sent was past the archive', () => {
    const c = cov({ beyondArchive: true })
    expect(derivativesCovers(c, { first: T0, newest: T0 + 11 * H })).toBe(true)
    expect(derivativesCovers(c, { first: T0, newest: T0 + 500 * H })).toBe(true)
    // The 20 000-bar cap slides forward as bars close: still covered.
    expect(derivativesCovers(c, { first: T0 + H, newest: T0 + 11 * H })).toBe(true)
  })

  it('does not retry a no-data / invalid answer on every bar close', () => {
    expect(derivativesCovers(cov({ sticky: true }), { first: T0, newest: T0 + 11 * H })).toBe(true)
    expect(isStickyDerivativesError('no-data')).toBe(true)
    expect(isStickyDerivativesError('invalid')).toBe(true)
    expect(isStickyDerivativesError('offline')).toBe(false)
    expect(isStickyDerivativesError('timeout')).toBe(false)
    expect(isStickyDerivativesError(null)).toBe(false)
  })

  it('never counts a transient failure (offline, timeout, http, parse) as covering', () => {
    const failed = cov({ failed: true })
    // Not even the exact window it asked for, nor a replay step back inside it.
    expect(derivativesCovers(failed, { first: T0, newest: T0 + 10 * H })).toBe(false)
    expect(derivativesCovers(failed, { first: T0, newest: T0 + 4 * H })).toBe(false)
  })

  it('keys a request by market and closed-bar window', () => {
    expect(derivativesRequestKey('BTCUSDT|1h|3600', { first: 1, newest: 2 })).toBe(
      'BTCUSDT|1h|3600|1|2',
    )
  })
})

describe('chip helpers', () => {
  it('pulls the CLI out of a 503 detail', () => {
    expect(
      quotedCommand(
        'No derivatives archive for BTCUSDT. Run `uv run python -m lab.data.derivatives BTCUSDT` in lab/.',
      ),
    ).toBe('uv run python -m lab.data.derivatives BTCUSDT')
    expect(quotedCommand('nothing quoted')).toBeNull()
  })

  it('reports the newest archive end over both series', () => {
    expect(derivativesArchiveEnd(null)).toBeNull()
    expect(
      derivativesArchiveEnd({
        funding: { first: 1, last: 50 },
        open_interest: { first: 2, last: 70 },
      }),
    ).toBe(70)
    expect(
      derivativesArchiveEnd({
        funding: { first: 1, last: 50 },
        open_interest: { first: null, last: null },
      }),
    ).toBe(50)
    expect(
      derivativesArchiveEnd({
        funding: { first: null, last: null },
        open_interest: { first: null, last: null },
      }),
    ).toBeNull()
  })
})

describe('derivativesPref', () => {
  function memory(): DerivativesStorage & { data: Map<string, string> } {
    const data = new Map<string, string>()
    return {
      data,
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => {
        data.set(k, v)
      },
    }
  }

  it('is off by default and remembers the switch', () => {
    const s = memory()
    expect(loadDerivativesOn(s)).toBe(false)
    saveDerivativesOn(true, s)
    expect(s.data.get(DERIVATIVES_PREF_KEY)).toBe('true')
    expect(loadDerivativesOn(s)).toBe(true)
    saveDerivativesOn(false, s)
    expect(loadDerivativesOn(s)).toBe(false)
  })

  it('reads garbage or a throwing storage as off and never throws on save', () => {
    const s = memory()
    s.data.set(DERIVATIVES_PREF_KEY, '{nope')
    expect(loadDerivativesOn(s)).toBe(false)
    const broken: DerivativesStorage = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }
    expect(loadDerivativesOn(broken)).toBe(false)
    expect(() => saveDerivativesOn(true, broken)).not.toThrow()
    expect(loadDerivativesOn(null)).toBe(false)
  })

  it('allows only Binance global spot charts', () => {
    expect(derivativesVenueBlocked({ id: 'binance', name: 'Binance' })).toBeNull()
    expect(derivativesVenueBlocked({ id: 'coinbase', name: 'Coinbase' })).toMatch(/Coinbase/)
    expect(derivativesVenueBlocked({ id: 'binance-us', name: 'Binance.US' })).toMatch(
      /Binance charts only/,
    )
    expect(derivativesVenueBlocked(null)).toMatch(/waiting/)
  })
})

describe('derivativesTrigger (what the request effect re-runs on)', () => {
  const SERIES = 'BTCUSDT|1h|3600'
  // Live chart: bars T0 .. T0+11H, the last one forming, so T0 .. T0+10H were sent.
  const liveCandles = bars(12)
  const liveWin = derivativesWindow(liveCandles, true)
  const liveKey = derivativesTrigger(SERIES, liveWin, null)
  const answered = (over: Partial<DerivativesCoverage & { requestKey: string }> = {}) => ({
    first: T0,
    newest: T0 + 10 * H,
    beyondArchive: false,
    sticky: false,
    failed: false,
    requestKey: liveKey ?? '',
    ...over,
  })

  it('is null (nothing to send) while off or with no closed bar', () => {
    expect(derivativesTrigger(SERIES, null, answered())).toBeNull()
    expect(derivativesTrigger(SERIES, derivativesWindow(bars(1), true), null)).toBeNull()
  })

  it('asks first for the live closed bars', () => {
    expect(liveKey).toBe(derivativesRequestKey(SERIES, { first: T0, newest: T0 + 10 * H }))
  })

  it('keeps the trigger on live ticks (in-flight request not aborted)', () => {
    const tick = liveCandles.map((c, i) => (i === 11 ? { ...c, close: 2 } : c))
    expect(derivativesTrigger(SERIES, derivativesWindow(tick, true), answered())).toBe(liveKey)
  })

  it('entering replay inside the live answer needs no request', () => {
    // Replay at bar 5: bars T0 .. T0+5H, every one closed (formingLast flips to false).
    const replay = bars(6)
    expect(derivativesTrigger(SERIES, derivativesWindow(replay, false), answered())).toBe(liveKey)
  })

  it('stepping forward through replay inside the live answer needs no request', () => {
    for (let n = 6; n <= 11; n++) {
      expect(derivativesTrigger(SERIES, derivativesWindow(bars(n), false), answered())).toBe(
        liveKey,
      )
    }
  })

  it('a step past the newest bar answered changes the trigger (older request aborted)', () => {
    const next = derivativesTrigger(SERIES, derivativesWindow(bars(12), false), answered())
    expect(next).not.toBe(liveKey)
    expect(next).toBe(derivativesRequestKey(SERIES, { first: T0, newest: T0 + 11 * H }))
  })

  it('a bar close on the live chart changes the trigger', () => {
    const next = derivativesTrigger(SERIES, derivativesWindow(bars(13), true), answered())
    expect(next).toBe(derivativesRequestKey(SERIES, { first: T0, newest: T0 + 11 * H }))
  })

  it('after a transient failure the trigger is the window itself, so retries stay mounted', () => {
    const failedKey = derivativesRequestKey(SERIES, { first: T0, newest: T0 + 10 * H })
    const failed = answered({ failed: true, requestKey: failedKey })
    // Same closed bars: same trigger, the effect keeps its retry timer.
    expect(derivativesTrigger(SERIES, liveWin, failed)).toBe(failedKey)
    // Replay inside the failed window is not covered: it asks for its own window.
    expect(derivativesTrigger(SERIES, derivativesWindow(bars(6), false), failed)).toBe(
      derivativesRequestKey(SERIES, { first: T0, newest: T0 + 5 * H }),
    )
  })

  it('a sticky error keeps covering new bar closes (no request per bar)', () => {
    const sticky = answered({ sticky: true })
    expect(derivativesTrigger(SERIES, derivativesWindow(bars(20), true), sticky)).toBe(liveKey)
  })
})

describe('derivativesRetryDelay (transient failures)', () => {
  it('backs off 15 s, 30 s, 60 s, then stops', () => {
    expect(DERIVATIVES_RETRY_MS).toEqual([15_000, 30_000, 60_000])
    expect([1, 2, 3, 4, 5].map(derivativesRetryDelay)).toEqual([15_000, 30_000, 60_000, null, null])
  })

  it('has no delay before a failure or for a nonsense count', () => {
    expect(derivativesRetryDelay(0)).toBeNull()
    expect(derivativesRetryDelay(-1)).toBeNull()
    expect(derivativesRetryDelay(1.5)).toBeNull()
  })

  it('never retries a sticky error', () => {
    // The hook schedules retries only for non-sticky kinds.
    for (const kind of ['offline', 'timeout', 'http', 'parse'] as const) {
      expect(isStickyDerivativesError(kind)).toBe(false)
    }
    expect(isStickyDerivativesError('no-data')).toBe(true)
    expect(isStickyDerivativesError('invalid')).toBe(true)
  })
})
