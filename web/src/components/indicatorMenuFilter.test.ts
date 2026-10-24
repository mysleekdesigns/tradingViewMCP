import { describe, expect, it } from 'vitest'
import { INDICATORS } from '../indicators/registry'
import {
  countByType,
  filterIndicators,
  flattenGroups,
  GROUP_LABELS,
  groupIndicators,
} from './indicatorMenuFilter'

const ids = (defs: readonly { id: string }[]) => defs.map((d) => d.id)

describe('filterIndicators', () => {
  it('returns the registry order for an empty or blank query', () => {
    expect(ids(filterIndicators(INDICATORS, ''))).toEqual(ids(INDICATORS))
    expect(ids(filterIndicators(INDICATORS, '   '))).toEqual(ids(INDICATORS))
  })

  it('matches case-insensitively on shortName', () => {
    expect(ids(filterIndicators(INDICATORS, 'rsi'))).toEqual(['rsi'])
    expect(ids(filterIndicators(INDICATORS, 'RsI'))).toEqual(['rsi'])
  })

  it('ranks name/shortName prefix matches first, then registry order', () => {
    // "Moving Average" (sma) is the only name prefix; others match in name or description.
    expect(ids(filterIndicators(INDICATORS, 'moving'))).toEqual([
      'sma',
      'ema',
      'bb',
      'volma',
      'macd',
    ])
    // Vol MA is a prefix match; VWAP only matches through its description ("Volume-weighted").
    expect(ids(filterIndicators(INDICATORS, 'vol'))).toEqual(['volma', 'vwap'])
  })

  it('matches the id and the description', () => {
    expect(ids(filterIndicators(INDICATORS, 'stoch'))).toEqual(['stoch'])
    expect(ids(filterIndicators(INDICATORS, 'volma'))).toEqual(['volma'])
    expect(ids(filterIndicators(INDICATORS, 'histogram'))).toEqual(['macd'])
    expect(ids(filterIndicators(INDICATORS, 'utc day'))).toEqual(['vwap'])
  })

  it('trims the query and returns nothing when nothing matches', () => {
    expect(ids(filterIndicators(INDICATORS, '  ema '))).toEqual(['ema'])
    expect(filterIndicators(INDICATORS, 'zzz')).toEqual([])
  })
})

describe('groupIndicators', () => {
  it('groups by placement as Overlays, Volume, Oscillators in registry order', () => {
    const groups = groupIndicators(INDICATORS)
    expect(groups.map((g) => g.label)).toEqual(['Overlays', 'Volume', 'Oscillators'])
    expect(groups.map((g) => ids(g.items))).toEqual([
      ['sma', 'ema', 'bb', 'vwap'],
      ['volma'],
      ['rsi', 'macd', 'stoch'],
    ])
    expect(GROUP_LABELS.pane).toBe('Oscillators')
  })

  it('drops empty groups and keeps the ranked order inside a group', () => {
    const groups = groupIndicators(filterIndicators(INDICATORS, 'moving'))
    expect(groups.map((g) => g.placement)).toEqual(['overlay', 'volume', 'pane'])
    expect(ids(groups[0].items)).toEqual(['sma', 'ema', 'bb'])
    expect(groupIndicators([])).toEqual([])
    expect(groupIndicators(filterIndicators(INDICATORS, 'rsi')).map((g) => g.label)).toEqual([
      'Oscillators',
    ])
  })

  it('flattens groups into display order for keyboard navigation', () => {
    const shown = flattenGroups(groupIndicators(filterIndicators(INDICATORS, 'vol')))
    // Grouping puts the overlay (VWAP) before the volume indicator.
    expect(ids(shown)).toEqual(['vwap', 'volma'])
  })
})

describe('countByType', () => {
  it('counts active instances per type', () => {
    const counts = countByType([{ type: 'ema' }, { type: 'rsi' }, { type: 'ema' }])
    expect(counts.get('ema')).toBe(2)
    expect(counts.get('rsi')).toBe(1)
    expect(counts.get('sma')).toBeUndefined()
  })
})
