import { describe, expect, it } from 'vitest'
import type { PaperOrder } from '../state/paper'
import { orderFeedback } from './paperFeedback'

function order(over: Partial<PaperOrder>): PaperOrder {
  return {
    id: 'o1',
    symbol: 'BTCUSDT',
    side: 'buy',
    type: 'market',
    qty: 0.05,
    limitPrice: null,
    status: 'filled',
    fillPrice: 79_325.88,
    fee: 3.97,
    reason: null,
    createdAt: 1_700_000_000,
    closedAt: 1_700_000_000,
    ...over,
  }
}

const fmt = (_symbol: string, p: number) => p.toFixed(2)

describe('orderFeedback', () => {
  it('reports a filled buy', () => {
    expect(orderFeedback(order({}), 'BTC', fmt)).toEqual({
      kind: 'ok',
      text: 'Bought 0.05 BTC at 79325.88',
    })
  })

  it('reports a filled sell, such as a position Close', () => {
    expect(orderFeedback(order({ side: 'sell', qty: 0.06 }), 'BTC', fmt)?.text).toBe(
      'Sold 0.06 BTC at 79325.88',
    )
  })

  it('reports a resting limit', () => {
    const resting = order({
      side: 'sell',
      type: 'limit',
      qty: 0.02,
      limitPrice: 85_000,
      status: 'open',
      fillPrice: null,
      closedAt: null,
    })
    expect(orderFeedback(resting, 'BTC', fmt)?.text).toBe(
      'Limit sell 0.02 BTC at 85000.00 placed. Fills on later prices only.',
    )
  })

  it('reports a rejection with its reason', () => {
    const rejected = order({ status: 'rejected', fillPrice: null, reason: 'Live feed offline' })
    expect(orderFeedback(rejected, 'BTC', fmt)).toEqual({
      kind: 'error',
      text: 'Live feed offline',
    })
  })
})
