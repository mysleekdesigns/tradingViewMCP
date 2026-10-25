/**
 * Pure helpers behind the Research page: outcome and status labels, budget meters, the live
 * polling decision, event formatting, a small Markdown → safe blocks parser for the journal, and
 * the candidate → Strategy Tester run mapping. No React, no DOM, no network.
 *
 * Journal Markdown and idea sources are partly untrusted web text. The parser only produces
 * plain-text blocks; React renders them as escaped text (never as HTML).
 */

import { LabError } from '../data/labApi'
import type { LabPlan } from '../data/labTypes'
import { FASTAPI_NOT_FOUND, RESEARCH_OFFLINE_MESSAGE, httpStatusMessage } from '../data/researchApi'
import type {
  IdeaStatus,
  JournalEntry,
  PaperComparison,
  PaperVerdict,
  ResearchIdea,
  ResearchOutcome,
  ResearchPhase,
  RunEvent,
  RunItem,
  RunState,
} from '../data/researchTypes'
import type { Interval } from '../types'
import { EMPTY, fmtInt, fmtPct, fmtRatio, isChartInterval } from './backtestView'

// ---------------------------------------------------------------------------------------------
// Tabs (persisted so a candidate click → chart → back returns to the same tab)
// ---------------------------------------------------------------------------------------------

export type ResearchTab = 'runs' | 'ideas' | 'ledger' | 'journal' | 'candidates'

export const RESEARCH_TABS: readonly { id: ResearchTab; label: string }[] = [
  { id: 'runs', label: 'Runs' },
  { id: 'ideas', label: 'Ideas' },
  { id: 'ledger', label: 'Ledger' },
  { id: 'journal', label: 'Journal' },
  { id: 'candidates', label: 'Candidates' },
]

export const RESEARCH_TAB_KEY = 'tv.research.tab.v1'

export interface TabStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

function defaultStorage(): TabStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function loadResearchTab(storage: TabStorage | null = defaultStorage()): ResearchTab {
  try {
    const v = storage?.getItem(RESEARCH_TAB_KEY)
    return RESEARCH_TABS.some((t) => t.id === v) ? (v as ResearchTab) : 'runs'
  } catch {
    return 'runs'
  }
}

export function saveResearchTab(
  tab: ResearchTab,
  storage: TabStorage | null = defaultStorage(),
): void {
  try {
    storage?.setItem(RESEARCH_TAB_KEY, tab)
  } catch {
    // Private mode / quota: the tab simply is not remembered.
  }
}

/**
 * A tab (or the whole view) failed to render: remember Runs instead, so reopening the view or
 * pressing Reload never lands on the tab that just threw.
 */
export function resetResearchTab(storage: TabStorage | null = defaultStorage()): void {
  saveResearchTab('runs', storage)
}

/**
 * The journal entry a run's "Open journal entry" asked for applies only to that one visit:
 * leaving the Journal tab drops it, so a later Journal click shows the newest entry.
 */
export function journalTargetAfterTabChange(
  target: string | null,
  nextTab: ResearchTab,
): string | null {
  return nextTab === 'journal' ? target : null
}

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

export interface ResearchErrorView {
  kind: 'offline' | 'missing' | 'aborted' | 'error'
  title: string
  message: string
}

export function describeResearchError(err: unknown): ResearchErrorView {
  if (err instanceof LabError) {
    switch (err.kind) {
      case 'offline':
        return {
          kind: 'offline',
          title: 'Lab service not running',
          message: RESEARCH_OFFLINE_MESSAGE,
        }
      case 'aborted':
        return { kind: 'aborted', title: 'Cancelled', message: 'Request cancelled' }
      case 'timeout':
        return {
          kind: 'error',
          title: 'The lab service is slow',
          message: 'The lab service did not answer in time. It retries on the next refresh.',
        }
      case 'http':
        if (err.status === 404) {
          // FastAPI's own "Not Found" (or a bare 404) means the route itself is missing: an old
          // lab process. A handler's detail ("journal entry not found") is the real answer.
          if (err.message === FASTAPI_NOT_FOUND || err.message === httpStatusMessage(404)) {
            return {
              kind: 'missing',
              title: 'Research endpoints not found',
              message:
                'This lab service has no /research endpoints. Restart it from an up-to-date checkout: cd lab && uv run python -m lab.api',
            }
          }
          return { kind: 'error', title: 'Not found', message: err.message }
        }
        return { kind: 'error', title: 'The lab service returned an error', message: err.message }
      default:
        return { kind: 'error', title: 'Unexpected answer from the lab', message: err.message }
    }
  }
  const message = err instanceof Error && err.message ? err.message : 'Unknown error'
  return { kind: 'error', title: 'Something went wrong', message }
}

// ---------------------------------------------------------------------------------------------
// Outcomes, phases, statuses
// ---------------------------------------------------------------------------------------------

/**
 * `calm` = a correct, finished outcome that found nothing (no edge, budget used up). It is styled
 * as respectable and neutral, never as an error: an honest "no edge found" is the expected result.
 */
export type ResearchTone = 'live' | 'good' | 'calm' | 'neutral' | 'warn' | 'bad'

export interface OutcomeView {
  label: string
  tone: ResearchTone
  description: string
}

/**
 * What a candidate has and has not shown. The event-driven cross-check is a separate criterion
 * that can still be pending, so a candidate is never described as having passed everything.
 */
export const CANDIDATE_STILL_NEEDS =
  'It counts as successful only after the event-driven cross-check (unless shown as passed) and 4–8 weeks of forward paper trading that matches the backtest.'

/** The Candidates tab's one-line explanation. */
export const CANDIDATE_EXPLAINER = `A candidate passed the design-window criteria and the locked holdout. ${CANDIDATE_STILL_NEEDS}`

const OUTCOMES: Record<ResearchOutcome, OutcomeView> = {
  running: {
    label: 'Running',
    tone: 'live',
    description: 'The research loop is working. This page refreshes every few seconds.',
  },
  candidate: {
    label: 'Candidate found',
    tone: 'good',
    description: `A plan passed the design-window criteria and the locked holdout. ${CANDIDATE_STILL_NEEDS}`,
  },
  no_edge: {
    label: 'No edge found',
    tone: 'calm',
    description:
      'The run finished correctly: nothing cleared the fixed success bar. Most honest research ends here.',
  },
  budget_exhausted: {
    label: 'Budget used — no edge',
    tone: 'calm',
    description:
      'The run spent its fixed trial budget without a plan clearing the success bar. That is a valid, finished result.',
  },
  error: {
    label: 'Error',
    tone: 'bad',
    description: 'The run stopped because of an error. See the latest events.',
  },
  stopped: {
    label: 'Stopped',
    tone: 'neutral',
    description: 'The run was stopped before it finished.',
  },
}

const NO_STATE: OutcomeView = {
  label: 'No live progress',
  tone: 'neutral',
  description:
    'This run has ledger entries but no progress file (for example an interactive /research-strategy session).',
}

export function outcomeView(outcome: ResearchOutcome | null | undefined): OutcomeView {
  return outcome ? OUTCOMES[outcome] : NO_STATE
}

export const PHASE_STEPS: readonly { id: ResearchPhase; label: string }[] = [
  { id: 'starting', label: 'Start' },
  { id: 'scouting', label: 'Scout' },
  { id: 'designing', label: 'Design' },
  { id: 'testing', label: 'Test' },
  { id: 'critic', label: 'Critic' },
  { id: 'reporting', label: 'Report' },
  { id: 'done', label: 'Done' },
]

export type StepState = 'done' | 'current' | 'todo'

/** The loop's phase as a stepper; a finished run (outcome not running) shows every step done. */
export function phaseSteps(
  phase: ResearchPhase,
  outcome: ResearchOutcome,
): { id: ResearchPhase; label: string; state: StepState }[] {
  const at = PHASE_STEPS.findIndex((s) => s.id === phase)
  const finished = outcome !== 'running' || phase === 'done'
  return PHASE_STEPS.map((s, i) => ({
    ...s,
    state: finished
      ? i <= at || phase === 'done'
        ? 'done'
        : 'todo'
      : i < at
        ? 'done'
        : i === at
          ? 'current'
          : 'todo',
  }))
}

const IDEA_STATUS: Record<IdeaStatus, { label: string; tone: ResearchTone }> = {
  new: { label: 'New', tone: 'neutral' },
  testing: { label: 'Testing', tone: 'live' },
  promising: { label: 'Promising', tone: 'warn' },
  candidate: { label: 'Candidate', tone: 'good' },
  abandoned: { label: 'Abandoned', tone: 'calm' },
  dead: { label: 'Dead', tone: 'calm' },
  duplicate: { label: 'Duplicate', tone: 'neutral' },
}

export function ideaStatusView(status: IdeaStatus): { label: string; tone: ResearchTone } {
  return IDEA_STATUS[status]
}

const VERDICTS: Record<PaperVerdict, OutcomeView> = {
  consistent: {
    label: 'Consistent',
    tone: 'good',
    description: 'Forward paper trades match the backtest within tolerance.',
  },
  diverging: {
    label: 'Diverging',
    tone: 'bad',
    description:
      'Forward paper trades differ from the backtest. Treat the backtest with suspicion.',
  },
  insufficient_data: {
    label: 'Not enough trades yet',
    tone: 'neutral',
    description: 'Too few forward trades to compare. Keep paper trading.',
  },
}

export function verdictView(v: PaperVerdict): OutcomeView {
  return VERDICTS[v]
}

/** When a comparison was made, in epoch ms: `generated_at` (ISO), else the period end. */
function paperTimeMs(c: PaperComparison): number {
  return toEpochMs(c.generated_at) ?? toEpochMs(c.period_end) ?? 0
}

/** Comparisons for one candidate's plan, newest first. */
export function paperFor(
  comparisons: readonly PaperComparison[],
  planHash: string,
): PaperComparison[] {
  if (planHash === '') return []
  return comparisons
    .filter((c) => c.plan_hash === planHash)
    .sort((a, b) => paperTimeMs(b) - paperTimeMs(a))
}

/** Comparisons whose plan is not among the listed candidates. */
export function unmatchedPaper(
  comparisons: readonly PaperComparison[],
  planHashes: ReadonlySet<string>,
): PaperComparison[] {
  return comparisons.filter((c) => !planHashes.has(c.plan_hash))
}

// ---------------------------------------------------------------------------------------------
// Polling and freshness
// ---------------------------------------------------------------------------------------------

export const RESEARCH_POLL_MS = 5000
/** A running run whose progress file has not changed for this long looks stalled. */
export const RESEARCH_STALE_MS = 10 * 60_000

/** While the lab is unreachable, look for it again this often. */
export const RESEARCH_OFFLINE_RECHECK_MS = 8000

/** Nothing is running (or the list is empty): look for new runs this often. */
export const RESEARCH_IDLE_POLL_MS = 30_000

/** Some run is live (outcome running); finished runs never change. */
export function shouldPoll(runs: readonly RunItem[] | null): boolean {
  return runs !== null && runs.some((r) => r.state?.outcome === 'running')
}

/**
 * Some running run is still making progress. A run that has looked stalled for over
 * `RESEARCH_STALE_MS` does not keep the page polling: the user re-checks it by hand.
 */
export function hasFreshLiveRun(runs: readonly RunItem[] | null, nowMs: number): boolean {
  return (
    runs !== null && runs.some((r) => r.state?.outcome === 'running' && !isStale(r.state, nowMs))
  )
}

/**
 * Why the run list is being scheduled: the view just opened, a request just settled, or the
 * browser tab just became visible again.
 */
export type RunsPollTrigger = 'initial' | 'response' | 'visible'

export interface RunsPollInput {
  runs: readonly RunItem[] | null
  /** The last request failed because the lab is unreachable. */
  offline: boolean
  /** The browser tab is hidden (`document.hidden`). */
  hidden: boolean
  nowMs: number
  /** Defaults to `'response'`. */
  trigger?: RunsPollTrigger
}

/**
 * How long to wait before asking for the run list again: 0 = now, null = no timer.
 * One request at a time: after a response, the next one is only scheduled once it settled.
 *
 * - Hidden tab: nothing (the visibility handler catches up on return).
 * - Opening the view, or the tab becoming visible again: ask at once. The list held then may be
 *   many minutes old, so its `updated_at` says nothing about whether a run is still live; the
 *   fresh answer decides whether polling continues.
 * - After an answer: offline → 8 s; a running run making progress → 5 s; a running run that
 *   looks stalled (and nothing fresh), nothing running, an empty list, or an error → slow 30 s
 *   check, so a new or resumed run still shows up (an interrupted run stays "running" until it
 *   is resumed, so a stalled run must never stop the list refreshing).
 */
export function runsPollDelay({
  runs,
  offline,
  hidden,
  nowMs,
  trigger = 'response',
}: RunsPollInput): number | null {
  if (hidden) return null
  if (trigger !== 'response') return 0
  if (offline) return RESEARCH_OFFLINE_RECHECK_MS
  if (hasFreshLiveRun(runs, nowMs)) return RESEARCH_POLL_MS
  return RESEARCH_IDLE_POLL_MS
}

/** What the header's live chip says. */
export type LiveChipState = 'live' | 'paused' | 'idle' | 'offline'

export function liveChipState(
  runs: readonly RunItem[] | null,
  offline: boolean,
  nowMs: number,
): LiveChipState {
  if (offline) return 'offline'
  if (!shouldPoll(runs)) return 'idle'
  return hasFreshLiveRun(runs, nowMs) ? 'live' : 'paused'
}

const secs = (ms: number): string => `${Math.round(ms / 1000)} s`

/** The Runs tab's note on how the list refreshes itself. */
export function runsRefreshNote(chip: LiveChipState): string {
  switch (chip) {
    case 'live':
      return `Auto-refresh every ${secs(RESEARCH_POLL_MS)}`
    case 'paused':
      return `Run stalled · checks for new runs every ${secs(RESEARCH_IDLE_POLL_MS)}`
    case 'offline':
      return `Lab offline · retrying every ${secs(RESEARCH_OFFLINE_RECHECK_MS)}`
    case 'idle':
      return `Checks for new runs every ${secs(RESEARCH_IDLE_POLL_MS)}`
  }
}

/** An ISO string or a UNIX time (seconds or ms) as epoch ms, else null. */
export function toEpochMs(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) {
    // UNIX seconds stay below 1e11 until the year 5138; ms are above it.
    return v < 1e11 ? v * 1000 : v
  }
  if (typeof v === 'string' && v.trim() !== '') {
    const t = Date.parse(v)
    return Number.isNaN(t) ? null : t
  }
  return null
}

export function isStale(state: RunState | null, nowMs: number): boolean {
  if (state === null || state.outcome !== 'running') return false
  const updated = toEpochMs(state.updated_at)
  return updated !== null && nowMs - updated > RESEARCH_STALE_MS
}

/** "just now" / "4 min ago" / "3 h ago" / "2 d ago"; future times read "just now". */
export function relativeTime(ms: number | null, nowMs: number): string {
  if (ms === null) return EMPTY
  const s = Math.max(0, (nowMs - ms) / 1000)
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`
  if (s < 86_400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86_400)} d ago`
}

/** Year 1 … 9999 in epoch ms: outside it `Date#toISOString` throws or prints ±YYYYYY. */
const MIN_ISO_MS = -62_135_596_800_000
const MAX_ISO_MS = 253_402_300_799_999

/** `new Date(ms).toISOString()`, or null for a non-finite or out-of-range time. */
function isoOf(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < MIN_ISO_MS || ms > MAX_ISO_MS) {
    return null
  }
  return new Date(ms).toISOString()
}

/** "2026-10-24 14:03 UTC", or "—" for a missing, non-finite or out-of-range time. */
export function fmtUtc(ms: number | null): string {
  const iso = isoOf(ms)
  return iso === null ? EMPTY : `${iso.slice(0, 16).replace('T', ' ')} UTC`
}

/** "14:03:05 UTC" from epoch ms, or "—" for an unusable time. */
export function fmtClockUtc(ms: number | null): string {
  const iso = isoOf(ms)
  return iso === null ? EMPTY : `${iso.slice(11, 19)} UTC`
}

/** "2026-10-24" (UTC) from UNIX seconds; "—" when the time is unusable (never throws). */
export function fmtDate(timeSeconds: number | null): string {
  const iso = isoOf(timeSeconds === null ? null : timeSeconds * 1000)
  return iso === null ? EMPTY : iso.slice(0, 10)
}

/** "2026-10-24 14:03" (UTC) from UNIX seconds; "—" when the time is unusable. */
export function fmtDateTime(timeSeconds: number | null): string {
  const iso = isoOf(timeSeconds === null ? null : timeSeconds * 1000)
  return iso === null ? EMPTY : iso.slice(0, 16).replace('T', ' ')
}

// ---------------------------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------------------------

export interface EventRow {
  key: string
  /** "14:03:05" (UTC) or "—". */
  time: string
  /** Full timestamp for a tooltip. */
  title: string
  kind: string
  tone: ResearchTone
  message: string
}

const EVENT_TONES: Record<string, ResearchTone> = {
  error: 'bad',
  fail: 'bad',
  failed: 'bad',
  warn: 'warn',
  warning: 'warn',
  candidate: 'good',
  pass: 'good',
  passed: 'good',
  phase: 'live',
  start: 'live',
  no_edge: 'calm',
  abandon: 'calm',
  abandoned: 'calm',
}

export function eventRow(e: RunEvent, index: number): EventRow {
  const ms = toEpochMs(e.at)
  const kind = e.kind.trim().toLowerCase() || 'info'
  return {
    key: `${index}:${String(e.at)}:${kind}`,
    time: isoOf(ms)?.slice(11, 19) ?? EMPTY,
    title: fmtUtc(ms),
    kind: kind.replace(/_/g, ' '),
    tone: EVENT_TONES[kind] ?? 'neutral',
    message: e.message,
  }
}

/** The latest `limit` events, newest first. */
export function recentEvents(state: RunState | null, limit = 40): EventRow[] {
  if (state === null) return []
  const rows = state.events.map(eventRow)
  return rows.slice(-limit).reverse()
}

// ---------------------------------------------------------------------------------------------
// Budget meters
// ---------------------------------------------------------------------------------------------

export interface BudgetMeter {
  key: string
  label: string
  used: number | null
  limit: number | null
  /** 0–100, or null when there is no limit to compare with. */
  pct: number | null
  level: 'ok' | 'high' | 'full' | 'none'
  text: string
}

const METER_DEFS: readonly {
  key: 'hypotheses' | 'proposals' | 'backtests' | 'holdout_checks' | 'usd'
  label: string
  limitKey: keyof RunItem['budget']
  /** The lab counts and enforces this one itself (`RunItem.usage`); the rest are self-reported. */
  enforced: boolean
}[] = [
  { key: 'hypotheses', label: 'Hypotheses', limitKey: 'max_hypotheses', enforced: false },
  { key: 'proposals', label: 'Plan proposals', limitKey: 'max_proposals', enforced: true },
  { key: 'backtests', label: 'Backtests', limitKey: 'max_backtests', enforced: true },
  {
    key: 'holdout_checks',
    label: 'Holdout checks',
    limitKey: 'max_holdout_checks',
    enforced: true,
  },
  { key: 'usd', label: 'Model spend', limitKey: 'max_usd', enforced: false },
]

function firstNumber(...vs: (number | null | undefined)[]): number | null {
  for (const v of vs) if (typeof v === 'number' && Number.isFinite(v)) return v
  return null
}

function money(v: number): string {
  return `$${v.toFixed(2)}`
}

/**
 * Used vs limit per budget counter. Proposals, backtests and holdout checks come from the lab's
 * own server-enforced counters (`item.usage`, then the frozen limits) whenever the lab sent them:
 * the orchestrator's progress file can lag or be wrong. Hypotheses and model spend exist only in
 * the progress file. Without `usage`, every counter falls back to the progress file first.
 */
export function budgetMeters(item: RunItem): BudgetMeter[] {
  const state = item.state
  const usage = item.usage
  const out: BudgetMeter[] = []
  for (const d of METER_DEFS) {
    const server = d.enforced && usage !== null
    const used = server
      ? firstNumber(usage.used[d.key], state?.used[d.key])
      : firstNumber(state?.used[d.key], usage?.used[d.key])
    const limit = server
      ? firstNumber(
          usage.budget[d.key],
          item.budget[d.limitKey],
          state?.budget[d.key],
          state?.budget[d.limitKey],
        )
      : firstNumber(
          state?.budget[d.key],
          state?.budget[d.limitKey],
          item.budget[d.limitKey],
          usage?.budget[d.key],
        )
    if (used === null && limit === null) continue
    const pct =
      limit !== null && limit > 0 && used !== null
        ? Math.min(100, Math.max(0, (used / limit) * 100))
        : limit === 0 && used !== null
          ? 100
          : null
    const level = pct === null ? 'none' : pct >= 100 ? 'full' : pct >= 80 ? 'high' : 'ok'
    const fmt = d.key === 'usd' ? money : (v: number) => fmtInt(v)
    const usedText = used === null ? EMPTY : fmt(used)
    const text = limit === null ? `${usedText} (no limit)` : `${usedText} / ${fmt(limit)}`
    out.push({ key: d.key, label: d.label, used, limit, pct, level, text })
  }
  return out
}

// ---------------------------------------------------------------------------------------------
// Success criteria (the frozen bar)
// ---------------------------------------------------------------------------------------------

const CRITERIA_LABELS: Record<string, string> = {
  wf_oos_sharpe_min: 'Walk-forward OOS Sharpe ≥',
  dsr_min: 'Deflated Sharpe ≥',
  pbo_max: 'PBO ≤',
  beat_buy_hold: 'Beats buy-and-hold (Sharpe)',
  beat_vol_target: 'Beats vol-targeted hold (Sharpe)',
  random_entry_percentile_min: 'Random-entry percentile ≥',
  cost_stress_factor: 'Still profitable at costs ×',
  require_gradual_delay_decay: 'Gradual decay under signal delay',
  max_drawdown_max: 'Max drawdown ≤',
  min_trades: 'Trades ≥',
  min_profitable_regimes: 'Profitable regimes (of 3) ≥',
  neighbourhood_pct: 'Parameter neighbours profitable at ±',
  require_holdout_pass: 'Passes the holdout',
  require_crosscheck_match: 'Cross-check engine agrees',
}

/**
 * Parameters stored with the frozen criteria that define how a criterion is measured (how the
 * regimes are labelled), not a pass/fail bar of their own. Shown apart, as definitions.
 */
const CRITERIA_DEFINITIONS: Record<string, { label: string; fmt(v: number): string }> = {
  regime_window_days: {
    label: 'Regime window',
    fmt: (v) => `${fmtInt(v)} ${v === 1 ? 'day' : 'days'}`,
  },
  regime_threshold: {
    label: 'Regime threshold',
    // A fraction (0.10 = ±10%); a value above 1 is already in percent.
    fmt: (v) => `±${Number((v <= 1 ? v * 100 : v).toFixed(2))}%`,
  },
}

/** The regime-labelling definitions among the frozen criteria (not pass/fail rows). */
export function criteriaDefinitions(
  criteria: Record<string, number | boolean>,
): { key: string; label: string; value: string }[] {
  const out: { key: string; label: string; value: string }[] = []
  for (const [key, v] of Object.entries(criteria)) {
    const def = CRITERIA_DEFINITIONS[key]
    if (def === undefined) continue
    out.push({ key, label: def.label, value: typeof v === 'number' ? def.fmt(v) : String(v) })
  }
  return out
}

function humanize(key: string): string {
  const s = key.replace(/_/g, ' ').trim()
  return s === '' ? key : s[0].toUpperCase() + s.slice(1)
}

export function criteriaRows(
  criteria: Record<string, number | boolean>,
): { key: string; label: string; value: string }[] {
  return Object.entries(criteria)
    .filter(([key]) => !(key in CRITERIA_DEFINITIONS))
    .map(([key, v]) => {
      let value: string
      if (typeof v === 'boolean') value = v ? 'Required' : 'Off'
      else if (key === 'max_drawdown_max') value = fmtPct(v, 0)
      else if (key === 'neighbourhood_pct') value = `${v <= 1 ? Math.round(v * 100) : v}%`
      else if (key === 'cost_stress_factor') value = `${v}×`
      else value = Number.isInteger(v) ? fmtInt(v) : fmtRatio(v)
      return { key, label: CRITERIA_LABELS[key] ?? humanize(key), value }
    })
}

// ---------------------------------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------------------------------

export interface CandidateRunRequest {
  plan: LabPlan
  /** Pretty JSON for the Strategy Tester editor. */
  planText: string
  name: string
  symbol: string
  /** The plan's interval when the chart has it, else null (the chart keeps its interval). */
  chartInterval: Interval | null
  interval: string
}

export type CandidateMapping =
  { ok: true; request: CandidateRunRequest } | { ok: false; reason: string }

/** A candidate's plan as a Strategy Tester run: load it, switch the chart, run it. */
export function candidateRunRequest(c: { plan: LabPlan }): CandidateMapping {
  const plan = c.plan
  const symbol = typeof plan.symbol === 'string' ? plan.symbol.trim().toUpperCase() : ''
  const interval = typeof plan.interval === 'string' ? plan.interval.trim() : ''
  if (symbol === '') return { ok: false, reason: 'The plan has no symbol' }
  if (interval === '') return { ok: false, reason: 'The plan has no interval' }
  const name = typeof plan.name === 'string' && plan.name.trim() !== '' ? plan.name.trim() : 'plan'
  return {
    ok: true,
    request: {
      plan,
      planText: JSON.stringify(plan, null, 2),
      name,
      symbol,
      interval,
      chartInterval: isChartInterval(interval) ? interval : null,
    },
  }
}

/**
 * A candidate's plan replaced the user's own Strategy Tester plan: `previous` is the user's text
 * (offered back with "Restore previous plan"), `loaded` the candidate text that replaced it.
 */
export interface PlanRestore {
  previous: string
  loaded: string
}

export interface TesterPlan {
  planText: string
  /** false = the untouched template, which nothing needs to restore. */
  planEdited: boolean
}

/**
 * What to offer back after loading `nextText` into the tester. Only an edited plan that differs
 * is worth restoring. While an earlier offer is still valid (the editor still holds the plan it
 * loaded), loading another candidate keeps the user's original text, not the first candidate's.
 */
export function planRestoreAfterLoad(
  current: TesterPlan,
  pending: PlanRestore | null,
  nextText: string,
): PlanRestore | null {
  if (pending !== null && planRestoreValid(current, pending)) {
    return pending.previous === nextText ? null : { previous: pending.previous, loaded: nextText }
  }
  if (!current.planEdited || current.planText.trim() === '' || current.planText === nextText) {
    return null
  }
  return { previous: current.planText, loaded: nextText }
}

/** The offer still applies: the editor holds exactly the plan that replaced the user's. */
export function planRestoreValid(current: TesterPlan, pending: PlanRestore | null): boolean {
  return pending !== null && current.planEdited && current.planText === pending.loaded
}

/**
 * The pending "Restore previous plan" offer is kept in localStorage so a page reload while the
 * notice is up does not lose the user's own plan (the tester plan itself is persisted already).
 */
export const PLAN_RESTORE_KEY = 'tv.research.planRestore.v1'

export interface RemovableStorage extends TabStorage {
  removeItem(key: string): void
}

function defaultRemovableStorage(): RemovableStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function loadPlanRestore(
  storage: RemovableStorage | null = defaultRemovableStorage(),
): PlanRestore | null {
  try {
    const raw = storage?.getItem(PLAN_RESTORE_KEY)
    if (raw === null || raw === undefined || raw === '') return null
    const v: unknown = JSON.parse(raw)
    if (typeof v !== 'object' || v === null) return null
    const { previous, loaded } = v as Record<string, unknown>
    if (typeof previous !== 'string' || typeof loaded !== 'string' || previous.trim() === '') {
      return null
    }
    return { previous, loaded }
  } catch {
    return null
  }
}

/** Store the offer, or remove it when there is none (null). Never throws. */
export function savePlanRestore(
  value: PlanRestore | null,
  storage: RemovableStorage | null = defaultRemovableStorage(),
): void {
  try {
    if (value === null) storage?.removeItem(PLAN_RESTORE_KEY)
    else storage?.setItem(PLAN_RESTORE_KEY, JSON.stringify(value))
  } catch {
    // Private mode / quota: the offer lasts only until the page reloads.
  }
}

const SUMMARY_LABELS: Record<string, string> = {
  sharpe: 'Sharpe',
  dsr: 'DSR',
  psr: 'PSR',
  pbo: 'PBO',
  wf_oos_sharpe: 'WF OOS Sharpe',
  total_return: 'Total return',
  cagr: 'CAGR',
  max_drawdown: 'Max drawdown',
  n_trades: 'Trades',
  win_rate: 'Win rate',
  profit_factor: 'Profit factor',
  exposure: 'Exposure',
}

const PCT_KEY = /(return|drawdown|win_rate|exposure|cagr|volatility)/

function summaryValue(key: string, v: unknown): string | null {
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return EMPTY
    if (PCT_KEY.test(key)) return fmtPct(v)
    return Number.isInteger(v) ? fmtInt(v) : fmtRatio(v)
  }
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'string') return v
  if (v === null) return EMPTY
  return null
}

/** A candidate's summary: free text, or label/value pairs for a record of scalars. */
export function summaryView(summary: string | Record<string, unknown> | null): {
  text: string | null
  items: { key: string; label: string; value: string }[]
} {
  if (summary === null) return { text: null, items: [] }
  if (typeof summary === 'string') return { text: summary.trim() || null, items: [] }
  const items: { key: string; label: string; value: string }[] = []
  for (const [key, v] of Object.entries(summary)) {
    const value = summaryValue(key, v)
    if (value !== null) items.push({ key, label: SUMMARY_LABELS[key] ?? humanize(key), value })
  }
  return { text: null, items }
}

export function shortHash(hash: string, n = 10): string {
  return hash.length > n ? hash.slice(0, n) : hash
}

// ---------------------------------------------------------------------------------------------
// Ideas and journal
// ---------------------------------------------------------------------------------------------

export function ideaFamilies(ideas: readonly ResearchIdea[]): string[] {
  return [...new Set(ideas.map((i) => i.family).filter((f) => f !== ''))].sort()
}

/** The file name of a journal path (`agents/journal/x.md` → `x.md`), else null. */
export function journalNameFromPath(path: string | null): string | null {
  if (path === null) return null
  const name = path.split(/[\\/]/).pop() ?? ''
  return name === '' ? null : name
}

export type JournalPick =
  | { kind: 'entry'; entry: JournalEntry }
  /** A specific entry was asked for (a run's "Open journal entry") and is not in the list. */
  | { kind: 'missing'; name: string }
  | { kind: 'empty' }

/**
 * The journal entry to show. With nothing picked, the newest; a picked name that is not in the
 * list is reported as missing, never swapped for another run's entry.
 */
export function pickJournalEntry(
  entries: readonly JournalEntry[],
  picked: string | null,
): JournalPick {
  if (picked !== null) {
    const entry = entries.find((e) => e.name === picked)
    return entry ? { kind: 'entry', entry } : { kind: 'missing', name: picked }
  }
  return entries.length > 0 ? { kind: 'entry', entry: entries[0] } : { kind: 'empty' }
}

/** Only http(s) URLs become links; anything else (javascript:, data:, …) stays text. */
export function safeHttpUrl(raw: string): string | null {
  const s = raw.trim()
  if (!/^https?:\/\//i.test(s)) return null
  try {
    const u = new URL(s)
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null
  } catch {
    return null
  }
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

// ---------------------------------------------------------------------------------------------
// Markdown → safe blocks
// ---------------------------------------------------------------------------------------------

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  /** `href` is a vetted http(s) URL, or null when the target was unsafe (shown as text). */
  | { kind: 'link'; text: string; href: string | null }

export type MdBlock =
  | { kind: 'meta'; entries: [string, string][] }
  | { kind: 'heading'; level: 1 | 2 | 3 | 4 | 5 | 6; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'list'; ordered: boolean; items: { text: string; depth: number }[] }
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'table'; header: string[]; rows: string[][] }
  | { kind: 'rule' }

// Every repeat is bounded, so one start position scans a bounded number of characters and the
// whole match is linear in the (capped) input; unbounded groups made hostile lines O(n²).
// A link URL may hold one level of balanced parentheses (`…/wiki/Foo_(bar)`); the two URL
// alternatives start with different characters, so there is only one way to match each one.
const INLINE_RE =
  /(?<!`)(`{1,8})(?!`)([\s\S]{0,500}?[^`])\1(?!`)|\*\*([^*]{1,500})\*\*|__([^_]{1,500})__|\*([^*\s][^*]{0,499})\*|\[([^\]]{1,500})\]\(((?:[^()\s]|\([^()\s]{0,500}\)){1,2048})(?:\s+"[^"]{0,300}")?\)/g

/** Only this much of one paragraph / line gets inline formatting; the rest is literal text. */
export const INLINE_MAX_CHARS = 8000

/** Inline code, bold, italics and links; everything else is literal text. */
export function parseInline(input: string): Inline[] {
  const out: Inline[] = []
  const text = input.length > INLINE_MAX_CHARS ? input.slice(0, INLINE_MAX_CHARS) : input
  let last = 0
  for (const m of text.matchAll(INLINE_RE)) {
    const at = m.index
    if (at > last) out.push({ kind: 'text', text: text.slice(last, at) })
    if (m[2] !== undefined)
      out.push({ kind: 'code', text: m[2].trim() === '' ? m[2] : m[2].trim() })
    else if (m[3] !== undefined || m[4] !== undefined)
      out.push({ kind: 'strong', text: m[3] ?? m[4] })
    else if (m[5] !== undefined) out.push({ kind: 'em', text: m[5] })
    else out.push({ kind: 'link', text: m[6], href: safeHttpUrl(m[7]) })
    last = at + m[0].length
  }
  if (last < input.length) out.push({ kind: 'text', text: input.slice(last) })
  return out
}

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/
const RULE_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/
const QUOTE_RE = /^\s{0,3}>\s?(.*)$/
const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/
const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/

function tableCells(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1)
  return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'))
}

function isTableStart(lines: readonly string[], i: number): boolean {
  return (
    lines[i].includes('|') &&
    i + 1 < lines.length &&
    lines[i + 1].includes('-') &&
    TABLE_SEP_RE.test(lines[i + 1])
  )
}

function startsBlock(lines: readonly string[], i: number): boolean {
  const l = lines[i]
  return (
    FENCE_RE.test(l) ||
    HEADING_RE.test(l) ||
    RULE_RE.test(l) ||
    QUOTE_RE.test(l) ||
    LIST_RE.test(l) ||
    isTableStart(lines, i)
  )
}

/** At most this many characters are parsed; a huge file is cut with a note. */
export const MARKDOWN_MAX_CHARS = 400_000

/**
 * A small CommonMark subset as plain-text blocks: YAML front matter, headings, paragraphs,
 * bullet/numbered lists, fenced code, block quotes, pipe tables and rules. Raw HTML is not
 * interpreted: `<script>` stays the literal text "<script>".
 */
export function parseMarkdown(markdown: string): MdBlock[] {
  let src = markdown.replace(/\r\n?/g, '\n')
  const truncated = src.length > MARKDOWN_MAX_CHARS
  if (truncated) src = src.slice(0, MARKDOWN_MAX_CHARS)
  const lines = src.split('\n')
  const blocks: MdBlock[] = []
  let i = 0

  // Front matter (Obsidian-style) at the very top.
  if (lines[0]?.trim() === '---') {
    const end = lines.findIndex((l, j) => j > 0 && l.trim() === '---')
    if (end > 0) {
      const entries: [string, string][] = []
      for (const l of lines.slice(1, end)) {
        const m = /^([\w.-]+)\s*:\s*(.*)$/.exec(l.trim())
        if (m) entries.push([m[1], m[2].replace(/^["']|["']$/g, '')])
      }
      if (entries.length > 0) blocks.push({ kind: 'meta', entries })
      i = end + 1
    }
  }

  while (i < lines.length) {
    const line = lines[i]
    if (line.trim() === '') {
      i++
      continue
    }

    const fence = FENCE_RE.exec(line)
    if (fence) {
      const marker = fence[1]
      const body: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        body.push(lines[i])
        i++
      }
      i++ // the closing fence (or EOF)
      blocks.push({ kind: 'code', lang: fence[2], text: body.join('\n') })
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading) {
      blocks.push({
        kind: 'heading',
        level: heading[1].length as 1 | 2 | 3 | 4 | 5 | 6,
        text: heading[2],
      })
      i++
      continue
    }

    if (RULE_RE.test(line)) {
      blocks.push({ kind: 'rule' })
      i++
      continue
    }

    if (QUOTE_RE.test(line)) {
      const parts: string[] = []
      while (i < lines.length) {
        const q = QUOTE_RE.exec(lines[i])
        if (!q) break
        parts.push(q[1])
        i++
      }
      blocks.push({ kind: 'quote', text: parts.join(' ').trim() })
      continue
    }

    if (isTableStart(lines, i)) {
      const header = tableCells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        const cells = tableCells(lines[i])
        rows.push(header.map((_, c) => cells[c] ?? ''))
        i++
      }
      blocks.push({ kind: 'table', header, rows })
      continue
    }

    const first = LIST_RE.exec(line)
    if (first) {
      const ordered = /\d/.test(first[2])
      const items: { text: string; depth: number }[] = []
      while (i < lines.length) {
        const l = lines[i]
        const m = LIST_RE.exec(l)
        if (m) {
          const indent = m[1].replace(/\t/g, '  ').length
          items.push({ text: m[3], depth: Math.min(4, Math.floor(indent / 2)) })
          i++
        } else if (l.trim() !== '' && /^\s{2,}\S/.test(l) && items.length > 0) {
          items[items.length - 1].text += ` ${l.trim()}`
          i++
        } else {
          break
        }
      }
      blocks.push({ kind: 'list', ordered, items })
      continue
    }

    const para: string[] = [line.trim()]
    i++
    while (i < lines.length && lines[i].trim() !== '' && !startsBlock(lines, i)) {
      para.push(lines[i].trim())
      i++
    }
    blocks.push({ kind: 'paragraph', text: para.join(' ') })
  }

  if (truncated) {
    blocks.push({ kind: 'quote', text: 'This journal entry is very long; the rest is not shown.' })
  }
  return blocks
}
