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
