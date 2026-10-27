import { describe, expect, it } from 'vitest'
import {
  DEFAULT_DISCLAIMER,
  LAB_OFFLINE_MESSAGE,
  LabError,
  createLabClient,
  parseBacktestReport,
  parseEquity,
  parseLedgerItem,
  parseDerivativesResponse,
  parseMetrics,
  parseRegimeResponse,
} from './labApi'
import type { FetchLike } from './persistence'

interface Call {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(respond: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = []
  const fetchImpl: FetchLike = async (url, init) => {
    const call: Call = {
      url,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    }
    calls.push(call)
    return respond(call)
  }
  return { fetchImpl, calls }
}

const METRICS = {
  total_return: 0.42,
  cagr: 0.1,
  volatility: 0.5,
  sharpe: 1.2,
  sortino: 1.8,
  max_drawdown: -0.3,
  win_rate: 0.45,
  profit_factor: 1.4,
  n_trades: 30,
  exposure: 0.55,
  turnover: 12,
  bars: 1000,
}

function rawReport(): Record<string, unknown> {
  return {
    trial_id: 7,
    run_id: 'ui',
    plan_hash: 'abc123',
    plan: { name: 'ema' },
    symbol: 'BTCUSDT',
    interval: '4h',
    window: { kind: 'design', start: 1_546_300_800, end: 1_700_000_000 },
    costs: { fee_rate: 0.001, slippage_rate: 0.0005, slippage_vol_mult: 0.5 },
    metrics: { net: METRICS, gross: { ...METRICS, sharpe: 1.4 } },
    benchmarks: {
      buy_and_hold: { ...METRICS, sharpe: 0.9 },
      vol_target: { ...METRICS, sharpe: 1.0 },
      random_entry: { n_paths: 200, sharpe_median: 0.1, sharpe_p95: 0.9, percentile: 97.5 },
    },
    stats: {
      dsr: { dsr: 0.5, psr: 0.97, n_trials: 12, sr0_annual: 0.8 },
      pbo: { pbo: 0.31, n_trials: 16, n_splits: 16 },
      sharpe_ci: { low: 0.2, high: 2.1, level: 0.95 },
    },
    walk_forward: [
      { fold: 0, test_start: 1, test_end: 2, sharpe: null, total_return: 0.1, n_trades: 3 },
    ],
    trades: [
      {
        entry_time: 2000,
        entry_price: 101,
        exit_time: 3000,
        exit_price: 99,
        pnl: -2,
        return_pct: -0.02,
        exit_reason: 'stop_loss',
      },
      {
        entry_time: 1000,
        entry_price: 100,
        exit_time: 1500,
        exit_price: 110,
        pnl: 10,
        return_pct: 0.1,
        exit_reason: 'signal',
      },
    ],
    equity: [
      { time: 2, value: 1.1 },
      { time: 1, value: 1 },
    ],
    benchmark_equity: { buy_and_hold: [{ time: 1, value: 1 }], vol_target: [] },
    warnings: ['few trades', 3],
    disclaimer: 'Historical simulation, not forward-looking.',
    elapsed_ms: 812,
  }
}

describe('parse helpers', () => {
  it('maps null and missing metric values to null and keeps counts numeric', () => {
    const m = parseMetrics({ ...METRICS, sharpe: null, sortino: undefined, n_trades: null })
    expect(m.sharpe).toBeNull()
    expect(m.sortino).toBeNull()
    expect(m.n_trades).toBe(0)
    expect(m.total_return).toBe(0.42)
  })

  it('sorts equity, drops non-finite and duplicate times', () => {
    expect(
      parseEquity([
        { time: 3, value: 1.2 },
        { time: 1, value: 1 },
        { time: 2, value: null },
        { time: 3, value: 9 },
      ]),
    ).toEqual([
      { time: 1, value: 1 },
      { time: 3, value: 1.2 },
    ])
  })

  it('parses a full report, sorting trades by entry time', () => {
    const r = parseBacktestReport(rawReport())
    expect(r.symbol).toBe('BTCUSDT')
    expect(r.trades.map((t) => t.entry_time)).toEqual([1000, 2000])
    expect(r.equity.map((p) => p.time)).toEqual([1, 2])
    expect(r.metrics.gross.sharpe).toBe(1.4)
    expect(r.walk_forward[0].sharpe).toBeNull()
    expect(r.warnings).toEqual(['few trades'])
    expect(r.stats.dsr.n_trials).toBe(12)
    expect(r.benchmarks.random_entry.percentile).toBe(97.5)
  })

  it('falls back to the default disclaimer when the report has none', () => {
    expect(parseBacktestReport({ ...rawReport(), disclaimer: '' }).disclaimer).toBe(
      DEFAULT_DISCLAIMER,
    )
  })

  it('leaves walk_forward_kind and pbo.basis out of an older report that lacks them', () => {
    const r = parseBacktestReport(rawReport())
    expect('walk_forward_kind' in r).toBe(false)
    expect('basis' in r.stats.pbo).toBe(false)
  })

  it('keeps walk_forward_kind and pbo.basis from a newer report', () => {
    const raw = rawReport()
    const stats = raw.stats as Record<string, unknown>
    const r = parseBacktestReport({
      ...raw,
      walk_forward_kind: 'fixed_params',
      stats: { ...stats, pbo: { pbo: 0.1, n_trials: 9, n_splits: 8, basis: 'neighbourhood' } },
    })
    expect(r.walk_forward_kind).toBe('fixed_params')
    expect(r.stats.pbo.basis).toBe('neighbourhood')
  })

  it('drops unknown walk_forward_kind and pbo.basis values', () => {
    const raw = rawReport()
    const stats = raw.stats as Record<string, unknown>
    const r = parseBacktestReport({
      ...raw,
      walk_forward_kind: 'rolling',
      stats: { ...stats, pbo: { pbo: 0.1, n_trials: 9, n_splits: 8, basis: 'cscv' } },
    })
    expect(r.walk_forward_kind).toBeUndefined()
    expect(r.stats.pbo.basis).toBeUndefined()
  })

  it('rejects a report without metrics or a window', () => {
    expect(() => parseBacktestReport({ ...rawReport(), metrics: null })).toThrow(LabError)
    expect(() => parseBacktestReport({ ...rawReport(), window: {} })).toThrow(LabError)
  })

  it('skips malformed ledger rows', () => {
    expect(parseLedgerItem({ id: 'x', created_at: 1 })).toBeNull()
    expect(
      parseLedgerItem({ id: 3, created_at: 10, status: 'error', error: 'boom' }),
    ).toMatchObject({ id: 3, status: 'error', error: 'boom', sharpe: null })
  })
})

describe('createLabClient', () => {
  it('posts a backtest to /lab/backtest and parses the report', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, rawReport()))
    const client = createLabClient({ fetchImpl })
    const report = await client.backtest({ name: 'ema' }, { runId: 'r1' })
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe('/lab/backtest')
    expect(calls[0].method).toBe('POST')
    expect(calls[0].body).toEqual({ plan: { name: 'ema' }, run_id: 'r1' })
    expect(calls[0].headers['Content-Type']).toBe('application/json')
    expect(report.trial_id).toBe(7)
  })

  it('omits run_id when not given', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, rawReport()))
    await createLabClient({ fetchImpl }).backtest({ a: 1 })
    expect(calls[0].body).toEqual({ plan: { a: 1 } })
  })

  it('turns a 422 into an invalid LabError carrying every plan error', async () => {
    const errors = [
      { path: 'features.ema_fast', message: 'unknown feature "emma"' },
      { path: 'long_entry', message: 'unknown name x' },
    ]
    const { fetchImpl } = fakeFetch(() => jsonResponse(422, { errors }))
    const err = await createLabClient({ fetchImpl })
      .backtest({})
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LabError)
    const e = err as LabError
    expect(e.kind).toBe('invalid')
    expect(e.errors).toEqual(errors)
    expect(e.message).toBe('features.ema_fast: unknown feature "emma"')
  })

  it('turns a 503 with detail into no-data and keeps the server message', async () => {
    const detail = 'No data for BTCUSDT 4h. Run: uv run lab download BTCUSDT 4h'
    const { fetchImpl } = fakeFetch(() => jsonResponse(503, { detail }))
    const err = (await createLabClient({ fetchImpl })
      .backtest({})
      .catch((e: unknown) => e)) as LabError
    expect(err.kind).toBe('no-data')
    expect(err.message).toBe(detail)
  })

  it('reports offline when fetch itself fails', async () => {
    const fetchImpl: FetchLike = () => Promise.reject(new TypeError('Failed to fetch'))
    const err = (await createLabClient({ fetchImpl })
      .health()
      .catch((e: unknown) => e)) as LabError
    expect(err.kind).toBe('offline')
    expect(err.message).toBe(LAB_OFFLINE_MESSAGE)
  })

  it('reports offline when the proxy answers 502 or 504', async () => {
    for (const status of [502, 504]) {
      const { fetchImpl } = fakeFetch(
        () => new Response('', { status, headers: { 'Content-Type': 'text/plain' } }),
      )
      const err = (await createLabClient({ fetchImpl })
        .registry()
        .catch((e: unknown) => e)) as LabError
      expect(err.kind).toBe('offline')
    }
  })

  it('reports other statuses as http errors with the server detail', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(500, { detail: 'engine crashed' }))
    const err = (await createLabClient({ fetchImpl })
      .health()
      .catch((e: unknown) => e)) as LabError
    expect(err.kind).toBe('http')
    expect(err.status).toBe(500)
    expect(err.message).toBe('engine crashed')
  })

  it('reports a timeout when the lab does not answer in time', async () => {
    const fetchImpl: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
      })
    const err = (await createLabClient({ fetchImpl, timeoutMs: 5 })
      .health()
      .catch((e: unknown) => e)) as LabError
    expect(err.kind).toBe('timeout')
  })

  it('reports aborted when the caller cancels', async () => {
    const controller = new AbortController()
    const fetchImpl: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
      })
    const pending = createLabClient({ fetchImpl })
      .registry(controller.signal)
      .catch((e: unknown) => e)
    controller.abort()
    expect(((await pending) as LabError).kind).toBe('aborted')
  })

  it('validates a plan and returns its errors', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, { ok: false, errors: [{ path: 'risk', message: 'bad' }] }),
    )
    const result = await createLabClient({ fetchImpl }).validate({ x: 1 })
    expect(calls[0].url).toBe('/lab/validate')
    expect(calls[0].body).toEqual({ plan: { x: 1 } })
    expect(result).toEqual({ ok: false, errors: [{ path: 'risk', message: 'bad' }] })
  })

  it('treats a 422 from validate as an invalid result, not a failure', async () => {
    const { fetchImpl } = fakeFetch(() =>
      jsonResponse(422, { errors: [{ path: 'name', message: 'required' }] }),
    )
    expect(await createLabClient({ fetchImpl }).validate({})).toEqual({
      ok: false,
      errors: [{ path: 'name', message: 'required' }],
    })
  })

  it('reads the registry and skips malformed features', async () => {
    const { fetchImpl } = fakeFetch(() =>
      jsonResponse(200, {
        features: [
          {
            name: 'ema',
            description: 'Exponential MA',
            outputs: ['value'],
            params: [
              { name: 'period', kind: 'int', default: 20, low: 2, high: 400, description: 'bars' },
            ],
          },
          { description: 'no name' },
        ],
      }),
    )
    const reg = await createLabClient({ fetchImpl }).registry()
    expect(reg.features).toHaveLength(1)
    expect(reg.features[0].params[0]).toMatchObject({ name: 'period', default: 20 })
  })

  it('pages the ledger with run filter, limit and offset', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, { total: 51, items: [{ id: 9, created_at: 100 }, null] }),
    )
    const page = await createLabClient({ fetchImpl }).ledger({
      runId: ' ui ',
      limit: 25,
      offset: 50,
    })
    expect(calls[0].url).toBe('/lab/ledger?run_id=ui&limit=25&offset=50')
    expect(page.total).toBe(51)
    expect(page.items.map((i) => i.id)).toEqual([9])
  })

  it('reads one ledger entry with its plan and metrics', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, { id: 4, created_at: 1, plan: { name: 'p' }, metrics: METRICS }),
    )
    const d = await createLabClient({ fetchImpl }).ledgerItem(4)
    expect(calls[0].url).toBe('/lab/ledger/4')
    expect(d.plan).toEqual({ name: 'p' })
    expect(d.metrics?.sharpe).toBe(1.2)
  })

  it('honours a custom base path without a trailing slash', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, { ok: true, version: '0.1' }))
    const h = await createLabClient({ fetchImpl, basePath: 'http://x/lab/' }).health()
    expect(calls[0].url).toBe('http://x/lab/health')
    expect(h).toEqual({ ok: true, version: '0.1' })
  })
})

describe('regime', () => {
  const answer = {
    points: [
      { time: 100, state: null },
      { time: 200, state: 1 },
      { time: 300, state: -1 },
      { time: 400, state: 0 },
    ],
    window_bars: 20,
    threshold: 0.05,
    engine: 'hedgefund.regime.labels.label_regimes',
  }

  it('posts times, closes and parameters to /lab/regime and parses the labels', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, answer))
    const res = await createLabClient({ fetchImpl }).regime({
      times: [100, 200, 300, 400],
      closes: [1, 2, 3, 4],
      window_bars: 20,
      threshold: 0.05,
    })
    expect(calls[0]).toMatchObject({
      url: '/lab/regime',
      method: 'POST',
      body: { times: [100, 200, 300, 400], closes: [1, 2, 3, 4], window_bars: 20, threshold: 0.05 },
    })
    expect(res).toEqual(answer)
  })

  it('omits parameters that are not given so the lab defaults apply', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, answer))
    await createLabClient({ fetchImpl }).regime({ times: [1], closes: [1] })
    expect(calls[0].body).toEqual({ times: [1], closes: [1] })
  })

  it('reads unknown states as null, drops points without a time, and sorts by time', () => {
    const res = parseRegimeResponse({
      points: [
        { time: 300, state: 2 },
        { time: 100, state: -1 },
        { state: 1 },
        null,
        { time: 100, state: 1 },
        { time: 200, state: '1' },
      ],
    })
    expect(res.points).toEqual([
      { time: 100, state: -1 },
      { time: 200, state: null },
      { time: 300, state: null },
    ])
    expect(res).toMatchObject({ window_bars: 0, threshold: 0, engine: '' })
  })

  it('rejects an answer without a points list as a parse error', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { window_bars: 20 }))
    const err = await createLabClient({ fetchImpl })
      .regime({ times: [1], closes: [1] })
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LabError)
    expect((err as LabError).kind).toBe('parse')
  })

  it('turns a 422 into an invalid LabError with the field path', async () => {
    const { fetchImpl } = fakeFetch(() =>
      jsonResponse(422, {
        errors: [
          { path: 'times', message: 'times must be strictly ascending (unique UNIX seconds)' },
        ],
      }),
    )
    const err = await createLabClient({ fetchImpl })
      .regime({ times: [2, 1], closes: [1, 1] })
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LabError)
    expect((err as LabError).kind).toBe('invalid')
    expect((err as LabError).message).toBe(
      'times: times must be strictly ascending (unique UNIX seconds)',
    )
  })

  it('reports offline when the lab is not running', async () => {
    const { fetchImpl } = fakeFetch(() => new Response('Bad Gateway', { status: 502 }))
    const err = await createLabClient({ fetchImpl })
      .regime({ times: [1], closes: [1] })
      .catch((e: unknown) => e)
    expect((err as LabError).kind).toBe('offline')
    expect((err as LabError).message).toBe(LAB_OFFLINE_MESSAGE)
  })
})

describe('derivatives', () => {
  const H4 = 14_400
  const T0 = 1_704_067_200
  const answer = {
    symbol: 'BTCUSDT',
    bar_seconds: H4,
    funding: [
      { time: T0, value: 0.00037409 },
      { time: T0 + H4, value: null },
    ],
    open_interest: [
      { time: T0, value: 3_131_493_738.9 },
      { time: T0 + H4, value: null },
    ],
    available: {
      funding: { first: 1_577_836_800, last: 1_790_000_000 },
      open_interest: { first: null, last: null },
    },
    source: 'data.binance.vision futures/um archive (historical; no live stream)',
  }
  const req = { symbol: 'BTCUSDT', times: [T0, T0 + H4], bar_seconds: H4 }

  async function errorOf(body: unknown, status = 200): Promise<LabError> {
    const { fetchImpl } = fakeFetch(() => jsonResponse(status, body))
    const err = await createLabClient({ fetchImpl })
      .fetchDerivatives(req)
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LabError)
    return err as LabError
  }

  it('posts symbol, closed bar times and bar length to /lab/derivatives and parses the answer', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, answer))
    const res = await createLabClient({ fetchImpl }).fetchDerivatives(req)
    expect(calls[0]).toMatchObject({
      url: '/lab/derivatives',
      method: 'POST',
      body: { symbol: 'BTCUSDT', times: [T0, T0 + H4], bar_seconds: H4 },
    })
    expect(res).toEqual(answer)
  })

  it('keeps null values as null (whitespace), never filling them in', () => {
    const res = parseDerivativesResponse(answer)
    expect(res.funding[1]).toEqual({ time: T0 + H4, value: null })
    expect(res.open_interest[1].value).toBeNull()
    expect(res.available.open_interest).toEqual({ first: null, last: null })
  })

  it('defaults a missing source to an empty string', () => {
    const rest: Record<string, unknown> = { ...answer }
    delete rest.source
    expect(parseDerivativesResponse(rest).source).toBe('')
  })

  it.each([
    ['no funding list', { ...answer, funding: undefined }],
    ['a non-object point', { ...answer, funding: [null, answer.funding[1]] }],
    ['a string value', { ...answer, funding: [{ time: T0, value: '0.0003' }, answer.funding[1]] }],
    ['a point without a time', { ...answer, funding: [{ value: 0.1 }, answer.funding[1]] }],
    [
      'a fractional time',
      { ...answer, funding: [{ time: T0 + 0.5, value: 0.1 }, answer.funding[1]] },
    ],
    ['descending times', { ...answer, funding: [...answer.funding].reverse() }],
    ['lists of different lengths', { ...answer, open_interest: [answer.open_interest[0]] }],
    ['no availability', { ...answer, available: undefined }],
    [
      'a string availability bound',
      { ...answer, available: { ...answer.available, funding: { first: '1', last: null } } },
    ],
    ['no symbol', { ...answer, symbol: undefined }],
    ['no bar length', { ...answer, bar_seconds: null }],
  ])('rejects an answer with %s as a parse error', async (_name, body) => {
    expect((await errorOf(body)).kind).toBe('parse')
  })

  it('rejects an answer whose bars do not match the requested times', async () => {
    const shifted = {
      ...answer,
      funding: answer.funding.map((p) => ({ ...p, time: p.time + 60 })),
      open_interest: answer.open_interest.map((p) => ({ ...p, time: p.time + 60 })),
    }
    expect((await errorOf(shifted)).kind).toBe('parse')
    const short = {
      ...answer,
      funding: [answer.funding[0]],
      open_interest: [answer.open_interest[0]],
    }
    expect((await errorOf(short)).kind).toBe('parse')
  })

  it('turns a 503 with a detail into a no-data LabError carrying the CLI hint', async () => {
    const detail =
      'No derivatives data for BTCUSDT: run `uv run python -m lab.data.archive derivatives BTCUSDT` in lab/'
    const err = await errorOf({ detail }, 503)
    expect(err.kind).toBe('no-data')
    expect(err.status).toBe(503)
    expect(err.message).toBe(detail)
  })

  it('turns a 422 into an invalid LabError', async () => {
    const err = await errorOf(
      { errors: [{ path: 'bar_seconds', message: 'must be between 60 and 604800' }] },
      422,
    )
    expect(err.kind).toBe('invalid')
    expect(err.message).toBe('bar_seconds: must be between 60 and 604800')
  })

  it('reports offline when the lab is not running', async () => {
    const { fetchImpl } = fakeFetch(() => new Response('Bad Gateway', { status: 502 }))
    const err = await createLabClient({ fetchImpl })
      .fetchDerivatives(req)
      .catch((e: unknown) => e)
    expect((err as LabError).kind).toBe('offline')
  })
})
