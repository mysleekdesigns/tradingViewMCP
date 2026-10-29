import type { PaperOrder } from '../state/paper'

/** The status line under the order ticket, after an order from the ticket or a position's Close. */
export type Feedback = { kind: 'ok' | 'error'; text: string } | null

const QTY = new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 })
export const fmtQty = (q: number) => QTY.format(q)

/** What the ticket says about a just-placed order: its fill, its resting limit, or why it failed. */
export function orderFeedback(
  order: PaperOrder,
  base: string,
  fmtPrice: (symbol: string, price: number) => string,
): Feedback {
  if (order.status === 'rejected') return { kind: 'error', text: order.reason ?? 'Order rejected' }
  if (order.status === 'filled' && order.fillPrice !== null) {
    const verb = order.side === 'buy' ? 'Bought' : 'Sold'
    return {
      kind: 'ok',
      text: `${verb} ${fmtQty(order.qty)} ${base} at ${fmtPrice(order.symbol, order.fillPrice)}`,
    }
  }
  return {
    kind: 'ok',
    text: `Limit ${order.side} ${fmtQty(order.qty)} ${base} at ${fmtPrice(order.symbol, order.limitPrice ?? 0)} placed. Fills on later prices only.`,
  }
}
