import {
  memo,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { DEFAULT_DISCLAIMER, LEDGER_PAGE_SIZE, type LabClient } from '../data/labApi'
import type { VenueInfo } from '../data/venue'
import type {
  BacktestReport,
  LabFeature,
  LabTrade,
  LedgerDetail,
  LedgerItem,
  PlanError,
} from '../data/labTypes'
import type { Interval } from '../types'
import { EXIT_LABELS } from '../chart/backtestOverlay'
import {
  DOCK_MAX_SHARE,
  DOCK_MIN_HEIGHT,
  EMPTY,
  MAX_HISTORY_BARS,
  METRIC_COLUMNS,
  METRIC_ROWS,
  costsText,
  describeLabError,
  editorErrors,
  featureSnippet,
  fmtDate,
  fmtDateTime,
  fmtElapsed,
  fmtInt,
  fmtPct,
  fmtRatio,
  fmtSignedPct,
  formatMetric,
  historyLoadingText,
  metricsTable,
  pageInfo,
  pageOffset,
  parsePlanText,
  planLabel,
  labIntervalFor,
  planStatus,
  planTextFor,
  runBlockedReason,
  statItems,
  summaryTiles,
  templateIntervalNote,
  tradeFocusable,
  tradeFocusRange,
  unreachableText,
  venueNote,
  walkForwardHeading,
  walkForwardRows,
  type HistoryPlan,
  type TesterAction,
  type TesterState,
} from './backtestView'
import './Backtest.css'

/** How the current report relates to the chart (computed by App). */
export interface BacktestChartLink {
  /** Markers and equity pane are on the chart now. */
  drawn: boolean
  /** The chart shows the report's symbol and interval. */
  matches: boolean
  /** The report's interval is one the chart supports. */
  canSwitch: boolean
  /** Trades whose entry bar is loaded on the chart. */
  tradesOnChart: number
  /** Time of the oldest bar loaded on the chart (UNIX seconds), null before data. */
  firstBarTime: number | null
  /** Older bars needed to show the whole design window (null when not drawn). */
  history: HistoryPlan | null
  historyLoading: boolean
  historyExhausted: boolean
  /** The last history auto-load gave up after repeated failures (offer a retry). */
  historyFailed: boolean
}

export interface BacktestPanelProps {
  client: LabClient
  state: TesterState
  dispatch: Dispatch<TesterAction>
  chartSymbol: string
  chartInterval: Interval
  link: BacktestChartLink
  /** Draw the report; switches the chart to its symbol/interval first when needed. */
  onShowOnChart(): void
  onClearFromChart(): void
  onLoadHistory(): void
  onStopHistory(): void
  /** Scroll/zoom the chart to a time range (UNIX seconds). */
  onFocusRange(from: number, to: number): void
  onClose(): void
  height: number
  onResize(height: number): void
  formatPrice(symbol: string, price: number): string
  /** The chart's market-data venue; null until the server reports it. */
  chartVenue: VenueInfo | null
}

type Tab = 'overview' | 'robustness' | 'trades' | 'ledger'

const VALIDATE_DEBOUNCE_MS = 450
const LAB_RECHECK_MS = 8000

/**
 * Strategy Tester dock (TradingView-style, under the chart): a JSON plan editor validated live
 * against the lab, a Run button, and results (metrics vs benchmarks, DSR/PBO and other
 * robustness statistics, walk-forward folds, trades) plus the trial ledger. Backtests run on the
 * lab's design window only; the holdout is never reachable from here.
 *
 * Memoised: App keeps every prop stable between live ticks, so a tick does not re-render it.
 */
export const BacktestPanel = memo(function BacktestPanel(props: BacktestPanelProps) {
  const {
    client,
    state,
    dispatch,
    chartSymbol,
    chartInterval,
    link,
    onFocusRange,
    onClose,
    height,
    onResize,
    formatPrice,
    chartVenue,
  } = props
  const ids = useId()
  const [tab, setTab] = useState<Tab>('overview')
  const [labVersion, setLabVersion] = useState<string | null>(null)
  const [healthNonce, setHealthNonce] = useState(0)

  const planText = planTextFor(state, chartSymbol, chartInterval)
  const parsed = useMemo(() => parsePlanText(planText), [planText])
  const errors = editorErrors(state, planText, parsed)
  const blocked = runBlockedReason(state, planText, parsed)
  const running = state.run.kind === 'running'

  // Health check on open and on "Retry".
  useEffect(() => {
    const controller = new AbortController()
    client.health(controller.signal).then(
      (h) => {
        setLabVersion(h.version || null)
        dispatch({ type: 'lab-status', status: h.ok ? 'online' : 'offline' })
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        const view = describeLabError(err)
        dispatch({ type: 'lab-status', status: view.kind === 'offline' ? 'offline' : 'online' })
      },
    )
    return () => controller.abort()
  }, [client, dispatch, healthNonce])

  // Debounced validation of the exact text in the editor (skipped once the lab has answered it).
  const v = state.validation
  const settledText = v.kind === 'valid' || v.kind === 'invalid' ? v.text : null
  const plan = parsed.ok ? parsed.plan : null
  useEffect(() => {
    if (plan === null || settledText === planText) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      dispatch({ type: 'validate-start', text: planText })
      client.validate(plan, controller.signal).then(
        (r) => dispatch({ type: 'validate-done', text: planText, ok: r.ok, errors: r.errors }),
        (err: unknown) => {
          if (controller.signal.aborted) return
          dispatch({ type: 'validate-fail', text: planText, error: describeLabError(err) })
        },
      )
    }, VALIDATE_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [client, dispatch, plan, planText, settledText, healthNonce])

  // While the lab is down, look for it again every few seconds so Run unlocks by itself.
  const offline = state.lab === 'offline'
  useEffect(() => {
    if (!offline) return
    const id = window.setInterval(() => setHealthNonce((n) => n + 1), LAB_RECHECK_MS)
    return () => window.clearInterval(id)
  }, [offline])

  const run = useCallback(() => {
    if (blocked !== null || plan === null) return
    const id = state.runSeq
    const text = planText
    dispatch({ type: 'run-start', startedAt: Date.now() })
    // Not aborted on close: the run finishes and its report waits in App state.
    void client.backtest(plan).then(
      (report) => {
        dispatch({ type: 'run-done', id, report })
        setTab((t) => (t === 'ledger' ? 'overview' : t))
      },
      (err: unknown) => dispatch({ type: 'run-fail', id, text, error: describeLabError(err) }),
    )
  }, [blocked, client, dispatch, plan, planText, state.runSeq])

  const retryLab = () => setHealthNonce((n) => n + 1)

  const report = state.report
  const windowEnd = report?.window.end ?? null
  const reportSymbol = report?.symbol ?? chartSymbol
  const focusTrade = useCallback(
    (t: LabTrade) => {
      if (windowEnd === null) return
      const { from, to } = tradeFocusRange(t, windowEnd)
      onFocusRange(from, to)
    },
    [windowEnd, onFocusRange],
  )
  const formatReportPrice = useCallback(
    (p: number) => formatPrice(reportSymbol, p),
    [formatPrice, reportSymbol],
  )
  const tabs: readonly { id: Tab; label: string; count?: number }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'robustness', label: 'Robustness' },
    { id: 'trades', label: 'List of trades', count: report?.trades.length },
    { id: 'ledger', label: 'Ledger' },
  ]
  const tabId = (t: Tab) => `${ids}-tab-${t}`
  const panelId = `${ids}-tabpanel`

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const order = tabs.map((t) => t.id)
    const i = order.indexOf(tab)
    const next =
      e.key === 'Home'
        ? order[0]
        : e.key === 'End'
          ? order[order.length - 1]
          : order[(i + (e.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length]
    setTab(next)
    document.getElementById(tabId(next))?.focus()
  }

  return (
    <section className="tester" aria-labelledby={`${ids}-title`}>
      <ResizeHandle height={height} onResize={onResize} />
      <header className="tester__head">
        <span className="tester__head-icon" aria-hidden="true">
          <TesterIcon />
        </span>
        <h2 id={`${ids}-title`} className="tester__title">
          Strategy Tester
        </h2>
        <LabStatusChip status={state.lab} version={labVersion} />
        <div className="tester__tabs" role="tablist" aria-label="Strategy Tester view">
          {tabs.map((t) => (
            <button
              key={t.id}
              id={tabId(t.id)}
              type="button"
              role="tab"
              className="tester__tab"
              aria-selected={tab === t.id}
              aria-controls={tab === t.id ? panelId : undefined}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={onTabKeyDown}
            >
              {t.label}
              {t.count !== undefined && t.count > 0 && (
                <span className="tester__tab-count">{fmtInt(t.count)}</span>
              )}
            </button>
          ))}
        </div>
        <span className="tester__spacer" />
        <button
          type="button"
          className="tester__icon-btn"
          aria-label="Close Strategy Tester"
          title="Close"
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </header>

      <div className="tester__body">
        <PlanEditor
          client={client}
          state={state}
          dispatch={dispatch}
          planText={planText}
          errors={errors}
          parsedOk={parsed.ok}
          chartSymbol={chartSymbol}
          chartInterval={chartInterval}
          blocked={blocked}
          onRun={run}
        />

        <div
          id={panelId}
          className="tester__results"
          role="tabpanel"
          aria-labelledby={tabId(tab)}
          aria-busy={running}
        >
          {state.lab === 'offline' && <OfflineBanner onRetry={retryLab} />}
          {state.run.kind === 'error' && state.run.error.kind !== 'offline' && (
            <RunErrorBanner
              title={state.run.error.title}
              message={state.run.error.message}
              tone={state.run.error.kind === 'no-data' ? 'warn' : 'bad'}
              onDismiss={() => dispatch({ type: 'dismiss-error' })}
            />
          )}
          {tab === 'ledger' ? (
            <LedgerView
              client={client}
              refreshKey={report ? `${report.run_id}#${report.trial_id}` : ''}
              labOnline={state.lab !== 'offline'}
              onLoadPlan={(text) => dispatch({ type: 'edit', text })}
            />
          ) : report === null ? (
            <EmptyResults running={running} />
          ) : (
            <>
              <ReportBar report={report} {...props} />
              {tab === 'overview' && <OverviewTab report={report} chartVenue={chartVenue} />}
              {tab === 'robustness' && <RobustnessTab report={report} />}
              {tab === 'trades' && (
                <TradesTab
                  report={report}
                  drawn={link.drawn}
                  firstBarTime={link.firstBarTime}
                  onFocus={focusTrade}
                  formatPrice={formatReportPrice}
                />
              )}
            </>
          )}
        </div>
      </div>

      <footer className="tester__foot">
        <InfoIcon />
        <span className="tester__disclaimer">{report?.disclaimer ?? DEFAULT_DISCLAIMER}</span>
        <span className="tester__foot-note">
          Design window only · the locked holdout is never used here
        </span>
      </footer>
    </section>
  )
})

// ---------------------------------------------------------------------------------------------
// Resize handle
// ---------------------------------------------------------------------------------------------

function ResizeHandle({ height, onResize }: { height: number; onResize(h: number): void }) {
  const stopRef = useRef<(() => void) | null>(null)
  useEffect(() => () => stopRef.current?.(), [])

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    const startY = e.clientY
    const startH = height
    const move = (ev: PointerEvent) => onResize(startH + (startY - ev.clientY))
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
      document.body.classList.remove('tester-resizing')
      stopRef.current = null
    }
    stopRef.current?.()
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    document.body.classList.add('tester-resizing')
    stopRef.current = stop
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    e.preventDefault()
    onResize(height + (e.key === 'ArrowUp' ? 24 : -24))
  }

  // Same bounds as clampDockHeight.
  const max = Math.max(DOCK_MIN_HEIGHT, Math.floor(window.innerHeight * DOCK_MAX_SHARE))
  return (
    <div
      className="tester__resize"
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize Strategy Tester"
      aria-valuemin={DOCK_MIN_HEIGHT}
      aria-valuemax={max}
      aria-valuenow={Math.round(height)}
      tabIndex={0}
      title="Drag to resize"
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
    />
  )
}

// ---------------------------------------------------------------------------------------------
// Plan editor
// ---------------------------------------------------------------------------------------------

interface PlanEditorProps {
  client: LabClient
  state: TesterState
  dispatch: Dispatch<TesterAction>
  planText: string
  errors: PlanError[]
  parsedOk: boolean
  chartSymbol: string
  chartInterval: Interval
  blocked: string | null
  onRun(): void
}

function PlanEditor({
  client,
  state,
  dispatch,
  planText,
  errors,
  parsedOk,
  chartSymbol,
  chartInterval,
  blocked,
  onRun,
}: PlanEditorProps) {
  const ids = useId()
  const running = state.run.kind === 'running'
  const status = planStatus(state, planText, parsedOk, errors.length)
  const intervalNote = templateIntervalNote(chartInterval)

  const format = () => {
    try {
      dispatch({ type: 'edit', text: JSON.stringify(JSON.parse(planText), null, 2) })
    } catch {
      // Invalid JSON stays as typed; the error list already says why.
    }
  }

  return (
    <div className="tester__editor">
      <div className="tester__editor-bar">
        <label htmlFor={`${ids}-plan`} className="tester__label">
          Plan
        </label>
        <span className={`tester__chip tester__chip--${status.tone}`}>{status.text}</span>
        {/* Announces only the lab's settled verdict, not every "Checking…" while typing. */}
        <span className="visually-hidden" aria-live="polite" aria-atomic="true">
          {status.announce}
        </span>
        <span className="tester__spacer" />
        <button
          type="button"
          className="tester__text-btn"
          title="Pretty-print the JSON"
          disabled={!parsedOk}
          onClick={format}
        >
          Format
        </button>
        <button
          type="button"
          className="tester__text-btn"
          title={`EMA 20/50 crossover for ${chartSymbol} ${labIntervalFor(chartInterval)}`}
          disabled={!state.planEdited}
          onClick={() => dispatch({ type: 'reset-template' })}
        >
          Reset
        </button>
      </div>
      <textarea
        id={`${ids}-plan`}
        className="tester__plan"
        value={planText}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        aria-invalid={errors.length > 0}
        aria-describedby={errors.length > 0 ? `${ids}-errors` : `${ids}-hint`}
        onChange={(e) => dispatch({ type: 'edit', text: e.target.value })}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            onRun()
          }
        }}
      />
      {errors.length > 0 ? (
        <ul id={`${ids}-errors`} className="tester__errors" aria-label="Plan errors">
          {errors.map((err, i) => (
            <li key={`${err.path}-${i}`} className="tester__error">
              {err.path !== '' && <code className="tester__error-path">{err.path}</code>}
              <span>{err.message}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p id={`${ids}-hint`} className="tester__hint">
          {state.planEdited
            ? 'Your plan. Reset brings back the EMA crossover template.'
            : intervalNote !== null
              ? `Template: EMA 20/50 crossover on ${chartSymbol}. ${intervalNote}`
              : `Template: EMA 20/50 crossover on ${chartSymbol} ${chartInterval} (follows the chart until you edit it).`}
        </p>
      )}
      <FeatureReference client={client} labOnline={state.lab === 'online'} />
      <div className="tester__run-row">
        <button
          type="button"
          className="tester__run"
          disabled={blocked !== null}
          title={blocked ?? 'Run on the design window (Ctrl/Cmd+Enter)'}
          aria-keyshortcuts="Control+Enter Meta+Enter"
          onClick={onRun}
        >
          {state.run.kind === 'running' ? (
            <>
              <span className="tester__spinner" aria-hidden="true" />
              Running… <Elapsed startedAt={state.run.startedAt} />
            </>
          ) : (
            <>
              <PlayIcon />
              Run backtest
            </>
          )}
        </button>
        {!running && state.report && (
          <span className="tester__run-meta">
            Last run {fmtElapsed(state.report.elapsed_ms)} · trial #{state.report.trial_id}
          </span>
        )}
      </div>
    </div>
  )
}

function Elapsed({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(id)
  }, [])
  return <span className="tester__elapsed">{fmtElapsed(Math.max(0, now - startedAt))}</span>
}

type RegistryState =
  | { kind: 'loading' }
  | { kind: 'ready'; features: LabFeature[] }
  | { kind: 'error'; message: string }

function FeatureReference({ client, labOnline }: { client: LabClient; labOnline: boolean }) {
  const [registry, setRegistry] = useState<RegistryState>({ kind: 'loading' })
  const loaded = registry.kind === 'ready'
  useEffect(() => {
    if (loaded || !labOnline) return
    const controller = new AbortController()
    client.registry(controller.signal).then(
      (r) => setRegistry({ kind: 'ready', features: r.features }),
      (err: unknown) => {
        if (controller.signal.aborted) return
        setRegistry({ kind: 'error', message: describeLabError(err).message })
      },
    )
    return () => controller.abort()
  }, [client, labOnline, loaded])

  const count = registry.kind === 'ready' ? registry.features.length : null
  return (
    <details className="tester__ref">
      <summary className="tester__ref-summary">
        <ChevronIcon />
        Feature reference
        {count !== null && <span className="tester__tab-count">{count}</span>}
      </summary>
      <div className="tester__ref-body">
        {registry.kind === 'ready' ? (
          registry.features.length === 0 ? (
            <p className="tester__hint">The lab registry has no features.</p>
          ) : (
            <ul className="tester__features">
              {registry.features.map((f) => (
                <li key={f.name} className="tester__feature">
                  <div className="tester__feature-head">
                    <code className="tester__feature-name">{f.name}</code>
                    {f.outputs.length > 0 && (
                      <span className="tester__feature-outputs">→ {f.outputs.join(', ')}</span>
                    )}
                  </div>
                  {f.description && <p className="tester__feature-desc">{f.description}</p>}
                  {f.params.length > 0 && (
                    <ul className="tester__params">
                      {f.params.map((p) => (
                        <li key={p.name} title={p.description}>
                          <code>{p.name}</code>
                          <span className="tester__param-meta">
                            {p.kind} · default {p.default} · {p.low}–{p.high}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <code className="tester__snippet">{featureSnippet(f)}</code>
                </li>
              ))}
            </ul>
          )
        ) : registry.kind === 'error' ? (
          <p className="tester__hint">{registry.message}</p>
        ) : (
          <p className="tester__hint">
            {labOnline ? 'Loading features…' : 'Available when the lab service is running.'}
          </p>
        )}
        <p className="tester__hint">
          Rules combine feature names with <code>and</code>, <code>or</code>, comparisons and{' '}
          <code>crosses_above(a, b)</code> / <code>crosses_below(a, b)</code>. Signals use the bar
          close; fills happen at the next bar&apos;s open.
        </p>
      </div>
    </details>
  )
}

// ---------------------------------------------------------------------------------------------
// Status banners
// ---------------------------------------------------------------------------------------------

/** Renders `code` spans for text between backticks (server messages name CLI commands). */
function RichText({ text }: { text: string }) {
  const parts = text.split('`')
  return <>{parts.map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : part))}</>
}

function LabStatusChip({
  status,
  version,
}: {
  status: TesterState['lab']
  version: string | null
}) {
  const label =
    status === 'online'
      ? `Lab online${version ? ` · v${version}` : ''}`
      : status === 'offline'
        ? 'Lab offline'
        : 'Connecting to lab…'
  return (
    <span className={`tester__lab tester__lab--${status}`} title={label}>
      <span className="tester__lab-dot" aria-hidden="true" />
      <span className="tester__lab-text">{label}</span>
    </span>
  )
}

function OfflineBanner({ onRetry }: { onRetry(): void }) {
  return (
    <div className="tester__banner tester__banner--bad" role="alert">
      <ServerIcon />
      <div className="tester__banner-main">
        <p className="tester__banner-title">Lab service not running</p>
        <p className="tester__banner-text">
          Lab service not running — start it with <code>npm run dev</code> at the repo root (it
          listens on 127.0.0.1:8788).
        </p>
      </div>
      <button type="button" className="tester__banner-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

function RunErrorBanner({
  title,
  message,
  tone,
  onDismiss,
}: {
  title: string
  message: string
  tone: 'warn' | 'bad'
  onDismiss(): void
}) {
  return (
    <div className={`tester__banner tester__banner--${tone}`} role="alert">
      <WarnIcon />
      <div className="tester__banner-main">
        <p className="tester__banner-title">{title}</p>
        <p className="tester__banner-text">
          <RichText text={message} />
        </p>
      </div>
      <button
        type="button"
        className="tester__icon-btn"
        aria-label="Dismiss"
        title="Dismiss"
        onClick={onDismiss}
      >
        <CloseIcon />
      </button>
    </div>
  )
}

function EmptyResults({ running }: { running: boolean }) {
  return (
    <div className="tester__empty">
      <span className="tester__empty-icon" aria-hidden="true">
        <TesterIcon size={28} />
      </span>
      <p className="tester__empty-title">{running ? 'Running the backtest…' : 'No results yet'}</p>
      <p className="tester__empty-text">
        {running
          ? 'The lab simulates the plan with fees and slippage, then the benchmarks, random-entry paths and overfitting checks.'
          : 'Edit the plan on the left and press Run backtest. Trades appear on the chart, equity in a pane below it, and every run is recorded in the ledger.'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Report header (market, window, chart controls)
// ---------------------------------------------------------------------------------------------

function ReportBar({
  report,
  state,
  link,
  onShowOnChart,
  onClearFromChart,
  onLoadHistory,
  onStopHistory,
  onFocusRange,
}: BacktestPanelProps & { report: BacktestReport }) {
  const { name } = planLabel(report.plan)
  const market = `${report.symbol} ${report.interval}`
  const h = link.history
  return (
    <div className="tester__report-bar">
      <div className="tester__report-id">
        <span className="tester__report-name" title={name || undefined}>
          {name || 'Unnamed plan'}
        </span>
        <span className="tester__report-meta">
          {market} · design window {fmtDate(report.window.start)} → {fmtDate(report.window.end)}
        </span>
      </div>
      <span className="tester__spacer" />
      <div className="tester__chart-ctl" role="group" aria-label="Chart overlay">
        {link.drawn ? (
          <>
            <span className="tester__chart-note">
              <span className="tester__dot tester__dot--on" aria-hidden="true" />
              {fmtInt(link.tradesOnChart)} of {fmtInt(report.trades.length)} trades on chart
            </span>
            {h?.needed &&
              (h.unreachable ? (
                <span
                  className="tester__chart-note tester__chart-note--muted tester__chart-note--wrap"
                  title="Older bars at this interval would exceed what the chart can hold. A higher interval shows the whole window."
                >
                  {unreachableText(report.window.end)}
                </span>
              ) : link.historyLoading ? (
                <>
                  <span
                    className="tester__chart-note tester__chart-note--wrap"
                    role="status"
                    title={`About ${fmtInt(h.bars)} older bars left to load`}
                  >
                    <span className="tester__spinner tester__spinner--small" aria-hidden="true" />
                    {historyLoadingText(report.window, link.tradesOnChart, report.trades.length)}
                  </span>
                  <button
                    type="button"
                    className="tester__btn"
                    title="Stop loading older bars"
                    onClick={onStopHistory}
                  >
                    Stop
                  </button>
                </>
              ) : link.historyFailed ? (
                <>
                  <span className="tester__chart-note tester__chart-note--warn" role="status">
                    History load failed
                  </span>
                  <button
                    type="button"
                    className="tester__btn"
                    title="Try loading the older bars again"
                    onClick={onLoadHistory}
                  >
                    Retry
                  </button>
                </>
              ) : h.partial && h.bars === 0 ? (
                <span className="tester__chart-note tester__chart-note--muted">
                  Chart holds at most {fmtInt(MAX_HISTORY_BARS)} bars
                </span>
              ) : link.historyExhausted ? (
                <span className="tester__chart-note tester__chart-note--muted">
                  Exchange history starts later than the window
                </span>
              ) : (
                <button
                  type="button"
                  className="tester__btn"
                  title={
                    h.partial
                      ? `The window is longer than the chart can hold; loads the most recent ${fmtInt(h.bars)} bars`
                      : `Loads ~${fmtInt(h.bars)} older bars so every trade is on the chart`
                  }
                  onClick={onLoadHistory}
                >
                  Load full history
                </button>
              ))}
            <button
              type="button"
              className="tester__btn"
              title="Zoom the chart to the design window"
              onClick={() => onFocusRange(report.window.start, report.window.end)}
            >
              Zoom to window
            </button>
            <button type="button" className="tester__btn" onClick={onClearFromChart}>
              Clear from chart
            </button>
          </>
        ) : (
          <>
            {state.shownOnChart && !link.matches && (
              <span className="tester__chart-note tester__chart-note--muted">
                Chart shows another market
              </span>
            )}
            <button
              type="button"
              className="tester__btn tester__btn--accent"
              disabled={!link.matches && !link.canSwitch}
              title={
                link.matches
                  ? 'Draw trades and equity on the chart'
                  : link.canSwitch
                    ? `Switch the chart to ${market} and draw the trades`
                    : `The chart has no ${report.interval} interval`
              }
              onClick={onShowOnChart}
            >
              {link.matches ? 'Show on chart' : `Show on chart (${market})`}
            </button>
          </>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------------------------

const OverviewTab = memo(function OverviewTab({
  report,
  chartVenue,
}: {
  report: BacktestReport
  chartVenue: VenueInfo | null
}) {
  const tiles = summaryTiles(report)
  const rows = metricsTable(report)
  return (
    <div className="tester__overview">
      <div className="tester__tiles">
        {tiles.map((t) => (
          <div key={t.label} className="tester__tile">
            <span className="tester__tile-label">{t.label}</span>
            <span className={`tester__tile-value tester__tone--${t.tone}`}>{t.value}</span>
            {t.sub && <span className="tester__tile-sub">{t.sub}</span>}
          </div>
        ))}
      </div>

      {report.warnings.length > 0 && (
        <ul className="tester__warnings" aria-label="Warnings">
          {report.warnings.map((w, i) => (
            <li key={i}>
              <WarnIcon />
              <span>
                <RichText text={w} />
              </span>
            </li>
          ))}
        </ul>
      )}

      <div className="tester__table-wrap">
        <table className="tester__table">
          <caption className="visually-hidden">
            Strategy metrics against buy-and-hold and vol-targeted buy-and-hold
          </caption>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              {METRIC_COLUMNS.map((c) => (
                <th key={c.id} scope="col" className="tester__num">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <th scope="row" title={row.hint}>
                  {row.label}
                </th>
                {row.cells.map((cell, i) => (
                  <td
                    key={i}
                    className={`tester__num${
                      i === 0 && row.compare ? ` tester__cmp--${row.compare}` : ''
                    }`}
                    title={
                      i === 0 && row.compare
                        ? row.compare === 'better'
                          ? 'Better than buy & hold'
                          : 'Worse than buy & hold'
                        : undefined
                    }
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="tester__costs">
        <span className="tester__costs-label">Costs used</span> {costsText(report.costs)}
      </p>
      <p className="tester__costs">
        <span className="tester__costs-label">Data</span> {venueNote(chartVenue)}
      </p>
    </div>
  )
})

// ---------------------------------------------------------------------------------------------
// Robustness: DSR, PSR, PBO, Sharpe CI, random entry, walk-forward
// ---------------------------------------------------------------------------------------------

const RobustnessTab = memo(function RobustnessTab({ report }: { report: BacktestReport }) {
  const items = statItems(report)
  const folds = walkForwardRows(report.walk_forward)
  const foldHeading = walkForwardHeading(report)
  const random = report.benchmarks.random_entry
  return (
    <div className="tester__robust">
      <ul className="tester__stats" aria-label="Robustness statistics">
        {items.map((s) => (
          <li key={s.id} className={`tester__stat tester__stat--${s.tone}`}>
            <span className="tester__stat-label">{s.label}</span>
            <span className={`tester__stat-value tester__tone--${s.tone}`}>{s.value}</span>
            <span className="tester__stat-hint">{s.hint}</span>
          </li>
        ))}
      </ul>
      <p className="tester__hint tester__hint--block">
        Random-entry null: median Sharpe {fmtRatio(random.sharpe_median)}, 95th percentile{' '}
        {fmtRatio(random.sharpe_p95)} over {fmtInt(random.n_paths)} paths. DSR hurdle Sharpe{' '}
        {fmtRatio(report.stats.dsr.sr0_annual)} (annualised) for {fmtInt(report.stats.dsr.n_trials)}{' '}
        trials.
      </p>

      <h3 className="tester__section-title">{foldHeading.title}</h3>
      {foldHeading.hint !== null && folds.length > 0 && (
        <p className="tester__hint">{foldHeading.hint}</p>
      )}
      {folds.length === 0 ? (
        <p className="tester__hint">The design window is too short for walk-forward folds.</p>
      ) : (
        <div className="tester__table-wrap">
          <table className="tester__table tester__table--compact">
            <thead>
              <tr>
                <th scope="col">Fold</th>
                <th scope="col">Test period</th>
                <th scope="col" className="tester__num">
                  Sharpe
                </th>
                <th scope="col" className="tester__num">
                  Return
                </th>
                <th scope="col" className="tester__num">
                  Trades
                </th>
              </tr>
            </thead>
            <tbody>
              {folds.map((f) => (
                <tr key={f.fold}>
                  <th scope="row">{f.fold}</th>
                  <td>{f.period}</td>
                  <td className="tester__num">{f.sharpe}</td>
                  <td className={`tester__num tester__tone--${f.tone}`}>{f.totalReturn}</td>
                  <td className="tester__num">{f.trades}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
})

// ---------------------------------------------------------------------------------------------
// Trades
// ---------------------------------------------------------------------------------------------

const TradesTab = memo(function TradesTab({
  report,
  drawn,
  firstBarTime,
  onFocus,
  formatPrice,
}: {
  report: BacktestReport
  /** The report is drawn on the chart. */
  drawn: boolean
  /** Oldest loaded chart bar; a trade before it cannot be shown. */
  firstBarTime: number | null
  onFocus(t: LabTrade): void
  formatPrice(price: number): string
}) {
  if (report.trades.length === 0) {
    return (
      <div className="tester__empty">
        <p className="tester__empty-title">No trades</p>
        <p className="tester__empty-text">The entry rule never fired in the design window.</p>
      </div>
    )
  }
  return (
    <div className="tester__table-wrap tester__table-wrap--fill">
      <table className="tester__table tester__table--compact">
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Entry (UTC)</th>
            <th scope="col" className="tester__num">
              Entry price
            </th>
            <th scope="col">Exit (UTC)</th>
            <th scope="col" className="tester__num">
              Exit price
            </th>
            <th scope="col">Reason</th>
            <th scope="col" className="tester__num">
              Return
            </th>
          </tr>
        </thead>
        <tbody>
          {report.trades.map((t, i) => {
            const focusable = tradeFocusable(t, drawn, firstBarTime)
            return (
              <tr
                key={`${t.entry_time}-${i}`}
                className={focusable ? 'tester__row--link' : undefined}
                tabIndex={focusable ? 0 : undefined}
                onClick={focusable ? () => onFocus(t) : undefined}
                onKeyDown={
                  focusable
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onFocus(t)
                        }
                      }
                    : undefined
                }
                title={
                  focusable
                    ? 'Show this trade on the chart'
                    : drawn
                      ? 'This trade is older than the bars on the chart (Load full history)'
                      : undefined
                }
              >
                <th scope="row">{i + 1}</th>
                <td>{fmtDateTime(t.entry_time)}</td>
                <td className="tester__num">{formatPrice(t.entry_price)}</td>
                <td>{t.exit_time === null ? 'Open' : fmtDateTime(t.exit_time)}</td>
                <td className="tester__num">
                  {t.exit_price === null ? EMPTY : formatPrice(t.exit_price)}
                </td>
                <td>
                  <span className={`tester__reason tester__reason--${t.exit_reason}`}>
                    {EXIT_LABELS[t.exit_reason]}
                  </span>
                </td>
                <td className={`tester__num tester__tone--${t.return_pct >= 0 ? 'good' : 'bad'}`}>
                  {fmtSignedPct(t.return_pct)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
})

// ---------------------------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------------------------

type PageResult =
  | { key: string; kind: 'ready'; total: number; items: LedgerItem[] }
  | { key: string; kind: 'error'; message: string }

type DetailResult =
  | { id: number; kind: 'ready'; detail: LedgerDetail }
  | { id: number; kind: 'error'; message: string }

function LedgerView({
  client,
  refreshKey,
  labOnline,
  onLoadPlan,
}: {
  client: LabClient
  /** Changes after each run so the newest entry shows up. */
  refreshKey: string
  labOnline: boolean
  onLoadPlan(text: string): void
}) {
  const ids = useId()
  const [filterInput, setFilterInput] = useState('')
  const [filter, setFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [nonce, setNonce] = useState(0)
  const [page, setPage] = useState<PageResult | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<DetailResult | null>(null)

  const key = `${filter}|${offset}|${refreshKey}|${nonce}`
  useEffect(() => {
    if (!labOnline) return
    const controller = new AbortController()
    client.ledger({ runId: filter, limit: LEDGER_PAGE_SIZE, offset }, controller.signal).then(
      (p) => {
        // Newest first, whatever order the page arrives in.
        const items = [...p.items].sort((a, b) => b.created_at - a.created_at || b.id - a.id)
        setPage({ key, kind: 'ready', total: p.total, items })
      },
      (err: unknown) => {
        if (controller.signal.aborted) return
        setPage({ key, kind: 'error', message: describeLabError(err).message })
      },
    )
    return () => controller.abort()
  }, [client, filter, offset, key, labOnline])

  useEffect(() => {
    if (selected === null) return
    const controller = new AbortController()
    client.ledgerItem(selected, controller.signal).then(
      (d) => setDetail({ id: selected, kind: 'ready', detail: d }),
      (err: unknown) => {
        if (controller.signal.aborted) return
        setDetail({ id: selected, kind: 'error', message: describeLabError(err).message })
      },
    )
    return () => controller.abort()
  }, [client, selected])

  if (selected !== null) {
    const current = detail?.id === selected ? detail : null
    return (
      <LedgerDetailView result={current} onBack={() => setSelected(null)} onLoadPlan={onLoadPlan} />
    )
  }

  const current = page?.key === key ? page : null
  const loading = labOnline && current === null
  const info =
    current?.kind === 'ready' ? pageInfo(current.total, offset, current.items.length) : null

  return (
    <div className="tester__ledger">
      <form
        className="tester__ledger-bar"
        role="search"
        onSubmit={(e) => {
          e.preventDefault()
          setFilter(filterInput.trim())
          setOffset(0)
        }}
      >
        <label htmlFor={`${ids}-run`} className="tester__label">
          Run
        </label>
        <input
          id={`${ids}-run`}
          className="tester__input"
          placeholder="All runs (filter by run id)"
          value={filterInput}
          spellCheck={false}
          onChange={(e) => setFilterInput(e.target.value)}
        />
        <button type="submit" className="tester__btn">
          Filter
        </button>
        {filter !== '' && (
          <button
            type="button"
            className="tester__text-btn"
            onClick={() => {
              setFilter('')
              setFilterInput('')
              setOffset(0)
            }}
          >
            Clear
          </button>
        )}
        <span className="tester__spacer" />
        <button
          type="button"
          className="tester__icon-btn"
          aria-label="Refresh ledger"
          title="Refresh"
          disabled={!labOnline}
          onClick={() => setNonce((n) => n + 1)}
        >
          <RefreshIcon />
        </button>
      </form>

      {!labOnline ? (
        <p className="tester__hint tester__hint--block">The ledger lives in the lab service.</p>
      ) : current?.kind === 'error' ? (
        <p className="tester__hint tester__hint--block tester__tone--bad">{current.message}</p>
      ) : (
        <div className="tester__table-wrap tester__table-wrap--fill" aria-busy={loading}>
          <table className="tester__table tester__table--compact">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">When (UTC)</th>
                <th scope="col">Plan</th>
                <th scope="col">Market</th>
                <th scope="col">Run</th>
                <th scope="col" className="tester__num">
                  Sharpe
                </th>
                <th scope="col" className="tester__num">
                  Return
                </th>
                <th scope="col" className="tester__num">
                  Max DD
                </th>
                <th scope="col" className="tester__num">
                  Trades
                </th>
              </tr>
            </thead>
            <tbody>
              {current?.kind === 'ready' && current.items.length === 0 && (
                <tr>
                  <td colSpan={9} className="tester__table-empty">
                    {filter ? `No runs with id “${filter}”.` : 'No runs recorded yet.'}
                  </td>
                </tr>
              )}
              {loading && (
                <tr>
                  <td colSpan={9} className="tester__table-empty">
                    Loading the ledger…
                  </td>
                </tr>
              )}
              {current?.kind === 'ready' &&
                current.items.map((item) => (
                  <tr
                    key={item.id}
                    className="tester__row--link"
                    tabIndex={0}
                    onClick={() => setSelected(item.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        setSelected(item.id)
                      }
                    }}
                  >
                    <th scope="row">{item.id}</th>
                    <td>{fmtDateTime(item.created_at)}</td>
                    <td className="tester__cell-name" title={item.plan_name}>
                      {item.status === 'error' && (
                        <span className="tester__reason tester__reason--stop_loss">Error</span>
                      )}{' '}
                      {item.plan_name || item.plan_hash.slice(0, 10) || EMPTY}
                    </td>
                    <td>
                      {item.symbol} {item.interval}
                    </td>
                    <td className="tester__cell-run" title={`${item.source} · ${item.window_kind}`}>
                      {item.run_id || EMPTY}
                    </td>
                    <td className="tester__num">{fmtRatio(item.sharpe)}</td>
                    <td
                      className={`tester__num tester__tone--${
                        item.total_return === null
                          ? 'neutral'
                          : item.total_return >= 0
                            ? 'good'
                            : 'bad'
                      }`}
                    >
                      {fmtSignedPct(item.total_return)}
                    </td>
                    <td className="tester__num">{fmtPct(item.max_drawdown)}</td>
                    <td className="tester__num">{fmtInt(item.n_trades)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}

      {info && (info.hasPrev || info.hasNext || info.to > 0) && (
        <div className="tester__pager">
          <span className="tester__pager-label">{info.label}</span>
          <button
            type="button"
            className="tester__btn"
            disabled={!info.hasPrev}
            onClick={() => setOffset((o) => pageOffset(o, LEDGER_PAGE_SIZE, -1, 0))}
          >
            Newer
          </button>
          <button
            type="button"
            className="tester__btn"
            disabled={!info.hasNext}
            onClick={() =>
              setOffset((o) =>
                pageOffset(o, LEDGER_PAGE_SIZE, 1, current?.kind === 'ready' ? current.total : 0),
              )
            }
          >
            Older
          </button>
        </div>
      )}
    </div>
  )
}

function LedgerDetailView({
  result,
  onBack,
  onLoadPlan,
}: {
  result: DetailResult | null
  onBack(): void
  onLoadPlan(text: string): void
}) {
  const backRef = useRef<HTMLButtonElement>(null)
  useEffect(() => backRef.current?.focus(), [])
  const d = result?.kind === 'ready' ? result.detail : null
  const planJson = d ? JSON.stringify(d.plan, null, 2) : ''
  return (
    <div className="tester__ledger-detail">
      <div className="tester__ledger-bar">
        <button ref={backRef} type="button" className="tester__btn" onClick={onBack}>
          ← Ledger
        </button>
        {d && (
          <span className="tester__report-meta">
            #{d.id} · {d.plan_name || d.plan_hash.slice(0, 10)} · {d.symbol} {d.interval} ·{' '}
            {d.window_kind} · {fmtDateTime(d.created_at)} UTC · {d.source}
          </span>
        )}
        <span className="tester__spacer" />
        {d && (
          <button
            type="button"
            className="tester__btn"
            title="Replace the editor's plan with this one"
            onClick={() => onLoadPlan(planJson)}
          >
            Load plan into editor
          </button>
        )}
      </div>
      {result === null ? (
        <p className="tester__hint tester__hint--block">Loading the entry…</p>
      ) : result.kind === 'error' ? (
        <p className="tester__hint tester__hint--block tester__tone--bad">{result.message}</p>
      ) : (
        d && (
          <div className="tester__detail-grid">
            <div>
              <h3 className="tester__section-title">Metrics</h3>
              {d.status === 'error' && (
                <p className="tester__hint tester__tone--bad">{d.error ?? 'The run failed'}</p>
              )}
              {d.metrics ? (
                <table className="tester__table tester__table--compact">
                  <tbody>
                    {METRIC_ROWS.map((row) => (
                      <tr key={row.key}>
                        <th scope="row" title={row.hint}>
                          {row.label}
                        </th>
                        <td className="tester__num">
                          {formatMetric(row.kind, d.metrics ? d.metrics[row.key] : null)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="tester__hint">No metrics recorded.</p>
              )}
            </div>
            <div className="tester__detail-plan">
              <h3 className="tester__section-title">Plan · {d.plan_hash.slice(0, 12)}</h3>
              <pre className="tester__pre">{planJson}</pre>
            </div>
          </div>
        )
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------------------------

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

export function TesterIcon({ size = 16 }: { size?: number }): ReactNode {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" {...STROKE}>
      <path d="M3.5 17.5h15M5 14.5l3.5-4 3 2.5 5-6.5" />
      <path d="M14 6.5h2.5V9" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" />
    </svg>
  )
}

function PlayIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 14 14" {...STROKE} fill="currentColor">
      <path d="M4.5 3v8l6.5-4z" />
    </svg>
  )
}

function ChevronIcon() {
  return (
    <svg className="tester__chevron" width="10" height="10" viewBox="0 0 10 10" {...STROKE}>
      <path d="M3.5 2l3 3-3 3" />
    </svg>
  )
}

function InfoIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" {...STROKE}>
      <circle cx="7" cy="7" r="5.5" />
      <path d="M7 6.3v3.4M7 4.4h.01" />
    </svg>
  )
}

function WarnIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M7 1.8l5.6 10H1.4zM7 5.6v2.8M7 10.2h.01" />
    </svg>
  )
}

function ServerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 28 28" {...STROKE}>
      <rect x="5" y="5" width="18" height="7" rx="1.5" />
      <rect x="5" y="16" width="18" height="7" rx="1.5" />
      <path d="M9 8.5h.01M9 19.5h.01" strokeWidth="2" />
    </svg>
  )
}

function RefreshIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M11.5 6.5A4.5 4.5 0 1 0 10 10M11.5 3v3.5H8" />
    </svg>
  )
}
