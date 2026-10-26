import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { createAccount, type PaperAccount } from '../state/paper'
import {
  LIVE_OFFLINE_MESSAGE,
  REPLAY_PICKING_MESSAGE,
  REPLAY_REVIEW_MESSAGE,
} from '../state/paperFeeds'
import type { PaperAccountApi } from '../state/usePaperAccount'
import { PaperTradingPanel, type PaperTradingPanelProps } from './PaperTradingPanel'

function api(account: PaperAccount = createAccount(), highWater: number | null = null) {
  const paper: PaperAccountApi = {
    scope: highWater === null ? 'live' : 'replay',
    account,
    highWater,
    place: vi.fn(),
    cancel: vi.fn(),
    onPrice: vi.fn(),
    onBar: vi.fn(),
    reset: vi.fn(),
    restart: vi.fn(),
  }
  return paper
}

function render(over: Partial<PaperTradingPanelProps>): string {
  const props: PaperTradingPanelProps = {
    symbol: 'BTCUSDT',
    price: 50_000,
    time: 1_700_000_000,
    scope: 'live',
    paper: api(),
    ...over,
  }
  return renderToStaticMarkup(createElement(PaperTradingPanel, props))
}

/** The ticket's submit button is disabled. */
const submitDisabled = (html: string) =>
  /<button type="submit" class="paper-ticket__submit" disabled=""/.test(html)

describe('PaperTradingPanel ticket', () => {
  it('trades when live, on the account currency, at the latest bar', () => {
    const html = render({})
    expect(html).not.toContain('paper-ticket__paused')
  })

  it('is disabled while reviewing earlier replay bars', () => {
    const html = render({
      scope: 'replay',
      paper: api(createAccount(), 1_700_000_600),
      time: 1_700_000_000,
    })
    expect(html).toContain(REPLAY_REVIEW_MESSAGE)
    expect(submitDisabled(html)).toBe(true)
    // Back at the high-water bar: open again.
    const back = render({ scope: 'replay', paper: api(createAccount(), 1_700_000_000) })
    expect(back).not.toContain(REPLAY_REVIEW_MESSAGE)
  })

  it('stays on the replay account but is disabled while picking a replay start bar', () => {
    const html = render({
      scope: 'replay',
      paper: api(createAccount(), 1_700_000_000),
      pausedReason: REPLAY_PICKING_MESSAGE,
    })
    expect(html).toContain('Replay')
    expect(html).toContain(REPLAY_PICKING_MESSAGE)
    expect(submitDisabled(html)).toBe(true)
  })

  it('pauses market orders while the live feed is offline', () => {
    const html = render({ offlineReason: LIVE_OFFLINE_MESSAGE })
    expect(html).toContain(LIVE_OFFLINE_MESSAGE)
    expect(submitDisabled(html)).toBe(true)
  })

  it('is disabled for a symbol quoted in another currency, with the reason', () => {
    const html = render({ symbol: 'ETHBTC', price: 0.05, quoteAsset: 'BTC' })
    expect(html).toContain('ETHBTC is quoted in BTC, but this paper account is in USDT.')
    expect(submitDisabled(html)).toBe(true)
    // A Coinbase symbol on a USD account trades.
    const usd = render({ symbol: 'BTC-USD', paper: api(createAccount({ quoteAsset: 'USD' })) })
    expect(usd).not.toContain('paper-ticket__paused')
    expect(usd).toContain('Buy BTC · Market')
  })
})
