import { createElement, isValidElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { createAccount, type PaperAccount } from '../state/paper'
import {
  LIVE_OFFLINE_MESSAGE,
  REPLAY_PICKING_MESSAGE,
  REPLAY_REVIEW_MESSAGE,
} from '../state/paperFeeds'
import type { PaperAccountApi } from '../state/usePaperAccount'
import {
  PAPER_MONITOR_NO_SERVER_MESSAGE,
  PAPER_MONITOR_OFFLINE_MESSAGE,
  type PaperMonitorResponse,
  type PaperMonitorTest,
} from '../data/paperMonitor'
import type { PaperMonitorState } from '../state/usePaperMonitor'
import { PaperMonitor } from './PaperMonitor'
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

describe('Paper tests section', () => {
  /** A current HC test: fixed stake, no sizing block, no protections, no breaker. */
  const hcTest = (over: Partial<PaperMonitorTest> = {}): PaperMonitorTest => ({
    id: 'hc-macd-rsi-btc-4h-exploratory',
    name: 'hc-macd-rsi-btc-4h-exploratory',
    symbol: 'BTCUSDT',
    interval: '4h',
    start: 1_793_318_400,
    decisionEnd: 1_798_156_800,
    wallet: 1000,
    closedTrades: 3,
    openTrades: 1,
    netPnl: -12.5,
    netPnlPct: -1.25,
    drawdownPct: 2.5,
    pnlBasis: 'realized',
    stake: { last: null, riskPerTradePct: null, maxTotalExposurePct: null, openExposurePct: null },
    protections: false,
    protectionLock: null,
    circuitBreaker: null,
    halt: null,
    expected: {
      tradesLo: 2,
      tradesHi: 14,
      exposureLo: 0.0363,
      exposureHi: 0.4754,
      verdict: 'insufficient_data',
      asof: 1_793_793_600,
    },
    read: { protocol: true, trades: true },
    malformed: { protections: false, circuitBreaker: false },
    error: null,
    ...over,
  })

  const response = (tests: PaperMonitorTest[]): PaperMonitorResponse => ({
    generatedAt: 1_793_800_800,
    tests,
  })

  const view = (state: Partial<PaperMonitorState>) =>
    renderToStaticMarkup(
      createElement(PaperMonitor, {
        state: { data: null, error: null, loading: false, ...state },
      }),
    )

  /** Markup with tags dropped, for reading what the row says. */
  const text = (html: string) => html.replace(/<[^>]*>/g, '')

  const halted = hcTest({
    halt: {
      reason: 'daily_loss',
      threshold: 10,
      value: 11.53,
      time: '2026-11-03T14:00:00+00:00',
    },
  })

  it('is a tab of the panel and fetches nothing until opened', () => {
    const html = render({})
    expect(html).toContain('>Tests<')
    expect(html).not.toContain('paper-monitor')
  })

  it('shows one row per test with realized P&L, drawdown and trades against the range', () => {
    const html = view({ data: response([hcTest(), hcTest({ id: 'other', name: 'other' })]) })
    expect(html.match(/<li/g)).toHaveLength(2)
    const row = text(html)
    expect(row).toContain('hc-macd-rsi-btc-4h-exploratory')
    expect(row).toContain('BTCUSDT · 4h · to 2026-12-25')
    expect(row).toContain('Realized P&amp;L −12.50 −1.25%')
    expect(row).toContain('Realized DD 2.50%')
    expect(row).toContain('Trades 3 + 1 open')
    expect(row).toContain('Expected 2–14')
    expect(row).toContain('In market 3.6–47.5%')
    expect(row).toContain('insufficient data')
    expect(row).toContain('Updated 11-04 14:00 UTC')
  })

  it('shows a dash for stake, risk and exposure on a test without a sizing block', () => {
    const row = text(view({ data: response([hcTest()]) }))
    expect(row).toContain('Stake —')
    expect(row).toContain('Risk —')
    expect(row).toContain('Max exposure —')
    expect(row).toContain('Protections Off')
    expect(row).toContain('Breaker None')
  })

  it('shows the sizing block and the breaker of a test that has them', () => {
    const t = hcTest({
      stake: { last: 125.5, riskPerTradePct: 1, maxTotalExposurePct: 80, openExposurePct: 12.55 },
      protections: true,
      circuitBreaker: { maxDailyLossPct: 3, maxDrawdownPct: 10 },
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('Stake 125.50')
    expect(row).toContain('Risk 1.00%')
    expect(row).toContain('Open exposure 12.55%')
    expect(row).toContain('Max exposure 80.00%')
    expect(row).toContain('Protections On')
    expect(row).toContain('Breaker 3.00% / 10.00%')
  })

  it('marks a halted test with its reason, value, limit and time', () => {
    const html = view({ data: response([halted, hcTest({ id: 'other', name: 'other' })]) })
    expect(html).toContain('paper-monitor__row--halted')
    const row = text(html)
    expect(row).toContain('1 HALTED')
    expect(row).toContain('daily loss: 11.53% against a limit of 10.00%, 2026-11-03 14:00 UTC')
  })

  it('shows HALTED without made-up figures when the lock could not be read', () => {
    const t = hcTest({
      halt: { reason: 'unreadable halt.lock', threshold: null, value: null, time: '' },
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('HALTED')
    expect(row).toContain('unreadable halt.lock')
    expect(row).not.toContain('against a limit')
  })

  it('says how a halted test restarts, and that it happens outside the app', () => {
    expect(text(view({ data: response([]) }))).toContain(
      'A halted test restarts only when the owner deletes its halt.lock outside this app.',
    )
  })

  it('offers no action in any state: no button, link, input or form', () => {
    const states: Partial<PaperMonitorState>[] = [
      { loading: true },
      { error: PAPER_MONITOR_OFFLINE_MESSAGE },
      { data: response([]) },
      { data: response([hcTest(), halted]), error: PAPER_MONITOR_OFFLINE_MESSAGE },
      { data: response([hcTest({ error: 'dryrun.sqlite: locked' })]) },
    ]
    for (const s of states) {
      expect(view(s)).not.toMatch(/<(button|a|input|select|textarea|form)[\s>]/)
    }
  })

  it('shows dashes, not zeros, for a test whose DB could not be read', () => {
    const t = hcTest({
      closedTrades: 0,
      openTrades: 0,
      netPnl: 0,
      netPnlPct: 0,
      drawdownPct: 0,
      // The server sends no exposure here; a stale figure must not get through the row's gate.
      stake: { last: null, riskPerTradePct: null, maxTotalExposurePct: null, openExposurePct: 0 },
      read: { protocol: true, trades: false },
      error: 'dryrun.sqlite: file is not a database',
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('dryrun.sqlite: file is not a database')
    expect(row).toContain('Realized P&amp;L —')
    expect(row).toContain('Realized DD —')
    expect(row).toContain('Trades —')
    expect(row).toContain('Open exposure —')
    expect(row).not.toContain('0.00')
  })

  it('shows the figures of a test without a wallet, with drawdown a dash', () => {
    // The row as the server sends it: the DB read fine, so P&L and trades are real.
    const t = hcTest({
      wallet: null,
      closedTrades: 3,
      openTrades: 0,
      netPnl: -12.5,
      netPnlPct: null,
      drawdownPct: 0,
      read: { protocol: true, trades: true },
      error: 'config.json has no dry_run_wallet: drawdown not computed',
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('config.json has no dry_run_wallet')
    expect(row).toContain('Realized P&amp;L −12.50 —')
    expect(row).toContain('Trades 3')
    expect(row).toContain('Realized DD —')
    expect(row).toContain('Open exposure —')
  })

  it('keeps the figures of a test whose comparison is corrupt', () => {
    const t = hcTest({ error: 'comparison-2026-11-04.json is not valid JSON' })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('Realized P&amp;L −12.50 −1.25%')
    expect(row).toContain('Realized DD 2.50%')
    expect(row).toContain('Trades 3 + 1 open')
  })

  it('shows dashes for protections and breaker when the protocol could not be read', () => {
    const t = hcTest({
      protections: false,
      circuitBreaker: null,
      read: { protocol: false, trades: true },
      error: 'protocol.json is not valid JSON',
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('Protections —')
    expect(row).toContain('Breaker —')
    expect(row).not.toContain('Protections Off')
    expect(row).not.toContain('Breaker None')
  })

  it('shows a dash for a malformed breaker, leaving a readable protections block alone', () => {
    const t = hcTest({
      protections: true,
      malformed: { protections: false, circuitBreaker: true },
      error: 'protocol.json circuit_breaker is malformed',
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('Breaker —')
    expect(row).not.toContain('Breaker None')
    expect(row).toContain('Protections On')
  })

  it('shows a dash for malformed protections, leaving a readable breaker alone', () => {
    const t = hcTest({
      circuitBreaker: { maxDailyLossPct: 3, maxDrawdownPct: 10 },
      malformed: { protections: true, circuitBreaker: false },
      error: 'protocol.json protections is malformed',
    })
    const row = text(view({ data: response([t]) }))
    expect(row).toContain('Protections —')
    expect(row).not.toContain('Protections Off')
    expect(row).toContain('Breaker 3.00% / 10.00%')
  })

  it('shows a running protection lock with its reason', () => {
    const t = hcTest({
      protections: true,
      protectionLock: { until: '2026-11-05T04:00:00Z', reason: 'Cooldown period for 2 candles.' },
    })
    const html = view({ data: response([t]) })
    expect(text(html)).toContain('Locked until 2026-11-05 04:00 UTC')
    expect(html).toContain('title="Cooldown period for 2 candles."')
  })

  it('still says locked when the lock end cannot be read', () => {
    const t = hcTest({ protectionLock: { until: '', reason: '' } })
    expect(text(view({ data: response([t]) }))).toContain('Locked until —')
  })

  it('says nothing of a lock when none is running', () => {
    expect(text(view({ data: response([hcTest()]) }))).not.toContain('Locked until')
  })

  it('renders a row with an impossible date as a dash instead of crashing the app', () => {
    const t = hcTest({
      decisionEnd: 9e12,
      expected: {
        tradesLo: 2,
        tradesHi: 14,
        exposureLo: 0.1,
        exposureHi: 0.2,
        verdict: 'continue',
        asof: 9e12,
      },
    })
    const row = text(view({ data: { generatedAt: 9e12, tests: [t] } }))
    expect(row).toContain('BTCUSDT · 4h · to —')
    expect(row).toContain('Updated —')
  })

  it('shows a dash, not a garbled date, for a time sent in milliseconds', () => {
    const row = text(view({ data: response([hcTest({ decisionEnd: 1_798_156_800_000 })]) }))
    expect(row).toContain('to —')
    expect(row).not.toMatch(/to \+?\d{5}/)
  })

  it('shows a null decision end as a dash', () => {
    expect(text(view({ data: response([hcTest({ decisionEnd: null })]) }))).toContain('to —')
  })

  it('prints a halt time beyond the date range as written', () => {
    const time = '+275760-09-13T00:00:00.000Z'
    const t = hcTest({ halt: { reason: 'x', threshold: 1, value: 2, time } })
    expect(text(view({ data: response([t]) }))).toContain(time)
  })

  it('keys each row by its id, so two tests with one name and start both stay', () => {
    const keys: (string | null)[] = []
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(walk)
      if (!isValidElement(node)) return
      const props = node.props as { test?: PaperMonitorTest; children?: unknown }
      if (props.test !== undefined) keys.push(node.key)
      walk(props.children)
    }
    const data = response([hcTest({ id: 'folder-one' }), hcTest({ id: 'folder-two' })])
    walk(PaperMonitor({ state: { data, error: null, loading: false } }))
    expect(keys).toEqual(['folder-one', 'folder-two'])
  })

  it('has loading, unavailable, no-server and empty states', () => {
    expect(text(view({ loading: true }))).toContain('Loading paper tests')
    expect(text(view({ error: PAPER_MONITOR_OFFLINE_MESSAGE }))).toContain(
      PAPER_MONITOR_OFFLINE_MESSAGE,
    )
    expect(text(view({ error: PAPER_MONITOR_NO_SERVER_MESSAGE }))).toContain(
      PAPER_MONITOR_NO_SERVER_MESSAGE,
    )
    expect(text(view({ data: response([]) }))).toContain('No paper tests found')
  })

  it('keeps the last answer on screen, marked, when a refresh fails', () => {
    const row = text(view({ data: response([hcTest()]), error: PAPER_MONITOR_OFFLINE_MESSAGE }))
    expect(row).toContain('hc-macd-rsi-btc-4h-exploratory')
    expect(row).toContain('Not refreshed')
    expect(row).toContain('Updated 11-04 14:00 UTC')
  })
})
