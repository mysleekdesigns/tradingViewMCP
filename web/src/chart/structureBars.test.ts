import { describe, expect, it } from 'vitest'
import type { CandleSource, CandleSubscribeOptions } from '../data/timeframeProvider'
import { computeMarketStructure, type MarketStructureParams } from '../indicators/marketStructure'
import type { IndicatorConfig } from '../state/indicators'
import {
  initialSnapshot,
  LiveCandlesController,
  type LiveCandlesSnapshot,
  type LoadState,
} from '../state/liveCandles'
import type { Candle, ConnectionStatus } from '../types'
import { MarketStructureLayer } from './marketStructureLayer'
import { closedBarCount, structureClosedThrough } from './structureBars'

const H = 3600

function c(time: number, open: number, high: number, low: number, close: number): Candle {
  return { time, open, high, low, close, volume: 1 }
}

function ready(candles: Candle[]): LoadState {
  return { kind: 'ready', candles, bars: candles }
}

const ROWS = [c(0, 1, 2, 0, 1), c(H, 1, 2, 0, 1), c(2 * H, 1, 2, 0, 1), c(3 * H, 1, 2, 0, 1)]

describe('structureClosedThrough (the closed-bar wiring ChartPanel uses)', () => {
  it('nothing loaded: null', () => {
    expect(structureClosedThrough(initialSnapshot, null)).toBeNull()
    expect(structureClosedThrough({ load: { kind: 'loading' }, closedThrough: 5 }, ROWS)).toBeNull()
  })

  it("live: the controller's closedThrough, never the forming last bar", () => {
    const live = { load: ready(ROWS), closedThrough: 2 * H }
    expect(structureClosedThrough(live, null)).toBe(2 * H)
    // Not "every plotted bar is closed": the forming bar 3H is not used.
    expect(closedBarCount(ROWS, structureClosedThrough(live, null))).toBe(3)
    expect(structureClosedThrough({ load: ready(ROWS), closedThrough: null }, null)).toBeNull()
  })

  it('replay: the last replay bar, every replay bar being closed', () => {
    const live = { load: ready(ROWS), closedThrough: 2 * H }
    const replay = ROWS.slice(0, 2)
    expect(structureClosedThrough(live, replay)).toBe(H)
    expect(closedBarCount(replay, structureClosedThrough(live, replay))).toBe(2)
    expect(structureClosedThrough(live, [])).toBeNull()
  })

  it('replay never goes past the live closedThrough of the same stream', () => {
    // A partial bar (2H) awaiting a gap fill can be in the replay source; it is not used.
    const live = { load: ready(ROWS), closedThrough: H }
    expect(structureClosedThrough(live, ROWS.slice(0, 3))).toBe(H)
    expect(structureClosedThrough({ load: ready(ROWS), closedThrough: null }, ROWS)).toBeNull()
  })

  it('closedBarCount counts bars at or before the time (binary search)', () => {
    expect(closedBarCount(ROWS, null)).toBe(0)
    expect(closedBarCount([], 10)).toBe(0)
    expect(closedBarCount(ROWS, -1)).toBe(0)
    expect(closedBarCount(ROWS, 0)).toBe(1)
    expect(closedBarCount(ROWS, H + 1)).toBe(2)
    expect(closedBarCount(ROWS, 3 * H)).toBe(4)
    expect(closedBarCount(ROWS, 99 * H)).toBe(4)
  })
})

/* ---------------------------------------------------------------------------------------- */
/* Live controller -> closedThrough -> structure layer, through a socket drop                 */
/* ---------------------------------------------------------------------------------------- */

/** REST calls resolve on demand; the stream is driven by the test. */
class FakeSource implements CandleSource {
  requests: { resolve: (rows: Candle[]) => void; reject: (e: unknown) => void }[] = []
  private onCandle: ((c: Candle, closed: boolean, provisional?: boolean) => void) | null = null
  private onStatus: ((s: ConnectionStatus) => void) | null = null

  getCandles(): Promise<Candle[]> {
    return new Promise((resolve, reject) => this.requests.push({ resolve, reject }))
  }

  subscribeCandles(
    _symbol: string,
    _tf: string,
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: CandleSubscribeOptions,
  ): () => void {
    this.onCandle = onCandle
    this.onStatus = opts?.onStatus ?? null
    return () => {
      this.onCandle = null
      this.onStatus = null
    }
  }

  tick(bar: Candle, closed = false): void {
    this.onCandle?.(bar, closed)
  }

  /** A closed bar the source will send once more, possibly corrected (Coinbase). */
  provisionalClose(bar: Candle): void {
    this.onCandle?.(bar, true, true)
  }

  status(s: ConnectionStatus): void {
    this.onStatus?.(s)
  }

  last(): { resolve: (rows: Candle[]) => void; reject: (e: unknown) => void } {
    const r = this.requests[this.requests.length - 1]
    if (r === undefined) throw new Error('no request')
    return r
  }
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0))

const PARAMS: MarketStructureParams = { left: 1, right: 1, maxZones: 3 }
const CFG: IndicatorConfig[] = [
  {
    uid: 'ms',
    type: 'mstructure',
    inputs: { ...PARAMS, showSwings: 1, showBos: 1, showChoch: 1, showZones: 1 },
    source: 'close',
    colors: {},
    visible: true,
  },
]

// Hand-built 1h bars. With left = right = 1 the swing high is bar 2 (high 110 > 105, >= 106),
// confirmed on bar 3's close. Bar 4's FINAL close (101) stays below 110: no break through bar 6.
// Bar 4's PARTIAL close (120, seen before the socket dropped) is above 110: it would be a BOS.
const B = [
  c(0, 100, 101, 99, 100),
  c(H, 100, 105, 99, 104),
  c(2 * H, 104, 110, 103, 108),
  c(3 * H, 108, 106, 104, 105),
  c(4 * H, 105, 107, 100, 101), // final values of bar 4
  c(5 * H, 101, 104, 100.5, 103),
  c(6 * H, 103, 105, 101, 104),
  c(7 * H, 104, 106, 103, 105),
]
const BAR4_EARLY = c(4 * H, 105, 106, 104, 105) // REST's newest row: forming
const BAR4_PARTIAL = c(4 * H, 105, 121, 104, 120) // last tick before the drop

describe('live closedThrough drives the structure layer (forming and partial bars unused)', () => {
  it('a partial bar left by a drop makes no event; the gap fill advances incrementally', async () => {
    // The trap is real on this data: the partial bar 4 would make a BOS, its final values do not.
    const partialRun = computeMarketStructure([...B.slice(0, 4), BAR4_PARTIAL], PARAMS)
    expect(partialRun.events.map((e) => [e.time, e.kind])).toEqual([[4 * H, 'bos']])
    expect(computeMarketStructure(B.slice(0, 7), PARAMS).events).toEqual([])

    const source = new FakeSource()
    let snap: LiveCandlesSnapshot = initialSnapshot
    const controller = new LiveCandlesController(
      { provider: source, symbol: 'BTCUSDT', interval: '1h', limit: 5, source: 'Binance' },
      (s) => (snap = s),
    )
    const layer = new MarketStructureLayer()
    const sync = () => {
      const bars = snap.load.kind === 'ready' ? snap.load.bars : []
      layer.sync(CFG, bars, structureClosedThrough(snap, null))
      return bars
    }
    controller.start()
    source.status('live')
    source.last().resolve([...B.slice(0, 4), BAR4_EARLY])
    await flush()
    sync()
    expect(layer.statsOf('ms')).toEqual({ recomputes: 1, pushed: 4, count: 4 })

    // Bar 4 forming; the socket drops and comes back (gap fill in flight).
    source.tick(BAR4_PARTIAL)
    source.status('reconnecting')
    source.status('live')
    // A tick for bar 7 arrives before the gap fill: it is appended right after partial bar 4.
    source.tick(B[7])
    const shown = sync()
    expect(shown.map((x) => x.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H, 7 * H])
    expect(snap.closedThrough).toBe(3 * H)
    expect(layer.resultOf('ms')?.events).toEqual([])
    expect(layer.markers()).toEqual([])
    expect(layer.statsOf('ms')?.count).toBe(4)

    // The gap fill lands with bar 4's final values; bars 4..6 are final, 7 still forming.
    source.last().resolve(B.slice(3, 8))
    await flush()
    sync()
    expect(snap.closedThrough).toBe(6 * H)
    expect(layer.resultOf('ms')).toEqual(computeMarketStructure(B.slice(0, 7), PARAMS))
    expect(layer.statsOf('ms')).toEqual({ recomputes: 1, pushed: 7, count: 7 })

    // Bar 7's closed tick: it is final now.
    source.tick(B[7], true)
    sync()
    expect(snap.closedThrough).toBe(7 * H)
    expect(layer.resultOf('ms')).toEqual(computeMarketStructure(B, PARAMS))
    expect(layer.statsOf('ms')).toEqual({ recomputes: 1, pushed: 8, count: 8 })
    controller.stop()
  })

  it('a provisional close (Coinbase) makes no event and no recompute; the settled bar is pushed once', async () => {
    // Bar 4 closed with its provisional values would be a BOS; the corrected values are not.
    expect(
      computeMarketStructure([...B.slice(0, 4), BAR4_PARTIAL], PARAMS).events.map((e) => e.kind),
    ).toEqual(['bos'])

    const source = new FakeSource()
    let snap: LiveCandlesSnapshot = initialSnapshot
    const controller = new LiveCandlesController(
      { provider: source, symbol: 'BTCUSD', interval: '1h', limit: 5, source: 'Coinbase' },
      (s) => (snap = s),
    )
    const layer = new MarketStructureLayer()
    const throughs: (number | null)[] = []
    const sync = () => {
      const bars = snap.load.kind === 'ready' ? snap.load.bars : []
      const through = structureClosedThrough(snap, null)
      throughs.push(through)
      layer.sync(CFG, bars, through)
      // Never, at any step, an event or marker.
      expect(layer.resultOf('ms')?.events).toEqual([])
      expect(layer.markers()).toEqual([])
    }
    controller.start()
    source.status('live')
    source.last().resolve([...B.slice(0, 4), BAR4_EARLY])
    await flush()
    sync()

    source.tick(BAR4_PARTIAL) // forming
    sync()
    source.provisionalClose(BAR4_PARTIAL) // closed, but a second read-back is pending
    sync()
    source.tick(B[5]) // the next bar forms before the read-back
    sync()
    source.tick(B[4], true) // settled with the corrected values
    sync()
    expect(throughs).toEqual([3 * H, 3 * H, 3 * H, 3 * H, 4 * H])
    expect(layer.resultOf('ms')).toEqual(computeMarketStructure(B.slice(0, 5), PARAMS))
    expect(layer.statsOf('ms')).toEqual({ recomputes: 1, pushed: 5, count: 5 })
    controller.stop()
  })
  it('a bar skipped without a reported drop (Binance idle reconnect) is never computed across', async () => {
    // The trap: with 5H missing, 6H is bar 4's right neighbour and confirms its swing low at
    // 6H instead of 5H. That result would be repainted once 5H arrives.
    const across = computeMarketStructure([...B.slice(0, 5), B[6]], PARAMS)
    expect(across).not.toEqual(computeMarketStructure(B.slice(0, 7), PARAMS))

    const source = new FakeSource()
    let snap: LiveCandlesSnapshot = initialSnapshot
    let clock = 4 * H * 1000 + 10_000
    const controller = new LiveCandlesController(
      {
        provider: source,
        symbol: 'BTCUSDT',
        interval: '1h',
        limit: 5,
        source: 'Binance',
        now: () => clock,
      },
      (s) => (snap = s),
    )
    const layer = new MarketStructureLayer()
    const sync = () => {
      const bars = snap.load.kind === 'ready' ? snap.load.bars : []
      layer.sync(CFG, bars, structureClosedThrough(snap, null))
    }
    controller.start()
    source.status('live')
    source.last().resolve([...B.slice(0, 4), BAR4_EARLY])
    await flush()
    source.tick(B[4], true)
    sync()
    expect(snap.closedThrough).toBe(4 * H)

    // The socket went quiet and reconnected silently (status stayed 'live'): 5H was missed.
    clock = 6 * H * 1000 + 5_000
    source.tick(B[6])
    source.tick(B[6], true)
    sync()
    expect(snap.closedThrough).toBe(4 * H)
    expect(layer.resultOf('ms')).toEqual(computeMarketStructure(B.slice(0, 5), PARAMS))
    expect(source.requests).toHaveLength(2) // one gap fill

    source.last().resolve(B.slice(4, 7))
    await flush()
    sync()
    expect(snap.closedThrough).toBe(6 * H)
    expect(layer.resultOf('ms')).toEqual(computeMarketStructure(B.slice(0, 7), PARAMS))
    // Pushed one bar at a time, never recomputed: nothing was repainted.
    expect(layer.statsOf('ms')).toEqual({ recomputes: 1, pushed: 7, count: 7 })
    controller.stop()
  })
})
