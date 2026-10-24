import { describe, expect, it } from 'vitest'
import type { SymbolInfo } from '../types'
import { normalizeQuery, quoteAssetsOf, searchSymbols, tradableSymbols } from './symbolRank'

function sym(baseAsset: string, quoteAsset: string): SymbolInfo {
  return {
    symbol: baseAsset + quoteAsset,
    baseAsset,
    quoteAsset,
    status: 'TRADING',
    tickSize: 0.01,
  }
}

const UNIVERSE: SymbolInfo[] = [
  sym('ETHFI', 'USDT'),
  sym('ETH', 'BTC'),
  sym('ETH', 'USDC'),
  sym('ETH', 'USDT'),
  sym('BTC', 'USDC'),
  sym('BTC', 'USDT'),
  sym('BTC', 'USD'),
  sym('WBTC', 'USDT'),
  sym('SOL', 'USDT'),
  sym('SOL', 'BTC'),
  sym('BNB', 'USDT'),
  sym('BETH', 'ETH'),
]

const names = (r: SymbolInfo[]) => r.map((s) => s.symbol)

describe('normalizeQuery', () => {
  it('uppercases and strips separators', () => {
    expect(normalizeQuery(' btc/usdt ')).toBe('BTCUSDT')
    expect(normalizeQuery('BTC-USDT')).toBe('BTCUSDT')
    expect(normalizeQuery('btc usdt')).toBe('BTCUSDT')
  })
})

describe('searchSymbols', () => {
  it('puts the exact symbol first, case-insensitively and ignoring separators', () => {
    expect(names(searchSymbols(UNIVERSE, 'btc/usdt'))[0]).toBe('BTCUSDT')
    expect(names(searchSymbols(UNIVERSE, 'ethbtc'))[0]).toBe('ETHBTC')
    expect(names(searchSymbols(UNIVERSE, 'Sol-Btc'))[0]).toBe('SOLBTC')
  })

  it('ranks an exact base asset above a longer base with the same prefix', () => {
    // ETH pairs (USDT, USDC, BTC order) before ETHFIUSDT, then BETHETH via substring.
    expect(names(searchSymbols(UNIVERSE, 'eth'))).toEqual([
      'ETHUSDT',
      'ETHUSDC',
      'ETHBTC',
      'ETHFIUSDT',
      'BETHETH',
    ])
  })

  it('ranks symbol prefix above substring above subsequence', () => {
    // "BTCU": prefix of BTCUSDT/BTCUSDC/BTCUSD; substring of WBTCUSDT.
    expect(names(searchSymbols(UNIVERSE, 'btcu'))).toEqual([
      'BTCUSDT',
      'BTCUSDC',
      'BTCUSD',
      'WBTCUSDT',
    ])
    // "SLUS" only matches SOLUSDT, as a subsequence (S-o-L-U-S-dt).
    expect(names(searchSymbols(UNIVERSE, 'slus'))).toEqual(['SOLUSDT'])
  })

  it('breaks ties by quote order, then length, then alphabetically, deterministically', () => {
    const shuffled = [...UNIVERSE].reverse()
    expect(names(searchSymbols(shuffled, 'btc'))).toEqual(names(searchSymbols(UNIVERSE, 'btc')))
    expect(names(searchSymbols(UNIVERSE, 'btc')).slice(0, 3)).toEqual([
      'BTCUSDT',
      'BTCUSDC',
      'BTCUSD',
    ])
  })

  it('returns nothing for a query that matches nothing', () => {
    expect(searchSymbols(UNIVERSE, 'zzzz')).toEqual([])
  })

  it('lists popular bases first for an empty query and respects the limit', () => {
    const r = names(searchSymbols(UNIVERSE, '', 4))
    expect(r).toEqual(['BTCUSDT', 'BTCUSDC', 'BTCUSD', 'ETHUSDT'])
    expect(searchSymbols(UNIVERSE, 'eth', 0)).toEqual([])
  })

  it('filters by quote asset', () => {
    expect(names(searchSymbols(UNIVERSE, 'eth', 50, { quote: 'btc' }))).toEqual(['ETHBTC'])
    expect(names(searchSymbols(UNIVERSE, '', 50, { quote: 'USDC' }))).toEqual([
      'BTCUSDC',
      'ETHUSDC',
    ])
  })
})

describe('quoteAssetsOf', () => {
  it('lists preferred quotes first, then the rest alphabetically', () => {
    expect(quoteAssetsOf(UNIVERSE)).toEqual(['USDT', 'USDC', 'BTC', 'USD', 'ETH'])
  })
})

describe('tradableSymbols', () => {
  it('keeps TRADING symbols quoted in USDT, USDC, BTC, or USD', () => {
    const halted = { ...sym('LUNA', 'USDT'), status: 'BREAK' }
    const r = tradableSymbols([...UNIVERSE, halted, sym('ADA', 'EUR')])
    expect(names(r)).not.toContain('LUNAUSDT')
    expect(names(r)).not.toContain('ADAEUR')
    expect(names(r)).not.toContain('BETHETH')
    expect(names(r)).toContain('BTCUSD')
    expect(r).toHaveLength(UNIVERSE.length - 1)
  })
})
