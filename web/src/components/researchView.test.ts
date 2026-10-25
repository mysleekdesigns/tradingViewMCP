import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { LabError } from '../data/labApi'
import type { JournalEntry, PaperComparison, RunItem, RunState } from '../data/researchTypes'
import { httpStatusMessage } from '../data/researchApi'
import { MarkdownView, ResearchFallback, TabFallback } from './ResearchPage'
import {
  CANDIDATE_EXPLAINER,
  INLINE_MAX_CHARS,
  PLAN_RESTORE_KEY,
  RESEARCH_IDLE_POLL_MS,
  RESEARCH_OFFLINE_RECHECK_MS,
  RESEARCH_POLL_MS,
  RESEARCH_STALE_MS,
  RESEARCH_TAB_KEY,
  budgetMeters,
  candidateRunRequest,
  criteriaDefinitions,
  criteriaRows,
  describeResearchError,
  eventRow,
  fmtDate,
  fmtClockUtc,
  fmtDateTime,
  fmtUtc,
  hasFreshLiveRun,
  ideaFamilies,
  ideaStatusView,
  isStale,
  journalNameFromPath,
  journalTargetAfterTabChange,
  liveChipState,
  loadPlanRestore,
  loadResearchTab,
  outcomeView,
  paperFor,
  parseInline,
  parseMarkdown,
  phaseSteps,
  pickJournalEntry,
  planRestoreAfterLoad,
  planRestoreValid,
  recentEvents,
  relativeTime,
  resetResearchTab,
  runsPollDelay,
  runsRefreshNote,
  safeHttpUrl,
  savePlanRestore,
  saveResearchTab,
  shouldPoll,
  summaryView,
  toEpochMs,
  unmatchedPaper,
  verdictView,
  type RemovableStorage,
  type TabStorage,
} from './researchView'

function state(over: Partial<RunState> = {}): RunState {
  return {
    run_id: 'r-1',
    symbols: ['BTCUSDT'],
    interval: '4h',
    started_at: '2026-10-24T12:00:00Z',
    updated_at: '2026-10-24T12:05:00Z',
    phase: 'testing',
    outcome: 'running',
    budget: {},
    used: { hypotheses: 0, proposals: 0, backtests: 0, holdout_checks: 0, usd: 0 },
    current_idea_id: null,
    candidates: [],
    journal_path: null,
    events: [],
    ...over,
  }
}

function run(over: Partial<RunItem> = {}): RunItem {
  return {
    run_id: 'r-1',
    created_at: 1_793_000_000,
    label: '',
    config_sha256: '',
    criteria: {},
    budget: {
      max_hypotheses: null,
      max_proposals: null,
      max_backtests: null,
      max_holdout_checks: null,
      max_usd: null,
    },
    usage: null,
    state: null,
    ...over,
  }
}

function paper(over: Partial<PaperComparison> = {}): PaperComparison {
  const stats = { n_trades: 1, total_return: 0, win_rate: 0, mean_trade_return: 0 }
  return {
    candidate: 'c',
    plan_hash: 'h1',
    symbol: 'BTCUSDT',
    interval: '4h',
    period_start: 100,
    period_end: 200,
    generated_at: '2026-10-24T00:00:00+00:00',
    forward: stats,
    backtest: stats,
    entry_match_rate: null,
    mean_entry_slippage_bps: null,
    verdict: 'consistent',
    notes: [],
    ...over,
  }
}

describe('outcome labels', () => {
  it('labels no_edge as "No edge found" with the calm (non-error) tone', () => {
    const v = outcomeView('no_edge')
    expect(v.label).toBe('No edge found')
    expect(v.tone).toBe('calm')
    expect(v.description).toMatch(/finished correctly/)
  })

  it('treats an exhausted budget as a calm, finished result too', () => {
    expect(outcomeView('budget_exhausted').tone).toBe('calm')
  })

  it('labels the other outcomes, and a missing state', () => {
    expect(outcomeView('running')).toMatchObject({ label: 'Running', tone: 'live' })
    expect(outcomeView('candidate')).toMatchObject({ label: 'Candidate found', tone: 'good' })
    expect(outcomeView('error').tone).toBe('bad')
    expect(outcomeView('stopped').tone).toBe('neutral')
    expect(outcomeView(null).label).toBe('No live progress')
  })

  it('candidate description says paper trading is still required', () => {
    expect(outcomeView('candidate').description).toMatch(/paper trading/)
  })

  it('never claims a candidate passed every criterion: the cross-check may be pending', () => {
    for (const text of [outcomeView('candidate').description, CANDIDATE_EXPLAINER]) {
      expect(text).toMatch(/design-window criteria and the locked holdout/)
      expect(text).toMatch(/event-driven cross-check/)
      expect(text).toMatch(/4–8 weeks of forward paper trading/)
      expect(text).not.toMatch(/every success criterion|passed every|all criteria/i)
    }
  })

  it('labels idea statuses and paper verdicts', () => {
    expect(ideaStatusView('dead')).toEqual({ label: 'Dead', tone: 'calm' })
    expect(ideaStatusView('candidate').tone).toBe('good')
    expect(verdictView('diverging').tone).toBe('bad')
    expect(verdictView('insufficient_data').label).toBe('Not enough trades yet')
  })
})

describe('phaseSteps', () => {
  it('marks earlier phases done and the current one current while running', () => {
    const steps = phaseSteps('testing', 'running')
    expect(steps.map((s) => s.state)).toEqual([
      'done',
      'done',
      'done',
      'current',
      'todo',
      'todo',
      'todo',
    ])
  })

  it('a finished run has no current step', () => {
    expect(phaseSteps('done', 'no_edge').every((s) => s.state === 'done')).toBe(true)
    const errored = phaseSteps('designing', 'error')
    expect(errored.some((s) => s.state === 'current')).toBe(false)
    expect(errored[2].state).toBe('done')
    expect(errored[3].state).toBe('todo')
  })
})

describe('budgetMeters', () => {
  it('computes used/limit percentages from the live state first', () => {
    const meters = budgetMeters(
      run({
        budget: {
          max_hypotheses: 10,
          max_proposals: 40,
          max_backtests: 200,
          max_holdout_checks: 2,
          max_usd: 5,
        },
        state: state({
          budget: { backtests: 100 },
          used: { hypotheses: 5, proposals: 36, backtests: 120, holdout_checks: 2, usd: 1.5 },
        }),
      }),
    )
    const by = Object.fromEntries(meters.map((m) => [m.key, m]))
    expect(by.hypotheses).toMatchObject({ pct: 50, level: 'ok', text: '5 / 10' })
    expect(by.proposals).toMatchObject({ pct: 90, level: 'high' })
    // The state's own limit (100) wins over the frozen RunItem budget (200); capped at 100%.
    expect(by.backtests).toMatchObject({ limit: 100, pct: 100, level: 'full' })
    expect(by.holdout_checks).toMatchObject({ pct: 100, level: 'full', text: '2 / 2' })
    expect(by.usd).toMatchObject({ pct: 30, text: '$1.50 / $5.00' })
  })

  it('uses the server-enforced usage for proposals, backtests and holdout checks', () => {
    const meters = budgetMeters(
      run({
        budget: {
          max_hypotheses: 10,
          max_proposals: 40,
          max_backtests: 200,
          max_holdout_checks: 2,
          max_usd: 5,
        },
        usage: {
          budget: { proposals: 40, backtests: 200, holdout_checks: 1 },
          used: { proposals: 12, backtests: 150, holdout_checks: 1 },
          remaining: {},
        },
        // The progress file lags (or lies); it must not win over the lab's own counters.
        state: state({
          budget: { backtests: 100, holdout_checks: 5 },
          used: { hypotheses: 5, proposals: 3, backtests: 20, holdout_checks: 0, usd: 1.5 },
        }),
      }),
    )
    const by = Object.fromEntries(meters.map((m) => [m.key, m]))
    expect(by.proposals).toMatchObject({ used: 12, limit: 40, text: '12 / 40' })
    expect(by.backtests).toMatchObject({ used: 150, limit: 200, pct: 75, level: 'ok' })
    expect(by.holdout_checks).toMatchObject({ used: 1, limit: 1, level: 'full' })
    // Only the progress file knows these two.
    expect(by.hypotheses).toMatchObject({ used: 5, limit: 10 })
    expect(by.usd).toMatchObject({ text: '$1.50 / $5.00' })
  })

  it('an enforced counter missing from usage falls back to the progress file', () => {
    const [m] = budgetMeters(
      run({
        usage: { budget: {}, used: {}, remaining: {} },
        state: state({
          used: { hypotheses: 0, proposals: 0, backtests: 9, holdout_checks: 0, usd: 0 },
        }),
      }),
    ).filter((x) => x.key === 'backtests')
    expect(m).toMatchObject({ used: 9, limit: null })
  })

  it('falls back to ledger usage, and shows "no limit" without a limit', () => {
    const meters = budgetMeters(
      run({ usage: { budget: {}, used: { backtests: 7 }, remaining: {} } }),
    )
    expect(meters).toHaveLength(1)
    expect(meters[0]).toMatchObject({
      key: 'backtests',
      pct: null,
      level: 'none',
      text: '7 (no limit)',
    })
  })

  it('a zero limit with usage is full, not a division by zero', () => {
    const [m] = budgetMeters(
      run({
        budget: {
          max_hypotheses: null,
          max_proposals: null,
          max_backtests: null,
          max_holdout_checks: 0,
          max_usd: null,
        },
        usage: { budget: {}, used: { holdout_checks: 0 }, remaining: {} },
      }),
    )
    expect(m).toMatchObject({ key: 'holdout_checks', pct: 100, level: 'full' })
  })
})

describe('polling and time', () => {
  it('polls only while some run is running', () => {
    expect(RESEARCH_POLL_MS).toBe(5000)
    expect(shouldPoll(null)).toBe(false)
    expect(shouldPoll([])).toBe(false)
    expect(shouldPoll([run({ state: state({ outcome: 'no_edge' }) }), run()])).toBe(false)
    expect(
      shouldPoll([run({ state: state({ outcome: 'no_edge' }) }), run({ state: state() })]),
    ).toBe(true)
  })

  it('schedules the next poll only while a running run is making progress', () => {
    const updated = Date.UTC(2026, 9, 24, 12, 5)
    const fresh = updated + 60_000
    const stalled = updated + RESEARCH_STALE_MS + 1
    const live = [run({ state: state() })]
    const base = { runs: live, offline: false, hidden: false, nowMs: fresh }
    expect(runsPollDelay(base)).toBe(RESEARCH_POLL_MS)
    // Paused while the tab is hidden; a stalled run only slows the list to the 30 s check.
    expect(runsPollDelay({ ...base, hidden: true })).toBeNull()
    expect(runsPollDelay({ ...base, nowMs: stalled })).toBe(RESEARCH_IDLE_POLL_MS)
    // Nothing running, an empty list, or nothing loaded yet (an error): slow check for new runs.
    expect(runsPollDelay({ ...base, runs: [run({ state: state({ outcome: 'no_edge' }) })] })).toBe(
      RESEARCH_IDLE_POLL_MS,
    )
    expect(runsPollDelay({ ...base, runs: [] })).toBe(RESEARCH_IDLE_POLL_MS)
    expect(runsPollDelay({ ...base, runs: [run()] })).toBe(RESEARCH_IDLE_POLL_MS)
    expect(runsPollDelay({ ...base, runs: null })).toBe(RESEARCH_IDLE_POLL_MS)
    // Offline: look for the lab again, unless hidden.
    expect(runsPollDelay({ ...base, runs: null, offline: true })).toBe(RESEARCH_OFFLINE_RECHECK_MS)
    expect(runsPollDelay({ ...base, offline: true, hidden: true })).toBeNull()
  })

  it('idle and empty lists refresh slowly while visible, never while hidden', () => {
    expect(RESEARCH_IDLE_POLL_MS).toBe(30_000)
    const now = Date.UTC(2026, 9, 24, 13)
    const finished = [run({ state: state({ outcome: 'no_edge' }) })]
    for (const runs of [[], finished]) {
      expect(runsPollDelay({ runs, offline: false, hidden: false, nowMs: now })).toBe(30_000)
      expect(
        runsPollDelay({ runs, offline: false, hidden: false, nowMs: now, trigger: 'response' }),
      ).toBe(30_000)
      expect(runsPollDelay({ runs, offline: false, hidden: true, nowMs: now })).toBeNull()
      // Back in view: refetch at once, so a run started meanwhile shows up immediately.
      expect(
        runsPollDelay({ runs, offline: false, hidden: false, nowMs: now, trigger: 'visible' }),
      ).toBe(0)
    }
  })

  it('opening the view loads at once (unless hidden)', () => {
    const input = { runs: null, offline: false, hidden: false, nowMs: 0 }
    expect(runsPollDelay({ ...input, trigger: 'initial' })).toBe(0)
    expect(runsPollDelay({ ...input, hidden: true, trigger: 'initial' })).toBeNull()
  })

  it('hidden 11 minutes, return: loads once, and the fresh answer decides whether polling continues', () => {
    const updated = Date.UTC(2026, 9, 24, 12, 5)
    const held = [run({ state: state() })] // last list seen before the tab was hidden
    const back = updated + 11 * 60_000 // now judged stale against the old list
    // Judged from the old list alone the run looks stalled: only the slow 30 s check, never null.
    expect(runsPollDelay({ runs: held, offline: false, hidden: false, nowMs: back })).toBe(
      RESEARCH_IDLE_POLL_MS,
    )
    // Now the visibility handler always asks once…
    expect(
      runsPollDelay({ runs: held, offline: false, hidden: false, nowMs: back, trigger: 'visible' }),
    ).toBe(0)
    // …offline or not, and whatever the old list said.
    expect(
      runsPollDelay({ runs: held, offline: true, hidden: false, nowMs: back, trigger: 'visible' }),
    ).toBe(0)
    // The fresh answer shows the run kept working while hidden: 5 s polling resumes.
    const fresh = [run({ state: state({ updated_at: new Date(back - 20_000).toISOString() }) })]
    expect(runsPollDelay({ runs: fresh, offline: false, hidden: false, nowMs: back + 50 })).toBe(
      RESEARCH_POLL_MS,
    )
    // It really stalled: the list keeps the slow 30 s check so a new or resumed run appears.
    expect(runsPollDelay({ runs: held, offline: false, hidden: false, nowMs: back + 50 })).toBe(
      RESEARCH_IDLE_POLL_MS,
    )
    // It finished while hidden: back to the slow idle check.
    const done = [
      run({ state: state({ outcome: 'no_edge', updated_at: new Date(back).toISOString() }) }),
    ]
    expect(runsPollDelay({ runs: done, offline: false, hidden: false, nowMs: back + 50 })).toBe(
      RESEARCH_IDLE_POLL_MS,
    )
  })

  it('the Runs tab says how the list refreshes', () => {
    expect(runsRefreshNote('live')).toBe('Auto-refresh every 5 s')
    expect(runsRefreshNote('idle')).toBe('Checks for new runs every 30 s')
    expect(runsRefreshNote('paused')).toBe('Run stalled · checks for new runs every 30 s')
    expect(runsRefreshNote('offline')).toBe('Lab offline · retrying every 8 s')
    expect(fmtClockUtc(Date.UTC(2026, 9, 24, 14, 3, 5))).toBe('14:03:05 UTC')
    expect(fmtClockUtc(Number.NaN)).toBe('—')
    expect(fmtClockUtc(null)).toBe('—')
  })

  it('one fresh running run keeps polling even when another one stalled', () => {
    const updated = Date.UTC(2026, 9, 24, 12, 5)
    const now = updated + RESEARCH_STALE_MS + 1
    const old = run({ state: state() })
    const recent = run({
      run_id: 'r-2',
      state: state({ run_id: 'r-2', updated_at: new Date(now - 1000).toISOString() }),
    })
    expect(hasFreshLiveRun([old], now)).toBe(false)
    expect(hasFreshLiveRun([old, recent], now)).toBe(true)
  })

  it('the live chip says live, paused (stalled), idle or offline', () => {
    const updated = Date.UTC(2026, 9, 24, 12, 5)
    const live = [run({ state: state() })]
    expect(liveChipState(live, false, updated + 1000)).toBe('live')
    expect(liveChipState(live, false, updated + RESEARCH_STALE_MS + 1)).toBe('paused')
    expect(liveChipState([run()], false, updated)).toBe('idle')
    expect(liveChipState(live, true, updated)).toBe('offline')
  })

  it('toEpochMs reads ISO strings, UNIX seconds and ms', () => {
    expect(toEpochMs('2026-10-24T12:00:00Z')).toBe(Date.UTC(2026, 9, 24, 12))
    expect(toEpochMs(1_793_000_000)).toBe(1_793_000_000_000)
    expect(toEpochMs(1_793_000_000_000)).toBe(1_793_000_000_000)
    expect(toEpochMs('nope')).toBeNull()
    expect(toEpochMs(null)).toBeNull()
    expect(toEpochMs(Number.NaN)).toBeNull()
  })

  it('flags a running run whose progress file is older than the stale limit', () => {
    const updated = Date.UTC(2026, 9, 24, 12, 5)
    const s = state()
    expect(isStale(s, updated + RESEARCH_STALE_MS - 1)).toBe(false)
    expect(isStale(s, updated + RESEARCH_STALE_MS + 1)).toBe(true)
    expect(isStale(state({ outcome: 'no_edge' }), updated + 10 * RESEARCH_STALE_MS)).toBe(false)
    expect(isStale(null, 0)).toBe(false)
  })

  it('date formatters return "—" for non-finite or out-of-range times instead of throwing', () => {
    const t = Date.UTC(2026, 9, 24, 14, 3) / 1000
    expect(fmtDate(t)).toBe('2026-10-24')
    expect(fmtDateTime(t)).toBe('2026-10-24 14:03')
    expect(fmtUtc(Date.UTC(2026, 9, 24, 14, 3))).toBe('2026-10-24 14:03 UTC')
    for (const bad of [Number.NaN, Infinity, -Infinity, 1e15, -1e15]) {
      expect(fmtDate(bad)).toBe('—')
      expect(fmtDateTime(bad)).toBe('—')
      expect(fmtUtc(bad * 1000)).toBe('—')
    }
    expect(fmtDate(null)).toBe('—')
    expect(fmtUtc(null)).toBe('—')
    // A far-future event time (finite, but beyond year 9999) must not crash the event list.
    expect(eventRow({ at: 1e17, kind: 'info', message: 'x' }, 0)).toMatchObject({
      time: '—',
      title: '—',
    })
  })

  it('relativeTime buckets', () => {
    const now = 1_000_000_000
    expect(relativeTime(now - 10_000, now)).toBe('just now')
    expect(relativeTime(now - 5 * 60_000, now)).toBe('5 min ago')
    expect(relativeTime(now - 3 * 3_600_000, now)).toBe('3 h ago')
    expect(relativeTime(now - 2 * 86_400_000, now)).toBe('2 d ago')
    expect(relativeTime(now + 60_000, now)).toBe('just now')
    expect(relativeTime(null, now)).toBe('—')
  })
})

describe('events', () => {
  it('formats time as UTC clock, kind with a tone, and keeps the message', () => {
    const row = eventRow({ at: '2026-10-24T12:01:02Z', kind: 'no_edge', message: 'done' }, 0)
    expect(row).toMatchObject({ time: '12:01:02', kind: 'no edge', tone: 'calm', message: 'done' })
    expect(row.title).toBe('2026-10-24 12:01 UTC')
    expect(eventRow({ at: null, kind: 'ERROR', message: 'x' }, 1)).toMatchObject({
      time: '—',
      kind: 'error',
      tone: 'bad',
    })
    expect(eventRow({ at: 1_793_000_000, kind: '', message: 'x' }, 2).kind).toBe('info')
  })

  it('recentEvents returns the latest events newest first', () => {
    const events = Array.from({ length: 5 }, (_, i) => ({
      at: 1_793_000_000 + i,
      kind: 'info',
      message: `m${i}`,
    }))
    const rows = recentEvents(state({ events }), 3)
    expect(rows.map((r) => r.message)).toEqual(['m4', 'm3', 'm2'])
    expect(new Set(rows.map((r) => r.key)).size).toBe(3)
    expect(recentEvents(null)).toEqual([])
  })
})

describe('errors', () => {
  it('offline → the lab start command', () => {
    const v = describeResearchError(new LabError('offline', 'x'))
    expect(v.kind).toBe('offline')
    expect(v.title).toBe('Lab service not running')
    expect(v.message).toContain('cd lab && uv run python -m lab.api')
  })

  it('a FastAPI "Not Found" (or bare) 404 → the lab has no research endpoints', () => {
    expect(describeResearchError(new LabError('http', 'Not Found', 404)).kind).toBe('missing')
    expect(describeResearchError(new LabError('http', httpStatusMessage(404), 404)).kind).toBe(
      'missing',
    )
  })

  it('a 404 with a handler detail shows that detail, not "restart the lab"', () => {
    const v = describeResearchError(new LabError('http', 'journal entry not found', 404))
    expect(v).toEqual({ kind: 'error', title: 'Not found', message: 'journal entry not found' })
    expect(v.message).not.toMatch(/restart/i)
  })

  it('other errors keep their message', () => {
    expect(describeResearchError(new LabError('parse', 'bad body'))).toMatchObject({
      kind: 'error',
      message: 'bad body',
    })
    expect(describeResearchError(new Error('boom')).message).toBe('boom')
    expect(describeResearchError(new LabError('aborted', 'x')).kind).toBe('aborted')
  })
})

describe('candidateRunRequest', () => {
  const plan = {
    name: 'vol_breakout',
    symbol: 'btcusdt',
    interval: '4h',
    features: {},
    long_entry: 'x',
  }

  it('maps a candidate plan to a tester run on its own market', () => {
    const m = candidateRunRequest({ plan })
    expect(m.ok).toBe(true)
    if (!m.ok) return
    expect(m.request).toMatchObject({
      name: 'vol_breakout',
      symbol: 'BTCUSDT',
      interval: '4h',
      chartInterval: '4h',
    })
    expect(m.request.plan).toBe(plan)
    expect(JSON.parse(m.request.planText)).toEqual(plan)
    expect(m.request.planText).toContain('\n  "symbol"')
  })

  it('an interval the chart lacks keeps the chart interval (null)', () => {
    const m = candidateRunRequest({ plan: { ...plan, interval: '2h' } })
    expect(m.ok && m.request.chartInterval).toBeNull()
  })

  it('a plan without symbol or interval cannot be shown', () => {
    expect(candidateRunRequest({ plan: { interval: '4h' } })).toEqual({
      ok: false,
      reason: 'The plan has no symbol',
    })
    expect(candidateRunRequest({ plan: { symbol: 'BTCUSDT' } }).ok).toBe(false)
  })

  it('falls back to a generic name', () => {
    const m = candidateRunRequest({ plan: { symbol: 'ETHUSDT', interval: '1h' } })
    expect(m.ok && m.request.name).toBe('plan')
  })
})

describe('summaries, criteria, paper matching', () => {
  it('formats a summary record by key and passes text through', () => {
    const v = summaryView({
      sharpe: 1.234,
      total_return: 0.42,
      n_trades: 31,
      passed: true,
      nested: {},
    })
    expect(v.items).toEqual([
      { key: 'sharpe', label: 'Sharpe', value: '1.23' },
      { key: 'total_return', label: 'Total return', value: '42.00%' },
      { key: 'n_trades', label: 'Trades', value: '31' },
      { key: 'passed', label: 'Passed', value: 'yes' },
    ])
    expect(summaryView('  all good ').text).toBe('all good')
    expect(summaryView(null)).toEqual({ text: null, items: [] })
  })

  it('labels the frozen success criteria', () => {
    const rows = criteriaRows({
      dsr_min: 0.95,
      beat_buy_hold: true,
      max_drawdown_max: 0.25,
      min_trades: 30,
    })
    expect(rows).toEqual([
      { key: 'dsr_min', label: 'Deflated Sharpe ≥', value: '0.95' },
      { key: 'beat_buy_hold', label: 'Beats buy-and-hold (Sharpe)', value: 'Required' },
      { key: 'max_drawdown_max', label: 'Max drawdown ≤', value: '25%' },
      { key: 'min_trades', label: 'Trades ≥', value: '30' },
    ])
    expect(criteriaRows({ some_new_rule: false })[0]).toEqual({
      key: 'some_new_rule',
      label: 'Some new rule',
      value: 'Off',
    })
  })

  it('shows the regime parameters as definitions, not as pass/fail criteria', () => {
    const criteria = { min_profitable_regimes: 2, regime_window_days: 90, regime_threshold: 0.1 }
    expect(criteriaRows(criteria).map((r) => r.key)).toEqual(['min_profitable_regimes'])
    expect(criteriaDefinitions(criteria)).toEqual([
      { key: 'regime_window_days', label: 'Regime window', value: '90 days' },
      { key: 'regime_threshold', label: 'Regime threshold', value: '±10%' },
    ])
    expect(criteriaDefinitions({ regime_threshold: 0.075 })[0].value).toBe('±7.5%')
    expect(criteriaDefinitions({ regime_window_days: 1 })[0].value).toBe('1 day')
    expect(criteriaDefinitions({ dsr_min: 0.95 })).toEqual([])
  })

  it('sorts a comparison without generated_at by its period end (seconds)', () => {
    const all = [
      paper({ generated_at: null, period_end: 1_793_000_000 }), // 2026-10-26, after the 24th
      paper({ generated_at: '2026-10-24T00:00:00Z' }),
    ]
    expect(paperFor(all, 'h1').map((p) => p.generated_at)).toEqual([null, '2026-10-24T00:00:00Z'])
  })

  it('matches paper comparisons to a candidate by plan hash, newest first (ISO generated_at)', () => {
    const all = [
      paper({ generated_at: '2026-10-10T12:00:00+00:00' }),
      // Lexically smaller but later in time: sorting must parse, not compare strings.
      paper({ generated_at: '2026-10-17T01:00:00+02:00' }),
      paper({ generated_at: '2026-10-16T23:30:00Z' }),
      paper({ plan_hash: 'other', generated_at: '2026-10-20T00:00:00Z' }),
    ]
    expect(paperFor(all, 'h1').map((p) => p.generated_at)).toEqual([
      '2026-10-16T23:30:00Z',
      '2026-10-17T01:00:00+02:00',
      '2026-10-10T12:00:00+00:00',
    ])
    expect(paperFor(all, '')).toEqual([])
    expect(unmatchedPaper(all, new Set(['h1'])).map((p) => p.plan_hash)).toEqual(['other'])
  })

  it('lists idea families and journal names', () => {
    const idea = (family: string) =>
      ({ family }) as unknown as Parameters<typeof ideaFamilies>[0][number]
    expect(ideaFamilies([idea('trend'), idea(''), idea('carry'), idea('trend')])).toEqual([
      'carry',
      'trend',
    ])
    expect(journalNameFromPath('agents/journal/2026-10-24.md')).toBe('2026-10-24.md')
    expect(journalNameFromPath('C:\\x\\y.md')).toBe('y.md')
    expect(journalNameFromPath(null)).toBeNull()
  })
})

describe('tab persistence', () => {
  function memory(): TabStorage & { data: Map<string, string> } {
    const data = new Map<string, string>()
    return {
      data,
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
    }
  }

  it('round-trips a valid tab and ignores junk', () => {
    const s = memory()
    expect(loadResearchTab(s)).toBe('runs')
    saveResearchTab('candidates', s)
    expect(s.data.get(RESEARCH_TAB_KEY)).toBe('candidates')
    expect(loadResearchTab(s)).toBe('candidates')
    s.data.set(RESEARCH_TAB_KEY, 'evil')
    expect(loadResearchTab(s)).toBe('runs')
  })

  it('a render error resets the saved tab to Runs', () => {
    const s = memory()
    saveResearchTab('journal', s)
    resetResearchTab(s)
    expect(loadResearchTab(s)).toBe('runs')
    expect(() =>
      resetResearchTab({
        getItem: () => null,
        setItem: () => {
          throw new Error('quota')
        },
      }),
    ).not.toThrow()
  })

  it("a run's journal target lasts only while the Journal tab stays open", () => {
    expect(journalTargetAfterTabChange('run-7.md', 'journal')).toBe('run-7.md')
    expect(journalTargetAfterTabChange('run-7.md', 'runs')).toBeNull()
    expect(journalTargetAfterTabChange('run-7.md', 'candidates')).toBeNull()
    // A later plain Journal click then shows the newest entry.
    const later = journalTargetAfterTabChange(
      journalTargetAfterTabChange('run-7.md', 'runs'),
      'journal',
    )
    expect(later).toBeNull()
    const entries: JournalEntry[] = [
      { name: 'new.md', title: '', run_id: '', date: '', outcome: '', idea_ids: [], size: 0 },
      { name: 'run-7.md', title: '', run_id: '', date: '', outcome: '', idea_ids: [], size: 0 },
    ]
    expect(pickJournalEntry(entries, later)).toEqual({ kind: 'entry', entry: entries[0] })
  })

  it('survives a throwing storage', () => {
    const bad: TabStorage = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }
    expect(loadResearchTab(bad)).toBe('runs')
    expect(() => saveResearchTab('ideas', bad)).not.toThrow()
  })
})

describe('safe links', () => {
  it('only http(s) URLs become links', () => {
    expect(safeHttpUrl('https://arxiv.org/abs/1')).toBe('https://arxiv.org/abs/1')
    expect(safeHttpUrl(' http://example.com ')).toBe('http://example.com/')
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl('JAVASCRIPT:alert(1)')).toBeNull()
    expect(safeHttpUrl('data:text/html,<script>')).toBeNull()
    expect(safeHttpUrl('//evil.com')).toBeNull()
    expect(safeHttpUrl('https://')).toBeNull()
  })
})

describe('parseMarkdown', () => {
  it('parses headings, paragraphs, lists, code, quotes, rules and tables', () => {
    const md = [
      '# Run report',
      '',
      'First line',
      'continues here.',
      '',
      '- one',
      '- two',
      '  wrapped',
      '  - nested',
      '',
      '1. first',
      '2. second',
      '',
      '```json',
      '{"a": 1}',
      '```',
      '',
      '> quoted',
      '> more',
      '',
      '---',
      '',
      '| Idea | Verdict |',
      '|---|:--:|',
      '| trend | dead |',
      '| carry |',
    ].join('\n')
    expect(parseMarkdown(md)).toEqual([
      { kind: 'heading', level: 1, text: 'Run report' },
      { kind: 'paragraph', text: 'First line continues here.' },
      {
        kind: 'list',
        ordered: false,
        items: [
          { text: 'one', depth: 0 },
          { text: 'two wrapped', depth: 0 },
          { text: 'nested', depth: 1 },
        ],
      },
      {
        kind: 'list',
        ordered: true,
        items: [
          { text: 'first', depth: 0 },
          { text: 'second', depth: 0 },
        ],
      },
      { kind: 'code', lang: 'json', text: '{"a": 1}' },
      { kind: 'quote', text: 'quoted more' },
      { kind: 'rule' },
      {
        kind: 'table',
        header: ['Idea', 'Verdict'],
        rows: [
          ['trend', 'dead'],
          ['carry', ''],
        ],
      },
    ])
  })

  it('reads front matter as metadata', () => {
    const blocks = parseMarkdown('---\nrun_id: r-1\noutcome: "no_edge"\n---\n# T')
    expect(blocks[0]).toEqual({
      kind: 'meta',
      entries: [
        ['run_id', 'r-1'],
        ['outcome', 'no_edge'],
      ],
    })
    expect(blocks[1]).toEqual({ kind: 'heading', level: 1, text: 'T' })
  })

  it('keeps an unterminated code fence as code to the end', () => {
    expect(parseMarkdown('```\nx\ny')).toEqual([{ kind: 'code', lang: '', text: 'x\ny' }])
  })

  it('treats raw HTML as literal text, never as markup', () => {
    const blocks = parseMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')
    expect(blocks).toEqual([
      { kind: 'paragraph', text: '<script>alert(1)</script>' },
      { kind: 'paragraph', text: '<img src=x onerror=alert(1)>' },
    ])
  })

  it('parses inline code, bold, italics and links; unsafe link targets are dropped', () => {
    expect(parseInline('a `x<y>` **b** *c* [d](https://e.com) [f](javascript:alert(1))')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'code', text: 'x<y>' },
      { kind: 'text', text: ' ' },
      { kind: 'strong', text: 'b' },
      { kind: 'text', text: ' ' },
      { kind: 'em', text: 'c' },
      { kind: 'text', text: ' ' },
      { kind: 'link', text: 'd', href: 'https://e.com/' },
      { kind: 'text', text: ' ' },
      // The balanced "(1)" belongs to the URL now; the unsafe target is still dropped.
      { kind: 'link', text: 'f', href: null },
    ])
  })
})

describe('MarkdownView rendering', () => {
  const render = (markdown: string) =>
    renderToStaticMarkup(createElement(MarkdownView, { markdown }))

  it('escapes <script> and HTML instead of rendering it', () => {
    const html = render(
      '# <b>hi</b>\n\n<script>alert(1)</script>\n\n- <img src=x onerror=alert(1)>',
    )
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('<b>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
  })

  it('renders safe links with rel="noopener noreferrer" and drops javascript: links', () => {
    const html = render('[paper](https://arxiv.org/abs/1) and [bad](javascript:alert(1))')
    expect(html).toContain('href="https://arxiv.org/abs/1"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).toContain('target="_blank"')
    expect(html).not.toContain('javascript:')
  })
})

describe('ResearchPage first render', () => {
  it('renders the tab list, the back button and a loading state without fetching synchronously', async () => {
    const { ResearchPage } = await import('./ResearchPage')
    const never = () => new Promise<never>(() => {})
    const client = {
      runs: never,
      run: never,
      ideas: never,
      journal: never,
      journalEntry: never,
      candidates: never,
      paper: never,
    }
    const labClient = {
      health: never,
      registry: never,
      validate: never,
      backtest: never,
      ledger: never,
      ledgerItem: never,
    }
    const html = renderToStaticMarkup(
      createElement(ResearchPage, {
        client,
        labClient,
        onClose: () => {},
        onShowCandidate: () => {},
        onLoadPlan: () => {},
        testerBusy: false,
      }),
    )
    expect(html).toContain('role="tablist"')
    for (const label of ['Runs', 'Ideas', 'Ledger', 'Journal', 'Candidates']) {
      expect(html).toContain(`>${label}<`)
    }
    expect(html).toContain('Back to chart')
    expect(html).toContain('Loading research runs')
    // The Runs tab has the shared refresh button and says how it refreshes.
    expect(html).toContain('aria-label="Refresh runs"')
    expect(html).toContain('Checks for new runs every 30 s')
  })
})

describe('pickJournalEntry', () => {
  const entry = (name: string): JournalEntry => ({
    name,
    title: name,
    run_id: '',
    date: '',
    outcome: '',
    idea_ids: [],
    size: 0,
  })
  const entries = [entry('b.md'), entry('a.md')]

  it('shows the newest entry when nothing was asked for', () => {
    expect(pickJournalEntry(entries, null)).toEqual({ kind: 'entry', entry: entries[0] })
    expect(pickJournalEntry([], null)).toEqual({ kind: 'empty' })
  })

  it('shows the asked-for entry', () => {
    expect(pickJournalEntry(entries, 'a.md')).toEqual({ kind: 'entry', entry: entries[1] })
  })

  it("reports a run's missing entry instead of falling back to another run's", () => {
    expect(pickJournalEntry(entries, 'run-7.md')).toEqual({ kind: 'missing', name: 'run-7.md' })
    expect(pickJournalEntry([], 'run-7.md')).toEqual({ kind: 'missing', name: 'run-7.md' })
  })
})

describe('plan restore after a candidate replaces the tester plan', () => {
  const mine = { planText: '{"name":"mine"}', planEdited: true }
  const cand = '{"name":"candidate"}'

  it("keeps the user's edited plan to offer back", () => {
    const r = planRestoreAfterLoad(mine, null, cand)
    expect(r).toEqual({ previous: mine.planText, loaded: cand })
    // Valid while the editor holds the candidate's plan.
    expect(planRestoreValid({ planText: cand, planEdited: true }, r)).toBe(true)
  })

  it('offers nothing for the untouched template, an empty plan, or the same plan', () => {
    expect(planRestoreAfterLoad({ planText: '', planEdited: false }, null, cand)).toBeNull()
    expect(planRestoreAfterLoad({ planText: '  ', planEdited: true }, null, cand)).toBeNull()
    expect(planRestoreAfterLoad({ planText: cand, planEdited: true }, null, cand)).toBeNull()
  })

  it("a second candidate keeps the user's original plan, not the first candidate's", () => {
    const first = planRestoreAfterLoad(mine, null, cand)
    const second = planRestoreAfterLoad({ planText: cand, planEdited: true }, first, '{"n":2}')
    expect(second).toEqual({ previous: mine.planText, loaded: '{"n":2}' })
    // Loading the user's own plan text again leaves nothing to restore.
    expect(planRestoreAfterLoad({ planText: cand, planEdited: true }, first, mine.planText)).toBe(
      null,
    )
  })

  it('the offer lapses once the user edits or resets the loaded plan', () => {
    const r = planRestoreAfterLoad(mine, null, cand)
    expect(planRestoreValid({ planText: `${cand} `, planEdited: true }, r)).toBe(false)
    expect(planRestoreValid({ planText: '', planEdited: false }, r)).toBe(false)
    expect(planRestoreValid({ planText: cand, planEdited: true }, null)).toBe(false)
    // A lapsed offer is replaced by the plan in the editor now.
    const edited = { planText: `${cand} edited`, planEdited: true }
    expect(planRestoreAfterLoad(edited, r, '{"n":3}')).toEqual({
      previous: edited.planText,
      loaded: '{"n":3}',
    })
  })
})

describe('plan restore offer survives a page reload', () => {
  function memory(): RemovableStorage & { data: Map<string, string> } {
    const data = new Map<string, string>()
    return {
      data,
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
      removeItem: (k) => void data.delete(k),
    }
  }

  it('round-trips the offer and removes it when cleared', () => {
    const s = memory()
    expect(loadPlanRestore(s)).toBeNull()
    const offer = { previous: '{"name":"mine"}', loaded: '{"name":"candidate"}' }
    savePlanRestore(offer, s)
    expect(loadPlanRestore(s)).toEqual(offer)
    savePlanRestore(null, s)
    expect(s.data.has(PLAN_RESTORE_KEY)).toBe(false)
    expect(loadPlanRestore(s)).toBeNull()
  })

  it('ignores junk and a throwing storage', () => {
    const s = memory()
    for (const junk of [
      'nope',
      '"x"',
      'null',
      '[]',
      '{"previous":1,"loaded":"a"}',
      '{"previous":" ","loaded":"a"}',
    ]) {
      s.data.set(PLAN_RESTORE_KEY, junk)
      expect(loadPlanRestore(s)).toBeNull()
    }
    const bad: RemovableStorage = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('quota')
      },
      removeItem: () => {
        throw new Error('denied')
      },
    }
    expect(loadPlanRestore(bad)).toBeNull()
    expect(() => savePlanRestore({ previous: 'a', loaded: 'b' }, bad)).not.toThrow()
    expect(() => savePlanRestore(null, bad)).not.toThrow()
    expect(loadPlanRestore(null)).toBeNull()
  })

  it('a reloaded offer is still valid against the persisted tester plan', () => {
    const mine = { planText: '{"name":"mine"}', planEdited: true }
    const cand = '{"name":"candidate"}'
    const s = memory()
    savePlanRestore(planRestoreAfterLoad(mine, null, cand), s)
    // After the reload the tester holds the candidate plan (persisted as an edited plan).
    const offer = loadPlanRestore(s)
    expect(planRestoreValid({ planText: cand, planEdited: true }, offer)).toBe(true)
    expect(offer?.previous).toBe(mine.planText)
  })
})

describe('inline parsing stays linear on hostile input', () => {
  const N = 200_000
  const hostile = [
    `[${'a'.repeat(N)}`, // link text that never closes
    `[x](${'b'.repeat(N)}`, // URL that never closes
    '[x]('.repeat(N / 4), // many link starts
    '['.repeat(N),
    '*'.repeat(N),
    `*${'c'.repeat(N)}`,
    '`'.repeat(N),
    `\`${'d'.repeat(N)}`,
    '**'.repeat(N / 2),
    '_'.repeat(N),
  ]

  it('parses each 200k-char hostile line quickly and keeps every character', () => {
    for (const line of hostile) {
      const t0 = performance.now()
      const parts = parseInline(line)
      const ms = performance.now() - t0
      // Generous bound: the old unbounded regex took ~1 s on 50k "[" and never finished on 50k backticks.
      expect(ms).toBeLessThan(1000)
      expect(parts.reduce((n, p) => n + p.text.length, 0)).toBeGreaterThanOrEqual(
        Math.min(line.length, INLINE_MAX_CHARS) - 10,
      )
      expect(parts[parts.length - 1].kind).toBe('text')
    }
  })

  it('formats only the first INLINE_MAX_CHARS of a line; the rest is literal text', () => {
    const tail = '**not bold**'
    const parts = parseInline(`**bold** ${'x'.repeat(INLINE_MAX_CHARS)}${tail}`)
    expect(parts[0]).toEqual({ kind: 'strong', text: 'bold' })
    expect(parts.some((p) => p.kind === 'strong' && p.text === 'not bold')).toBe(false)
    expect(parts[parts.length - 1].text.endsWith(tail)).toBe(true)
  })

  it('keeps balanced parentheses inside a link URL', () => {
    expect(parseInline('[w](https://en.wikipedia.org/wiki/Foo_(bar))')).toEqual([
      { kind: 'link', text: 'w', href: 'https://en.wikipedia.org/wiki/Foo_(bar)' },
    ])
    expect(parseInline('see [w](https://x.org/a_(b)_c "t") now')).toEqual([
      { kind: 'text', text: 'see ' },
      { kind: 'link', text: 'w', href: 'https://x.org/a_(b)_c' },
      { kind: 'text', text: ' now' },
    ])
    // A link inside prose parentheses still ends at its own ")".
    expect(parseInline('(see [w](https://x.org/a))')).toEqual([
      { kind: 'text', text: '(see ' },
      { kind: 'link', text: 'w', href: 'https://x.org/a' },
      { kind: 'text', text: ')' },
    ])
  })

  it('stays fast on hostile parenthesised URLs', () => {
    const N = 200_000
    for (const line of [
      `[x](${'('.repeat(N)}`,
      `[x](a${'(b'.repeat(N / 2)}`,
      `[x](${'(bbbb)'.repeat(N / 6)}`,
      `${'[x](a(b)'.repeat(N / 8)}`,
    ]) {
      const t0 = performance.now()
      const parts = parseInline(line)
      expect(performance.now() - t0).toBeLessThan(1000)
      expect(parts.length).toBeGreaterThan(0)
    }
  })

  it('still parses ordinary links, code, bold and italics', () => {
    expect(parseInline('see [paper](https://arxiv.org/abs/1 "t") `x` **b** *i*')).toEqual([
      { kind: 'text', text: 'see ' },
      { kind: 'link', text: 'paper', href: 'https://arxiv.org/abs/1' },
      { kind: 'text', text: ' ' },
      { kind: 'code', text: 'x' },
      { kind: 'text', text: ' ' },
      { kind: 'strong', text: 'b' },
      { kind: 'text', text: ' ' },
      { kind: 'em', text: 'i' },
    ])
  })
})

describe('ResearchFallback', () => {
  it('renders a contained "failed to render" panel with Reload and Back to chart', () => {
    const html = renderToStaticMarkup(
      createElement(ResearchFallback, {
        error: new Error('<b>bad</b> payload'),
        onReload: () => {},
        onClose: () => {},
      }),
    )
    expect(html).toContain('Research view failed to render')
    expect(html).toContain('>Reload<')
    expect(html).toContain('Back to chart')
    expect(html).toContain('role="alert"')
    // The error text is escaped, never HTML.
    expect(html).toContain('&lt;b&gt;bad&lt;/b&gt; payload')
  })
})

describe('TabFallback', () => {
  const render = (tab: 'runs' | 'journal') =>
    renderToStaticMarkup(
      createElement(TabFallback, {
        tab,
        error: new Error('<i>bad</i> entry'),
        onRetry: () => {},
        onOpenRuns: () => {},
      }),
    )

  it('replaces only the tab that threw, with Try again and a way back to Runs', () => {
    const html = render('journal')
    expect(html).toContain('The Journal tab failed to render')
    expect(html).toContain('>Try again<')
    expect(html).toContain('>Open Runs<')
    expect(html).toContain('role="alert"')
    expect(html).toContain('&lt;i&gt;bad&lt;/i&gt; entry')
  })

  it('does not offer "Open Runs" on the Runs tab itself', () => {
    const html = render('runs')
    expect(html).toContain('The Runs tab failed to render')
    expect(html).not.toContain('Open Runs')
  })
})
