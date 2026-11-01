/**
 * Client for the lab's read-only Phase 8 research endpoints (`GET /lab/research/...`): research
 * runs and their live progress, the idea library, the journal, candidates, and the weekly
 * paper-trading comparisons. Same-origin `/lab`, proxied to the lab service on 127.0.0.1:8788.
 *
 * Only GET requests: nothing here starts runs, spends budget, touches the holdout, or trades.
 * Every response is validated; an unexpected shape becomes a `LabError('parse')`, never a crash.
 */

import {
  compact,
  isRecord,
  numberOr as numOr,
  numberOrNull as num,
  stringOr as str,
  stringsOf as strings,
} from './guards'
import type { FetchLike } from './http'
import {
  LabError,
  createLabRequest,
  labFail as fail,
  labList as list,
  labRecord as record,
} from './labApi'
import type { LabPlan } from './labTypes'
import type {
  CandidateSummary,
  IdeaQuery,
  IdeaSource,
  IdeaStatus,
  JournalDocument,
  JournalEntry,
  PaperComparison,
  PaperVerdict,
  ResearchCandidate,
  ResearchIdea,
  ResearchOutcome,
  ResearchPhase,
  RunBudgetLimits,
  RunEvent,
  RunItem,
  RunState,
  RunUsage,
  RunUsed,
  TradeStats,
} from './researchTypes'

export const RESEARCH_BASE_PATH = '/lab/research'
export const RESEARCH_TIMEOUT_MS = 10_000

export const RESEARCH_OFFLINE_MESSAGE =
  'Lab service not running — start it with `cd lab && uv run python -m lab.api` (or `npm run dev` at the repo root).'

/** The message of an HTTP error whose body carried no `detail` (shared with the lab client). */
export { httpStatusMessage } from './labApi'

/** FastAPI's own `detail` for a route that does not exist (vs. a handler's "… not found"). */
export const FASTAPI_NOT_FOUND = 'Not Found'

export interface ResearchClientOptions {
  /** Defaults to the global `fetch`, looked up at call time. */
  fetchImpl?: FetchLike
  /** Defaults to `/lab/research`. A trailing slash is dropped. */
  basePath?: string
  timeoutMs?: number
}

export interface ResearchClient {
  runs(signal?: AbortSignal): Promise<RunItem[]>
  run(runId: string, signal?: AbortSignal): Promise<RunItem>
  ideas(query?: IdeaQuery, signal?: AbortSignal): Promise<ResearchIdea[]>
  journal(signal?: AbortSignal): Promise<JournalEntry[]>
  journalEntry(name: string, signal?: AbortSignal): Promise<JournalDocument>
  candidates(signal?: AbortSignal): Promise<ResearchCandidate[]>
  paper(signal?: AbortSignal): Promise<PaperComparison[]>
}

// ---------------------------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------------------------

/** Finite numeric entries only. */
function numberRecord(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (!isRecord(v)) return out
  for (const [k, x] of Object.entries(v)) {
    const n = num(x)
    if (n !== null) out[k] = n
  }
  return out
}

/** Year 1 … 9999 in epoch ms: the range `Date#toISOString` can print as YYYY-MM-DD. */
const MIN_ISO_MS = -62_135_596_800_000
const MAX_ISO_MS = 253_402_300_799_999

/**
 * An ISO-8601 timestamp as written by the orchestrator (kept as the string), or null. A UNIX
 * time (seconds or ms) from an older writer is converted to ISO; anything unparseable is null.
 */
function isoOrNull(v: unknown): string | null {
  if (typeof v === 'string') {
    const s = v.trim()
    return s !== '' && !Number.isNaN(Date.parse(s)) ? s : null
  }
  if (typeof v === 'number' && Number.isFinite(v)) {
    const ms = Math.abs(v) < 1e11 ? v * 1000 : v
    return ms >= MIN_ISO_MS && ms <= MAX_ISO_MS ? new Date(ms).toISOString() : null
  }
  return null
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : fallback
}

// ---------------------------------------------------------------------------------------------
// Parsers (exported for tests)
// ---------------------------------------------------------------------------------------------

export const RESEARCH_PHASES: readonly ResearchPhase[] = [
  'starting',
  'scouting',
  'designing',
  'testing',
  'critic',
  'reporting',
  'done',
]

export const RESEARCH_OUTCOMES: readonly ResearchOutcome[] = [
  'running',
  'candidate',
  'no_edge',
  'budget_exhausted',
  'error',
  'stopped',
]

export const IDEA_STATUSES: readonly IdeaStatus[] = [
  'new',
  'testing',
  'promising',
  'candidate',
  'abandoned',
  'dead',
  'duplicate',
]

const PAPER_VERDICTS: readonly PaperVerdict[] = ['consistent', 'diverging', 'insufficient_data']

export function parseCandidate(v: unknown): CandidateSummary | null {
  if (!isRecord(v) || !isRecord(v.plan)) return null
  const summary = v.summary
  return {
    idea_id: num(v.idea_id),
    plan_hash: str(v.plan_hash),
    plan: v.plan as LabPlan,
    trial_id: num(v.trial_id),
    summary: typeof summary === 'string' || isRecord(summary) ? summary : null,
  }
}

function parseEvent(v: unknown): RunEvent | null {
  if (!isRecord(v)) return null
  const message = str(v.message).trim()
  if (message === '') return null
  const at = typeof v.at === 'string' ? v.at : num(v.at)
  return { at, kind: str(v.kind, 'info'), message }
}

function parseUsed(v: unknown): RunUsed {
  const u = isRecord(v) ? v : {}
  return {
    hypotheses: numOr(u.hypotheses, 0),
    proposals: numOr(u.proposals, 0),
    backtests: numOr(u.backtests, 0),
    holdout_checks: numOr(u.holdout_checks, 0),
    usd: numOr(u.usd, 0),
  }
}

/** A run-state file, or null when it is missing or not a run state at all. */
export function parseRunState(v: unknown): RunState | null {
  if (!isRecord(v)) return null
  const runId = str(v.run_id)
  if (runId === '') return null
  return {
    run_id: runId,
    symbols: strings(v.symbols),
    interval: str(v.interval),
    started_at: str(v.started_at),
    updated_at: str(v.updated_at),
    phase: oneOf(v.phase, RESEARCH_PHASES, 'starting'),
    // An unknown outcome is not "running": it must not keep the page polling forever.
    outcome: oneOf(v.outcome, RESEARCH_OUTCOMES, 'stopped'),
    budget: numberRecord(v.budget),
    used: parseUsed(v.used),
    current_idea_id: num(v.current_idea_id),
    candidates: Array.isArray(v.candidates) ? compact(v.candidates, parseCandidate) : [],
    journal_path:
      typeof v.journal_path === 'string' && v.journal_path !== '' ? v.journal_path : null,
    events: Array.isArray(v.events) ? compact(v.events, parseEvent) : [],
  }
}

function parseUsage(v: unknown): RunUsage | null {
  if (!isRecord(v)) return null
  return {
    budget: numberRecord(v.budget),
    used: numberRecord(v.used),
    remaining: numberRecord(v.remaining),
  }
}

function parseLimits(v: unknown): RunBudgetLimits {
  const b = isRecord(v) ? v : {}
  return {
    max_hypotheses: num(b.max_hypotheses),
    max_proposals: num(b.max_proposals),
    max_backtests: num(b.max_backtests),
    max_holdout_checks: num(b.max_holdout_checks),
    max_usd: num(b.max_usd),
  }
}

function parseCriteria(v: unknown): Record<string, number | boolean> {
  const out: Record<string, number | boolean> = {}
  if (!isRecord(v)) return out
  for (const [k, x] of Object.entries(v)) {
    if (typeof x === 'boolean') out[k] = x
    else if (num(x) !== null) out[k] = x as number
  }
  return out
}

export function parseRunItem(v: unknown): RunItem | null {
  if (!isRecord(v)) return null
  const runId = str(v.run_id)
  if (runId === '') return null
  return {
    run_id: runId,
    created_at: numOr(v.created_at, 0),
    label: str(v.label),
    config_sha256: str(v.config_sha256),
    criteria: parseCriteria(v.criteria),
    budget: parseLimits(v.budget),
    usage: parseUsage(v.usage),
    state: parseRunState(v.state),
  }
}

function parseSource(v: unknown): IdeaSource | null {
  if (!isRecord(v)) return null
  const url = str(v.url).trim()
  if (url === '') return null
  // Web sources are untrusted unless the lab says otherwise explicitly.
  return { url, title: str(v.title).trim(), untrusted: v.untrusted !== false }
}

export function parseIdea(v: unknown): ResearchIdea | null {
  if (!isRecord(v)) return null
  const id = num(v.id)
  if (id === null) return null
  return {
    id,
    created_at: numOr(v.created_at, 0),
    run_id: str(v.run_id),
    family: str(v.family),
    title: str(v.title).trim() || `Idea #${id}`,
    hypothesis: str(v.hypothesis),
    rationale: str(v.rationale),
    status: oneOf(v.status, IDEA_STATUSES, 'new'),
    verdict_note: str(v.verdict_note),
    plan_hashes: strings(v.plan_hashes),
    sources: Array.isArray(v.sources) ? compact(v.sources, parseSource) : [],
  }
}

export function parseJournalEntry(v: unknown): JournalEntry | null {
  if (!isRecord(v)) return null
  const name = str(v.name)
  if (name === '') return null
  const ids = Array.isArray(v.idea_ids)
    ? v.idea_ids.filter((x): x is number => typeof x === 'number' && Number.isFinite(x))
    : []
  return {
    name,
    title: str(v.title).trim() || name,
    run_id: str(v.run_id),
    date: str(v.date),
    outcome: str(v.outcome),
    idea_ids: ids,
    size: numOr(v.size, 0),
  }
}

function parseTradeStats(v: unknown): TradeStats {
  const s = isRecord(v) ? v : {}
  return {
    n_trades: num(s.n_trades),
    total_return: num(s.total_return),
    win_rate: num(s.win_rate),
    mean_trade_return: num(s.mean_trade_return),
  }
}

export function parsePaperComparison(v: unknown): PaperComparison | null {
  if (!isRecord(v)) return null
  const candidate = str(v.candidate)
  const planHash = str(v.plan_hash)
  if (candidate === '' && planHash === '') return null
  return {
    candidate,
    plan_hash: planHash,
    symbol: str(v.symbol),
    interval: str(v.interval),
    period_start: num(v.period_start),
    period_end: num(v.period_end),
    generated_at: isoOrNull(v.generated_at),
    forward: parseTradeStats(v.forward),
    backtest: parseTradeStats(v.backtest),
    entry_match_rate: num(v.entry_match_rate),
    mean_entry_slippage_bps: num(v.mean_entry_slippage_bps),
    verdict: oneOf(v.verdict, PAPER_VERDICTS, 'insufficient_data'),
    notes: strings(v.notes),
  }
}

// ---------------------------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------------------------

const RUN_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/
const JOURNAL_NAME_RE = /^[A-Za-z0-9._-]{1,200}$/

export function createResearchClient(opts: ResearchClientOptions = {}): ResearchClient {
  const base = (opts.basePath ?? RESEARCH_BASE_PATH).replace(/\/+$/, '')
  const timeoutMs = opts.timeoutMs ?? RESEARCH_TIMEOUT_MS
  // The research endpoints take no plans, so a 422 is a plain http error here.
  const request = createLabRequest({
    fetchImpl: opts.fetchImpl,
    base,
    timeoutMs,
    offlineMessage: RESEARCH_OFFLINE_MESSAGE,
    planErrors: false,
  })
  const get = (path: string, signal?: AbortSignal): Promise<unknown> =>
    request(path, { method: 'GET', signal })

  return {
    async runs(signal) {
      const body = record(await get('/runs', signal), 'research run list')
      const runs = compact(list(body.runs, 'research run list'), parseRunItem)
      // Newest first, whatever order the list arrives in.
      return runs.sort((a, b) => b.created_at - a.created_at)
    },
    async run(runId, signal) {
      if (!RUN_ID_RE.test(runId)) throw new LabError('http', `Invalid run id: ${runId}`)
      const item = parseRunItem(await get(`/runs/${encodeURIComponent(runId)}`, signal))
      if (item === null) fail('research run')
      return item
    },
    async ideas(query = {}, signal) {
      const params = new URLSearchParams()
      const runId = query.runId?.trim()
      if (runId) params.set('run_id', runId)
      if (query.status) params.set('status', query.status)
      const qs = params.toString()
      const body = record(await get(`/ideas${qs ? `?${qs}` : ''}`, signal), 'idea list')
      return compact(list(body.ideas, 'idea list'), parseIdea)
    },
    async journal(signal) {
      const body = record(await get('/journal', signal), 'journal list')
      return compact(list(body.entries, 'journal list'), parseJournalEntry)
    },
    async journalEntry(name, signal) {
      if (!JOURNAL_NAME_RE.test(name) || name.includes('..')) {
        throw new LabError('http', `Invalid journal entry name: ${name}`)
      }
      const body = record(
        await get(`/journal/${encodeURIComponent(name)}`, signal),
        'journal entry',
      )
      if (typeof body.markdown !== 'string') fail('journal entry')
      return { name: str(body.name, name), markdown: body.markdown }
    },
    async candidates(signal) {
      const body = record(await get('/candidates', signal), 'candidate list')
      return compact(list(body.candidates, 'candidate list'), (v) => {
        const c = parseCandidate(v)
        return c === null || !isRecord(v) ? null : { ...c, run_id: str(v.run_id) }
      })
    },
    async paper(signal) {
      const body = record(await get('/paper', signal), 'paper-trading comparison list')
      return compact(list(body.comparisons, 'paper-trading comparison list'), parsePaperComparison)
    },
  }
}
