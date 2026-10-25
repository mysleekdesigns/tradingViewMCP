import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle } from '../types'
import {
  MONTH_LABELS,
  MULTIPLE_COMPARISONS_NOTE,
  SMALL_N_MONTH,
  SMALL_N_WEEKDAY,
  WEEKDAY_LABELS,
  computeSeasonals,
  deviation,
  historyTruncated,
  incompleteReason,
  loadDailyHistory,
  nextTabIndex,
  noiseHalfWidth,
  noneComparable,
  pooledMean,
  weekdayBar,
  weekdayScale,
  type Deviation,
  type MonthCell,
  type ReturnStats,
  type SeasonalsResult,
} from './seasonals'
import { useModalKeys } from './useModalKeys'
import './Seasonals.css'

export interface SeasonalsViewProps {
  symbol: string
  provider: MarketDataProvider
  venueName: string
  onClose: () => void
}

type Tab = 'monthly' | 'weekday'
const TABS: readonly Tab[] = ['monthly', 'weekday']
const TAB_LABELS: Record<Tab, string> = { monthly: 'Monthly', weekday: 'Weekday' }

/** Heat-map intensity saturates at this absolute monthly return. */
const HEAT_CAP = 0.25

/** Focus the selected tab (it exists in every body state), else the dialog itself. */
function focusActiveTab(root: HTMLElement | null): void {
  const active = root?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
  ;(active ?? root)?.focus()
}

type Load =
  | { key: string; kind: 'ready'; candles: Candle[]; nowSec: number }
  | { key: string; kind: 'error'; message: string }

const DATE_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
})

function fmtDate(sec: number): string {
  return DATE_FMT.format(new Date(sec * 1000))
}

/** Signed percentage with a true minus sign; tiny values never show as "-0.0%". */
function fmtPct(x: number | null, digits: number): string {
  if (x === null || !Number.isFinite(x)) return '—'
  const abs = Math.abs(x * 100).toFixed(digits)
  if (Number(abs) === 0) return `${(0).toFixed(digits)}%`
  return `${x > 0 ? '+' : '−'}${abs}%`
}

/** Unsigned "±x.x%" for a standard error. */
function fmtSe(x: number | null, digits: number): string {
  if (x === null || !Number.isFinite(x)) return '—'
  return `±${Math.abs(x * 100).toFixed(digits)}%`
}

/** What a table's cells are compared with, in tooltips and captions. */
const MONTH_REF = 'the all-month average'
const WEEKDAY_REF = 'the all-day average'

/**
 * Plain-words reading of a mean against the pooled mean (`refLabel`, e.g. "the all-month
 * average") and its 95% t band, for tooltips and screen readers: a cell that stands out says
 * whether it is above or below the average and by how much. Small samples are never compared.
 * Never says "significant": this is a per-cell screen among many comparisons.
 */
function noiseVerdict(
  s: ReturnStats,
  reference: number | null,
  minN: number,
  refLabel: string,
  digits: number,
): string {
  if (s.mean === null) return 'No data.'
  if (s.n < minN) return `Small sample (fewer than ${minN}): not compared, untinted.`
  if (s.se === null) return 'Single observation: no standard error.'
  if (reference === null) return 'No average to compare with.'
  const dev = deviation(s, reference, minN)
  return dev
    ? `Stands out: ${(dev.size * 100).toFixed(digits)} percentage points ${dev.sign} ${refLabel} (beyond the 95% t band).`
    : `Within noise of ${refLabel} (inside the 95% t band).`
}

/** "vs. the all-month average +1.2%: 95% band ±8.4%" for a tooltip. */
function bandText(s: ReturnStats, reference: number | null, refLabel: string, digits: number) {
  const hw = noiseHalfWidth(s)
  return `vs. ${refLabel} ${fmtPct(reference, digits)}: 95% band ${fmtSe(hw, digits)}`
}

/**
 * Visually hidden reading appended to a Mean cell, so its colour is never the only cue: the
 * direction against the average when it stands out, else why it is untinted.
 */
function meanNote(
  s: ReturnStats,
  dev: Deviation | null,
  minN: number,
  refLabel: string,
): ReactNode {
  if (s.mean === null) return null
  if (dev) return <span className="visually-hidden">{` (${dev.sign} ${refLabel})`}</span>
  if (s.n < minN) return <span className="visually-hidden"> (small sample, not compared)</span>
  if (s.se === null) return null
  return <span className="visually-hidden"> (within noise)</span>
}

/** Tint class of a notable Mean cell: green above the average, red below it. */
function deviationClass(dev: Deviation | null): string {
  if (dev === null) return ''
  return dev.sign === 'above' ? ' is-up' : ' is-down'
}

function fmtShare(x: number | null): string {
  return x === null ? '—' : `${Math.round(x)}%`
}

function signClass(x: number | null): string {
  if (x === null || x === 0) return ''
  return x > 0 ? ' is-up' : ' is-down'
}

/**
 * Inline custom property for the heat tint (a percentage of --up / --down), from a magnitude:
 * a raw return for a year cell, the distance from the average for a Mean cell.
 */
function heatStyle(ret: number | null): CSSProperties | undefined {
  if (ret === null || ret === 0 || !Number.isFinite(ret)) return undefined
  const t = Math.min(Math.abs(ret) / HEAT_CAP, 1)
  return { '--heat': `${(8 + 62 * t).toFixed(1)}%` } as CSSProperties
}

function errorMessage(err: unknown, symbol: string, venueName: string): string {
  if (err instanceof MarketDataError) {
    switch (err.kind) {
      case 'rate-limited':
      case 'banned':
        return `${venueName} is limiting requests${
          err.retryAfter ? `; try again in ${Math.ceil(err.retryAfter)} s` : ''
        }.`
      case 'region-blocked':
        return `${venueName} is not available from this location.`
      case 'bad-symbol':
        return `${venueName} has no daily data for ${symbol}.`
      case 'network':
        return 'The request failed. Check the connection (and the local server) and try again.'
      default:
        return err.message
    }
  }
  return err instanceof Error ? err.message : 'Unknown error.'
}

/**
 * Seasonals for one symbol: a year x month return heat map and a weekday table, from closed
 * daily bars (UTC days). Modal with a focus trap; Esc, the close button, or a click on the
 * backdrop closes it. Mount it fresh per open; it restores the previous focus on unmount.
 */
export function SeasonalsView({ symbol, provider, venueName, onClose }: SeasonalsViewProps) {
  const [tab, setTab] = useState<Tab>('monthly')
  const [attempt, setAttempt] = useState(0)
  const [load, setLoad] = useState<Load | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const ids = useId()
  const titleId = `${ids}-title`
  const captionId = `${ids}-caption`
  const reqKey = `${symbol}|${attempt}`

  // Esc closes and Tab stays inside, even after focus falls to <body>; keys never reach the
  // chart or app shortcuts behind the modal.
  useModalKeys(dialogRef, onClose)

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const active = dialogRef.current?.querySelector<HTMLElement>(
      '[role="tab"][aria-selected="true"]',
    )
    ;(active ?? dialogRef.current)?.focus()
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    // One clock for the loader and the tables: the loader drops the forming row against it, so
    // its row count is the number of closed bars and the cap counts closed bars only.
    const nowSec = Math.floor(Date.now() / 1000)
    loadDailyHistory(provider, symbol, { signal: controller.signal, nowSec }).then(
      (candles) => {
        if (controller.signal.aborted) return
        setLoad({ key: reqKey, kind: 'ready', candles, nowSec })
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        if (err instanceof MarketDataError && err.kind === 'aborted') return
        setLoad({ key: reqKey, kind: 'error', message: errorMessage(err, symbol, venueName) })
      },
    )
    return () => controller.abort()
  }, [provider, symbol, venueName, reqKey])

  const current = load !== null && load.key === reqKey ? load : null
  const ready = current?.kind === 'ready' ? current : null
  const result = useMemo(
    () => (ready ? computeSeasonals(ready.candles, ready.nowSec) : null),
    [ready],
  )
  // The loader was given `nowSec`, so every row is a closed bar.
  const truncated = ready !== null && historyTruncated(ready.candles.length)

  // When the body swaps views (loading -> error -> loading -> tables), the focused node can
  // unmount and focus falls to <body>. Pull it back to the active tab.
  const view = current === null ? 'loading' : current.kind
  useEffect(() => {
    const active = document.activeElement
    if (active === null || active === document.body) focusActiveTab(dialogRef.current)
  }, [view])

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = nextTabIndex(TABS.indexOf(tab), TABS.length, e.key)
    if (i === null) return
    // preventDefault also tells useModalKeys the key was handled here.
    e.preventDefault()
    const next = TABS[i]
    setTab(next)
    document.getElementById(`${ids}-tab-${next}`)?.focus()
  }

  const hasData =
    result !== null && result.lastTime !== null && result.byWeekday.some((s) => s.n > 0)
  const barsUsed = result
    ? 1 + result.byWeekday.reduce((s, x) => s + x.n, 0) + result.excludedDays
    : 0

  return (
    <div
      className="modal-backdrop seasonals__backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="seasonals"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={captionId}
        aria-busy={current === null}
        tabIndex={-1}
      >
        <div className="seasonals__head">
          <span className="seasonals__head-icon" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <rect x="2.5" y="3.5" width="13" height="12" rx="1.5" stroke="currentColor" />
              <path d="M2.5 7h13M6 2v3M12 2v3" stroke="currentColor" strokeLinecap="round" />
              <rect x="5" y="9" width="2.2" height="2" rx=".4" fill="currentColor" />
              <rect x="8" y="9" width="2.2" height="2" rx=".4" fill="currentColor" opacity=".55" />
              <rect x="11" y="9" width="2.2" height="2" rx=".4" fill="currentColor" opacity=".3" />
              <rect x="5" y="12" width="2.2" height="2" rx=".4" fill="currentColor" opacity=".3" />
              <rect x="8" y="12" width="2.2" height="2" rx=".4" fill="currentColor" opacity=".8" />
            </svg>
          </span>
          <div className="seasonals__titles">
            <h2 id={titleId} className="seasonals__title">
              {`Seasonals · ${symbol}`}
            </h2>
            <p id={captionId} className="seasonals__caption">
              <span>{venueName}</span>
              {result && result.firstTime !== null && result.lastTime !== null ? (
                <>
                  <span className="seasonals__sep" aria-hidden="true">
                    ·
                  </span>
                  <span className="seasonals__num">
                    {fmtDate(result.firstTime)} – {fmtDate(result.lastTime)}
                  </span>
                  <span className="seasonals__sep" aria-hidden="true">
                    ·
                  </span>
                  <span>
                    <span className="seasonals__num">{barsUsed.toLocaleString('en-US')}</span>{' '}
                    closed UTC days{truncated ? ' (most recent only)' : ''}
                  </span>
                </>
              ) : (
                <>
                  <span className="seasonals__sep" aria-hidden="true">
                    ·
                  </span>
                  <span>Daily bars, UTC days</span>
                </>
              )}
            </p>
          </div>
          <button
            type="button"
            className="icon-btn"
            aria-label="Close seasonals"
            title="Close (Esc)"
            onClick={onClose}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </button>
        </div>

        <div className="seasonals__bar">
          <div className="seasonals__tabs" role="tablist" aria-label="Seasonals view">
            {TABS.map((t) => (
              <button
                key={t}
                id={`${ids}-tab-${t}`}
                type="button"
                role="tab"
                aria-selected={tab === t}
                aria-controls={`${ids}-panel`}
                tabIndex={tab === t ? 0 : -1}
                className="seasonals__tab"
                onClick={() => setTab(t)}
                onKeyDown={onTabKeyDown}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
          </div>
          {hasData && result && <SummaryMeta tab={tab} result={result} />}
        </div>

        <div
          id={`${ids}-panel`}
          className="seasonals__body"
          role="tabpanel"
          aria-labelledby={`${ids}-tab-${tab}`}
        >
          {current === null ? (
            <LoadingState />
          ) : current.kind === 'error' ? (
            <div className="seasonals-state" role="alert">
              <svg
                className="seasonals-state__icon seasonals-state__icon--error"
                width="26"
                height="26"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.5" />
                <path
                  d="M12 7.5v5.5M12 16v.5"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                />
              </svg>
              <h3 className="seasonals-state__title">Couldn’t load daily history</h3>
              <p className="seasonals-state__message">{current.message}</p>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  setAttempt((a) => a + 1)
                  // This button unmounts with the error view; keep focus in the dialog.
                  focusActiveTab(dialogRef.current)
                }}
              >
                Retry
              </button>
            </div>
          ) : !hasData || result === null ? (
            <div className="seasonals-state" role="status">
              <svg
                className="seasonals-state__icon"
                width="26"
                height="26"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <rect
                  x="3.5"
                  y="5"
                  width="17"
                  height="15"
                  rx="2"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeDasharray="2.4 2.4"
                />
                <path d="M3.5 9.5h17" stroke="currentColor" strokeWidth="1.4" />
              </svg>
              <h3 className="seasonals-state__title">Not enough daily history</h3>
              <p className="seasonals-state__message">
                {venueName} has fewer than two consecutive closed daily bars for {symbol}, so there
                are no returns to tabulate yet.
              </p>
            </div>
          ) : tab === 'monthly' ? (
            <MonthlyTable result={result} />
          ) : (
            <WeekdayTable stats={result.byWeekday} />
          )}
        </div>

        <div className="seasonals__foot">
          <div className="seasonals__notes">
            <p className="seasonals__disclaimer">
              <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
                <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" strokeWidth="1.2" />
                <path
                  d="M7 6.2v4M7 4v.4"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                />
              </svg>
              Historical, not forward-looking. Small samples; not a trading signal.
            </p>
            <p className="seasonals__note">{MULTIPLE_COMPARISONS_NOTE}</p>
          </div>
          {hasData && (tab === 'monthly' ? <HeatLegend /> : <NoiseLegend />)}
        </div>
      </div>
    </div>
  )
}

/**
 * The line beside the tabs: what a return is, the sample size, and the pooled mean every cell
 * is compared with ("All months: mean +x%" / "All days: mean +x%").
 */
export function SummaryMeta({ tab, result }: { tab: Tab; result: SeasonalsResult }) {
  if (tab === 'monthly') {
    const pooled = pooledMean(result.byMonth)
    return (
      <span className="seasonals__meta">
        Month-end close to month-end close ·{' '}
        <span className="seasonals__num">{result.byMonth.reduce((s, x) => s + x.n, 0)}</span>{' '}
        complete months ·{' '}
        <span title="Pooled mean of every complete month; each month's mean is compared with it">
          All months: mean <span className="seasonals__num">{fmtPct(pooled, 1)}</span>
        </span>
      </span>
    )
  }
  const pooled = pooledMean(result.byWeekday)
  return (
    <span className="seasonals__meta">
      Daily close-to-close · <span className="seasonals__num">{result.excludedDays}</span>{' '}
      {result.excludedDays === 1 ? 'day' : 'days'} skipped after gaps ·{' '}
      <span title="Pooled mean of every daily return; each weekday's mean is compared with it">
        All days: mean <span className="seasonals__num">{fmtPct(pooled, 2)}</span>
      </span>
    </span>
  )
}

function LoadingState() {
  return (
    <div className="seasonals-state" role="status">
      <div className="seasonals-skeleton" aria-hidden="true">
        {Array.from({ length: 5 * 12 }, (_, i) => (
          <span
            key={i}
            className="seasonals-skeleton__cell"
            style={{ animationDelay: `${((i % 12) + Math.floor(i / 12)) * 40}ms` }}
          />
        ))}
      </div>
      <p className="seasonals-state__message">Loading daily history…</p>
    </div>
  )
}

/**
 * Footer key for the monthly table. Two colour meanings share one ramp: year cells show the
 * raw return; the Mean row shows the distance above or below the all-month average.
 */
export function HeatLegend() {
  const cap = `${Math.round(HEAT_CAP * 100)}%`
  return (
    <div className="seasonals-legend">
      <span className="seasonals-legend__scale">
        <span className="seasonals-legend__label">Year cells: return</span>
        <span className="visually-hidden">, colour scale capped at plus or minus {cap}: </span>
        <span className="seasonals__num">−{cap}</span>
        <span className="seasonals-legend__ramp" aria-hidden="true" />
        <span className="seasonals__num">+{cap}</span>
      </span>
      <span className="seasonals-legend__item">
        <span className="seasonals-legend__pair" aria-hidden="true">
          <span className="seasonals-legend__dot seasonals-legend__dot--up" />
          <span className="seasonals-legend__dot seasonals-legend__dot--down" />
        </span>
        Mean row: above / below {MONTH_REF}, beyond its 95% band (shade = distance from it)
      </span>
      <span className="seasonals-legend__item">
        <span
          className="seasonals-legend__swatch seasonals-legend__swatch--noise"
          aria-hidden="true"
        />
        Mean within the band, or n &lt; {SMALL_N_MONTH}: untinted
      </span>
      <span className="seasonals-legend__item">
        <span className="seasonals-legend__swatch" aria-hidden="true" />
        Incomplete, excluded
      </span>
    </div>
  )
}

/** Footer key for the weekday table: what the band and the bar colours mean. */
export function NoiseLegend() {
  return (
    <div className="seasonals-legend">
      <span className="seasonals-legend__item">
        <span className="seasonals-legend__band" aria-hidden="true" />
        95% band around {WEEKDAY_REF} (dashed): a bar ending inside it is noise
      </span>
      <span className="seasonals-legend__item">
        <span className="seasonals-legend__pair" aria-hidden="true">
          <span className="seasonals-legend__dot seasonals-legend__dot--up" />
          <span className="seasonals-legend__dot seasonals-legend__dot--down" />
        </span>
        Mean above / below {WEEKDAY_REF}, beyond its band; grey: within noise or n &lt;{' '}
        {SMALL_N_WEEKDAY}
      </span>
    </div>
  )
}

/** One line shown when no cell in a table has enough data to be compared, so nothing is tinted. */
function NotComparedNotice({ text }: { text: string }) {
  return (
    <p className="seasonals-notice" role="note">
      <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
        <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" strokeWidth="1.2" />
        <path d="M7 6.2v4M7 4v.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      {text}
    </p>
  )
}

/** Visually hidden "(small sample)" plus a tooltip, so the warn colour is not the only cue. */
function SmallSampleNote({ small }: { small: boolean }) {
  return small ? <span className="visually-hidden"> (small sample)</span> : null
}

interface StatCell {
  text: string
  cls?: string
  style?: CSSProperties
  title?: string
  extra?: ReactNode
}

export function MonthlyTable({ result }: { result: SeasonalsResult }) {
  const grid = useMemo(() => {
    const m = new Map<string, MonthCell>()
    for (const c of result.monthly) m.set(`${c.year}-${c.month}`, c)
    return m
  }, [result])
  const yearsDesc = [...result.years].reverse()
  const pooled = pooledMean(result.byMonth)
  const noneCompared = noneComparable(result.byMonth, SMALL_N_MONTH)

  const statRow = (
    label: string,
    hint: string,
    render: (s: ReturnStats, month: string) => StatCell,
    rowCls: string,
  ) => (
    <tr className={`seasonals-grid__stat ${rowCls}`}>
      <th scope="row" title={hint}>
        {label}
      </th>
      {result.byMonth.map((s, i) => {
        const v = render(s, MONTH_LABELS[i])
        return (
          <td
            key={i}
            className={v.cls}
            style={v.style}
            title={v.title ?? `${MONTH_LABELS[i]} ${label.toLowerCase()}: ${v.text} (n = ${s.n})`}
          >
            {v.text}
            {v.extra}
          </td>
        )
      })}
    </tr>
  )

  return (
    <>
      {noneCompared && (
        <NotComparedNotice
          text={`Fewer than ${SMALL_N_MONTH} complete years per month: no month is compared.`}
        />
      )}
      <div
        className="seasonals-scroll"
        tabIndex={0}
        role="region"
        aria-label="Monthly returns by year"
      >
        <table className="seasonals-grid">
          <caption className="visually-hidden">
            Monthly returns by year (month-end close to month-end close, UTC), with summary rows
            over complete months only. Year cells are coloured by their return. All months: mean{' '}
            {fmtPct(pooled, 1)}. A month’s mean is tinted only when it lies outside its 95% t band
            around {MONTH_REF} and the month has at least {SMALL_N_MONTH} complete samples: green
            when above {MONTH_REF}, red when below, shaded by the distance from it.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="seasonals-grid__corner">
                Year
              </th>
              {MONTH_LABELS.map((m) => (
                <th key={m} scope="col">
                  {m}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {yearsDesc.map((year) => (
              <tr key={year}>
                <th scope="row" className="seasonals__num">
                  {year}
                </th>
                {MONTH_LABELS.map((label, month) => {
                  const c = grid.get(`${year}-${month}`)
                  if (!c) {
                    return (
                      <td key={month} className="seasonals-cell seasonals-cell--none">
                        <span className="visually-hidden">No data</span>
                      </td>
                    )
                  }
                  const reason = incompleteReason(c, result)
                  const text = fmtPct(c.ret, 1)
                  const cls = `seasonals-cell${signClass(c.ret)}${
                    c.complete ? '' : ' seasonals-cell--incomplete'
                  }`
                  return (
                    <td
                      key={month}
                      className={cls}
                      style={c.complete ? heatStyle(c.ret) : undefined}
                      title={
                        reason
                          ? `${label} ${year}: ${text}. ${reason}`
                          : `${label} ${year}: ${text}`
                      }
                    >
                      {text}
                      {reason && <span className="visually-hidden"> (incomplete: {reason})</span>}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            {statRow(
              'Mean',
              `Average over complete months; tinted only outside the 95% t band around ${MONTH_REF} (${fmtPct(pooled, 1)}): green above it, red below it, shaded by the distance. Never tinted when n < ${SMALL_N_MONTH}`,
              (s, month) => {
                // Colour and shade follow the deviation from the average, not the mean's sign.
                const dev = deviation(s, pooled, SMALL_N_MONTH)
                const title = `${month} mean: ${fmtPct(s.mean, 1)} ${fmtSe(s.se, 1)} SE (n = ${s.n}), ${bandText(s, pooled, MONTH_REF, 1)}. ${noiseVerdict(s, pooled, SMALL_N_MONTH, MONTH_REF, 1)}`
                const extra = meanNote(s, dev, SMALL_N_MONTH, MONTH_REF)
                return dev
                  ? {
                      text: fmtPct(s.mean, 1),
                      cls: `seasonals-cell${deviationClass(dev)}`,
                      style: heatStyle(dev.size),
                      title,
                      extra,
                    }
                  : {
                      text: fmtPct(s.mean, 1),
                      cls: `seasonals-cell${s.mean === null ? '' : ' seasonals-cell--noise'}`,
                      title,
                      extra,
                    }
              },
              'seasonals-grid__stat--first',
            )}
            {statRow(
              '± SE',
              'Standard error of the mean: sample sd (n − 1) / √n. Blank when n < 2',
              (s, month) => ({
                text: fmtSe(s.se, 1),
                cls: 'seasonals-cell seasonals-cell--se',
                title: `${month} standard error: ${fmtSe(s.se, 1)}; sd ${s.sd === null ? '—' : `${(s.sd * 100).toFixed(1)}%`} (n = ${s.n})`,
              }),
              '',
            )}
            {statRow(
              'Median',
              'Median over complete months',
              (s) => ({ text: fmtPct(s.median, 1), cls: `seasonals-cell${signClass(s.median)}` }),
              '',
            )}
            {statRow(
              '% up',
              'Share of complete months with a positive return',
              (s) => ({ text: fmtShare(s.positivePct), cls: 'seasonals-cell' }),
              '',
            )}
            {statRow(
              'n',
              `Number of complete months in each column; fewer than ${SMALL_N_MONTH} is a small sample`,
              (s, month) => {
                const small = s.n < SMALL_N_MONTH
                return {
                  text: String(s.n),
                  cls: `seasonals-cell seasonals-n${small ? ' seasonals-n--small' : ''}`,
                  title: small
                    ? `${month}: ${s.n} complete months. Small sample (fewer than ${SMALL_N_MONTH}).`
                    : `${month}: ${s.n} complete months`,
                  extra: <SmallSampleNote small={small} />,
                }
              },
              'seasonals-grid__stat--n',
            )}
          </tfoot>
        </table>
      </div>
    </>
  )
}

export function WeekdayTable({ stats }: { stats: readonly ReturnStats[] }) {
  const pooled = pooledMean(stats)
  const scale = weekdayScale(stats, pooled)
  const noneCompared = noneComparable(stats, SMALL_N_WEEKDAY)
  return (
    <>
      {noneCompared && (
        <NotComparedNotice
          text={`Fewer than ${SMALL_N_WEEKDAY} daily returns per weekday: no weekday is compared.`}
        />
      )}
      <div className="seasonals-scroll" tabIndex={0} role="region" aria-label="Returns by weekday">
        <table className="seasonals-week">
          <caption className="visually-hidden">
            Daily close-to-close returns by UTC weekday of the bar’s open time, with the standard
            error of each mean. All days: mean {fmtPct(pooled, 2)}. A weekday’s mean inside its 95%
            t band around {WEEKDAY_REF} is noise; weekdays with fewer than {SMALL_N_WEEKDAY} returns
            are not compared. A mean that stands out is green when above {WEEKDAY_REF} and red when
            below it.
          </caption>
          <thead>
            <tr>
              <th scope="col">Day (UTC)</th>
              <th scope="col" className="seasonals-week__num">
                Mean
              </th>
              <th
                scope="col"
                className="seasonals-week__num"
                title="Standard error of the mean: sample sd (n − 1) / √n"
              >
                ± SE
              </th>
              <th scope="col" className="seasonals-week__num">
                Median
              </th>
              <th scope="col" className="seasonals-week__num">
                % up
              </th>
              <th scope="col" className="seasonals-week__num">
                n
              </th>
              <th scope="col" className="seasonals-week__bar-head">
                <span>Mean daily return, 95% band around the average</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {stats.map((s, i) => {
              const { fill, bandStart, bandWidth, ref } = weekdayBar(s, scale, pooled)
              // Colour follows the deviation from the average, not the mean's sign.
              const dev = deviation(s, pooled, SMALL_N_WEEKDAY)
              const small = s.n < SMALL_N_WEEKDAY
              const day = WEEKDAY_LABELS[i]
              const verdict = noiseVerdict(s, pooled, SMALL_N_WEEKDAY, WEEKDAY_REF, 2)
              return (
                <tr key={day}>
                  <th scope="row">{day}</th>
                  <td
                    className={`seasonals-week__num${
                      dev ? deviationClass(dev) : ' seasonals-week__num--noise'
                    }`}
                    title={`${day} mean: ${fmtPct(s.mean, 2)} ${fmtSe(s.se, 2)} SE, ${bandText(s, pooled, WEEKDAY_REF, 2)}. ${verdict}`}
                  >
                    {fmtPct(s.mean, 2)}
                    {meanNote(s, dev, SMALL_N_WEEKDAY, WEEKDAY_REF)}
                  </td>
                  <td className="seasonals-week__num seasonals-week__num--se">{fmtSe(s.se, 2)}</td>
                  <td className={`seasonals-week__num${signClass(s.median)}`}>
                    {fmtPct(s.median, 2)}
                  </td>
                  <td className="seasonals-week__num">{fmtShare(s.positivePct)}</td>
                  <td
                    className={`seasonals-week__num seasonals-n${small ? ' seasonals-n--small' : ''}`}
                    title={
                      small
                        ? `${day}: ${s.n} daily returns. Small sample (fewer than ${SMALL_N_WEEKDAY}).`
                        : `${day}: ${s.n} daily returns`
                    }
                  >
                    {s.n.toLocaleString('en-US')}
                    <SmallSampleNote small={small} />
                  </td>
                  <td className="seasonals-week__bar-cell" aria-hidden="true" title={verdict}>
                    <span className="seasonals-bar">
                      {bandWidth > 0 && (
                        <span
                          className="seasonals-bar__band"
                          style={{ left: `${bandStart}%`, width: `${bandWidth}%` }}
                        />
                      )}
                      <span className="seasonals-bar__axis" />
                      {ref !== null && (
                        <span className="seasonals-bar__ref" style={{ left: `${ref}%` }} />
                      )}
                      {s.mean !== null && s.mean !== 0 && (
                        // is-up / is-down place the bar on its side of zero; the colour comes
                        // from the deviation (above / below the average) or is grey for noise.
                        <span
                          className={`seasonals-bar__fill${signClass(s.mean)}${
                            dev
                              ? ` seasonals-bar__fill--${dev.sign}`
                              : ' seasonals-bar__fill--noise'
                          }`}
                          style={{ width: `${fill}%` }}
                        />
                      )}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}
