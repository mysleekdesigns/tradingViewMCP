// Price feeds for the paper accounts: live ticks of the charts on screen, and the bars a bar
// replay reveals. Pure; App applies the steps to usePaperAccount.
import type { Candle, ConnectionStatus } from '../types'
import {
  onBar,
  placeOrder,
  type PaperAccount,
  type PaperBar,
  type PaperMarket,
  type PaperOptions,
  type PaperOrderRequest,
  type PlaceResult,
} from './paper'
import type { ReplayState } from './replay'

/** Ticket and engine message while the replay cursor is behind the latest replayed bar. */
export const REPLAY_REVIEW_MESSAGE =
  'Reviewing earlier bars. Step forward to the latest replayed bar to trade.'

/** Ticket and engine message while a new start bar is being picked from a running replay. */
export const REPLAY_PICKING_MESSAGE = 'Replay paused while picking a start bar.'

/**
 * Why the replay account cannot trade in replay status `status`: while picking a start bar the
 * chart still shows the replay but the cursor is about to move, so trading is paused (the
 * account stays on screen; nothing is fed either, see replayFeedStep).
 */
export function replayPaperPause(status: ReplayState['status']): string | null {
  return status === 'picking' ? REPLAY_PICKING_MESSAGE : null
}

/** Ticket and engine message for market orders while the active chart's live feed is not live. */
export const LIVE_OFFLINE_MESSAGE = 'Live feed offline: market orders are paused'

/** What the replay feed remembers between renders. */
export interface ReplayMark {
  status: ReplayState['status']
  /** Kept while picking (the replay a cancelled re-pick resumes); null when off. */
  cursorTime: number | null
}

export const REPLAY_MARK_OFF: ReplayMark = { status: 'off', cursorTime: null }

export type ReplayFeedStep =
  | { kind: 'none' }
  /**
   * A replay started (or restarted somewhere else), or ended: start a fresh replay account
   * whose high-water mark is `at` (the start bar, already seen; null when replay ended).
   */
  | { kind: 'reset'; at: number | null }
  /** Bars revealed since the last step, oldest first: each goes to `onBar` in this order. */
  | { kind: 'bars'; bars: readonly Candle[] }

const NONE: ReplayFeedStep = { kind: 'none' }

/**
 * The paper step for a replay change from `prev` to `next`. `visible` is the replay's visible
 * bars for `next` (closed bars with time <= next.cursorTime).
 *
 * - Stepping or playing forward reveals bars (prev.cursorTime, next.cursorTime]: all of them are
 *   fed, so a 10-bar skip fills limits exactly as ten single steps would. Bars already fed once
 *   (after a step back and forward again) are dropped by the account's high-water mark
 *   (`feedReplayBar`), never fed twice.
 * - Stepping back reveals nothing, and trading is refused until the cursor is back at the
 *   high-water mark (`placeReplayOrder`).
 * - A start (from off, or a pick that lands on another bar) resets the replay account; cancelling
 *   a re-pick resumes the same replay. Leaving replay resets it as well.
 */
export function replayFeedStep(
  prev: ReplayMark,
  next: ReplayMark,
  visible: readonly Candle[] | null,
): ReplayFeedStep {
  if (next.status !== 'active' || next.cursorTime === null) {
    return next.status === 'off' && prev.status !== 'off' ? { kind: 'reset', at: null } : NONE
  }
  if (prev.status !== 'active' || prev.cursorTime === null) {
    const resumed = prev.status === 'picking' && prev.cursorTime === next.cursorTime
    return resumed ? NONE : { kind: 'reset', at: next.cursorTime }
  }
  if (next.cursorTime <= prev.cursorTime || visible === null) return NONE
  const from = prev.cursorTime
  const bars: Candle[] = []
  // Newest first from the end: a step reveals a handful of bars at the tail.
  for (let i = visible.length - 1; i >= 0 && visible[i].time > from; i--) bars.push(visible[i])
  bars.reverse()
  return bars.length > 0 ? { kind: 'bars', bars } : NONE
}

/**
 * The replay paper account and its high-water mark: the open time of the latest bar it has seen
 * (the start bar, then every bar fed). Bars at or before it are never fed again, and no order
 * can be placed at an earlier market time, so stepping back cannot trade on bars already seen.
 */
export interface ReplayBook {
  account: PaperAccount
  highWater: number | null
}

/** Why trading is refused at market time `time` (the cursor is behind the high-water mark). */
export function replayTradeBlock(highWater: number | null, time: number | null): string | null {
  return highWater !== null && time !== null && time < highWater ? REPLAY_REVIEW_MESSAGE : null
}

/** Feed one revealed bar; a bar at or before the high-water mark was fed already and is dropped. */
export function feedReplayBar(
  book: ReplayBook,
  symbol: string,
  bar: PaperBar,
  opts?: PaperOptions,
): ReplayBook {
  if (!Number.isFinite(bar.time)) return book
  if (book.highWater !== null && bar.time <= book.highWater) return book
  return { account: onBar(book.account, symbol, bar, opts), highWater: bar.time }
}

/** Place an order on the replay account; refused (recorded as rejected) behind the high-water mark. */
export function placeReplayOrder(
  book: ReplayBook,
  req: PaperOrderRequest,
  market: PaperMarket,
  opts?: PaperOptions,
): { book: ReplayBook; order: PlaceResult['order'] } {
  const blocked = market.blocked ?? replayTradeBlock(book.highWater, market.time)
  const { account, order } = placeOrder(book.account, req, { ...market, blocked }, opts)
  return { book: account === book.account ? book : { ...book, account }, order }
}

/**
 * When one panel's latest price last changed: the last bar's time and close, and the wall-clock
 * UNIX seconds that pair was first seen. History loads and refreshes that leave the last bar as
 * it was keep the stamp, so an unchanged price is never re-fed as a fresh tick.
 */
export interface PriceStamp {
  symbol: string
  barTime: number
  price: number
  /** Wall-clock UNIX seconds the price was first seen. */
  at: number
}

/**
 * The stamp for `candles` (ascending, the last may be forming): `prev` while the last bar's time
 * and close are unchanged (or no candles are loaded), else a new stamp at `nowMs`.
 */
export function priceStamp(
  prev: PriceStamp | null,
  symbol: string,
  candles: readonly Candle[] | null,
  nowMs: number,
): PriceStamp | null {
  const kept = prev !== null && prev.symbol === symbol ? prev : null
  if (candles === null || candles.length === 0) return kept
  const last = candles[candles.length - 1]
  if (kept !== null && kept.barTime === last.time && kept.price === last.close) return kept
  return { symbol, barTime: last.time, price: last.close, at: nowMs / 1000 }
}

/** One chart panel's feed state: its connection status and price stamp. */
export interface PanelFeed {
  panelId: string
  symbol: string
  status: ConnectionStatus
  stamp: PriceStamp | null
}

/** The latest price of one chart panel, as seen by its live stream. */
export interface LiveQuote {
  panelId: string
  symbol: string
  price: number
  /** Wall-clock UNIX seconds the price was first seen (PriceStamp.at). */
  time: number
}

/**
 * Quotes of the panels whose stream is live. A connecting, reconnecting or offline panel shows a
 * price that may be stale: it neither fills limits nor marks positions.
 */
export function liveQuotesOf(panels: readonly PanelFeed[]): LiveQuote[] {
  const out: LiveQuote[] = []
  for (const p of panels) {
    if (p.status !== 'live' || p.stamp === null || p.stamp.symbol !== p.symbol) continue
    out.push({ panelId: p.panelId, symbol: p.symbol, price: p.stamp.price, time: p.stamp.at })
  }
  return out
}

/** Ticks already fed, by panel: "symbol|price|time". */
export type LiveFeedSeen = ReadonlyMap<string, string>

/**
 * The quotes not fed yet (a new price, a new tick time, or a new symbol on a panel), and the
 * updated `seen` map. Panels that are gone drop out of it. Returns the same map when nothing
 * changed.
 */
export function liveFeedUpdates(
  seen: LiveFeedSeen,
  quotes: readonly LiveQuote[],
): { fresh: LiveQuote[]; seen: LiveFeedSeen } {
  const fresh: LiveQuote[] = []
  const next = new Map<string, string>()
  for (const q of quotes) {
    if (!Number.isFinite(q.price) || q.price <= 0 || !Number.isFinite(q.time)) continue
    const key = `${q.symbol}|${q.price}|${q.time}`
    next.set(q.panelId, key)
    if (seen.get(q.panelId) !== key) fresh.push(q)
  }
  const same = next.size === seen.size && [...next].every(([id, key]) => seen.get(id) === key)
  return { fresh, seen: same ? seen : next }
}

/**
 * Marks for positions in other symbols: each symbol's latest price on any live chart on screen
 * (pass `liveQuotesOf`). The symbol the ticket trades is left out (the paper panel adds its own
 * price while its feed is live). Positions without a mark use their last fill price.
 */
export function paperMarks(quotes: readonly LiveQuote[], exclude: string): Record<string, number> {
  const marks: Record<string, number> = {}
  const at: Record<string, number> = {}
  for (const q of quotes) {
    if (q.symbol === exclude || !Number.isFinite(q.price) || q.price <= 0) continue
    if (at[q.symbol] === undefined || q.time > at[q.symbol]) {
      marks[q.symbol] = q.price
      at[q.symbol] = q.time
    }
  }
  return marks
}
