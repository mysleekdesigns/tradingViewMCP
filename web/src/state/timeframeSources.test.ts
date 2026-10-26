import { describe, expect, it } from 'vitest'
import { secondsSourceKind } from './timeframeSources'

describe('secondsSourceKind', () => {
  it('uses the direct Binance provider’s own 1s klines', () => {
    expect(secondsSourceKind('binance', 'binance')).toBe('provider')
    expect(secondsSourceKind('binance', null)).toBe('provider')
  })

  it('goes straight to Binance for the local server only when it serves Binance global', () => {
    expect(secondsSourceKind('server', 'binance')).toBe('binance-direct')
    expect(secondsSourceKind('server', 'binance-us')).toBeNull()
    expect(secondsSourceKind('server', null)).toBeNull()
  })

  it('has no seconds on Coinbase', () => {
    expect(secondsSourceKind('coinbase', 'coinbase')).toBeNull()
  })
})
