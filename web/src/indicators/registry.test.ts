import { describe, expect, it } from 'vitest'
import { computeIndicator, defaultInputs } from './compute'
import { INDICATORS, INDICATOR_BY_ID, getIndicatorDef } from './registry'
import type { Candle } from '../types'

const IDS = [
  'sma',
  'ema',
  'bb',
  'vwap',
  'avwap',
  'vprofile',
  'volma',
  'rsi',
  'macd',
  'stoch',
] as const

function candles(n: number): Candle[] {
  const out: Candle[] = []
  let price = 100
  for (let i = 0; i < n; i++) {
    // Deterministic zig-zag so every indicator leaves warm-up.
    const open = price
    price += ((i * 7) % 11) - 5
    out.push({
      time: 1_700_000_000 + i * 3600,
      open,
      high: Math.max(open, price) + 1,
      low: Math.min(open, price) - 1,
      close: price,
      volume: 10 + (i % 5),
    })
  }
  return out
}

describe('indicator registry', () => {
  it('registers every indicator once, in menu order', () => {
    expect(INDICATORS.map((d) => d.id)).toEqual(IDS)
    expect(INDICATOR_BY_ID.size).toBe(IDS.length)
    expect(getIndicatorDef('rsi')?.name).toBe('Relative Strength Index')
  })

  it('places overlays on price, Volume MA on volume, oscillators in panes', () => {
    const placement = Object.fromEntries(INDICATORS.map((d) => [d.id, d.placement]))
    expect(placement).toEqual({
      sma: 'overlay',
      ema: 'overlay',
      bb: 'overlay',
      vwap: 'overlay',
      avwap: 'overlay',
      vprofile: 'overlay',
      volma: 'volume',
      rsi: 'pane',
      macd: 'pane',
      stoch: 'pane',
    })
  })

  it('has valid defaults: inputs inside their bounds and #rrggbb colours', () => {
    for (const def of INDICATORS) {
      for (const input of def.inputs) {
        expect(input.default).toBeGreaterThanOrEqual(input.min)
        expect(input.default).toBeLessThanOrEqual(input.max)
      }
      for (const line of def.lines) expect(line.color).toMatch(/^#[0-9a-f]{6}$/i)
      const keys = def.lines.map((l) => l.key)
      expect(new Set(keys).size).toBe(keys.length)
    }
  })

  it('time inputs are whole UNIX seconds defaulting to 0 (not set)', () => {
    for (const def of INDICATORS) {
      for (const input of def.inputs) {
        if (input.kind !== 'time') continue
        expect(input.default).toBe(0)
        expect(input.min).toBe(0)
        expect(input.integer).toBe(true)
        expect(input.max).toBeGreaterThan(1_700_000_000)
      }
    }
  })

  it('range indicators have no lines; every other indicator has at least one', () => {
    for (const def of INDICATORS) {
      if (def.profile) expect(def.lines).toEqual([])
      else expect(def.lines.length).toBeGreaterThan(0)
    }
  })

  it('every indicator produces only its declared lines, with finite values, at default inputs', () => {
    const data = candles(200)
    for (const def of INDICATORS) {
      // Time inputs default to 0 (not set), which produces nothing by design; set them to bar
      // open times inside the data (the j-th time input -> bar 20 + 100 j) so the check is real.
      const inputs = defaultInputs(def)
      def.inputs
        .filter((i) => i.kind === 'time')
        .forEach((i, j) => {
          inputs[i.key] = data[20 + 100 * j].time
        })
      const out = computeIndicator(def, data, inputs)
      expect(Object.keys(out).sort()).toEqual(def.lines.map((l) => l.key).sort())
      for (const points of Object.values(out)) {
        expect(points.length).toBeGreaterThan(0)
        for (const p of points) expect(Number.isFinite(p.value)).toBe(true)
      }
    }
  })

  it('range indicators give a finite profile once their range is set, and null at defaults', () => {
    const data = candles(200)
    for (const def of INDICATORS) {
      if (!def.profile) continue
      expect(def.profile(data, defaultInputs(def))).toBeNull()
      const inputs = defaultInputs(def)
      def.inputs
        .filter((i) => i.kind === 'time')
        .forEach((i, j) => {
          inputs[i.key] = data[20 + 100 * j].time
        })
      const p = def.profile(data, inputs)
      expect(p).not.toBeNull()
      if (p === null) continue
      expect(p.rows.length).toBe(inputs.rows)
      for (const v of [p.poc, p.vah, p.val, p.totalVolume]) expect(Number.isFinite(v)).toBe(true)
      expect(p.totalVolume).toBeGreaterThan(0)
    }
  })
})
