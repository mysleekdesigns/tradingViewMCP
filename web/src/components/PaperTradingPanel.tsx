import { useId, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  DEFAULT_PAPER_CONFIG,
  accountSummary,
  availableCash,
  availableQty,
  estimateOrder,
  executionPrice,
  isMarketableLimit,
  maxOrderQty,
  openOrders,
  openPositions,
  positionPnl,
  quoteMismatchReason,
  roundQtyDown,
  symbolBaseAsset,
  symbolQuoteAsset,
  type PaperAccount,
  type PaperFill,
  type PaperMarks,
  type PaperOrder,
  type PaperOrderType,
  type PaperPosition,
  type PaperScope,
  type PaperSide,
} from '../state/paper'
import { replayTradeBlock } from '../state/paperFeeds'
import type { PaperAccountApi } from '../state/usePaperAccount'
import { formatPriceByTick } from '../state/chartTools'
import { formatUtc } from './quote'
import './Paper.css'

export interface PaperTradingPanelProps {
  /** Symbol on the active chart: the ticket trades it. */
  symbol: string
  /** Its current price (last live tick, or the replay bar's close); null before data arrives. */
  price: number | null
  /**
   * Market time of `price` in UNIX seconds: wall-clock seconds for live ticks, the current
   * replay bar's open time in replay. Orders are stamped with it. Null before data arrives.
   */
  time: number | null
  scope: PaperScope
  /** The account for `scope` (from `usePaperAccount(scope)`, owned by the caller). */
  paper: PaperAccountApi
  /** Latest prices of other symbols (e.g. watchlist tickers) for marking their positions. */
  marks?: PaperMarks
  /** The symbol's quote asset when known (SymbolInfo.quoteAsset); else parsed from the symbol. */
  quoteAsset?: string | null
  /**
   * Set while the active chart's live feed is not live: market orders (and limits that would
   * fill at once) are paused with this message, and `price` is not used as a mark.
   */
  offlineReason?: string | null
  /**
   * Trading is paused with this message (e.g. picking a new replay start bar). The engine
   * rejects orders with it too.
   */
  pausedReason?: string | null
  formatPrice?(symbol: string, price: number): string
  onSelectSymbol?(symbol: string): void
}

type Tab = 'positions' | 'orders' | 'history'
const TABS: readonly Tab[] = ['positions', 'orders', 'history']
const TAB_LABELS: Readonly<Record<Tab, string>> = {
  positions: 'Positions',
  orders: 'Orders',
  history: 'History',
}
const PERCENTS = [0.25, 0.5, 1] as const

const MONEY = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const QTY = new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 })

const money = (n: number) => MONEY.format(Math.abs(n) < 0.005 ? 0 : n)
function signedMoney(n: number): string {
  if (Math.abs(n) < 0.005) return '0.00'
  return `${n > 0 ? '+' : '−'}${MONEY.format(Math.abs(n))}`
}
function signedPct(f: number): string {
  const p = f * 100
  if (Math.abs(p) < 0.005) return '0.00%'
  return `${p > 0 ? '+' : '−'}${Math.abs(p).toFixed(2)}%`
}
const tone = (n: number) => (n > 0.005 ? 'up' : n < -0.005 ? 'down' : 'flat')
const fmtQty = (q: number) => QTY.format(q)

/** Quote assets the reset form always offers (USDT for Binance, USD for Coinbase). */
const QUOTE_CHOICES: readonly string[] = ['USDT', 'USD']

function parseNum(text: string): number | null {
  const t = text.replace(/,/g, '').trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

/**
 * Sidebar panel: a manual paper-trading account. Everything runs in the browser against the
 * prices the chart already shows; no order ever leaves this page.
 */
export function PaperTradingPanel(props: PaperTradingPanelProps) {
  const { symbol, price, time, scope, paper, marks, onSelectSymbol } = props
  const offlineReason = props.offlineReason ?? null
  const { account } = paper
  const quote = account.config.quoteAsset
  const symbolQuote = props.quoteAsset ?? symbolQuoteAsset(symbol)
  // Trading is refused while paused (picking a replay start bar), while reviewing earlier replay
  // bars, and for symbols quoted in another currency than the account (the engine rejects all
  // of these as well).
  const blockedReason =
    props.pausedReason ??
    replayTradeBlock(paper.highWater, time) ??
    quoteMismatchReason(account, symbol, symbolQuote)
  const fmtPrice = (sym: string, p: number) =>
    props.formatPrice ? props.formatPrice(sym, p) : formatPriceByTick(p, undefined)

  const [tab, setTab] = useState<Tab>('positions')
  const [resetting, setResetting] = useState(false)
  const ids = useId()
  const tabId = (t: Tab) => `${ids}-tab-${t}`

  // The chart's own price marks its position only while its feed is live (a stale price is not
  // a mark); otherwise the position falls back to its last fill price.
  const markPrice = offlineReason === null ? price : null
  const allMarks = useMemo<PaperMarks>(
    () => (markPrice !== null && markPrice > 0 ? { ...marks, [symbol]: markPrice } : { ...marks }),
    [marks, symbol, markPrice],
  )
  const summary = useMemo(() => accountSummary(account, allMarks), [account, allMarks])
  const positions = useMemo(() => openPositions(account), [account])
  const orders = useMemo(() => openOrders(account).reverse(), [account])
  const fills = useMemo(() => [...account.fills].reverse(), [account])

  const counts: Readonly<Record<Tab, number>> = {
    positions: positions.length,
    orders: orders.length,
    history: fills.length,
  }

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.indexOf(tab)
    const next =
      e.key === 'ArrowRight'
        ? TABS[(i + 1) % TABS.length]
        : e.key === 'ArrowLeft'
          ? TABS[(i + TABS.length - 1) % TABS.length]
          : e.key === 'Home'
            ? TABS[0]
            : e.key === 'End'
              ? TABS[TABS.length - 1]
              : null
    if (next === null) return
    e.preventDefault()
    setTab(next)
    document.getElementById(tabId(next))?.focus()
  }

  const closePosition = (p: PaperPosition) => {
    if (price === null || time === null) return
    const qty = availableQty(account, p.symbol)
    if (qty <= 0) return
    paper.place(
      { symbol: p.symbol, side: 'sell', type: 'market', qty, quoteAsset: symbolQuote },
      { price, time, blocked: blockedReason, marketBlocked: offlineReason },
    )
  }

  return (
    <section className="paper" aria-labelledby={`${ids}-title`}>
      <header className="paper__header">
        <h2 id={`${ids}-title`} className="paper__title">
          Paper trading
        </h2>
        <span className={`paper__scope paper__scope--${scope}`}>
          {scope === 'replay' ? 'Replay' : 'Live prices'}
        </span>
        <span className="paper__spacer" />
        <button
          type="button"
          className="paper__icon-btn"
          aria-label="Reset paper account"
          aria-expanded={resetting}
          title="Reset account"
          onClick={() => setResetting((r) => !r)}
        >
          <ResetIcon />
        </button>
      </header>

      <p className="paper__sim" role="note">
        <ShieldIcon />
        <span>
          <strong>Paper — simulated, no real orders.</strong>{' '}
          {scope === 'replay'
            ? 'Replay account: separate from live, cleared when replay ends.'
            : 'Pretend balance, filled on the prices shown here.'}
        </span>
      </p>

      {resetting && (
        <ResetForm
          account={account}
          symbolQuote={symbolQuote}
          onCancel={() => setResetting(false)}
          onReset={(cfg) => {
            paper.reset(cfg)
            setResetting(false)
          }}
        />
      )}

      <div className="paper__scroll">
        <div className="paper__summary" aria-label="Account summary">
          <div className="paper__equity">
            <span className="paper__label">Equity</span>
            <span className="paper__equity-value">
              {money(summary.equity)} <span className="paper__unit">{quote}</span>
            </span>
            <span className={`paper__return paper__tone--${tone(summary.totalPnl)}`}>
              {signedMoney(summary.totalPnl)} ({signedPct(summary.returnPct)})
            </span>
          </div>
          <dl className="paper__stats">
            <Stat label="Available" value={money(summary.availableCash)} />
            <Stat label="In orders" value={money(summary.reservedCash)} />
            <Stat
              label="Realized"
              value={signedMoney(summary.realizedPnl)}
              tone={tone(summary.realizedPnl)}
            />
            <Stat
              label="Unrealized"
              value={signedMoney(summary.unrealizedPnl)}
              tone={tone(summary.unrealizedPnl)}
            />
          </dl>
        </div>

        <OrderTicket
          key={`${scope}:${symbol}`}
          symbol={symbol}
          symbolQuote={symbolQuote}
          price={price}
          time={time}
          paper={paper}
          blockedReason={blockedReason}
          offlineReason={offlineReason}
          fmtPrice={fmtPrice}
        />

        <div className="paper__tabs" role="tablist" aria-label="Paper account">
          {TABS.map((t) => (
            <button
              key={t}
              id={tabId(t)}
              type="button"
              role="tab"
              className="paper__tab"
              aria-selected={tab === t}
              aria-controls={tab === t ? `${ids}-panel` : undefined}
              tabIndex={tab === t ? 0 : -1}
              onClick={() => setTab(t)}
              onKeyDown={onTabKeyDown}
            >
              {TAB_LABELS[t]}
              {counts[t] > 0 && <span className="paper__tab-count">{counts[t]}</span>}
            </button>
          ))}
        </div>

        <div
          id={`${ids}-panel`}
          className="paper__list"
          role="tabpanel"
          aria-labelledby={tabId(tab)}
        >
          {tab === 'positions' &&
            (positions.length === 0 ? (
              <Empty text="No open positions" />
            ) : (
              <ul className="paper__rows">
                {positions.map((p) => (
                  <PositionRow
                    key={p.symbol}
                    position={p}
                    mark={allMarks[p.symbol] ?? null}
                    quote={quote}
                    fmtPrice={fmtPrice}
                    onSelectSymbol={onSelectSymbol}
                    canClose={
                      p.symbol === symbol &&
                      price !== null &&
                      time !== null &&
                      blockedReason === null &&
                      offlineReason === null &&
                      availableQty(account, p.symbol) > 0
                    }
                    closeBlockedReason={
                      p.symbol === symbol ? (blockedReason ?? offlineReason) : null
                    }
                    onClose={() => closePosition(p)}
                  />
                ))}
              </ul>
            ))}
          {tab === 'orders' &&
            (orders.length === 0 ? (
              <Empty text="No open orders" />
            ) : (
              <ul className="paper__rows">
                {orders.map((o) => (
                  <OrderRow
                    key={o.id}
                    order={o}
                    quote={quote}
                    fmtPrice={fmtPrice}
                    onCancel={() => paper.cancel(o.id, time ?? o.createdAt)}
                  />
                ))}
              </ul>
            ))}
          {tab === 'history' &&
            (fills.length === 0 ? (
              <Empty text="No trades yet" />
            ) : (
              <ul className="paper__rows">
                {fills.map((f) => (
                  <FillRow key={f.id} fill={f} quote={quote} fmtPrice={fmtPrice} />
                ))}
              </ul>
            ))}
        </div>
      </div>
    </section>
  )
}

function Stat({ label, value, tone: t }: { label: string; value: string; tone?: string }) {
  return (
    <div className="paper__stat">
      <dt className="paper__label">{label}</dt>
      <dd className={`paper__stat-value${t ? ` paper__tone--${t}` : ''}`}>{value}</dd>
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return <p className="paper__empty">{text}</p>
}

// ---------- Order ticket ----------

interface TicketProps {
  symbol: string
  /** The symbol's quote asset, null when unknown. */
  symbolQuote: string | null
  price: number | null
  time: number | null
  paper: PaperAccountApi
  /** Every order is refused (reviewing earlier replay bars, another quote currency). */
  blockedReason: string | null
  /** Orders that would fill now are refused (live feed offline). */
  offlineReason: string | null
  fmtPrice(symbol: string, price: number): string
}

type Feedback = { kind: 'ok' | 'error'; text: string } | null

function OrderTicket({
  symbol,
  symbolQuote,
  price,
  time,
  paper,
  blockedReason,
  offlineReason,
  fmtPrice,
}: TicketProps) {
  const { account } = paper
  const quote = account.config.quoteAsset
  const base = symbolBaseAsset(symbol, symbolQuote)
  const ids = useId()

  const [side, setSide] = useState<PaperSide>('buy')
  const [type, setType] = useState<PaperOrderType>('market')
  const [qtyText, setQtyText] = useState('')
  const [limitText, setLimitText] = useState('')
  const [feedback, setFeedback] = useState<Feedback>(null)

  const qty = parseNum(qtyText)
  const limit = parseNum(limitText)
  const execPrice = executionPrice(account, side, type, price, limit)
  const est = qty !== null ? estimateOrder(account, side, qty, execPrice) : null
  const cashFree = availableCash(account)
  const qtyFree = availableQty(account, symbol)
  const maxQty = maxOrderQty(account, symbol, side, execPrice)

  const qtyError =
    qtyText.trim() !== '' && (qty === null || qty <= 0) ? 'Enter a quantity above 0' : null
  const limitError =
    type === 'limit' && limitText.trim() !== '' && (limit === null || limit <= 0)
      ? 'Enter a price above 0'
      : null
  const overError =
    est === null
      ? null
      : side === 'buy'
        ? est.total > cashFree * (1 + 1e-9)
          ? `Exceeds available ${quote}`
          : null
        : qty !== null && qty > qtyFree * (1 + 1e-9)
          ? `Exceeds available ${base}`
          : null
  // Limits need the market price too: a limit at or through it fills at once.
  const waiting = time === null || price === null
  const takesNow = type === 'market' || isMarketableLimit(side, limit, price)
  const pausedReason = blockedReason ?? (takesNow ? offlineReason : null)
  const canSubmit =
    !waiting &&
    pausedReason === null &&
    qty !== null &&
    qty > 0 &&
    (type === 'market' || (limit !== null && limit > 0)) &&
    overError === null

  const edit =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v)
      setFeedback(null)
    }

  const chooseType = (t: PaperOrderType) => {
    setType(t)
    setFeedback(null)
    if (t === 'limit' && limitText === '' && price !== null) setLimitText(String(price))
  }

  const fillPercent = (f: number) => {
    const q = roundQtyDown(maxQty * f)
    setQtyText(q > 0 ? String(q) : '')
    setFeedback(null)
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!canSubmit || qty === null || time === null || price === null) return
    const order = paper.place(
      {
        symbol,
        side,
        type,
        qty,
        limitPrice: type === 'limit' ? limit : null,
        quoteAsset: symbolQuote,
      },
      { price, time, blocked: blockedReason, marketBlocked: offlineReason },
    )
    const verb = side === 'buy' ? 'Bought' : 'Sold'
    if (order.status === 'rejected') {
      setFeedback({ kind: 'error', text: order.reason ?? 'Order rejected' })
    } else if (order.status === 'filled' && order.fillPrice !== null) {
      setFeedback({
        kind: 'ok',
        text: `${verb} ${fmtQty(order.qty)} ${base} at ${fmtPrice(symbol, order.fillPrice)}`,
      })
      setQtyText('')
    } else {
      setFeedback({
        kind: 'ok',
        text: `Limit ${side} ${fmtQty(order.qty)} ${base} at ${fmtPrice(symbol, order.limitPrice ?? 0)} placed. Fills on later prices only.`,
      })
      setQtyText('')
    }
  }

  const sideLabel = side === 'buy' ? 'Buy' : 'Sell'

  return (
    <form className={`paper-ticket paper-ticket--${side}`} onSubmit={submit} noValidate>
      <div className="paper-ticket__sides" role="radiogroup" aria-label="Side">
        {(['buy', 'sell'] as const).map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={side === s}
            className={`paper-ticket__side paper-ticket__side--${s}`}
            onClick={() => edit(setSide)(s)}
          >
            <span>{s === 'buy' ? 'Buy' : 'Sell'}</span>
            <span className="paper-ticket__side-price">
              {price !== null
                ? fmtPrice(symbol, executionPrice(account, s, 'market', price, null) ?? price)
                : '—'}
            </span>
          </button>
        ))}
      </div>

      <div className="paper-ticket__types" role="radiogroup" aria-label="Order type">
        {(['market', 'limit'] as const).map((t) => (
          <button
            key={t}
            type="button"
            role="radio"
            aria-checked={type === t}
            className="paper-ticket__type"
            onClick={() => chooseType(t)}
          >
            {t === 'market' ? 'Market' : 'Limit'}
          </button>
        ))}
      </div>

      <div className="paper-field">
        <label
          className="paper-field__label"
          id={`${ids}-price-label`}
          htmlFor={type === 'limit' ? `${ids}-price` : undefined}
        >
          Price
        </label>
        {type === 'market' ? (
          <div className="paper-input paper-input--readonly" aria-labelledby={`${ids}-price-label`}>
            <span>Market</span>
            <span className="paper-input__aside">
              {price !== null ? fmtPrice(symbol, price) : 'waiting…'}
            </span>
          </div>
        ) : (
          <div className="paper-input-wrap">
            <input
              id={`${ids}-price`}
              className={`paper-input paper-input--num${limitError ? ' paper-input--invalid' : ''}`}
              inputMode="decimal"
              autoComplete="off"
              spellCheck={false}
              placeholder={price !== null ? String(price) : 'Limit price'}
              value={limitText}
              aria-invalid={limitError !== null}
              onChange={(e) => edit(setLimitText)(e.target.value)}
            />
            <button
              type="button"
              className="paper-input-wrap__btn"
              disabled={price === null}
              title="Use the last price"
              onClick={() => price !== null && edit(setLimitText)(String(price))}
            >
              Last
            </button>
            <span className="paper-input-wrap__unit">{quote}</span>
          </div>
        )}
        {limitError && <p className="paper-field__error">{limitError}</p>}
      </div>

      <div className="paper-field">
        <label className="paper-field__label" htmlFor={`${ids}-qty`}>
          Quantity
        </label>
        <div className="paper-input-wrap">
          <input
            id={`${ids}-qty`}
            className={`paper-input paper-input--num${qtyError || overError ? ' paper-input--invalid' : ''}`}
            inputMode="decimal"
            autoComplete="off"
            spellCheck={false}
            placeholder="0"
            value={qtyText}
            aria-invalid={qtyError !== null || overError !== null}
            aria-describedby={`${ids}-avail`}
            onChange={(e) => edit(setQtyText)(e.target.value)}
          />
          <span className="paper-input-wrap__unit">{base}</span>
        </div>
        <div className="paper-ticket__pcts">
          {PERCENTS.map((f) => (
            <button
              key={f}
              type="button"
              className="paper-ticket__pct"
              disabled={maxQty <= 0}
              onClick={() => fillPercent(f)}
            >
              {f * 100}%
            </button>
          ))}
        </div>
        {(qtyError || overError) && <p className="paper-field__error">{qtyError ?? overError}</p>}
      </div>

      <dl className="paper-ticket__est">
        <div id={`${ids}-avail`}>
          <dt>Available</dt>
          <dd>{side === 'buy' ? `${money(cashFree)} ${quote}` : `${fmtQty(qtyFree)} ${base}`}</dd>
        </div>
        <div>
          <dt>{side === 'buy' ? 'Est. cost' : 'Est. proceeds'}</dt>
          <dd>{est ? `${money(est.notional)} ${quote}` : '—'}</dd>
        </div>
        <div>
          <dt>Fee ({(account.config.feeRate * 100).toFixed(2)}%)</dt>
          <dd>{est ? `${money(est.fee)} ${quote}` : '—'}</dd>
        </div>
        <div className="paper-ticket__total">
          <dt>{side === 'buy' ? 'Total' : 'You receive'}</dt>
          <dd>{est ? `${money(est.total)} ${quote}` : '—'}</dd>
        </div>
      </dl>

      {pausedReason !== null && (
        <p className="paper-ticket__paused" role="note">
          {pausedReason}
          {pausedReason === offlineReason && type === 'limit'
            ? '. This limit is at or through the last price and would fill now.'
            : ''}
        </p>
      )}

      <button type="submit" className="paper-ticket__submit" disabled={!canSubmit}>
        {waiting
          ? 'Waiting for prices…'
          : `${sideLabel} ${qty !== null && qty > 0 ? `${fmtQty(qty)} ` : ''}${base} · ${
              type === 'market' ? 'Market' : 'Limit'
            }`}
      </button>

      <p
        className={`paper-ticket__feedback${feedback ? ` paper-ticket__feedback--${feedback.kind}` : ''}`}
        role="status"
        aria-live="polite"
      >
        {feedback?.text}
      </p>
    </form>
  )
}

// ---------- Rows ----------

function SideChip({ side }: { side: PaperSide }) {
  return <span className={`paper-chip paper-chip--${side}`}>{side === 'buy' ? 'Buy' : 'Sell'}</span>
}

function SymbolLabel({
  symbol,
  onSelectSymbol,
}: {
  symbol: string
  onSelectSymbol?(symbol: string): void
}) {
  return onSelectSymbol ? (
    <button
      type="button"
      className="paper-row__symbol paper-row__symbol--link"
      title={`Show ${symbol} on the chart`}
      onClick={() => onSelectSymbol(symbol)}
    >
      {symbol}
    </button>
  ) : (
    <span className="paper-row__symbol">{symbol}</span>
  )
}

function PositionRow({
  position: p,
  mark,
  quote,
  fmtPrice,
  onSelectSymbol,
  canClose,
  closeBlockedReason,
  onClose,
}: {
  position: PaperPosition
  mark: number | null
  quote: string
  fmtPrice(symbol: string, price: number): string
  onSelectSymbol?(symbol: string): void
  canClose: boolean
  /** Why closing at market is paused right now, if it is. */
  closeBlockedReason: string | null
  onClose(): void
}) {
  const pnl = positionPnl(p, mark)
  const t = tone(pnl.unrealized)
  return (
    <li className="paper-row">
      <div className="paper-row__top">
        <SymbolLabel symbol={p.symbol} onSelectSymbol={onSelectSymbol} />
        <span className="paper-row__qty">{fmtQty(p.qty)}</span>
        <span className="paper__spacer" />
        <span className={`paper-row__pnl paper__tone--${t}`}>
          {signedMoney(pnl.unrealized)} <small>{signedPct(pnl.unrealizedPct)}</small>
        </span>
      </div>
      <div className="paper-row__meta">
        <span>
          Avg <b>{fmtPrice(p.symbol, p.avgPrice)}</b>
        </span>
        <span>
          Mark <b>{fmtPrice(p.symbol, pnl.mark)}</b>
          {mark === null && <span title="No live price: last fill price"> *</span>}
        </span>
        <span className="paper__spacer" />
        <button
          type="button"
          className="paper-row__action"
          disabled={!canClose}
          title={
            canClose
              ? `Sell the free ${p.symbol} quantity at market`
              : (closeBlockedReason ?? 'Open this symbol on the chart to close at market')
          }
          onClick={onClose}
        >
          Close
        </button>
      </div>
      <div className="paper-row__meta">
        <span>
          Value{' '}
          <b>
            {money(pnl.marketValue)} {quote}
          </b>
        </span>
        {Math.abs(p.realizedPnl) >= 0.005 && (
          <span>
            Realized{' '}
            <b className={`paper__tone--${tone(p.realizedPnl)}`}>{signedMoney(p.realizedPnl)}</b>
          </span>
        )}
      </div>
    </li>
  )
}

function OrderRow({
  order: o,
  quote,
  fmtPrice,
  onCancel,
}: {
  order: PaperOrder
  quote: string
  fmtPrice(symbol: string, price: number): string
  onCancel(): void
}) {
  return (
    <li className="paper-row">
      <div className="paper-row__top">
        <SideChip side={o.side} />
        <span className="paper-row__symbol">{o.symbol}</span>
        <span className="paper__spacer" />
        <button
          type="button"
          className="paper-row__icon"
          aria-label={`Cancel ${o.side} limit ${o.symbol} at ${fmtPrice(o.symbol, o.limitPrice ?? 0)}`}
          title="Cancel order"
          onClick={onCancel}
        >
          <CloseIcon />
        </button>
      </div>
      <div className="paper-row__meta">
        <span>
          Limit <b>{fmtPrice(o.symbol, o.limitPrice ?? 0)}</b>
        </span>
        <span>
          Qty <b>{fmtQty(o.qty)}</b>
        </span>
        <span className="paper__spacer" />
        <time className="paper-row__time" dateTime={new Date(o.createdAt * 1000).toISOString()}>
          {formatUtc(o.createdAt).slice(5)}
        </time>
      </div>
      {o.side === 'buy' && o.limitPrice !== null && (
        <div className="paper-row__meta">
          <span>
            Reserved{' '}
            <b>
              {money(o.qty * o.limitPrice)} {quote}
            </b>{' '}
            + fee
          </span>
        </div>
      )}
    </li>
  )
}

function FillRow({
  fill: f,
  quote,
  fmtPrice,
}: {
  fill: PaperFill
  quote: string
  fmtPrice(symbol: string, price: number): string
}) {
  return (
    <li className="paper-row">
      <div className="paper-row__top">
        <SideChip side={f.side} />
        <span className="paper-row__symbol">{f.symbol}</span>
        <span className="paper__spacer" />
        {f.side === 'sell' && (
          <span className={`paper-row__pnl paper__tone--${tone(f.realizedPnl)}`}>
            {signedMoney(f.realizedPnl)}
          </span>
        )}
      </div>
      <div className="paper-row__meta">
        <span>
          <b>{fmtQty(f.qty)}</b> @ <b>{fmtPrice(f.symbol, f.price)}</b>
        </span>
        <span className="paper__spacer" />
        <time className="paper-row__time" dateTime={new Date(f.time * 1000).toISOString()}>
          {formatUtc(f.time).slice(5)}
        </time>
      </div>
      <div className="paper-row__meta">
        <span>
          Fee{' '}
          <b>
            {money(f.fee)} {quote}
          </b>
        </span>
      </div>
    </li>
  )
}

// ---------- Reset ----------

function ResetForm({
  account,
  symbolQuote,
  onCancel,
  onReset,
}: {
  account: PaperAccount
  /** The active symbol's quote asset: offered so its chart can be traded after the reset. */
  symbolQuote: string | null
  onCancel(): void
  onReset(cfg: {
    startingBalance: number
    feeRate: number
    slippageBps: number
    quoteAsset: string
  }): void
}) {
  const ids = useId()
  const own = account.config.quoteAsset
  const choices = [
    ...new Set([...QUOTE_CHOICES, own, ...(symbolQuote !== null ? [symbolQuote] : [])]),
  ]
  // Start on the chart's currency when the account cannot trade it, else keep the account's.
  const [quoteAsset, setQuoteAsset] = useState(
    symbolQuote !== null && symbolQuote !== own.toUpperCase() ? symbolQuote : own,
  )
  const [balance, setBalance] = useState(String(account.config.startingBalance))
  const [feePct, setFeePct] = useState(String(+(account.config.feeRate * 100).toFixed(4)))
  const [slip, setSlip] = useState(String(account.config.slippageBps))

  const b = parseNum(balance)
  const f = parseNum(feePct)
  const s = parseNum(slip)
  const valid =
    b !== null &&
    b > 0 &&
    b <= 1e12 &&
    f !== null &&
    f >= 0 &&
    f <= 10 &&
    s !== null &&
    s >= 0 &&
    s <= 1000

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!valid) return
    onReset({ startingBalance: b, feeRate: f / 100, slippageBps: s, quoteAsset })
  }

  return (
    <form
      className="paper-reset"
      onSubmit={submit}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          onCancel()
        }
      }}
      aria-label="Reset paper account"
    >
      <p className="paper-reset__text">
        Start over: clears every position, order and trade in this {own} account. This cannot be
        undone.
      </p>
      <div className="paper-reset__grid">
        <label htmlFor={`${ids}-quote`}>Currency</label>
        <select
          id={`${ids}-quote`}
          className="paper-input"
          value={quoteAsset}
          onChange={(e) => setQuoteAsset(e.target.value)}
        >
          {choices.map((q) => (
            <option key={q} value={q}>
              {q}
            </option>
          ))}
        </select>
        <label htmlFor={`${ids}-bal`}>Balance</label>
        <input
          id={`${ids}-bal`}
          className="paper-input paper-input--num"
          inputMode="decimal"
          value={balance}
          onChange={(e) => setBalance(e.target.value)}
        />
        <label htmlFor={`${ids}-fee`}>Fee %</label>
        <input
          id={`${ids}-fee`}
          className="paper-input paper-input--num"
          inputMode="decimal"
          value={feePct}
          onChange={(e) => setFeePct(e.target.value)}
        />
        <label htmlFor={`${ids}-slip`}>Slippage bps</label>
        <input
          id={`${ids}-slip`}
          className="paper-input paper-input--num"
          inputMode="decimal"
          value={slip}
          onChange={(e) => setSlip(e.target.value)}
        />
      </div>
      <div className="paper-reset__actions">
        <button
          type="button"
          className="paper-reset__btn"
          onClick={() => {
            setQuoteAsset(DEFAULT_PAPER_CONFIG.quoteAsset)
            setBalance(String(DEFAULT_PAPER_CONFIG.startingBalance))
            setFeePct(String(DEFAULT_PAPER_CONFIG.feeRate * 100))
            setSlip(String(DEFAULT_PAPER_CONFIG.slippageBps))
          }}
        >
          Defaults
        </button>
        <span className="paper__spacer" />
        <button type="button" className="paper-reset__btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="submit"
          className="paper-reset__btn paper-reset__btn--danger"
          disabled={!valid}
        >
          Reset account
        </button>
      </div>
    </form>
  )
}

// ---------- Icons ----------

const ICON = {
  width: 16,
  height: 16,
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

function ResetIcon() {
  return (
    <svg {...ICON}>
      <path d="M2.8 8a5.2 5.2 0 1 0 1.6-3.8" />
      <path d="M2.6 2.4v2.9h2.9" />
    </svg>
  )
}

function ShieldIcon() {
  return (
    <svg {...ICON} width={14} height={14}>
      <path d="M8 1.8 3 3.6v4c0 3 2.1 5.4 5 6.6 2.9-1.2 5-3.6 5-6.6v-4L8 1.8Z" />
      <path d="m5.8 8 1.5 1.5 3-3" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg {...ICON} width={12} height={12} viewBox="0 0 12 12">
      <path d="M3 3l6 6M9 3l-6 6" />
    </svg>
  )
}
