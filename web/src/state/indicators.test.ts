import { describe, expect, it } from 'vitest'
import { Sma } from '../indicators/primitives'
import type { IndicatorDef, IndicatorId, IndicatorStepper } from '../indicators/types'
import {
  createIndicatorConfig,
  INDICATORS_STORAGE_KEY,
  indicatorsReducer,
  INSTANCE_PALETTE,
  loadIndicators,
  MAX_INDICATORS,
  parseIndicators,
  sanitizeColors,
  sanitizeInputs,
  saveIndicators,
  type IndicatorConfig,
} from './indicators'

function smaStepper(period: number): IndicatorStepper {
  const make = (s: Sma): IndicatorStepper => ({
    next: (c) => {
      const v = s.next(c.close)
      return v === undefined ? {} : { value: v }
    },
    clone: () => make(s.clone()),
  })
  return make(new Sma(period))
}

/** Fake one-line overlay with a source selector. */
const fakeEma: IndicatorDef = {
  id: 'ema',
  name: 'Fake EMA',
  shortName: 'EMA',
  description: 'test',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 1, max: 500, step: 1, integer: true },
  ],
  source: 'close',
  lines: [{ key: 'value', label: 'EMA', kind: 'line', color: '#2962ff', lineWidth: 2 }],
  guides: [],
  createStepper: (inputs) => smaStepper(inputs.period),
}

/** Fake multi-line def without a source selector and a non-integer input. */
const fakeBb: IndicatorDef = {
  id: 'bb',
  name: 'Fake BB',
  shortName: 'BB',
  description: 'test',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 2, max: 200, step: 1, integer: true },
    { key: 'mult', label: 'StdDev', default: 2, min: 0.1, max: 10, step: 0.1, integer: false },
  ],
  source: null,
  lines: [
    { key: 'upper', label: 'Upper', kind: 'line', color: '#ff0000', lineWidth: 1 },
    { key: 'lower', label: 'Lower', kind: 'line', color: '#00ff00', lineWidth: 1 },
  ],
  guides: [],
  createStepper: (inputs) => smaStepper(inputs.period),
}

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map<IndicatorId, IndicatorDef>([
  ['ema', fakeEma],
  ['bb', fakeBb],
])

class MemoryStorage {
  data = new Map<string, string>()
  getItem(key: string): string | null {
    return this.data.get(key) ?? null
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value)
  }
}

const throwingStorage = {
  getItem(): string | null {
    throw new Error('SecurityError')
  },
  setItem(): void {
    throw new Error('QuotaExceededError')
  },
}

describe('sanitizeInputs', () => {
  it('fills defaults for missing, non-numeric, and non-finite values', () => {
    expect(sanitizeInputs(fakeBb, undefined)).toEqual({ period: 20, mult: 2 })
    expect(sanitizeInputs(fakeBb, { period: '50', mult: NaN })).toEqual({ period: 20, mult: 2 })
    expect(sanitizeInputs(fakeBb, { period: Infinity, mult: null })).toEqual({
      period: 20,
      mult: 2,
    })
  })
  it('clamps to [min, max] and rounds integer inputs only', () => {
    expect(sanitizeInputs(fakeBb, { period: 0, mult: 99 })).toEqual({ period: 2, mult: 10 })
    expect(sanitizeInputs(fakeBb, { period: 1000, mult: 0 })).toEqual({ period: 200, mult: 0.1 })
    expect(sanitizeInputs(fakeBb, { period: 14.6, mult: 2.55 })).toEqual({ period: 15, mult: 2.55 })
  })
  it('drops unknown keys', () => {
    expect(sanitizeInputs(fakeEma, { period: 50, evil: 1 })).toEqual({ period: 50 })
  })
})

describe('sanitizeColors', () => {
  it('keeps valid #rrggbb (lower-cased) and defaults the rest per line', () => {
    expect(sanitizeColors(fakeBb, { upper: '#ABCDEF', lower: 'red' })).toEqual({
      upper: '#abcdef',
      lower: '#00ff00',
    })
    expect(sanitizeColors(fakeBb, { upper: '#abc', lower: '#1234567' })).toEqual({
      upper: '#ff0000',
      lower: '#00ff00',
    })
    expect(sanitizeColors(fakeBb, 'nope')).toEqual({ upper: '#ff0000', lower: '#00ff00' })
  })
})

describe('createIndicatorConfig', () => {
  it('uses defaults and a valid unique uid', () => {
    const c = createIndicatorConfig(fakeBb, [])
    expect(c.type).toBe('bb')
    expect(c.inputs).toEqual({ period: 20, mult: 2 })
    expect(c.source).toBe('close')
    expect(c.colors).toEqual({ upper: '#ff0000', lower: '#00ff00' })
    expect(c.visible).toBe(true)
    expect(c.uid).toMatch(/^[a-z0-9-]{1,40}$/)
    expect(c.uid.startsWith('bb-')).toBe(true)
  })
  it('honours a given uid unless it is taken or invalid', () => {
    const a = createIndicatorConfig(fakeEma, [], 'ema-a')
    expect(a.uid).toBe('ema-a')
    expect(createIndicatorConfig(fakeEma, [a], 'ema-a').uid).not.toBe('ema-a')
    expect(createIndicatorConfig(fakeEma, [], 'Bad UID!').uid).toMatch(/^ema-/)
  })
  it('gives a second instance of a one-line type a different palette colour', () => {
    const first = createIndicatorConfig(fakeEma, [])
    expect(first.colors.value).toBe('#2962ff')
    const second = createIndicatorConfig(fakeEma, [first])
    expect(second.colors.value).toBe(INSTANCE_PALETTE[1])
    const third = createIndicatorConfig(fakeEma, [first, second])
    expect(third.colors.value).toBe(INSTANCE_PALETTE[2])
    expect(second.uid).not.toBe(first.uid)
  })
  it('does not rotate colours for multi-line types', () => {
    const a = createIndicatorConfig(fakeBb, [])
    expect(createIndicatorConfig(fakeBb, [a]).colors).toEqual(a.colors)
  })
})

describe('parseIndicators', () => {
  it('rejects non-arrays', () => {
    for (const raw of [null, undefined, 1, 'x', {}, { 0: 1 }]) {
      expect(parseIndicators(raw, DEFS)).toEqual([])
    }
  })
  it('drops entries with unknown type or bad uid and keeps valid fields per entry', () => {
    const raw = [
      null,
      'ema',
      { uid: 'x-1', type: 'rsi' }, // not in defs
      { uid: 'BAD UID', type: 'ema' },
      { uid: 42, type: 'ema' },
      {
        uid: 'ema-1',
        type: 'ema',
        inputs: { period: 50.4 },
        source: 'hl2',
        colors: { value: 'javascript:alert(1)' },
        visible: false,
      },
      { uid: 'bb-1', type: 'bb', inputs: { period: -5, mult: 'x' }, source: 'hl2', visible: 'no' },
    ]
    expect(parseIndicators(raw, DEFS)).toEqual<IndicatorConfig[]>([
      {
        uid: 'ema-1',
        type: 'ema',
        inputs: { period: 50 },
        source: 'hl2',
        colors: { value: '#2962ff' },
        visible: false,
      },
      {
        uid: 'bb-1',
        type: 'bb',
        inputs: { period: 2, mult: 2 },
        source: 'close', // def has no source selector
        colors: { upper: '#ff0000', lower: '#00ff00' },
        visible: true,
      },
    ])
  })
  it('falls back to the default source when the stored one is invalid', () => {
    const out = parseIndicators([{ uid: 'e', type: 'ema', source: 'volume' }], DEFS)
    expect(out[0].source).toBe('close')
  })
  it('dedupes uids, keeping the first', () => {
    const out = parseIndicators(
      [
        { uid: 'same', type: 'ema', inputs: { period: 10 } },
        { uid: 'same', type: 'bb' },
      ],
      DEFS,
    )
    expect(out).toHaveLength(1)
    expect(out[0].inputs.period).toBe(10)
  })
  it('caps at MAX_INDICATORS', () => {
    const raw = Array.from({ length: 50 }, (_, i) => ({ uid: `e-${i}`, type: 'ema' }))
    const out = parseIndicators(raw, DEFS)
    expect(out).toHaveLength(MAX_INDICATORS)
    expect(out[MAX_INDICATORS - 1].uid).toBe(`e-${MAX_INDICATORS - 1}`)
  })
})

describe('load / save', () => {
  it('round-trips', () => {
    const s = new MemoryStorage()
    const a = createIndicatorConfig(fakeEma, [])
    const b = { ...createIndicatorConfig(fakeBb, [a]), visible: false }
    saveIndicators([a, b], s)
    expect(s.data.has(INDICATORS_STORAGE_KEY)).toBe(true)
    expect(loadIndicators(DEFS, s)).toEqual([a, b])
  })
  it('returns [] for missing, invalid JSON, or null storage', () => {
    const s = new MemoryStorage()
    expect(loadIndicators(DEFS, s)).toEqual([])
    s.setItem(INDICATORS_STORAGE_KEY, '{not json')
    expect(loadIndicators(DEFS, s)).toEqual([])
    expect(loadIndicators(DEFS, null)).toEqual([])
    expect(() => saveIndicators([], null)).not.toThrow()
  })
  it('survives storage that throws', () => {
    expect(loadIndicators(DEFS, throwingStorage)).toEqual([])
    expect(() =>
      saveIndicators([createIndicatorConfig(fakeEma, [])], throwingStorage),
    ).not.toThrow()
  })
})

describe('indicatorsReducer', () => {
  const add = (items: readonly IndicatorConfig[], def: IndicatorDef, uid: string) =>
    indicatorsReducer(items, { type: 'add', def, uid })

  it('adds with the given uid and rotates colours for a second instance', () => {
    let items = add([], fakeEma, 'ema-1')
    items = add(items, fakeEma, 'ema-2')
    expect(items.map((c) => c.uid)).toEqual(['ema-1', 'ema-2'])
    expect(items[0].colors.value).not.toBe(items[1].colors.value)
  })
  it('ignores a duplicate uid and stops at MAX_INDICATORS', () => {
    const one = add([], fakeEma, 'ema-1')
    expect(add(one, fakeEma, 'ema-1')).toBe(one)
    let items: readonly IndicatorConfig[] = []
    for (let i = 0; i < MAX_INDICATORS + 3; i++) items = add(items, fakeBb, `bb-${i}`)
    expect(items).toHaveLength(MAX_INDICATORS)
  })
  it('removes by uid and returns the same array for an unknown uid', () => {
    const items = add(add([], fakeEma, 'a'), fakeBb, 'b')
    expect(indicatorsReducer(items, { type: 'remove', uid: 'a' }).map((c) => c.uid)).toEqual(['b'])
    expect(indicatorsReducer(items, { type: 'remove', uid: 'zzz' })).toBe(items)
  })
  it('toggles visibility', () => {
    const items = add([], fakeEma, 'a')
    const hidden = indicatorsReducer(items, { type: 'toggle', uid: 'a' })
    expect(hidden[0].visible).toBe(false)
    expect(indicatorsReducer(hidden, { type: 'toggle', uid: 'a' })[0].visible).toBe(true)
  })
  it('updates with sanitization, keeping current values for invalid fields', () => {
    const items = add(add([], fakeEma, 'a'), fakeBb, 'b')
    let next = indicatorsReducer(items, {
      type: 'update',
      defs: DEFS,
      uid: 'a',
      patch: { inputs: { period: 9999 }, source: 'hlc3', colors: { value: '#FF00AA' } },
    })
    expect(next[0]).toMatchObject({
      inputs: { period: 500 },
      source: 'hlc3',
      colors: { value: '#ff00aa' },
    })
    expect(next[1]).toBe(items[1])

    next = indicatorsReducer(items, {
      type: 'update',
      defs: DEFS,
      uid: 'b',
      patch: {
        inputs: { mult: 3.5, period: NaN },
        colors: { upper: 'bad' },
        source: 'open', // def has no source selector
        visible: false,
      },
    })
    expect(next[1]).toMatchObject({
      inputs: { period: 20, mult: 3.5 },
      colors: { upper: '#ff0000', lower: '#00ff00' },
      source: 'close',
      visible: false,
    })
    const bogus = indicatorsReducer(items, {
      type: 'update',
      defs: DEFS,
      uid: 'a',
      patch: { source: 'volume' as never },
    })
    expect(bogus[0].source).toBe('close')
    expect(indicatorsReducer(items, { type: 'update', defs: DEFS, uid: 'nope', patch: {} })).toBe(
      items,
    )
  })
  it('replace swaps the list', () => {
    const items = add([], fakeEma, 'a')
    expect(indicatorsReducer([], { type: 'replace', items })).toBe(items)
  })
})
