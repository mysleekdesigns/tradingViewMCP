import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PersistenceClient } from '../data/persistence'
import { Sma } from '../indicators/primitives'
import { INDICATOR_BY_ID } from '../indicators/registry'
import type { IndicatorDef, IndicatorId, IndicatorStepper } from '../indicators/types'
import {
  anchorInputs,
  createIndicatorConfig,
  createIndicatorsSync,
  INDICATORS_STORAGE_KEY,
  indicatorsFromStorageEvent,
  indicatorsReducer,
  INSTANCE_PALETTE,
  listenIndicatorsStorage,
  loadIndicators,
  MAX_INDICATORS,
  parseIndicatorLayout,
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

/** Fake event def (like market structure): one numeric input and two on/off toggles. */
const fakeToggles: IndicatorDef = {
  id: 'mstructure',
  name: 'Fake Structure',
  shortName: 'MS',
  description: 'test',
  placement: 'overlay',
  inputs: [
    { key: 'left', label: 'Left bars', default: 5, min: 1, max: 50, step: 1, integer: true },
    {
      key: 'showSwings',
      label: 'Swings',
      default: 1,
      min: 0,
      max: 1,
      step: 1,
      integer: true,
      kind: 'toggle',
    },
    {
      key: 'showZones',
      label: 'Flip zones',
      default: 0,
      min: 0,
      max: 1,
      step: 1,
      integer: true,
      kind: 'toggle',
    },
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper() {
    const stepper: IndicatorStepper = { next: () => ({}), clone: () => stepper }
    return stepper
  },
  structure: () => ({ swings: [], events: [], zones: [], state: 'none' }),
}

describe('toggle inputs', () => {
  const TOGGLE_DEFS = new Map<IndicatorId, IndicatorDef>([['mstructure', fakeToggles]])

  it('keep a stored 0 or 1 exactly', () => {
    expect(sanitizeInputs(fakeToggles, { left: 7, showSwings: 0, showZones: 1 })).toEqual({
      left: 7,
      showSwings: 0,
      showZones: 1,
    })
  })
  it('fall back to the default for anything but 0 or 1, without rounding or clamping', () => {
    for (const bad of [0.6, 0.4, 2, -1, 1.0000001, NaN, Infinity, '1', true, null]) {
      expect(sanitizeInputs(fakeToggles, { showSwings: bad, showZones: bad })).toEqual({
        left: 5,
        showSwings: 1,
        showZones: 0,
      })
    }
  })
  it('are sanitized the same way when loaded from storage', () => {
    const raw = [
      { uid: 'ms-1', type: 'mstructure', inputs: { left: 3, showSwings: 5, showZones: 1 } },
    ]
    expect(parseIndicators(raw, TOGGLE_DEFS)[0].inputs).toEqual({
      left: 3,
      showSwings: 1,
      showZones: 1,
    })
  })
  it('an update with 0 / 1 applies; an invalid toggle keeps the current value', () => {
    const items = [createIndicatorConfig(fakeToggles, [], 'ms')]
    const off = indicatorsReducer(items, {
      type: 'update',
      defs: TOGGLE_DEFS,
      uid: 'ms',
      patch: { inputs: { showSwings: 0, showZones: 1 } },
    })
    expect(off[0].inputs).toEqual({ left: 5, showSwings: 0, showZones: 1 })
    const bogus = indicatorsReducer(off, {
      type: 'update',
      defs: TOGGLE_DEFS,
      uid: 'ms',
      patch: { inputs: { left: 9, showSwings: 0.5, showZones: 2 } },
    })
    expect(bogus[0].inputs).toEqual({ left: 9, showSwings: 0, showZones: 1 })
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

const one = (uid: string): IndicatorConfig[] => [createIndicatorConfig(fakeEma, [], uid)]

describe('parseIndicatorLayout', () => {
  it('reads { indicators } and sanitizes entries', () => {
    const out = parseIndicatorLayout(
      {
        indicators: [
          { uid: 'a', type: 'ema', inputs: { period: 9999 } },
          { uid: 'b', type: 'nope' },
        ],
      },
      DEFS,
    )
    expect(out).toHaveLength(1)
    expect(out?.[0].inputs.period).toBe(500)
  })
  it('returns null for a document without an indicators array', () => {
    expect(parseIndicatorLayout({}, DEFS)).toBeNull()
    expect(parseIndicatorLayout({ indicators: 'x' }, DEFS)).toBeNull()
    expect(parseIndicatorLayout(null, DEFS)).toBeNull()
  })
})

function fakeClient(over: Partial<PersistenceClient>): PersistenceClient {
  const fail = () => Promise.reject(new Error('unexpected call'))
  return { getWatchlist: fail, putWatchlist: fail, getLayout: fail, putLayout: fail, ...over }
}

describe('createIndicatorsSync', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('adopts the server layout from /api/layouts/default', async () => {
    const getLayout = vi.fn(async (id: string) => ({
      id,
      data: { indicators: [{ uid: 'srv', type: 'ema' }] },
      updatedAt: 1,
    }))
    const remote: IndicatorConfig[][] = []
    const sync = createIndicatorsSync([], DEFS, (i) => remote.push(i), fakeClient({ getLayout }))
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(getLayout).toHaveBeenCalledWith('default')
    expect(remote).toHaveLength(1)
    expect(remote[0].map((c) => c.uid)).toEqual(['srv'])
    expect(remote[0][0].colors.value).toBe('#2962ff')
  })

  it('ignores a layout without indicators and keeps local', async () => {
    const remote: IndicatorConfig[][] = []
    const sync = createIndicatorsSync(
      [],
      DEFS,
      (i) => remote.push(i),
      fakeClient({ getLayout: async (id) => ({ id, data: { other: 1 }, updatedAt: 1 }) }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(remote).toEqual([])
  })

  it('migrates local indicators on 404 as { indicators }', async () => {
    const local = one('loc')
    const putLayout = vi.fn(async (id: string, data: Record<string, unknown>) => ({
      id,
      data,
      updatedAt: 1,
    }))
    const sync = createIndicatorsSync(
      local,
      DEFS,
      () => {},
      fakeClient({ getLayout: async () => null, putLayout }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(putLayout).toHaveBeenCalledWith('default', { indicators: local })
  })

  it('stays local-only when the server is unreachable', async () => {
    const putLayout = vi.fn()
    const sync = createIndicatorsSync(
      [],
      DEFS,
      () => {},
      fakeClient({ getLayout: () => Promise.reject(new Error('down')), putLayout }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    sync.update(one('x'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(sync.state).toBe('local-only')
    expect(putLayout).not.toHaveBeenCalled()
  })
})

function storageEvent(key: string | null, newValue: string | null): Event {
  return Object.assign(new Event('storage'), { key, newValue })
}

describe('indicatorsFromStorageEvent', () => {
  it('parses the indicators key with the stored-value rules', () => {
    const items = indicatorsFromStorageEvent(
      INDICATORS_STORAGE_KEY,
      JSON.stringify([
        { uid: 'a', type: 'ema' },
        { uid: 'b', type: 'nope' },
      ]),
      DEFS,
    )
    expect(items?.map((c) => c.uid)).toEqual(['a'])
    expect(items?.[0].colors.value).toBe('#2962ff')
  })

  it('ignores other keys, removals, and invalid JSON', () => {
    expect(indicatorsFromStorageEvent('tv.watchlist.v1', '[]', DEFS)).toBeNull()
    expect(indicatorsFromStorageEvent(null, '[]', DEFS)).toBeNull()
    expect(indicatorsFromStorageEvent(INDICATORS_STORAGE_KEY, null, DEFS)).toBeNull()
    expect(indicatorsFromStorageEvent(INDICATORS_STORAGE_KEY, '{bad', DEFS)).toBeNull()
  })
})

describe('listenIndicatorsStorage (W2: cross-tab sync)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it("delivers another tab's list and stops after unsubscribe", () => {
    const target = new EventTarget()
    const got: string[][] = []
    const off = listenIndicatorsStorage(
      target,
      () => DEFS,
      (items) => got.push(items.map((c) => c.uid)),
    )
    target.dispatchEvent(storageEvent(INDICATORS_STORAGE_KEY, JSON.stringify(one('x'))))
    target.dispatchEvent(storageEvent('other', JSON.stringify(one('y'))))
    target.dispatchEvent(storageEvent(INDICATORS_STORAGE_KEY, 'not json'))
    expect(got).toEqual([['x']])
    off()
    target.dispatchEvent(storageEvent(INDICATORS_STORAGE_KEY, JSON.stringify(one('z'))))
    expect(got).toEqual([['x']])
  })

  it("a stale tab adopts the other tab's layout without echoing or later PUTting its own", async () => {
    const putLayout = vi.fn(async (id: string, data: Record<string, unknown>) => ({
      id,
      data,
      updatedAt: 1,
    }))
    const server = { id: 'default', data: { indicators: one('srv') }, updatedAt: 1 }
    const sync = createIndicatorsSync(
      one('srv'),
      DEFS,
      () => {},
      fakeClient({ getLayout: async () => server, putLayout }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    // This (stale) tab edits; a PUT is queued.
    sync.update(one('stale'))
    await vi.advanceTimersByTimeAsync(100)
    // The other tab writes its layout; wire the listener the way useIndicators does.
    const target = new EventTarget()
    const off = listenIndicatorsStorage(
      target,
      () => DEFS,
      (next) => {
        sync.adopt(next)
        sync.update(next) // the hook's [items] effect after dispatch(replace)
      },
    )
    target.dispatchEvent(storageEvent(INDICATORS_STORAGE_KEY, JSON.stringify(one('other'))))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(putLayout).not.toHaveBeenCalled()
    // A real edit afterwards is still saved.
    sync.update(one('new'))
    await vi.advanceTimersByTimeAsync(300)
    expect(putLayout).toHaveBeenCalledTimes(1)
    expect(putLayout).toHaveBeenCalledWith('default', { indicators: one('new') })
    off()
  })
})

/** Fake range indicator with time inputs (UNIX seconds, 0 = not set) and a number input. */
const MAX_TIME = 4_102_444_800
const fakeProfile: IndicatorDef = {
  id: 'vprofile',
  name: 'Fake VP',
  shortName: 'VP',
  description: 'test',
  placement: 'overlay',
  inputs: [
    {
      key: 'from',
      label: 'From',
      kind: 'time',
      default: 0,
      min: 0,
      max: MAX_TIME,
      step: 60,
      integer: false,
    },
    {
      key: 'to',
      label: 'To',
      kind: 'time',
      default: 0,
      min: 0,
      max: MAX_TIME,
      step: 60,
      integer: true,
    },
    { key: 'rows', label: 'Rows', default: 24, min: 4, max: 200, step: 1, integer: true },
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper: () => smaStepper(1),
}

describe('sanitizeInputs: time inputs', () => {
  it('keeps whole UNIX seconds and rounds fractional ones even when integer is false', () => {
    expect(sanitizeInputs(fakeProfile, { from: 1_700_000_000, to: 1_700_003_600.4 })).toEqual({
      from: 1_700_000_000,
      to: 1_700_003_600,
      rows: 24,
    })
    expect(sanitizeInputs(fakeProfile, { from: 1_700_000_000.6 }).from).toBe(1_700_000_001)
  })

  it('clamps to [min, max]', () => {
    expect(sanitizeInputs(fakeProfile, { from: -5, to: MAX_TIME + 10 })).toMatchObject({
      from: 0,
      to: MAX_TIME,
    })
  })

  it('falls back to the default 0 (not set) for invalid values', () => {
    expect(sanitizeInputs(fakeProfile, { from: 'yesterday', to: NaN })).toMatchObject({
      from: 0,
      to: 0,
    })
    expect(sanitizeInputs(fakeProfile, { from: Infinity }).from).toBe(0)
  })
})

describe('add with input overrides', () => {
  it('applies sanitized overrides at creation and keeps defaults for the rest', () => {
    const items = indicatorsReducer([], {
      type: 'add',
      def: fakeProfile,
      uid: 'vp-1',
      inputs: { from: 1_700_000_000 },
    })
    expect(items[0].inputs).toEqual({ from: 1_700_000_000, to: 0, rows: 24 })
  })

  it('clamps out-of-range overrides and ignores invalid or unknown ones', () => {
    const c = createIndicatorConfig(fakeProfile, [], 'vp-1', {
      from: 1_700_000_000.4,
      to: NaN,
      rows: 1000,
      evil: 5,
    })
    expect(c.inputs).toEqual({ from: 1_700_000_000, to: 0, rows: 200 })
  })

  it('an add without inputs still uses the defaults (existing callers)', () => {
    const items = indicatorsReducer([], { type: 'add', def: fakeProfile, uid: 'vp-1' })
    expect(items[0].inputs).toEqual({ from: 0, to: 0, rows: 24 })
    expect(createIndicatorConfig(fakeEma, []).inputs).toEqual({ period: 20 })
  })

  it('round-trips a time input through save / load', () => {
    const items = indicatorsReducer([], {
      type: 'add',
      def: fakeProfile,
      uid: 'vp-1',
      inputs: { from: 1_700_000_000, to: 1_700_086_400 },
    })
    const storage = new MemoryStorage()
    saveIndicators(items, storage)
    const defs = new Map<IndicatorId, IndicatorDef>([['vprofile', fakeProfile]])
    expect(loadIndicators(defs, storage)[0].inputs).toEqual({
      from: 1_700_000_000,
      to: 1_700_086_400,
      rows: 24,
    })
  })
})

describe('anchorInputs', () => {
  it('sets the first time input of a time-anchored indicator', () => {
    const avwap = INDICATOR_BY_ID.get('avwap')!
    const vprofile = INDICATOR_BY_ID.get('vprofile')!
    expect(anchorInputs(avwap, 1_700_000_040)).toEqual({ anchor: 1_700_000_040 })
    expect(anchorInputs(vprofile, 1_700_000_040)).toEqual({ from: 1_700_000_040 })
  })

  it('is empty without a time input or without a usable time', () => {
    expect(anchorInputs(INDICATOR_BY_ID.get('ema')!, 1_700_000_040)).toEqual({})
    expect(anchorInputs(INDICATOR_BY_ID.get('avwap')!, null)).toEqual({})
    expect(anchorInputs(INDICATOR_BY_ID.get('avwap')!, 0)).toEqual({})
    expect(anchorInputs(INDICATOR_BY_ID.get('avwap')!, Number.NaN)).toEqual({})
  })
})
