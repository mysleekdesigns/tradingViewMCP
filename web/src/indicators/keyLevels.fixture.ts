/**
 * Hand-built key levels fixture (Phase 21).
 *
 * Every expected value was worked out by hand from the tables below, before the code ran. Edit
 * nothing here without re-doing the hand working.
 *
 * 2024-01-01 is a Monday, so jan(1)..jan(7) is one whole Monday-to-Sunday week and jan(8) opens the
 * next. The intraday bars cross the UTC midnight and the Sunday-to-Monday boundary between jan(7)
 * and jan(8), and the midnight into jan(9). They deliberately do NOT agree with the daily candles'
 * highs and lows, so a level that leaked from an intraday bar would show up as a wrong value.
 *
 * All expected numbers use atrPeriod 3, smaPeriod 3, showWeekly on.
 */
import type { Candle } from '../types'
import type { IndicatorPoint, KeyLevelDay, KeyLevelStep } from './types'

export const JAN1 = 1_704_067_200 // Monday 2024-01-01 00:00:00 UTC (19723 * 86400)
export const DAY = 86_400
export const HOUR = 3_600

/** 00:00 UTC of January day n, and hour h of it. */
export function jan(n: number, h = 0): number {
  return JAN1 + (n - 1) * DAY + h * HOUR
}

export const FIXTURE_PARAMS = { atrPeriod: 3, smaPeriod: 3, showWeekly: true }

function candle(time: number, open: number, high: number, low: number, close: number): Candle {
  return { time, open, high, low, close, volume: 1 }
}

/*
 * DAILY. TR = max(H - L, |H - prev C|, |L - prev C|); the first candle's TR is H - L.
 *
 * day      O    H    L    C    TR working                                ATR(3)         SMA(3) of C
 * Mon  1  100  110   90  105   110-90 = 20                               -              -
 * Tue  2  105  115  100  111   max(15, |115-105|=10, |100-105|=5) = 15   -              -
 * Wed  3  111  112  102  105   max(10, |112-111|=1, |102-111|=9) = 10    45/3 = 15      321/3 = 107
 * Thu  4  105  106  100  102   max(6, |106-105|=1, |100-105|=5) = 6      15-9/3 = 12    318/3 = 106
 * Fri  5  118  123  114  120   max(9, |123-102|=21, |114-102|=12) = 21   12+9/3 = 15    327/3 = 109
 * Sat  6  120  122  116  117   max(6, |122-120|=2, |116-120|=4) = 6      15-9/3 = 12    339/3 = 113
 * Sun  7  104  109   99  105   max(10, |109-117|=8, |99-117|=18) = 18    12+6/3 = 14    342/3 = 114
 * Mon  8  105  118  101  111   max(17, |118-105|=13, |101-105|=4) = 17   14+3/3 = 15    333/3 = 111
 */
export const FIXTURE_DAILY: readonly Candle[] = [
  candle(jan(1), 100, 110, 90, 105),
  candle(jan(2), 105, 115, 100, 111),
  candle(jan(3), 111, 112, 102, 105),
  candle(jan(4), 105, 106, 100, 102),
  candle(jan(5), 118, 123, 114, 120),
  candle(jan(6), 120, 122, 116, 117),
  candle(jan(7), 104, 109, 99, 105),
  candle(jan(8), 105, 118, 101, 111),
]

/*
 * What each day knows at its start: the row of the day before.
 *
 * day      from    atr   sma   close vs sma        PDH  PDL
 * Mon  1   none    null  null  null                no step
 * Tue  2   Jan 1   null  null  null                110   90
 * Wed  3   Jan 2   null  null  null                115  100
 * Thu  4   Jan 3   15    107   105 < 107 below     112  102
 * Fri  5   Jan 4   12    106   102 < 106 below     106  100
 * Sat  6   Jan 5   15    109   120 > 109 above     123  114
 * Sun  7   Jan 6   12    113   117 > 113 above     122  116
 * Mon  8   Jan 7   14    114   105 < 114 below     109   99
 * Tue  9   Jan 8   15    111   111 = 111 null      118  101
 */
export const EXPECTED_ALL_DAYS: readonly KeyLevelDay[] = [
  { day: jan(1), atr: null, sma: null, bias: null },
  { day: jan(2), atr: null, sma: null, bias: null },
  { day: jan(3), atr: null, sma: null, bias: null },
  { day: jan(4), atr: 15, sma: 107, bias: 'below' },
  { day: jan(5), atr: 12, sma: 106, bias: 'below' },
  { day: jan(6), atr: 15, sma: 109, bias: 'above' },
  { day: jan(7), atr: 12, sma: 113, bias: 'above' },
  { day: jan(8), atr: 14, sma: 114, bias: 'below' },
  { day: jan(9), atr: 15, sma: 111, bias: null },
]

function pd(n: number, high: number, low: number): KeyLevelStep[] {
  return [
    { key: 'pdh', start: jan(n), end: jan(n + 1), price: high },
    { key: 'pdl', start: jan(n), end: jan(n + 1), price: low },
  ]
}

/*
 * PREVIOUS WEEK for the week of Monday jan(8), from jan(1)..jan(7):
 *   highs 110, 115, 112, 106, 123, 122, 109 -> 123 (Fri); lows 90, 100, 102, 100, 114, 116, 99 -> 90.
 *   It runs [jan(8), jan(15)). The week of jan(1) reads December, which has no candles: no step.
 */
export const EXPECTED_PW: readonly KeyLevelStep[] = [
  { key: 'pwh', start: jan(8), end: jan(15), price: 123 },
  { key: 'pwl', start: jan(8), end: jan(15), price: 90 },
]

/** Steps for one bar on jan(1) through jan(9): days 2..9, the week on its Monday after pdh, pdl. */
export const EXPECTED_ALL_STEPS: readonly KeyLevelStep[] = [
  ...pd(2, 110, 90),
  ...pd(3, 115, 100),
  ...pd(4, 112, 102),
  ...pd(5, 106, 100),
  ...pd(6, 123, 114),
  ...pd(7, 122, 116),
  ...pd(8, 109, 99),
  ...EXPECTED_PW,
  ...pd(9, 118, 101),
]

/*
 * INTRADAY, 6h bars, closed. ATR of the day from the table above. (running high - running low) /
 * ATR * 100, the running values reset at each UTC midnight:
 *
 * bar          H      L      running hi / lo     range   ATR   range used
 * Sun 7 12:00  105    102    105 / 102           3       12    3/12 = 25
 * Sun 7 18:00  108    104    108 / 102           6       12    6/12 = 50
 * Mon 8 00:00  106    102.5  106 / 102.5         3.5     14    3.5/14 = 25   (reset)
 * Mon 8 06:00  109.5  103    109.5 / 102.5       7       14    7/14 = 50
 * Mon 8 12:00  115.5  101.5  115.5 / 101.5       14      14    14/14 = 100
 * Mon 8 18:00  113    110    115.5 / 101.5       14      14    100           (inside bar)
 * Tue 9 00:00  113    110    113 / 110           3       15    3/15 = 20     (reset)
 * Tue 9 06:00  114    108    114 / 108           6       15    6/15 = 40
 */
export const FIXTURE_BARS: readonly Candle[] = [
  candle(jan(7, 12), 103, 105, 102, 104),
  candle(jan(7, 18), 104, 108, 104, 105),
  candle(jan(8, 0), 105, 106, 102.5, 104),
  candle(jan(8, 6), 104, 109.5, 103, 108),
  candle(jan(8, 12), 108, 115.5, 101.5, 112),
  candle(jan(8, 18), 112, 113, 110, 111),
  candle(jan(9, 0), 111, 113, 110, 112),
  candle(jan(9, 6), 112, 114, 108, 110),
]

/** Day 7 has none: FIXTURE_BARS start at its 12:00, so its range is not known. */
export const EXPECTED_RANGE_USED: readonly IndicatorPoint[] = [
  { time: jan(8, 0), value: 25 },
  { time: jan(8, 6), value: 50 },
  { time: jan(8, 12), value: 100 },
  { time: jan(8, 18), value: 100 },
  { time: jan(9, 0), value: 20 },
  { time: jan(9, 6), value: 40 },
]

/** FIXTURE_BARS run to the end: days 7, 8, 9. The week of jan(1) has no step (no December). */
export const EXPECTED_DAYS: readonly KeyLevelDay[] = EXPECTED_ALL_DAYS.slice(6)
export const EXPECTED_STEPS: readonly KeyLevelStep[] = EXPECTED_ALL_STEPS.slice(10)

/*
 * GAP: FIXTURE_DAILY without Thursday jan(4). One closed bar on jan(4), through jan(9).
 *
 * The chain restarts at Fri 5, whose TR has no previous close: 123 - 114 = 9, not 21.
 * Fri 5 TR 9; Sat 6 TR 6; Sun 7 TR 18 -> ATR (9 + 6 + 18) / 3 = 11; Mon 8 TR 17 -> 11 + 6/3 = 13.
 * SMA restarts too: Sun 7 (120 + 117 + 105) / 3 = 114; Mon 8 (117 + 105 + 111) / 3 = 111.
 *
 * day      from    atr   sma   close vs sma        PDH  PDL
 * Thu  4   Jan 3   15    107   105 < 107 below     112  102
 * Fri  5   none    null  null  null                no step (the day after the gap: never filled)
 * Sat  6   Jan 5   null  null  null                123  114
 * Sun  7   Jan 6   null  null  null                122  116
 * Mon  8   Jan 7   11    114   105 < 114 below     109   99
 * Tue  9   Jan 8   13    111   111 = 111 null      118  101
 *
 * Week of jan(8): jan(4) is missing from its 7 candles, so no PW step.
 */
export const GAP_DAILY: readonly Candle[] = FIXTURE_DAILY.filter((c) => c.time !== jan(4))

export const EXPECTED_GAP_DAYS: readonly KeyLevelDay[] = [
  { day: jan(4), atr: 15, sma: 107, bias: 'below' },
  { day: jan(5), atr: null, sma: null, bias: null },
  { day: jan(6), atr: null, sma: null, bias: null },
  { day: jan(7), atr: null, sma: null, bias: null },
  { day: jan(8), atr: 11, sma: 114, bias: 'below' },
  { day: jan(9), atr: 13, sma: 111, bias: null },
]

export const EXPECTED_GAP_STEPS: readonly KeyLevelStep[] = [
  ...pd(4, 112, 102),
  ...pd(6, 123, 114),
  ...pd(7, 122, 116),
  ...pd(8, 109, 99),
  ...pd(9, 118, 101),
]
