/**
 * Client for the Python lab service (`lab/`, FastAPI on 127.0.0.1:8788). The browser calls
 * same-origin `/lab/...`; the Vite dev/preview proxy forwards it.
 *
 * Read-only research calls: health, feature registry, plan validation, design-window backtests,
 * the trial ledger, and regime labels for the chart's regime ribbon. No keys, no exchange calls, no trading endpoints.
 */

import type {
  BacktestReport,
  EquityPoint,
  ExitReason,
  LabFeature,
  LabFeatureParam,
  LabHealth,
  LabMetrics,
  LabPlan,
  LabRegistry,
  LabTrade,
  LedgerDetail,
  LedgerItem,
  LedgerPage,
  LedgerQuery,
  PlanError,
  RegimePoint,
  RegimeRequest,
  RegimeResponse,
  RegimeState,
  ValidationResult,
  WalkForwardFold,
} from './labTypes'
import type { FetchLike } from './persistence'

export const LAB_BASE_PATH = '/lab'
/** Health, registry, validate, ledger. */
export const LAB_TIMEOUT_MS = 10_000
/** A backtest runs the engine, benchmarks, random-entry paths and PBO neighbours. */
export const LAB_BACKTEST_TIMEOUT_MS = 180_000
export const LEDGER_PAGE_SIZE = 25

/**
 * - `offline`: the lab service cannot be reached (fetch failed, or the proxy answered 502/504).
 * - `invalid`: 422, the plan has errors (`errors`).
 * - `no-data`: 503 with a `detail` message (e.g. no candles downloaded yet; says which CLI to run).
 * - `http`: any other non-2xx status.
 * - `parse`: the body is not what the contract says.
 * - `timeout` / `aborted`: our timer, or the caller's signal, cancelled the request.
 */
export type LabErrorKind =
  'offline' | 'invalid' | 'no-data' | 'http' | 'parse' | 'timeout' | 'aborted'

export class LabError extends Error {
  readonly kind: LabErrorKind
  readonly status: number | undefined
  readonly errors: PlanError[]
  constructor(kind: LabErrorKind, message: string, status?: number, errors: PlanError[] = []) {
    super(message)
    this.name = 'LabError'
    this.kind = kind
    this.status = status
    this.errors = errors
  }
}

export const LAB_OFFLINE_MESSAGE = 'Lab service not running — start it with `npm run dev`'

export interface LabClientOptions {
  /** Defaults to the global `fetch`, looked up at call time. */
  fetchImpl?: FetchLike
  /** Defaults to `/lab` (same origin). A trailing slash is dropped. */
  basePath?: string
  timeoutMs?: number
  backtestTimeoutMs?: number
}

/** The chart's regime ribbon needs only this call (`useRegime` takes just this). */
export interface LabRegimeClient {
  /** hedgeFund regime labels for closed bars (display only; nothing is written to the ledger). */
  regime(req: RegimeRequest, signal?: AbortSignal): Promise<RegimeResponse>
}

export interface LabClient extends LabRegimeClient {
  health(signal?: AbortSignal): Promise<LabHealth>
  registry(signal?: AbortSignal): Promise<LabRegistry>
  validate(plan: LabPlan, signal?: AbortSignal): Promise<ValidationResult>
  /** Always the design window. Rejects with LabError `invalid` / `no-data` / `offline` / … */
  backtest(plan: LabPlan, opts?: { runId?: string; signal?: AbortSignal }): Promise<BacktestReport>
  ledger(query?: LedgerQuery, signal?: AbortSignal): Promise<LedgerPage>
  ledgerItem(id: number, signal?: AbortSignal): Promise<LedgerDetail>
}

// ---------------------------------------------------------------------------------------------
// Parsing (tolerant where the contract allows null, strict on structure)
// ---------------------------------------------------------------------------------------------

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** A finite number, else null (the lab sends null for NaN/inf). */
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** A finite number, else `fallback`. */
function numOr(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

function fail(what: string): never {
  throw new LabError('parse', `The lab service sent an unreadable ${what}`)
}

function record(v: unknown, what: string): Record<string, unknown> {
  if (!isRecord(v)) fail(what)
  return v
}

function list(v: unknown, what: string): unknown[] {
  if (!Array.isArray(v)) fail(what)
  return v
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

export function parsePlanErrors(v: unknown): PlanError[] {
  if (!Array.isArray(v)) return []
  const out: PlanError[] = []
  for (const e of v) {
    if (!isRecord(e) || typeof e.message !== 'string') continue
    out.push({ path: str(e.path), message: e.message })
  }
  return out
}

export function parseMetrics(v: unknown, what = 'metrics'): LabMetrics {
  const m = record(v, what)
  return {
    total_return: num(m.total_return),
    cagr: num(m.cagr),
    volatility: num(m.volatility),
    sharpe: num(m.sharpe),
    sortino: num(m.sortino),
    max_drawdown: num(m.max_drawdown),
    win_rate: num(m.win_rate),
    profit_factor: num(m.profit_factor),
    n_trades: numOr(m.n_trades, 0),
    exposure: num(m.exposure),
    turnover: num(m.turnover),
    bars: numOr(m.bars, 0),
  }
}

/** Points with finite time and value, ascending by time with duplicates dropped. */
export function parseEquity(v: unknown, what = 'equity curve'): EquityPoint[] {
  const out: EquityPoint[] = []
  for (const p of list(v, what)) {
    if (!isRecord(p)) continue
    const time = num(p.time)
    const value = num(p.value)
    if (time === null || value === null) continue
    out.push({ time, value })
  }
  out.sort((a, b) => a.time - b.time)
  return out.filter((p, i) => i === 0 || p.time !== out[i - 1].time)
}

const EXIT_REASONS: readonly ExitReason[] = ['signal', 'stop_loss', 'take_profit', 'end']

function parseTrade(v: unknown): LabTrade | null {
  if (!isRecord(v)) return null
  const entryTime = num(v.entry_time)
  const entryPrice = num(v.entry_price)
  if (entryTime === null || entryPrice === null) return null
  const reason = EXIT_REASONS.includes(v.exit_reason as ExitReason)
    ? (v.exit_reason as ExitReason)
    : 'signal'
  return {
    entry_time: entryTime,
    entry_price: entryPrice,
    exit_time: num(v.exit_time),
    exit_price: num(v.exit_price),
    pnl: numOr(v.pnl, 0),
    return_pct: numOr(v.return_pct, 0),
    exit_reason: reason,
  }
}

function parseFold(v: unknown): WalkForwardFold | null {
  if (!isRecord(v)) return null
  const start = num(v.test_start)
  const end = num(v.test_end)
  if (start === null || end === null) return null
  return {
    fold: numOr(v.fold, 0),
    test_start: start,
    test_end: end,
    sharpe: num(v.sharpe),
    total_return: num(v.total_return),
    n_trades: numOr(v.n_trades, 0),
  }
}

function compact<T>(items: readonly unknown[], parse: (v: unknown) => T | null): T[] {
  const out: T[] = []
  for (const item of items) {
    const parsed = parse(item)
    if (parsed !== null) out.push(parsed)
  }
  return out
}

export const DEFAULT_DISCLAIMER =
  'Historical simulation, not forward-looking. Past performance does not predict future results.'

export function parseBacktestReport(v: unknown): BacktestReport {
  const r = record(v, 'backtest report')
  const symbol = r.symbol
  const interval = r.interval
  if (typeof symbol !== 'string' || typeof interval !== 'string') fail('backtest report')
  const window = record(r.window, 'backtest window')
  const start = num(window.start)
  const end = num(window.end)
  if (start === null || end === null) fail('backtest window')
  const costs = record(r.costs, 'cost settings')
  const metrics = record(r.metrics, 'metrics')
  const bench = record(r.benchmarks, 'benchmarks')
  const random = isRecord(bench.random_entry) ? bench.random_entry : {}
  const stats = record(r.stats, 'statistics')
  const dsr = isRecord(stats.dsr) ? stats.dsr : {}
  const pbo = isRecord(stats.pbo) ? stats.pbo : {}
  const ci = isRecord(stats.sharpe_ci) ? stats.sharpe_ci : {}
  const benchEq = isRecord(r.benchmark_equity) ? r.benchmark_equity : {}
  const disclaimer = str(r.disclaimer).trim()
  return {
    trial_id: numOr(r.trial_id, 0),
    run_id: str(r.run_id),
    plan_hash: str(r.plan_hash),
    plan: isRecord(r.plan) ? r.plan : {},
    symbol,
    interval,
    window: { kind: 'design', start, end },
    costs: {
      fee_rate: numOr(costs.fee_rate, 0),
      slippage_rate: numOr(costs.slippage_rate, 0),
      slippage_vol_mult: numOr(costs.slippage_vol_mult, 0),
    },
    metrics: {
      net: parseMetrics(metrics.net, 'net metrics'),
      gross: parseMetrics(metrics.gross, 'gross metrics'),
    },
    benchmarks: {
      buy_and_hold: parseMetrics(bench.buy_and_hold, 'buy-and-hold metrics'),
      vol_target: parseMetrics(bench.vol_target, 'vol-target metrics'),
      random_entry: {
        n_paths: numOr(random.n_paths, 0),
        sharpe_median: num(random.sharpe_median),
        sharpe_p95: num(random.sharpe_p95),
        percentile: num(random.percentile),
      },
    },
    stats: {
      dsr: {
        dsr: num(dsr.dsr),
        psr: num(dsr.psr),
        n_trials: numOr(dsr.n_trials, 0),
        sr0_annual: num(dsr.sr0_annual),
      },
      pbo: {
        pbo: num(pbo.pbo),
        n_trials: numOr(pbo.n_trials, 0),
        n_splits: numOr(pbo.n_splits, 0),
        ...(pbo.basis === 'neighbourhood' ? { basis: 'neighbourhood' as const } : {}),
      },
      sharpe_ci: { low: num(ci.low), high: num(ci.high), level: numOr(ci.level, 0.95) },
    },
    walk_forward: Array.isArray(r.walk_forward) ? compact(r.walk_forward, parseFold) : [],
    ...(r.walk_forward_kind === 'fixed_params'
      ? { walk_forward_kind: 'fixed_params' as const }
      : {}),
    trades: compact(list(r.trades, 'trade list'), parseTrade).sort(
      (a, b) => a.entry_time - b.entry_time,
    ),
    equity: parseEquity(r.equity),
    benchmark_equity: {
      buy_and_hold: Array.isArray(benchEq.buy_and_hold) ? parseEquity(benchEq.buy_and_hold) : [],
      vol_target: Array.isArray(benchEq.vol_target) ? parseEquity(benchEq.vol_target) : [],
    },
    warnings: strings(r.warnings),
    disclaimer: disclaimer === '' ? DEFAULT_DISCLAIMER : disclaimer,
    elapsed_ms: numOr(r.elapsed_ms, 0),
  }
}

export function parseLedgerItem(v: unknown): LedgerItem | null {
  if (!isRecord(v)) return null
  const id = num(v.id)
  const createdAt = num(v.created_at)
  if (id === null || createdAt === null) return null
  return {
    id,
    run_id: str(v.run_id),
    created_at: createdAt,
    source: str(v.source),
    status: v.status === 'error' ? 'error' : 'ok',
    plan_hash: str(v.plan_hash),
    plan_name: str(v.plan_name),
    symbol: str(v.symbol),
    interval: str(v.interval),
    window_kind: str(v.window_kind),
    sharpe: num(v.sharpe),
    total_return: num(v.total_return),
    max_drawdown: num(v.max_drawdown),
    n_trades: numOr(v.n_trades, 0),
    error: typeof v.error === 'string' ? v.error : null,
  }
}

function parseParam(v: unknown): LabFeatureParam | null {
  if (!isRecord(v) || typeof v.name !== 'string') return null
  return {
    name: v.name,
    kind: v.kind === 'float' ? 'float' : 'int',
    default: numOr(v.default, 0),
    low: numOr(v.low, 0),
    high: numOr(v.high, 0),
    description: str(v.description),
  }
}

function parseFeature(v: unknown): LabFeature | null {
  if (!isRecord(v) || typeof v.name !== 'string') return null
  return {
    name: v.name,
    description: str(v.description),
    outputs: strings(v.outputs),
    params: Array.isArray(v.params) ? compact(v.params, parseParam) : [],
  }
}

const REGIME_STATES: readonly RegimeState[] = [-1, 0, 1]

function parseRegimePoint(v: unknown): RegimePoint | null {
  if (!isRecord(v)) return null
  const time = num(v.time)
  if (time === null) return null
  const state = REGIME_STATES.includes(v.state as RegimeState) ? (v.state as RegimeState) : null
  return { time, state }
}

/** Points with a finite time, ascending with duplicates dropped; an unknown state reads as null. */
export function parseRegimeResponse(v: unknown): RegimeResponse {
  const r = record(v, 'regime answer')
  const points = compact(list(r.points, 'regime points'), parseRegimePoint)
  points.sort((a, b) => a.time - b.time)
  return {
    points: points.filter((p, i) => i === 0 || p.time !== points[i - 1].time),
    window_bars: numOr(r.window_bars, 0),
    threshold: numOr(r.threshold, 0),
    engine: str(r.engine),
  }
}

// ---------------------------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------------------------

async function readJson(res: Response): Promise<unknown> {
  try {
    return (await res.json()) as unknown
  } catch {
    return undefined
  }
}

/** Throws the LabError a non-2xx response stands for. */
async function errorFor(res: Response): Promise<never> {
  // The Vite proxy answers 502 (text/plain) when nothing listens on 8788; 504 = gateway timeout.
  if (res.status === 502 || res.status === 504) {
    throw new LabError('offline', LAB_OFFLINE_MESSAGE, res.status)
  }
  const body = await readJson(res)
  const detail = isRecord(body) && typeof body.detail === 'string' ? body.detail.trim() : ''
  if (res.status === 422) {
    const errors = isRecord(body) ? parsePlanErrors(body.errors) : []
    const first = errors[0]
    throw new LabError(
      'invalid',
      first ? `${first.path ? `${first.path}: ` : ''}${first.message}` : detail || 'Invalid plan',
      422,
      errors,
    )
  }
  if (res.status === 503) {
    if (detail !== '') throw new LabError('no-data', detail, 503)
    throw new LabError('offline', LAB_OFFLINE_MESSAGE, 503)
  }
  throw new LabError('http', detail || `The lab service answered HTTP ${res.status}`, res.status)
}

export function createLabClient(opts: LabClientOptions = {}): LabClient {
  const base = (opts.basePath ?? LAB_BASE_PATH).replace(/\/+$/, '')
  const timeoutMs = opts.timeoutMs ?? LAB_TIMEOUT_MS
  const backtestTimeoutMs = opts.backtestTimeoutMs ?? LAB_BACKTEST_TIMEOUT_MS
  const doFetch: FetchLike =
    opts.fetchImpl ??
    ((input, init) => {
      if (typeof globalThis.fetch !== 'function') {
        return Promise.reject(new Error('fetch is not available'))
      }
      return globalThis.fetch(input, init)
    })

  async function request(
    path: string,
    init: { method: 'GET' | 'POST'; body?: unknown; signal?: AbortSignal; timeoutMs?: number },
  ): Promise<unknown> {
    if (init.signal?.aborted) throw new LabError('aborted', 'Request cancelled')
    const controller = new AbortController()
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, init.timeoutMs ?? timeoutMs)
    const onAbort = (): void => controller.abort()
    init.signal?.addEventListener('abort', onAbort, { once: true })
    const reqInit: RequestInit = { method: init.method, signal: controller.signal }
    if (init.body !== undefined) {
      reqInit.headers = { 'Content-Type': 'application/json' }
      reqInit.body = JSON.stringify(init.body)
    }
    try {
      let res: Response
      try {
        res = await doFetch(`${base}${path}`, reqInit)
      } catch {
        if (timedOut) throw new LabError('timeout', 'The lab service did not answer in time')
        if (init.signal?.aborted) throw new LabError('aborted', 'Request cancelled')
        throw new LabError('offline', LAB_OFFLINE_MESSAGE)
      }
      if (!res.ok) await errorFor(res)
      const body = await readJson(res)
      if (body === undefined) throw new LabError('parse', 'The lab service sent a non-JSON answer')
      return body
    } finally {
      clearTimeout(timer)
      init.signal?.removeEventListener('abort', onAbort)
    }
  }

  return {
    async health(signal) {
      const body = record(await request('/health', { method: 'GET', signal }), 'health answer')
      return { ok: body.ok === true, version: str(body.version) }
    },
    async registry(signal) {
      const body = record(await request('/registry', { method: 'GET', signal }), 'registry')
      return { features: compact(list(body.features, 'feature list'), parseFeature) }
    },
    async validate(plan, signal) {
      let body: unknown
      try {
        body = await request('/validate', { method: 'POST', body: { plan }, signal })
      } catch (err) {
        // Some validators answer 422 with the same payload; treat it as a normal result.
        if (err instanceof LabError && err.kind === 'invalid') {
          return { ok: false, errors: err.errors }
        }
        throw err
      }
      const r = record(body, 'validation answer')
      const errors = parsePlanErrors(r.errors)
      return { ok: r.ok === true && errors.length === 0, errors }
    },
    async backtest(plan, o = {}) {
      const payload: { plan: LabPlan; run_id?: string } = { plan }
      if (o.runId !== undefined && o.runId !== '') payload.run_id = o.runId
      const body = await request('/backtest', {
        method: 'POST',
        body: payload,
        signal: o.signal,
        timeoutMs: backtestTimeoutMs,
      })
      return parseBacktestReport(body)
    },
    async ledger(query = {}, signal) {
      const params = new URLSearchParams()
      const runId = query.runId?.trim()
      if (runId) params.set('run_id', runId)
      const limit = Math.min(500, Math.max(1, Math.floor(query.limit ?? LEDGER_PAGE_SIZE)))
      params.set('limit', String(Number.isFinite(limit) ? limit : LEDGER_PAGE_SIZE))
      const offset = Math.max(0, Math.floor(query.offset ?? 0))
      params.set('offset', String(Number.isFinite(offset) ? offset : 0))
      const body = record(
        await request(`/ledger?${params.toString()}`, { method: 'GET', signal }),
        'ledger page',
      )
      const items = compact(list(body.items, 'ledger list'), parseLedgerItem)
      return { total: numOr(body.total, items.length), items }
    },
    async ledgerItem(id, signal) {
      if (!Number.isInteger(id) || id < 0) throw new LabError('http', `Invalid ledger id: ${id}`)
      const body = await request(`/ledger/${id}`, { method: 'GET', signal })
      const item = parseLedgerItem(body)
      if (item === null || !isRecord(body)) fail('ledger entry')
      return {
        ...item,
        plan: isRecord(body.plan) ? body.plan : {},
        metrics: isRecord(body.metrics) ? parseMetrics(body.metrics) : null,
      }
    },
    async regime(req, signal) {
      const payload: RegimeRequest = { times: req.times, closes: req.closes }
      if (req.window_bars !== undefined) payload.window_bars = req.window_bars
      if (req.threshold !== undefined) payload.threshold = req.threshold
      const body = await request('/regime', { method: 'POST', body: payload, signal })
      return parseRegimeResponse(body)
    },
  }
}
