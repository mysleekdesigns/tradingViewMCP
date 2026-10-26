import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import {
  cancelPicking,
  pickAt,
  REPLAY_OFF as REPLAY_STATE_OFF,
  replayVisible,
  startPicking,
} from './replay'
import { createAccount, onBar, onPrice, placeOrder, type PaperOrderRequest } from './paper'
import {
  feedReplayBar,
  LIVE_OFFLINE_MESSAGE,
  liveFeedUpdates,
  liveQuotesOf,
  paperMarks,
  placeReplayOrder,
  priceStamp,
  REPLAY_MARK_OFF,
  REPLAY_PICKING_MESSAGE,
  REPLAY_REVIEW_MESSAGE,
  replayPaperPause,
  replayFeedStep,
  replayTradeBlock,
  type LiveQuote,
  type PanelFeed,
  type ReplayBook,
  type ReplayMark,
} from './paperFeeds'

const bar = (time: number, close = 100): Candle => ({
  time,
  open: close,
  high: close + 1,
  low: close - 1,
  close,
  volume: 1,
})
const bars = [60, 120, 180, 240, 300].map((t) => bar(t))
const upTo = (t: number) => bars.filter((b) => b.time <= t)
const active = (cursorTime: number): ReplayMark => ({ status: 'active', cursorTime })

describe('replayFeedStep', () => {
  it('feeds the one bar a single step reveals', () => {
    expect(replayFeedStep(active(120), active(180), upTo(180))).toEqual({
      kind: 'bars',
      bars: [bars[2]],
    })
  })

  it('feeds every bar of a multi-bar jump, oldest first', () => {
    expect(replayFeedStep(active(60), active(300), upTo(300))).toEqual({
      kind: 'bars',
      bars: [bars[1], bars[2], bars[3], bars[4]],
    })
  })

  it('feeds nothing on a step back or no move', () => {
    expect(replayFeedStep(active(240), active(180), upTo(180))).toEqual({ kind: 'none' })
    expect(replayFeedStep(active(180), active(180), upTo(180))).toEqual({ kind: 'none' })
  })

  it('resets on a start from off and on a pick that lands elsewhere, at the start bar', () => {
    expect(replayFeedStep(REPLAY_MARK_OFF, active(120), upTo(120))).toEqual({
      kind: 'reset',
      at: 120,
    })
    expect(replayFeedStep({ status: 'picking', cursorTime: null }, active(120), upTo(120))).toEqual(
      { kind: 'reset', at: 120 },
    )
    expect(replayFeedStep({ status: 'picking', cursorTime: 240 }, active(120), upTo(120))).toEqual({
      kind: 'reset',
      at: 120,
    })
  })

  it('resumes without a reset after a re-pick, which never showed a bar past the cursor', () => {
    // Re-pick from a replay on bar 180: the chart keeps the replay view (bars <= 180), and a
    // click past the cursor is capped at it, so nothing after 180 was seen during the pick.
    const running = { ...REPLAY_STATE_OFF, status: 'active' as const, cursorTime: 180 }
    const picking = startPicking(running)
    expect(replayVisible(bars, picking, { lastBarClosed: true })?.map((b) => b.time)).toEqual([
      60, 120, 180,
    ])
    const cancelled = cancelPicking(picking)
    const clickedLater = pickAt(bars, picking, 300, { lastBarClosed: true })
    for (const resumed of [cancelled, clickedLater]) {
      expect(resumed).toMatchObject({ status: 'active', cursorTime: 180 })
      // Same account and high-water mark: safe, since the user saw nothing new.
      const prev: ReplayMark = { status: picking.status, cursorTime: picking.cursorTime }
      const next: ReplayMark = { status: resumed.status, cursorTime: resumed.cursorTime }
      expect(replayFeedStep(prev, next, upTo(180))).toEqual({ kind: 'none' })
    }
  })

  it('resets when replay ends, and does nothing while off or picking', () => {
    expect(replayFeedStep(active(180), REPLAY_MARK_OFF, null)).toEqual({ kind: 'reset', at: null })
    expect(replayFeedStep(REPLAY_MARK_OFF, REPLAY_MARK_OFF, null)).toEqual({ kind: 'none' })
    expect(replayFeedStep(active(180), { status: 'picking', cursorTime: 180 }, null)).toEqual({
      kind: 'none',
    })
  })

  it('never feeds a bar at or before the cursor an order was placed on (no look-ahead)', () => {
    // A buy limit placed on bar 120 (after seeing its close) can only fill from bar 180 on.
    const placed = placeOrder(
      createAccount(),
      { symbol: 'BTCUSDT', side: 'buy', type: 'limit', qty: 1, limitPrice: 99.5 },
      { price: 100, time: 120 },
    )
    expect(placed.order.status).toBe('open')
    const step = replayFeedStep(active(120), active(180), upTo(180))
    expect(step.kind).toBe('bars')
    if (step.kind !== 'bars') return
    expect(step.bars.every((b) => b.time > 120)).toBe(true)
    const after = step.bars.reduce((acct, b) => onBar(acct, 'BTCUSDT', b), placed.account)
    expect(after.fills).toHaveLength(1)
    expect(after.fills[0].time).toBe(180)
  })
})

describe('replay high-water mark (no hindsight trading)', () => {
  // Closes rise 100 -> 140 over bars 60..300, so buying at an early close and selling later would
  // be a guaranteed profit if stepping back allowed it.
  const rising = [60, 120, 180, 240, 300].map((t, i) => bar(t, 100 + 10 * i))
  const seen = (t: number) => rising.filter((b) => b.time <= t)
  const buyMkt: PaperOrderRequest = { symbol: 'BTCUSDT', side: 'buy', type: 'market', qty: 1 }

  /** App's replay effect: apply one cursor change to the book. */
  function move(book: ReplayBook, prev: ReplayMark, next: ReplayMark): ReplayBook {
    const step = replayFeedStep(prev, next, seen(next.cursorTime ?? 0))
    if (step.kind === 'reset') return { account: createAccount(), highWater: step.at }
    if (step.kind === 'bars') {
      return step.bars.reduce((b, c) => feedReplayBar(b, 'BTCUSDT', c), book)
    }
    return book
  }

  it('refuses orders after stepping back, until the cursor is back at the latest bar', () => {
    let book: ReplayBook = { account: createAccount(), highWater: null }
    book = move(book, REPLAY_MARK_OFF, active(60))
    expect(book.highWater).toBe(60)
    book = move(book, active(60), active(240))
    expect(book.highWater).toBe(240)
    book = move(book, active(240), active(120))
    expect(book.highWater).toBe(240)

    // Back on bar 120: buying at its close (110) is refused in the engine, market or limit.
    const mkt = placeReplayOrder(book, buyMkt, { price: 110, time: 120 })
    expect(mkt.order).toMatchObject({ status: 'rejected', reason: REPLAY_REVIEW_MESSAGE })
    expect(mkt.book.account.positions).toEqual({})
    const lim = placeReplayOrder(
      book,
      { ...buyMkt, type: 'limit', limitPrice: 105 },
      { price: 110, time: 120 },
    )
    expect(lim.order.status).toBe('rejected')
    expect(replayTradeBlock(book.highWater, 120)).toBe(REPLAY_REVIEW_MESSAGE)

    // Forward to the high-water bar again: trading is open, at that bar's close.
    book = move(mkt.book, active(120), active(240))
    expect(replayTradeBlock(book.highWater, 240)).toBeNull()
    const ok = placeReplayOrder(book, buyMkt, { price: 130, time: 240 })
    expect(ok.order).toMatchObject({ status: 'filled', fillPrice: 130 })
  })

  it('never feeds a bar twice after stepping back and forward again', () => {
    let book: ReplayBook = { account: createAccount(), highWater: null }
    book = move(book, REPLAY_MARK_OFF, active(60))
    // Buy 1 at bar 60's close, then rest a sell limit at 125 (above the 100 close).
    book = placeReplayOrder(book, buyMkt, { price: 100, time: 60 }).book
    book = placeReplayOrder(
      book,
      { symbol: 'BTCUSDT', side: 'sell', type: 'limit', qty: 1, limitPrice: 125 },
      { price: 100, time: 60 },
    ).book
    book = move(book, active(60), active(180))
    // Bar 180's high is 121: not reached yet.
    expect(book.account.fills).toHaveLength(1)
    book = move(book, active(180), active(60))
    book = move(book, active(60), active(240))
    // Bars 120 and 180 were dropped (already fed); only bar 240 (high 131) is new and fills once.
    expect(book.highWater).toBe(240)
    expect(book.account.fills).toHaveLength(2)
    expect(book.account.fills[1]).toMatchObject({ side: 'sell', price: 125, time: 240 })
    book = move(book, active(240), active(120))
    book = move(book, active(120), active(300))
    expect(book.account.fills).toHaveLength(2)
  })

  it('pauses the replay account while picking a start bar: nothing fed, orders refused', () => {
    let book: ReplayBook = { account: createAccount(), highWater: null }
    book = move(book, REPLAY_MARK_OFF, active(180))
    expect(replayPaperPause('picking')).toBe(REPLAY_PICKING_MESSAGE)
    expect(replayPaperPause('active')).toBeNull()
    expect(replayPaperPause('off')).toBeNull()
    // Entering pick mode from a running replay feeds nothing and keeps the account.
    const picking: ReplayMark = { status: 'picking', cursorTime: 180 }
    expect(replayFeedStep(active(180), picking, seen(180))).toEqual({ kind: 'none' })
    expect(move(book, active(180), picking)).toBe(book)
    // The ticket's pause reaches the engine: market and limit orders are rejected.
    const blocked = replayPaperPause(picking.status)
    const mkt = placeReplayOrder(book, buyMkt, { price: 120, time: 180, blocked })
    expect(mkt.order).toMatchObject({ status: 'rejected', reason: REPLAY_PICKING_MESSAGE })
    const lim = placeReplayOrder(
      book,
      { ...buyMkt, type: 'limit', limitPrice: 110 },
      { price: 120, time: 180, blocked },
    )
    expect(lim.order.status).toBe('rejected')
    expect(lim.book.account.positions).toEqual({})
    expect(lim.book.account.fills).toEqual([])
  })

  it('a new start resets the account and its high-water mark; exit clears it', () => {
    let book: ReplayBook = { account: createAccount(), highWater: null }
    book = move(book, REPLAY_MARK_OFF, active(240))
    book = placeReplayOrder(book, buyMkt, { price: 130, time: 240 }).book
    expect(book.account.fills).toHaveLength(1)
    book = move(book, { status: 'picking', cursorTime: 240 }, active(60))
    expect(book).toMatchObject({ highWater: 60 })
    expect(book.account.fills).toEqual([])
    book = move(book, active(60), REPLAY_MARK_OFF)
    expect(book.highWater).toBeNull()
  })
})

const quote = (panelId: string, symbol: string, price: number, time: number): LiveQuote => ({
  panelId,
  symbol,
  price,
  time,
})

describe('liveFeedUpdates', () => {
  it('returns new ticks once and keeps the same map when nothing changed', () => {
    const first = liveFeedUpdates(new Map(), [quote('p0', 'BTC', 10, 1), quote('p1', 'ETH', 2, 1)])
    expect(first.fresh).toHaveLength(2)
    const again = liveFeedUpdates(first.seen, [quote('p0', 'BTC', 10, 1), quote('p1', 'ETH', 2, 1)])
    expect(again.fresh).toEqual([])
    expect(again.seen).toBe(first.seen)
  })

  it('feeds a new tick time even at the same price, and a symbol switch', () => {
    const { seen } = liveFeedUpdates(new Map(), [quote('p0', 'BTC', 10, 1)])
    expect(liveFeedUpdates(seen, [quote('p0', 'BTC', 10, 2)]).fresh).toHaveLength(1)
    expect(liveFeedUpdates(seen, [quote('p0', 'ETH', 10, 1)]).fresh).toHaveLength(1)
  })

  it('skips invalid prices and drops panels that are gone', () => {
    const { seen } = liveFeedUpdates(new Map(), [quote('p0', 'BTC', 10, 1), quote('p1', 'X', 5, 1)])
    const next = liveFeedUpdates(seen, [quote('p0', 'BTC', 10, 1), quote('p2', 'Y', 0, 1)])
    expect(next.fresh).toEqual([])
    expect([...next.seen.keys()]).toEqual(['p0'])
  })
})

describe('price stamps and live-only quotes', () => {
  const c = (time: number, close: number): Candle => ({ ...bar(time, close), close })
  const feed = (over: Partial<PanelFeed>): PanelFeed => ({
    panelId: 'p0',
    symbol: 'BTCUSDT',
    status: 'live',
    stamp: null,
    ...over,
  })

  it('a history load or refresh with the same last bar does not re-feed the price', () => {
    const first = priceStamp(null, 'BTCUSDT', [c(60, 100), c(120, 101)], 1_000_000)
    expect(first).toEqual({ symbol: 'BTCUSDT', barTime: 120, price: 101, at: 1000 })
    const fed = liveFeedUpdates(new Map(), liveQuotesOf([feed({ stamp: first })]))
    expect(fed.fresh).toHaveLength(1)

    // Older bars prepended (loadOlder) a minute later: same stamp, nothing new to feed.
    const older = priceStamp(first, 'BTCUSDT', [c(0, 99), c(60, 100), c(120, 101)], 1_060_000)
    expect(older).toBe(first)
    // A refresh that is still loading (no candles) keeps the stamp as well.
    expect(priceStamp(first, 'BTCUSDT', null, 1_070_000)).toBe(first)
    const again = liveFeedUpdates(fed.seen, liveQuotesOf([feed({ stamp: older })]))
    expect(again.fresh).toEqual([])
  })

  it('stamps a changed close or a new bar with the time it was seen', () => {
    const first = priceStamp(null, 'BTCUSDT', [c(120, 101)], 1_000_000)
    expect(priceStamp(first, 'BTCUSDT', [c(120, 102)], 1_002_500)?.at).toBe(1002.5)
    expect(priceStamp(first, 'BTCUSDT', [c(120, 101), c(180, 101)], 1_003_000)?.at).toBe(1003)
    // Another symbol on the panel starts over.
    expect(priceStamp(first, 'ETHUSDT', [c(120, 101)], 1_004_000)?.symbol).toBe('ETHUSDT')
  })

  it('an offline, reconnecting or connecting panel is not fed and gives no mark', () => {
    const stamp = priceStamp(null, 'ETHUSDT', [c(120, 3000)], 1_000_000)
    for (const status of ['offline', 'reconnecting', 'connecting'] as const) {
      const quotes = liveQuotesOf([feed({ panelId: 'p1', symbol: 'ETHUSDT', status, stamp })])
      expect(quotes).toEqual([])
      expect(liveFeedUpdates(new Map(), quotes).fresh).toEqual([])
      expect(paperMarks(quotes, 'BTCUSDT')).toEqual({})
    }
  })

  it('a panel back live with an unchanged price fills nothing placed while it was offline', () => {
    const stamp = priceStamp(null, 'BTCUSDT', [c(120, 101)], 1_000_000)
    const live = liveFeedUpdates(new Map(), liveQuotesOf([feed({ stamp })]))
    const down = liveFeedUpdates(live.seen, liveQuotesOf([feed({ status: 'offline', stamp })]))
    expect(down.fresh).toEqual([])
    // While offline, a resting buy limit is stamped with the panel's last price time (1000).
    const placed = placeOrder(
      createAccount(),
      { symbol: 'BTCUSDT', side: 'buy', type: 'limit', qty: 1, limitPrice: 101 },
      { price: 102, time: stamp?.at ?? 0 },
    )
    expect(placed.order.status).toBe('open')
    // Back live: the quote keeps its original time, so it is not after the order and never
    // fills it, even though 101 <= the limit.
    const back = liveFeedUpdates(down.seen, liveQuotesOf([feed({ stamp })]))
    const after = back.fresh.reduce((a, q) => onPrice(a, q.symbol, q.price, q.time), placed.account)
    expect(after).toBe(placed.account)
  })

  it('names the offline pause', () => {
    expect(LIVE_OFFLINE_MESSAGE).toBe('Live feed offline: market orders are paused')
  })
})

describe('paperMarks', () => {
  it('takes the newest price per symbol and leaves out the traded symbol', () => {
    expect(
      paperMarks(
        [quote('p0', 'BTC', 10, 1), quote('p1', 'ETH', 2, 1), quote('p2', 'ETH', 3, 5)],
        'BTC',
      ),
    ).toEqual({ ETH: 3 })
  })
})
