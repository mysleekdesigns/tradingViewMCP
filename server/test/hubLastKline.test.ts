import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createHub,
  DEFAULT_RELEASE_GRACE_MS,
  klineKey,
  TICKERS_KEY,
  type Hub,
} from '../src/upstream/hub.ts'

/** Fake upstream WebSocket: the test drives open/message/close. */
class FakeWS {
  static instances: FakeWS[] = []
  readonly url: string
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeWS.instances.push(this)
  }
  send(): void {}
  close(): void {}
  emitOpen(): void {
    this.onopen?.({})
  }
  emitMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) })
  }
  emitClose(): void {
    this.onclose?.({})
  }
}

const klineMsg = (tMs: number, c: string, x = false) => ({
  e: 'kline',
  E: tMs + 1,
  s: 'BTCUSDT',
  k: { t: tMs, s: 'BTCUSDT', i: '1m', o: '1', h: '2', l: '0.5', c, v: '10', x },
})

describe('hub.lastKline', () => {
  const clock = { ms: 1_700_000_130_000 }
  const BTC = klineKey('BTCUSDT', '1m')
  let hub: Hub

  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    hub = createHub({
      WebSocketImpl: FakeWS as unknown as typeof WebSocket,
      wsBase: 'wss://up.test',
      stats: { upstreamSockets: 0 },
      random: () => 0.5,
      now: () => clock.ms,
    })
  })

  afterEach(() => {
    hub.close()
    vi.useRealTimers()
  })

  it('is undefined for a key that is not held', () => {
    expect(hub.lastKline(BTC)).toBeUndefined()
  })

  it('is undefined while live but before the first kline push', () => {
    hub.acquire(BTC, () => {})
    FakeWS.instances[0]!.emitOpen()
    expect(hub.status(BTC)).toBe('live')
    expect(hub.lastKline(BTC)).toBeUndefined()
  })

  it('returns the newest push with its closed flag and local receive time', () => {
    hub.acquire(BTC, () => {})
    const ws = FakeWS.instances[0]!
    ws.emitOpen()
    ws.emitMessage(klineMsg(1_700_000_100_000, '1.5'))
    clock.ms += 700
    ws.emitMessage(klineMsg(1_700_000_100_000, '1.75', true))
    expect(hub.lastKline(BTC)).toEqual({
      candle: { time: 1_700_000_100, open: 1, high: 2, low: 0.5, close: 1.75, volume: 10 },
      closed: true,
      receivedAtMs: clock.ms,
    })
  })

  it('returns a copy the caller may change', () => {
    hub.acquire(BTC, () => {})
    const ws = FakeWS.instances[0]!
    ws.emitOpen()
    ws.emitMessage(klineMsg(1_700_000_100_000, '1.5'))
    const a = hub.lastKline(BTC)!
    a.candle.close = 99
    expect(hub.lastKline(BTC)?.candle.close).toBe(1.5)
  })

  it('is cleared when the socket drops and stays empty on the new socket until it pushes', () => {
    hub.acquire(BTC, () => {})
    const first = FakeWS.instances[0]!
    first.emitOpen()
    first.emitMessage(klineMsg(1_700_000_100_000, '1.5'))
    first.emitClose()
    expect(hub.status(BTC)).toBe('reconnecting')
    expect(hub.lastKline(BTC)).toBeUndefined()
    vi.advanceTimersByTime(1_000)
    const second = FakeWS.instances[1]!
    second.emitOpen()
    expect(hub.status(BTC)).toBe('live')
    expect(hub.lastKline(BTC)).toBeUndefined()
    second.emitMessage(klineMsg(1_700_000_160_000, '2'))
    expect(hub.lastKline(BTC)?.candle.time).toBe(1_700_000_160)
  })

  it('is kept through the release grace period and gone after teardown', () => {
    const release = hub.acquire(BTC, () => {})
    const ws = FakeWS.instances[0]!
    ws.emitOpen()
    ws.emitMessage(klineMsg(1_700_000_100_000, '1.5'))
    release()
    // No listener, socket kept: pushes during the grace period still update it.
    ws.emitMessage(klineMsg(1_700_000_100_000, '1.6'))
    expect(hub.lastKline(BTC)?.candle.close).toBe(1.6)
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS + 1)
    expect(hub.lastKline(BTC)).toBeUndefined()
  })

  it('is undefined for the tickers stream', () => {
    hub.acquire(TICKERS_KEY, () => {})
    FakeWS.instances[0]!.emitOpen()
    expect(hub.lastKline(TICKERS_KEY)).toBeUndefined()
  })
})
