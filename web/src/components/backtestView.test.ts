import { describe, expect, it } from 'vitest'
import { LabError, LAB_OFFLINE_MESSAGE } from '../data/labApi'
import type { BacktestReport, LabMetrics } from '../data/labTypes'
import {
  BACKTEST_TIMEFRAME_MESSAGE,
  backtestSupported,
  DOCK_DEFAULT_HEIGHT,
  DOCK_MIN_HEIGHT,
  EMPTY,
  clampDockHeight,
  loadDockPrefs,
  saveDockPrefs,
  METRIC_COLUMNS,
  METRIC_ROWS,
  MAX_HISTORY_BARS,
  compareMetric,
  costsText,
  describeLabError,
  editorErrors,
  featureSnippet,
  fmtElapsed,
  fmtInt,
  fmtPct,
  fmtRatio,
  fmtSignedPct,
  historyLoadFailed,
  historyLoadReducer,
  historyPlan,
  initialHistoryLoad,
  initTesterState,
  labIntervalFor,
  MAX_HISTORY_FAILURES,
  MAX_HISTORY_IDLE_TICKS,
  planStatus,
  templateIntervalNote,
  tradeFocusable,
  tradeFocusRange,
  VENUE_NOTE,
  walkForwardHeading,
  type HistoryLoadAction,
  type HistoryLoadState,
  isChartInterval,
  lineColumn,
  loadPlanText,
  metricsTable,
  pageInfo,
  pageOffset,
  parsePlanText,
  planTemplate,
  planTemplateText,
  planTextFor,
  reportMatchesChart,
  runBlockedReason,
  savePlanText,
  statItems,
  summaryTiles,
  testerReducer,
  walkForwardRows,
  type PlanStorage,
  type RunErrorView,
  type TesterState,
} from './backtestView'

const M = (over: Partial<LabMetrics> = {}): LabMetrics => ({
  total_return: 0.5,
  cagr: 0.12,
  volatility: 0.4,
  sharpe: 1.234,
  sortino: 1.5,
  max_drawdown: -0.25,
  win_rate: 0.4,
  profit_factor: 1.6,
  n_trades: 120,
  exposure: 0.6,
  turnover: 30,
  bars: 10_000,
  ...over,
})

function report(over: Partial<BacktestReport> = {}): BacktestReport {
  return {
    trial_id: 1,
    run_id: 'ui',
    plan_hash: 'h',
    plan: {},
    symbol: 'BTCUSDT',
    interval: '4h',
    window: { kind: 'design', start: 1_546_300_800, end: 1_700_000_000 },
    costs: { fee_rate: 0.001, slippage_rate: 0.0005, slippage_vol_mult: 0.5 },
    metrics: { net: M(), gross: M({ sharpe: 1.5 }) },
    benchmarks: {
      buy_and_hold: M({ total_return: 0.8, sharpe: 0.9, max_drawdown: -0.75 }),
      vol_target: M({ sharpe: 1.0 }),
      random_entry: { n_paths: 200, sharpe_median: 0.2, sharpe_p95: 1.1, percentile: 97 },
    },
    stats: {
      dsr: { dsr: 0.41, psr: 0.98, n_trials: 12, sr0_annual: 1.1 },
      pbo: { pbo: 0.35, n_trials: 16, n_splits: 16 },
      sharpe_ci: { low: -0.1, high: 2.2, level: 0.95 },
    },
    walk_forward: [
      {
        fold: 0,
        test_start: 1_600_000_000,
        test_end: 1_615_000_000,
        sharpe: 1.1,
        total_return: 0.05,
        n_trades: 4,
      },
    ],
    trades: [],
    equity: [],
    benchmark_equity: { buy_and_hold: [], vol_target: [] },
    warnings: [],
    disclaimer: 'Historical simulation, not forward-looking.',
    elapsed_ms: 900,
    ...over,
  }
}

describe('plan template', () => {
  it('is an EMA 20/50 crossover for the chart symbol and interval', () => {
    const plan = planTemplate('ETHUSDT', '1h')
    expect(plan).toMatchObject({
      symbol: 'ETHUSDT',
      interval: '1h',
      features: { ema_fast: { ema: { period: 20 } }, ema_slow: { ema: { period: 50 } } },
      long_entry: 'crosses_above(ema_fast, ema_slow)',
      long_exit: 'crosses_below(ema_fast, ema_slow)',
      risk: { position_size_pct: 100 },
    })
    expect(JSON.parse(planTemplateText('ETHUSDT', '1h'))).toEqual(plan)
  })
})

describe('parsePlanText', () => {
  it('accepts a JSON object', () => {
    expect(parsePlanText('{"a":1}')).toEqual({ ok: true, plan: { a: 1 } })
  })

  it('rejects empty text, arrays and invalid JSON with a (json) path', () => {
    for (const text of ['', '[]', '42', '{"a":']) {
      const r = parsePlanText(text)
      expect(r.ok).toBe(false)
      if (!r.ok) expect(r.error.path).toBe('(json)')
    }
  })

  it('converts a character offset to line and column', () => {
    expect(lineColumn('ab\ncd\nef', 4)).toEqual({ line: 2, column: 2 })
    expect(lineColumn('x', 0)).toEqual({ line: 1, column: 1 })
  })

  it('builds a features snippet from registry defaults', () => {
    expect(
      featureSnippet({
        name: 'rsi',
        description: '',
        outputs: ['value'],
        params: [{ name: 'period', kind: 'int', default: 14, low: 2, high: 100, description: '' }],
      }),
    ).toBe('{"rsi":{"period":14}}')
  })
})

describe('formatting', () => {
  it('formats fractions as percent with a real minus sign and null as a dash', () => {
    expect(fmtPct(0.1234)).toBe('12.34%')
    expect(fmtPct(-0.25)).toBe('−25.00%')
    expect(fmtPct(null)).toBe(EMPTY)
    expect(fmtPct(-0.00001)).toBe('0.00%')
    expect(fmtSignedPct(0.05)).toBe('+5.00%')
    expect(fmtSignedPct(-0.05)).toBe('−5.00%')
    expect(fmtSignedPct(0)).toBe('0.00%')
  })

  it('formats ratios to 2 dp and counts with separators', () => {
    expect(fmtRatio(1.236)).toBe('1.24')
    expect(fmtRatio(-0.5)).toBe('−0.50')
    expect(fmtRatio(null)).toBe(EMPTY)
    expect(fmtInt(12345)).toBe('12,345')
  })

  it('formats elapsed time', () => {
    expect(fmtElapsed(812)).toBe('812 ms')
    expect(fmtElapsed(12_340)).toBe('12.3 s')
    expect(fmtElapsed(125_000)).toBe('2 min 05 s')
  })

  it('describes the costs used', () => {
    expect(costsText(report().costs)).toBe(
      'Fee 0.10% · Slippage 0.05%, widened ×0.50 on wide bars · per side',
    )
  })
})

describe('metricsTable', () => {
  it('has a row for every LabMetrics field and a cell per column', () => {
    const rows = metricsTable(report())
    expect(rows.map((r) => r.key).sort()).toEqual(Object.keys(M()).sort())
    expect(rows.every((r) => r.cells.length === METRIC_COLUMNS.length)).toBe(true)
    expect(METRIC_ROWS).toHaveLength(12)
  })

  it('formats each kind and shows null as a dash', () => {
    const rows = metricsTable(report({ metrics: { net: M({ sortino: null }), gross: M() } }))
    const byKey = new Map(rows.map((r) => [r.key, r]))
    expect(byKey.get('total_return')?.cells[0]).toBe('50.00%')
    expect(byKey.get('sharpe')?.cells).toEqual(['1.23', '1.23', '0.90', '1.00'])
    expect(byKey.get('sortino')?.cells[0]).toBe(EMPTY)
    expect(byKey.get('n_trades')?.cells[0]).toBe('120')
    expect(byKey.get('bars')?.cells[0]).toBe('10,000')
  })

  it('compares the net strategy with buy-and-hold', () => {
    const byKey = new Map(metricsTable(report()).map((r) => [r.key, r.compare]))
    expect(byKey.get('total_return')).toBe('worse')
    expect(byKey.get('sharpe')).toBe('better')
    expect(byKey.get('max_drawdown')).toBe('better') // −25% beats −75%
    expect(byKey.get('volatility')).toBeNull()
    expect(compareMetric('higher', null, 1)).toBeNull()
  })
})

describe('statItems', () => {
  it('flags a DSR below 0.95 as not significant after N trials', () => {
    const dsr = statItems(report()).find((s) => s.id === 'dsr')
    expect(dsr?.value).toBe('0.41')
    expect(dsr?.hint).toBe('DSR < 0.95: not significant after 12 trials')
    expect(dsr?.tone).toBe('bad')
  })

  it('passes a DSR at or above 0.95', () => {
    const r = report()
    r.stats.dsr.dsr = 0.97
    expect(statItems(r).find((s) => s.id === 'dsr')?.tone).toBe('good')
  })

  it('grades PBO against 0.2 and 0.5 and explains a missing value', () => {
    const grade = (pbo: number | null) => {
      const r = report()
      r.stats.pbo.pbo = pbo
      return statItems(r).find((s) => s.id === 'pbo')
    }
    expect(grade(0.1)?.tone).toBe('good')
    expect(grade(0.35)?.tone).toBe('warn')
    expect(grade(0.7)?.tone).toBe('bad')
    expect(grade(null)?.value).toBe(EMPTY)
    expect(grade(null)?.hint).toContain('16 trials, 16 splits')
  })

  it('warns when the Sharpe CI includes zero', () => {
    const ci = statItems(report()).find((s) => s.id === 'sharpe_ci')
    expect(ci?.label).toBe('Sharpe 95% CI')
    expect(ci?.value).toBe('−0.10 … 2.20')
    expect(ci?.hint).toMatch(/^Includes 0/)
    expect(ci?.tone).toBe('warn')
  })

  it('reports the random-entry percentile', () => {
    const re = statItems(report()).find((s) => s.id === 'random_entry')
    expect(re?.value).toBe('97th pct')
    expect(re?.tone).toBe('good')
    expect(re?.hint).toContain('200 random paths')
  })

  it('covers DSR, PSR, PBO, Sharpe CI and random entry', () => {
    expect(statItems(report()).map((s) => s.id)).toEqual([
      'dsr',
      'psr',
      'pbo',
      'sharpe_ci',
      'random_entry',
    ])
  })
})

describe('summary and walk-forward', () => {
  it('shows net return and the difference to buy-and-hold', () => {
    const tiles = summaryTiles(report())
    expect(tiles[0]).toMatchObject({ label: 'Net return', value: '+50.00%', tone: 'good' })
    expect(tiles[1]).toMatchObject({ value: '−30.00%', tone: 'bad', sub: 'B&H +80.00%' })
  })

  it('formats walk-forward folds 1-based with dates', () => {
    expect(walkForwardRows(report().walk_forward)[0]).toEqual({
      fold: '1',
      period: '2020-09-13 → 2021-03-06',
      sharpe: '1.10',
      totalReturn: '+5.00%',
      trades: '4',
      tone: 'good',
    })
  })
})

describe('describeLabError', () => {
  it('explains how to start the lab when it is offline', () => {
    const v = describeLabError(new LabError('offline', 'x'))
    expect(v.kind).toBe('offline')
    expect(v.message).toBe(LAB_OFFLINE_MESSAGE)
  })

  it('shows the server message when data is missing', () => {
    const v = describeLabError(new LabError('no-data', 'Run `uv run lab download BTCUSDT 4h`', 503))
    expect(v).toMatchObject({ kind: 'no-data', message: 'Run `uv run lab download BTCUSDT 4h`' })
  })

  it('carries plan errors', () => {
    const errors = [{ path: 'risk', message: 'bad' }]
    expect(describeLabError(new LabError('invalid', 'risk: bad', 422, errors)).errors).toEqual(
      errors,
    )
  })

  it('handles unknown errors', () => {
    expect(describeLabError(new Error('boom'))).toMatchObject({ kind: 'error', message: 'boom' })
  })
})

const offline: RunErrorView = { kind: 'offline', title: 't', message: 'm', errors: [] }

function fresh(): TesterState {
  return initTesterState()
}

describe('testerReducer', () => {
  it('shows the template for the chart market until the user edits it', () => {
    let s = fresh()
    expect(JSON.parse(planTextFor(s, 'BTCUSDT', '4h')).symbol).toBe('BTCUSDT')
    expect(JSON.parse(planTextFor(s, 'ETHUSDT', '1h'))).toMatchObject({
      symbol: 'ETHUSDT',
      interval: '1h',
    })
    s = testerReducer(s, { type: 'edit', text: '{"name":"mine"}' })
    expect(planTextFor(s, 'SOLUSDT', '1d')).toBe('{"name":"mine"}')
    s = testerReducer(s, { type: 'reset-template' })
    expect(s.planEdited).toBe(false)
    expect(JSON.parse(planTextFor(s, 'SOLUSDT', '1d')).symbol).toBe('SOLUSDT')
  })

  it('restores an edited plan from storage and ignores an unedited one', () => {
    const s = initTesterState({ text: '{"x":1}', edited: true })
    expect(planTextFor(s, 'BTCUSDT', '4h')).toBe('{"x":1}')
    const t = initTesterState({ text: '{"x":1}', edited: false })
    expect(planTextFor(t, 'BTCUSDT', '4h')).toBe(planTemplateText('BTCUSDT', '4h'))
  })

  it('records validation per exact text', () => {
    let s = fresh()
    s = testerReducer(s, { type: 'validate-start', text: 'A' })
    expect(s.validation).toEqual({ kind: 'checking', text: 'A' })
    s = testerReducer(s, {
      type: 'validate-done',
      text: 'A',
      ok: false,
      errors: [{ path: 'name', message: 'required' }],
    })
    expect(s.validation.kind).toBe('invalid')
    expect(s.lab).toBe('online')
    const ok = parsePlanText('{}')
    expect(editorErrors(s, 'A', ok)).toHaveLength(1)
    expect(editorErrors(s, 'B', ok)).toHaveLength(0)
  })

  it('marks the lab offline when validation cannot reach it', () => {
    const s = testerReducer(fresh(), { type: 'validate-fail', text: 'A', error: offline })
    expect(s.lab).toBe('offline')
    expect(s.validation.kind).toBe('unavailable')
  })

  it('runs one backtest at a time, ignores stale answers and shows the result on the chart', () => {
    let s = testerReducer(fresh(), { type: 'run-start', startedAt: 10 })
    expect(s.run).toEqual({ kind: 'running', id: 1, startedAt: 10 })
    expect(testerReducer(s, { type: 'run-start', startedAt: 11 })).toBe(s)
    const r = report()
    expect(testerReducer(s, { type: 'run-done', id: 99, report: r })).toBe(s)
    s = testerReducer(s, { type: 'run-done', id: 1, report: r })
    expect(s.report).toBe(r)
    expect(s.shownOnChart).toBe(true)
    expect(s.run.kind).toBe('idle')
  })

  it('marks the lab offline on an offline failure and keeps the last report', () => {
    let s = testerReducer(fresh(), { type: 'run-start', startedAt: 0 })
    s = testerReducer(s, { type: 'run-done', id: 1, report: report() })
    s = testerReducer(s, { type: 'run-start', startedAt: 1 })
    s = testerReducer(s, { type: 'run-fail', id: 2, text: 'A', error: offline })
    expect(s.lab).toBe('offline')
    expect(s.run.kind).toBe('error')
    expect(s.report).not.toBeNull()
    s = testerReducer(s, { type: 'dismiss-error' })
    expect(s.run.kind).toBe('idle')
  })

  it('moves 422 plan errors from a run into the editor', () => {
    let s = testerReducer(fresh(), { type: 'run-start', startedAt: 0 })
    const errors = [{ path: 'long_entry', message: 'unknown name' }]
    s = testerReducer(s, {
      type: 'run-fail',
      id: 1,
      text: '{}',
      error: { kind: 'invalid', title: '', message: '', errors },
    })
    expect(editorErrors(s, '{}', parsePlanText('{}'))).toEqual(errors)
  })

  it('toggles the chart overlay', () => {
    const s = testerReducer(fresh(), { type: 'show-on-chart', shown: true })
    expect(s.shownOnChart).toBe(true)
    expect(testerReducer(s, { type: 'show-on-chart', shown: true })).toBe(s)
  })
})

describe('runBlockedReason', () => {
  it('blocks while running, on invalid JSON, on plan errors, and when the lab is offline', () => {
    const s = fresh()
    const text = planTextFor(s, 'BTCUSDT', '4h')
    const ok = parsePlanText(text)
    expect(runBlockedReason(s, text, ok)).toBeNull()
    expect(runBlockedReason(s, '{', parsePlanText('{'))).toBe('Fix the JSON first')
    const running = testerReducer(s, { type: 'run-start', startedAt: 0 })
    expect(runBlockedReason(running, text, ok)).toBe('A backtest is running')
    const invalid = testerReducer(s, {
      type: 'validate-done',
      text,
      ok: false,
      errors: [{ path: 'x', message: 'y' }],
    })
    expect(runBlockedReason(invalid, text, ok)).toBe('Fix the plan errors first')
    const off = testerReducer(s, { type: 'lab-status', status: 'offline' })
    expect(runBlockedReason(off, text, ok)).toBe(LAB_OFFLINE_MESSAGE)
  })
})

describe('non-native timeframes', () => {
  it('backtestSupported accepts exactly the native intervals', () => {
    for (const tf of ['1m', '5m', '15m', '1h', '4h', '1d', '1w'] as const) {
      expect(backtestSupported(tf)).toBe(true)
    }
    for (const tf of ['1s', '5s', '30s', '3m', '30m', '2h', '12h', '3d', '2w'] as const) {
      expect(backtestSupported(tf)).toBe(false)
    }
  })

  it('blocks Run on a seconds or custom chart timeframe with the explanation', () => {
    const s = fresh()
    const text = planTextFor(s, 'BTCUSDT', '4h')
    const ok = parsePlanText(text)
    expect(runBlockedReason(s, text, ok, '4h')).toBeNull()
    expect(runBlockedReason(s, text, ok, '1w')).toBeNull()
    expect(runBlockedReason(s, text, ok, '3m')).toBe(BACKTEST_TIMEFRAME_MESSAGE)
    expect(runBlockedReason(s, text, ok, '5s')).toBe(BACKTEST_TIMEFRAME_MESSAGE)
    expect(BACKTEST_TIMEFRAME_MESSAGE).toBe(
      'Backtests run on native intervals (1m–1W); switch timeframe',
    )
    // A running backtest still says so first.
    const running = testerReducer(s, { type: 'run-start', startedAt: 0 })
    expect(runBlockedReason(running, text, ok, '3m')).toBe('A backtest is running')
  })

  it('the plan chip says "Not checked" on an unsupported timeframe, without announcing', () => {
    const s = fresh()
    const text = planTextFor(s, 'BTCUSDT', '2h')
    expect(planStatus(s, text, true, 0, '2h')).toEqual({
      tone: 'warn',
      text: 'Not checked',
      announce: '',
    })
    expect(planStatus(s, text, true, 0, '4h').text).toBe('Checking…')
    expect(planStatus(s, '{', false, 1, '2h').text).toBe('Invalid JSON')
  })

  it('plans history with the custom timeframe length', () => {
    const w = { kind: 'design' as const, start: 0, end: 10 * 7200 }
    expect(historyPlan(10 * 7200, w, '2h')).toMatchObject({ needed: true, bars: 10 })
    expect(historyPlan(100, w, '5s')).toMatchObject({ needed: true, bars: 20 })
  })
})

describe('chart link', () => {
  it('matches the report to the chart by symbol and interval', () => {
    expect(reportMatchesChart(report(), 'BTCUSDT', '4h')).toBe(true)
    expect(reportMatchesChart(report(), 'BTCUSDT', '1h')).toBe(false)
    expect(reportMatchesChart(report(), 'ETHUSDT', '4h')).toBe(false)
  })

  it('accepts only intervals the chart supports', () => {
    expect(isChartInterval('4h')).toBe(true)
    expect(isChartInterval('3m')).toBe(false)
  })

  it('plans the older bars needed to show the design window', () => {
    const h = 3600
    const w = { kind: 'design' as const, start: 0, end: (MAX_HISTORY_BARS + 100) * h }
    expect(historyPlan(null, w, '1h').needed).toBe(false)
    expect(historyPlan(0, w, '1h').needed).toBe(false)
    expect(historyPlan(10 * h, w, '1h')).toEqual({
      needed: true,
      target: 0,
      bars: 10,
      partial: false,
      unreachable: false,
    })
    const far = historyPlan((MAX_HISTORY_BARS + 5) * h, w, '1h')
    expect(far).toEqual({
      needed: true,
      target: 5 * h,
      bars: MAX_HISTORY_BARS,
      partial: true,
      unreachable: false,
    })
  })

  it('marks a design window that ends beyond the bar limit as unreachable', () => {
    // 1m chart whose first bar is 30,000 minutes after a window that ended 25,000 minutes
    // before it: 20,000 more bars reach back only to 10,000 minutes after the window end.
    const m = 60
    const first = 1_700_000_000
    const w = { kind: 'design' as const, start: first - 40_000 * m, end: first - 25_000 * m }
    expect(historyPlan(first, w, '1m')).toEqual({
      needed: true,
      target: first - MAX_HISTORY_BARS * m,
      bars: 0,
      partial: true,
      unreachable: true,
    })
  })

  it('treats a window ending exactly at the reachable limit as unreachable', () => {
    const m = 60
    const first = 1_700_000_000
    const w = {
      kind: 'design' as const,
      start: first - 30_000 * m,
      end: first - MAX_HISTORY_BARS * m,
    }
    expect(historyPlan(first, w, '1m').unreachable).toBe(true)
    const justInside = { ...w, end: w.end + m }
    expect(historyPlan(first, justInside, '1m')).toMatchObject({
      unreachable: false,
      partial: true,
      bars: MAX_HISTORY_BARS,
    })
  })
})

describe('ledger paging', () => {
  it('labels the visible range and knows about neighbours', () => {
    expect(pageInfo(51, 25, 25)).toEqual({
      from: 26,
      to: 50,
      hasPrev: true,
      hasNext: true,
      label: '26–50 of 51',
    })
    expect(pageInfo(0, 0, 0).label).toBe('No runs')
    expect(pageInfo(51, 50, 1).hasNext).toBe(false)
  })

  it('clamps page offsets', () => {
    expect(pageOffset(0, 25, -1, 100)).toBe(0)
    expect(pageOffset(25, 25, 1, 100)).toBe(50)
    expect(pageOffset(75, 25, 1, 100)).toBe(75)
  })
})

describe('plan storage', () => {
  function memory(): PlanStorage & { data: Map<string, string> } {
    const data = new Map<string, string>()
    return {
      data,
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
    }
  }

  it('round-trips the plan text', () => {
    const s = memory()
    savePlanText('{"a":1}', true, s)
    expect(loadPlanText(s)).toEqual({ text: '{"a":1}', edited: true })
  })

  it('returns null for missing or corrupt data and survives a throwing storage', () => {
    const s = memory()
    expect(loadPlanText(s)).toBeNull()
    s.data.set('tv.backtest.plan.v1', 'not json')
    expect(loadPlanText(s)).toBeNull()
    const throwing: PlanStorage = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }
    expect(loadPlanText(throwing)).toBeNull()
    expect(() => savePlanText('x', true, throwing)).not.toThrow()
  })
})

describe('dock prefs', () => {
  it('clamps the dock height between the minimum and a share of the viewport', () => {
    expect(clampDockHeight(50, 1000)).toBe(DOCK_MIN_HEIGHT)
    expect(clampDockHeight(5000, 1000)).toBe(720)
    expect(clampDockHeight(Number.NaN, 1000)).toBe(DOCK_DEFAULT_HEIGHT)
    expect(clampDockHeight(300, 100)).toBe(DOCK_MIN_HEIGHT)
  })

  it('round-trips open state and height, defaulting when unreadable', () => {
    const data = new Map<string, string>()
    const s: PlanStorage = {
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
    }
    expect(loadDockPrefs(s)).toEqual({ open: false, height: DOCK_DEFAULT_HEIGHT })
    saveDockPrefs({ open: true, height: 410 }, s)
    expect(loadDockPrefs(s)).toEqual({ open: true, height: 410 })
    data.set('tv.backtest.dock.v1', '{bad')
    expect(loadDockPrefs(s).open).toBe(false)
  })
})

describe('history cap', () => {
  it('loads nothing more once the chart holds the maximum bars', () => {
    const w = { kind: 'design' as const, start: 0, end: 200 * 3600 }
    expect(historyPlan(100 * 3600, w, '1h', 0)).toEqual({
      needed: true,
      target: 100 * 3600,
      bars: 0,
      partial: true,
      unreachable: false,
    })
  })

  it('is unreachable when the chart is full and the window ends before its first bar', () => {
    const w = { kind: 'design' as const, start: 0, end: 50 * 3600 }
    expect(historyPlan(100 * 3600, w, '1h', 0).unreachable).toBe(true)
  })
})

describe('1w chart interval', () => {
  it('builds the template on 1d, which the lab has, instead of 1w', () => {
    const plan = planTemplate('BTCUSDT', '1w')
    expect(plan.interval).toBe('1d')
    expect(plan.name).toBe('ema_cross_20_50_btcusdt_1d')
  })

  it('keeps every other chart interval as is', () => {
    for (const i of ['1m', '5m', '15m', '1h', '4h', '1d']) expect(labIntervalFor(i)).toBe(i)
  })

  it('explains the fallback only for 1w', () => {
    expect(templateIntervalNote('1w')).toBe('The lab has no 1w bars, so the template uses 1d.')
    expect(templateIntervalNote('4h')).toBeNull()
  })
})

describe('neighbourhood PBO', () => {
  const grade = (pbo: number) => {
    const r = report()
    r.stats.pbo = { pbo, n_trials: 9, n_splits: 8, basis: 'neighbourhood' }
    return statItems(r).find((s) => s.id === 'pbo')
  }

  it('labels a neighbourhood PBO and says what it measures', () => {
    const item = grade(0.1)
    expect(item?.label).toBe('PBO (±20% neighbourhood)')
    expect(item?.hint).toContain('measures parameter stability, not search overfitting')
  })

  it('grades a neighbourhood PBO on the Phase 8 bar (≤ 0.2 good, > 0.5 bad)', () => {
    expect(grade(0.2)?.tone).toBe('good')
    expect(grade(0.21)?.tone).toBe('warn')
    expect(grade(0.5)?.tone).toBe('warn')
    expect(grade(0.51)?.tone).toBe('bad')
  })

  it('keeps the classic label without a basis', () => {
    expect(statItems(report()).find((s) => s.id === 'pbo')?.label).toBe(
      'Overfitting probability (PBO)',
    )
  })
})

describe('walkForwardHeading', () => {
  it('calls fixed-parameter folds a fold check, not out-of-sample', () => {
    const h = walkForwardHeading({ walk_forward_kind: 'fixed_params' })
    expect(h.title).toBe('Fold check (fixed parameters — not out-of-sample)')
    expect(h.hint).not.toBeNull()
  })

  it('keeps the walk-forward title for older reports', () => {
    expect(walkForwardHeading({})).toEqual({
      title: 'Walk-forward (out-of-sample folds)',
      hint: null,
    })
  })
})

describe('venue note', () => {
  it('says chart and backtests share the Binance spot data', () => {
    expect(VENUE_NOTE).toContain('data.binance.vision')
    expect(VENUE_NOTE).toContain('same Binance spot data')
    expect(VENUE_NOTE).not.toContain('Binance.US')
  })
})

describe('planStatus', () => {
  const text = '{"a":1}'
  const base = initTesterState()

  it('announces nothing while the lab is checking', () => {
    const s = testerReducer(base, { type: 'validate-start', text })
    expect(planStatus(s, text, true, 0)).toEqual({
      tone: 'neutral',
      text: 'Checking…',
      announce: '',
    })
  })

  it('announces nothing for a local JSON error while typing', () => {
    expect(planStatus(base, '{', false, 1).announce).toBe('')
    expect(planStatus(base, '{', false, 1).text).toBe('Invalid JSON')
  })

  it('announces the settled lab verdict for the current text', () => {
    const valid = testerReducer(base, { type: 'validate-done', text, ok: true, errors: [] })
    expect(planStatus(valid, text, true, 0)).toMatchObject({
      text: 'Valid',
      announce: 'Plan valid',
    })
    const errs = [
      { path: 'a', message: 'x' },
      { path: 'b', message: 'y' },
    ]
    const invalid = testerReducer(base, { type: 'validate-done', text, ok: false, errors: errs })
    expect(planStatus(invalid, text, true, 2)).toMatchObject({
      text: '2 errors',
      announce: 'Plan has 2 errors',
    })
  })

  it('does not announce a verdict for older text', () => {
    const valid = testerReducer(base, { type: 'validate-done', text, ok: true, errors: [] })
    expect(planStatus(valid, '{"a":2}', true, 0).announce).toBe('')
  })

  it('says not checked when the lab is offline', () => {
    const off = testerReducer(base, { type: 'lab-status', status: 'offline' })
    expect(planStatus(off, text, true, 0)).toMatchObject({ tone: 'warn', text: 'Not checked' })
  })
})

describe('historyLoadReducer', () => {
  const target = { symbol: 'BTCUSDT', interval: '1h' as const, target: 1000 }
  const run = (actions: HistoryLoadAction[], from: HistoryLoadState = initialHistoryLoad) =>
    actions.reduce(historyLoadReducer, from)
  const started = run([{ type: 'start', target }])
  const failPage: HistoryLoadAction[] = [
    { type: 'request' },
    { type: 'page-done', progressed: false },
  ]

  it('keeps loading through fewer than the maximum consecutive failures', () => {
    const s = run(
      Array(MAX_HISTORY_FAILURES - 1)
        .fill(failPage)
        .flat(),
      started,
    )
    expect(s.target).toEqual(target)
    expect(s.failures).toBe(MAX_HISTORY_FAILURES - 1)
    expect(s.failed).toBeNull()
  })

  it('gives up after the maximum consecutive failed pages', () => {
    const s = run(Array(MAX_HISTORY_FAILURES).fill(failPage).flat(), started)
    expect(s.target).toBeNull()
    expect(historyLoadFailed(s, 'BTCUSDT', '1h')).toBe(true)
    expect(historyLoadFailed(s, 'BTCUSDT', '4h')).toBe(false)
  })

  it('resets the failure count when a page adds bars', () => {
    const s = run(
      [...failPage, ...failPage, { type: 'request' }, { type: 'page-done', progressed: true }],
      started,
    )
    expect(s.failures).toBe(0)
    expect(s.target).toEqual(target)
  })

  it('gives up when retry ticks never start a request (e.g. offline)', () => {
    const ticks: HistoryLoadAction[] = Array(MAX_HISTORY_IDLE_TICKS).fill({ type: 'tick' })
    const s = run(ticks, started)
    expect(s.target).toBeNull()
    expect(s.failed).toEqual({ symbol: 'BTCUSDT', interval: '1h' })
  })

  it('counts idle ticks only since the last request', () => {
    const ticks: HistoryLoadAction[] = Array(MAX_HISTORY_IDLE_TICKS - 1).fill({ type: 'tick' })
    const s = run([...ticks, { type: 'request' }, ...ticks], started)
    expect(s.target).toEqual(target)
  })

  it('clears the failure notice on retry (start) and on stop', () => {
    const failed = run(Array(MAX_HISTORY_FAILURES).fill(failPage).flat(), started)
    const retried = historyLoadReducer(failed, { type: 'start', target })
    expect(retried).toEqual({ target, failures: 0, idleTicks: 0, failed: null })
    expect(historyLoadReducer(failed, { type: 'stop' })).toEqual(initialHistoryLoad)
  })

  it('ignores page results and ticks while no load runs', () => {
    expect(run([{ type: 'page-done', progressed: false }, { type: 'tick' }])).toBe(
      initialHistoryLoad,
    )
  })
})

describe('trade focus', () => {
  it('pads a trade range by 60% of its length, at least a day', () => {
    const day = 86_400
    expect(tradeFocusRange({ entry_time: 10 * day, exit_time: 20 * day }, 0)).toEqual({
      from: 4 * day,
      to: 26 * day,
    })
    expect(tradeFocusRange({ entry_time: 1000, exit_time: 2000 }, 0)).toEqual({
      from: 1000 - day,
      to: 2000 + day,
    })
  })

  it('runs an open trade to the window end', () => {
    const day = 86_400
    expect(tradeFocusRange({ entry_time: 0, exit_time: null }, 10 * day)).toEqual({
      from: -6 * day,
      to: 16 * day,
    })
  })

  it('makes a trade clickable only when drawn and its entry bar is loaded', () => {
    expect(tradeFocusable({ entry_time: 500 }, true, 500)).toBe(true)
    expect(tradeFocusable({ entry_time: 499 }, true, 500)).toBe(false)
    expect(tradeFocusable({ entry_time: 900 }, false, 500)).toBe(false)
    expect(tradeFocusable({ entry_time: 900 }, true, null)).toBe(false)
  })
})
