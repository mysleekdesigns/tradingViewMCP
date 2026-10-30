import { createElement, useState } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { CandleSource } from '../data/timeframeProvider'
import { initialSnapshot } from './liveCandles'
import { useLiveCandles, type UseLiveCandles } from './useLiveCandles'

const noop = () => {}

function source(): CandleSource {
  return {
    getCandles: vi.fn(() => new Promise<never>(noop)),
    subscribeCandles: vi.fn(() => noop),
  }
}

describe('useLiveCandles', () => {
  it('returns the same object on a re-render with nothing new', () => {
    const provider = source()
    const seen: UseLiveCandles[] = []
    // Renders twice in one pass (a render-phase update re-runs the component with its hooks'
    // state kept), so both results come from the same hook instance.
    function Probe() {
      const [pass, setPass] = useState(0)
      if (pass === 0) setPass(1)
      seen.push(useLiveCandles(provider, 'BTCUSDT', '1m', 500, 'Binance'))
      return null
    }
    renderToStaticMarkup(createElement(Probe))
    expect(seen.length).toBeGreaterThanOrEqual(2)
    expect(seen[1]).toBe(seen[0])
    // Before the first snapshot: the initial snapshot plus the callbacks.
    expect(seen[0]).toMatchObject(initialSnapshot)
    expect(typeof seen[0].reload).toBe('function')
    expect(typeof seen[0].loadOlder).toBe('function')
  })
})
