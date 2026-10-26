import { describe, expect, it } from 'vitest'
import {
  MAX_CLOSED_ORDERS,
  PAPER_STORAGE_KEY,
  accountSummary,
  availableCash,
  availableQty,
  cancelOrder,
  createAccount,
  equity,
  estimateOrder,
  executionPrice,
  loadPaperAccount,
  maxOrderQty,
  onBar,
  onPrice,
  openPositions,
  parseAccount,
  placeOrder,
  positionPnl,
  quoteMismatchReason,
  reservedCash,
  roundQtyDown,
  savePaperAccount,
  symbolBaseAsset,
  symbolQuoteAsset,
  type PaperAccount,
  type PaperStorage,
} from './paper'

const T = 1_700_000_000

function memoryStorage(init: Record<string, string> = {}): PaperStorage & {
  data: Record<string, string>
} {
  const data = { ...init }
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v
    },
  }
}

const throwingStorage: PaperStorage = {
  getItem: () => {
    throw new Error('denied')
  },
  setItem: () => {
    throw new Error('quota')
  },
}

const buy = (acct: PaperAccount, qty: number, price: number, time = T) =>
  placeOrder(acct, { symbol: 'BTCUSDT', side: 'buy', type: 'market', qty }, { price, time })
const sell = (acct: PaperAccount, qty: number, price: number, time = T) =>
  placeOrder(acct, { symbol: 'BTCUSDT', side: 'sell', type: 'market', qty }, { price, time })
/** A limit order placed while the market trades at `marketPrice` (50,000 by default). */
const limit = (
  acct: PaperAccount,
  side: 'buy' | 'sell',
  qty: number,
  limitPrice: number,
  time = T,
  symbol = 'BTCUSDT',
  marketPrice = 50_000,
) =>
  placeOrder(acct, { symbol, side, type: 'limit', qty, limitPrice }, { price: marketPrice, time })

const bar = (time: number, open: number, high: number, low: number, close: number) => ({
  time,
  open,
  high,
  low,
  close,
})

describe('createAccount', () => {
  it('defaults to 10,000 USDT, 0.1% fee, no slippage', () => {
    const a = createAccount()
    expect(a.cash).toBe(10_000)
    expect(a.config).toEqual({
      startingBalance: 10_000,
      feeRate: 0.001,
      slippageBps: 0,
      quoteAsset: 'USDT',
    })
    expect(a.orders).toEqual([])
    expect(a.fills).toEqual([])
  })

  it('takes a custom balance and falls back on invalid values', () => {
    expect(createAccount({ startingBalance: 100_000 }).cash).toBe(100_000)
    const bad = createAccount({ startingBalance: -5, feeRate: Number.NaN, slippageBps: 1e9 })
    expect(bad.cash).toBe(10_000)
    expect(bad.config.feeRate).toBe(0.001)
    expect(bad.config.slippageBps).toBe(1000)
  })
})

describe('market orders', () => {
  it('buys at the price and charges the fee in quote', () => {
    const { account, order } = buy(createAccount(), 0.1, 50_000)
    expect(order.status).toBe('filled')
    expect(order.fillPrice).toBe(50_000)
    expect(order.fee).toBeCloseTo(5, 10)
    expect(account.cash).toBeCloseTo(10_000 - 5_000 - 5, 10)
    const pos = account.positions.BTCUSDT
    expect(pos.qty).toBeCloseTo(0.1, 12)
    // Average cost includes the buy fee: (5000 + 5) / 0.1.
    expect(pos.avgPrice).toBeCloseTo(50_050, 8)
    expect(account.fills).toHaveLength(1)
    expect(account.fills[0]).toMatchObject({ side: 'buy', price: 50_000, time: T, realizedPnl: 0 })
  })

  it('applies slippage against the trader', () => {
    const a = createAccount({ slippageBps: 10 })
    const b = buy(a, 0.1, 50_000)
    expect(b.order.fillPrice).toBeCloseTo(50_050, 8)
    const s = sell(b.account, 0.1, 50_000)
    expect(s.order.fillPrice).toBeCloseTo(49_950, 8)
  })

  it('averages the cost over several buys', () => {
    const noFee = createAccount({ feeRate: 0 })
    const one = buy(noFee, 1, 100).account
    const two = buy(one, 1, 200).account
    expect(two.positions.BTCUSDT.avgPrice).toBeCloseTo(150, 10)
    expect(two.positions.BTCUSDT.qty).toBe(2)

    const fee = buy(buy(createAccount(), 1, 100).account, 1, 200).account
    expect(fee.positions.BTCUSDT.avgPrice).toBeCloseTo((100.1 + 200.2) / 2, 10)
  })

  it('realizes P&L on sells with both fees included', () => {
    const bought = buy(createAccount(), 1, 100).account
    const { account, order } = sell(bought, 1, 110)
    expect(order.status).toBe('filled')
    // 110 - 100.1 (cost with buy fee) - 0.11 (sell fee)
    expect(account.positions.BTCUSDT.realizedPnl).toBeCloseTo(9.79, 10)
    expect(account.fills[1].realizedPnl).toBeCloseTo(9.79, 10)
    expect(account.cash).toBeCloseTo(10_009.79, 10)
    expect(account.positions.BTCUSDT.qty).toBe(0)
    expect(account.positions.BTCUSDT.avgPrice).toBe(0)
    expect(openPositions(account)).toEqual([])
    expect(equity(account)).toBeCloseTo(10_009.79, 10)
  })

  it('keeps the average on a partial sell', () => {
    const bought = buy(createAccount({ feeRate: 0 }), 2, 100).account
    const { account } = sell(bought, 0.5, 120)
    expect(account.positions.BTCUSDT.qty).toBeCloseTo(1.5, 12)
    expect(account.positions.BTCUSDT.avgPrice).toBeCloseTo(100, 12)
    expect(account.positions.BTCUSDT.realizedPnl).toBeCloseTo(10, 12)
  })
})

describe('rejections', () => {
  it('rejects a buy that costs more than the cash, leaving the account unchanged', () => {
    const a = createAccount()
    // 0.2 * 50,000 = 10,000 plus a 10 fee > 10,000
    const { account, order } = buy(a, 0.2, 50_000)
    expect(order.status).toBe('rejected')
    expect(order.reason).toMatch(/Insufficient USDT/)
    expect(account.cash).toBe(10_000)
    expect(account.positions).toEqual({})
    expect(account.fills).toEqual([])
    expect(account.orders).toEqual([order])
  })

  it('rejects shorts and sells beyond the held quantity', () => {
    const short = sell(createAccount(), 0.01, 50_000)
    expect(short.order.status).toBe('rejected')
    expect(short.order.reason).toMatch(/Insufficient BTCUSDT/)

    const held = buy(createAccount(), 0.1, 50_000).account
    const tooMuch = sell(held, 0.11, 50_000)
    expect(tooMuch.order.status).toBe('rejected')
    expect(tooMuch.account.positions.BTCUSDT.qty).toBeCloseTo(0.1, 12)
  })

  it('rejects invalid requests', () => {
    const a = createAccount()
    expect(buy(a, 0, 100).order.reason).toMatch(/Quantity/)
    expect(buy(a, Number.NaN, 100).order.reason).toMatch(/Quantity/)
    expect(buy(a, 1, Number.NaN).order.reason).toMatch(/market price/)
    expect(buy(a, 1, 100, Number.NaN).order.reason).toMatch(/market time/)
    expect(limit(a, 'buy', 1, 0).order.reason).toMatch(/Limit price/)
    expect(
      placeOrder(a, { symbol: ' ', side: 'buy', type: 'market', qty: 1 }, { price: 1, time: T })
        .order.reason,
    ).toMatch(/symbol/)
  })

  it('lets a 100% buy through despite float noise and never leaves negative cash', () => {
    const a = createAccount({ startingBalance: 1000 })
    const price = 33_333.33
    const qty = roundQtyDown(maxOrderQty(a, 'BTCUSDT', 'buy', price))
    const { account, order } = buy(a, qty, price)
    expect(order.status).toBe('filled')
    expect(account.cash).toBeGreaterThanOrEqual(0)
    expect(account.cash).toBeLessThan(0.01)
  })
})

describe('limit orders and reserves', () => {
  it('reserves cash (cost + fee) for an open buy limit', () => {
    const { account, order } = limit(createAccount(), 'buy', 0.1, 40_000)
    expect(order.status).toBe('open')
    expect(account.cash).toBe(10_000)
    expect(reservedCash(account)).toBeCloseTo(4_004, 8)
    expect(availableCash(account)).toBeCloseTo(5_996, 8)
    // Reserved cash cannot be spent twice.
    const second = buy(account, 0.12, 50_000)
    expect(second.order.status).toBe('rejected')
    const third = limit(account, 'buy', 0.15, 40_000)
    expect(third.order.status).toBe('rejected')
  })

  it('reserves quantity for an open sell limit', () => {
    const held = buy(createAccount(), 0.1, 50_000).account
    const { account, order } = limit(held, 'sell', 0.06, 60_000)
    expect(order.status).toBe('open')
    expect(availableQty(account, 'BTCUSDT')).toBeCloseTo(0.04, 12)
    expect(sell(account, 0.05, 50_000).order.status).toBe('rejected')
    expect(sell(account, 0.04, 50_000).order.status).toBe('filled')
    expect(limit(account, 'sell', 0.05, 60_000).order.status).toBe('rejected')
  })

  it('cancel releases the reserve', () => {
    const { account, order } = limit(createAccount(), 'buy', 0.1, 40_000)
    const cancelled = cancelOrder(account, order.id, T + 5)
    const o = cancelled.orders.find((x) => x.id === order.id)
    expect(o).toMatchObject({ status: 'cancelled', closedAt: T + 5 })
    expect(availableCash(cancelled)).toBe(10_000)
    // Cancelled orders never fill.
    expect(onPrice(cancelled, 'BTCUSDT', 30_000, T + 10)).toBe(cancelled)

    const held = buy(createAccount(), 0.1, 50_000).account
    const s = limit(held, 'sell', 0.1, 60_000)
    expect(availableQty(cancelOrder(s.account, s.order.id, T), 'BTCUSDT')).toBeCloseTo(0.1, 12)
  })

  it('cancel of an unknown or closed order returns the same account', () => {
    const { account, order } = buy(createAccount(), 0.01, 50_000)
    expect(cancelOrder(account, order.id, T)).toBe(account)
    expect(cancelOrder(account, 'nope', T)).toBe(account)
  })
})

describe('onPrice (live ticks)', () => {
  it('fills a buy limit only on ticks strictly after it was placed', () => {
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000, T)
    // Same second as placement: not after it.
    expect(onPrice(account, 'BTCUSDT', 39_000, T)).toBe(account)
    expect(onPrice(account, 'BTCUSDT', 39_000, T - 60)).toBe(account)
    // Above the limit: no fill (and the same object back).
    expect(onPrice(account, 'BTCUSDT', 40_001, T + 1)).toBe(account)
    // At the limit: fills at the limit.
    const at = onPrice(account, 'BTCUSDT', 40_000, T + 1)
    expect(at.fills[0]).toMatchObject({ side: 'buy', price: 40_000, time: T + 1 })
    expect(at.cash).toBeCloseTo(10_000 - 4_004, 8)
    expect(reservedCash(at)).toBe(0)
    // A tick already through the limit still fills at exactly the limit: the order was resting
    // (not marketable when placed), so it never gets a better price than it asked for.
    const through = onPrice(account, 'BTCUSDT', 39_500, T + 1)
    expect(through.fills[0].price).toBe(40_000)
    expect(through.cash).toBeCloseTo(10_000 - 4_004, 8)
  })

  it('fills a sell limit when price >= limit', () => {
    const held = buy(createAccount({ feeRate: 0 }), 0.1, 50_000).account
    const { account } = limit(held, 'sell', 0.1, 55_000, T + 10)
    expect(onPrice(account, 'BTCUSDT', 54_999, T + 11)).toBe(account)
    const filled = onPrice(account, 'BTCUSDT', 55_000, T + 11)
    expect(filled.positions.BTCUSDT.qty).toBe(0)
    expect(filled.positions.BTCUSDT.realizedPnl).toBeCloseTo(500, 8)
  })

  it('ignores other symbols and invalid ticks', () => {
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000)
    expect(onPrice(account, 'ETHUSDT', 1, T + 1)).toBe(account)
    expect(onPrice(account, 'BTCUSDT', Number.NaN, T + 1)).toBe(account)
    expect(onPrice(account, 'BTCUSDT', 30_000, Number.NaN)).toBe(account)
  })
})

describe('onBar (replay) never looks ahead', () => {
  it('never fills from a bar that opened at or before the order time', () => {
    // Placed while the bar opening at T was the current replay bar.
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000, T)
    expect(onBar(account, 'BTCUSDT', bar(T, 41_000, 41_000, 30_000, 35_000))).toBe(account)
    expect(onBar(account, 'BTCUSDT', bar(T - 60, 41_000, 41_000, 30_000, 35_000))).toBe(account)
    // Placed mid-bar on live data: the forming bar (opened before) must not count either.
    const mid = limit(createAccount(), 'buy', 0.1, 40_000, T + 30)
    expect(mid.order.status).toBe('open')
    expect(onBar(mid.account, 'BTCUSDT', bar(T, 41_000, 41_000, 30_000, 35_000))).toBe(mid.account)
  })

  it('fills at the limit when the next bar trades through it', () => {
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000, T)
    const next = onBar(account, 'BTCUSDT', bar(T + 60, 41_000, 41_500, 39_800, 40_200))
    expect(next.fills).toHaveLength(1)
    expect(next.fills[0]).toMatchObject({ price: 40_000, time: T + 60 })
  })

  it('does not fill when the bar does not reach the limit', () => {
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000, T)
    expect(onBar(account, 'BTCUSDT', bar(T + 60, 41_000, 42_000, 40_001, 41_000))).toBe(account)
  })

  it('a gap through a resting limit fills at exactly the limit, never the better open', () => {
    const buyAcct = limit(createAccount(), 'buy', 0.1, 40_000, T).account
    const gappedDown = onBar(buyAcct, 'BTCUSDT', bar(T + 60, 38_000, 39_000, 37_000, 38_500))
    expect(gappedDown.fills[0]).toMatchObject({ price: 40_000, time: T + 60 })
    // 0.1 * 40,000 = 4,000 plus a 4 fee (0.1%), exactly the reserve.
    expect(gappedDown.cash).toBeCloseTo(10_000 - 4_004, 8)

    const held = buy(createAccount(), 0.1, 50_000, T - 120).account
    const sellAcct = limit(held, 'sell', 0.1, 55_000, T).account
    const gappedUp = onBar(sellAcct, 'BTCUSDT', bar(T + 60, 57_000, 58_000, 56_500, 57_500))
    expect(gappedUp.fills.at(-1)?.price).toBe(55_000)
    // Proceeds 5,500 minus a 5.5 fee.
    expect(gappedUp.cash - sellAcct.cash).toBeCloseTo(5_494.5, 8)
    // Sell limit not reached by the high: no fill.
    expect(onBar(sellAcct, 'BTCUSDT', bar(T + 60, 54_000, 54_999, 53_000, 54_500))).toBe(sellAcct)
  })

  it('a future bar changes nothing about earlier fills (perturbation)', () => {
    const { account } = limit(createAccount(), 'buy', 0.1, 40_000, T)
    const b1 = bar(T + 60, 41_000, 41_000, 39_000, 40_500)
    const after1 = onBar(account, 'BTCUSDT', b1)
    const a = onBar(after1, 'BTCUSDT', bar(T + 120, 40_500, 41_000, 40_000, 40_800))
    const b = onBar(after1, 'BTCUSDT', bar(T + 120, 10, 99_999, 1, 50_000))
    expect(a.fills[0]).toEqual(after1.fills[0])
    expect(b.fills[0]).toEqual(after1.fills[0])
  })
})

describe('marketable limits fill at once like a taker', () => {
  it('a buy limit at or above the market fills now at the market price (live)', () => {
    const { account, order } = limit(createAccount(), 'buy', 0.1, 51_000, T, 'BTCUSDT', 50_000)
    expect(order).toMatchObject({ status: 'filled', type: 'limit', fillPrice: 50_000, closedAt: T })
    // 0.1 * 50,000 = 5,000 plus the normal 0.1% fee: 5.
    expect(order.fee).toBeCloseTo(5, 10)
    expect(account.cash).toBeCloseTo(10_000 - 5_005, 8)
    expect(reservedCash(account)).toBe(0)
    expect(account.fills[0]).toMatchObject({ price: 50_000, time: T })
  })

  it('a sell limit at or below the market fills now at the market price', () => {
    const held = buy(createAccount({ feeRate: 0 }), 0.1, 40_000, T - 60).account
    const { order } = limit(held, 'sell', 0.1, 49_000, T, 'BTCUSDT', 50_000)
    expect(order).toMatchObject({ status: 'filled', fillPrice: 50_000 })
  })

  it('applies slippage but never fills past the limit', () => {
    const a = createAccount({ slippageBps: 10 })
    // Market 50,000 + 10 bps = 50,050, below the 51,000 limit.
    expect(limit(a, 'buy', 0.1, 51_000, T, 'BTCUSDT', 50_000).order.fillPrice).toBeCloseTo(
      50_050,
      8,
    )
    // The 50,020 limit caps the slipped 50,050.
    expect(limit(a, 'buy', 0.1, 50_020, T, 'BTCUSDT', 50_000).order.fillPrice).toBe(50_020)
  })

  it('a marketable limit fills at the replay bar close it was placed on', () => {
    // Replay: placed on the bar whose close is 40,000, buy limit 40,500 -> fills at 40,000.
    const { account } = limit(createAccount(), 'buy', 0.1, 40_500, T, 'BTCUSDT', 40_000)
    expect(account.fills[0]).toMatchObject({ price: 40_000, time: T })
    // A later bar changes nothing (no second fill, no better price).
    expect(onBar(account, 'BTCUSDT', bar(T + 60, 39_000, 39_500, 38_000, 39_000))).toBe(account)
  })

  it('needs a market price to judge a limit', () => {
    const r = placeOrder(
      createAccount(),
      { symbol: 'BTCUSDT', side: 'buy', type: 'limit', qty: 0.1, limitPrice: 40_000 },
      { price: Number.NaN, time: T },
    )
    expect(r.order.reason).toMatch(/market price/)
  })

  it('reports the expected execution price for both kinds of limit', () => {
    const a = createAccount({ slippageBps: 10 })
    expect(executionPrice(a, 'buy', 'limit', 50_000, 51_000)).toBeCloseTo(50_050, 8)
    expect(executionPrice(a, 'buy', 'limit', 50_000, 40_000)).toBe(40_000)
    expect(executionPrice(a, 'sell', 'limit', 50_000, 49_000)).toBeCloseTo(49_950, 8)
  })
})

describe('paused trading', () => {
  const req = { symbol: 'BTCUSDT', side: 'buy' as const, type: 'market' as const, qty: 0.01 }

  it('rejects every order while blocked, with the reason', () => {
    const r = placeOrder(createAccount(), req, { price: 50_000, time: T, blocked: 'Reviewing' })
    expect(r.order).toMatchObject({ status: 'rejected', reason: 'Reviewing' })
    const l = placeOrder(
      createAccount(),
      { ...req, type: 'limit', limitPrice: 40_000 },
      { price: 50_000, time: T, blocked: 'Reviewing' },
    )
    expect(l.order.status).toBe('rejected')
    expect(l.account.fills).toEqual([])
  })

  it('marketBlocked rejects market orders and marketable limits, not resting limits', () => {
    const market = { price: 50_000, time: T, marketBlocked: 'Feed offline' }
    expect(placeOrder(createAccount(), req, market).order).toMatchObject({
      status: 'rejected',
      reason: 'Feed offline',
    })
    const taker = placeOrder(createAccount(), { ...req, type: 'limit', limitPrice: 51_000 }, market)
    expect(taker.order.status).toBe('rejected')
    const resting = placeOrder(
      createAccount(),
      { ...req, type: 'limit', limitPrice: 40_000 },
      market,
    )
    expect(resting.order.status).toBe('open')
  })
})

describe('quote currency', () => {
  it('rejects a symbol quoted in another currency (ETHBTC on a USDT account)', () => {
    const { account, order } = placeOrder(
      createAccount(),
      { symbol: 'ETHBTC', side: 'buy', type: 'market', qty: 1 },
      { price: 0.05, time: T },
    )
    expect(order.status).toBe('rejected')
    expect(order.reason).toBe(
      'ETHBTC is quoted in BTC, but this paper account is in USDT. Reset the account in BTC to trade it.',
    )
    expect(account.cash).toBe(10_000)
    expect(account.positions).toEqual({})
  })

  it('trades Coinbase symbols on a USD account and rejects them on USDT', () => {
    const usd = createAccount({ quoteAsset: 'USD' })
    const btcUsd = { symbol: 'BTC-USD', side: 'buy' as const, type: 'market' as const, qty: 0.1 }
    expect(placeOrder(usd, btcUsd, { price: 50_000, time: T }).order.status).toBe('filled')
    expect(placeOrder(createAccount(), btcUsd, { price: 50_000, time: T }).order.reason).toMatch(
      /quoted in USD/,
    )
    // BTCUSDT is not a USD symbol.
    expect(quoteMismatchReason(usd, 'BTCUSDT')).toMatch(/quoted in USDT/)
  })

  it('prefers the known quote asset over parsing, and parses common forms', () => {
    expect(quoteMismatchReason(createAccount(), 'WEIRD', 'USDT')).toBeNull()
    expect(quoteMismatchReason(createAccount(), 'WEIRD')).toMatch(/Cannot tell/)
    expect(symbolQuoteAsset('BTCUSDT')).toBe('USDT')
    expect(symbolQuoteAsset('BTCFDUSD')).toBe('FDUSD')
    expect(symbolQuoteAsset('BTCTUSD')).toBe('TUSD')
    expect(symbolQuoteAsset('ETHBTC')).toBe('BTC')
    expect(symbolQuoteAsset('BTC-USD')).toBe('USD')
    expect(symbolQuoteAsset('XYZ')).toBeNull()
    expect(symbolBaseAsset('BTC-USD', 'USD')).toBe('BTC')
    expect(symbolBaseAsset('BTCUSDT', 'USDT')).toBe('BTC')
  })
})

describe('derived values', () => {
  it('equity minus start equals realized plus unrealized', () => {
    let a = createAccount()
    a = buy(a, 0.1, 50_000).account
    a = sell(a, 0.04, 52_000).account
    a = placeOrder(
      a,
      { symbol: 'ETHUSDT', side: 'buy', type: 'market', qty: 1 },
      { price: 3000, time: T },
    ).account
    const marks = { BTCUSDT: 49_000, ETHUSDT: 3_100 }
    const s = accountSummary(a, marks)
    expect(s.equity).toBeCloseTo(a.cash + 0.06 * 49_000 + 3_100, 8)
    expect(s.totalPnl).toBeCloseTo(s.realizedPnl + s.unrealizedPnl, 8)
    expect(s.returnPct).toBeCloseTo(s.totalPnl / 10_000, 12)
  })

  it('marks a position at its last fill price when no mark is given', () => {
    const a = buy(createAccount({ feeRate: 0 }), 1, 100).account
    expect(equity(a)).toBeCloseTo(10_000, 10)
    const p = positionPnl(a.positions.BTCUSDT, 110)
    expect(p).toMatchObject({ mark: 110, marketValue: 110, unrealized: 10, realized: 0 })
    expect(p.unrealizedPct).toBeCloseTo(0.1, 12)
    expect(positionPnl(a.positions.BTCUSDT, null).mark).toBe(100)
  })

  it('estimates cost, fee and the execution price', () => {
    const a = createAccount({ slippageBps: 20 })
    expect(executionPrice(a, 'buy', 'market', 100, null)).toBeCloseTo(100.2, 12)
    expect(executionPrice(a, 'sell', 'market', 100, null)).toBeCloseTo(99.8, 12)
    expect(executionPrice(a, 'buy', 'limit', 100, 95)).toBe(95)
    expect(executionPrice(a, 'buy', 'market', null, null)).toBeNull()
    expect(estimateOrder(a, 'buy', 2, 100)).toEqual({
      price: 100,
      notional: 200,
      fee: 0.2,
      total: 200.2,
    })
    expect(estimateOrder(a, 'sell', 2, 100)?.total).toBeCloseTo(199.8, 12)
    expect(estimateOrder(a, 'buy', 0, 100)).toBeNull()
  })

  it('rounds quantities down', () => {
    expect(roundQtyDown(0.123456789)).toBe(0.12345678)
    expect(roundQtyDown(0.29, 2)).toBe(0.29)
    expect(roundQtyDown(-1)).toBe(0)
  })
})

describe('ids and history', () => {
  it('uses an injected id generator', () => {
    let n = 0
    const newId = (kind: 'order' | 'fill') => `${kind}-${++n}`
    const { account, order } = placeOrder(
      createAccount(),
      { symbol: 'btcusdt', side: 'buy', type: 'market', qty: 0.01 },
      { price: 50_000, time: T },
      { newId },
    )
    expect(order.id).toBe('order-1')
    expect(order.symbol).toBe('BTCUSDT')
    expect(account.fills[0].id).toBe('fill-2')
    expect(account.fills[0].orderId).toBe('order-1')
  })

  it('default ids are unique and deterministic', () => {
    const a = buy(buy(createAccount(), 0.01, 100).account, 0.01, 100).account
    const ids = [...a.orders.map((o) => o.id), ...a.fills.map((f) => f.id)]
    expect(new Set(ids).size).toBe(ids.length)
    expect(buy(buy(createAccount(), 0.01, 100).account, 0.01, 100).account).toEqual(a)
  })

  it('keeps open orders when pruning closed history', () => {
    let a = limit(createAccount(), 'buy', 0.01, 1).account
    const openId = a.orders[0].id
    for (let i = 0; i < MAX_CLOSED_ORDERS + 20; i++) a = buy(a, 0, 1).account // rejected
    expect(a.orders).toHaveLength(MAX_CLOSED_ORDERS + 1)
    expect(a.orders[0].id).toBe(openId)
  })
})

describe('persistence', () => {
  it('round-trips through storage', () => {
    let a = buy(createAccount({ startingBalance: 5000 }), 0.01, 50_000).account
    a = limit(a, 'buy', 0.01, 40_000).account
    const s = memoryStorage()
    savePaperAccount(a, s)
    expect(Object.keys(s.data)).toEqual([PAPER_STORAGE_KEY])
    expect(loadPaperAccount(s)).toEqual(a)
  })

  it('starts fresh on missing, corrupt or foreign data', () => {
    const fresh = createAccount()
    expect(loadPaperAccount(memoryStorage())).toEqual(fresh)
    expect(loadPaperAccount(memoryStorage({ [PAPER_STORAGE_KEY]: '{not json' }))).toEqual(fresh)
    expect(loadPaperAccount(memoryStorage({ [PAPER_STORAGE_KEY]: '"text"' }))).toEqual(fresh)
    expect(loadPaperAccount(memoryStorage({ [PAPER_STORAGE_KEY]: 'null' }))).toEqual(fresh)
    expect(
      loadPaperAccount(
        memoryStorage({ [PAPER_STORAGE_KEY]: JSON.stringify({ version: 2, cash: 1 }) }),
      ),
    ).toEqual(fresh)
    expect(
      loadPaperAccount(
        memoryStorage({ [PAPER_STORAGE_KEY]: JSON.stringify({ version: 1, cash: -1 }) }),
      ),
    ).toEqual(fresh)
  })

  it('works without storage and never throws', () => {
    expect(loadPaperAccount(null)).toEqual(createAccount())
    expect(loadPaperAccount(throwingStorage)).toEqual(createAccount())
    expect(() => savePaperAccount(createAccount(), throwingStorage)).not.toThrow()
    expect(() => savePaperAccount(createAccount(), null)).not.toThrow()
  })

  it('drops bad entries one by one and keeps the rest', () => {
    const good = buy(createAccount(), 0.01, 50_000).account
    const raw = JSON.parse(JSON.stringify(good)) as Record<string, unknown>
    raw.orders = [...(raw.orders as unknown[]), { id: 'x' }, 'junk', null]
    raw.fills = [...(raw.fills as unknown[]), { id: 'f', price: 'NaN' }]
    raw.positions = {
      ...(raw.positions as object),
      ETHUSDT: { qty: 'lots' },
      SOLUSDT: { qty: 1, avgPrice: 0 },
    }
    raw.config = { startingBalance: 'big', feeRate: 0.002 }
    const parsed = parseAccount(raw)
    expect(parsed).not.toBeNull()
    expect(parsed?.orders).toEqual(good.orders)
    expect(parsed?.fills).toEqual(good.fills)
    expect(Object.keys(parsed?.positions ?? {})).toEqual(['BTCUSDT'])
    expect(parsed?.config.startingBalance).toBe(10_000)
    expect(parsed?.config.feeRate).toBe(0.002)
  })

  it('cancels open orders whose reserve the stored cash cannot cover', () => {
    const a = limit(createAccount(), 'buy', 0.1, 40_000).account
    const raw = { ...JSON.parse(JSON.stringify(a)), cash: 100 } as unknown
    const parsed = parseAccount(raw)
    expect(parsed?.orders[0]).toMatchObject({ status: 'cancelled' })
    expect(parsed?.orders[0].reason).toMatch(/Cancelled on load/)
    expect(reservedCash(parsed as PaperAccount)).toBe(0)
  })

  it('never reuses a stored id after a bad counter', () => {
    const a = buy(createAccount(), 0.01, 100).account
    const raw = { ...JSON.parse(JSON.stringify(a)), seq: 'oops' } as unknown
    const parsed = parseAccount(raw) as PaperAccount
    const next = buy(parsed, 0.01, 100).account
    const ids = [...next.orders.map((o) => o.id), ...next.fills.map((f) => f.id)]
    expect(new Set(ids).size).toBe(ids.length)
  })
})
