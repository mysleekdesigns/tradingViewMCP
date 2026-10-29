/**
 * Hand-built market structure fixture (Phase 16).
 *
 * PARITY SOURCE: this file is the reference for any future lab feature that reuses the swing /
 * BOS / ChoCh / flip-zone rules (e.g. a causal swing feature in `lab/lab/registry/`). A port must
 * reproduce every expected value below from FIXTURE_CANDLES, bit for bit. The rules are written
 * out in `web/README.md` ("Market structure overlay") and in `marketStructure.ts`.
 *
 * Every expected value was worked out by hand, from the table below, before the code ran. The
 * working is in the comments: (1) every pivot candidate, (2) the break walk, (3) the zone walk.
 * Edit nothing here without re-doing the hand working.
 *
 * Bar i has open time T0 + i * STEP (UNIX seconds, 4h bars). Times in the expected values are
 * written as t(i).
 */
import type { Candle } from '../types'
import type { FlipZone, MarketStructure, StructureEvent, SwingPoint } from './types'

export const T0 = 1_700_000_000
export const STEP = 14_400

/** Open time of fixture bar i. */
export function t(i: number): number {
  return T0 + i * STEP
}

/** [open, high, low, close] per bar, index = bar number. */
const OHLC: readonly (readonly [number, number, number, number])[] = [
  /*  0 */ [9, 10, 8, 9.5],
  /*  1 */ [9.5, 11, 9, 10.5],
  /*  2 */ [10.5, 12, 10, 11], //   swing high (2/2), price 12
  /*  3 */ [11, 11.5, 9.5, 10],
  /*  4 */ [10, 11, 8.5, 9],
  /*  5 */ [9, 10, 7, 7.5], //      swing low (2/2 and 5/5), price 7; equal low with bar 6
  /*  6 */ [7.5, 9, 7, 8], //       low 7 ties bar 5: strict left test fails, not a pivot
  /*  7 */ [8, 9.5, 7.5, 9],
  /*  8 */ [9, 10.5, 8.5, 10.4],
  /*  9 */ [10.4, 12, 9.5, 11.9], // swing high (2/2 and 5/5), price 12; equal high with bar 10
  /* 10 */ [11.9, 12, 10, 11.5], //  high 12 ties bar 9: strict left test fails, not a pivot
  /* 11 */ [11.5, 11.5, 9, 9.5],
  /* 12 */ [9.5, 10, 8, 8.5],
  /* 13 */ [8.5, 9, 6.5, 6.8], //    close 6.8 < 7: first break (BOS down from 'none')
  /* 14 */ [6.8, 7.2, 6, 6.2], //    retests resistance zone [7, 7.5]
  /* 15 */ [6.2, 7, 5.5, 5.8],
  /* 16 */ [5.8, 6.5, 5, 6], //      swing low (2/2 and 5/5), price 5
  /* 17 */ [6, 6.8, 5.4, 6.5],
  /* 18 */ [6.5, 7, 5.6, 6.9],
  /* 19 */ [6.9, 8, 6.5, 7.8], //    swing high (2/2), price 8; close 7.8 > 7.5 invalidates [7, 7.5]
  /* 20 */ [7.8, 7.8, 6.6, 7],
  /* 21 */ [7, 7.4, 6, 6.2],
  /* 22 */ [6.2, 7, 5.8, 6.5], //    swing low (2/2), price 5.8
  /* 23 */ [6.5, 8.4, 6.4, 8.2], //  close 8.2 > 8 while 'down': ChoCh up
  /* 24 */ [8.2, 9, 7.9, 8.8], //    retests support zone [7.8, 8]
  /* 25 */ [8.8, 10, 8.6, 9.8],
  /* 26 */ [9.8, 10.5, 9.4, 10.2], // swing high (2/2), price 10.5
  /* 27 */ [10.2, 10.2, 9, 9.4],
  /* 28 */ [9.4, 9.8, 8.9, 9.2], //  swing low (2/2), price 8.9
  /* 29 */ [9.2, 11, 9, 10.8], //    close 10.8 > 10.5 while 'up': BOS up
  /* 30 */ [10.8, 11.5, 10.6, 11.3],
  /* 31 */ [11.3, 12, 11, 11.8], //  swing high (2/2 and 5/5), price 12
  /* 32 */ [11.6, 11.6, 10.8, 11],
  /* 33 */ [11, 11.2, 10.7, 10.9],
  /* 34 */ [10.9, 11, 9.5, 10], //   swing low (2/2), price 9.5; retest + invalidation of [10.2, 10.5]
  /* 35 */ [10, 10.4, 9.6, 10.2],
  /* 36 */ [10.2, 10.8, 10, 10.6],
  /* 37 */ [10.6, 10.6, 9.2, 9.3], // close 9.3 < 9.5 while 'up': ChoCh down
  /* 38 */ [9.3, 9.8, 8.6, 8.8], //  retests resistance zone [9.5, 10]; close < 8.9 (superseded swing)
  /* 39 */ [8.8, 9.4, 8.2, 8.5], //  swing low (2/2), price 8.2; close < 8.9 (superseded swing)
  /* 40 */ [8.5, 9, 8.3, 8.9],
  /* 41 */ [8.9, 9.2, 8.5, 9.1],
  /* 42 */ [8.6, 8.6, 7.9, 8], //    close 8 < 8.2 while 'down': BOS down; drops [7.8, 8] at maxZones 2
  /* 43 */ [8, 8.4, 8, 8.3], //      retests resistance zone [8.2, 8.5]
]

/** The fixture as closed candles (volume 1 each; volume plays no part in the rules). */
export const FIXTURE_CANDLES: readonly Candle[] = OHLC.map(([open, high, low, close], i) => ({
  time: t(i),
  open,
  high,
  low,
  close,
  volume: 1,
}))

/*
 * ---------------------------------------------------------------------------------------------
 * HAND WORKING, left = 2, right = 2.
 *
 * Highs: 0:10 1:11 2:12 3:11.5 4:11 5:10 6:9 7:9.5 8:10.5 9:12 10:12 11:11.5 12:10 13:9 14:7.2
 *   15:7 16:6.5 17:6.8 18:7 19:8 20:7.8 21:7.4 22:7 23:8.4 24:9 25:10 26:10.5 27:10.2 28:9.8
 *   29:11 30:11.5 31:12 32:11.6 33:11.2 34:11 35:10.4 36:10.8 37:10.6 38:9.8 39:9.4 40:9 41:9.2
 *   42:8.6 43:8.4
 * Lows: 0:8 1:9 2:10 3:9.5 4:8.5 5:7 6:7 7:7.5 8:8.5 9:9.5 10:10 11:9 12:8 13:6.5 14:6 15:5.5
 *   16:5 17:5.4 18:5.6 19:6.5 20:6.6 21:6 22:5.8 23:6.4 24:7.9 25:8.6 26:9.4 27:9 28:8.9 29:9
 *   30:10.6 31:11 32:10.8 33:10.7 34:9.5 35:9.6 36:10 37:9.2 38:8.6 39:8.2 40:8.3 41:8.5 42:7.9
 *   43:8
 * Closes: 0:9.5 1:10.5 2:11 3:10 4:9 5:7.5 6:8 7:9 8:10.4 9:11.9 10:11.5 11:9.5 12:8.5 13:6.8
 *   14:6.2 15:5.8 16:6 17:6.5 18:6.9 19:7.8 20:7 21:6.2 22:6.5 23:8.2 24:8.8 25:9.8 26:10.2
 *   27:9.4 28:9.2 29:10.8 30:11.3 31:11.8 32:11 33:10.9 34:10 35:10.2 36:10.6 37:9.3 38:8.8
 *   39:8.5 40:8.9 41:9.1 42:8 43:8.3
 *
 * (1) Pivots, p = 2..41 (p >= left; confirmed at p + 2 <= 43). "x" = first failing test.
 * Swing highs (strictly above the 2 before, at or above the 2 after):
 *   p=2  12 > 11, 10; 12 >= 11.5, 11                 -> HIGH, confirm 4
 *   p=3  11.5 > 12 x    p=4 11 > 11.5 x    p=5 10 > 11 x    p=6 9 > 10 x    p=7 9.5 > 10 x
 *   p=8  10.5 > 9.5, 9; 10.5 >= 12 x
 *   p=9  12 > 10.5, 9.5; 12 >= 12 (tie allowed on the right), 11.5   -> HIGH, confirm 11
 *   p=10 12 > 12 x (strict-left tie: the later bar of the equal-high pair is not a pivot)
 *   p=11 11.5 > 12 x   p=12 10 > 11.5 x   p=13..16 below bar before x   p=17 6.8 > 7 x
 *   p=18 7 > 6.8, 6.5; 7 >= 8 x
 *   p=19 8 > 7, 6.8; 8 >= 7.8, 7.4                   -> HIGH, confirm 21
 *   p=20 7.8 > 8 x     p=21 7.4 > 7.8 x   p=22 7 > 7.4 x
 *   p=23 8.4 >= 9 x    p=24 9 >= 10 x     p=25 10 >= 10.5 x
 *   p=26 10.5 > 10, 9; 10.5 >= 10.2, 9.8             -> HIGH, confirm 28
 *   p=27 10.2 > 10.5 x p=28 9.8 > 10.2 x  p=29 11 >= 11.5 x  p=30 11.5 >= 12 x
 *   p=31 12 > 11.5, 11; 12 >= 11.6, 11.2             -> HIGH, confirm 33
 *   p=32..35 below bar before x   p=36 10.8 > 11 x (bar 34)   p=37 10.6 > 10.8 x
 *   p=38..40 below bar before x   p=41 9.2 > 9.4 x (bar 39)
 * Swing lows (strictly below the 2 before, at or below the 2 after):
 *   p=2 10 < 9 x       p=3 9.5 < 9 x      p=4 8.5 <= 7 x
 *   p=5  7 < 8.5, 9.5; 7 <= 7 (tie allowed on the right), 7.5  -> LOW, confirm 7
 *   p=6  7 < 7 x (strict-left tie)        p=7..10 above bar before x
 *   p=11 9 <= 8 x      p=12 8 <= 6.5 x    p=13 6.5 <= 6 x    p=14 6 <= 5.5 x   p=15 5.5 <= 5 x
 *   p=16 5 < 5.5, 6; 5 <= 5.4, 5.6                   -> LOW, confirm 18
 *   p=17 5.4 < 5 x     p=18..20 above bar before x   p=21 6 <= 5.8 x
 *   p=22 5.8 < 6, 6.6; 5.8 <= 6.4, 7.9               -> LOW, confirm 24
 *   p=23..26 above bar before x   p=27 9 < 8.6 x
 *   p=28 8.9 < 9, 9.4; 8.9 <= 9, 10.6                -> LOW, confirm 30
 *   p=29..31 above bar before x   p=32 10.8 < 10.6 x  p=33 10.7 <= 9.5 x
 *   p=34 9.5 < 10.7, 10.8; 9.5 <= 9.6, 10            -> LOW, confirm 36
 *   p=35 9.6 < 9.5 x   p=36 10 x          p=37 9.2 <= 8.6 x  p=38 8.6 <= 8.2 x
 *   p=39 8.2 < 8.6, 9.2; 8.2 <= 8.3, 8.5             -> LOW, confirm 41
 *   p=40 8.3 < 8.2 x   p=41 8.5 x
 *
 * (2) Break walk. H / L = latest confirmed swing high / low (confirmed at a bar before t).
 *   bars 5..10  H = high@2 (12): closes <= 11.9, no break. Bar 11 confirms high@9 (12), new H.
 *               high@2 is superseded and never broken.
 *   bars 8..12  L = low@5 (7): closes >= 8.5.
 *   bar 13      close 6.8 < 7: state 'none' -> BOS down (first break is a BOS); low@5 broken.
 *               Resistance zone from bar 5: bottom = low 7, top = min(open 9, close 7.5) = 7.5.
 *   bars 12..   H = high@9 (12) never closed above (max close after is 11.8) -> never broken.
 *   bar 18      confirms low@16 (5); no close after goes below 5 -> never broken.
 *   bar 21      confirms high@19 (8), new H (high@9 superseded).
 *   bar 23      close 8.2 > 8 while 'down' -> ChoCh up; high@19 broken.
 *               Support zone from bar 19: top = high 8, bottom = max(open 6.9, close 7.8) = 7.8.
 *   bar 24      confirms low@22 (5.8), new L; no close goes below 5.8 before bar 30.
 *   bar 28      confirms high@26 (10.5), new H.
 *   bar 29      close 10.8 > 10.5 while 'up' -> BOS up; high@26 broken.
 *               Support zone from bar 26: top = 10.5, bottom = max(open 9.8, close 10.2) = 10.2.
 *   bar 30      confirms low@28 (8.9), new L. Closes 30..36 are all >= 10 > 8.9.
 *   bar 33      confirms high@31 (12), new H; no close after goes above 12 -> never broken.
 *   bar 36      confirms low@34 (9.5), new L; low@28 (8.9) is superseded, unbroken.
 *   bar 37      close 9.3 < 9.5 while 'up' -> ChoCh down; low@34 broken.
 *               Resistance zone from bar 34: bottom = low 9.5, top = min(open 10.9, close 10) = 10.
 *   bars 38, 39 close 8.8 and 8.5 are below the superseded low@28 (8.9): NO event, because only
 *               the latest swing low (low@34, already broken) can be broken.
 *   bar 41      confirms low@39 (8.2), new L.
 *   bar 42      close 8 < 8.2 while 'down' -> BOS down; low@39 broken.
 *               Resistance zone from bar 39: bottom = 8.2, top = min(open 8.8, close 8.5) = 8.5.
 *   Final state: 'down'.
 *
 * (3) Zone walk (step 1 runs before the breaks, on zones created at earlier bars).
 *   Z1 resistance [7, 7.5], created 13.
 *     bar 14 high 7.2 >= 7 and low 6 <= 7.5 -> first retest 14; close 6.2 not > 7.5.
 *     bars 15..18 closes 5.8, 6, 6.5, 6.9 not > 7.5. bar 19 close 7.8 > 7.5 -> invalidated 19.
 *   Z2 support [7.8, 8], created 23.
 *     bar 24 high 9 >= 7.8 and low 7.9 <= 8 -> first retest 24; close 8.8 not < 7.8.
 *     No later close goes below 7.8 (lowest is 8 at bar 42).
 *   Z3 support [10.2, 10.5], created 29. Active zones now Z2, Z3.
 *     bars 30..33 lows 10.6, 11, 10.8, 10.7 all > 10.5: no retest.
 *     bar 34 high 11 >= 10.2 and low 9.5 <= 10.5 -> first retest 34, and close 10 < 10.2 ->
 *     invalidated 34 (the same bar).
 *   Z4 resistance [9.5, 10], created 37. Active zones Z2, Z4.
 *     bar 38 high 9.8 >= 9.5 and low 8.6 <= 10 -> first retest 38. No later close above 10.
 *   Z5 resistance [8.2, 8.5], created 42.
 *     maxZones 2: Z2 and Z4 are active (2 = maxZones), so the oldest, Z2, ends at 42 'dropped'
 *     before Z5 is added. maxZones 10: nothing is dropped and Z2 stays active.
 *     bar 43 high 8.4 >= 8.2 and low 8 <= 8.5 -> first retest 43; close 8.3 not > 8.5.
 *
 * ---------------------------------------------------------------------------------------------
 * HAND WORKING, defaults left = 5, right = 5, maxZones = 10. Pivots p = 5..38.
 *   Swing highs: p=9 12 > 11, 10, 9, 9.5, 10.5 (bars 4..8); 12 >= 12, 11.5, 10, 9, 7.2 -> HIGH,
 *     confirm 14. p=31 12 > 10.5, 10.2, 9.8, 11, 11.5; 12 >= 11.6, 11.2, 11, 10.4, 10.8 -> HIGH,
 *     confirm 36. Every other p fails: p=19 8 >= 8.4 (bar 23) x, p=23 8.4 >= 9 x,
 *     p=24 9 >= 10 x, p=25 10 >= 10.5 x, p=26 10.5 >= 11 (bar 29) x, p=29 11 >= 11.5 x,
 *     p=30 11.5 >= 12 x, p=8 10.5 > 11.5 (bar 3) x, p=10 12 > 12 x, the rest are below a bar
 *     in the 5 before. (Bar 2 cannot be a pivot: it has only 2 bars before it.)
 *   Swing lows: p=5 7 < 8, 9, 10, 9.5, 8.5; 7 <= 7, 7.5, 8.5, 9.5, 10 -> LOW, confirm 10.
 *     p=16 5 < 9, 8, 6.5, 6, 5.5; 5 <= 5.4, 5.6, 6.5, 6.6, 6 -> LOW, confirm 21.
 *     Every other p fails: p=22 5.8 < 5.4 (bar 17) x, p=28 8.9 < 6.4 (bar 23) x,
 *     p=34 9.5 < 9 (bar 29) x, p=37 9.2 <= 8.6 (bar 38) x, p=38 8.6 <= 8.2 x, p=13 6.5 <= 6 x,
 *     p=14 6 <= 5.5 x, p=15 5.5 <= 5 x; bar 39 would need bar 44.
 *   Breaks: bar 13 close 6.8 < 7 (low@5, confirmed 10) -> BOS down. high@9 (12, from bar 15) and
 *     high@31 (12, from bar 37) are never closed above; low@16 (5, from bar 22) never closed
 *     below. Final state 'down'.
 *   Zones: Z1 resistance [7, 7.5] created 13, retest 14, invalidated 19 (as above).
 */

function swing(
  kind: 'high' | 'low',
  pivot: number,
  confirm: number,
  price: number,
  broken: number | null,
): SwingPoint {
  return {
    kind,
    pivotTime: t(pivot),
    confirmTime: t(confirm),
    price,
    brokenTime: broken === null ? null : t(broken),
  }
}

function event(
  kind: 'bos' | 'choch',
  direction: 'up' | 'down',
  at: number,
  level: number,
  pivot: number,
): StructureEvent {
  return { kind, direction, time: t(at), level, swingPivotTime: t(pivot) }
}

function zone(
  side: 'support' | 'resistance',
  bottom: number,
  top: number,
  pivot: number,
  created: number,
  retest: number | null,
  end: number | null,
  endReason: FlipZone['endReason'],
): FlipZone {
  return {
    side,
    top,
    bottom,
    pivotTime: t(pivot),
    createdTime: t(created),
    retestTime: retest === null ? null : t(retest),
    endTime: end === null ? null : t(end),
    endReason,
  }
}

const SWINGS_2_2: readonly SwingPoint[] = [
  swing('high', 2, 4, 12, null), //    superseded at bar 11, never broken
  swing('low', 5, 7, 7, 13), //        equal-low pair 5/6: bar 5 is the pivot
  swing('high', 9, 11, 12, null), //   equal-high pair 9/10: bar 9 is the pivot; superseded at 21
  swing('low', 16, 18, 5, null), //    superseded at 24
  swing('high', 19, 21, 8, 23),
  swing('low', 22, 24, 5.8, null), //  superseded at 30
  swing('high', 26, 28, 10.5, 29),
  swing('low', 28, 30, 8.9, null), //  superseded at 36; closes at 38, 39 below it break nothing
  swing('high', 31, 33, 12, null), //  still the latest high, never closed above
  swing('low', 34, 36, 9.5, 37),
  swing('low', 39, 41, 8.2, 42),
]

const EVENTS_2_2: readonly StructureEvent[] = [
  event('bos', 'down', 13, 7, 5), //      first break from 'none'
  event('choch', 'up', 23, 8, 19),
  event('bos', 'up', 29, 10.5, 26),
  event('choch', 'down', 37, 9.5, 34),
  event('bos', 'down', 42, 8.2, 39),
]

/** left 2, right 2, maxZones 2: Z2 is dropped at bar 42. */
export const EXPECTED_2_2_MAX2: MarketStructure = {
  swings: [...SWINGS_2_2],
  events: [...EVENTS_2_2],
  zones: [
    zone('resistance', 7, 7.5, 5, 13, 14, 19, 'invalidated'),
    zone('support', 7.8, 8, 19, 23, 24, 42, 'dropped'),
    zone('support', 10.2, 10.5, 26, 29, 34, 34, 'invalidated'), // retest + invalidation, one bar
    zone('resistance', 9.5, 10, 34, 37, 38, null, null),
    zone('resistance', 8.2, 8.5, 39, 42, 43, null, null),
  ],
  state: 'down',
}

/** left 2, right 2, maxZones 10: as above, but nothing is dropped. */
export const EXPECTED_2_2_MAX10: MarketStructure = {
  swings: [...SWINGS_2_2],
  events: [...EVENTS_2_2],
  zones: [
    zone('resistance', 7, 7.5, 5, 13, 14, 19, 'invalidated'),
    zone('support', 7.8, 8, 19, 23, 24, null, null),
    zone('support', 10.2, 10.5, 26, 29, 34, 34, 'invalidated'),
    zone('resistance', 9.5, 10, 34, 37, 38, null, null),
    zone('resistance', 8.2, 8.5, 39, 42, 43, null, null),
  ],
  state: 'down',
}

/** Defaults: left 5, right 5, maxZones 10. */
export const EXPECTED_DEFAULTS: MarketStructure = {
  swings: [
    swing('low', 5, 10, 7, 13),
    swing('high', 9, 14, 12, null),
    swing('low', 16, 21, 5, null),
    swing('high', 31, 36, 12, null),
  ],
  events: [event('bos', 'down', 13, 7, 5)],
  zones: [zone('resistance', 7, 7.5, 5, 13, 14, 19, 'invalidated')],
  state: 'down',
}
