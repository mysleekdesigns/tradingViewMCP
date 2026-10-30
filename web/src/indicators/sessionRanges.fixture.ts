/**
 * Hand-built session ranges fixture (Phase 18).
 *
 * PARITY SOURCE: this file is the reference for any future lab feature that reuses the session
 * rules (e.g. an hour-of-day or opening-range feature in `lab/lab/registry/`). A port must
 * reproduce every expected value below from FIXTURE_CANDLES exactly. The rules are written out in
 * `web/README.md` ("Session ranges overlay") and in `sessionRanges.ts`.
 *
 * Every expected value was worked out by hand from the table below, before the code ran. Edit
 * nothing here without re-doing the hand working.
 *
 * 1h bars from day 1 20:00 UTC to day 2 17:00 UTC, across the UTC midnight between them.
 * Day 1 = 2024-01-01 00:00 UTC (D1), day 2 = D2 = D1 + 86400. The 03:00 bar of day 2 is MISSING
 * (a gap inside Asia, never filled). Times in the expected values are written as h1(h) / h2(h)
 * = the open time of hour h on day 1 / day 2.
 */
import type { Candle } from '../types'
import type { SessionBox } from './types'

export const D1 = 1_704_067_200 // 2024-01-01 00:00:00 UTC (19723 * 86400)
export const D2 = D1 + 86_400
export const HOUR = 3_600

/** Open time of hour h on day 1 / day 2. */
export function h1(h: number): number {
  return D1 + h * HOUR
}
export function h2(h: number): number {
  return D2 + h * HOUR
}

/** [open time, open, high, low, close] per bar, index = bar number. */
const ROWS: readonly (readonly [number, number, number, number, number])[] = [
  /*  0 */ [h1(20), 100, 102, 99, 101], //       NY d1
  /*  1 */ [h1(21), 101, 104, 100, 103], //      NY d1 (last bar: 22:00 is the exclusive end)
  /*  2 */ [h1(22), 103, 105, 102, 104], //      no default session; custom 22->06 starts here
  /*  3 */ [h1(23), 104, 106, 103, 105], //      no default session
  /*  4 */ [h2(0), 105, 107, 101, 102], //       Asia d2 + opening d2; high 107
  /*  5 */ [h2(1), 102, 103, 98, 99], //         Asia d2
  /*  6 */ [h2(2), 99, 107, 97, 106], //         Asia d2; high 107 TIES bar 4
  //         h2(3) missing: a gap inside Asia
  /*  7 */ [h2(4), 106, 106.5, 104, 105], //     Asia d2
  /*  8 */ [h2(5), 105, 105.5, 96, 97], //       Asia d2
  /*  9 */ [h2(6), 97, 99, 95, 98], //           Asia d2
  /* 10 */ [h2(7), 98, 100, 94, 99], //          Asia d2 (last) AND London d2 (first): overlap
  /* 11 */ [h2(8), 99, 108, 98, 107], //         London d2 (08:00 is Asia's exclusive end)
  /* 12 */ [h2(9), 107, 109, 106, 108], //       London d2
  /* 13 */ [h2(10), 108, 110, 107, 109], //      London d2
  /* 14 */ [h2(11), 109, 109.5, 103, 104], //    London d2
  /* 15 */ [h2(12), 104, 105, 101, 102], //      London d2
  /* 16 */ [h2(13), 102, 103, 93, 94], //        London d2 AND NY d2 (first): overlap
  /* 17 */ [h2(14), 94, 96, 92, 95], //          London d2 AND NY d2; low 92
  /* 18 */ [h2(15), 95, 111, 94, 110], //        London d2 (last) AND NY d2; high 111
  /* 19 */ [h2(16), 110, 112, 109, 111], //      NY d2 only (16:00 is London's exclusive end)
  /* 20 */ [h2(17), 111, 113, 108, 112], //      NY d2 (still open: it runs to 22:00)
]

export const FIXTURE_CANDLES: readonly Candle[] = ROWS.map(([time, open, high, low, close]) => ({
  time,
  open,
  high,
  low,
  close,
  volume: 1,
}))

/*
 * DEFAULTS: Asia 00-08, London 07-16, New York 13-22, opening range N = 1.
 *
 * Day 1: Asia [h1(0), h1(8)), London [h1(7), h1(16)) and opening [h1(0), h1(1)) hold no bars, so
 * they have NO box. NY [h1(13), h1(22)) holds bars 0, 1 (20:00, 21:00; 22:00 is excluded).
 *   NY d1: highs 102, 104 -> 104; lows 99, 100 -> 99; first h1(20), last h1(21), 2 bars.
 * Day 2:
 *   Asia [h2(0), h2(8)): bars 4..10 = 00, 01, 02, 04, 05, 06, 07 (03 missing) = 7 bars.
 *     highs 107, 103, 107, 106.5, 105.5, 99, 100 -> 107 (tie, bars 4 and 6, same value).
 *     lows 101, 98, 97, 104, 96, 95, 94 -> 94. first h2(0), last h2(7).
 *   Opening [h2(0), h2(1)): bar 4 only. high 107, low 101, first = last = h2(0), 1 bar.
 *   London [h2(7), h2(16)): bars 10..18 = 07..15 = 9 bars.
 *     highs 100, 108, 109, 110, 109.5, 105, 103, 96, 111 -> 111.
 *     lows 94, 98, 106, 107, 103, 101, 93, 92, 94 -> 92. first h2(7), last h2(15).
 *   NY [h2(13), h2(22)): bars 16..20 = 13..17 = 5 bars.
 *     highs 103, 96, 111, 112, 113 -> 113. lows 93, 92, 94, 109, 108 -> 92.
 *     first h2(13), last h2(17).
 * Order by start: NY d1 (h1(13)), then Asia d2 and opening d2 (both h2(0): Asia first), London d2
 * (h2(7)), NY d2 (h2(13)).
 */
export const EXPECTED_DEFAULTS: readonly SessionBox[] = [
  {
    session: 'newyork',
    start: h1(13),
    end: h1(22),
    high: 104,
    low: 99,
    firstTime: h1(20),
    lastTime: h1(21),
    bars: 2,
  },
  {
    session: 'asia',
    start: h2(0),
    end: h2(8),
    high: 107,
    low: 94,
    firstTime: h2(0),
    lastTime: h2(7),
    bars: 7,
  },
  {
    session: 'opening',
    start: h2(0),
    end: h2(1),
    high: 107,
    low: 101,
    firstTime: h2(0),
    lastTime: h2(0),
    bars: 1,
  },
  {
    session: 'london',
    start: h2(7),
    end: h2(16),
    high: 111,
    low: 92,
    firstTime: h2(7),
    lastTime: h2(15),
    bars: 9,
  },
  {
    session: 'newyork',
    start: h2(13),
    end: h2(22),
    high: 113,
    low: 92,
    firstTime: h2(13),
    lastTime: h2(17),
    bars: 5,
  },
]

/*
 * WRAPPING ASIA 22 -> 06 (duration (6 - 22 + 24) % 24 = 8 h), everything else default.
 *
 * Bar 0 (h1(20)): latest start <= t is h1(22) - 1 day = day 0 22:00, which ends at h1(6): not in.
 * Bar 1 (h1(21)): same, not in.
 * Instance [h1(22), h2(6)): bars 2..8 = 22, 23, 00, 01, 02, 04, 05 (03 missing) = 7 bars.
 *   highs 105, 106, 107, 103, 107, 106.5, 105.5 -> 107. lows 102, 103, 101, 98, 97, 104, 96 -> 96.
 *   first h1(22), last h2(5). h2(6) is the exclusive end.
 * Instance [h2(22), h3(6)): no bars, no box.
 * Order: NY d1 (h1(13)), Asia (h1(22)), opening d2 (h2(0)), London d2, NY d2.
 */
export const EXPECTED_ASIA_WRAP: readonly SessionBox[] = [
  EXPECTED_DEFAULTS[0],
  {
    session: 'asia',
    start: h1(22),
    end: h2(6),
    high: 107,
    low: 96,
    firstTime: h1(22),
    lastTime: h2(5),
    bars: 7,
  },
  EXPECTED_DEFAULTS[2],
  EXPECTED_DEFAULTS[3],
  EXPECTED_DEFAULTS[4],
]

/*
 * OPENING RANGE N = 3, everything else default: [h2(0), h2(3)) holds bars 4, 5, 6 (03 would be
 * outside anyway). highs 107, 103, 107 -> 107; lows 101, 98, 97 -> 97; first h2(0), last h2(2).
 */
export const EXPECTED_OPENING_3: readonly SessionBox[] = [
  EXPECTED_DEFAULTS[0],
  EXPECTED_DEFAULTS[1],
  {
    session: 'opening',
    start: h2(0),
    end: h2(3),
    high: 107,
    low: 97,
    firstTime: h2(0),
    lastTime: h2(2),
    bars: 3,
  },
  EXPECTED_DEFAULTS[3],
  EXPECTED_DEFAULTS[4],
]

/*
 * AS-OF-t SNAPSHOTS, DEFAULTS: the boxes after only bars 0..lastIndex have closed (what the chart
 * draws at that moment). Worked by hand from the table above.
 *
 * Through bar 3 (h1(23)): only NY d1 exists (EXPECTED_DEFAULTS[0]); Asia / opening d2 have no box.
 * Through bar 6 (h2(2)): Asia d2 = bars 4, 5, 6: highs 107, 103, 107 -> 107 (tie), lows 101, 98,
 *   97 -> 97, first h2(0), last h2(2), 3 bars. Opening d2 is complete (bar 4). No London, NY d2.
 * Through bar 7 (h2(4)): Asia d2 + bar 7 (high 106.5, low 104), after the missing 03:00:
 *   107 / 97, last h2(4), 4 bars (not 5).
 * Through bar 15 (h2(12)): Asia d2 and opening d2 complete. London d2 = bars 10..15:
 *   highs 100, 108, 109, 110, 109.5, 105 -> 110; lows 94, 98, 106, 107, 103, 101 -> 94;
 *   first h2(7), last h2(12), 6 bars. NY d2 not started (13:00).
 * Through bar 17 (h2(14)), inside the London / New York overlap: London d2 + bars 16, 17
 *   (highs 103, 96; lows 93, 92) -> 110 / 92, last h2(14), 8 bars. NY d2 = bars 16, 17:
 *   highs 103, 96 -> 103; lows 93, 92 -> 92; first h2(13), last h2(14), 2 bars.
 */
export interface SessionAsOf {
  /** Index of the last closed bar in FIXTURE_CANDLES. */
  lastIndex: number
  boxes: readonly SessionBox[]
}

export const EXPECTED_AS_OF_DEFAULTS: readonly SessionAsOf[] = [
  { lastIndex: 3, boxes: [EXPECTED_DEFAULTS[0]] },
  {
    lastIndex: 6,
    boxes: [
      EXPECTED_DEFAULTS[0],
      {
        session: 'asia',
        start: h2(0),
        end: h2(8),
        high: 107,
        low: 97,
        firstTime: h2(0),
        lastTime: h2(2),
        bars: 3,
      },
      EXPECTED_DEFAULTS[2],
    ],
  },
  {
    lastIndex: 7,
    boxes: [
      EXPECTED_DEFAULTS[0],
      {
        session: 'asia',
        start: h2(0),
        end: h2(8),
        high: 107,
        low: 97,
        firstTime: h2(0),
        lastTime: h2(4),
        bars: 4,
      },
      EXPECTED_DEFAULTS[2],
    ],
  },
  {
    lastIndex: 15,
    boxes: [
      EXPECTED_DEFAULTS[0],
      EXPECTED_DEFAULTS[1],
      EXPECTED_DEFAULTS[2],
      {
        session: 'london',
        start: h2(7),
        end: h2(16),
        high: 110,
        low: 94,
        firstTime: h2(7),
        lastTime: h2(12),
        bars: 6,
      },
    ],
  },
  {
    lastIndex: 17,
    boxes: [
      EXPECTED_DEFAULTS[0],
      EXPECTED_DEFAULTS[1],
      EXPECTED_DEFAULTS[2],
      {
        session: 'london',
        start: h2(7),
        end: h2(16),
        high: 110,
        low: 92,
        firstTime: h2(7),
        lastTime: h2(14),
        bars: 8,
      },
      {
        session: 'newyork',
        start: h2(13),
        end: h2(22),
        high: 103,
        low: 92,
        firstTime: h2(13),
        lastTime: h2(14),
        bars: 2,
      },
    ],
  },
]
