// SQLite candle cache with range bookkeeping. Closed bars are stored once and never refetched;
// the forming bar (and any bar closed less than SETTLE_SEC ago, or not yet followed upstream by
// the next bar) is fetched fresh and never stored.
import type { Db } from '../db/index.ts'
import { alignOpenTime, lastClosedOpenTime } from '../time.ts'
import type { RestClient } from '../upstream/rest.ts'
import { ApiError, INTERVAL_SECONDS, type Candle, type Interval } from '../types.ts'

/** Upstream klines page size. */
export const PAGE_BARS = 1000
/**
 * A bar is stored only once it has been closed this long, so a bar the exchange is still
 * finalising right at the boundary is not cached. Until then it is served fresh like the
 * forming bar.
 */
export const SETTLE_SEC = 5

/** Inclusive open-time range [start, end] (UNIX s). */
export interface Range {
  start: number
  end: number
}

/** Parts of `want` (inclusive, step `step`) not covered by `covered` (sorted by start). */
export function subtractRanges(want: Range, covered: readonly Range[], step: number): Range[] {
  const out: Range[] = []
  let cursor = want.start
  for (const r of covered) {
    if (r.end < cursor) continue
    if (r.start > want.end) break
    if (r.start > cursor) out.push({ start: cursor, end: Math.min(r.start - step, want.end) })
    cursor = Math.max(cursor, r.end + step)
    if (cursor > want.end) break
  }
  if (cursor <= want.end) out.push({ start: cursor, end: want.end })
  return out.filter((r) => r.start <= r.end)
}

interface Chunk {
  start: number
  /** Last closed (storable) open time in this chunk, or null if the chunk is fresh-only. */
  closedEnd: number | null
  /** Last open time requested (closedEnd, or E when the fresh tail shares this request). */
  end: number
}

/** Split missing closed ranges into ≤ PAGE_BARS requests, sharing the last with the fresh tail. */
export function planChunks(missing: readonly Range[], fresh: Range | null, step: number): Chunk[] {
  const chunks: Chunk[] = []
  for (const r of missing) {
    for (let a = r.start; a <= r.end; a += PAGE_BARS * step) {
      const b = Math.min(r.end, a + (PAGE_BARS - 1) * step)
      chunks.push({ start: a, closedEnd: b, end: b })
    }
  }
  if (fresh !== null) {
    const last = chunks[chunks.length - 1]
    const freshBars = (fresh.end - fresh.start) / step + 1
    if (
      last !== undefined &&
      last.end + step === fresh.start &&
      (last.end - last.start) / step + 1 + freshBars <= PAGE_BARS
    ) {
      last.end = fresh.end
    } else {
      chunks.push({ start: fresh.start, closedEnd: null, end: fresh.end })
    }
  }
  return chunks
}

/** Upstream failures after which cached closed bars are still worth serving. */
function isSoftUpstreamError(err: unknown): err is ApiError {
  return (
    err instanceof ApiError &&
    (err.kind === 'rate-limited' || err.kind === 'banned' || err.kind === 'network')
  )
}

export interface CandleCache {
  /**
   * The last `limit` bars of `symbol`/`interval` with open time <= E (Binance semantics: gaps
   * are skipped, so a gap does not shorten the answer unless history really starts), ascending
   * and unique, where E = bar containing min(endTime ?? now, now). The window
   * [E − (limit − 1)·I, E] is filled first; if it holds fewer than `limit` bars, older bars come
   * from coverage or one upstream call without startTime.
   * If only the fresh (unstored) tail fails with rate-limited/banned/network, the cached closed
   * bars are served without it; the error is thrown only when nothing usable remains.
   * `symbol` must already be validated/uppercased. `endTime` is UNIX seconds.
   */
  getCandles(symbol: string, interval: Interval, limit: number, endTime?: number): Promise<Candle[]>
}

export function createCandleCache(deps: {
  db: Db
  client: RestClient
  now: () => number
}): CandleCache {
  const { db, client } = deps

  const selectCovered = db.prepare<[string, string, number, number], Range>(
    `SELECT start, "end" AS end FROM candle_ranges
      WHERE symbol = ? AND interval = ? AND "end" >= ? AND start <= ?
      ORDER BY start`,
  )
  const deleteRanges = db.prepare<[string, string, number, number]>(
    `DELETE FROM candle_ranges WHERE symbol = ? AND interval = ? AND "end" >= ? AND start <= ?`,
  )
  const insertRange = db.prepare<[string, string, number, number]>(
    `INSERT INTO candle_ranges (symbol, interval, start, "end") VALUES (?, ?, ?, ?)`,
  )
  const insertCandle = db.prepare<[string, string, number, number, number, number, number, number]>(
    `INSERT OR REPLACE INTO candles (symbol, interval, time, open, high, low, close, volume)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  )
  const selectCandles = db.prepare<[string, string, number, number], Candle>(
    `SELECT time, open, high, low, close, volume FROM candles
      WHERE symbol = ? AND interval = ? AND time >= ? AND time <= ?
      ORDER BY time`,
  )

  /** Store closed candles of [range] and record the range as covered (merged), atomically. */
  const storeChunk = db.transaction(
    (symbol: string, interval: Interval, range: Range, candles: readonly Candle[]) => {
      const step = INTERVAL_SECONDS[interval]
      for (const c of candles) {
        if (c.time < range.start || c.time > range.end) continue
        insertCandle.run(symbol, interval, c.time, c.open, c.high, c.low, c.close, c.volume)
      }
      // Overlapping or adjacent (end + I == start) ranges merge into one.
      const lo = range.start - step
      const hi = range.end + step
      let start = range.start
      let end = range.end
      for (const r of selectCovered.all(symbol, interval, lo, hi)) {
        start = Math.min(start, r.start)
        end = Math.max(end, r.end)
      }
      deleteRanges.run(symbol, interval, lo, hi)
      insertRange.run(symbol, interval, start, end)
    },
  )

  /** Newest `n` stored bars with open time in [lo, hi], ascending. */
  const selectNewest = db.prepare<[string, string, number, number, number], Candle>(
    `SELECT time, open, high, low, close, volume FROM candles
      WHERE symbol = ? AND interval = ? AND time >= ? AND time <= ?
      ORDER BY time DESC LIMIT ?`,
  )

  async function fill(
    symbol: string,
    interval: Interval,
    limit: number,
    endTime: number | undefined,
  ): Promise<Candle[]> {
    const step = INTERVAL_SECONDS[interval]
    const nowSec = Math.floor(deps.now() / 1000)
    const e = alignOpenTime(Math.min(endTime ?? nowSec, nowSec), interval)
    if (e < 0) return []
    const s = e - Math.min(limit - 1, Math.floor(e / step)) * step
    // Newest bar that is closed and settled by the local clock, so it may be stored.
    const storable = lastClosedOpenTime(nowSec - SETTLE_SEC, interval)
    const closedEnd = Math.min(e, storable)

    const missing =
      closedEnd >= s
        ? subtractRanges(
            { start: s, end: closedEnd },
            selectCovered.all(symbol, interval, s, closedEnd),
            step,
          )
        : []
    const freshStart = Math.max(s, storable + step)
    const fresh: Range | null = freshStart <= e ? { start: freshStart, end: e } : null

    const chunks = planChunks(missing, fresh, step)
    // Clock-skew guard: the bar at `storable` is stored only if upstream already has the next
    // bar (so upstream itself considers it closed). With no fresh tail in this request, probe
    // one bar past the boundary in the same call.
    const boundary = chunks.find((c) => c.closedEnd === storable)
    if (boundary !== undefined && boundary.end === storable) boundary.end = storable + step

    let maxSeen = -Infinity
    let deferred: { range: Range; bars: Candle[] } | null = null
    /** Bars in [s, e] that are served but not stored (forming, unsettled, unconfirmed). */
    const freshBars: Candle[] = []
    let freshError: ApiError | null = null
    for (const chunk of chunks) {
      let bars: Candle[]
      try {
        bars = await client.klines({
          symbol,
          interval,
          startTime: chunk.start,
          // Covers open times <= chunk.end whether upstream filters on open or close time.
          endTimeMs: (chunk.end + step) * 1000 - 1,
          limit: PAGE_BARS,
        })
      } catch (err) {
        // A fresh-only request may fail softly: the cached closed bars are still served.
        if (chunk.closedEnd === null && isSoftUpstreamError(err)) {
          freshError = err
          continue
        }
        throw err
      }
      for (const c of bars) maxSeen = Math.max(maxSeen, c.time)
      if (chunk.closedEnd !== null) {
        const range = { start: chunk.start, end: chunk.closedEnd }
        if (chunk === boundary) deferred = { range, bars }
        else storeChunk(symbol, interval, range, bars)
      }
      for (const c of bars) {
        if (c.time >= freshStart && c.time <= e) freshBars.push(c)
      }
    }
    if (deferred !== null) {
      const { range, bars } = deferred
      if (maxSeen > storable) {
        storeChunk(symbol, interval, range, bars)
      } else {
        // Upstream has not opened the next bar yet: the boundary bar may still be forming there.
        if (range.end - step >= range.start) {
          storeChunk(symbol, interval, { start: range.start, end: range.end - step }, bars)
        }
        for (const c of bars)
          if (c.time === storable && c.time >= s && c.time <= e) freshBars.push(c)
      }
    }

    const out: Candle[] = closedEnd >= s ? selectCandles.all(symbol, interval, s, closedEnd) : []
    freshBars.sort((a, b) => a.time - b.time)
    for (const c of freshBars) {
      const last = out[out.length - 1]
      if (last !== undefined && last.time >= c.time) {
        if (last.time === c.time) out[out.length - 1] = c
        continue
      }
      out.push(c)
    }
    if (out.length === 0 && freshError !== null) throw freshError

    // Backfill only when the window was fully seen; a failed fresh tail is not a gap.
    if (out.length < limit && freshError === null) {
      const older = await backfill(symbol, interval, s, limit - out.length, storable)
      out.unshift(...older)
    }
    return out.length > limit ? out.slice(out.length - limit) : out
  }

  /**
   * Up to `need` bars with open time < `before`, newest first in coverage terms (Binance
   * `endTime`-only semantics: the last `need` bars, skipping gaps). Served from coverage where
   * possible; otherwise one upstream call without startTime. An empty or short upstream answer
   * means history starts there, recorded as coverage from 0 so it is never refetched.
   */
  async function backfill(
    symbol: string,
    interval: Interval,
    before: number,
    need: number,
    storable: number,
  ): Promise<Candle[]> {
    const step = INTERVAL_SECONDS[interval]
    const parts: Candle[][] = []
    let cursor = before
    // Each pass either consumes one coverage range or makes one upstream call; bounded.
    for (let pass = 0; pass < 8 && need > 0; pass++) {
      const top = cursor - step
      if (top < 0 || top >= storable) break
      const [cov] = selectCovered.all(symbol, interval, top, top)
      if (cov !== undefined) {
        const got = selectNewest.all(symbol, interval, cov.start, top, need).reverse()
        parts.unshift(got)
        need -= got.length
        if (cov.start <= 0) break
        cursor = cov.start
        continue
      }
      const want = Math.min(need, PAGE_BARS)
      const bars = (
        await client.klines({ symbol, interval, endTimeMs: cursor * 1000 - 1, limit: want })
      ).filter((c) => c.time <= top)
      const first = bars[0]
      if (first === undefined || bars.length < want) {
        // History starts here (pre-listing): cover [0, top] so it is never asked for again.
        storeChunk(symbol, interval, { start: 0, end: top }, bars)
      } else {
        storeChunk(symbol, interval, { start: first.time, end: top }, bars)
      }
      parts.unshift(bars)
      need -= bars.length
      if (first === undefined || bars.length < want) break
      cursor = first.time
    }
    return parts.flat()
  }

  /** Fills of one series run one after another so a second fill sees the first's coverage. */
  const seriesTail = new Map<string, Promise<unknown>>()
  /** Identical concurrent requests share one promise. */
  const inFlight = new Map<string, Promise<Candle[]>>()

  return {
    getCandles(symbol, interval, limit, endTime) {
      const nowSec = Math.floor(deps.now() / 1000)
      const e = alignOpenTime(Math.min(endTime ?? nowSec, nowSec), interval)
      const key = `${symbol}|${interval}|${limit}|${e}`
      const existing = inFlight.get(key)
      if (existing) return existing

      const series = `${symbol}|${interval}`
      const prev = seriesTail.get(series) ?? Promise.resolve()
      const p = prev.catch(() => undefined).then(() => fill(symbol, interval, limit, endTime))
      // Cleanup is registered on `p` before any caller awaits it, so it runs first: a request
      // made right after this one settles never gets the old (possibly stale-forming) result.
      const settled = p.then(
        () => cleanup(),
        () => cleanup(),
      )
      const cleanup = (): void => {
        if (inFlight.get(key) === p) inFlight.delete(key)
        if (seriesTail.get(series) === settled) seriesTail.delete(series)
      }
      seriesTail.set(series, settled)
      inFlight.set(key, p)
      return p
    },
  }
}
