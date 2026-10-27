/**
 * Types of the Python lab service (`lab/`, FastAPI on 127.0.0.1:8788, proxied at same-origin
 * `/lab`). All times are UNIX seconds (UTC bar open time). Non-finite numbers arrive as `null`.
 * Ratios and returns are fractions (0.12 = 12%), never percent.
 *
 * Backtests always run on the DESIGN window: the locked holdout is never exposed to the UI.
 */

/** A plan is free-form JSON for the UI; the lab validates it. */
export type LabPlan = Record<string, unknown>

export interface LabHealth {
  ok: boolean
  version: string
}

export interface LabFeatureParam {
  name: string
  kind: 'int' | 'float'
  default: number
  low: number
  high: number
  description: string
}

export interface LabFeature {
  name: string
  description: string
  outputs: string[]
  params: LabFeatureParam[]
}

export interface LabRegistry {
  features: LabFeature[]
}

/** One validation problem; `path` is a dotted location in the plan, e.g. `features.ema_fast`. */
export interface PlanError {
  path: string
  message: string
}

export interface ValidationResult {
  ok: boolean
  errors: PlanError[]
}

export interface LabMetrics {
  total_return: number | null
  cagr: number | null
  volatility: number | null
  sharpe: number | null
  sortino: number | null
  max_drawdown: number | null
  win_rate: number | null
  profit_factor: number | null
  n_trades: number
  exposure: number | null
  turnover: number | null
  bars: number
}

/** Normalised equity: 1.0 at the window start. */
export interface EquityPoint {
  time: number
  value: number
}

export type ExitReason = 'signal' | 'stop_loss' | 'take_profit' | 'end'

export interface LabTrade {
  entry_time: number
  entry_price: number
  /** null while the trade is still open at the window end. */
  exit_time: number | null
  exit_price: number | null
  pnl: number
  /** Fraction (0.05 = +5%), after costs. */
  return_pct: number
  exit_reason: ExitReason
}

export interface RandomEntryStats {
  n_paths: number
  sharpe_median: number | null
  sharpe_p95: number | null
  /** Where the strategy's Sharpe falls among the random-entry paths, 0–100. */
  percentile: number | null
}

export interface WalkForwardFold {
  fold: number
  test_start: number
  test_end: number
  sharpe: number | null
  total_return: number | null
  n_trades: number
}

export interface BacktestReport {
  trial_id: number
  run_id: string
  plan_hash: string
  plan: LabPlan
  symbol: string
  interval: string
  window: { kind: 'design'; start: number; end: number }
  costs: { fee_rate: number; slippage_rate: number; slippage_vol_mult: number }
  metrics: { net: LabMetrics; gross: LabMetrics }
  benchmarks: {
    buy_and_hold: LabMetrics
    vol_target: LabMetrics
    random_entry: RandomEntryStats
  }
  stats: {
    dsr: { dsr: number | null; psr: number | null; n_trials: number; sr0_annual: number | null }
    pbo: {
      pbo: number | null
      n_trials: number
      n_splits: number
      /**
       * 'neighbourhood': PBO over the ±20% parameter neighbourhood of this plan, which measures
       * parameter stability, not search overfitting. Absent on older lab responses.
       */
      basis?: 'neighbourhood'
    }
    sharpe_ci: { low: number | null; high: number | null; level: number }
  }
  walk_forward: WalkForwardFold[]
  /**
   * 'fixed_params': every fold scores the same parameters (nothing is re-fitted per fold), so
   * the folds are a stability check, not out-of-sample evidence. Absent on older lab responses.
   */
  walk_forward_kind?: 'fixed_params'
  trades: LabTrade[]
  equity: EquityPoint[]
  benchmark_equity: { buy_and_hold: EquityPoint[]; vol_target: EquityPoint[] }
  warnings: string[]
  disclaimer: string
  elapsed_ms: number
}

export interface LedgerItem {
  id: number
  run_id: string
  /** UNIX seconds. */
  created_at: number
  source: string
  status: 'ok' | 'error'
  plan_hash: string
  plan_name: string
  symbol: string
  interval: string
  window_kind: string
  sharpe: number | null
  total_return: number | null
  max_drawdown: number | null
  n_trades: number
  error: string | null
}

export interface LedgerPage {
  total: number
  items: LedgerItem[]
}

export interface LedgerDetail extends LedgerItem {
  plan: LabPlan
  metrics: LabMetrics | null
}

export interface LedgerQuery {
  runId?: string
  limit?: number
  offset?: number
}

/** hedgeFund regime of a bar: -1 Bear, 0 Sideways, +1 Bull. */
export type RegimeState = -1 | 0 | 1

/** `state` is null during the warm-up (fewer than `window_bars` earlier bars sent). */
export interface RegimePoint {
  time: number
  state: RegimeState | null
}

/**
 * Closed bars to label (never the forming last bar): ascending unique UNIX-second times and
 * their closes (> 0), at most 20 000. Each label uses only closes at or before its bar.
 */
export interface RegimeRequest {
  times: number[]
  closes: number[]
  /** Bars in the trailing return, 1..5000 (lab default 20). */
  window_bars?: number
  /** Trailing-return threshold as a fraction, 0.001..1 (lab default 0.05). */
  threshold?: number
}

export interface RegimeResponse {
  /** One point per requested bar, same order. */
  points: RegimePoint[]
  window_bars: number
  threshold: number
  /** The hedgeFund function that labels the bars. */
  engine: string
}

/**
 * One derivatives value per requested bar. `value` is null when nothing was visible at that
 * bar's close (a gap, or outside the downloaded archive range): drawn as whitespace, never
 * interpolated.
 */
export interface DerivativesPoint {
  time: number
  value: number | null
}

/**
 * Historical USDⓈ-M funding and open interest aligned to spot bars (`POST /lab/derivatives`).
 * Times: closed bar OPEN times (UNIX seconds), strictly ascending, at most MAX_DERIVATIVES_BARS.
 */
export interface DerivativesRequest {
  symbol: string
  times: number[]
  /** Bar length, 60..604800 seconds. */
  bar_seconds: number
}

/** First / last archive timestamp of a series (UNIX seconds), both null when none is downloaded. */
export interface DerivativesRange {
  first: number | null
  last: number | null
}

export interface DerivativesResponse {
  symbol: string
  bar_seconds: number
  /** Funding rate as a fraction (0.0001 = 0.01%); one point per requested time, same order. */
  funding: DerivativesPoint[]
  /** Open interest value in USD notional; one point per requested time, same order. */
  open_interest: DerivativesPoint[]
  available: { funding: DerivativesRange; open_interest: DerivativesRange }
  /** e.g. "data.binance.vision futures/um archive (historical; no live stream)". */
  source: string
}
