import { describe, expect, it } from 'vitest'
import { buildChartData, buildTradeMarkers, DEFAULT_TRADE_COLORS } from '../chart/backtestOverlay'
import type { BacktestReport, LabTrade } from '../data/labTypes'
import { COINBASE_VENUE, DIRECT_VENUE, type VenueInfo } from '../data/venue'
import {
  alertsUnavailableReason,
  fallbackSymbol,
  formatClock,
  labOnChart,
  labOverlayBlockedNotice,
  providerNeedsReload,
  providerProfile,
  shortcutHint,
  symbolFallbackNotice,
  toggledTheme,
} from './appSettings'
import { defaultSettings, type Settings } from './settings'

const settings = (over: Partial<Settings> = {}): Settings => ({ ...defaultSettings(), ...over })

describe('providerProfile', () => {
  it('keeps server features on for the local server, venue reported later', () => {
    expect(providerProfile('server')).toEqual({
      id: 'server',
      direct: false,
      initialVenue: null,
      errorSource: 'the local server',
      defaultSymbol: 'BTCUSDT',
    })
  })

  it('direct Binance: fixed Binance venue, lab overlays allowed from the start', () => {
    const p = providerProfile('binance')
    expect(p.direct).toBe(true)
    expect(p.initialVenue).toEqual(DIRECT_VENUE)
    expect(p.errorSource).toBe('Binance')
    expect(p.defaultSymbol).toBe('BTCUSDT')
    expect(labOnChart(p.initialVenue)).toBe(true)
  })

  it('Coinbase: own venue and USD default, never draws Binance backtests on its prices', () => {
    const p = providerProfile('coinbase')
    expect(p.direct).toBe(true)
    expect(p.initialVenue).toEqual(COINBASE_VENUE)
    expect(p.errorSource).toBe('Coinbase')
    expect(p.defaultSymbol).toBe('BTCUSD')
    expect(labOnChart(p.initialVenue)).toBe(false)
  })
})

describe('providerNeedsReload', () => {
  it('only a different provider needs a reload', () => {
    expect(providerNeedsReload('server', settings({ provider: 'server' }))).toBe(false)
    expect(providerNeedsReload('server', settings({ provider: 'coinbase' }))).toBe(true)
    expect(providerNeedsReload('coinbase', settings({ provider: 'binance' }))).toBe(true)
  })

  it('ignores theme, colour and time zone changes', () => {
    const next = settings({
      provider: 'binance',
      theme: 'light',
      upColor: '#123456',
      timezone: 'Asia/Tokyo',
    })
    expect(providerNeedsReload('binance', next)).toBe(false)
  })
})

describe('alertsUnavailableReason', () => {
  it('is null when alerts are ready', () => {
    expect(alertsUnavailableReason('server', 'ready')).toBeNull()
    expect(alertsUnavailableReason('coinbase', 'ready')).toBeNull()
  })

  it('names the direct provider and points to Settings', () => {
    const text = alertsUnavailableReason('coinbase', 'unavailable')
    expect(text).toContain('Coinbase Exchange (direct)')
    expect(text).toContain('Settings')
    expect(alertsUnavailableReason('binance', 'unavailable')).toContain('Binance (direct)')
  })

  it('distinguishes an unreachable server from a direct provider', () => {
    expect(alertsUnavailableReason('server', 'unavailable')).toBe(
      'Alerts need the local server, which cannot be reached',
    )
    expect(alertsUnavailableReason('server', 'loading')).toBe('Connecting to the local server…')
  })
})

describe('fallbackSymbol', () => {
  const coinbase = ['BTCUSD', 'ETHUSD', 'BTCUSDT', 'SOLUSD']

  it('keeps a listed symbol', () => {
    expect(fallbackSymbol('BTCUSDT', coinbase, 'BTCUSD')).toBeNull()
    expect(fallbackSymbol('ETHUSD', coinbase, 'BTCUSD')).toBeNull()
  })

  it('switches an unlisted symbol to the provider default', () => {
    expect(fallbackSymbol('SUIUSDT', coinbase, 'BTCUSD')).toBe('BTCUSD')
    expect(fallbackSymbol('BTCUSD', ['BTCUSDT', 'ETHUSDT'], 'BTCUSDT')).toBe('BTCUSDT')
  })

  it('uses the first listed symbol when the default is not listed', () => {
    expect(fallbackSymbol('XYZ', ['ETHUSD', 'SOLUSD'], 'BTCUSD')).toBe('ETHUSD')
  })

  it('does nothing for an empty list (never loops)', () => {
    expect(fallbackSymbol('BTCUSDT', [], 'BTCUSD')).toBeNull()
  })

  it('is a fixed point: applying the fallback once leaves nothing to do', () => {
    const next = fallbackSymbol('DOGEUSDT', coinbase, 'BTCUSD')
    expect(next).not.toBeNull()
    expect(fallbackSymbol(next as string, coinbase, 'BTCUSD')).toBeNull()
  })

  it('writes a short notice', () => {
    expect(symbolFallbackNotice('SUIUSDT', 'BTCUSD', 'Coinbase')).toBe(
      'SUIUSDT is not listed on Coinbase. Showing BTCUSD instead.',
    )
  })
})

const BINANCE_US: VenueInfo = { id: 'binance-us', name: 'Binance.US' }

describe('labOnChart (runtime venue gate)', () => {
  it('allows only the archive venue (Binance global spot)', () => {
    expect(labOnChart(DIRECT_VENUE)).toBe(true)
    expect(labOnChart({ id: 'binance', name: 'Binance' })).toBe(true)
  })

  it('blocks a server on Binance.US, Coinbase, and an unknown venue', () => {
    expect(labOnChart(BINANCE_US)).toBe(false)
    expect(labOnChart(COINBASE_VENUE)).toBe(false)
    expect(labOnChart({ id: 'kraken', name: 'Kraken' })).toBe(false)
  })

  it('draws nothing while the server has not reported its venue', () => {
    expect(providerProfile('server').initialVenue).toBeNull()
    expect(labOnChart(null)).toBe(false)
  })
})

describe('labOverlayBlockedNotice', () => {
  it('Coinbase (direct): names the venue and points to Settings', () => {
    const text = labOverlayBlockedNotice(COINBASE_VENUE, true)
    expect(text).toContain('Coinbase prices')
    expect(text).toContain('Binance spot data')
    expect(text).toContain('Settings')
    expect(text).not.toContain('TV_MARKET_VENUE')
  })

  it('a local server on Binance.US: names Binance.US and the server setting', () => {
    const text = labOverlayBlockedNotice(BINANCE_US, false)
    expect(text).toContain('Binance.US prices')
    expect(text).toContain('TV_MARKET_VENUE=binance')
    expect(text).toContain('Settings')
  })

  it('an unreported venue: says so instead of naming one', () => {
    const text = labOverlayBlockedNotice(null, false)
    expect(text).toContain('not reported')
    expect(text).not.toContain('prices')
  })
})

describe('toggledTheme', () => {
  it('flips dark and light', () => {
    expect(toggledTheme('dark')).toBe('light')
    expect(toggledTheme('light')).toBe('dark')
  })
})

describe('formatClock', () => {
  // 2026-10-25 14:03:07 UTC
  const ms = Date.UTC(2026, 9, 25, 14, 3, 7)

  it('formats UTC as HH:MM:SS', () => {
    expect(formatClock(ms, 'UTC')).toBe('14:03:07')
  })

  it('shifts to the display zone', () => {
    // Tokyo is UTC+9 all year.
    expect(formatClock(ms, 'Asia/Tokyo')).toBe('23:03:07')
    // New York: EDT (UTC-4) on 2026-10-25.
    expect(formatClock(ms, 'America/New_York')).toBe('10:03:07')
  })
})

describe('shortcutHint', () => {
  it('uses macOS glyphs on a Mac and names elsewhere', () => {
    expect(shortcutHint(true, 'screenshot')).toBe('⌥S')
    expect(shortcutHint(false, 'screenshot')).toBe('Alt+S')
    expect(shortcutHint(true, 'theme')).toBe('⌥⇧L')
    expect(shortcutHint(false, 'theme')).toBe('Alt+Shift+L')
    expect(shortcutHint(false, 'help')).toBe('?')
  })
})

describe('trade marker colours follow Settings', () => {
  const trade = (pnl: number): LabTrade =>
    ({
      entry_time: 100,
      entry_price: 10,
      exit_time: 300,
      exit_price: pnl >= 0 ? 11 : 9,
      exit_reason: 'signal',
      return_pct: pnl / 10,
      pnl,
    }) as LabTrade

  it('defaults to the TradingView colours', () => {
    const [, win] = buildTradeMarkers([trade(1)])
    const [, loss] = buildTradeMarkers([trade(-1)])
    expect(win.color).toBe(DEFAULT_TRADE_COLORS.win)
    expect(loss.color).toBe(DEFAULT_TRADE_COLORS.loss)
  })

  it('uses the given win / loss colours for exits; entries stay blue', () => {
    const colors = { win: '#00ff00', loss: '#ff00ff' }
    const [entry, win] = buildTradeMarkers([trade(1)], colors)
    const [, loss] = buildTradeMarkers([trade(-1)], colors)
    expect(win.color).toBe('#00ff00')
    expect(loss.color).toBe('#ff00ff')
    expect(entry.color).toBe('#2962ff')
  })

  it('passes the colours through buildChartData', () => {
    const report = {
      run_id: 'ui',
      trial_id: 1,
      plan_hash: 'h',
      trades: [trade(-1)],
      equity: [],
      benchmark_equity: { buy_and_hold: [], vol_target: [] },
    } as unknown as BacktestReport
    const data = buildChartData(report, { win: '#111111', loss: '#222222' })
    expect(data.markers[1].color).toBe('#222222')
  })
})
