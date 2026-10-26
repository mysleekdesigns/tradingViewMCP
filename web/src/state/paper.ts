// Manual paper trading: a pure, deterministic, in-browser simulator. Spot, long-only, no
// leverage. Nothing here talks to an exchange or a server; every function takes an account and
// returns a new one (or the same object when nothing changed). Times are UNIX seconds.
//
// No look-ahead: a resting limit order only fills on prices strictly after the moment it was
// placed. `onPrice` needs a tick time > createdAt; `onBar` needs a bar that opened after
// createdAt, so the bar that was already forming when the order went in never fills it.
//
// Limits are judged at placement. A marketable limit (a buy at or above the market price, a sell
// at or below it) takes liquidity at once, at the market price moved by the slippage but never
// past the limit. Any other limit rests and later fills at exactly its limit price, even when
// the price gaps through it: a resting order never fills better than its limit.

import type { Candle } from '../types'

export const PAPER_STORAGE_KEY = 'tv.paper.v1'

/** Fills kept in history (oldest dropped first). */
export const MAX_PAPER_FILLS = 500
/** Filled, cancelled and rejected orders kept in history; open orders are always kept. */
export const MAX_CLOSED_ORDERS = 200
/** Decimals the ticket's % buttons round quantities down to. */
export const QTY_DECIMALS = 8

/** Relative slack for float noise in "cost <= available" checks (100% buttons). */
const TOLERANCE = 1e-9

export type PaperScope = 'live' | 'replay'
export type PaperSide = 'buy' | 'sell'
export type PaperOrderType = 'market' | 'limit'
export type PaperOrderStatus = 'open' | 'filled' | 'cancelled' | 'rejected'

export interface PaperConfig {
  /** Pretend starting balance in the quote currency. */
  startingBalance: number
  /** Taker fee as a fraction of notional (0.001 = 0.1%), charged in the quote currency. */
  feeRate: number
  /** Adverse slippage for market orders, in basis points. */
  slippageBps: number
  /** Display label for the quote currency. */
  quoteAsset: string
}

export const DEFAULT_PAPER_CONFIG: Readonly<PaperConfig> = {
  startingBalance: 10_000,
  feeRate: 0.001,
  slippageBps: 0,
  quoteAsset: 'USDT',
}

export interface PaperOrder {
  id: string
  symbol: string
  side: PaperSide
  type: PaperOrderType
  qty: number
  /** Limit price; null for market orders. */
  limitPrice: number | null
  status: PaperOrderStatus
  /** Market time the order was placed (UNIX seconds). */
  createdAt: number
  /** When it filled, was cancelled or was rejected; null while open. */
  closedAt: number | null
  fillPrice: number | null
  fee: number
  /** Why it was rejected (or cancelled on load); null otherwise. */
  reason: string | null
}

export interface PaperPosition {
  symbol: string
  qty: number
  /** Average cost per unit, buy fees included. 0 when flat. */
  avgPrice: number
  /** Realized P&L from sells on this symbol, all fees included. */
  realizedPnl: number
  /** Last fill price: the mark used when no live mark is supplied. */
  lastPrice: number
}

export interface PaperFill {
  id: string
  orderId: string
  symbol: string
  side: PaperSide
  qty: number
  price: number
  fee: number
  time: number
  /** Realized P&L of this fill (sells only; 0 for buys). */
  realizedPnl: number
}

export interface PaperAccount {
  version: 1
  config: PaperConfig
  /** Total cash, including what open buy limits reserve. */
  cash: number
  positions: Readonly<Record<string, PaperPosition>>
  /** Oldest first. */
  orders: readonly PaperOrder[]
  /** Oldest first. */
  fills: readonly PaperFill[]
  /** Counter behind the default ids. */
  seq: number
}

export interface PaperOrderRequest {
  symbol: string
  side: PaperSide
  type: PaperOrderType
  qty: number
  /** Required for limit orders; ignored for market orders. */
  limitPrice?: number | null
  /**
   * The symbol's quote asset when known (SymbolInfo.quoteAsset); else it is parsed from the
   * symbol ('BTCUSDT' -> USDT, 'BTC-USD' -> USD). Must match the account's quote asset.
   */
  quoteAsset?: string | null
}

/**
 * The market at the moment an order is placed: last price and its time (UNIX seconds). Both
 * market and limit orders need the price (a limit is checked against it for marketability).
 */
export interface PaperMarket {
  price: number
  time: number
  /**
   * Trading is paused at this moment (e.g. reviewing earlier replay bars): every order is
   * rejected with this reason.
   */
  blocked?: string | null
  /**
   * Orders that would fill now are paused (e.g. the live feed is offline, so the price may be
   * stale): market orders and marketable limits are rejected with this reason. Resting limits
   * may still be placed.
   */
  marketBlocked?: string | null
}

export type PaperBar = Pick<Candle, 'time' | 'open' | 'high' | 'low' | 'close'>

export interface PaperOptions {
  /** Id generator (tests inject one); defaults to "o<n>" / "f<n>" from `account.seq`. */
  newId?(kind: 'order' | 'fill'): string
}

export interface PlaceResult {
  account: PaperAccount
  order: PaperOrder
}

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)
const isPos = (x: unknown): x is number => isNum(x) && x > 0

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(Math.max(x, lo), hi)
}

/** A fresh account. Invalid config values fall back to the defaults. */
export function createAccount(cfg: Partial<PaperConfig> = {}): PaperAccount {
  const d = DEFAULT_PAPER_CONFIG
  const quote = typeof cfg.quoteAsset === 'string' ? cfg.quoteAsset.trim() : ''
  const config: PaperConfig = {
    startingBalance:
      isPos(cfg.startingBalance) && cfg.startingBalance <= 1e12
        ? cfg.startingBalance
        : d.startingBalance,
    feeRate: isNum(cfg.feeRate) ? clamp(cfg.feeRate, 0, 0.1) : d.feeRate,
    slippageBps: isNum(cfg.slippageBps) ? clamp(cfg.slippageBps, 0, 1000) : d.slippageBps,
    quoteAsset: quote.length > 0 && quote.length <= 12 ? quote : d.quoteAsset,
  }
  return {
    version: 1,
    config,
    cash: config.startingBalance,
    positions: {},
    orders: [],
    fills: [],
    seq: 1,
  }
}

// ---------- Quote currency ----------

/** Quote assets recognised at the end of a symbol without a separator, longest first. */
const KNOWN_QUOTES: readonly string[] = [
  'FDUSD',
  'USDT',
  'USDC',
  'TUSD',
  'BUSD',
  'USDP',
  'AEUR',
  'USD',
  'DAI',
  'EUR',
  'GBP',
  'TRY',
  'BRL',
  'JPY',
  'AUD',
  'BTC',
  'ETH',
  'BNB',
]

/**
 * The quote asset of a symbol: after the separator of 'BTC-USD' / 'BTC/USD', else a known quote
 * at the end of 'BTCUSDT'. Null when it cannot be told.
 */
export function symbolQuoteAsset(symbol: string): string | null {
  const s = symbol.trim().toUpperCase()
  const sep = Math.max(s.lastIndexOf('-'), s.lastIndexOf('/'))
  if (sep > 0) return sep < s.length - 1 ? s.slice(sep + 1) : null
  for (const q of KNOWN_QUOTES) if (s.length > q.length && s.endsWith(q)) return q
  return null
}

/** The base asset of a symbol given its quote ('BTC' for BTCUSDT or BTC-USD); else the symbol. */
export function symbolBaseAsset(symbol: string, quote: string | null): string {
  const s = symbol.trim().toUpperCase()
  const q = quote?.trim().toUpperCase() ?? ''
  if (q === '' || s.length <= q.length || !s.endsWith(q)) return symbol
  return s.slice(0, -q.length).replace(/[-/]$/, '')
}

/**
 * Why `account` cannot trade `symbol` (its quote asset is not the account's), or null when it
 * can. `quoteAsset` is the symbol's known quote asset; without it the symbol is parsed.
 */
export function quoteMismatchReason(
  account: PaperAccount,
  symbol: string,
  quoteAsset?: string | null,
): string | null {
  const known = typeof quoteAsset === 'string' ? quoteAsset.trim().toUpperCase() : ''
  const quote = known !== '' ? known : symbolQuoteAsset(symbol)
  const own = account.config.quoteAsset.trim().toUpperCase()
  const sym = symbol.trim().toUpperCase()
  if (quote === null) {
    return `Cannot tell which currency ${sym} is quoted in; this paper account is in ${own}.`
  }
  if (quote !== own) {
    return `${sym} is quoted in ${quote}, but this paper account is in ${own}. Reset the account in ${quote} to trade it.`
  }
  return null
}

// ---------- Reserves and availability ----------

/** Cash an open buy limit holds back: qty * limit plus the fee at the limit price. */
function buyReserve(order: PaperOrder, feeRate: number): number {
  return order.qty * (order.limitPrice ?? 0) * (1 + feeRate)
}

export function openOrders(account: PaperAccount, symbol?: string): PaperOrder[] {
  return account.orders.filter(
    (o) => o.status === 'open' && (symbol === undefined || o.symbol === symbol),
  )
}

/** Cash reserved by open buy limits. */
export function reservedCash(account: PaperAccount): number {
  let sum = 0
  for (const o of account.orders) {
    if (o.status === 'open' && o.side === 'buy') sum += buyReserve(o, account.config.feeRate)
  }
  return sum
}

/** Cash free for new buys. */
export function availableCash(account: PaperAccount): number {
  return Math.max(0, account.cash - reservedCash(account))
}

/** Quantity of `symbol` reserved by open sell limits. */
export function reservedQty(account: PaperAccount, symbol: string): number {
  let sum = 0
  for (const o of account.orders) {
    if (o.status === 'open' && o.side === 'sell' && o.symbol === symbol) sum += o.qty
  }
  return sum
}

/** Quantity of `symbol` free for new sells. */
export function availableQty(account: PaperAccount, symbol: string): number {
  const held = account.positions[symbol]?.qty ?? 0
  return Math.max(0, held - reservedQty(account, symbol))
}

const exceeds = (need: number, available: number) => need > available * (1 + TOLERANCE) + 1e-12

// ---------- Orders ----------

function nextId(
  account: PaperAccount,
  kind: 'order' | 'fill',
  opts: PaperOptions | undefined,
): [string, PaperAccount] {
  const id = opts?.newId ? opts.newId(kind) : `${kind === 'order' ? 'o' : 'f'}${account.seq}`
  return [id, { ...account, seq: account.seq + 1 }]
}

/** Keep every open order and the newest MAX_CLOSED_ORDERS closed ones, in order. */
function pruneOrders(orders: readonly PaperOrder[]): readonly PaperOrder[] {
  let closed = 0
  for (const o of orders) if (o.status !== 'open') closed++
  if (closed <= MAX_CLOSED_ORDERS) return orders
  let drop = closed - MAX_CLOSED_ORDERS
  return orders.filter((o) => {
    if (o.status === 'open' || drop === 0) return true
    drop--
    return false
  })
}

function withOrder(account: PaperAccount, order: PaperOrder): PaperAccount {
  const i = account.orders.findIndex((o) => o.id === order.id)
  const orders =
    i === -1 ? [...account.orders, order] : account.orders.map((o, j) => (j === i ? order : o))
  return { ...account, orders: pruneOrders(orders) }
}

/**
 * Execute `order` in full at `price`. Buys add to the position at a fee-inclusive average cost;
 * sells realize (price - avgPrice) * qty - fee. Callers check cash/qty beforehand.
 */
function fillOrder(
  account: PaperAccount,
  order: PaperOrder,
  price: number,
  time: number,
  opts: PaperOptions | undefined,
): PlaceResult {
  const { feeRate } = account.config
  const prev = account.positions[order.symbol]
  const q0 = prev?.qty ?? 0
  const avg0 = prev?.avgPrice ?? 0
  const realized0 = prev?.realizedPnl ?? 0

  let qty = order.qty
  let cash = account.cash
  let position: PaperPosition
  let realized = 0
  let fee: number

  if (order.side === 'buy') {
    const cost = qty * price
    fee = cost * feeRate
    cash -= cost + fee
    const q = q0 + qty
    position = {
      symbol: order.symbol,
      qty: q,
      avgPrice: (q0 * avg0 + cost + fee) / q,
      realizedPnl: realized0,
      lastPrice: price,
    }
  } else {
    qty = Math.min(qty, q0) // within TOLERANCE of the holding: never sell more than held
    const proceeds = qty * price
    fee = proceeds * feeRate
    cash += proceeds - fee
    realized = qty * (price - avg0) - fee
    let q = q0 - qty
    if (q <= q0 * TOLERANCE) q = 0
    position = {
      symbol: order.symbol,
      qty: q,
      avgPrice: q === 0 ? 0 : avg0,
      realizedPnl: realized0 + realized,
      lastPrice: price,
    }
  }
  // Float noise from a 100% buy must not leave cash a hair below zero.
  if (cash < 0 && cash > -1e-6) cash = 0

  const [fillId, withId] = nextId(account, 'fill', opts)
  const filled: PaperOrder = {
    ...order,
    status: 'filled',
    closedAt: time,
    fillPrice: price,
    fee,
  }
  const fill: PaperFill = {
    id: fillId,
    orderId: order.id,
    symbol: order.symbol,
    side: order.side,
    qty,
    price,
    fee,
    time,
    realizedPnl: realized,
  }
  const next = withOrder(
    {
      ...withId,
      cash,
      positions: { ...withId.positions, [order.symbol]: position },
      fills: [...withId.fills, fill].slice(-MAX_PAPER_FILLS),
    },
    filled,
  )
  return { account: next, order: filled }
}

/**
 * Place an order at market time `market.time`. Market orders fill at once at `market.price`
 * moved against the trader by the slippage. A marketable limit fills at once the same way, but
 * never past its limit; any other limit rests as 'open' with its cash (buy) or quantity (sell)
 * reserved. Invalid, unaffordable or paused requests, and symbols quoted in another currency
 * than the account's, are recorded as 'rejected' with a reason. No shorts: a sell can never
 * exceed the free quantity held.
 */
export function placeOrder(
  account: PaperAccount,
  req: PaperOrderRequest,
  market: PaperMarket,
  opts?: PaperOptions,
): PlaceResult {
  const { feeRate, slippageBps } = account.config
  const symbol = typeof req.symbol === 'string' ? req.symbol.trim().toUpperCase() : ''
  const isLimit = req.type === 'limit'
  const time = isNum(market.time) ? market.time : 0
  const [id, next] = nextId(account, 'order', opts)
  const order: PaperOrder = {
    id,
    symbol,
    side: req.side === 'sell' ? 'sell' : 'buy',
    type: isLimit ? 'limit' : 'market',
    qty: isNum(req.qty) ? req.qty : 0,
    limitPrice: isLimit && isNum(req.limitPrice) ? req.limitPrice : null,
    status: 'open',
    createdAt: time,
    closedAt: null,
    fillPrice: null,
    fee: 0,
    reason: null,
  }
  const reject = (reason: string): PlaceResult => {
    const rejected: PaperOrder = { ...order, status: 'rejected', closedAt: time, reason }
    return { account: withOrder(next, rejected), order: rejected }
  }

  if (symbol === '') return reject('No symbol')
  if (typeof market.blocked === 'string' && market.blocked !== '') return reject(market.blocked)
  const mismatch = quoteMismatchReason(account, symbol, req.quoteAsset)
  if (mismatch !== null) return reject(mismatch)
  if (req.side !== 'buy' && req.side !== 'sell') return reject('Unknown side')
  if (req.type !== 'market' && req.type !== 'limit') return reject('Unknown order type')
  if (!isPos(req.qty)) return reject('Quantity must be greater than 0')
  if (!isNum(market.time)) return reject('No market time')
  if (isLimit && !isPos(req.limitPrice)) return reject('Limit price must be greater than 0')
  if (!isPos(market.price)) return reject('No market price')

  const quote = account.config.quoteAsset
  const slip = slippageBps / 10_000
  const limit = order.limitPrice
  const marketable =
    limit === null || (order.side === 'buy' ? limit >= market.price : limit <= market.price)

  if (!marketable) {
    // Resting limit: reserve now, fill at exactly the limit later.
    if (order.side === 'buy') {
      const need = buyReserve(order, feeRate)
      const avail = availableCash(next)
      if (exceeds(need, avail)) {
        return reject(
          `Insufficient ${quote}: needs ${need.toFixed(2)}, available ${avail.toFixed(2)}`,
        )
      }
    } else {
      const avail = availableQty(next, symbol)
      if (exceeds(order.qty, avail)) {
        return reject(`Insufficient ${symbol}: selling ${order.qty}, available ${avail}`)
      }
    }
    return { account: withOrder(next, order), order }
  }

  // Fills now, as a taker: market orders, and limits at or through the market price.
  if (typeof market.marketBlocked === 'string' && market.marketBlocked !== '') {
    return reject(market.marketBlocked)
  }
  if (order.side === 'buy') {
    const taker = market.price * (1 + slip)
    const price = limit === null ? taker : Math.min(limit, taker)
    const need = order.qty * price * (1 + feeRate)
    const avail = availableCash(next)
    if (exceeds(need, avail)) {
      return reject(
        `Insufficient ${quote}: needs ${need.toFixed(2)}, available ${avail.toFixed(2)}`,
      )
    }
    return fillOrder(next, order, price, time, opts)
  }
  const avail = availableQty(next, symbol)
  if (exceeds(order.qty, avail)) {
    return reject(`Insufficient ${symbol}: selling ${order.qty}, available ${avail}`)
  }
  const taker = market.price * (1 - slip)
  return fillOrder(next, order, limit === null ? taker : Math.max(limit, taker), time, opts)
}

/** Cancel an open order; its reserve is released. Returns `account` unchanged otherwise. */
export function cancelOrder(account: PaperAccount, orderId: string, time: number): PaperAccount {
  const order = account.orders.find((o) => o.id === orderId)
  if (!order || order.status !== 'open') return account
  return withOrder(account, {
    ...order,
    status: 'cancelled',
    closedAt: isNum(time) ? time : order.createdAt,
  })
}

/**
 * Fill every open limit on `symbol` whose condition the price range [low, high] meets, in
 * placement order, skipping orders not placed strictly before `time`. A resting limit fills at
 * exactly its limit price, also when the range is entirely through it (a gap): it was not
 * marketable when placed, so it is never filled better than the limit.
 */
function sweep(
  account: PaperAccount,
  symbol: string,
  time: number,
  low: number,
  high: number,
  opts: PaperOptions | undefined,
): PaperAccount {
  let next = account
  for (const o of account.orders) {
    if (o.status !== 'open' || o.symbol !== symbol || o.limitPrice === null) continue
    if (!(time > o.createdAt)) continue
    const limit = o.limitPrice
    if ((o.side === 'buy' && low <= limit) || (o.side === 'sell' && high >= limit)) {
      next = fillOrder(next, o, limit, time, opts).account
    }
  }
  return next
}

/**
 * A live tick. Open limits placed strictly before `time` fill at their limit price when the
 * price reaches them: a buy when price <= limit, a sell when price >= limit.
 */
export function onPrice(
  account: PaperAccount,
  symbol: string,
  price: number,
  time: number,
  opts?: PaperOptions,
): PaperAccount {
  if (!isPos(price) || !isNum(time)) return account
  return sweep(account, symbol, time, price, price, opts)
}

/**
 * A closed bar (replay step). Only bars that opened strictly after an order was placed can fill
 * it: `bar.time > order.createdAt`. A buy fills if bar.low <= limit, a sell if
 * bar.high >= limit, both at exactly the limit price (a gap through it included).
 */
export function onBar(
  account: PaperAccount,
  symbol: string,
  bar: PaperBar,
  opts?: PaperOptions,
): PaperAccount {
  if (!isNum(bar.time) || !isPos(bar.open) || !isPos(bar.low) || !isPos(bar.high)) return account
  return sweep(account, symbol, bar.time, bar.low, bar.high, opts)
}

// ---------- Derived values ----------

export type PaperMarks = Readonly<Record<string, number>>

function markFor(position: PaperPosition, marks: PaperMarks | undefined): number {
  const m = marks?.[position.symbol]
  return isPos(m) ? m : position.lastPrice
}

/** Positions with a non-zero quantity, in symbol order. */
export function openPositions(account: PaperAccount): PaperPosition[] {
  return Object.values(account.positions)
    .filter((p) => p.qty > 0)
    .sort((a, b) => a.symbol.localeCompare(b.symbol))
}

/** Cash plus every position at its mark (the last fill price when no mark is given). */
export function equity(account: PaperAccount, marks?: PaperMarks): number {
  let sum = account.cash
  for (const p of Object.values(account.positions)) {
    if (p.qty > 0) sum += p.qty * markFor(p, marks)
  }
  return sum
}

export interface PositionPnl {
  mark: number
  marketValue: number
  /** qty * (mark - avgPrice); buy fees are in avgPrice, so they count here until sold. */
  unrealized: number
  /** Unrealized as a fraction of cost (0.05 = +5%). */
  unrealizedPct: number
  realized: number
}

export function positionPnl(position: PaperPosition, mark?: number | null): PositionPnl {
  const m = isPos(mark) ? mark : position.lastPrice
  const cost = position.qty * position.avgPrice
  const unrealized = position.qty * (m - position.avgPrice)
  return {
    mark: m,
    marketValue: position.qty * m,
    unrealized,
    unrealizedPct: cost > 0 ? unrealized / cost : 0,
    realized: position.realizedPnl,
  }
}

export interface AccountSummary {
  equity: number
  cash: number
  availableCash: number
  reservedCash: number
  realizedPnl: number
  unrealizedPnl: number
  /** equity - startingBalance (= realized + unrealized). */
  totalPnl: number
  /** totalPnl / startingBalance (0.05 = +5%). */
  returnPct: number
}

export function accountSummary(account: PaperAccount, marks?: PaperMarks): AccountSummary {
  let realized = 0
  let unrealized = 0
  for (const p of Object.values(account.positions)) {
    realized += p.realizedPnl
    if (p.qty > 0) unrealized += positionPnl(p, markFor(p, marks)).unrealized
  }
  const eq = equity(account, marks)
  const start = account.config.startingBalance
  return {
    equity: eq,
    cash: account.cash,
    availableCash: availableCash(account),
    reservedCash: reservedCash(account),
    realizedPnl: realized,
    unrealizedPnl: unrealized,
    totalPnl: eq - start,
    returnPct: start > 0 ? eq / start - 1 : 0,
  }
}

/** Round a quantity down to `decimals` places, so a "100%" order never overshoots. */
export function roundQtyDown(qty: number, decimals: number = QTY_DECIMALS): number {
  if (!isPos(qty)) return 0
  const f = 10 ** decimals
  return Math.floor(qty * f * (1 + 1e-12)) / f
}

/** True when a limit at `limitPrice` would fill at once against `marketPrice` (see placeOrder). */
export function isMarketableLimit(
  side: PaperSide,
  limitPrice: number | null,
  marketPrice: number | null,
): boolean {
  if (!isPos(limitPrice) || !isPos(marketPrice)) return false
  return side === 'buy' ? limitPrice >= marketPrice : limitPrice <= marketPrice
}

/**
 * The price an order is expected to execute at: the market moved by slippage, capped at the
 * limit for a marketable limit; the limit price for a resting limit.
 */
export function executionPrice(
  account: PaperAccount,
  side: PaperSide,
  type: PaperOrderType,
  marketPrice: number | null,
  limitPrice: number | null,
): number | null {
  if (type === 'limit' && !isPos(limitPrice)) return null
  if (type === 'limit' && !isMarketableLimit(side, limitPrice, marketPrice)) return limitPrice
  if (!isPos(marketPrice)) return null
  const slip = account.config.slippageBps / 10_000
  const taker = marketPrice * (side === 'buy' ? 1 + slip : 1 - slip)
  if (type === 'market' || limitPrice === null) return taker
  return side === 'buy' ? Math.min(limitPrice, taker) : Math.max(limitPrice, taker)
}

/** Largest quantity the account can buy at `price` (fee included) or sell of `symbol`. */
export function maxOrderQty(
  account: PaperAccount,
  symbol: string,
  side: PaperSide,
  price: number | null,
): number {
  if (side === 'sell') return availableQty(account, symbol)
  if (!isPos(price)) return 0
  return availableCash(account) / (price * (1 + account.config.feeRate))
}

export interface OrderEstimate {
  price: number
  notional: number
  fee: number
  /** Cash out for a buy (notional + fee), cash in for a sell (notional - fee). */
  total: number
}

export function estimateOrder(
  account: PaperAccount,
  side: PaperSide,
  qty: number,
  price: number | null,
): OrderEstimate | null {
  if (!isPos(qty) || !isPos(price)) return null
  const notional = qty * price
  const fee = notional * account.config.feeRate
  return { price, notional, fee, total: side === 'buy' ? notional + fee : notional - fee }
}

// ---------- Persistence ----------

export type PaperStorage = Pick<Storage, 'getItem' | 'setItem'>

export function defaultPaperStorage(): PaperStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

const isObj = (x: unknown): x is Record<string, unknown> =>
  typeof x === 'object' && x !== null && !Array.isArray(x)
const isStr = (x: unknown): x is string => typeof x === 'string' && x.length > 0 && x.length <= 64

function parseOrder(x: unknown): PaperOrder | null {
  if (!isObj(x)) return null
  const { id, symbol, side, type, qty, limitPrice, status, createdAt, closedAt, fillPrice, fee } = x
  if (!isStr(id) || !isStr(symbol)) return null
  if (side !== 'buy' && side !== 'sell') return null
  if (type !== 'market' && type !== 'limit') return null
  if (status !== 'open' && status !== 'filled' && status !== 'cancelled' && status !== 'rejected')
    return null
  if (!isNum(qty) || qty < 0 || !isNum(createdAt)) return null
  if (type === 'limit' && status === 'open' && !isPos(limitPrice)) return null
  if (status === 'open' && !isPos(qty)) return null
  return {
    id,
    symbol,
    side,
    type,
    qty,
    limitPrice: type === 'limit' && isPos(limitPrice) ? limitPrice : null,
    status: type === 'market' && status === 'open' ? 'cancelled' : status,
    createdAt,
    closedAt: isNum(closedAt) ? closedAt : status === 'open' ? null : createdAt,
    fillPrice: isPos(fillPrice) ? fillPrice : null,
    fee: isNum(fee) && fee >= 0 ? fee : 0,
    reason: typeof x.reason === 'string' ? x.reason.slice(0, 200) : null,
  }
}

function parseFill(x: unknown): PaperFill | null {
  if (!isObj(x)) return null
  const { id, orderId, symbol, side, qty, price, fee, time, realizedPnl } = x
  if (!isStr(id) || !isStr(orderId) || !isStr(symbol)) return null
  if (side !== 'buy' && side !== 'sell') return null
  if (!isPos(qty) || !isPos(price) || !isNum(time)) return null
  return {
    id,
    orderId,
    symbol,
    side,
    qty,
    price,
    fee: isNum(fee) && fee >= 0 ? fee : 0,
    time,
    realizedPnl: isNum(realizedPnl) ? realizedPnl : 0,
  }
}

function parsePosition(symbol: string, x: unknown): PaperPosition | null {
  if (!isObj(x) || !isStr(symbol)) return null
  const { qty, avgPrice, realizedPnl, lastPrice } = x
  if (!isNum(qty) || qty < 0 || !isNum(avgPrice) || avgPrice < 0) return null
  if (qty > 0 && !(avgPrice > 0)) return null
  return {
    symbol,
    qty,
    avgPrice: qty > 0 ? avgPrice : 0,
    realizedPnl: isNum(realizedPnl) ? realizedPnl : 0,
    lastPrice: isPos(lastPrice) ? lastPrice : avgPrice,
  }
}

/**
 * A stored account, or null when it is not one (wrong version, unusable cash). Bad orders,
 * fills and positions are dropped one by one. Open orders whose reserve the account can no
 * longer cover are cancelled, so the reserve invariants hold after any edit to storage.
 */
export function parseAccount(raw: unknown): PaperAccount | null {
  if (!isObj(raw) || raw.version !== 1) return null
  if (!isNum(raw.cash) || raw.cash < 0) return null
  const base = createAccount(isObj(raw.config) ? (raw.config as Partial<PaperConfig>) : {})

  const positions: Record<string, PaperPosition> = {}
  if (isObj(raw.positions)) {
    for (const [sym, p] of Object.entries(raw.positions)) {
      const pos = parsePosition(sym, p)
      if (pos) positions[sym] = pos
    }
  }
  const orders = (Array.isArray(raw.orders) ? raw.orders : [])
    .map(parseOrder)
    .filter((o): o is PaperOrder => o !== null)
  const fills = (Array.isArray(raw.fills) ? raw.fills : [])
    .map(parseFill)
    .filter((f): f is PaperFill => f !== null)
    .slice(-MAX_PAPER_FILLS)

  // Next id must not reuse one already in history.
  let seq = Number.isSafeInteger(raw.seq) && (raw.seq as number) > 0 ? (raw.seq as number) : 1
  for (const item of [...orders, ...fills]) {
    const m = /^[of](\d+)$/.exec(item.id)
    if (m) seq = Math.max(seq, Number(m[1]) + 1)
  }

  let cashLeft = raw.cash
  const qtyLeft = new Map<string, number>()
  const checked = orders.map((o): PaperOrder => {
    if (o.status !== 'open') return o
    if (o.side === 'buy') {
      const need = buyReserve(o, base.config.feeRate)
      if (!exceeds(need, cashLeft)) {
        cashLeft -= need
        return o
      }
    } else {
      const left = qtyLeft.get(o.symbol) ?? positions[o.symbol]?.qty ?? 0
      if (!exceeds(o.qty, left)) {
        qtyLeft.set(o.symbol, left - o.qty)
        return o
      }
    }
    return {
      ...o,
      status: 'cancelled',
      closedAt: o.createdAt,
      reason: 'Cancelled on load: reserve no longer available',
    }
  })

  return {
    ...base,
    cash: raw.cash,
    positions,
    orders: pruneOrders(checked),
    fills,
    seq,
  }
}

/** The stored live account, or a fresh default one when missing, corrupt or unreadable. */
export function loadPaperAccount(
  storage: PaperStorage | null = defaultPaperStorage(),
  key: string = PAPER_STORAGE_KEY,
): PaperAccount {
  if (!storage) return createAccount()
  try {
    const text = storage.getItem(key)
    if (text === null) return createAccount()
    return parseAccount(JSON.parse(text) as unknown) ?? createAccount()
  } catch {
    return createAccount()
  }
}

export function savePaperAccount(
  account: PaperAccount,
  storage: PaperStorage | null = defaultPaperStorage(),
  key: string = PAPER_STORAGE_KEY,
): void {
  if (!storage) return
  try {
    storage.setItem(key, JSON.stringify(account))
  } catch {
    // Quota exceeded or storage disabled: keep it in memory only.
  }
}
