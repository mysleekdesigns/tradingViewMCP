// One `createSeriesMarkers` primitive per main series, shared by named channels.
//
// LWC draws one marker list per markers primitive, and two primitives on one series would each
// own a separate list (and a second `createSeriesMarkers` replacing the first is a classic way to
// lose the backtest's trade markers). The mux keeps one list per channel ('backtest',
// 'structure', ...) and hands LWC the merge: sorted by time (stable; at equal times the channel
// order decides, then each channel's own order). The primitive is created lazily, the first time
// some channel has markers while a series is attached, and then kept for that series: when every
// channel is empty it is emptied with `setMarkers([])`, not detached. It is only detached by
// `detach` (series removed or switched). LWC 5.2.1's `SeriesMarkersPrimitive.detached()` does not
// unsubscribe its data-changed handler (it subscribes an anonymous arrow and unsubscribes a null
// handle), so detaching and re-creating it per empty/non-empty cycle would leak one handler each
// time. Channels survive `detach` / `attach`, so a chart-type switch re-applies them to the new
// main series.

import type { SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import type { MarkersHandle } from './backtestOverlay'

export type ChartMarker = SeriesMarker<UTCTimestamp>

/** The part of `createSeriesMarkers(...)`'s result the mux uses. */
export interface MarkersPrimitive {
  setMarkers(markers: ChartMarker[]): void
  detach(): void
}

/** `(series, markers) => createSeriesMarkers(series, markers)`; tests inject a fake. */
export type MarkersFactory<S> = (series: S, markers: ChartMarker[]) => MarkersPrimitive

const NO_MARKERS: readonly ChartMarker[] = Object.freeze([])

export class SeriesMarkersMux<S> {
  private series: S | null = null
  private primitive: MarkersPrimitive | null = null
  /** Insertion order = channel order (constructor `order` first, then first use). */
  private readonly channels = new Map<string, readonly ChartMarker[]>()
  private merged: readonly ChartMarker[] = NO_MARKERS
  private readonly factory: MarkersFactory<S>

  constructor(factory: MarkersFactory<S>, order: readonly string[] = []) {
    this.factory = factory
    for (const name of order) this.channels.set(name, NO_MARKERS)
  }

  /**
   * A handle on channel `name`, behaving like a fresh `createSeriesMarkers(series, [])`: the
   * channel starts empty, `setMarkers` replaces its markers and `detach` clears them (the other
   * channels are untouched).
   */
  channel(name: string): MarkersHandle {
    this.clear(name)
    return {
      setMarkers: (markers) => this.set(name, markers),
      detach: () => this.clear(name),
    }
  }

  /** Replace channel `name`'s markers (any order; they are sorted by time when merged). */
  set(name: string, markers: readonly ChartMarker[]): void {
    const prev = this.channels.get(name)
    if (prev === markers) return
    if (markers.length === 0 && (prev === undefined || prev.length === 0)) {
      // Nothing changes on screen; only register the channel (its tie-break position).
      if (prev === undefined) this.channels.set(name, NO_MARKERS)
      return
    }
    this.channels.set(name, markers.length === 0 ? NO_MARKERS : markers)
    this.remerge()
  }

  /** Empty channel `name`. */
  clear(name: string): void {
    this.set(name, NO_MARKERS)
  }

  /** Put the merged markers on `series` (moving them off a previous series). */
  attach(series: S): void {
    if (this.series === series) return
    if (this.series !== null) this.detach(true)
    this.series = series
    this.apply()
  }

  /**
   * Take the primitive off the series. `alive` false = the series (or chart) is already removed,
   * so only the reference is dropped. The channels are kept for the next `attach`.
   */
  detach(alive = true): void {
    const primitive = this.primitive
    this.primitive = null
    this.series = null
    if (primitive !== null && alive) primitive.detach()
  }

  /** The merged list (what the primitive shows while attached). */
  markers(): readonly ChartMarker[] {
    return this.merged
  }

  /** Markers of one channel (tests, debugging). */
  channelMarkers(name: string): readonly ChartMarker[] {
    return this.channels.get(name) ?? NO_MARKERS
  }

  /** A markers primitive is on the series (it may be empty). */
  hasPrimitive(): boolean {
    return this.primitive !== null
  }

  private remerge(): void {
    const all: ChartMarker[] = []
    let sorted = true
    let last = Number.NEGATIVE_INFINITY
    for (const list of this.channels.values()) {
      for (const m of list) {
        if (m.time < last) sorted = false
        last = m.time
        all.push(m)
      }
    }
    // Array.prototype.sort is stable: equal times keep channel order, then list order.
    if (!sorted) all.sort((a, b) => a.time - b.time)
    this.merged = all.length === 0 ? NO_MARKERS : all
    this.apply()
  }

  private apply(): void {
    const series = this.series
    if (series === null) return
    if (this.merged.length === 0) {
      // Keep the primitive (see the header): empty it.
      if (this.primitive !== null) this.primitive.setMarkers([])
      return
    }
    // LWC keeps the array it is given; hand it a copy so later merges never alias it.
    const copy = this.merged.slice()
    if (this.primitive === null) this.primitive = this.factory(series, copy)
    else this.primitive.setMarkers(copy)
  }
}
