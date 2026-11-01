import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import {
  compact,
  dedupeByTime,
  finite,
  isFiniteNumber,
  isRecord,
  nonEmptyString,
  nonNegative,
  numberOr,
  numberOrNull,
  sameCandle,
  stringOr,
  stringsOf,
  toFiniteNumber,
} from './guards'
import { MarketDataError } from './provider'

const c = (time: number, close = 1): Candle => ({
  time,
  open: 1,
  high: 2,
  low: 0.5,
  close,
  volume: 3,
})

describe('guards', () => {
  it('isRecord: plain objects only', () => {
    expect(isRecord({})).toBe(true)
    expect(isRecord({ a: 1 })).toBe(true)
    expect(isRecord(null)).toBe(false)
    expect(isRecord([])).toBe(false)
    expect(isRecord('x')).toBe(false)
    expect(isRecord(1)).toBe(false)
    expect(isRecord(undefined)).toBe(false)
  })

  it('isFiniteNumber / nonNegative / numberOrNull / numberOr: numbers only', () => {
    expect(isFiniteNumber(0)).toBe(true)
    expect(isFiniteNumber(-1.5)).toBe(true)
    expect(isFiniteNumber(Number.NaN)).toBe(false)
    expect(isFiniteNumber(Infinity)).toBe(false)
    expect(isFiniteNumber('1')).toBe(false)
    expect(nonNegative(0)).toBe(0)
    expect(nonNegative(2.5)).toBe(2.5)
    expect(nonNegative(-1)).toBeUndefined()
    expect(nonNegative('3')).toBeUndefined()
    expect(nonNegative(Number.NaN)).toBeUndefined()
    expect(numberOrNull(4)).toBe(4)
    expect(numberOrNull('4')).toBeNull()
    expect(numberOrNull(null)).toBeNull()
    expect(numberOr(4, 9)).toBe(4)
    expect(numberOr(Infinity, 9)).toBe(9)
    expect(numberOr(undefined, 9)).toBe(9)
  })

  it('finite: numbers and non-empty numeric strings', () => {
    expect(finite(1.5)).toBe(1.5)
    expect(finite('37000.10')).toBe(37000.1)
    expect(finite(' 2 ')).toBe(2)
    expect(finite('')).toBeNull()
    expect(finite('   ')).toBeNull()
    expect(finite('abc')).toBeNull()
    expect(finite('Infinity')).toBeNull()
    expect(finite(Number.NaN)).toBeNull()
    expect(finite(null)).toBeNull()
    expect(finite(true)).toBeNull()
  })

  it('nonEmptyString / stringOr / stringsOf', () => {
    expect(nonEmptyString('a')).toBe(true)
    expect(nonEmptyString(' ')).toBe(false)
    expect(nonEmptyString(1)).toBe(false)
    expect(stringOr('x')).toBe('x')
    expect(stringOr(1)).toBe('')
    expect(stringOr(null, 'd')).toBe('d')
    expect(stringsOf(['a', 1, 'b', null])).toEqual(['a', 'b'])
    expect(stringsOf('a')).toEqual([])
  })

  it('compact drops nulls and keeps order', () => {
    expect(compact([1, 'x', 2, null], (v) => (typeof v === 'number' ? v * 10 : null))).toEqual([
      10, 20,
    ])
  })

  it('toFiniteNumber parses or throws parse errors with the caller wording', () => {
    const w = { prefix: 'kline', wrongType: 'expected string or number', notFinite: 'not finite' }
    expect(toFiniteNumber('1.25', 'open', w)).toBe(1.25)
    expect(toFiniteNumber(7, 'open', w)).toBe(7)
    const err = (v: unknown) => {
      try {
        toFiniteNumber(v, 'open', w)
      } catch (e) {
        return e
      }
      return null
    }
    expect(err(null)).toBeInstanceOf(MarketDataError)
    expect(err(null)).toMatchObject({
      kind: 'parse',
      message: 'kline open: expected string or number',
    })
    expect(err(' ')).toMatchObject({ kind: 'parse', message: 'kline open: empty value' })
    expect(err('x')).toMatchObject({ kind: 'parse', message: 'kline open: not finite' })
    expect(err(Infinity)).toMatchObject({ kind: 'parse', message: 'kline open: not finite' })
  })

  it('dedupeByTime keeps the later row per time', () => {
    const rows = [c(1, 1), c(1, 2), c(2, 3), c(3, 4), c(3, 5), c(3, 6)]
    expect(dedupeByTime(rows).map((x) => [x.time, x.close])).toEqual([
      [1, 2],
      [2, 3],
      [3, 6],
    ])
    expect(dedupeByTime([])).toEqual([])
  })

  it('sameCandle compares every field exactly', () => {
    expect(sameCandle(c(1), c(1))).toBe(true)
    expect(sameCandle(c(1), c(2))).toBe(false)
    expect(sameCandle(c(1, 1), c(1, 1.0000001))).toBe(false)
    expect(sameCandle(c(1), { ...c(1), volume: 4 })).toBe(false)
    expect(sameCandle(c(1), { ...c(1), high: 3 })).toBe(false)
    expect(sameCandle(c(1), { ...c(1), low: 0 })).toBe(false)
    expect(sameCandle(c(1), { ...c(1), open: 0 })).toBe(false)
  })
})
