// Binary searches over ascending sequences, shared by the chart, the indicator runner and the
// candle store (Phase 19 DRY: one implementation instead of nine).

/**
 * First index i in [0, n) with key(i) >= x, or n when there is none. `key` must be
 * non-decreasing in i. NaN keys compare as "not below x", like the copies this replaces.
 */
export function lowerBound(n: number, key: (i: number) => number, x: number): number {
  let lo = 0
  let hi = n
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (key(mid) < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** First index i in [0, n) with key(i) > x, or n when there is none. */
export function upperBound(n: number, key: (i: number) => number, x: number): number {
  let lo = 0
  let hi = n
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (key(mid) <= x) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** `lowerBound` over items with a numeric `time` (candles, indicator points, markers). */
export function lowerBoundByTime(
  items: readonly { readonly time: number }[],
  time: number,
): number {
  return lowerBound(items.length, (i) => items[i].time, time)
}

/** `upperBound` over items with a numeric `time`. */
export function upperBoundByTime(
  items: readonly { readonly time: number }[],
  time: number,
): number {
  return upperBound(items.length, (i) => items[i].time, time)
}

/**
 * Index of the item whose `time` equals `time` (the first one, with duplicates), or -1.
 * `items` ascending by time; a NaN `time` is never found.
 */
export function indexByTime(items: readonly { readonly time: number }[], time: number): number {
  const i = lowerBoundByTime(items, time)
  return i < items.length && items[i].time === time ? i : -1
}

/**
 * Index of the last item with `time` <= `time` (the last one, with duplicates), or -1 when every
 * item is later (or `time` is NaN). `items` ascending by time.
 */
export function lastIndexAtOrBeforeTime(
  items: readonly { readonly time: number }[],
  time: number,
): number {
  return upperBoundByTime(items, time) - 1
}
