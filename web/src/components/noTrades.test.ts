import { describe, expect, it } from 'vitest'
import { MAX_AUTO_OLDER_PAGES } from '../chart/seriesSync'
import type { Candle } from '../types'
import { canLoadOlder, noTradesCopy, shouldAutoLoadOlder, type OlderHistoryState } from './noTrades'

const flat = (time: number): Candle => ({
  time,
  open: 1,
  high: 1,
  low: 1,
  close: 1,
  volume: 0,
})

// 2026-10-24 14:00 and 14:02 UTC.
const T0 = Date.UTC(2026, 9, 24, 14, 0) / 1000
const raw = [flat(T0), flat(T0 + 60), flat(T0 + 120)]
const free: OlderHistoryState = {
  loadingOlder: false,
  historyExhausted: false,
  atLimit: false,
  retryBlocked: false,
  olderCooldown: false,
  offline: false,
}
const base = {
  symbol: 'SUIUSDT',
  interval: '1m' as const,
  exchange: 'Binance',
  raw,
  maxBars: 20_000,
  ...free,
}

describe('noTradesCopy', () => {
  it('names the exchange in the title', () => {
    expect(noTradesCopy(base).title).toBe('No trades in this period on Binance')
  })

  it('quotes the symbol, interval, bar count, and UTC span of the empty window', () => {
    expect(noTradesCopy(base).message).toBe(
      'SUIUSDT had no trades in the last 3 bars on 1m between 2026-10-24 14:00 and 2026-10-24 14:02 UTC. Bars without trades are not drawn. Load older history to find the last trades.',
    )
  })

  it('quotes a custom or seconds timeframe as given', () => {
    expect(noTradesCopy({ ...base, interval: '3m' }).message).toContain('on 3m between')
    expect(noTradesCopy({ ...base, interval: '15s' }).message).toContain('on 15s between')
  })

  it('offers older history while there is more', () => {
    expect(noTradesCopy(base)).toMatchObject({
      action: 'Load older history',
      actionDisabled: false,
      note: null,
    })
  })

  it('shows progress and disables the button while a page loads', () => {
    expect(noTradesCopy({ ...base, loadingOlder: true })).toMatchObject({
      action: 'Loading older bars…',
      actionDisabled: true,
    })
  })

  it('says there is no older history once it is exhausted', () => {
    const copy = noTradesCopy({ ...base, historyExhausted: true })
    expect(copy).toMatchObject({ action: 'No older history', actionDisabled: true })
    expect(copy.message).toContain('There is no older history.')
  })

  it('disables the button with the bar limit once it is reached', () => {
    expect(noTradesCopy({ ...base, atLimit: true })).toMatchObject({
      action: 'History limit reached',
      actionDisabled: true,
      note: 'The chart holds at most 20,000 bars. Try a longer interval.',
    })
  })

  it('disables the button with a reason during a Retry-After window', () => {
    expect(noTradesCopy({ ...base, retryBlocked: true })).toMatchObject({
      actionDisabled: true,
      note: 'Binance asked us to wait before the next request.',
    })
  })

  it('disables the button with a reason during the cooldown after a failed page', () => {
    expect(noTradesCopy({ ...base, olderCooldown: true })).toMatchObject({
      actionDisabled: true,
      note: 'The last request failed. Retrying in a few seconds.',
    })
  })

  it('disables the button with a reason while offline', () => {
    expect(noTradesCopy({ ...base, offline: true })).toMatchObject({
      actionDisabled: true,
      note: 'Offline. Check your connection.',
    })
  })

  it('uses the singular for one bar', () => {
    expect(noTradesCopy({ ...base, raw: [flat(T0)] }).message).toContain('in the last 1 bar on 1m')
  })
})

describe('canLoadOlder', () => {
  it('is true only when nothing blocks a request', () => {
    expect(canLoadOlder(free)).toBe(true)
    for (const key of Object.keys(free) as (keyof OlderHistoryState)[]) {
      expect(canLoadOlder({ ...free, [key]: true })).toBe(false)
    }
  })
})

describe('shouldAutoLoadOlder', () => {
  it('requests while under the page cap and a request can run', () => {
    expect(shouldAutoLoadOlder(0, true)).toBe(true)
    expect(shouldAutoLoadOlder(MAX_AUTO_OLDER_PAGES - 1, true)).toBe(true)
  })

  it('stops at the page cap', () => {
    expect(shouldAutoLoadOlder(MAX_AUTO_OLDER_PAGES, true)).toBe(false)
  })

  it('waits while a request cannot run (loading, blocked, cooling down, offline)', () => {
    expect(shouldAutoLoadOlder(0, false)).toBe(false)
  })

  it('a run of empty pages requests exactly MAX_AUTO_OLDER_PAGES pages', () => {
    // The view's effect: one call per landed page while availability stays true.
    let pages = 0
    for (let landed = 0; landed < MAX_AUTO_OLDER_PAGES + 3; landed++) {
      if (shouldAutoLoadOlder(pages, true)) pages += 1
    }
    expect(pages).toBe(MAX_AUTO_OLDER_PAGES)
  })
})
