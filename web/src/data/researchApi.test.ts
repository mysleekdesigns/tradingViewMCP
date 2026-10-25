import { describe, expect, it } from 'vitest'
import { LabError } from './labApi'
import type { FetchLike } from './persistence'
import {
  RESEARCH_OFFLINE_MESSAGE,
  createResearchClient,
  parseIdea,
  parsePaperComparison,
  parseRunItem,
  parseRunState,
} from './researchApi'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(respond: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const calls: { url: string; method: string }[] = []
  const fetchImpl: FetchLike = async (url, init) => {
    calls.push({ url, method: init?.method ?? 'GET' })
    return respond(url, init)
  }
  return { fetchImpl, calls }
}

const PLAN = {
  name: 'vol_breakout',
  symbol: 'BTCUSDT',
  interval: '4h',
  features: { atr: { atr: { period: 14 } } },
  long_entry: 'crosses_above(close, atr)',
}

function rawState(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: 'r-20261024-120000-abcdef',
    symbols: ['BTCUSDT', 7],
    interval: '4h',
    started_at: '2026-10-24T12:00:00Z',
    updated_at: '2026-10-24T12:05:00Z',
    phase: 'testing',
    outcome: 'running',
    budget: { hypotheses: 10, backtests: 200, usd: 5, bogus: 'x' },
    used: { hypotheses: 3, proposals: 4, backtests: 50, holdout_checks: 0, usd: 1.25 },
    current_idea_id: 12,
    candidates: [
      { idea_id: 12, plan_hash: 'abc123', plan: PLAN, trial_id: 99, summary: { sharpe: 1.3 } },
      { idea_id: 13, plan_hash: 'nope', plan: 'not-a-plan' },
    ],
    journal_path: 'agents/journal/2026-10-24-r-20261024.md',
    events: [
      { at: '2026-10-24T12:01:00Z', kind: 'phase', message: 'scouting' },
      { at: 1, kind: 'x', message: '   ' },
      'garbage',
    ],
    ...over,
  }
}

function rawRun(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: 'r-20261024-120000-abcdef',
    created_at: 1_793_000_000,
    label: 'BTC trend filters',
    config_sha256: 'deadbeef'.repeat(8),
    criteria: { dsr_min: 0.95, beat_buy_hold: true, junk: 'str', inf: null },
    budget: {
      max_hypotheses: 10,
      max_proposals: 30,
      max_backtests: 200,
      max_holdout_checks: 2,
      max_usd: 5,
    },
    usage: {
      budget: { proposals: 30, backtests: 200, holdout_checks: 2 },
      used: { proposals: 4, backtests: 50, holdout_checks: 0 },
      remaining: { proposals: 26, backtests: 150, holdout_checks: 2 },
    },
    state: rawState(),
    ...over,
  }
}

describe('parseRunState', () => {
  it('keeps valid fields and drops malformed candidates, events and budget entries', () => {
    const s = parseRunState(rawState())
    expect(s).not.toBeNull()
    expect(s?.symbols).toEqual(['BTCUSDT'])
    expect(s?.phase).toBe('testing')
    expect(s?.outcome).toBe('running')
    expect(s?.budget).toEqual({ hypotheses: 10, backtests: 200, usd: 5 })
    expect(s?.used.usd).toBe(1.25)
    expect(s?.candidates).toHaveLength(1)
    expect(s?.candidates[0].plan).toEqual(PLAN)
    expect(s?.candidates[0].summary).toEqual({ sharpe: 1.3 })
    expect(s?.events).toEqual([{ at: '2026-10-24T12:01:00Z', kind: 'phase', message: 'scouting' }])
  })

  it('maps an unknown outcome to "stopped" so it never keeps the page polling', () => {
    expect(parseRunState(rawState({ outcome: 'weird' }))?.outcome).toBe('stopped')
    expect(parseRunState(rawState({ phase: 'nap' }))?.phase).toBe('starting')
  })

  it('returns null for non-objects or a missing run id', () => {
    expect(parseRunState(null)).toBeNull()
    expect(parseRunState([])).toBeNull()
    expect(parseRunState({ phase: 'testing' })).toBeNull()
  })

  it('fills missing counters with zero', () => {
    const s = parseRunState({ run_id: 'r-1' })
    expect(s?.used).toEqual({
      hypotheses: 0,
      proposals: 0,
      backtests: 0,
      holdout_checks: 0,
      usd: 0,
    })
    expect(s?.journal_path).toBeNull()
    expect(s?.events).toEqual([])
  })
})

describe('parseRunItem', () => {
  it('parses a run with usage and state; criteria keep numbers and booleans only', () => {
    const r = parseRunItem(rawRun())
    expect(r?.label).toBe('BTC trend filters')
    expect(r?.criteria).toEqual({ dsr_min: 0.95, beat_buy_hold: true })
    expect(r?.budget.max_usd).toBe(5)
    expect(r?.usage?.used.backtests).toBe(50)
    expect(r?.state?.run_id).toBe('r-20261024-120000-abcdef')
  })

  it('accepts null usage and state (an interactive session without a progress file)', () => {
    const r = parseRunItem(rawRun({ usage: null, state: null, budget: null }))
    expect(r?.usage).toBeNull()
    expect(r?.state).toBeNull()
    expect(r?.budget.max_backtests).toBeNull()
  })

  it('rejects an item without run_id', () => {
    expect(parseRunItem({ label: 'x' })).toBeNull()
  })
})

describe('parseIdea', () => {
  it('parses sources and treats them as untrusted unless explicitly false', () => {
    const idea = parseIdea({
      id: 4,
      created_at: 1_793_000_000,
      run_id: 'r-1',
      family: 'trend',
      title: 'Vol compression breakout',
      hypothesis: 'h',
      rationale: 'r',
      status: 'promising',
      verdict_note: '',
      plan_hashes: ['a', 1, 'b'],
      sources: [
        { url: 'https://example.com/a', title: 'A' },
        { url: 'https://example.com/b', title: 'B', untrusted: false },
        { url: '', title: 'empty' },
        null,
      ],
    })
    expect(idea?.plan_hashes).toEqual(['a', 'b'])
    expect(idea?.sources).toEqual([
      { url: 'https://example.com/a', title: 'A', untrusted: true },
      { url: 'https://example.com/b', title: 'B', untrusted: false },
    ])
  })

  it('falls back on an unknown status and a missing title; needs an id', () => {
    const idea = parseIdea({ id: 9, status: 'zombie' })
    expect(idea?.status).toBe('new')
    expect(idea?.title).toBe('Idea #9')
    expect(parseIdea({ title: 'no id' })).toBeNull()
  })
})

describe('parsePaperComparison', () => {
  it('parses stats with nulls and an unknown verdict as insufficient_data', () => {
    const p = parsePaperComparison({
      candidate: 'vol_breakout',
      plan_hash: 'abc123',
      symbol: 'BTCUSDT',
      interval: '4h',
      period_start: 1_793_000_000,
      period_end: 1_793_600_000,
      generated_at: 1_793_600_100,
      forward: { n_trades: 3, total_return: null, win_rate: 0.66, mean_trade_return: 'x' },
      backtest: { n_trades: 4, total_return: 0.02, win_rate: 0.5, mean_trade_return: 0.005 },
      entry_match_rate: 0.75,
      mean_entry_slippage_bps: 3.2,
      verdict: 'great',
      notes: ['one', 2],
    })
    expect(p?.forward).toEqual({
      n_trades: 3,
      total_return: null,
      win_rate: 0.66,
      mean_trade_return: null,
    })
    expect(p?.verdict).toBe('insufficient_data')
    expect(p?.notes).toEqual(['one'])
  })

  it('rejects an entry with neither candidate nor plan hash', () => {
    expect(parsePaperComparison({ symbol: 'BTCUSDT' })).toBeNull()
  })

  it('keeps generated_at as the ISO string compare.py writes', () => {
    const iso = '2026-10-24T12:00:00.123456+00:00'
    expect(parsePaperComparison({ plan_hash: 'h', generated_at: iso })?.generated_at).toBe(iso)
  })

  it('converts a numeric generated_at to ISO and drops junk', () => {
    expect(
      parsePaperComparison({ plan_hash: 'h', generated_at: 1_793_600_100 })?.generated_at,
    ).toBe(new Date(1_793_600_100_000).toISOString())
    for (const bad of ['not a date', '', 1e300, Number.NaN, null, {}]) {
      expect(parsePaperComparison({ plan_hash: 'h', generated_at: bad })?.generated_at).toBeNull()
    }
  })
})

describe('createResearchClient', () => {
  it('GETs /lab/research/runs and sorts newest first', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, {
        runs: [
          rawRun({ run_id: 'r-old', created_at: 100 }),
          rawRun({ run_id: 'r-new', created_at: 200 }),
          { nope: true },
        ],
      }),
    )
    const runs = await createResearchClient({ fetchImpl }).runs()
    expect(calls).toEqual([{ url: '/lab/research/runs', method: 'GET' }])
    expect(runs.map((r) => r.run_id)).toEqual(['r-new', 'r-old'])
  })

  it('GETs one run by id and rejects a malformed id before fetching', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, rawRun()))
    const client = createResearchClient({ fetchImpl })
    const run = await client.run('r-20261024-120000-abcdef')
    expect(run.run_id).toBe('r-20261024-120000-abcdef')
    expect(calls[0].url).toBe('/lab/research/runs/r-20261024-120000-abcdef')
    await expect(client.run('../etc')).rejects.toMatchObject({ kind: 'http' })
    expect(calls).toHaveLength(1)
  })

  it('a run answer that is not a run is a parse error', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { hello: 1 }))
    await expect(createResearchClient({ fetchImpl }).run('r-1')).rejects.toMatchObject({
      kind: 'parse',
    })
  })

  it('passes run_id and status filters to /ideas', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, { ideas: [{ id: 1 }] }))
    const client = createResearchClient({ fetchImpl })
    await client.ideas({ runId: ' r-1 ', status: 'dead' })
    await client.ideas()
    expect(calls.map((c) => c.url)).toEqual([
      '/lab/research/ideas?run_id=r-1&status=dead',
      '/lab/research/ideas',
    ])
  })

  it('lists journal entries and reads one by name', async () => {
    const { fetchImpl, calls } = fakeFetch((url) =>
      url.endsWith('/journal')
        ? jsonResponse(200, {
            entries: [
              {
                name: 'a.md',
                title: 'A',
                run_id: 'r-1',
                date: '2026-10-24',
                outcome: 'no_edge',
                idea_ids: [1, 'x'],
                size: 10,
              },
              { title: 'nameless' },
            ],
          })
        : jsonResponse(200, { name: 'a.md', markdown: '# Hi' }),
    )
    const client = createResearchClient({ fetchImpl })
    const entries = await client.journal()
    expect(entries).toHaveLength(1)
    expect(entries[0].idea_ids).toEqual([1])
    const doc = await client.journalEntry('a.md')
    expect(doc).toEqual({ name: 'a.md', markdown: '# Hi' })
    expect(calls[1].url).toBe('/lab/research/journal/a.md')
  })

  it('refuses path-like journal names without fetching', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, {}))
    const client = createResearchClient({ fetchImpl })
    await expect(client.journalEntry('../secrets')).rejects.toBeInstanceOf(LabError)
    await expect(client.journalEntry('a/b.md')).rejects.toBeInstanceOf(LabError)
    expect(calls).toHaveLength(0)
  })

  it('a journal entry without markdown text is a parse error', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { name: 'a.md', markdown: 3 }))
    await expect(createResearchClient({ fetchImpl }).journalEntry('a.md')).rejects.toMatchObject({
      kind: 'parse',
    })
  })

  it('parses candidates with their run id and paper comparisons', async () => {
    const { fetchImpl } = fakeFetch((url) =>
      url.endsWith('/candidates')
        ? jsonResponse(200, {
            candidates: [
              {
                run_id: 'r-1',
                idea_id: 1,
                plan_hash: 'abc',
                plan: PLAN,
                trial_id: 3,
                summary: 'ok',
              },
              { run_id: 'r-1', plan_hash: 'bad', plan: [] },
            ],
          })
        : jsonResponse(200, {
            comparisons: [{ candidate: 'c', plan_hash: 'abc', verdict: 'consistent' }],
          }),
    )
    const client = createResearchClient({ fetchImpl })
    const cands = await client.candidates()
    expect(cands).toHaveLength(1)
    expect(cands[0]).toMatchObject({ run_id: 'r-1', plan_hash: 'abc', summary: 'ok' })
    const paper = await client.paper()
    expect(paper[0].verdict).toBe('consistent')
  })

  it('a list endpoint without its array is a parse error', async () => {
    for (const body of [{}, { runs: 'x' }, [], 'str']) {
      const { fetchImpl } = fakeFetch(() => jsonResponse(200, body))
      await expect(createResearchClient({ fetchImpl }).runs()).rejects.toMatchObject({
        kind: 'parse',
      })
    }
  })

  it('a non-JSON 200 answer is a parse error', async () => {
    const { fetchImpl } = fakeFetch(() => new Response('<html>', { status: 200 }))
    await expect(createResearchClient({ fetchImpl }).candidates()).rejects.toMatchObject({
      kind: 'parse',
    })
  })

  it('a failed fetch is "offline" with the lab start command', async () => {
    const fetchImpl: FetchLike = () => Promise.reject(new TypeError('Failed to fetch'))
    const err = await createResearchClient({ fetchImpl })
      .runs()
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LabError)
    expect((err as LabError).kind).toBe('offline')
    expect((err as LabError).message).toBe(RESEARCH_OFFLINE_MESSAGE)
    expect(RESEARCH_OFFLINE_MESSAGE).toContain('uv run python -m lab.api')
  })

  it('a proxy 502/504 is "offline"; 404 is http with the status; 503 detail is no-data', async () => {
    for (const status of [502, 504]) {
      const { fetchImpl } = fakeFetch(() => new Response('Bad gateway', { status }))
      await expect(createResearchClient({ fetchImpl }).ideas()).rejects.toMatchObject({
        kind: 'offline',
        status,
      })
    }
    const nf = fakeFetch(() => jsonResponse(404, { detail: 'Not Found' }))
    await expect(createResearchClient({ fetchImpl: nf.fetchImpl }).runs()).rejects.toMatchObject({
      kind: 'http',
      status: 404,
    })
    const nd = fakeFetch(() => jsonResponse(503, { detail: 'success criteria missing' }))
    await expect(createResearchClient({ fetchImpl: nd.fetchImpl }).runs()).rejects.toMatchObject({
      kind: 'no-data',
      message: 'success criteria missing',
    })
  })

  it('an already-aborted signal rejects as aborted without fetching', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, { runs: [] }))
    const ctrl = new AbortController()
    ctrl.abort()
    await expect(createResearchClient({ fetchImpl }).runs(ctrl.signal)).rejects.toMatchObject({
      kind: 'aborted',
    })
    expect(calls).toHaveLength(0)
  })

  it('aborting mid-request rejects as aborted', async () => {
    const fetchImpl: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('Aborted', 'AbortError')),
        )
      })
    const ctrl = new AbortController()
    const p = createResearchClient({ fetchImpl }).runs(ctrl.signal)
    ctrl.abort()
    await expect(p).rejects.toMatchObject({ kind: 'aborted' })
  })

  it('times out with kind "timeout"', async () => {
    const fetchImpl: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('Aborted', 'AbortError')),
        )
      })
    await expect(createResearchClient({ fetchImpl, timeoutMs: 5 }).paper()).rejects.toMatchObject({
      kind: 'timeout',
    })
  })

  it('uses a custom base path without a trailing slash', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, { entries: [] }))
    await createResearchClient({ fetchImpl, basePath: '/x/research/' }).journal()
    expect(calls[0].url).toBe('/x/research/journal')
  })
})
