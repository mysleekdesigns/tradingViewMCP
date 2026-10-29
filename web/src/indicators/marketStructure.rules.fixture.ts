/**
 * Hand-built market structure RULE cases (Phase 16).
 *
 * PARITY SOURCE, together with `marketStructure.fixture.ts`: any future lab feature that reuses
 * the swing / BOS / ChoCh / flip-zone rules must reproduce every expected value below, bit for
 * bit, as well as the 44-bar fixture. The rules are written out in `web/README.md` ("Market
 * structure overlay") and in `marketStructure.ts`.
 *
 * Each case is a few bars long and pins ONE rule that the 44-bar fixture leaves loose. Every
 * expected value was worked out by hand from the OHLC table in the case's comment. Notation:
 * bar i is [open, high, low, close]; "HIGH c4" = swing high confirmed at bar 4; "x" = the first
 * failing pivot test. With left = right = 1 a swing high at p needs high[p] > high[p-1] and
 * high[p] >= high[p+1] (p >= 1) and is confirmed at p + 1; a swing low is mirrored.
 * Edit nothing here without re-doing the hand working.
 *
 * Bar i has open time R0 + i * STEP (UNIX seconds, 4h bars); times are written as rt(i).
 */
import type { Candle } from '../types'
import type { MarketStructureParams } from './marketStructure'
import type { FlipZone, MarketStructure, StructureEvent, SwingPoint } from './types'

export const R0 = 1_710_000_000
export const STEP = 14_400

/** Open time of bar i of a rule case. */
export function rt(i: number): number {
  return R0 + i * STEP
}

export interface RuleCase {
  /** Short id used in test names. */
  name: string
  /** The rule this case pins. */
  rule: string
  params: MarketStructureParams
  candles: readonly Candle[]
  expected: MarketStructure
}

type Ohlc = readonly [number, number, number, number]

function bars(rows: readonly Ohlc[]): readonly Candle[] {
  return rows.map(([open, high, low, close], i) => ({
    time: rt(i),
    open,
    high,
    low,
    close,
    volume: 1,
  }))
}

function swing(
  kind: 'high' | 'low',
  pivot: number,
  confirm: number,
  price: number,
  broken: number | null,
): SwingPoint {
  return {
    kind,
    pivotTime: rt(pivot),
    confirmTime: rt(confirm),
    price,
    brokenTime: broken === null ? null : rt(broken),
  }
}

function event(
  kind: 'bos' | 'choch',
  direction: 'up' | 'down',
  at: number,
  level: number,
  pivot: number,
): StructureEvent {
  return { kind, direction, time: rt(at), level, swingPivotTime: rt(pivot) }
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
    pivotTime: rt(pivot),
    createdTime: rt(created),
    retestTime: retest === null ? null : rt(retest),
    endTime: end === null ? null : rt(end),
    endReason,
  }
}

const P11: MarketStructureParams = { left: 1, right: 1, maxZones: 10 }
const P11_ONE_ZONE: MarketStructureParams = { left: 1, right: 1, maxZones: 1 }

/*
 * Shared prefixes. UP_PREFIX makes swing high@1 (12) and breaks it up at bar 3; DOWN_PREFIX
 * makes swing low@1 (8) and breaks it down at bar 3. Their working is repeated in each case.
 */
const UP_PREFIX: readonly Ohlc[] = [
  /* 0 */ [9, 10, 8, 9.5],
  /* 1 */ [9.5, 12, 9, 11], //    swing high 12; support band [max(9.5, 11), 12] = [11, 12]
  /* 2 */ [11, 11, 9.5, 10],
  /* 3 */ [10, 12.6, 9.8, 12.5], // close 12.5 > 12: BOS up
]
const DOWN_PREFIX: readonly Ohlc[] = [
  /* 0 */ [11, 12, 10, 10.5],
  /* 1 */ [10.5, 11, 8, 9], //    swing low 8; resistance band [8, min(10.5, 9)] = [8, 9]
  /* 2 */ [9, 10.5, 8.5, 10],
  /* 3 */ [10, 10.2, 7.4, 7.5], // close 7.5 < 8: BOS down
]

/*
 * 1a. Closes, not wicks (high side). left 1, right 1, maxZones 10.
 *   0 [9, 10, 8, 9.5]  1 [9.5, 12, 9, 11]  2 [11, 11, 9.5, 10]  3 [10, 13, 9.8, 11.5]
 *   4 [11.5, 12.8, 11.2, 12.5]
 * Pivots p = 1..3. Highs 10 12 11 13 12.8: p=1 12 > 10, 12 >= 11 -> HIGH c2; p=2 11 > 12 x;
 *   p=3 13 > 11, 13 >= 12.8 -> HIGH c4. Lows 8 9 9.5 9.8 11.2: p=1 9 < 8 x; p=2 9.5 < 9 x;
 *   p=3 9.8 < 9.5 x.
 * Bar 3: H = high@1 (12). High 13 > 12 but close 11.5 <= 12: a wick, NO event.
 * Bar 4: close 12.5 > 12, state 'none' -> BOS up; high@1 broken at 4. Support zone from bar 1:
 *   top 12, bottom max(9.5, 11) = 11, created 4. Then step 3 confirms high@3 (13).
 * Mutant `bar.high > price`: the event moves to bar 3 (and bar 4 retests the zone).
 */
export const CASE_CLOSE_NOT_WICK_HIGH: RuleCase = {
  name: 'close-not-wick-high',
  rule: 'breaks use closes, not highs',
  params: P11,
  candles: bars([
    [9, 10, 8, 9.5],
    [9.5, 12, 9, 11],
    [11, 11, 9.5, 10],
    [10, 13, 9.8, 11.5],
    [11.5, 12.8, 11.2, 12.5],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 12, 4), swing('high', 3, 4, 13, null)],
    events: [event('bos', 'up', 4, 12, 1)],
    zones: [zone('support', 11, 12, 1, 4, null, null, null)],
    state: 'up',
  },
}

/*
 * 1b. Closes, not wicks (low side, mirrored). left 1, right 1, maxZones 10.
 *   0 [11, 12, 10, 10.5]  1 [10.5, 11, 8, 9]  2 [9, 10.5, 8.5, 10]  3 [10, 10.2, 7, 8.5]
 *   4 [8.5, 9, 7.5, 7.8]
 * Highs 12 11 10.5 10.2 9: p=1 11 > 12 x; p=2 10.5 > 11 x; p=3 10.2 > 10.5 x.
 * Lows 10 8 8.5 7 7.5: p=1 8 < 10, 8 <= 8.5 -> LOW c2; p=2 8.5 < 8 x; p=3 7 < 8.5, 7 <= 7.5 ->
 *   LOW c4.
 * Bar 3: L = low@1 (8). Low 7 < 8 but close 8.5 >= 8: a wick, NO event.
 * Bar 4: close 7.8 < 8, state 'none' -> BOS down; low@1 broken at 4. Resistance zone from bar 1:
 *   bottom 8, top min(10.5, 9) = 9, created 4. Then step 3 confirms low@3 (7).
 * Mutant `bar.low < price`: the event moves to bar 3.
 */
export const CASE_CLOSE_NOT_WICK_LOW: RuleCase = {
  name: 'close-not-wick-low',
  rule: 'breaks use closes, not lows',
  params: P11,
  candles: bars([
    [11, 12, 10, 10.5],
    [10.5, 11, 8, 9],
    [9, 10.5, 8.5, 10],
    [10, 10.2, 7, 8.5],
    [8.5, 9, 7.5, 7.8],
  ]),
  expected: {
    swings: [swing('low', 1, 2, 8, 4), swing('low', 3, 4, 7, null)],
    events: [event('bos', 'down', 4, 8, 1)],
    zones: [zone('resistance', 8, 9, 1, 4, null, null, null)],
    state: 'down',
  },
}

/*
 * 2a. Break tie (high side): a close exactly on the swing price is not a break.
 *   0 [9, 10, 8, 9.5]  1 [9.5, 12, 9, 11]  2 [11, 11, 9.5, 10]  3 [10, 12, 9.8, 12]
 *   4 [12, 12.4, 11.5, 12.2]
 * Highs 10 12 11 12 12.4: p=1 HIGH c2; p=2 11 > 12 x; p=3 12 > 11, 12 >= 12.4 x.
 * Lows 8 9 9.5 9.8 11.5: p=1 9 < 8 x; p=2 9.5 < 9 x; p=3 9.8 < 9.5 x.
 * Bar 3: close 12 = H 12, not > 12: NO event. Bar 4: close 12.2 > 12 -> BOS up at 4. Support
 *   zone from bar 1: [11, 12], created 4.
 * Mutant `close >= price`: the event moves to bar 3 (and bar 4 retests the zone).
 */
export const CASE_BREAK_TIE_HIGH: RuleCase = {
  name: 'break-tie-high',
  rule: 'close equal to a swing high is not a break',
  params: P11,
  candles: bars([
    [9, 10, 8, 9.5],
    [9.5, 12, 9, 11],
    [11, 11, 9.5, 10],
    [10, 12, 9.8, 12],
    [12, 12.4, 11.5, 12.2],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 12, 4)],
    events: [event('bos', 'up', 4, 12, 1)],
    zones: [zone('support', 11, 12, 1, 4, null, null, null)],
    state: 'up',
  },
}

/*
 * 2b. Break tie (low side).
 *   0 [11, 12, 10, 10.5]  1 [10.5, 11, 8, 9]  2 [9, 10.5, 8.5, 10]  3 [10, 10.2, 8, 8]
 *   4 [8, 8.6, 7.6, 7.8]
 * Highs 12 11 10.5 10.2 8.6: p=1..3 each below the bar before x.
 * Lows 10 8 8.5 8 7.6: p=1 LOW c2; p=2 8.5 < 8 x; p=3 8 < 8.5, 8 <= 7.6 x.
 * Bar 3: close 8 = L 8, not < 8: NO event. Bar 4: close 7.8 < 8 -> BOS down at 4. Resistance zone
 *   from bar 1: [8, 9], created 4.
 * Mutant `close <= price`: the event moves to bar 3 (and bar 4 retests the zone).
 */
export const CASE_BREAK_TIE_LOW: RuleCase = {
  name: 'break-tie-low',
  rule: 'close equal to a swing low is not a break',
  params: P11,
  candles: bars([
    [11, 12, 10, 10.5],
    [10.5, 11, 8, 9],
    [9, 10.5, 8.5, 10],
    [10, 10.2, 8, 8],
    [8, 8.6, 7.6, 7.8],
  ]),
  expected: {
    swings: [swing('low', 1, 2, 8, 4)],
    events: [event('bos', 'down', 4, 8, 1)],
    zones: [zone('resistance', 8, 9, 1, 4, null, null, null)],
    state: 'down',
  },
}

/*
 * 3a. Retest edge tie, `low <= top`: a bar whose low equals a support zone's top retests it.
 *   0..3 UP_PREFIX  4 [12.5, 13.5, 12.4, 13.2]  5 [13.2, 13.4, 12, 12.6]
 * Highs 10 12 11 12.6 13.5 13.4: p=1 HIGH c2; p=2 x; p=3 12.6 > 11, 12.6 >= 13.5 x;
 *   p=4 13.5 > 12.6, 13.5 >= 13.4 -> HIGH c5.
 * Lows 8 9 9.5 9.8 12.4 12: p=1 9 < 8 x; p=2 9.5 < 9 x; p=3 9.8 < 9.5 x; p=4 12.4 < 9.8 x.
 * Bar 3: close 12.5 > 12 -> BOS up; support zone [11, 12] from bar 1, created 3.
 * Bar 4: low 12.4 > top 12: no retest. Bar 5: high 13.4 >= 11 and low 12 <= 12 (tie) -> first
 *   retest 5; close 12.6 not < 11. H is high@1 (broken) on bar 5; high@4 confirms after step 2.
 * Mutant strict retest (`low < top`): retestTime stays null.
 */
export const CASE_RETEST_TIE_SUPPORT: RuleCase = {
  name: 'retest-tie-support',
  rule: 'low equal to a zone top is a retest',
  params: P11,
  candles: bars([...UP_PREFIX, [12.5, 13.5, 12.4, 13.2], [13.2, 13.4, 12, 12.6]]),
  expected: {
    swings: [swing('high', 1, 2, 12, 3), swing('high', 4, 5, 13.5, null)],
    events: [event('bos', 'up', 3, 12, 1)],
    zones: [zone('support', 11, 12, 1, 3, 5, null, null)],
    state: 'up',
  },
}

/*
 * 3b. Retest edge tie, `high >= bottom`: a bar whose high equals a resistance zone's bottom
 * retests it.
 *   0..3 DOWN_PREFIX  4 [7.5, 7.6, 6.5, 6.8]  5 [6.8, 8, 6.6, 7.4]
 * Highs 12 11 10.5 10.2 7.6 8: p=1..4 each below the bar before x.
 * Lows 10 8 8.5 7.4 6.5 6.6: p=1 LOW c2; p=2 x; p=3 7.4 < 8.5, 7.4 <= 6.5 x;
 *   p=4 6.5 < 7.4, 6.5 <= 6.6 -> LOW c5.
 * Bar 3: close 7.5 < 8 -> BOS down; resistance zone [8, 9] from bar 1, created 3.
 * Bar 4: high 7.6 < bottom 8: no retest. Bar 5: high 8 >= 8 (tie) and low 6.6 <= 9 -> first
 *   retest 5; close 7.4 not > 9.
 * Mutant strict retest (`high > bottom`): retestTime stays null.
 */
export const CASE_RETEST_TIE_RESISTANCE: RuleCase = {
  name: 'retest-tie-resistance',
  rule: 'high equal to a zone bottom is a retest',
  params: P11,
  candles: bars([...DOWN_PREFIX, [7.5, 7.6, 6.5, 6.8], [6.8, 8, 6.6, 7.4]]),
  expected: {
    swings: [swing('low', 1, 2, 8, 3), swing('low', 4, 5, 6.5, null)],
    events: [event('bos', 'down', 3, 8, 1)],
    zones: [zone('resistance', 8, 9, 1, 3, 5, null, null)],
    state: 'down',
  },
}

/*
 * 4a. Invalidation tie (support): a close exactly on the far edge (bottom) does not invalidate.
 *   0..3 UP_PREFIX  4 [12.5, 12.8, 10.8, 11]  5 [11, 11.6, 10.5, 10.9]
 * Highs 10 12 11 12.6 12.8 11.6: p=1 HIGH c2; p=2 x; p=3 12.6 >= 12.8 x;
 *   p=4 12.8 > 12.6, 12.8 >= 11.6 -> HIGH c5.
 * Lows 8 9 9.5 9.8 10.8 10.5: p=1..4 each above the bar before x.
 * Bar 3: BOS up; support zone [11, 12], created 3.
 * Bar 4: high 12.8 >= 11 and low 10.8 <= 12 -> first retest 4; close 11 = bottom, not < 11: stays.
 * Bar 5: close 10.9 < 11 -> invalidated 5.
 * Mutant `close <= bottom`: invalidated at 4 instead.
 */
export const CASE_INVALIDATION_TIE_SUPPORT: RuleCase = {
  name: 'invalidation-tie-support',
  rule: 'close equal to a support bottom does not invalidate',
  params: P11,
  candles: bars([...UP_PREFIX, [12.5, 12.8, 10.8, 11], [11, 11.6, 10.5, 10.9]]),
  expected: {
    swings: [swing('high', 1, 2, 12, 3), swing('high', 4, 5, 12.8, null)],
    events: [event('bos', 'up', 3, 12, 1)],
    zones: [zone('support', 11, 12, 1, 3, 4, 5, 'invalidated')],
    state: 'up',
  },
}

/*
 * 4b. Invalidation tie (resistance): a close exactly on the far edge (top) does not invalidate.
 *   0..3 DOWN_PREFIX  4 [7.5, 9.2, 7.3, 9]  5 [9, 9.5, 8.8, 9.1]
 * Highs 12 11 10.5 10.2 9.2 9.5: p=1..4 each below the bar before x.
 * Lows 10 8 8.5 7.4 7.3 8.8: p=1 LOW c2; p=2 x; p=3 7.4 < 8.5, 7.4 <= 7.3 x;
 *   p=4 7.3 < 7.4, 7.3 <= 8.8 -> LOW c5.
 * Bar 3: BOS down; resistance zone [8, 9], created 3.
 * Bar 4: high 9.2 >= 8 and low 7.3 <= 9 -> first retest 4; close 9 = top, not > 9: stays.
 * Bar 5: close 9.1 > 9 -> invalidated 5.
 * Mutant `close >= top`: invalidated at 4 instead.
 */
export const CASE_INVALIDATION_TIE_RESISTANCE: RuleCase = {
  name: 'invalidation-tie-resistance',
  rule: 'close equal to a resistance top does not invalidate',
  params: P11,
  candles: bars([...DOWN_PREFIX, [7.5, 9.2, 7.3, 9], [9, 9.5, 8.8, 9.1]]),
  expected: {
    swings: [swing('low', 1, 2, 8, 3), swing('low', 4, 5, 7.3, null)],
    events: [event('bos', 'down', 3, 8, 1)],
    zones: [zone('resistance', 8, 9, 1, 3, 4, 5, 'invalidated')],
    state: 'down',
  },
}

/*
 * 5. Step order: breaks (step 2) run before the swings confirmed on the same bar (step 3).
 *   0 [8, 9, 7, 8.5]  1 [8.5, 10, 8, 9]  2 [9, 9.5, 8.2, 9.2]  3 [9.2, 12, 9, 9.8]
 *   4 [9.8, 11.5, 9.6, 11]
 * Highs 9 10 9.5 12 11.5: p=1 10 > 9, 10 >= 9.5 -> HIGH c2; p=2 9.5 > 10 x;
 *   p=3 12 > 9.5, 12 >= 11.5 -> HIGH c4. Lows 7 8 8.2 9 9.6: p=1..3 each above the bar before x.
 * Bar 3: H = high@1 (10); close 9.8 <= 10, no event (high 12 is a wick).
 * Bar 4 (= pivot 3 + right): step 2 first, against H = high@1 (10): close 11 > 10 -> BOS up at 4;
 *   support zone from bar 1: top 10, bottom max(8.5, 9) = 9. Only then step 3 confirms high@3
 *   (12) as the new H.
 * Mutant "confirm swings first": H would be high@3 (12) on bar 4, close 11 < 12, no event at all.
 */
export const CASE_BREAKS_BEFORE_NEW_SWINGS: RuleCase = {
  name: 'breaks-before-new-swings',
  rule: 'breaks are checked against the swings confirmed before this bar',
  params: P11,
  candles: bars([
    [8, 9, 7, 8.5],
    [8.5, 10, 8, 9],
    [9, 9.5, 8.2, 9.2],
    [9.2, 12, 9, 9.8],
    [9.8, 11.5, 9.6, 11],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 10, 4), swing('high', 3, 4, 12, null)],
    events: [event('bos', 'up', 4, 10, 1)],
    zones: [zone('support', 9, 10, 1, 4, null, null, null)],
    state: 'up',
  },
}

/*
 * 6. High break before low break on one bar: CANNOT HAPPEN, so it has no hand case. Both breaks
 * on bar t need H.price < close[t] < L.price, so H.price < L.price, with H and L both unbroken
 * and confirmed before t (at bars cH and cL). The bars of a swing low's window have lows at or
 * above L.price, so close[cL] >= L.price; likewise close[cH] <= H.price.
 *   - cH < cL: on bar cL, H is the latest swing high and close[cL] >= L.price > H.price, so H is
 *     already broken at cL.
 *   - cL < cH: on bar cH, close[cH] <= H.price < L.price, so L is already broken at cH.
 *   - cH = cL: the same pivot bar, so H.price = its high >= its low = L.price.
 * So the H-then-L order in step 2 never changes any output (an equivalent mutant), and no zone
 * is ever ended on its own creation bar. The test file checks "at most one event per bar" on
 * random walks instead.
 */

/*
 * 7a. Zone step order: step 1 (retest, invalidation) runs before the breaks create a zone and
 * before the maxZones drop. left 1, right 1, maxZones 1.
 *   0..3 UP_PREFIX  4 [12.5, 13.5, 12.5, 13.2]  5 [13.2, 13.4, 12.3, 12.6]
 *   6 [12.6, 13, 12.4, 12.8]  7 [12.8, 12.9, 10.4, 10.5]
 * Highs 10 12 11 12.6 13.5 13.4 13 12.9: p=1 HIGH c2; p=2 x; p=3 12.6 >= 13.5 x;
 *   p=4 13.5 > 12.6, 13.5 >= 13.4 -> HIGH c5; p=5 13.4 > 13.5 x; p=6 13 > 13.4 x.
 * Lows 8 9 9.5 9.8 12.5 12.3 12.4 10.4: p=1..4 each above the bar before x;
 *   p=5 12.3 < 12.5, 12.3 <= 12.4 -> LOW c6; p=6 12.4 < 12.3 x.
 * Bar 3: BOS up; support zone S [11, 12] from bar 1, created 3 (no zone active, no drop).
 * Bars 4, 5, 6: lows 12.5, 12.3, 12.4 > 12, no retest; no break (high@4 = 13.5 from bar 5).
 * Bar 7: step 1 on S: high 12.9 >= 11 and low 10.4 <= 12 -> first retest 7; close 10.5 < 11 ->
 *   S invalidated 7, leaving no active zone. Step 2: H = high@4 (13.5), close 10.5 no.
 *   L = low@5 (12.3), close 10.5 < 12.3 while 'up' -> ChoCh down at 7. 0 zones active < 1, so
 *   nothing is dropped; resistance zone from bar 5: bottom 12.3, top min(13.2, 12.6) = 12.6.
 * Mutant "drop before invalidating": S ends 'dropped' instead of 'invalidated'. Mutant "zones
 *   after breaks": S is dropped and the new zone is retested on its own creation bar.
 */
const ZONE_ORDER_PREFIX: readonly Ohlc[] = [
  ...UP_PREFIX,
  [12.5, 13.5, 12.5, 13.2],
  [13.2, 13.4, 12.3, 12.6],
  [12.6, 13, 12.4, 12.8],
]
const ZONE_ORDER_SWINGS: readonly SwingPoint[] = [
  swing('high', 1, 2, 12, 3),
  swing('high', 4, 5, 13.5, null),
  swing('low', 5, 6, 12.3, 7),
]
const ZONE_ORDER_EVENTS: readonly StructureEvent[] = [
  event('bos', 'up', 3, 12, 1),
  event('choch', 'down', 7, 12.3, 5),
]

export const CASE_INVALIDATION_FREES_SLOT: RuleCase = {
  name: 'invalidation-frees-slot',
  rule: 'zones are invalidated before a new zone is added and the oldest dropped',
  params: P11_ONE_ZONE,
  candles: bars([...ZONE_ORDER_PREFIX, [12.8, 12.9, 10.4, 10.5]]),
  expected: {
    swings: [...ZONE_ORDER_SWINGS],
    events: [...ZONE_ORDER_EVENTS],
    zones: [
      zone('support', 11, 12, 1, 3, 7, 7, 'invalidated'),
      zone('resistance', 12.3, 12.6, 5, 7, null, null, null),
    ],
    state: 'down',
  },
}

/*
 * 7b. Contrast to 7a: the same bars except bar 7 closes inside S, so S stays active and the new
 * zone drops it. maxZones 1.
 *   0..6 as 7a  7 [12.8, 12.9, 11.2, 11.5]
 * Pivots as 7a (bar 7 only enters p = 6, which fails on its high 13 > 13.4 and low 12.4 < 12.3).
 * Bar 7: step 1 on S: high 12.9 >= 11 and low 11.2 <= 12 -> first retest 7; close 11.5 not < 11:
 *   S stays. Step 2: close 11.5 < L 12.3 while 'up' -> ChoCh down at 7; 1 zone active = maxZones,
 *   so S ends 7 'dropped'; then resistance zone [12.3, 12.6] from bar 5 is added.
 */
export const CASE_DROP_WHEN_FULL: RuleCase = {
  name: 'drop-when-full',
  rule: 'a new zone with maxZones active drops the oldest',
  params: P11_ONE_ZONE,
  candles: bars([...ZONE_ORDER_PREFIX, [12.8, 12.9, 11.2, 11.5]]),
  expected: {
    swings: [...ZONE_ORDER_SWINGS],
    events: [...ZONE_ORDER_EVENTS],
    zones: [
      zone('support', 11, 12, 1, 3, 7, 7, 'dropped'),
      zone('resistance', 12.3, 12.6, 5, 7, null, null, null),
    ],
    state: 'down',
  },
}

/*
 * 8. One bar that is both a swing high and a swing low (an outside bar). left 1, right 1.
 *   0 [10, 11, 9, 10.5]  1 [10.5, 12, 8, 9]  2 [9, 11.5, 8.5, 10]  3 [10, 12.4, 9.8, 12.2]
 *   4 [12.2, 12.3, 7.5, 7.8]
 * Highs 11 12 11.5 12.4 12.3: p=1 12 > 11, 12 >= 11.5 -> HIGH c2; p=2 11.5 > 12 x;
 *   p=3 12.4 > 11.5, 12.4 >= 12.3 -> HIGH c4.
 * Lows 9 8 8.5 9.8 7.5: p=1 8 < 9, 8 <= 8.5 -> LOW c2; p=2 8.5 < 8 x; p=3 9.8 < 8.5 x.
 * Bar 2 step 3: bar 1 is both; the high is recorded first, then the low.
 * Bar 3: close 12.2 > H 12 -> BOS up; support zone from bar 1: top 12, bottom max(10.5, 9) = 10.5.
 *   L = low@1 (8): close 12.2 no.
 * Bar 4: step 1 on the support zone: high 12.3 >= 10.5 and low 7.5 <= 12 -> retest 4; close 7.8 <
 *   10.5 -> invalidated 4. Step 2: H broken; close 7.8 < L 8 while 'up' -> ChoCh down at 4;
 *   resistance zone from bar 1: bottom 8, top min(10.5, 9) = 9. Step 3 confirms high@3 (12.4).
 * Mutants: low recorded before high (swing order), or `else if (isLow)` (low@1 never exists).
 */
export const CASE_OUTSIDE_BAR_BOTH: RuleCase = {
  name: 'outside-bar-both',
  rule: 'one bar can be both a swing high and a swing low, high first',
  params: P11,
  candles: bars([
    [10, 11, 9, 10.5],
    [10.5, 12, 8, 9],
    [9, 11.5, 8.5, 10],
    [10, 12.4, 9.8, 12.2],
    [12.2, 12.3, 7.5, 7.8],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 12, 3), swing('low', 1, 2, 8, 4), swing('high', 3, 4, 12.4, null)],
    events: [event('bos', 'up', 3, 12, 1), event('choch', 'down', 4, 8, 1)],
    zones: [
      zone('support', 10.5, 12, 1, 3, 4, 4, 'invalidated'),
      zone('resistance', 8, 9, 1, 4, null, null, null),
    ],
    state: 'down',
  },
}

/*
 * 9. Zero-height zone: the pivot bar closes at its high, so its support band is [12, 12].
 *   0 [9, 10, 8, 9.5]  1 [9.5, 12, 9, 12]  2 [12, 12, 10, 10.5]  3 [10.5, 12.5, 10.4, 12.3]
 *   4 [12.3, 12.8, 12, 12.6]  5 [12.6, 12.7, 11.8, 12]  6 [12, 12.1, 11.5, 11.9]
 * Highs 10 12 12 12.5 12.8 12.7 12.1: p=1 12 > 10, 12 >= 12 (right tie) -> HIGH c2;
 *   p=2 12 > 12 x; p=3 12.5 >= 12.8 x; p=4 12.8 > 12.5, 12.8 >= 12.7 -> HIGH c5; p=5 12.7 > 12.8 x.
 * Lows 8 9 10 10.4 12 11.8 11.5: p=1..4 each above the bar before x; p=5 11.8 <= 11.5 x.
 * Bar 3: close 12.3 > 12 -> BOS up; support zone top 12, bottom max(9.5, 12) = 12 (zero height,
 *   kept), created 3.
 * Bar 4: high 12.8 >= 12 and low 12 <= 12 -> retest 4. Bar 5: close 12 = bottom, not < 12: stays.
 * Bar 6: close 11.9 < 12 -> invalidated 6. H = high@4 (12.8) on bar 6: close 11.9 no.
 * Mutant "skip zero-height zones": the zone is missing.
 */
export const CASE_ZERO_HEIGHT_ZONE: RuleCase = {
  name: 'zero-height-zone',
  rule: 'a zero-height zone is kept and retested / invalidated by the same rules',
  params: P11,
  candles: bars([
    [9, 10, 8, 9.5],
    [9.5, 12, 9, 12],
    [12, 12, 10, 10.5],
    [10.5, 12.5, 10.4, 12.3],
    [12.3, 12.8, 12, 12.6],
    [12.6, 12.7, 11.8, 12],
    [12, 12.1, 11.5, 11.9],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 12, 3), swing('high', 4, 5, 12.8, null)],
    events: [event('bos', 'up', 3, 12, 1)],
    zones: [zone('support', 12, 12, 1, 3, 4, 6, 'invalidated')],
    state: 'up',
  },
}

/*
 * 10. Equal highs further apart than `left` bars are both pivots (the strict-left tie only
 * blocks the later bar when the earlier one is inside its left window). left 1, right 1.
 *   0 [9, 10, 8, 9.5]  1 [9.5, 12, 9, 11]  2 [11, 11, 9.5, 10]  3 [10, 12, 9.8, 11.8]
 *   4 [11.8, 11.9, 10.5, 11]
 * Highs 10 12 11 12 11.9: p=1 12 > 10, 12 >= 11 -> HIGH c2; p=2 11 > 12 x;
 *   p=3 12 > 11 (bar 1 is outside its 1-bar left window), 12 >= 11.9 -> HIGH c4.
 * Lows 8 9 9.5 9.8 10.5: p=1..3 each above the bar before x.
 * Bar 3: H = high@1 (12): close 11.8 no. Bar 4: close 11 no. No events, no zones.
 */
export const CASE_EQUAL_HIGHS_APART: RuleCase = {
  name: 'equal-highs-apart',
  rule: 'equal highs more than left bars apart are both swing highs',
  params: P11,
  candles: bars([
    [9, 10, 8, 9.5],
    [9.5, 12, 9, 11],
    [11, 11, 9.5, 10],
    [10, 12, 9.8, 11.8],
    [11.8, 11.9, 10.5, 11],
  ]),
  expected: {
    swings: [swing('high', 1, 2, 12, null), swing('high', 3, 4, 12, null)],
    events: [],
    zones: [],
    state: 'none',
  },
}

/** Every rule case, in the order above. */
export const RULE_CASES: readonly RuleCase[] = [
  CASE_CLOSE_NOT_WICK_HIGH,
  CASE_CLOSE_NOT_WICK_LOW,
  CASE_BREAK_TIE_HIGH,
  CASE_BREAK_TIE_LOW,
  CASE_RETEST_TIE_SUPPORT,
  CASE_RETEST_TIE_RESISTANCE,
  CASE_INVALIDATION_TIE_SUPPORT,
  CASE_INVALIDATION_TIE_RESISTANCE,
  CASE_BREAKS_BEFORE_NEW_SWINGS,
  CASE_INVALIDATION_FREES_SLOT,
  CASE_DROP_WHEN_FULL,
  CASE_OUTSIDE_BAR_BOTH,
  CASE_ZERO_HEIGHT_ZONE,
  CASE_EQUAL_HIGHS_APART,
]
