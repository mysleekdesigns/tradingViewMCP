import {
  unixSeconds,
  type PaperMonitorExpected,
  type PaperMonitorHalt,
  type PaperMonitorProtectionLock,
  type PaperMonitorTest,
} from '../data/paperMonitor'
import type { PaperMonitorState } from '../state/usePaperMonitor'
import { formatUtc } from './quote'

/*
 * The Paper tests section of the paper panel: one read-only row per Freqtrade dry-run test.
 * It has no controls on purpose. Nothing here starts, stops or clears anything; a halted test
 * restarts only when the owner deletes its halt.lock outside the app.
 */

const DASH = '—'

const MONEY = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function signedMoney(n: number): string {
  if (Math.abs(n) < 0.005) return '0.00'
  return `${n > 0 ? '+' : '−'}${MONEY.format(Math.abs(n))}`
}

/** `p` is already in percent (12.5 = 12.5%). */
function pct(p: number): string {
  return `${p.toFixed(2)}%`
}

function signedPct(p: number): string {
  if (Math.abs(p) < 0.005) return '0.00%'
  return `${p > 0 ? '+' : '−'}${Math.abs(p).toFixed(2)}%`
}

const optional = (n: number | null, format: (v: number) => string) =>
  n === null ? DASH : format(n)

const tone = (n: number) => (n > 0.005 ? 'up' : n < -0.005 ? 'down' : 'flat')

/**
 * "2026-12-25 14:00", or null for a time that cannot be shown. `formatUtc` throws on an impossible
 * date, and one bad row must not take the app down, so every date here goes through this.
 */
function utcStamp(seconds: number | null): string | null {
  const safe = unixSeconds(seconds)
  return safe === null ? null : formatUtc(safe)
}

const utcDate = (seconds: number | null) => utcStamp(seconds)?.slice(0, 10) ?? DASH

/** The protocol stores exposure as fractions: 0.036..0.475 -> "3.6–47.5%". */
function exposureRange(e: PaperMonitorExpected): string {
  return `${(e.exposureLo * 100).toFixed(1)}–${(e.exposureHi * 100).toFixed(1)}%`
}

/** "insufficient_data" -> "insufficient data". */
const words = (s: string) => s.replace(/_/g, ' ')

/** An ISO time as shown elsewhere in the panel, or as written when it is not a usable date. */
function isoTime(time: string): string {
  const stamp = utcStamp(Date.parse(time) / 1000)
  return stamp === null ? time : `${stamp} UTC`
}

export function PaperMonitor({ state }: { state: PaperMonitorState }) {
  const { data, error, loading } = state
  return (
    <div className="paper-monitor">
      <p className="paper-monitor__note">
        Read-only view of the Freqtrade dry-run tests. Net P&amp;L is realized: open positions are
        not marked. A halted test restarts only when the owner deletes its <code>halt.lock</code>{' '}
        outside this app.
      </p>
      {data === null ? (
        <p className="paper__empty" role="status">
          {error ?? (loading ? 'Loading paper tests…' : '')}
        </p>
      ) : (
        <>
          <Summary tests={data.tests} generatedAt={data.generatedAt} />
          {error !== null && (
            <p className="paper-monitor__stale" role="status">
              Not refreshed, showing the last answer. {error}
            </p>
          )}
          {data.tests.length === 0 ? (
            <p className="paper__empty">No paper tests found</p>
          ) : (
            <ul className="paper__rows">
              {data.tests.map((t) => (
                <TestRow key={t.id} test={t} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

function Summary({
  tests,
  generatedAt,
}: {
  tests: readonly PaperMonitorTest[]
  generatedAt: number
}) {
  const halted = tests.filter((t) => t.halt !== null).length
  const updated = utcStamp(generatedAt)
  return (
    <div className="paper-monitor__summary">
      <h3 className="paper-monitor__title">Paper tests</h3>
      <span className="paper-row__qty">{tests.length}</span>
      {halted > 0 && <span className="paper-chip paper-chip--halted">{halted} HALTED</span>}
      <span className="paper__spacer" />
      <span className="paper-row__time">
        Updated {updated === null ? DASH : `${updated.slice(5)} UTC`}
      </span>
    </div>
  )
}

function TestRow({ test: t }: { test: PaperMonitorTest }) {
  const { expected, stake, circuitBreaker: breaker } = t
  // Unread sources leave placeholders, so these show a dash, never a reassuring 0.00, Off or None.
  // Other errors (no wallet, a bad comparison) leave the DB's figures standing.
  const figures = t.read.trades
  const protectionsKnown = t.read.protocol && !t.malformed.protections
  const breakerKnown = t.read.protocol && !t.malformed.circuitBreaker
  const place = [t.symbol, t.interval, `to ${utcDate(t.decisionEnd)}`]
    .filter((part) => part !== '')
    .join(' · ')
  return (
    <li className={`paper-row${t.halt !== null ? ' paper-monitor__row--halted' : ''}`}>
      <div className="paper-row__top">
        <span className="paper-row__symbol paper-monitor__name" title={t.name}>
          {t.name}
        </span>
        <span className="paper__spacer" />
        {t.halt !== null && <span className="paper-chip paper-chip--halted">HALTED</span>}
      </div>
      <div className="paper-row__meta">
        <span>{place}</span>
        <span className="paper__spacer" />
        {expected?.verdict != null && (
          <span
            title={
              expected.asof !== null
                ? `Latest compare verdict, data to ${utcDate(expected.asof)}`
                : 'Latest compare verdict'
            }
          >
            {words(expected.verdict)}
          </span>
        )}
      </div>
      <div className="paper-row__meta paper-monitor__meta">
        <span title="Closed trades only: open positions are not marked to market">
          Realized P&amp;L{' '}
          {figures ? (
            <b className={`paper__tone--${tone(t.netPnl)}`}>
              {signedMoney(t.netPnl)} {optional(t.netPnlPct, signedPct)}
            </b>
          ) : (
            <b>{DASH}</b>
          )}
        </span>
        <span title="Largest fall of realized equity from its peak">
          Realized DD <b>{figures && t.wallet !== null ? pct(t.drawdownPct) : DASH}</b>
        </span>
      </div>
      <div className="paper-row__meta paper-monitor__meta">
        <span>
          Trades <b>{figures ? t.closedTrades : DASH}</b>
          {figures && t.openTrades > 0 && ` + ${t.openTrades} open`}
        </span>
        <span title="Backtest-expected closed trades over the whole test, up to its end date">
          Expected <b>{expected ? `${expected.tradesLo}–${expected.tradesHi}` : DASH}</b>
        </span>
        <span title="Backtest-expected share of bars in a position over the whole test">
          In market <b>{expected ? exposureRange(expected) : DASH}</b>
        </span>
      </div>
      <div className="paper-row__meta paper-monitor__meta">
        <span title="Stake of the latest trade">
          Stake <b>{optional(stake.last, (n) => MONEY.format(n))}</b>
        </span>
        <span title="Risk per trade, percent of the wallet">
          Risk <b>{optional(stake.riskPerTradePct, pct)}</b>
        </span>
        <span title="Stake in open trades, percent of the wallet">
          Open exposure <b>{figures ? optional(stake.openExposurePct, pct) : DASH}</b>
        </span>
        <span title="Cap on total exposure, percent of the wallet">
          Max exposure <b>{optional(stake.maxTotalExposurePct, pct)}</b>
        </span>
      </div>
      <div className="paper-row__meta paper-monitor__meta">
        <span>
          Protections <b>{protectionsKnown ? (t.protections ? 'On' : 'Off') : DASH}</b>
        </span>
        <span title={breaker ? 'Daily loss / drawdown from peak that stop this test' : undefined}>
          Breaker <b>{breakerKnown ? breakerLimits(breaker) : DASH}</b>
        </span>
        {t.protectionLock !== null && <ProtectionLock lock={t.protectionLock} />}
      </div>
      {t.halt !== null && <HaltNotice halt={t.halt} />}
      {t.error !== null && <p className="paper-monitor__error">{t.error}</p>}
    </li>
  )
}

function breakerLimits(breaker: PaperMonitorTest['circuitBreaker']): string {
  if (breaker === null) return 'None'
  return `${pct(breaker.maxDailyLossPct)} / ${pct(breaker.maxDrawdownPct)}`
}

function ProtectionLock({ lock }: { lock: PaperMonitorProtectionLock }) {
  return (
    <span className="paper-monitor__lock" title={lock.reason === '' ? undefined : lock.reason}>
      Locked until <b>{lock.until === '' ? DASH : isoTime(lock.until)}</b>
    </span>
  )
}

function HaltNotice({ halt }: { halt: PaperMonitorHalt }) {
  const { reason, value, threshold, time } = halt
  return (
    <p className="paper-monitor__halt">
      <strong>{words(reason)}</strong>
      {value !== null &&
        threshold !== null &&
        `: ${pct(value)} against a limit of ${pct(threshold)}`}
      {time !== '' && `, ${isoTime(time)}`}
    </p>
  )
}
