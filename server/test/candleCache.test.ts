import { beforeEach, describe, expect, it } from 'vitest'
import {
  createCandleCache,
  planChunks,
  subtractRanges,
  type CandleCache,
} from '../src/cache/candleCache.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { createRestClient } from '../src/upstream/rest.ts'
import { ApiError, INTERVAL_SECONDS, type Interval } from '../src/types.ts'

const hour = (h: number, m = 0, s = 0) => Date.UTC(2026, 9, 21, h, m, s) / 1000

/**
 * Fake Binance klines endpoint over a synthetic series: one bar per interval from `listedAt`,
 * close = open time / 1000 + `version` (so a refetch with a bumped version is visible), minus
 * the open times in `gaps`. Filters on open time like Binance and honours `limit`.
 */
class FakeUpstream {
  calls: URL[] = []
  listedAt = 0
  gaps = new Set<number>()
  version = 0
  badSymbol = false
  /** Upstream clock = local clock − skewSec (local clock ahead when positive). */
  skewSec = 0
  /** When set, every call answers with this HTTP status. */
  failStatus: number | null = null
  clock: { ms: number }
  constructor(clock: { ms: number }) {
    this.clock = clock
  }

  fetch = (async (input: string | URL | Request) => {
    const url = new URL(String(input))
    this.calls.push(url)
    if (this.badSymbol) {
      return new Response(JSON.stringify({ code: -1121, msg: 'Invalid symbol.' }), { status: 400 })
    }
    if (this.failStatus !== null) {
      return new Response('{}', { status: this.failStatus, headers: { 'Retry-After': '30' } })
    }
    const iv = url.searchParams.get('interval') as Interval
    const step = INTERVAL_SECONDS[iv]
    const endMs = Number(url.searchParams.get('endTime') ?? Infinity)
    const limit = Number(url.searchParams.get('limit'))
    const nowSec = Math.floor(this.clock.ms / 1000) - this.skewSec
    const rawStart = url.searchParams.get('startTime')
    let times: number[] = []
    if (rawStart === null) {
      // Binance: endTime only → the last `limit` bars with open time <= endTime.
      const last = Math.floor(Math.min(endMs / 1000, nowSec) / step) * step
      for (let t = last; t >= this.listedAt && times.length < limit; t -= step) {
        if (!this.gaps.has(t)) times.unshift(t)
      }
    } else {
      for (let t = Number(rawStart) / 1000; t * 1000 <= endMs && t <= nowSec; t += step) {
        if (t >= this.listedAt && !this.gaps.has(t)) times.push(t)
        if (times.length >= limit) break
      }
    }
    times = times.filter((t) => t >= 0)
    const rows: unknown[] = []
    for (const t of times) {
      const c = t / 1000 + this.version + (t + step > nowSec ? 0.5 : 0) // forming bars end in .5
      rows.push([
        t * 1000,
        String(c),
        String(c + 1),
        String(c - 1),
        String(c),
        '10',
        (t + step) * 1000 - 1,
      ])
    }
    return new Response(JSON.stringify(rows), { status: 200 })
  }) as typeof fetch

  /** startTime (s) of each call; null for endTime-only (backfill) calls. */
  starts(): (number | null)[] {
    return this.calls.map((u) => {
      const v = u.searchParams.get('startTime')
      return v === null ? null : Number(v) / 1000
    })
  }
}

describe('subtractRanges / planChunks', () => {
  it('subtracts covered ranges', () => {
    expect(subtractRanges({ start: 0, end: 10 }, [], 1)).toEqual([{ start: 0, end: 10 }])
    expect(
      subtractRanges(
        { start: 0, end: 10 },
        [
          { start: 2, end: 3 },
          { start: 6, end: 12 },
        ],
        1,
      ),
    ).toEqual([
      { start: 0, end: 1 },
      { start: 4, end: 5 },
    ])
    expect(subtractRanges({ start: 5, end: 8 }, [{ start: 0, end: 20 }], 1)).toEqual([])
  })

  it('paginates ranges above 1000 bars', () => {
    const chunks = planChunks([{ start: 0, end: 2499 * 60 }], null, 60)
    expect(chunks.map((c) => [c.start / 60, c.end / 60])).toEqual([
      [0, 999],
      [1000, 1999],
      [2000, 2499],
    ])
  })

  it('shares the last closed request with the fresh tail when contiguous and small', () => {
    expect(planChunks([{ start: 0, end: 3 }], { start: 4, end: 4 }, 1)).toEqual([
      { start: 0, closedEnd: 3, end: 4 },
    ])
    expect(planChunks([{ start: 0, end: 2 }], { start: 4, end: 4 }, 1)).toEqual([
      { start: 0, closedEnd: 2, end: 2 },
      { start: 4, closedEnd: null, end: 4 },
    ])
    expect(planChunks([{ start: 0, end: 999 }], { start: 1000, end: 1000 }, 1)).toHaveLength(2)
    expect(planChunks([], { start: 4, end: 4 }, 1)).toEqual([{ start: 4, closedEnd: null, end: 4 }])
  })
})

describe('candle cache', () => {
  let db: Db
  let clock: { ms: number }
  let up: FakeUpstream
  let cache: CandleCache

  const rows = () =>
    db
      .prepare('SELECT time FROM candles ORDER BY time')
      .all()
      .map((r) => (r as { time: number }).time)
  const ranges = () =>
    db.prepare('SELECT start, "end" AS end FROM candle_ranges ORDER BY start').all() as {
      start: number
      end: number
    }[]

  beforeEach(() => {
    db = openDb(':memory:')
    clock = { ms: hour(13, 37, 42) * 1000 }
    up = new FakeUpstream(clock)
    const client = createRestClient({
      fetch: up.fetch,
      now: () => clock.ms,
      restBase: 'https://up.test',
    })
    cache = createCandleCache({ db, client, now: () => clock.ms })
  })

  it('cold fill: one upstream call, closed bars stored, forming bar appended', async () => {
    const out = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(out.map((c) => c.time)).toEqual([hour(9), hour(10), hour(11), hour(12), hour(13)])
    expect(out[4]?.close).toBe(hour(13) / 1000 + 0.5)
    expect(up.calls).toHaveLength(1)
    expect(up.starts()).toEqual([hour(9)])
    expect(rows()).toEqual([hour(9), hour(10), hour(11), hour(12)])
    expect(ranges()).toEqual([{ start: hour(9), end: hour(12) }])
    for (const c of out) for (const v of Object.values(c)) expect(typeof v).toBe('number')
  })

  it('second identical request only fetches the forming bar', async () => {
    const first = await cache.getCandles('BTCUSDT', '1h', 5)
    up.calls = []
    const second = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(second).toEqual(first)
    expect(up.starts()).toEqual([hour(13)])
  })

  it('partial overlap fetches only the gap', async () => {
    await cache.getCandles('BTCUSDT', '1h', 3) // closed 11..12
    up.calls = []
    const out = await cache.getCandles('BTCUSDT', '1h', 6) // closed 8..12
    expect(out.map((c) => c.time)).toEqual([8, 9, 10, 11, 12, 13].map((h) => hour(h)))
    expect(up.starts()).toEqual([hour(8), hour(13)])
    expect(up.calls[0]?.searchParams.get('endTime')).toBe(String(hour(11) * 1000 - 1))
    expect(ranges()).toEqual([{ start: hour(8), end: hour(12) }])
  })

  it('pre-listing: an empty window and empty backfill are recorded and never refetched', async () => {
    up.listedAt = hour(12)
    const before = await cache.getCandles('BTCUSDT', '1h', 3, hour(6))
    expect(before).toEqual([])
    // Window 4..6, then one endTime-only call that finds nothing: history starts later.
    expect(up.starts()).toEqual([hour(4), null])
    expect(up.calls[1]?.searchParams.get('endTime')).toBe(String(hour(4) * 1000 - 1))
    expect(ranges()).toEqual([{ start: 0, end: hour(6) }])
    const again = await cache.getCandles('BTCUSDT', '1h', 3, hour(6))
    expect(again).toEqual([])
    const older = await cache.getCandles('BTCUSDT', '1h', 500, hour(2))
    expect(older).toEqual([])
    expect(up.calls).toHaveLength(2)
  })

  it('listing inside the window: returns what exists and records the start of history', async () => {
    up.listedAt = hour(5)
    const out = await cache.getCandles('BTCUSDT', '1h', 4, hour(6))
    expect(out.map((c) => c.time)).toEqual([hour(5), hour(6)])
    expect(ranges()).toEqual([{ start: 0, end: hour(6) }])
    up.calls = []
    expect(await cache.getCandles('BTCUSDT', '1h', 4, hour(6))).toEqual(out)
    expect(up.calls).toHaveLength(0)
  })

  it('a genuine gap inside a range is not refetched; older bars make up the limit', async () => {
    up.gaps.add(hour(10))
    const a = await cache.getCandles('BTCUSDT', '1h', 4, hour(12))
    expect(a.map((c) => c.time)).toEqual([hour(8), hour(9), hour(11), hour(12)])
    expect(up.starts()).toEqual([hour(9), null])
    expect(up.calls[1]?.searchParams.get('limit')).toBe('1')
    const b = await cache.getCandles('BTCUSDT', '1h', 4, hour(12))
    expect(b).toEqual(a)
    expect(up.calls).toHaveLength(2)
  })

  it('a gap longer than limit returns the last limit bars before it, then serves from cache', async () => {
    // 1m bars: exchange halted from 3 days before 02:00 until 09:00 (far more than 500 bars).
    const gapStart = hour(2) - 3 * 86400
    for (let t = gapStart; t <= hour(9); t += 60) up.gaps.add(t)
    const end = hour(9, 59)
    const s = end - 499 * 60
    const out = await cache.getCandles('BTCUSDT', '1m', 500, end)
    // Window s..09:59 holds 09:01..09:59 (59 bars); the other 441 come from before the gap.
    expect(out).toHaveLength(500)
    const times = out.map((c) => c.time)
    expect(times.slice(0, 441)).toEqual(
      Array.from({ length: 441 }, (_, i) => gapStart - (441 - i) * 60),
    )
    expect(times[441]).toBe(hour(9, 1))
    expect(times[499]).toBe(end)
    expect(up.starts()).toEqual([s, null])
    expect(up.calls[1]?.searchParams.get('endTime')).toBe(String(s * 1000 - 1))
    expect(up.calls[1]?.searchParams.get('limit')).toBe('441')

    up.calls = []
    expect(await cache.getCandles('BTCUSDT', '1m', 500, end)).toEqual(out)
    expect(up.calls).toHaveLength(0)

    // Paging further back (loadOlder) gets a full page, never an empty one.
    const first = times[0] ?? 0
    const page = await cache.getCandles('BTCUSDT', '1m', 300, first - 1)
    expect(page).toHaveLength(300)
    expect(page[299]?.time).toBe(first - 60)
  })

  it('a backfill that hits an upstream error fails the request', async () => {
    up.gaps.add(hour(10))
    await cache.getCandles('BTCUSDT', '1h', 3, hour(12)) // window 10..12, backfill 1 bar
    up.failStatus = 429
    await expect(cache.getCandles('BTCUSDT', '1h', 3, hour(8))).rejects.toMatchObject({
      kind: 'rate-limited',
    })
  })

  it('never stores the forming bar; stores it once it has closed', async () => {
    await cache.getCandles('BTCUSDT', '1h', 2)
    expect(rows()).not.toContain(hour(13))
    clock.ms = hour(14, 5) * 1000
    up.calls = []
    const out = await cache.getCandles('BTCUSDT', '1h', 3)
    expect(out.map((c) => c.time)).toEqual([hour(12), hour(13), hour(14)])
    expect(out[1]?.close).toBe(hour(13) / 1000) // final, not the forming .5 value
    expect(rows()).toContain(hour(13))
    expect(rows()).not.toContain(hour(14))
    expect(up.starts()).toEqual([hour(13)]) // 13 (closed) + 14 (forming) share one call
  })

  it('a bar closed less than the settle time ago is served fresh, not stored', async () => {
    clock.ms = hour(14, 0, 1) * 1000
    const out = await cache.getCandles('BTCUSDT', '1h', 3)
    expect(out.map((c) => c.time)).toEqual([hour(12), hour(13), hour(14)])
    expect(rows()).toEqual([hour(12)])
    expect(ranges()).toEqual([{ start: hour(12), end: hour(12) }])
  })

  it('settle: a bar closed 4 s ago (local clock 3 s ahead of upstream) is not stored', async () => {
    up.skewSec = 3
    clock.ms = hour(14, 0, 4) * 1000 // upstream sees 14:00:01
    const out = await cache.getCandles('BTCUSDT', '1h', 3)
    expect(out.map((c) => c.time)).toEqual([hour(12), hour(13), hour(14)])
    expect(rows()).not.toContain(hour(13))
    expect(rows()).toContain(hour(12))
  })

  it('skew beyond the settle time: the boundary bar is stored only once upstream has the next bar', async () => {
    up.skewSec = 10
    clock.ms = hour(14, 0, 6) * 1000 // locally settled; upstream is at 13:59:56, 13 still forming
    const out = await cache.getCandles('BTCUSDT', '1h', 3)
    expect(out.map((c) => c.time)).toEqual([hour(11), hour(12), hour(13)])
    expect(out[2]?.close).toBe(hour(13) / 1000 + 0.5) // forming upstream, served fresh
    expect(rows()).toEqual([hour(11), hour(12)])
    expect(ranges()).toEqual([{ start: hour(11), end: hour(12) }])
    // Same with endTime at the boundary (no fresh tail): probe one bar past it.
    up.calls = []
    const past = await cache.getCandles('BTCUSDT', '1h', 2, hour(13))
    expect(past.map((c) => c.time)).toEqual([hour(12), hour(13)])
    expect(up.calls[0]?.searchParams.get('endTime')).toBe(String(hour(15) * 1000 - 1))
    expect(rows()).not.toContain(hour(13))
    // Once upstream has opened 14:00, 13 is final and stored.
    clock.ms = hour(14, 0, 20) * 1000
    const later = await cache.getCandles('BTCUSDT', '1h', 2, hour(13))
    expect(later[1]?.close).toBe(hour(13) / 1000)
    expect(rows()).toContain(hour(13))
  })

  it.each([
    [429, 'rate-limited'],
    [418, 'banned'],
  ])(
    'fresh tail upstream %i after a warm cache serves cached closed bars',
    async (status, kind) => {
      const warm = await cache.getCandles('BTCUSDT', '1h', 5)
      up.failStatus = status
      const out = await cache.getCandles('BTCUSDT', '1h', 5)
      expect(out).toEqual(warm.slice(0, 4)) // forming bar omitted
      // Later calls are gated locally by the client and degrade the same way.
      expect(await cache.getCandles('BTCUSDT', '1h', 5)).toEqual(warm.slice(0, 4))
      // Nothing usable cached for the request → the error surfaces.
      await expect(cache.getCandles('BTCUSDT', '1h', 1)).rejects.toMatchObject({ kind })
      // A needed closed range is missing → the error surfaces.
      await expect(cache.getCandles('BTCUSDT', '1h', 10)).rejects.toMatchObject({ kind })
    },
  )

  it('a network failure of the fresh tail also serves cached closed bars', async () => {
    const warm = await cache.getCandles('BTCUSDT', '1h', 3)
    const client = createRestClient({
      fetch: (() => Promise.reject(new Error('ECONNRESET'))) as typeof fetch,
      now: () => clock.ms,
      restBase: 'https://up.test',
      sleep: () => Promise.resolve(),
    })
    const offline = createCandleCache({ db, client, now: () => clock.ms })
    expect(await offline.getCandles('BTCUSDT', '1h', 3)).toEqual(warm.slice(0, 2))
  })

  it('a bar cached while closed is final and not refetched', async () => {
    await cache.getCandles('BTCUSDT', '1h', 3, hour(10))
    up.version = 100
    up.calls = []
    const out = await cache.getCandles('BTCUSDT', '1h', 3, hour(10))
    expect(out.map((c) => c.close)).toEqual([8, 9, 10].map((h) => hour(h) / 1000))
    expect(up.calls).toHaveLength(0)
  })

  it('merges overlapping and adjacent coverage', async () => {
    await cache.getCandles('BTCUSDT', '1h', 2, hour(3)) // 2..3
    await cache.getCandles('BTCUSDT', '1h', 2, hour(7)) // 6..7
    expect(ranges()).toHaveLength(2)
    await cache.getCandles('BTCUSDT', '1h', 2, hour(5)) // 4..5, adjacent to both
    expect(ranges()).toEqual([{ start: hour(2), end: hour(7) }])
    up.calls = []
    await cache.getCandles('BTCUSDT', '1h', 8, hour(9)) // 2..9, only 8..9 missing
    expect(up.starts()).toEqual([hour(8)])
    expect(ranges()).toEqual([{ start: hour(2), end: hour(9) }])
  })

  it('endTime in the past makes zero calls when covered', async () => {
    await cache.getCandles('BTCUSDT', '1h', 10, hour(12))
    up.calls = []
    const out = await cache.getCandles('BTCUSDT', '1h', 3, hour(7, 30))
    expect(out.map((c) => c.time)).toEqual([hour(5), hour(6), hour(7)])
    expect(up.calls).toHaveLength(0)
  })

  it('endTime in the future is capped at now', async () => {
    const out = await cache.getCandles('BTCUSDT', '1h', 2, hour(23))
    expect(out.map((c) => c.time)).toEqual([hour(12), hour(13)])
  })

  it('coalesces concurrent identical fills into one upstream fetch', async () => {
    const [a, b] = await Promise.all([
      cache.getCandles('BTCUSDT', '1h', 5),
      cache.getCandles('BTCUSDT', '1h', 5),
    ])
    expect(a).toEqual(b)
    expect(up.calls).toHaveLength(1)
  })

  it('serialises overlapping fills of one series so the second reuses coverage', async () => {
    await Promise.all([cache.getCandles('BTCUSDT', '1h', 5), cache.getCandles('BTCUSDT', '1h', 3)])
    // First: closed 9..12 + forming in one call; second: forming only (coalesced URL or fresh).
    expect(up.starts().filter((s) => s !== hour(13))).toEqual([hour(9)])
  })

  it('separates symbols and intervals', async () => {
    await cache.getCandles('BTCUSDT', '1h', 2, hour(5))
    await cache.getCandles('ETHUSDT', '1h', 2, hour(5))
    await cache.getCandles('BTCUSDT', '15m', 2, hour(5))
    expect(up.calls).toHaveLength(3)
  })

  it('weekly bars align to Monday and paginate-free fills work', async () => {
    const out = await cache.getCandles('BTCUSDT', '1w', 3)
    const monday = Date.UTC(2026, 9, 19) / 1000
    expect(out.map((c) => c.time)).toEqual([monday - 2 * 604800, monday - 604800, monday])
  })

  it('propagates bad-symbol and records nothing', async () => {
    up.badSymbol = true
    await expect(cache.getCandles('NOPEUSDT', '1h', 5)).rejects.toMatchObject({
      kind: 'bad-symbol',
      status: 400,
    })
    await expect(cache.getCandles('NOPEUSDT', '1h', 5)).rejects.toBeInstanceOf(ApiError)
    expect(ranges()).toEqual([])
  })

  it('returns [] for an endTime before the epoch-aligned first bar', async () => {
    expect(await cache.getCandles('BTCUSDT', '1w', 5, 0)).toEqual([])
    expect(up.calls).toHaveLength(0)
  })
})
