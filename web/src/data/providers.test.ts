import { describe, expect, it } from 'vitest'
import { BinanceProvider } from './binance'
import { CoinbaseProvider } from './coinbase'
import { PROVIDER_IDS } from './providerIds'
import { createProvider } from './providers'
import { ServerProvider } from './server'

describe('createProvider', () => {
  it('returns the provider class for each id', () => {
    expect(createProvider('server')).toBeInstanceOf(ServerProvider)
    expect(createProvider('binance')).toBeInstanceOf(BinanceProvider)
    expect(createProvider('coinbase')).toBeInstanceOf(CoinbaseProvider)
  })

  it('returns a fresh instance per call (no shared caches between providers)', () => {
    for (const id of PROVIDER_IDS) expect(createProvider(id)).not.toBe(createProvider(id))
  })

  it('direct providers report their venue', async () => {
    await expect(createProvider('binance').getVenue?.()).resolves.toEqual({
      id: 'binance',
      name: 'Binance',
    })
    await expect(createProvider('coinbase').getVenue?.()).resolves.toEqual({
      id: 'coinbase',
      name: 'Coinbase',
    })
  })
})
