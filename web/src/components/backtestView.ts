// Pure logic of the Strategy Tester panel: the plan template, formatting of lab metrics and
// statistics, the panel state reducer, history-load planning, and ledger paging.

import { LAB_OFFLINE_MESSAGE, LabError } from '../data/labApi'
import { ARCHIVE_VENUE_ID } from '../data/venue'
import type {
  BacktestReport,
  LabFeature,
  LabMetrics,
  LabPlan,
  LabTrade,
  PlanError,
  WalkForwardFold,
} from '../data/labTypes'
import { isNativeInterval, timeframeSeconds } from '../data/timeframes'
import { CHART_MAX_BARS } from '../chart/conflation'
import { INTERVALS, type Interval, type Timeframe } from '../types'

export const EMPTY = '—'

// ---------------------------------------------------------------------------------------------
// Plan text
// ---------------------------------------------------------------------------------------------

/**
 * The lab serves the native intervals only (1m … 1w; 1w via the 1d template). Seconds and custom
 * timeframes are chart-side aggregations, so the Strategy Tester is disabled on them.
 */
export function backtestSupported(tf: Timeframe): boolean {
  return isNativeInterval(tf)
}

/** Why the Strategy Tester cannot run on the chart's timeframe. */
export const BACKTEST_TIMEFRAME_MESSAGE =
  'Backtests run on native intervals (1m–1W); switch timeframe'

/** Chart intervals the lab has no bars for, and the lab interval the template uses instead. */
const LAB_INTERVAL_FALLBACK: Readonly<Record<string, string>> = { '1w': '1d' }

/** The interval a template for this chart interval uses (the lab has no `1w`: `1d`). */
export function labIntervalFor(interval: string): string {
  return LAB_INTERVAL_FALLBACK[interval] ?? interval
}

/** Short note when the template cannot use the chart's interval, else null. */
export function templateIntervalNote(interval: string): string | null {
  const lab = labIntervalFor(interval)
  return lab === interval ? null : `The lab has no ${interval} bars, so the template uses ${lab}.`
}

/** EMA 20/50 crossover, long only, full position: the starter plan for the chart's market. */
export function planTemplate(symbol: string, chartInterval: string): LabPlan {
  const interval = labIntervalFor(chartInterval)
  return {
    name: `ema_cross_20_50_${symbol.toLowerCase()}_${interval}`,
    symbol,
    interval,
    features: {
      ema_fast: { ema: { period: 20 } },
      ema_slow: { ema: { period: 50 } },
    },
    long_entry: 'crosses_above(ema_fast, ema_slow)',
    long_exit: 'crosses_below(ema_fast, ema_slow)',
    risk: { position_size_pct: 100 },
  }
}

export function planTemplateText(symbol: string, interval: string): string {
  return JSON.stringify(planTemplate(symbol, interval), null, 2)
}

export type ParsedPlan = { ok: true; plan: LabPlan } | { ok: false; error: PlanError }

/** "line 3, column 5" from a 0-based character offset. */
export function lineColumn(text: string, offset: number): { line: number; column: number } {
  const before = text.slice(0, Math.max(0, offset))
  const lines = before.split('\n')
  return { line: lines.length, column: lines[lines.length - 1].length + 1 }
}

/** The textarea as a JSON object, or a `(json)` error the editor shows like a plan error. */
export function parsePlanText(text: string): ParsedPlan {
  if (text.trim() === '') {
    return { ok: false, error: { path: '(json)', message: 'The plan is empty' } }
  }
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (err) {
    const raw = err instanceof Error ? err.message : String(err)
    const pos = /position (\d+)/.exec(raw)
    let message = raw
    if (pos && !/line \d+/.test(raw)) {
      const { line, column } = lineColumn(text, Number(pos[1]))
      message = `${raw} (line ${line}, column ${column})`
    }
    return { ok: false, error: { path: '(json)', message: `Invalid JSON: ${message}` } }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return { ok: false, error: { path: '(json)', message: 'The plan must be a JSON object' } }
  }
  return { ok: true, plan: value as LabPlan }
}

/** The plan's own name/symbol/interval, for headings. */
export function planLabel(plan: LabPlan): { name: string; symbol: string; interval: string } {
  const s = (v: unknown) => (typeof v === 'string' ? v : '')
  return { name: s(plan.name), symbol: s(plan.symbol), interval: s(plan.interval) }
}

/** An example features entry for a registry feature, using its default parameters. */
export function featureSnippet(feature: LabFeature): string {
  const params: Record<string, number> = {}
  for (const p of feature.params) params[p.name] = p.default
  return JSON.stringify({ [feature.name]: params })
}

// ---------------------------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------------------------

function finite(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** Fraction → "12.34%"; null → "—". */
export function fmtPct(v: number | null | undefined, dp = 2): string {
  if (!finite(v)) return EMPTY
  const s = (v * 100).toFixed(dp)
  return `${Number(s) === 0 ? (0).toFixed(dp) : s.replace('-', '−')}%`
}

/** Fraction → "+12.34%" / "−3.00%". */
export function fmtSignedPct(v: number | null | undefined, dp = 2): string {
  if (!finite(v)) return EMPTY
  const s = fmtPct(v, dp)
  return Number((v * 100).toFixed(dp)) > 0 ? `+${s}` : s
}

/** 2 dp; null → "—". */
export function fmtRatio(v: number | null | undefined, dp = 2): string {
  if (!finite(v)) return EMPTY
  const s = v.toFixed(dp)
  return Number(s) === 0 ? (0).toFixed(dp) : s.replace('-', '−')
}

/** "1,234". */
export function fmtInt(v: number | null | undefined): string {
  if (!finite(v)) return EMPTY
  return Math.round(v).toLocaleString('en-US')
}

/** "812 ms" / "12.3 s" / "2 min 05 s". */
export function fmtElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return EMPTY
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const whole = Math.floor(s)
  return `${Math.floor(whole / 60)} min ${String(whole % 60).padStart(2, '0')} s`
}

/** "2019-01-01" (UTC). */
export function fmtDate(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(0, 10)
}

/** "2024-03-01 14:00" (UTC). */
export function fmtDateTime(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

export type MetricKind = 'pct' | 'ratio' | 'int'

export interface MetricRowDef {
  key: keyof LabMetrics
  label: string
  kind: MetricKind
  hint: string
  /** For the strategy vs buy-and-hold highlight; omitted = not compared. */
  better?: 'higher' | 'lower' | 'smaller-abs'
}

/** Every LabMetrics field, in display order. */
export const METRIC_ROWS: readonly MetricRowDef[] = [
  {
    key: 'total_return',
    label: 'Total return',
    kind: 'pct',
    hint: 'Over the design window',
    better: 'higher',
  },
  { key: 'cagr', label: 'CAGR', kind: 'pct', hint: 'Annualised, 365-day year', better: 'higher' },
  { key: 'volatility', label: 'Volatility', kind: 'pct', hint: 'Annualised std. dev. of returns' },
  {
    key: 'sharpe',
    label: 'Sharpe',
    kind: 'ratio',
    hint: 'Annualised, 365-day year',
    better: 'higher',
  },
  { key: 'sortino', label: 'Sortino', kind: 'ratio', hint: 'Downside risk only', better: 'higher' },
  {
    key: 'max_drawdown',
    label: 'Max drawdown',
    kind: 'pct',
    hint: 'Worst peak-to-trough fall',
    better: 'smaller-abs',
  },
  { key: 'win_rate', label: 'Win rate', kind: 'pct', hint: 'Share of profitable trades' },
  {
    key: 'profit_factor',
    label: 'Profit factor',
    kind: 'ratio',
    hint: 'Gross profit / gross loss',
    better: 'higher',
  },
  { key: 'n_trades', label: 'Trades', kind: 'int', hint: 'Closed and open round trips' },
  { key: 'exposure', label: 'Exposure', kind: 'pct', hint: 'Share of bars in a position' },
  { key: 'turnover', label: 'Turnover', kind: 'ratio', hint: 'Traded notional / equity' },
  { key: 'bars', label: 'Bars', kind: 'int', hint: 'Bars in the window' },
]

export function formatMetric(kind: MetricKind, v: number | null): string {
  if (kind === 'pct') return fmtPct(v)
  if (kind === 'ratio') return fmtRatio(v)
  return fmtInt(v)
}

export const METRIC_COLUMNS = [
  { id: 'net', label: 'Strategy (net)' },
  { id: 'gross', label: 'Strategy (gross)' },
  { id: 'buy_and_hold', label: 'Buy & hold' },
  { id: 'vol_target', label: 'Vol-target B&H' },
] as const

export type Compare = 'better' | 'worse' | null

export interface MetricTableRow {
  key: keyof LabMetrics
  label: string
  hint: string
  cells: string[]
  /** Net strategy vs buy-and-hold for this row. */
  compare: Compare
}

/** `a` (strategy) against `b` (benchmark) under a row's rule. */
export function compareMetric(
  rule: MetricRowDef['better'],
  a: number | null,
  b: number | null,
): Compare {
  if (rule === undefined || !finite(a) || !finite(b) || a === b) return null
  if (rule === 'higher') return a > b ? 'better' : 'worse'
  if (rule === 'lower') return a < b ? 'better' : 'worse'
  const da = Math.abs(a)
  const db = Math.abs(b)
  if (da === db) return null
  return da < db ? 'better' : 'worse'
}

export function metricsTable(report: BacktestReport): MetricTableRow[] {
  const sources: LabMetrics[] = [
    report.metrics.net,
    report.metrics.gross,
    report.benchmarks.buy_and_hold,
    report.benchmarks.vol_target,
  ]
  return METRIC_ROWS.map((row) => ({
    key: row.key,
    label: row.label,
    hint: row.hint,
    cells: sources.map((m) => formatMetric(row.kind, m[row.key])),
    compare: compareMetric(
      row.better,
      report.metrics.net[row.key],
      report.benchmarks.buy_and_hold[row.key],
    ),
  }))
}

export type Tone = 'good' | 'warn' | 'bad' | 'neutral'

export interface StatItem {
  id: 'dsr' | 'psr' | 'pbo' | 'sharpe_ci' | 'random_entry'
  label: string
  value: string
  hint: string
  tone: Tone
}

const plural = (n: number, word: string) => `${fmtInt(n)} ${word}${n === 1 ? '' : 's'}`

/** Thresholds match PLAN.md Phase 8 success criteria (DSR ≥ 0.95, PBO ≤ 0.2). */
export const DSR_PASS = 0.95
export const PBO_PASS = 0.2
export const PBO_FAIL = 0.5
export const RANDOM_PASS_PERCENTILE = 95

export function statItems(report: BacktestReport): StatItem[] {
  const { dsr, pbo, sharpe_ci: ci } = report.stats
  const random = report.benchmarks.random_entry
  const trials = plural(dsr.n_trials, 'trial')
  const items: StatItem[] = []

  items.push(
    finite(dsr.dsr)
      ? {
          id: 'dsr',
          label: 'Deflated Sharpe (DSR)',
          value: fmtRatio(dsr.dsr),
          hint:
            dsr.dsr >= DSR_PASS
              ? `≥ ${DSR_PASS}: significant after ${trials}`
              : `DSR < ${DSR_PASS}: not significant after ${trials}`,
          tone: dsr.dsr >= DSR_PASS ? 'good' : 'bad',
        }
      : {
          id: 'dsr',
          label: 'Deflated Sharpe (DSR)',
          value: EMPTY,
          hint: `Not computable for this run (${trials})`,
          tone: 'neutral',
        },
  )

  items.push({
    id: 'psr',
    label: 'Probabilistic Sharpe (PSR)',
    value: fmtRatio(dsr.psr),
    hint: finite(dsr.psr)
      ? `Chance the true Sharpe is above 0, before correcting for the number of trials`
      : 'Not computable for this run',
    tone: !finite(dsr.psr) ? 'neutral' : dsr.psr >= DSR_PASS ? 'good' : 'warn',
  })

  const pboContext = `${plural(pbo.n_trials, 'trial')}, ${plural(pbo.n_splits, 'split')}`
  // A neighbourhood PBO scores this plan against its ±20% parameter neighbours: it says how
  // stable the parameters are, not how much a search overfit. Graded on the Phase 8 bar.
  const neighbourhood = pbo.basis === 'neighbourhood'
  const pboLabel = neighbourhood ? 'PBO (±20% neighbourhood)' : 'Overfitting probability (PBO)'
  if (finite(pbo.pbo)) {
    const v = pbo.pbo
    const tone: Tone = v <= PBO_PASS ? 'good' : v > PBO_FAIL ? 'bad' : 'warn'
    items.push({
      id: 'pbo',
      label: pboLabel,
      value: fmtRatio(v),
      hint: neighbourhood
        ? `${
            tone === 'good'
              ? `≤ ${PBO_PASS}: stable`
              : tone === 'bad'
                ? `> ${PBO_FAIL}: unstable`
                : `Above ${PBO_PASS}: fragile`
          } · measures parameter stability, not search overfitting (${pboContext})`
        : tone === 'good'
          ? `≤ ${PBO_PASS}: the in-sample best rarely fails out of sample (${pboContext})`
          : tone === 'bad'
            ? `> ${PBO_FAIL}: the in-sample best usually fails out of sample, likely overfit (${pboContext})`
            : `Above ${PBO_PASS}: material overfitting risk (${pboContext})`,
      tone,
    })
  } else {
    items.push({
      id: 'pbo',
      label: pboLabel,
      value: EMPTY,
      hint: `Needs several comparable trials to estimate (${pboContext})`,
      tone: 'neutral',
    })
  }

  const level = `${Math.round(ci.level * 100)}%`
  if (finite(ci.low) && finite(ci.high)) {
    const spansZero = ci.low <= 0 && ci.high >= 0
    items.push({
      id: 'sharpe_ci',
      label: `Sharpe ${level} CI`,
      value: `${fmtRatio(ci.low)} … ${fmtRatio(ci.high)}`,
      hint: spansZero
        ? 'Includes 0: cannot tell this Sharpe from no edge (stationary bootstrap)'
        : ci.low > 0
          ? 'Above 0 across the interval (stationary bootstrap)'
          : 'Below 0 across the interval: a losing strategy (stationary bootstrap)',
      tone: spansZero ? 'warn' : ci.low > 0 ? 'good' : 'bad',
    })
  } else {
    items.push({
      id: 'sharpe_ci',
      label: `Sharpe ${level} CI`,
      value: EMPTY,
      hint: 'Not computable for this run',
      tone: 'neutral',
    })
  }

  if (finite(random.percentile)) {
    const p = random.percentile
    items.push({
      id: 'random_entry',
      label: 'vs random entries',
      value: `${p.toFixed(0)}th pct`,
      hint:
        p >= RANDOM_PASS_PERCENTILE
          ? `Beats ${p.toFixed(0)}% of ${plural(random.n_paths, 'random path')} with the same trade count`
          : `Below the ${RANDOM_PASS_PERCENTILE}th percentile of ${plural(random.n_paths, 'random path')}: not distinguishable from luck`,
      tone: p >= RANDOM_PASS_PERCENTILE ? 'good' : 'bad',
    })
  } else {
    items.push({
      id: 'random_entry',
      label: 'vs random entries',
      value: EMPTY,
      hint: 'Not computable for this run',
      tone: 'neutral',
    })
  }
  return items
}

export interface SummaryTile {
  label: string
  value: string
  tone: Tone
  sub?: string
}

const toneOf = (v: number | null): Tone =>
  !finite(v) || v === 0 ? 'neutral' : v > 0 ? 'good' : 'bad'

/** Headline numbers above the table (net of costs). */
export function summaryTiles(report: BacktestReport): SummaryTile[] {
  const net = report.metrics.net
  const bh = report.benchmarks.buy_and_hold
  const diff =
    finite(net.total_return) && finite(bh.total_return) ? net.total_return - bh.total_return : null
  return [
    { label: 'Net return', value: fmtSignedPct(net.total_return), tone: toneOf(net.total_return) },
    {
      label: 'vs Buy & hold',
      value: fmtSignedPct(diff),
      tone: toneOf(diff),
      sub: `B&H ${fmtSignedPct(bh.total_return)}`,
    },
    {
      label: 'Sharpe',
      value: fmtRatio(net.sharpe),
      tone: 'neutral',
      sub: `B&H ${fmtRatio(bh.sharpe)}`,
    },
    {
      label: 'Max drawdown',
      value: fmtPct(net.max_drawdown),
      tone: 'neutral',
      sub: `B&H ${fmtPct(bh.max_drawdown)}`,
    },
    {
      label: 'Trades',
      value: fmtInt(net.n_trades),
      tone: 'neutral',
      sub: `Win ${fmtPct(net.win_rate, 1)}`,
    },
    { label: 'Profit factor', value: fmtRatio(net.profit_factor), tone: 'neutral' },
  ]
}

export interface WalkForwardRow {
  fold: string
  period: string
  sharpe: string
  totalReturn: string
  trades: string
  tone: Tone
}

export function walkForwardRows(folds: readonly WalkForwardFold[]): WalkForwardRow[] {
  return folds.map((f) => ({
    fold: String(f.fold + 1),
    period: `${fmtDate(f.test_start)} → ${fmtDate(f.test_end)}`,
    sharpe: fmtRatio(f.sharpe),
    totalReturn: fmtSignedPct(f.total_return),
    trades: fmtInt(f.n_trades),
    tone: toneOf(f.total_return),
  }))
}

/** Heading and note of the walk-forward table, by how the lab built the folds. */
export function walkForwardHeading(report: Pick<BacktestReport, 'walk_forward_kind'>): {
  title: string
  hint: string | null
} {
  if (report.walk_forward_kind === 'fixed_params') {
    return {
      title: 'Fold check (fixed parameters — not out-of-sample)',
      hint: 'Every fold scores the same parameters; nothing is re-fitted per fold, so this shows stability over time, not out-of-sample performance.',
    }
  }
  return { title: 'Walk-forward (out-of-sample folds)', hint: null }
}

/** Chart on Binance global (the archive's venue): one line for the panel. */
export const VENUE_NOTE =
  'Chart and backtests use the same Binance spot data: backtests read the verified data.binance.vision archive, the chart reads Binance’s live market data.'

/**
 * The "Data" line under the report. Same-data wording only when the chart's venue is the
 * archive's (Binance global); another venue gets an honest "differs" line; unknown (server not
 * answered yet) states only where backtests come from.
 */
export function venueNote(chartVenue: { id: string; name: string } | null): string {
  if (chartVenue === null) {
    return 'Backtests read the verified Binance global spot archive (data.binance.vision).'
  }
  if (chartVenue.id === ARCHIVE_VENUE_ID) return VENUE_NOTE
  return `The chart venue (${chartVenue.name}) differs from the backtest archive (Binance global spot, data.binance.vision), so prices and trade times on the chart can differ from the backtest.`
}

/** "Fee 0.10% · Slippage 0.05% (+50% × bar range) per side". */
export function costsText(costs: BacktestReport['costs']): string {
  const vol =
    costs.slippage_vol_mult > 0
      ? `, widened ×${fmtRatio(costs.slippage_vol_mult)} on wide bars`
      : ''
  return `Fee ${fmtPct(costs.fee_rate)} · Slippage ${fmtPct(costs.slippage_rate)}${vol} · per side`
}

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

export interface RunErrorView {
  kind: 'offline' | 'no-data' | 'invalid' | 'timeout' | 'error'
  title: string
  message: string
  errors: PlanError[]
}

export function describeLabError(err: unknown): RunErrorView {
  if (err instanceof LabError) {
    switch (err.kind) {
      case 'offline':
        return {
          kind: 'offline',
          title: 'Lab service not running',
          message: LAB_OFFLINE_MESSAGE,
          errors: [],
        }
      case 'no-data':
        return {
          kind: 'no-data',
          title: 'No market data for this plan',
          message: err.message,
          errors: [],
        }
      case 'invalid':
        return {
          kind: 'invalid',
          title: 'The plan has errors',
          message:
            err.errors.length > 0
              ? `${plural(err.errors.length, 'error')} — see the plan editor`
              : err.message,
          errors: err.errors,
        }
      case 'timeout':
        return {
          kind: 'timeout',
          title: 'The backtest timed out',
          message:
            'The lab service did not answer in time. Try a shorter window or a higher interval.',
          errors: [],
        }
      default:
        return { kind: 'error', title: 'The backtest failed', message: err.message, errors: [] }
    }
  }
  const message = err instanceof Error && err.message ? err.message : 'Unknown error'
  return { kind: 'error', title: 'The backtest failed', message, errors: [] }
}

// ---------------------------------------------------------------------------------------------
// Panel state
// ---------------------------------------------------------------------------------------------

export type LabStatus = 'unknown' | 'online' | 'offline'

export type ValidationState =
  | { kind: 'idle' }
  | { kind: 'checking'; text: string }
  | { kind: 'valid'; text: string }
  | { kind: 'invalid'; text: string; errors: PlanError[] }
  /** The lab could not validate (offline / error); the editor only shows local JSON errors. */
  | { kind: 'unavailable'; text: string; message: string }

export type RunState =
  | { kind: 'idle' }
  | { kind: 'running'; id: number; startedAt: number }
  | { kind: 'error'; error: RunErrorView }

export interface TesterState {
  /** The user's own plan text; only used while `planEdited` (see `planTextFor`). */
  planText: string
  /** false = the untouched template, which follows the chart's symbol and interval. */
  planEdited: boolean
  /** The lab's verdict on one exact plan text (compare `text` with the current text). */
  validation: ValidationState
  run: RunState
  /** Next run id; a late answer for an older id is ignored. */
  runSeq: number
  report: BacktestReport | null
  /** The user wants the report drawn on the chart (it is drawn only when the market matches). */
  shownOnChart: boolean
  lab: LabStatus
}

export type TesterAction =
  | { type: 'edit'; text: string }
  | { type: 'reset-template' }
  | { type: 'validate-start'; text: string }
  | { type: 'validate-done'; text: string; ok: boolean; errors: PlanError[] }
  | { type: 'validate-fail'; text: string; error: RunErrorView }
  | { type: 'run-start'; startedAt: number }
  | { type: 'run-done'; id: number; report: BacktestReport }
  | { type: 'run-fail'; id: number; text: string; error: RunErrorView }
  | { type: 'dismiss-error' }
  | { type: 'lab-status'; status: LabStatus }
  | { type: 'show-on-chart'; shown: boolean }

export function initTesterState(saved?: { text: string; edited: boolean } | null): TesterState {
  return {
    planText: saved?.edited ? saved.text : '',
    planEdited: saved?.edited === true,
    validation: { kind: 'idle' },
    run: { kind: 'idle' },
    runSeq: 1,
    report: null,
    shownOnChart: false,
    lab: 'unknown',
  }
}

/** The text in the editor: the user's plan, or the template for the chart's market. */
export function planTextFor(state: TesterState, symbol: string, interval: string): string {
  return state.planEdited ? state.planText : planTemplateText(symbol, interval)
}

function labFromError(prev: LabStatus, error: RunErrorView): LabStatus {
  return error.kind === 'offline' ? 'offline' : error.kind === 'timeout' ? prev : 'online'
}

export function testerReducer(state: TesterState, action: TesterAction): TesterState {
  switch (action.type) {
    case 'edit':
      if (state.planEdited && action.text === state.planText) return state
      return { ...state, planText: action.text, planEdited: true }
    case 'reset-template':
      return state.planEdited ? { ...state, planText: '', planEdited: false } : state
    case 'validate-start':
      return { ...state, validation: { kind: 'checking', text: action.text } }
    case 'validate-done':
      return {
        ...state,
        lab: 'online',
        validation:
          action.ok && action.errors.length === 0
            ? { kind: 'valid', text: action.text }
            : { kind: 'invalid', text: action.text, errors: action.errors },
      }
    case 'validate-fail':
      return {
        ...state,
        lab: labFromError(state.lab, action.error),
        validation: { kind: 'unavailable', text: action.text, message: action.error.message },
      }
    case 'run-start':
      if (state.run.kind === 'running') return state
      return {
        ...state,
        run: { kind: 'running', id: state.runSeq, startedAt: action.startedAt },
        runSeq: state.runSeq + 1,
      }
    case 'run-done':
      if (state.run.kind !== 'running' || state.run.id !== action.id) return state
      // A fresh report is drawn as soon as the chart shows its market.
      return {
        ...state,
        lab: 'online',
        run: { kind: 'idle' },
        report: action.report,
        shownOnChart: true,
      }
    case 'run-fail': {
      if (state.run.kind !== 'running' || state.run.id !== action.id) return state
      const { error } = action
      return {
        ...state,
        lab: labFromError(state.lab, error),
        run: { kind: 'error', error },
        validation:
          error.kind === 'invalid' && error.errors.length > 0
            ? { kind: 'invalid', text: action.text, errors: error.errors }
            : state.validation,
      }
    }
    case 'dismiss-error':
      return state.run.kind === 'error' ? { ...state, run: { kind: 'idle' } } : state
    case 'lab-status':
      return state.lab === action.status ? state : { ...state, lab: action.status }
    case 'show-on-chart':
      return state.shownOnChart === action.shown ? state : { ...state, shownOnChart: action.shown }
  }
}

/** Errors to list in the editor: a local JSON error first, else the lab's for this exact text. */
export function editorErrors(state: TesterState, text: string, parsed: ParsedPlan): PlanError[] {
  if (!parsed.ok) return [parsed.error]
  const v = state.validation
  return v.kind === 'invalid' && v.text === text ? v.errors : []
}

export interface PlanStatus {
  tone: Tone
  /** Chip text. */
  text: string
  /**
   * What a polite live region announces: the lab's settled verdict on this exact text, or
   * '' while typing / checking, so screen readers do not hear every keystroke's state.
   */
  announce: string
}

/** The editor's status chip, and what (if anything) to announce for it. */
export function planStatus(
  state: TesterState,
  text: string,
  parsedOk: boolean,
  errorCount: number,
  chartInterval?: Timeframe,
): PlanStatus {
  const v = state.validation
  const forText = 'text' in v && v.text === text
  if (!parsedOk) return { tone: 'bad', text: 'Invalid JSON', announce: '' }
  // Nothing is sent to the lab on a non-native timeframe, so the plan is never checked there.
  if (chartInterval !== undefined && !backtestSupported(chartInterval) && errorCount === 0) {
    return { tone: 'warn', text: 'Not checked', announce: '' }
  }
  if (errorCount > 0) {
    const label = `${fmtInt(errorCount)} error${errorCount === 1 ? '' : 's'}`
    return {
      tone: 'bad',
      text: label,
      announce: v.kind === 'invalid' && forText ? `Plan has ${label}` : '',
    }
  }
  if (v.kind === 'valid' && forText) return { tone: 'good', text: 'Valid', announce: 'Plan valid' }
  if (state.lab === 'offline' || (v.kind === 'unavailable' && forText)) {
    return {
      tone: 'warn',
      text: 'Not checked',
      announce: 'Plan not checked: the lab service is unavailable',
    }
  }
  return { tone: 'neutral', text: 'Checking…', announce: '' }
}

/** Why Run is disabled, or null when it can run. `chartInterval`: the chart's timeframe. */
export function runBlockedReason(
  state: TesterState,
  text: string,
  parsed: ParsedPlan,
  chartInterval?: Timeframe,
): string | null {
  if (state.run.kind === 'running') return 'A backtest is running'
  if (chartInterval !== undefined && !backtestSupported(chartInterval)) {
    return BACKTEST_TIMEFRAME_MESSAGE
  }
  if (!parsed.ok) return 'Fix the JSON first'
  const v = state.validation
  if (v.kind === 'invalid' && v.text === text) return 'Fix the plan errors first'
  if (state.lab === 'offline') return LAB_OFFLINE_MESSAGE
  return null
}

// ---------------------------------------------------------------------------------------------
// Chart link
// ---------------------------------------------------------------------------------------------

export function isChartInterval(v: string): v is Interval {
  return (INTERVALS as readonly string[]).includes(v)
}

export function reportMatchesChart(
  report: Pick<BacktestReport, 'symbol' | 'interval'>,
  symbol: string,
  interval: string,
): boolean {
  return report.symbol === symbol && report.interval === interval
}

/**
 * Most older bars one backtest history load fetches (a per-load budget, not a chart size: the
 * chart itself holds up to CHART_MAX_BARS in chart/conflation.ts, Phase 14).
 */
export const MAX_HISTORY_BARS = 20_000

/**
 * Older bars one backtest history load may still fetch onto a chart that holds `candleCount`
 * bars: at most MAX_HISTORY_BARS per load, and never past the chart's CHART_MAX_BARS.
 */
export function historyRoom(
  candleCount: number,
  perLoad = MAX_HISTORY_BARS,
  chartCap = CHART_MAX_BARS,
): number {
  return Math.max(0, Math.min(perLoad, chartCap - candleCount))
}

export interface HistoryPlan {
  /** Older bars are needed to show the whole design window. */
  needed: boolean
  /** Load until the first bar is at or before this time (UNIX seconds). */
  target: number
  /** Approximate bars still to load. */
  bars: number
  /** The window reaches further back than `cap` more bars: only its most recent part can be shown. */
  partial: boolean
  /**
   * Even the most bars this load may fetch end before the window does: no part of the window
   * can be shown at this interval, so nothing should be fetched.
   */
  unreachable: boolean
}

export function historyPlan(
  firstBar: number | null,
  window: BacktestReport['window'],
  interval: Timeframe,
  cap = MAX_HISTORY_BARS,
): HistoryPlan {
  const step = timeframeSeconds(interval)
  const room = Math.max(0, cap)
  if (firstBar === null || firstBar <= window.start) {
    return { needed: false, target: window.start, bars: 0, partial: false, unreachable: false }
  }
  const bars = Math.ceil((firstBar - window.start) / step)
  if (bars <= room) {
    return { needed: true, target: window.start, bars, partial: false, unreachable: false }
  }
  const target = firstBar - room * step
  if (target >= window.end) {
    return { needed: true, target, bars: 0, partial: true, unreachable: true }
  }
  return { needed: true, target, bars: room, partial: true, unreachable: false }
}

/** Load older bars until the chart's first bar reaches `target` (a backtest's window start). */
export interface HistoryTarget {
  symbol: string
  interval: Timeframe
  target: number
}

/** Consecutive failed older-bar pages before an auto-load gives up. */
export const MAX_HISTORY_FAILURES = 5
/** Retry ticks with no request starting (e.g. the browser is offline) before it gives up. */
export const MAX_HISTORY_IDLE_TICKS = 12
/** How often an active load nudges `loadOlder` (it is a no-op during its own cooldown). */
export const HISTORY_RETRY_MS = 2500

export interface HistoryLoadState {
  /** null = no load running. */
  target: HistoryTarget | null
  /** Consecutive pages that ended without adding bars. */
  failures: number
  /** Retry ticks since the last request started. */
  idleTicks: number
  /** The market whose last load gave up (shows "History load failed — Retry"). */
  failed: { symbol: string; interval: Timeframe } | null
}

export type HistoryLoadAction =
  | { type: 'start'; target: HistoryTarget }
  | { type: 'stop' }
  /** An older-bar request started. */
  | { type: 'request' }
  /** An older-bar request ended; `progressed` = the first bar moved back. */
  | { type: 'page-done'; progressed: boolean }
  /** The retry timer fired. */
  | { type: 'tick' }

export const initialHistoryLoad: HistoryLoadState = {
  target: null,
  failures: 0,
  idleTicks: 0,
  failed: null,
}

function giveUp(state: HistoryLoadState, failures: number, idleTicks: number): HistoryLoadState {
  const t = state.target
  return {
    target: null,
    failures,
    idleTicks,
    failed: t ? { symbol: t.symbol, interval: t.interval } : state.failed,
  }
}

export function historyLoadReducer(
  state: HistoryLoadState,
  action: HistoryLoadAction,
): HistoryLoadState {
  switch (action.type) {
    case 'start':
      return { target: action.target, failures: 0, idleTicks: 0, failed: null }
    case 'stop':
      return state.target === null && state.failed === null ? state : initialHistoryLoad
    case 'request':
      return state.target === null || state.idleTicks === 0 ? state : { ...state, idleTicks: 0 }
    case 'page-done': {
      if (state.target === null) return state
      if (action.progressed) {
        return state.failures === 0 && state.idleTicks === 0
          ? state
          : { ...state, failures: 0, idleTicks: 0 }
      }
      const failures = state.failures + 1
      if (failures >= MAX_HISTORY_FAILURES) return giveUp(state, failures, 0)
      return { ...state, failures, idleTicks: 0 }
    }
    case 'tick': {
      if (state.target === null) return state
      const idleTicks = state.idleTicks + 1
      if (idleTicks >= MAX_HISTORY_IDLE_TICKS) return giveUp(state, state.failures, idleTicks)
      return { ...state, idleTicks }
    }
  }
}

/** The failed-load notice applies to this market. */
export function historyLoadFailed(
  state: HistoryLoadState,
  symbol: string,
  interval: Timeframe,
): boolean {
  return (
    state.failed !== null && state.failed.symbol === symbol && state.failed.interval === interval
  )
}

// ---- Automatic history load + zoom after a backtest ----

/** One report on one market: the unit the automatic load runs at most once for. */
export function autoHistoryKey(
  report: Pick<BacktestReport, 'run_id' | 'trial_id' | 'symbol' | 'interval'>,
): string {
  return `${report.run_id}#${report.trial_id}@${report.symbol}:${report.interval}`
}

export interface AutoHistoryState {
  /** The report+market last handled; null before the first. */
  key: string | null
  /**
   * 'starting' = the automatic load for `key` was asked for and has not been seen running yet;
   * 'loading' = it is running; 'done' = nothing left to do for `key`.
   */
  phase: 'starting' | 'loading' | 'done'
}

export const initialAutoHistory: AutoHistoryState = { key: null, phase: 'done' }

export interface AutoHistoryInput {
  /** `autoHistoryKey` of the drawn report, null when no report is drawn on its market. */
  key: string | null
  /** History plan for the drawn report (null while there are no bars). */
  plan: HistoryPlan | null
  /** A history load is running for this market. */
  loading: boolean
  /** The last history load for this market gave up. */
  failed: boolean
  /** The exchange has no older bars. */
  exhausted: boolean
  /**
   * The chart's first bar is at or before the target of the last history load on this market.
   * A capped load can end there with fewer than MAX_HISTORY_BARS raw bars (exchange gaps), while
   * the plan, recomputed from the bar count, still asks for more.
   */
  reachedTarget: boolean
}

export type AutoHistoryCommand = 'none' | 'start' | 'zoom'

/**
 * As much of the window as the chart can hold is loaded: the window start is reached, the
 * exchange has nothing older, or (window longer than the chart can hold) the bar cap or the
 * last capped load's target is reached.
 */
function historyComplete(plan: HistoryPlan, input: AutoHistoryInput): boolean {
  if (!plan.needed || input.exhausted) return true
  return plan.partial && (plan.bars === 0 || input.reachedTarget)
}

/**
 * Drives the automatic "load history, then zoom to the trades" once per report and market.
 *  - A new key: an unreachable window does nothing (the bar explains it); a needed load starts
 *    ('start'); otherwise the chart already holds the window and zooms ('zoom').
 *  - While its load runs: nothing. When the load ends: 'zoom' if the window start (or the bar
 *    limit, or the exchange's first bar) was reached; nothing if the user stopped it or it failed.
 *  - The same key never starts again, so stopping, scrolling away, or revisiting the market
 *    leaves the chart alone. Respecting cooldowns, Retry-After, and offline is the history
 *    load's own job.
 */
export function autoHistoryStep(
  state: AutoHistoryState,
  input: AutoHistoryInput,
): { state: AutoHistoryState; command: AutoHistoryCommand } {
  const { key, plan } = input
  if (key === null || plan === null) return { state, command: 'none' }
  if (state.key !== key) {
    if (plan.unreachable) return { state: { key, phase: 'done' }, command: 'none' }
    // Already complete (e.g. the chart is at its bar cap): a load would never run, so zoom now.
    if (historyComplete(plan, input)) return { state: { key, phase: 'done' }, command: 'zoom' }
    return { state: { key, phase: 'starting' }, command: 'start' }
  }
  if (state.phase === 'starting') {
    // Wait until the load is seen running, so a stale read right after 'start' cannot end it.
    return input.loading
      ? { state: { key, phase: 'loading' }, command: 'none' }
      : { state, command: 'none' }
  }
  if (state.phase !== 'loading' || input.loading) return { state, command: 'none' }
  const done: AutoHistoryState = { key, phase: 'done' }
  if (!input.failed && historyComplete(plan, input)) return { state: done, command: 'zoom' }
  return { state: done, command: 'none' }
}

/** "Trades are in 2019-01-01 → 2025-10-01 · loading history… 12 of 137 trades on chart". */
export function historyLoadingText(
  window: { start: number; end: number },
  onChart: number,
  total: number,
): string {
  return `Trades are in ${fmtDate(window.start)} → ${fmtDate(window.end)} · loading history… ${fmtInt(onChart)} of ${fmtInt(total)} trades on chart`
}

export interface HistoryLoadFlags {
  loading: boolean
  failed: boolean
}

/**
 * Screen-reader announcement for a change of the history-load state: only start, end, and
 * failure, never the per-page progress. null = nothing to announce (keep the previous text).
 */
export function historyAnnouncement(
  prev: HistoryLoadFlags,
  next: HistoryLoadFlags,
  onChart: number,
  total: number,
): string | null {
  if (!prev.loading && next.loading) return 'Loading chart history for the backtest…'
  if (next.failed && (prev.loading || !prev.failed)) return 'Loading chart history failed.'
  if (prev.loading && !next.loading) {
    return `History loading ended: ${fmtInt(onChart)} of ${fmtInt(total)} trades on chart.`
  }
  return null
}

/** Why nothing is loaded for an unreachable window, with what to do instead. */
export function unreachableText(windowEnd: number): string {
  return `The design window ends ${fmtDate(windowEnd)}; at this interval it lies further back than one history load reaches (at most ${fmtInt(MAX_HISTORY_BARS)} older bars per load, ${fmtInt(CHART_MAX_BARS)} bars on the chart). Use 1h, 4h or 1d to see the trade markers.`
}

/** Zoom range for one trade, with context on both sides (open trades run to `windowEnd`). */
export function tradeFocusRange(
  trade: Pick<LabTrade, 'entry_time' | 'exit_time'>,
  windowEnd: number,
): { from: number; to: number } {
  const end = trade.exit_time ?? windowEnd
  const pad = Math.max((end - trade.entry_time) * 0.6, 3600 * 24)
  return { from: trade.entry_time - pad, to: end + pad }
}

/** A trade row can jump to the chart only while the report is drawn and its entry bar is loaded. */
export function tradeFocusable(
  trade: Pick<LabTrade, 'entry_time'>,
  drawn: boolean,
  firstBar: number | null,
): boolean {
  return drawn && firstBar !== null && trade.entry_time >= firstBar
}

// ---------------------------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------------------------

export interface PageInfo {
  /** 1-based, inclusive; 0 when empty. */
  from: number
  to: number
  hasPrev: boolean
  hasNext: boolean
  label: string
}

export function pageInfo(total: number, offset: number, count: number): PageInfo {
  if (total <= 0 || count <= 0) {
    return { from: 0, to: 0, hasPrev: offset > 0, hasNext: false, label: 'No runs' }
  }
  const from = offset + 1
  const to = offset + count
  return {
    from,
    to,
    hasPrev: offset > 0,
    hasNext: to < total,
    label: `${fmtInt(from)}–${fmtInt(to)} of ${fmtInt(total)}`,
  }
}

/** Offset of the previous / next page, clamped. */
export function pageOffset(offset: number, size: number, dir: -1 | 1, total: number): number {
  const next = offset + dir * size
  if (next < 0) return 0
  if (total > 0 && next >= total) return offset
  return next
}

// ---------------------------------------------------------------------------------------------
// Persistence of the plan text
// ---------------------------------------------------------------------------------------------

export const PLAN_STORAGE_KEY = 'tv.backtest.plan.v1'

export interface PlanStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

function defaultStorage(): PlanStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function loadPlanText(
  storage: PlanStorage | null = defaultStorage(),
): { text: string; edited: boolean } | null {
  try {
    const raw = storage?.getItem(PLAN_STORAGE_KEY)
    if (!raw) return null
    const v: unknown = JSON.parse(raw)
    if (typeof v !== 'object' || v === null) return null
    const { text, edited } = v as { text?: unknown; edited?: unknown }
    if (typeof text !== 'string' || typeof edited !== 'boolean') return null
    return { text: text.slice(0, 100_000), edited }
  } catch {
    return null
  }
}

export function savePlanText(
  text: string,
  edited: boolean,
  storage: PlanStorage | null = defaultStorage(),
): void {
  try {
    storage?.setItem(PLAN_STORAGE_KEY, JSON.stringify({ text, edited }))
  } catch {
    // Quota or privacy mode: the plan simply is not remembered.
  }
}

// ---------------------------------------------------------------------------------------------
// Dock layout prefs
// ---------------------------------------------------------------------------------------------

export const DOCK_STORAGE_KEY = 'tv.backtest.dock.v1'
export const DOCK_MIN_HEIGHT = 200
export const DOCK_DEFAULT_HEIGHT = 340
/** The chart keeps at least this share of the window height. */
export const DOCK_MAX_SHARE = 0.72

export interface DockPrefs {
  open: boolean
  height: number
}

/** Height clamped to [DOCK_MIN_HEIGHT, DOCK_MAX_SHARE × viewport]. */
export function clampDockHeight(height: number, viewport: number): number {
  const max = Math.max(DOCK_MIN_HEIGHT, Math.floor(viewport * DOCK_MAX_SHARE))
  if (!Number.isFinite(height)) return Math.min(DOCK_DEFAULT_HEIGHT, max)
  return Math.round(Math.min(max, Math.max(DOCK_MIN_HEIGHT, height)))
}

export function loadDockPrefs(storage: PlanStorage | null = defaultStorage()): DockPrefs {
  try {
    const raw = storage?.getItem(DOCK_STORAGE_KEY)
    if (raw) {
      const v = JSON.parse(raw) as { open?: unknown; height?: unknown }
      return {
        open: v.open === true,
        height:
          typeof v.height === 'number' && Number.isFinite(v.height)
            ? v.height
            : DOCK_DEFAULT_HEIGHT,
      }
    }
  } catch {
    // Unreadable prefs fall back to the defaults.
  }
  return { open: false, height: DOCK_DEFAULT_HEIGHT }
}

export function saveDockPrefs(
  prefs: DockPrefs,
  storage: PlanStorage | null = defaultStorage(),
): void {
  try {
    storage?.setItem(DOCK_STORAGE_KEY, JSON.stringify(prefs))
  } catch {
    // Quota or privacy mode: the layout simply is not remembered.
  }
}
