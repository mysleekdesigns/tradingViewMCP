import { describe, expect, it } from 'vitest'
import { computeIndicator, defaultInputs } from './compute'
import { INDICATORS, INDICATOR_BY_ID, getIndicatorDef } from './registry'
import type { Candle } from '../types'

const IDS = ['sma', 'ema', 'bb', 'vwap', 'volma', 'rsi', 'macd', 'stoch'] as const

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

  it('every indicator produces only its declared lines, with finite values, at default inputs', () => {
    const data = candles(200)
    for (const def of INDICATORS) {
      const out = computeIndicator(def, data, defaultInputs(def))
      expect(Object.keys(out).sort()).toEqual(def.lines.map((l) => l.key).sort())
      for (const points of Object.values(out)) {
        expect(points.length).toBeGreaterThan(0)
        for (const p of points) expect(Number.isFinite(p.value)).toBe(true)
      }
    }
  })
})
