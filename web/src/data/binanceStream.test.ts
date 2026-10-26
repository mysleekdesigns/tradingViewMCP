import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ConnectionStatus } from '../types'
import {
  backoffDelay,
  openReconnectingSocket,
  parseKlineMessage,
  type SocketControl,
} from './binanceStream'

function klineMsg(overrides: Record<string, unknown> = {}, top: Record<string, unknown> = {}) {
  return {
    e: 'kline',
    E: 1700000012345,
    s: 'BTCUSDT',
    k: {
      t: 1700000040000,
      T: 1700000099999,
      s: 'BTCUSDT',
      i: '1m',
      o: '37000.10',
      c: '37050.25',
      h: '37100.50',
      l: '36950.00',
      v: '12.5',
      n: 42,
      x: false,
      q: '463000.1',
      ...overrides,
    },
    ...top,
  }
}

describe('parseKlineMessage', () => {
  it('parses a forming kline from a JSON string, ms -> s, strings -> numbers', () => {
    const out = parseKlineMessage(JSON.stringify(klineMsg()))
    expect(out).toEqual({
      candle: {
        time: 1700000040,
        open: 37000.1,
        high: 37100.5,
        low: 36950,
        close: 37050.25,
        volume: 12.5,
      },
      closed: false,
    })
  })

  it('marks closed only when k.x === true', () => {
    expect(parseKlineMessage(klineMsg({ x: true }))?.closed).toBe(true)
    expect(parseKlineMessage(klineMsg({ x: 'true' }))?.closed).toBe(false)
    expect(parseKlineMessage(klineMsg({ x: 1 }))?.closed).toBe(false)
  })

  it('floors a non-whole-second open time', () => {
    expect(parseKlineMessage(klineMsg({ t: 1700000040999 }))?.candle.time).toBe(1700000040)
  })

  it('unwraps the combined-stream envelope', () => {
    const out = parseKlineMessage({ stream: 'btcusdt@kline_1m', data: klineMsg() })
    expect(out?.candle.close).toBe(37050.25)
  })

  it.each([
    ['invalid JSON', '{not json'],
    ['null', null],
    ['number', 42],
    ['array', [1, 2]],
    ['non-kline event', { e: 'trade', s: 'BTCUSDT', p: '1' }],
    ['missing k', { e: 'kline', s: 'BTCUSDT' }],
    ['empty price', klineMsg({ o: '' })],
    ['NaN price', klineMsg({ c: 'abc' })],
    ['Infinity volume', klineMsg({ v: 'Infinity' })],
    ['missing high', klineMsg({ h: undefined })],
    ['object time', klineMsg({ t: {} })],
  ])('returns null for %s', (_name, data) => {
    expect(parseKlineMessage(data)).toBeNull()
  })

  it('filters by expected symbol and interval', () => {
    const expected = { symbol: 'btcusdt', interval: '1m' }
    expect(parseKlineMessage(klineMsg(), expected)).not.toBeNull()
    expect(parseKlineMessage(klineMsg({ s: 'ETHUSDT' }), expected)).toBeNull()
    expect(parseKlineMessage(klineMsg({ i: '5m' }), expected)).toBeNull()
    expect(parseKlineMessage(klineMsg({ s: undefined }), expected)).toBeNull()
  })
})

describe('backoffDelay', () => {
  it('grows exponentially from 1 s at the top of the jitter range', () => {
    const max = () => 1
    expect([0, 1, 2, 3, 4].map((a) => backoffDelay(a, max))).toEqual([
      1000, 2000, 4000, 8000, 16000,
    ])
  })

  it('caps at 30 s, even for huge attempts', () => {
    expect(backoffDelay(5, () => 1)).toBe(30000)
    expect(backoffDelay(1000, () => 1)).toBe(30000)
    expect(backoffDelay(Number.MAX_SAFE_INTEGER, () => 0)).toBe(15000)
  })

  it('jitters within [base/2, base]', () => {
    for (let a = 0; a < 8; a++) {
      const base = Math.min(30000, 1000 * 2 ** a)
      expect(backoffDelay(a, () => 0)).toBe(base / 2)
      for (let i = 0; i < 50; i++) {
        const d = backoffDelay(a)
        expect(d).toBeGreaterThanOrEqual(base / 2)
        expect(d).toBeLessThanOrEqual(base)
      }
    }
  })

  it('treats negative / NaN attempts as 0 and clamps bad rand output', () => {
    expect(backoffDelay(-3, () => 1)).toBe(1000)
    expect(backoffDelay(Number.NaN, () => 1)).toBe(1000)
    expect(backoffDelay(0, () => 5)).toBe(1000)
    expect(backoffDelay(0, () => -5)).toBe(500)
  })
})

class MiniWs {
  static instances: MiniWs[] = []
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  readonly url: string
  sent: string[] = []
  constructor(url: string) {
    this.url = url
    MiniWs.instances.push(this)
  }
  send(data: string): void {
    this.sent.push(data)
  }
  close(): void {
    this.readyState = 3
  }
  open(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  message(data: string): void {
    this.onmessage?.({ data } as MessageEvent)
  }
  serverClose(code: number): void {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
}

describe('openReconnectingSocket options', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    MiniWs.instances = []
  })

  function open(extra: {
    fatalCloseCodes?: readonly number[]
    liveOnFirstMessage?: boolean
    staleIsFailure?: boolean
    onControl?: (data: unknown, control: SocketControl) => void
  }) {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const statuses: ConnectionStatus[] = []
    const order: string[] = []
    const { onControl, ...rest } = extra
    const teardown = openReconnectingSocket({
      WebSocketImpl: MiniWs as unknown as typeof WebSocket,
      url: 'ws://x',
      staleTimeoutMs: 60_000,
      onMessage: (d, control) => {
        order.push(`msg:${String(d)}`)
        onControl?.(d, control)
      },
      onStatus: (s) => {
        statuses.push(s)
        order.push(`status:${s}`)
      },
      ...rest,
    })
    return { statuses, order, teardown, last: () => MiniWs.instances[MiniWs.instances.length - 1] }
  }

  it('default (BinanceProvider): live on open, and a 1008 close reconnects as before', () => {
    const h = open({})
    h.last().open()
    expect(h.statuses).toEqual(['connecting', 'live'])
    h.last().serverClose(1008)
    expect(h.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    expect(MiniWs.instances).toHaveLength(2)
    h.teardown()
  })

  it('fatal close code: offline, no retries, no timers', () => {
    const h = open({ fatalCloseCodes: [1008] })
    h.last().open()
    h.last().serverClose(1008)
    expect(h.statuses).toEqual(['connecting', 'live', 'offline'])
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(600_000)
    expect(MiniWs.instances).toHaveLength(1)
    h.teardown()
    expect(h.statuses).toEqual(['connecting', 'live', 'offline'])
  })

  it('liveOnFirstMessage: delivers the first message, then reports live', () => {
    const h = open({ liveOnFirstMessage: true })
    h.last().open()
    expect(h.statuses).toEqual(['connecting'])
    h.last().message('a')
    h.last().message('b')
    expect(h.order).toEqual(['status:connecting', 'msg:a', 'status:live', 'msg:b'])
    h.teardown()
  })

  it('default: a stale timeout on an opened socket reconnects silently (status stays live)', () => {
    const h = open({})
    h.last().open()
    h.last().message('a')
    vi.advanceTimersByTime(60_000)
    vi.advanceTimersByTime(1_000)
    expect(MiniWs.instances).toHaveLength(2)
    h.last().open()
    expect(h.statuses).toEqual(['connecting', 'live'])
    h.teardown()
  })

  it('staleIsFailure: a stall reads reconnecting, then live again after the reconnect', () => {
    const h = open({ staleIsFailure: true })
    h.last().open()
    h.last().message('a')
    vi.advanceTimersByTime(60_000)
    expect(h.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(999)
    expect(MiniWs.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(MiniWs.instances).toHaveLength(2)
    h.last().open()
    expect(h.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    h.teardown()
  })

  it('control.fail() drops the socket as a failed attempt with escalating backoff', () => {
    const h = open({
      liveOnFirstMessage: true,
      onControl: (d, control) => {
        if (d === 'reject') control.fail()
      },
    })
    h.last().open()
    h.last().message('reject')
    // The rejected first message never reads live.
    expect(h.statuses).toEqual(['connecting', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    expect(MiniWs.instances).toHaveLength(2)
    h.last().open()
    h.last().message('reject')
    // Second consecutive failure: attempt 1 -> 2 s, not reset by the message.
    vi.advanceTimersByTime(1_999)
    expect(MiniWs.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(MiniWs.instances).toHaveLength(3)
    h.last().open()
    h.last().message('ok')
    expect(h.statuses).toEqual(['connecting', 'reconnecting', 'live'])
    h.teardown()
  })

  it('control.send() writes to the open socket only, and a stale control is a no-op', () => {
    let saved: SocketControl | null = null
    const h = open({
      onControl: (_d, control) => {
        saved = control
      },
    })
    h.last().open()
    h.last().message('a')
    const first = h.last()
    const ctl = saved as unknown as SocketControl
    expect(ctl.send('hello')).toBe(true)
    expect(first.sent).toEqual(['hello'])
    first.serverClose(1006)
    expect(ctl.send('late')).toBe(false)
    ctl.fail()
    expect(h.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    expect(first.sent).toEqual(['hello'])
    h.teardown()
  })
})

describe('parseKlineMessage (1s)', () => {
  it('parses a closed 1s kline and filters it by interval', () => {
    const msg = klineMsg({ t: 1700000041000, T: 1700000041999, i: '1s', x: true })
    expect(parseKlineMessage(msg, { symbol: 'BTCUSDT', interval: '1s' })).toEqual({
      candle: {
        time: 1700000041,
        open: 37000.1,
        high: 37100.5,
        low: 36950,
        close: 37050.25,
        volume: 12.5,
      },
      closed: true,
    })
    expect(parseKlineMessage(msg, { symbol: 'BTCUSDT', interval: '1m' })).toBeNull()
  })
})
