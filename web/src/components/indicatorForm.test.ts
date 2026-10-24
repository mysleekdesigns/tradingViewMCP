import { describe, expect, it } from 'vitest'
import { getIndicatorDef } from '../indicators/registry'
import type { IndicatorDef, IndicatorId, InputDef } from '../indicators/types'
import { checkInput, defaultDraft, draftErrors, draftFromConfig, parseDraft } from './indicatorForm'

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
