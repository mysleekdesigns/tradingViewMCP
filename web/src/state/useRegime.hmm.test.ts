// Phase 20: the ribbon's HMM source in useRegime, driven through the fake hook runtime.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RegimeHmmInfo, RegimeModel, RegimeRequest, RegimeResponse } from '../data/labTypes'
import type { LabRegimeClient } from '../data/labApi'
import { LabError } from '../data/labApi'
import type { Candle, Timeframe } from '../types'
import { fakeReactHooks, mountHook } from './hookHarness.testkit'
import {
  HMM_BELOW_1H_TEXT,
  HMM_OTHER_TIMEFRAME_TEXT,
  REGIME_DEBOUNCE_MS,
  REGIME_HMM_MAX_WAIT_MS,
  REGIME_HMM_RETRY_MS,
  REGIME_MAX_GAP_MS,
  hmmHistoryShort,
  hmmRibbonOff,
  hmmUnavailableReason,
  regimeModelFor,
  regimeRetryable,
  useRegime,
  type UseRegimeOptions,
} from './useRegime'

vi.mock('react', async (orig) => ({ ...(await orig<typeof import('react')>()), ...fakeReactHooks }))

/** Requests one key can make when every answer fails at once: the first, then one per wait. */
const MAX_ATTEMPTS = REGIME_HMM_MAX_WAIT_MS / REGIME_HMM_RETRY_MS + 1

const T0 = 1_700_006_400
const H4 = 14_400

function bars(n: number): Candle[] {
  return Array.from({ length: n }, (_, i) => ({
    time: T0 + i * H4,
    open: 100,
    high: 101,
    low: 99,
    close: 100 + i,
    volume: 1,
  }))
}

const HMM: RegimeHmmInfo = {
  n_states: 2,
  warmup_bars: 3,
  bars_needed: 3043,
  bars_have: 4000,
  unavailable: null,
  bear_is_high_vol: null,
}
const BLOCKED = 'HMM needs Binance global data, this chart shows Coinbase'
const BUSY = new LabError('busy', 'another HMM fit is running', 503)
const TIMEOUT = new LabError('timeout', 'The lab service did not answer in time')

function hmmAnswer(): RegimeResponse {
  return {
    points: [{ time: T0, state: 1, prob: 0.9 }],
    window_bars: 0,
    threshold: 0,
    engine: 'lab.registry.hmm.causal_hmm',
    hmm: HMM,
  }
}

const MARKOV_ANSWER: RegimeResponse = {
  points: [{ time: T0, state: -1 }],
  window_bars: 20,
  threshold: 0.05,
  engine: 'hedgefund.regime.labels.label_regimes',
}

interface Pending {
  req: RegimeRequest
  signal?: AbortSignal
  resolve: (r: RegimeResponse) => void
  reject: (e: unknown) => void
}

function fakeClient(): { client: LabRegimeClient; pending: Pending[] } {
  const pending: Pending[] = []
  return {
    pending,
    client: {
      regime: (req, signal) =>
        new Promise((resolve, reject) => pending.push({ req, signal, resolve, reject })),
    },
  }
}

function options(over: Partial<UseRegimeOptions> = {}): UseRegimeOptions {
  return {
    enabled: true,
    symbol: 'BTCUSDT',
    timeframe: '4h',
    source: 'hmm',
    candles: bars(4),
    windowBars: 20,
    threshold: 0.05,
    barSeconds: H4,
    ...over,
  }
}

function mount(client: LabRegimeClient, over: Partial<UseRegimeOptions> = {}) {
  return mountHook((o: UseRegimeOptions) => useRegime(client, o), options(over))
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('hmmUnavailableReason', () => {
  it('refuses everything below 1h with the plain reason', () => {
    for (const tf of ['1s', '30s', '1m', '15m', '30m', '59m'] as Timeframe[]) {
      expect(hmmUnavailableReason(tf)).toBe(HMM_BELOW_1H_TEXT)
    }
  })

  it('allows the lab intervals from 1h to 1d', () => {
    for (const tf of ['1h', '2h', '4h', '6h', '8h', '12h', '1d'] as Timeframe[]) {
      expect(hmmUnavailableReason(tf)).toBeNull()
    }
  })

  it('refuses timeframes the lab has no interval for', () => {
    for (const tf of ['90m', '3h', '3d', '1w'] as Timeframe[]) {
      expect(hmmUnavailableReason(tf)).toBe(HMM_OTHER_TIMEFRAME_TEXT)
    }
  })
})

describe('regimeModelFor', () => {
  it('runs the saved source where it can and Markov where it cannot', () => {
    expect(regimeModelFor('hmm', '4h')).toBe('hmm')
    expect(regimeModelFor('hmm', '15m')).toBe('markov')
    expect(regimeModelFor('markov', '4h')).toBe('markov')
  })

  it('falls back to Markov on a blocked venue, and the reason is the venue one', () => {
    expect(regimeModelFor('hmm', '4h', BLOCKED)).toBe('markov')
    expect(regimeModelFor('hmm', '4h', null)).toBe('hmm')
    expect(hmmUnavailableReason('4h', BLOCKED)).toBe(BLOCKED)
    expect(hmmUnavailableReason('15m', BLOCKED)).toBe(BLOCKED)
    expect(hmmUnavailableReason('4h', null)).toBeNull()
  })
})

describe('regimeRetryable', () => {
  it('asks again after a timeout or a busy lab, and after nothing else', () => {
    expect(regimeRetryable(TIMEOUT)).toBe(true)
    expect(regimeRetryable(BUSY)).toBe(true)
    expect(regimeRetryable(new LabError('no-data', 'nope', 503))).toBe(false)
    expect(regimeRetryable(new LabError('offline', 'down'))).toBe(false)
    expect(regimeRetryable(new Error('boom'))).toBe(false)
  })
})

describe('hmmHistoryShort', () => {
  it('is true only with an HMM answer below the bars needed', () => {
    expect(hmmHistoryShort(null)).toBe(false)
    expect(hmmHistoryShort(HMM)).toBe(false)
    expect(hmmHistoryShort({ ...HMM, bars_have: 3043 })).toBe(false)
    expect(hmmHistoryShort({ ...HMM, bars_have: 3042 })).toBe(true)
  })
})

describe('hmmRibbonOff', () => {
  it('holds for too little history and for nothing fitted, not for a normal answer', () => {
    expect(hmmRibbonOff(null)).toBe(false)
    expect(hmmRibbonOff(HMM)).toBe(false)
    expect(hmmRibbonOff({ ...HMM, bars_have: 3042 })).toBe(true)
    expect(hmmRibbonOff({ ...HMM, unavailable: 'no archive' })).toBe(true)
  })
})

describe('useRegime on the HMM source', () => {
  it('sends model, symbol and interval with the closed bars, and no Markov parameters', () => {
    const { client, pending } = fakeClient()
    mount(client)
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    expect(pending).toHaveLength(1)
    expect(pending[0].req).toEqual({
      times: [T0, T0 + H4, T0 + 2 * H4],
      closes: [100, 101, 102],
      model: 'hmm',
      symbol: 'BTCUSDT',
      interval: '4h',
    })
  })

  it('makes no HMM request below 1h: the Markov ribbon runs there', () => {
    const { client, pending } = fakeClient()
    mount(client, { timeframe: '15m', barSeconds: 900 })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    expect(pending).toHaveLength(1)
    expect(pending[0].req.model).toBeUndefined()
    expect(pending[0].req.window_bars).toBe(20)
    expect(pending[0].req.symbol).toBeUndefined()
  })

  it('makes no HMM request on a blocked venue: the Markov ribbon runs there', () => {
    const { client, pending } = fakeClient()
    mount(client, { hmmBlocked: BLOCKED })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    expect(pending).toHaveLength(1)
    expect(pending[0].req.model).toBeUndefined()
  })

  it('leaves a Markov request exactly as it was', () => {
    const { client, pending } = fakeClient()
    mount(client, { source: 'markov' })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    expect(pending[0].req).toEqual({
      times: [T0, T0 + H4, T0 + 2 * H4],
      closes: [100, 101, 102],
      window_bars: 20,
      threshold: 0.05,
    })
  })

  it('stays loading with no points until the fit answers, then exposes points and fit info', async () => {
    const { client, pending } = fakeClient()
    const h = mount(client)
    expect(h.result).toMatchObject({ points: null, hmm: null, loading: true, error: null })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    pending[0].resolve(hmmAnswer())
    await vi.advanceTimersByTimeAsync(0)
    h.flush()
    expect(h.result).toMatchObject({ loading: false, error: null, hmm: HMM })
    expect(h.result.points).toEqual(hmmAnswer().points)
  })

  it('ends the loading state with the message when the fit fails', async () => {
    const { client, pending } = fakeClient()
    const h = mount(client)
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    pending[0].reject(new LabError('no-data', 'HMM fit failed: every restart failed', 503))
    await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS * 2)
    h.flush()
    expect(pending).toHaveLength(1)
    expect(h.result).toMatchObject({
      loading: false,
      points: null,
      error: 'HMM fit failed: every restart failed',
    })
  })

  describe.each([
    ['a timeout', TIMEOUT],
    ['a busy lab', BUSY],
  ])('after %s', (_name, failure) => {
    /** Fail every pending request, one attempt per retry wait, up to `attempts` in total. */
    async function failAttempts(pending: Pending[], attempts: number) {
      for (let i = 0; i < attempts; i++) {
        pending[i].reject(failure)
        await vi.advanceTimersByTimeAsync(0)
        if (i < attempts - 1) await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS)
      }
    }

    it('keeps saying it is loading, with no error, and asks again after the wait', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      pending[0].reject(failure)
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS - 1)
      h.flush()
      expect(pending).toHaveLength(1)
      expect(h.result).toMatchObject({ loading: true, error: null, points: null })
      await vi.advanceTimersByTimeAsync(1)
      expect(pending).toHaveLength(2)
      expect(pending[1].req).toEqual(pending[0].req)
    })

    it('takes the answer of a later attempt', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      await failAttempts(pending, 2)
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS)
      pending[2].resolve(hmmAnswer())
      await vi.advanceTimersByTimeAsync(0)
      h.flush()
      expect(h.result).toMatchObject({ loading: false, error: null, hmm: HMM })
    })

    it('outlasts a fit far longer than six attempts would have', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      await failAttempts(pending, 24) // 23 waits of 20 s: 460 s, a cold 1h fit
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS)
      h.flush()
      expect(h.result).toMatchObject({ loading: true, error: null })
      pending[24].resolve(hmmAnswer())
      await vi.advanceTimersByTimeAsync(0)
      h.flush()
      expect(h.result).toMatchObject({ loading: false, error: null, hmm: HMM })
    })

    it('gives up when the time budget runs out and shows the error', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      await failAttempts(pending, MAX_ATTEMPTS - 1)
      h.flush()
      expect(h.result.error).toBeNull()
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS)
      expect(pending).toHaveLength(MAX_ATTEMPTS)
      pending[MAX_ATTEMPTS - 1].reject(failure)
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS * 3)
      h.flush()
      expect(pending).toHaveLength(MAX_ATTEMPTS)
      expect(h.result).toMatchObject({ loading: false, error: failure.message })
    })

    it('starts the count again on a new key', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      await failAttempts(pending, MAX_ATTEMPTS - 1)
      h.rerender(options({ candles: bars(5) }))
      await vi.advanceTimersByTimeAsync(REGIME_MAX_GAP_MS)
      const first = pending.length - 1
      for (let i = 0; i < MAX_ATTEMPTS - 1; i++) {
        expect(pending).toHaveLength(first + i + 1)
        pending[first + i].reject(failure)
        await vi.advanceTimersByTimeAsync(0)
        await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS)
      }
      h.flush()
      expect(pending).toHaveLength(first + MAX_ATTEMPTS)
      expect(h.result.error).toBeNull()
    })

    it('drops a waiting retry when the source changes', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client)
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      pending[0].reject(failure)
      await vi.advanceTimersByTimeAsync(0)
      h.rerender(options({ source: 'markov' }))
      await vi.advanceTimersByTimeAsync(REGIME_DEBOUNCE_MS)
      expect(pending).toHaveLength(2)
      expect(pending[1].req.model).toBeUndefined()
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS * 2)
      expect(pending).toHaveLength(2)
    })

    it('never retries a Markov request', async () => {
      const { client, pending } = fakeClient()
      const h = mount(client, { source: 'markov' })
      vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
      pending[0].reject(failure)
      await vi.advanceTimersByTimeAsync(REGIME_HMM_RETRY_MS * 3)
      h.flush()
      expect(pending).toHaveLength(1)
      expect(h.result).toMatchObject({ loading: false, error: failure.message })
    })
  })

  it('never shows the other model labels after switching source, in either direction', async () => {
    const { client, pending } = fakeClient()
    const h = mount(client, { source: 'markov' })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    pending[0].resolve(MARKOV_ANSWER)
    await vi.advanceTimersByTimeAsync(0)
    h.flush()
    expect(h.result.points).toEqual(MARKOV_ANSWER.points)

    const toHmm: RegimeModel = 'hmm'
    h.rerender(options({ source: toHmm }))
    expect(h.result).toMatchObject({ points: null, hmm: null, loading: true })
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    pending[1].resolve(hmmAnswer())
    await vi.advanceTimersByTimeAsync(0)
    h.flush()
    expect(h.result.hmm).toEqual(HMM)

    h.rerender(options({ source: 'markov' }))
    expect(h.result).toMatchObject({ points: null, hmm: null, loading: true })
  })

  it('aborts the running fit when a newer request replaces it, and ignores its late answer', async () => {
    const { client, pending } = fakeClient()
    const h = mount(client)
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    h.rerender(options({ candles: bars(5) }))
    expect(pending[0].signal?.aborted).toBe(true)
    pending[0].resolve(hmmAnswer())
    await vi.advanceTimersByTimeAsync(0)
    h.flush()
    expect(h.result.points).toBeNull()
    // A bar closed: the replacement waits out the bar spacing, then runs.
    vi.advanceTimersByTime(REGIME_MAX_GAP_MS)
    expect(pending).toHaveLength(2)
    expect(pending[1].signal?.aborted).toBe(false)
  })

  it('keeps earlier HMM points and fit info while a refresh runs or after it fails', async () => {
    const { client, pending } = fakeClient()
    const h = mount(client)
    vi.advanceTimersByTime(REGIME_DEBOUNCE_MS)
    pending[0].resolve(hmmAnswer())
    await vi.advanceTimersByTimeAsync(0)
    h.flush()

    h.rerender(options({ candles: bars(5) }))
    expect(h.result).toMatchObject({ hmm: HMM, points: hmmAnswer().points, loading: true })
    await vi.advanceTimersByTimeAsync(REGIME_MAX_GAP_MS)
    pending[1].reject(new LabError('offline', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    h.flush()
    expect(h.result).toMatchObject({ hmm: HMM, points: hmmAnswer().points, error: 'down' })
  })
})
