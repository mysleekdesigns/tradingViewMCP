import type { MouseEventParams, Time, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'

/**
 * Crosshair sync between charts (multi-chart layouts). Hovering one chart publishes its bar
 * time; every other registered chart moves its crosshair to the bar holding that time, or
 * clears it when the time is outside its data.
 *
 * No feedback loops: only user-originated moves are published (see bindCrosshairSync), the
 * group ignores publishes made while it is broadcasting, and a leave from a chart other than
 * the one being followed is ignored. A follower whose data update re-applied its crosshair is
 * re-shown the published time (`refresh`), which publishes nothing.
 */

/** One chart, as seen by the group. */
export interface CrosshairTarget {
  /** Mark `time` (UNIX seconds), or clear when the chart has no bar for it. */
  show(time: number): void
  clear(): void
}

export interface CrosshairSync {
  /**
   * Add a chart under `id` (re-registering an id replaces it). Returns the removal, which is
   * idempotent and never removes a newer registration of the same id.
   */
  register(id: string, target: CrosshairTarget): () => void
  /** The user's crosshair on chart `id` is over `time` (UNIX seconds), or left it (null). */
  publish(id: string, time: number | null): void
  /**
   * Chart `id` re-applied its crosshair by itself (a data update): if it follows another
   * chart, show it the published time again. No-op for the followed chart or with no time.
   */
  refresh(id: string): void
}

export function createCrosshairSync(): CrosshairSync {
  const targets = new Map<string, CrosshairTarget>()
  /** The chart the others currently follow, and the time it published (null = none). */
  let source: string | null = null
  let published: number | null = null
  let broadcasting = false

  const each = (except: string, fn: (t: CrosshairTarget) => void): void => {
    broadcasting = true
    try {
      for (const [id, t] of targets) {
        if (id === except) continue
        try {
          fn(t)
        } catch {
          // One chart failing (e.g. mid-teardown) must not stop the others.
        }
      }
    } finally {
      broadcasting = false
    }
  }

  return {
    register(id, target) {
      targets.set(id, target)
      let removed = false
      return () => {
        if (removed) return
        removed = true
        if (targets.get(id) !== target) return
        targets.delete(id)
        // The followed chart went away: nothing marks a time any more.
        if (source === id) {
          source = null
          published = null
          each(id, (t) => t.clear())
        }
      }
    },
    publish(id, time) {
      if (broadcasting || !targets.has(id)) return
      // A stale leave (e.g. after the pointer already entered another chart) must not clear
      // the chart the pointer is on now.
      if (time === null && source !== null && source !== id) return
      source = time === null ? null : id
      published = time
      each(id, (t) => (time === null ? t.clear() : t.show(time)))
    },
    refresh(id) {
      const target = targets.get(id)
      if (broadcasting || target === undefined) return
      if (source === null || source === id || published === null) return
      const time = published
      broadcasting = true
      try {
        target.show(time)
      } catch {
        // A chart mid-teardown.
      } finally {
        broadcasting = false
      }
    },
  }
}

/**
 * Index of the bar holding `time`: the last bar opening at or before it, provided `time` is
 * before that bar's close (`step` seconds later). Without a usable step only an exact match
 * counts. null when `time` is outside the bars (or inside a gap of missing bars).
 */
export function barIndexAt(
  bars: readonly Pick<Candle, 'time'>[],
  time: number,
  step: number,
): number | null {
  let lo = 0
  let hi = bars.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (bars[mid].time <= time) {
      found = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  if (found < 0) return null
  const open = bars[found].time
  if (open === time) return found
  return Number.isFinite(step) && step > 0 && time < open + step ? found : null
}

/** The parts of LWC's crosshair-move params the binding reads. */
export type CrosshairMoveParams = Pick<MouseEventParams<Time>, 'time' | 'point' | 'sourceEvent'>

/** The LWC surface the binding needs (IChartApi fits). */
export interface SyncableChart<S> {
  subscribeCrosshairMove(handler: (param: CrosshairMoveParams) => void): void
  unsubscribeCrosshairMove(handler: (param: CrosshairMoveParams) => void): void
  setCrosshairPosition(price: number, time: Time, series: S): void
  clearCrosshairPosition(): void
}

/** What the binding reads from the chart component, at event time. */
export interface SyncSource<S> {
  /** The main series (it can be replaced by a chart-type switch). */
  series(): S | null
  /** Bars currently on the chart. */
  bars(): readonly Candle[] | null
  /** Bar length in seconds. */
  step(): number
  /** The marked bar time (undefined = cleared), e.g. so the legend follows. */
  onShow?(time: number | undefined): void
}

/**
 * Connect an LWC chart to a sync group: its crosshair moves are published, and other charts'
 * moves are shown on it. Returns an idempotent teardown.
 */
export function bindCrosshairSync<S>(
  sync: CrosshairSync,
  id: string,
  chart: SyncableChart<S>,
  source: SyncSource<S>,
): () => void {
  const clear = (): void => {
    chart.clearCrosshairPosition()
    source.onShow?.(undefined)
  }
  const unregister = sync.register(id, {
    show(time) {
      const series = source.series()
      const bars = source.bars()
      const i = series && bars ? barIndexAt(bars, time, source.step()) : null
      if (i === null || !series || !bars) {
        clear()
        return
      }
      const bar = bars[i]
      chart.setCrosshairPosition(bar.close, bar.time as UTCTimestamp, series)
      source.onShow?.(bar.time)
    },
    clear,
  })
  // LWC 5.2 fires crosshair-move in three ways:
  // - a mouse/touch move: `sourceEvent` is set;
  // - the pointer leaving (or touch tracking ending): no `sourceEvent` and no `point`;
  // - any series update/setData re-applying the current crosshair, including a synthetic one
  //   set by setCrosshairPosition: no `sourceEvent`, but a `point`.
  // Only the first two are the user's. Publishing the third would let a follower that ticks
  // take over as source and move the crosshair on the chart the pointer is on. On a follower,
  // the third re-applied the saved x pixel, which is a different bar once bars were appended,
  // so the published time is shown again (setCrosshairPosition fires no event: no loop).
  const onMove = (param: CrosshairMoveParams): void => {
    if (param.sourceEvent === undefined) {
      if (param.point === undefined) sync.publish(id, null)
      else sync.refresh(id)
      return
    }
    // Candle times are UTCTimestamp numbers; anything else means "not over a bar".
    sync.publish(id, typeof param.time === 'number' ? param.time : null)
  }
  chart.subscribeCrosshairMove(onMove)
  let done = false
  return () => {
    if (done) return
    done = true
    try {
      chart.unsubscribeCrosshairMove(onMove)
    } catch {
      // The chart may already be removed.
    }
    unregister()
  }
}
