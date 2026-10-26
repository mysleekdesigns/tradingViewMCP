import { describe, expect, it, vi } from 'vitest'
import type { Coordinate, Time, TouchMouseEventData } from 'lightweight-charts'
import type { Candle } from '../types'
import {
  barIndexAt,
  bindCrosshairSync,
  createCrosshairSync,
  type CrosshairMoveParams,
  type CrosshairTarget,
  type SyncableChart,
} from './crosshairSync'

function target() {
  const t = {
    show: vi.fn<(time: number) => void>(),
    clear: vi.fn<() => void>(),
  }
  return t satisfies CrosshairTarget
}

const HOUR = 3600

function bars(times: number[]): Candle[] {
  return times.map((time, i) => ({
    time,
    open: 100 + i,
    high: 101 + i,
    low: 99 + i,
    close: 100.5 + i,
    volume: 1,
  }))
}

describe('createCrosshairSync', () => {
  it('a move on one chart shows the time on every other chart, not on itself', () => {
    const sync = createCrosshairSync()
    const a = target()
    const b = target()
    const c = target()
    sync.register('a', a)
    sync.register('b', b)
    sync.register('c', c)
    sync.publish('a', 1000)
    expect(a.show).not.toHaveBeenCalled()
    expect(b.show).toHaveBeenCalledWith(1000)
    expect(c.show).toHaveBeenCalledWith(1000)
  })

  it('leaving the chart clears the others', () => {
    const sync = createCrosshairSync()
    const b = target()
    sync.register('a', target())
    sync.register('b', b)
    sync.publish('a', null)
    expect(b.clear).toHaveBeenCalledTimes(1)
  })

  it('ignores publishes made while broadcasting (no feedback loop)', () => {
    const sync = createCrosshairSync()
    const a = target()
    // b echoes every show back into the group, as a chart that fires on programmatic moves would.
    const b: CrosshairTarget = { show: (t) => sync.publish('b', t), clear: vi.fn() }
    sync.register('a', a)
    sync.register('b', b)
    sync.publish('a', 1000)
    expect(a.show).not.toHaveBeenCalled()
  })

  it('a leave from a chart other than the followed one does not clear it', () => {
    const sync = createCrosshairSync()
    const a = target()
    const b = target()
    sync.register('a', a)
    sync.register('b', b)
    // The pointer entered b before a's leave arrived.
    sync.publish('b', 1000)
    sync.publish('a', null)
    expect(b.clear).not.toHaveBeenCalled()
    expect(a.clear).not.toHaveBeenCalled()
    // b's own leave still clears.
    sync.publish('b', null)
    expect(a.clear).toHaveBeenCalledTimes(1)
  })

  it('refresh re-shows the published time on a follower only, without broadcasting', () => {
    const sync = createCrosshairSync()
    const a = target()
    const b = target()
    const c = target()
    sync.register('a', a)
    sync.register('b', b)
    sync.register('c', c)
    sync.refresh('b') // nothing published yet
    expect(b.show).not.toHaveBeenCalled()
    sync.publish('a', 1000)
    sync.refresh('b')
    expect(b.show).toHaveBeenCalledTimes(2)
    expect(b.show).toHaveBeenLastCalledWith(1000)
    expect(c.show).toHaveBeenCalledTimes(1)
    sync.refresh('a') // the followed chart itself
    sync.refresh('zzz')
    expect(a.show).not.toHaveBeenCalled()
    expect(b.show).toHaveBeenCalledTimes(2)
  })

  it('refresh does nothing after a leave or after the followed chart is removed', () => {
    const sync = createCrosshairSync()
    const b = target()
    const removeA = sync.register('a', target())
    sync.register('b', b)
    sync.publish('a', 1000)
    sync.publish('a', null)
    sync.refresh('b')
    expect(b.show).toHaveBeenCalledTimes(1)
    sync.publish('a', 2000)
    removeA()
    sync.refresh('b')
    expect(b.show).toHaveBeenCalledTimes(2)
  })

  it('a refresh that re-enters the group (echoing target) cannot loop', () => {
    const sync = createCrosshairSync()
    let shows = 0
    const b: CrosshairTarget = {
      show: () => {
        shows++
        sync.refresh('b')
        sync.publish('b', 5)
      },
      clear: vi.fn(),
    }
    const a = target()
    sync.register('a', a)
    sync.register('b', b)
    sync.publish('a', 1000)
    sync.refresh('b')
    expect(shows).toBe(2)
    expect(a.show).not.toHaveBeenCalled()
  })

  it('ignores unknown ids', () => {
    const sync = createCrosshairSync()
    const b = target()
    sync.register('b', b)
    sync.publish('zzz', 5)
    expect(b.show).not.toHaveBeenCalled()
  })

  it('removal is idempotent and clears the others when the followed chart leaves', () => {
    const sync = createCrosshairSync()
    const b = target()
    const removeA = sync.register('a', target())
    sync.register('b', b)
    sync.publish('a', 1000)
    removeA()
    removeA()
    expect(b.clear).toHaveBeenCalledTimes(1)
    sync.publish('a', 2000)
    expect(b.show).toHaveBeenCalledTimes(1)
  })

  it('an old removal never removes a newer registration of the same id', () => {
    const sync = createCrosshairSync()
    const a1 = target()
    const a2 = target()
    const removeOld = sync.register('a', a1)
    sync.register('a', a2)
    removeOld()
    sync.register('b', target())
    sync.publish('b', 7)
    expect(a2.show).toHaveBeenCalledWith(7)
    expect(a1.show).not.toHaveBeenCalled()
  })

  it('one failing target does not stop the others', () => {
    const sync = createCrosshairSync()
    const c = target()
    sync.register('a', target())
    sync.register('b', {
      show: () => {
        throw new Error('gone')
      },
      clear: vi.fn(),
    })
    sync.register('c', c)
    sync.publish('a', 1)
    expect(c.show).toHaveBeenCalledWith(1)
  })
})

describe('barIndexAt', () => {
  const b = bars([0, 4 * HOUR, 8 * HOUR, 16 * HOUR]) // 12h bar missing

  it('exact bar times match', () => {
    expect(barIndexAt(b, 4 * HOUR, 4 * HOUR)).toBe(1)
  })

  it('a time inside a bar maps to that bar (1h time on a 4h chart)', () => {
    expect(barIndexAt(b, 5 * HOUR, 4 * HOUR)).toBe(1)
    expect(barIndexAt(b, 8 * HOUR - 1, 4 * HOUR)).toBe(1)
  })

  it('before the first bar, after the last bar closes, or in a gap: null', () => {
    expect(barIndexAt(b, -1, 4 * HOUR)).toBeNull()
    expect(barIndexAt(b, 20 * HOUR, 4 * HOUR)).toBeNull()
    expect(barIndexAt(b, 13 * HOUR, 4 * HOUR)).toBeNull()
    expect(barIndexAt([], 0, 4 * HOUR)).toBeNull()
  })

  it('without a usable step only exact times match', () => {
    expect(barIndexAt(b, 5 * HOUR, Number.NaN)).toBeNull()
    expect(barIndexAt(b, 8 * HOUR, 0)).toBe(2)
  })
})

type Handler = (param: CrosshairMoveParams) => void

const POINT = { x: 10 as Coordinate, y: 20 as Coordinate }
const USER_EVENT = { clientX: 10, clientY: 20 } as TouchMouseEventData

/**
 * Fires crosshair-move the way LWC 5.2.1 does: a user move carries `sourceEvent` and `point`;
 * a leave carries neither; a series update re-applies the current crosshair (user or
 * synthetic, if any) with `point` but no `sourceEvent`. setCrosshairPosition and
 * clearCrosshairPosition fire nothing.
 */
function fakeChart() {
  const handlers = new Set<Handler>()
  let crosshair: Time | undefined | null = null // null = no crosshair shown
  const fire = (param: CrosshairMoveParams) => {
    for (const h of handlers) h(param)
  }
  const chart = {
    handlers,
    subscribeCrosshairMove: (h: Handler) => void handlers.add(h),
    unsubscribeCrosshairMove: (h: Handler) => void handlers.delete(h),
    setCrosshairPosition: vi.fn((_price: number, time: Time) => {
      crosshair = time
    }),
    clearCrosshairPosition: vi.fn(() => {
      crosshair = null
    }),
    /** The user's pointer over `time` (undefined = over the chart but outside the data). */
    hover(time: Time | undefined) {
      crosshair = time
      fire({ time, point: POINT, sourceEvent: USER_EVENT })
    },
    leave() {
      crosshair = null
      fire({})
    },
    /**
     * A live tick (series.update): LWC re-fires the current crosshair, if any, at its saved x
     * pixel. When bars were appended that pixel is another bar: pass it as `drift`.
     */
    tick(drift?: Time) {
      if (crosshair === null) return
      if (drift !== undefined) crosshair = drift
      fire({ time: crosshair, point: POINT })
    },
  }
  return chart satisfies SyncableChart<string> & object
}

describe('bindCrosshairSync', () => {
  it('moves the other chart to the bar holding the hovered time, at its close', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const onShow = vi.fn()
    const bBars = bars([0, 4 * HOUR, 8 * HOUR])
    bindCrosshairSync(sync, 'a', a, {
      series: () => 'A',
      bars: () => bars([0, HOUR, 2 * HOUR, 5 * HOUR]),
      step: () => HOUR,
    })
    bindCrosshairSync(sync, 'b', b, {
      series: () => 'B',
      bars: () => bBars,
      step: () => 4 * HOUR,
      onShow,
    })
    a.hover((5 * HOUR) as Time)
    expect(b.setCrosshairPosition).toHaveBeenCalledWith(bBars[1].close, 4 * HOUR, 'B')
    expect(onShow).toHaveBeenLastCalledWith(4 * HOUR)
    expect(a.setCrosshairPosition).not.toHaveBeenCalled()
  })

  it('clears the other chart when the time is outside its data or the pointer leaves', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const onShow = vi.fn()
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => bars([0]), step: () => HOUR })
    bindCrosshairSync(sync, 'b', b, {
      series: () => 'B',
      bars: () => bars([10 * HOUR]),
      step: () => HOUR,
      onShow,
    })
    a.hover(0 as Time)
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(1)
    a.hover(undefined)
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(2)
    expect(onShow).toHaveBeenLastCalledWith(undefined)
  })

  it('a chart without data or series is cleared, not crashed', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => bars([0]), step: () => HOUR })
    bindCrosshairSync(sync, 'b', b, { series: () => null, bars: () => null, step: () => HOUR })
    a.hover(0 as Time)
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(1)
    expect(b.setCrosshairPosition).not.toHaveBeenCalled()
  })

  it('teardown unsubscribes once and survives a chart that throws on unsubscribe', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const unbindA = bindCrosshairSync(sync, 'a', a, {
      series: () => 'A',
      bars: () => bars([0]),
      step: () => HOUR,
    })
    bindCrosshairSync(sync, 'b', b, { series: () => 'B', bars: () => bars([0]), step: () => HOUR })
    expect(a.handlers.size).toBe(1)
    unbindA()
    unbindA()
    expect(a.handlers.size).toBe(0)
    // b no longer reaches a.
    b.hover(0 as Time)
    expect(a.setCrosshairPosition).not.toHaveBeenCalled()

    const c = fakeChart()
    c.unsubscribeCrosshairMove = () => {
      throw new Error('removed')
    }
    const unbindC = bindCrosshairSync(sync, 'c', c, {
      series: () => 'C',
      bars: () => null,
      step: () => HOUR,
    })
    expect(() => unbindC()).not.toThrow()
    b.hover(0 as Time)
    expect(c.setCrosshairPosition).not.toHaveBeenCalled()
  })

  it('a real pointer leave (no sourceEvent, no point) clears the followers', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const onShow = vi.fn()
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => bars([0]), step: () => HOUR })
    bindCrosshairSync(sync, 'b', b, {
      series: () => 'B',
      bars: () => bars([0]),
      step: () => HOUR,
      onShow,
    })
    a.hover(0 as Time)
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(1)
    a.leave()
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(1)
    expect(onShow).toHaveBeenLastCalledWith(undefined)
  })

  it('a follower tick re-firing its synthetic crosshair neither publishes nor moves others', () => {
    // Pointer on a (1m) at 10:37; b (1h) follows at 10:00; b ticks.
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const c = fakeChart()
    const t1037 = 10 * HOUR + 37 * 60
    const aBars = bars(Array.from({ length: 120 }, (_, i) => 9 * HOUR + i * 60))
    const hBars = bars([9 * HOUR, 10 * HOUR])
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => aBars, step: () => 60 })
    bindCrosshairSync(sync, 'b', b, { series: () => 'B', bars: () => hBars, step: () => HOUR })
    bindCrosshairSync(sync, 'c', c, { series: () => 'C', bars: () => aBars, step: () => 60 })
    a.hover(t1037 as Time)
    expect(b.setCrosshairPosition).toHaveBeenLastCalledWith(hBars[1].close, 10 * HOUR, 'B')
    expect(c.setCrosshairPosition).toHaveBeenLastCalledWith(aBars[97].close, t1037, 'C')
    b.tick()
    b.tick()
    // Nothing moved: a was never driven, c still marks 10:37, b stays on the published time
    // (it is re-shown 10:00 after each of its own updates; see the drift test).
    expect(a.setCrosshairPosition).not.toHaveBeenCalled()
    expect(a.clearCrosshairPosition).not.toHaveBeenCalled()
    expect(c.setCrosshairPosition).toHaveBeenCalledTimes(1)
    expect(c.clearCrosshairPosition).not.toHaveBeenCalled()
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(3)
    expect(new Set(b.setCrosshairPosition.mock.calls.map((call) => call[1]))).toEqual(
      new Set([10 * HOUR]),
    )
    // a is still the source: its next move drives b and c.
    a.hover((t1037 + 60) as Time)
    expect(c.setCrosshairPosition).toHaveBeenLastCalledWith(aBars[98].close, t1037 + 60, 'C')
  })

  it("the hovered chart's own tick re-fire is ignored too; its next real move publishes", () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => bars([0]), step: () => HOUR })
    bindCrosshairSync(sync, 'b', b, { series: () => 'B', bars: () => bars([0]), step: () => HOUR })
    a.hover(0 as Time)
    a.tick()
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(1)
    expect(b.clearCrosshairPosition).not.toHaveBeenCalled()
  })

  it('a follower appending a bar keeps the published time instead of drifting a bar ahead', () => {
    // Pointer on a (1h) at 10:00; b (1s) follows; b appends a bar so LWC's saved x pixel now
    // sits on the next bar.
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const onShow = vi.fn()
    const T = 10 * HOUR
    const bBars = bars([T - 1, T, T + 1])
    bindCrosshairSync(sync, 'a', a, {
      series: () => 'A',
      bars: () => bars([9 * HOUR, T]),
      step: () => HOUR,
    })
    bindCrosshairSync(sync, 'b', b, { series: () => 'B', bars: () => bBars, step: () => 1, onShow })
    a.hover(T as Time)
    expect(b.setCrosshairPosition).toHaveBeenLastCalledWith(bBars[1].close, T, 'B')
    b.tick((T + 1) as Time) // drifted
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(2)
    expect(b.setCrosshairPosition).toHaveBeenLastCalledWith(bBars[1].close, T, 'B')
    expect(onShow).toHaveBeenLastCalledWith(T)
    // Bounded: setCrosshairPosition fires nothing, so one update gives one re-show.
    b.tick((T + 1) as Time)
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(3)
    // a was never driven by b.
    expect(a.setCrosshairPosition).not.toHaveBeenCalled()
    expect(a.clearCrosshairPosition).not.toHaveBeenCalled()
  })

  it('the leave still clears a follower that was refreshed, and later updates do not revive it', () => {
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    bindCrosshairSync(sync, 'a', a, { series: () => 'A', bars: () => bars([0]), step: () => HOUR })
    bindCrosshairSync(sync, 'b', b, {
      series: () => 'B',
      bars: () => bars([0, HOUR]),
      step: () => HOUR,
    })
    a.hover(0 as Time)
    b.tick(HOUR as Time)
    a.leave()
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(1)
    const shown = b.setCrosshairPosition.mock.calls.length
    b.tick() // cleared: LWC fires nothing
    sync.refresh('b')
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(shown)
  })

  it('a follower re-fired inside its own push reads the bars being pushed', () => {
    // Mirrors Chart.tsx: bars() = pushing ?? applied, where `pushing` is set around the
    // series update()/setData() that synchronously re-fires the crosshair.
    const sync = createCrosshairSync()
    const a = fakeChart()
    const b = fakeChart()
    const T = 1_000
    let applied: Candle[] = bars([T - 2, T - 1, T])
    let pushing: Candle[] | null = null
    const push = (next: Candle[], drift?: Time) => {
      pushing = next
      b.tick(drift)
      pushing = null
      applied = next
    }
    bindCrosshairSync(sync, 'a', a, {
      series: () => 'A',
      bars: () => bars([T - 1, T]),
      step: () => 1,
    })
    bindCrosshairSync(sync, 'b', b, {
      series: () => 'B',
      bars: () => pushing ?? applied,
      step: () => 1,
    })
    a.hover(T as Time)
    expect(b.setCrosshairPosition).toHaveBeenLastCalledWith(applied[2].close, T, 'B')
    // A live update of bar T: the crosshair sits at the new close, not the stale one.
    const updated = [...applied.slice(0, 2), { ...applied[2], close: 999 }]
    push(updated)
    expect(b.setCrosshairPosition).toHaveBeenLastCalledWith(999, T, 'B')
    // A replay step back drops bar T: the follower clears instead of marking a missing bar.
    const shown = b.setCrosshairPosition.mock.calls.length
    push(updated.slice(0, 2), (T - 1) as Time)
    expect(b.clearCrosshairPosition).toHaveBeenCalledTimes(1)
    expect(b.setCrosshairPosition).toHaveBeenCalledTimes(shown)
  })
})
