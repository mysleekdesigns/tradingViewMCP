import { describe, expect, it } from 'vitest'
import { getIndicatorDef } from '../indicators/registry'
import type { IndicatorDef, IndicatorId, IndicatorStepper, InputDef } from '../indicators/types'
import {
  checkInput,
  defaultDraft,
  draftErrors,
  draftFromConfig,
  formatTimeLabel,
  hasStyleTab,
  inputText,
  isToggleOn,
  parseDraft,
  parseTimeInputText,
  splitInputs,
  timeInputText,
  toggleText,
} from './indicatorForm'

function def(id: IndicatorId): IndicatorDef {
  const d = getIndicatorDef(id)
  if (!d) throw new Error(`missing ${id}`)
  return d
}

const LENGTH: InputDef = {
  key: 'period',
  label: 'Length',
  default: 20,
  min: 1,
  max: 500,
  step: 1,
  integer: true,
}
const MULT: InputDef = {
  key: 'mult',
  label: 'StdDev',
  default: 2,
  min: 0.1,
  max: 10,
  step: 0.1,
  integer: false,
}

describe('checkInput', () => {
  it('accepts in-range numbers, with surrounding spaces', () => {
    expect(checkInput(LENGTH, '50')).toEqual({ ok: true, value: 50 })
    expect(checkInput(LENGTH, ' 1 ')).toEqual({ ok: true, value: 1 })
    expect(checkInput(LENGTH, '500')).toEqual({ ok: true, value: 500 })
    expect(checkInput(MULT, '2.5')).toEqual({ ok: true, value: 2.5 })
    expect(checkInput(MULT, '.5')).toEqual({ ok: true, value: 0.5 })
  })

  it('rejects empty and non-numeric text', () => {
    expect(checkInput(LENGTH, '')).toEqual({ ok: false, error: 'Enter a number' })
    expect(checkInput(LENGTH, '   ')).toEqual({ ok: false, error: 'Enter a number' })
    expect(checkInput(LENGTH, 'abc')).toEqual({ ok: false, error: 'Enter a number' })
    expect(checkInput(LENGTH, '12px')).toEqual({ ok: false, error: 'Enter a number' })
    expect(checkInput(LENGTH, 'Infinity')).toEqual({ ok: false, error: 'Enter a number' })
    expect(checkInput(LENGTH, '0x10')).toEqual({ ok: false, error: 'Enter a number' })
  })

  it('rejects decimals for integer inputs instead of rounding', () => {
    expect(checkInput(LENGTH, '14.5')).toEqual({ ok: false, error: 'Enter a whole number' })
    expect(checkInput(LENGTH, '14.0')).toEqual({ ok: true, value: 14 })
  })

  it('rejects out-of-range values instead of clamping', () => {
    expect(checkInput(LENGTH, '0')).toEqual({ ok: false, error: 'Must be between 1 and 500' })
    expect(checkInput(LENGTH, '501')).toEqual({ ok: false, error: 'Must be between 1 and 500' })
    expect(checkInput(LENGTH, '-5')).toEqual({ ok: false, error: 'Must be between 1 and 500' })
    expect(checkInput(MULT, '0.05')).toEqual({ ok: false, error: 'Must be between 0.1 and 10' })
  })
})

describe('drafts', () => {
  it('builds a draft from a config as text', () => {
    const bb = def('bb')
    const draft = draftFromConfig(bb, {
      inputs: { period: 30, mult: 2.5 },
      source: 'hlc3',
      colors: { basis: '#111111', upper: '#222222', lower: '#333333' },
    })
    expect(draft).toEqual({
      inputs: { period: '30', mult: '2.5' },
      source: 'hlc3',
      colors: { basis: '#111111', upper: '#222222', lower: '#333333' },
    })
  })

  it('fills missing config entries from the def', () => {
    const bb = def('bb')
    const draft = draftFromConfig(bb, { inputs: {}, source: 'close', colors: {} })
    expect(draft.inputs).toEqual({ period: '20', mult: '2' })
    expect(draft.colors).toEqual({ basis: '#ff6d00', upper: '#2962ff', lower: '#089981' })
  })

  it('defaultDraft resets inputs, source and line colours', () => {
    const macd = def('macd')
    const d = defaultDraft(macd)
    expect(d.source).toBe(macd.source)
    for (const i of macd.inputs) expect(d.inputs[i.key]).toBe(String(i.default))
    for (const l of macd.lines) expect(d.colors[l.key]).toBe(l.color)
    expect(defaultDraft(def('vwap'))).toEqual({
      inputs: {},
      source: 'close',
      colors: { vwap: '#ab47bc' },
    })
  })
})

describe('parseDraft', () => {
  it('returns numbers, source and lower-cased colours for a valid draft', () => {
    const bb = def('bb')
    const r = parseDraft(bb, {
      inputs: { period: ' 50 ', mult: '1.5' },
      source: 'ohlc4',
      colors: { basis: '#ABCDEF', upper: '#2962ff', lower: '#089981' },
    })
    expect(r).toEqual({
      ok: true,
      value: {
        inputs: { period: 50, mult: 1.5 },
        source: 'ohlc4',
        colors: { basis: '#abcdef', upper: '#2962ff', lower: '#089981' },
      },
    })
  })

  it('reports an error per invalid field', () => {
    const bb = def('bb')
    const r = parseDraft(bb, {
      inputs: { period: '2.5', mult: '' },
      source: 'close',
      colors: { basis: 'red', upper: '#2962ff', lower: '#089981' },
    })
    expect(r).toEqual({
      ok: false,
      errors: {
        period: 'Enter a whole number',
        mult: 'Enter a number',
        'color:basis': 'Invalid colour',
      },
    })
  })

  it('rejects an unknown source only when the indicator has a source selector', () => {
    const rsi = def('rsi')
    const bad = { inputs: { period: '14' }, source: 'vwap' as never, colors: { rsi: '#7e57c2' } }
    expect(draftErrors(rsi, bad)).toEqual({ source: 'Choose a source' })
    // Stochastic reads whole candles: the source is ignored and saved as 'close'.
    const stoch = def('stoch')
    const r = parseDraft(stoch, { ...defaultDraft(stoch), source: 'high' })
    expect(r.ok && r.value.source).toBe('close')
  })

  it('round-trips the defaults', () => {
    for (const id of ['sma', 'ema', 'bb', 'vwap', 'volma', 'rsi', 'macd', 'stoch'] as const) {
      const d = def(id)
      const r = parseDraft(d, defaultDraft(d))
      expect(r.ok).toBe(true)
      if (r.ok) {
        for (const i of d.inputs) expect(r.value.inputs[i.key]).toBe(i.default)
      }
    }
  })
})

/** A time input as the range indicators declare it (0 = not set, up to 2100-01-01). */
const MAX_TIME = 4_102_444_800
const ANCHOR: InputDef = {
  key: 'anchor',
  label: 'Anchor',
  kind: 'time',
  default: 0,
  min: 0,
  max: MAX_TIME,
  step: 60,
  integer: true,
}
const EMPTY_STEPPER: IndicatorStepper = { next: () => ({}), clone: () => EMPTY_STEPPER }
// 2023-11-14 22:13:20 UTC
const T = 1_700_000_000

describe('time inputs (UTC datetime-local)', () => {
  it('formats UNIX seconds as UTC datetime-local text, minutes only when seconds are 0', () => {
    expect(timeInputText(1_699_999_980)).toBe('2023-11-14T22:13')
    expect(timeInputText(T)).toBe('2023-11-14T22:13:20')
    expect(timeInputText(Date.UTC(2024, 1, 29, 0, 0) / 1000)).toBe('2024-02-29T00:00')
  })

  it('shows 0 (not set) as the empty field, labelled "Not set"', () => {
    expect(timeInputText(0)).toBe('')
    expect(timeInputText(NaN)).toBe('')
    expect(formatTimeLabel(0)).toBe('Not set')
    expect(formatTimeLabel(1_699_999_980)).toBe('2023-11-14 22:13 UTC')
  })

  it('parses datetime-local text as UTC, with or without seconds; empty is 0', () => {
    expect(parseTimeInputText('2023-11-14T22:13')).toBe(1_699_999_980)
    expect(parseTimeInputText(' 2023-11-14T22:13:20 ')).toBe(T)
    expect(parseTimeInputText('2023-11-14T22:13:20.000')).toBe(T)
    expect(parseTimeInputText('')).toBe(0)
  })

  it('rejects malformed text and dates that do not exist', () => {
    for (const bad of [
      'yesterday',
      '2023-11-14',
      '2023-11-14 22:13',
      '2023-02-30T10:00',
      '2023-13-01T00:00',
      '2023-11-14T24:00',
      '1700000000',
    ]) {
      expect(parseTimeInputText(bad)).toBeNull()
    }
  })

  it('round-trips seconds -> text -> seconds', () => {
    for (const s of [1, 60, 86_400, 1_699_999_980, T, 1_709_164_800, MAX_TIME]) {
      expect(parseTimeInputText(timeInputText(s))).toBe(s)
    }
  })

  it('checkInput: empty is not set (0), invalid and out-of-range text are errors', () => {
    expect(checkInput(ANCHOR, '')).toEqual({ ok: true, value: 0 })
    expect(checkInput(ANCHOR, '2023-11-14T22:13')).toEqual({ ok: true, value: 1_699_999_980 })
    expect(checkInput(ANCHOR, 'soon')).toEqual({ ok: false, error: 'Enter a date and time' })
    expect(checkInput(ANCHOR, '2100-01-01T00:01')).toEqual({
      ok: false,
      error: 'Must be between 1970-01-01 00:00:01 UTC and 2100-01-01 00:00 UTC',
    })
    expect(checkInput({ ...ANCHOR, min: 60 }, '')).toEqual({
      ok: false,
      error: 'Choose a date and time',
    })
  })

  it('drafts hold datetime text and parseDraft returns UNIX seconds', () => {
    const vp: IndicatorDef = {
      id: 'vprofile',
      name: 'Fake VP',
      shortName: 'VP',
      description: 'test',
      placement: 'overlay',
      inputs: [ANCHOR, { ...ANCHOR, key: 'to', label: 'To' }, LENGTH],
      source: null,
      lines: [],
      guides: [],
      createStepper: () => EMPTY_STEPPER,
    }
    const draft = draftFromConfig(vp, {
      inputs: { anchor: 1_699_999_980, to: 0, period: 30 },
      source: 'close',
      colors: {},
    })
    expect(draft.inputs).toEqual({ anchor: '2023-11-14T22:13', to: '', period: '30' })
    expect(defaultDraft(vp).inputs).toEqual({ anchor: '', to: '', period: '20' })
    expect(parseDraft(vp, draft)).toEqual({
      ok: true,
      value: { inputs: { anchor: 1_699_999_980, to: 0, period: 30 }, source: 'close', colors: {} },
    })
    expect(
      draftErrors(vp, { ...draft, inputs: { ...draft.inputs, anchor: '2023-02-30T00:00' } }),
    ).toEqual({
      anchor: 'Enter a date and time',
    })
  })

  it('rejects a set "To" before "From" on from/to range indicators', () => {
    const range: IndicatorDef = {
      id: 'vprofile',
      name: 'Fake VP',
      shortName: 'VP',
      description: 'test',
      placement: 'overlay',
      inputs: [
        { ...ANCHOR, key: 'from', label: 'From' },
        { ...ANCHOR, key: 'to', label: 'To' },
      ],
      source: null,
      lines: [],
      guides: [],
      createStepper: () => EMPTY_STEPPER,
    }
    const draft = (from: string, to: string) => ({
      inputs: { from, to },
      source: 'close' as const,
      colors: {},
    })
    const later = '2023-11-14T22:13'
    const earlier = '2023-11-14T22:12'
    expect(draftErrors(range, draft(later, earlier))).toEqual({ to: 'Must be after From' })
    expect(parseDraft(range, draft(later, earlier))).toEqual({
      ok: false,
      errors: { to: 'Must be after From' },
    })
    // Equal times, "To" not set, or "From" not set are all valid.
    expect(draftErrors(range, draft(later, later))).toEqual({})
    expect(draftErrors(range, draft(later, ''))).toEqual({})
    expect(draftErrors(range, draft('', earlier))).toEqual({})
    expect(parseDraft(range, draft(earlier, later))).toMatchObject({ ok: true })
    // A field that does not parse shows its own error, not the range error.
    expect(draftErrors(range, draft(later, 'soon'))).toEqual({ to: 'Enter a date and time' })
    // The real volume profile declares the same from/to time inputs.
    const vp = def('vprofile')
    expect(
      draftErrors(vp, {
        ...defaultDraft(vp),
        inputs: { ...defaultDraft(vp).inputs, from: later, to: earlier },
      }),
    ).toMatchObject({ to: 'Must be after From' })
    // Indicators without a from/to pair get no cross-field check.
    expect(draftErrors({ ...range, inputs: [ANCHOR] }, draft(later, earlier))).toEqual({})
  })
})

describe('toggle inputs (checkboxes stored as 1 / 0)', () => {
  const toggle = (key: string, label: string, dflt: 0 | 1): InputDef => ({
    key,
    label,
    default: dflt,
    min: 0,
    max: 1,
    step: 1,
    integer: true,
    kind: 'toggle',
  })
  const SWINGS = toggle('showSwings', 'Swings', 1)
  const ZONES = toggle('showZones', 'Flip zones', 0)
  // Like market structure: numbers first, then toggles; no lines (drawn by a primitive).
  const ms: IndicatorDef = {
    id: 'mstructure',
    name: 'Fake Structure',
    shortName: 'MS',
    description: 'test',
    placement: 'overlay',
    inputs: [SWINGS, LENGTH, ZONES],
    source: null,
    lines: [],
    guides: [],
    createStepper: () => EMPTY_STEPPER,
    structure: () => ({ swings: [], events: [], zones: [], state: 'none' }),
  }

  it('checkInput accepts only the checkbox values "1" and "0"', () => {
    expect(checkInput(SWINGS, '1')).toEqual({ ok: true, value: 1 })
    expect(checkInput(SWINGS, '0')).toEqual({ ok: true, value: 0 })
    for (const bad of ['', '2', '-1', '0.5', '1.0', 'true', 'on', 'x']) {
      expect(checkInput(SWINGS, bad)).toEqual({ ok: false, error: 'Choose on or off' })
    }
  })

  it('field text is "1" / "0"; a stray stored value shows the default', () => {
    expect(toggleText(SWINGS, 1)).toBe('1')
    expect(toggleText(SWINGS, 0)).toBe('0')
    expect(toggleText(SWINGS, 0.5)).toBe('1')
    expect(toggleText(ZONES, 7)).toBe('0')
    expect(inputText(ZONES, 1)).toBe('1')
    expect(isToggleOn('1')).toBe(true)
    expect(isToggleOn('0')).toBe(false)
    expect(isToggleOn(undefined)).toBe(false)
  })

  it('drafts hold "1" / "0" and parseDraft saves 1 / 0', () => {
    const draft = draftFromConfig(ms, {
      inputs: { showSwings: 0, period: 30, showZones: 1 },
      source: 'close',
      colors: {},
    })
    expect(draft.inputs).toEqual({ showSwings: '0', period: '30', showZones: '1' })
    expect(defaultDraft(ms).inputs).toEqual({ showSwings: '1', period: '20', showZones: '0' })
    expect(parseDraft(ms, draft)).toEqual({
      ok: true,
      value: { inputs: { showSwings: 0, period: 30, showZones: 1 }, source: 'close', colors: {} },
    })
    expect(draftErrors(ms, { ...draft, inputs: { ...draft.inputs, showZones: '2' } })).toEqual({
      showZones: 'Choose on or off',
    })
  })

  it('splitInputs puts value fields first and toggles after, each in definition order', () => {
    const { fields, toggles } = splitInputs(ms)
    expect(fields.map((i) => i.key)).toEqual(['period'])
    expect(toggles.map((i) => i.key)).toEqual(['showSwings', 'showZones'])
    expect(splitInputs(def('bb')).toggles).toEqual([])
  })

  it('defs without lines get no Style tab; defs with lines keep it', () => {
    expect(hasStyleTab(ms)).toBe(false)
    expect(hasStyleTab(def('vprofile'))).toBe(false)
    expect(hasStyleTab(def('ema'))).toBe(true)
    expect(hasStyleTab(def('macd'))).toBe(true)
  })
})
