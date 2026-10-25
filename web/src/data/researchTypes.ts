/**
 * Types of the lab's Phase 8 research endpoints (`GET /lab/research/...`, read-only). The
 * orchestrator in `agents/` writes run-state files, the idea library and the journal; the lab
 * API serves them. Unless noted otherwise, times are UNIX seconds (UTC); run-state timestamps
 * are ISO strings as the orchestrator writes them. Ratios are fractions (0.12 = 12%).
 *
 * Web-sourced text (idea sources, journal Markdown) is untrusted: render it as text only.
 */

import type { LabPlan } from './labTypes'

export type ResearchPhase =
  'starting' | 'scouting' | 'designing' | 'testing' | 'critic' | 'reporting' | 'done'

export type ResearchOutcome =
  'running' | 'candidate' | 'no_edge' | 'budget_exhausted' | 'error' | 'stopped'

/** The fixed per-run limits, frozen in the ledger when the run starts. */
export interface RunBudgetLimits {
  max_hypotheses: number | null
  max_proposals: number | null
  max_backtests: number | null
  max_holdout_checks: number | null
  max_usd: number | null
}

/** Ledger-side usage counters: `budget` (limit), `used`, `remaining`, per counter. */
export interface RunUsage {
  budget: Record<string, number>
  used: Record<string, number>
  remaining: Record<string, number>
}

export interface RunUsed {
  hypotheses: number
  proposals: number
  backtests: number
  holdout_checks: number
  usd: number
}

export interface RunEvent {
  /** As written by the orchestrator: an ISO string or a UNIX time; see `eventTimeMs`. */
  at: string | number | null
  kind: string
  message: string
}

/**
 * A plan that passed the design-window criteria and the locked holdout in a research run. The
 * event-driven cross-check is a separate criterion and may still be pending.
 */
export interface CandidateSummary {
  idea_id: number | null
  plan_hash: string
  /** Validated plan JSON, accepted by `POST /lab/backtest`. */
  plan: LabPlan
  trial_id: number | null
  /** Free text or a small record of headline numbers, as the orchestrator wrote it. */
  summary: string | Record<string, unknown> | null
}

export interface ResearchCandidate extends CandidateSummary {
  run_id: string
}

/** The orchestrator's progress file for one run (`agents/data/runs/<run_id>.json`). */
export interface RunState {
  run_id: string
  symbols: string[]
  interval: string
  /** ISO timestamps. */
  started_at: string
  updated_at: string
  phase: ResearchPhase
  outcome: ResearchOutcome
  /** Limits, keyed like `used` (hypotheses, proposals, …, usd). */
  budget: Record<string, number>
  used: RunUsed
  current_idea_id: number | null
  candidates: CandidateSummary[]
  journal_path: string | null
  events: RunEvent[]
}

export interface RunItem {
  run_id: string
  /** UNIX seconds. */
  created_at: number
  label: string
  config_sha256: string
  /** The frozen success bar, e.g. `{ dsr_min: 0.95, beat_buy_hold: true }`. */
  criteria: Record<string, number | boolean>
  budget: RunBudgetLimits
  usage: RunUsage | null
  /** null when no progress file exists for this run (e.g. an interactive MCP session). */
  state: RunState | null
}

export type IdeaStatus =
  'new' | 'testing' | 'abandoned' | 'promising' | 'candidate' | 'dead' | 'duplicate'

export interface IdeaSource {
  url: string
  title: string
  /** Always treat web content as untrusted; the flag is shown next to the link. */
  untrusted: boolean
}

export interface ResearchIdea {
  id: number
  /** UNIX seconds. */
  created_at: number
  run_id: string
  family: string
  title: string
  hypothesis: string
  rationale: string
  status: IdeaStatus
  verdict_note: string
  plan_hashes: string[]
  sources: IdeaSource[]
}

export interface IdeaQuery {
  runId?: string
  status?: IdeaStatus | ''
}

export interface JournalEntry {
  name: string
  title: string
  run_id: string
  date: string
  outcome: string
  idea_ids: number[]
  size: number
}

export interface JournalDocument {
  name: string
  markdown: string
}

export interface TradeStats {
  n_trades: number | null
  total_return: number | null
  win_rate: number | null
  mean_trade_return: number | null
}

export type PaperVerdict = 'consistent' | 'diverging' | 'insufficient_data'

/** A weekly forward (paper, dry-run) vs backtest comparison for one candidate. */
export interface PaperComparison {
  candidate: string
  plan_hash: string
  symbol: string
  interval: string
  period_start: number | null
  period_end: number | null
  /** ISO-8601 UTC, as `agents/research/paper/compare.py` writes it (not UNIX seconds). */
  generated_at: string | null
  forward: TradeStats
  backtest: TradeStats
  entry_match_rate: number | null
  mean_entry_slippage_bps: number | null
  verdict: PaperVerdict
  notes: string[]
}
