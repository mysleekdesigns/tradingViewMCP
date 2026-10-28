// Automatic "load history, then zoom to the trades" after a backtest: when it starts, when it
// zooms, that it runs once per report and market, and the zoom range itself.
import { describe, expect, it } from 'vitest'
import { TRADES_ZOOM_MAX_BARS, tradesZoomRange } from '../chart/backtestOverlay'
import {
  autoHistoryKey,
  autoHistoryStep,
  historyAnnouncement,
  historyLoadingText,
  historyPlan,
  initialAutoHistory,
  MAX_HISTORY_BARS,
  unreachableText,
  type AutoHistoryInput,
  type AutoHistoryState,
  type HistoryPlan,
} from './backtestView'

const H4 = 4 * 3600
const DAY = 86_400
const WIN = {
  kind: 'design' as const,
  start: Date.UTC(2019, 0, 1) / 1000,
  end: Date.UTC(2025, 9, 1) / 1000,
}
/** The chart's latest ~500 4h bars: first bar ~83 days before "now" (2026-10-24). */
const NOW = Date.UTC(2026, 9, 24) / 1000
const FIRST_RECENT = NOW - 500 * H4

const report = { run_id: 'r1', trial_id: 7, symbol: 'BTCUSDT', interval: '4h' }
const KEY = autoHistoryKey(report)

const needed: HistoryPlan = historyPlan(FIRST_RECENT, WIN, '4h', MAX_HISTORY_BARS - 500)
const reached: HistoryPlan = historyPlan(WIN.start - H4, WIN, '4h', MAX_HISTORY_BARS - 15_000)

function input(over: Partial<AutoHistoryInput> = {}): AutoHistoryInput {
  return {
    key: KEY,
    plan: needed,
    loading: false,
    failed: false,
    exhausted: false,
    reachedTarget: false,
    ...over,
  }
}

/** Feed inputs in order; return each command. */
function run(steps: AutoHistoryInput[], start: AutoHistoryState = initialAutoHistory) {
  let state = start
  const commands: string[] = []
  for (const s of steps) {
    const out = autoHistoryStep(state, s)
    state = out.state
    commands.push(out.command)
  }
  return { state, commands }
}

describe('historyPlan for the owner case (BTCUSDT 4h, 2019-01-01 → 2025-10-01)', () => {
  it('needs a reachable load back to the window start', () => {
    expect(needed).toMatchObject({ needed: true, unreachable: false, partial: false })
    expect(needed.target).toBe(WIN.start)
  })
})

describe('autoHistoryStep', () => {
  it('starts the history load when a drawn report needs older bars', () => {
    expect(run([input()]).commands).toEqual(['start'])
  })

  it('zooms once the load ran and reached the window start', () => {
    const { commands, state } = run([
      input(), // report drawn → start
      input(), // same render batch, load not seen yet → wait
      input({ loading: true }),
      input({ loading: true }),
      input({ plan: reached, loading: false }), // load ended at the window start
    ])
    expect(commands).toEqual(['start', 'none', 'none', 'none', 'zoom'])
    expect(state).toEqual({ key: KEY, phase: 'done' })
  })

  it('zooms when the load stops at the bar limit (partial window, nothing more to load)', () => {
    const atLimit: HistoryPlan = {
      needed: true,
      target: 0,
      bars: 0,
      partial: true,
      unreachable: false,
    }
    expect(run([input(), input({ loading: true }), input({ plan: atLimit })]).commands).toEqual([
      'start',
      'none',
      'zoom',
    ])
  })

  it('zooms when the exchange has no older bars', () => {
    expect(
      run([input(), input({ loading: true }), input({ exhausted: true })]).commands.at(-1),
    ).toBe('zoom')
  })

  it('does not zoom after the user stops the load, and never restarts it', () => {
    const { commands } = run([
      input(),
      input({ loading: true }),
      input({ loading: false }), // stopped: plan still needs bars
      input(),
      input(),
    ])
    expect(commands).toEqual(['start', 'none', 'none', 'none', 'none'])
  })

  it('does not zoom after a failed load', () => {
    const { commands } = run([input(), input({ loading: true }), input({ failed: true })])
    expect(commands).toEqual(['start', 'none', 'none'])
  })

  it('zooms at once for a new report when the chart is already at the bar cap', () => {
    // BTCUSDT 1h: an earlier run filled the chart to 20,000 bars; a second run on the market.
    const firstBar = NOW - MAX_HISTORY_BARS * 3600
    const atCap = historyPlan(firstBar, WIN, '1h', 0)
    expect(atCap).toMatchObject({ needed: true, partial: true, bars: 0, unreachable: false })
    expect(atCap.target).toBe(firstBar) // a load to this target could never run
    expect(
      run([input({ key: autoHistoryKey({ ...report, run_id: 'r2' }), plan: atCap })]).commands,
    ).toEqual(['zoom'])
  })

  it('zooms when a capped load reached its target short of the cap (exchange gaps)', () => {
    // 19,600 raw bars after the load: the recomputed plan still asks for 400 more.
    const gappy = historyPlan(NOW - 19_800 * 3600, WIN, '1h', MAX_HISTORY_BARS - 19_600)
    expect(gappy).toMatchObject({ needed: true, partial: true, bars: 400 })
    const ended = run([
      input({ plan: gappy }),
      input({ plan: gappy, loading: true }),
      input({ plan: gappy, loading: false, reachedTarget: true }),
    ])
    expect(ended.commands).toEqual(['start', 'none', 'zoom'])
    // A new report on that market zooms straight away instead of starting a load.
    const next = autoHistoryKey({ ...report, run_id: 'r3' })
    expect(
      autoHistoryStep(ended.state, input({ key: next, plan: gappy, reachedTarget: true })).command,
    ).toBe('zoom')
  })

  it('ignores a reached target when the whole window fits (the plan is exact)', () => {
    expect(run([input({ reachedTarget: true })]).commands).toEqual(['start'])
  })

  it('zooms at once when the chart already holds the window', () => {
    expect(run([input({ plan: reached })]).commands).toEqual(['zoom'])
  })

  it('does nothing for an unreachable window (e.g. 1m)', () => {
    const unreachable = historyPlan(NOW - 500 * 60, WIN, '1m', MAX_HISTORY_BARS - 500)
    expect(unreachable.unreachable).toBe(true)
    expect(run([input({ plan: unreachable }), input({ plan: unreachable })]).commands).toEqual([
      'none',
      'none',
    ])
  })

  it('waits while nothing is drawn or there are no bars yet', () => {
    expect(run([input({ key: null }), input({ plan: null })]).commands).toEqual(['none', 'none'])
    expect(run([input({ plan: null }), input()]).commands).toEqual(['none', 'start'])
  })

  it('runs once per report and market: leaving and coming back does not restart', () => {
    const { commands } = run([
      input(),
      input({ loading: true }),
      input({ plan: reached }), // zoom
      input({ key: null }), // switched market
      input({ plan: reached }), // back on the same market
    ])
    expect(commands).toEqual(['start', 'none', 'zoom', 'none', 'none'])
  })

  it('a new report (or the same report on another market) starts again', () => {
    const other = autoHistoryKey({ ...report, run_id: 'r2' })
    const otherMarket = autoHistoryKey({ ...report, interval: '1h' })
    expect(new Set([KEY, other, otherMarket]).size).toBe(3)
    const { commands } = run([
      input(),
      input({ loading: true }),
      input({ loading: false }), // stopped
      input({ key: other }),
    ])
    expect(commands).toEqual(['start', 'none', 'none', 'start'])
  })
})

describe('auto-load copy', () => {
  it('names the window and the trades on chart while loading', () => {
    expect(historyLoadingText(WIN, 12, 137)).toBe(
      'Trades are in 2019-01-01 → 2025-10-01 · loading history… 12 of 137 trades on chart',
    )
  })

  it('explains an unreachable window and points to higher intervals', () => {
    const text = unreachableText(WIN.end)
    expect(text).toContain('2025-10-01')
    // States the real limits: a per-load budget and the chart cap, not a 20k-bar chart.
    expect(text).toContain('20,000 older bars per load')
    expect(text).toContain('150,000 bars on the chart')
    expect(text).toContain('Use 1h, 4h or 1d')
  })
})

describe('tradesZoomRange', () => {
  const trade = (entry: number, exit: number | null) => ({ entry_time: entry, exit_time: exit })

  it('fits every trade with 5 bars of padding when they span few bars', () => {
    const t0 = WIN.end - 100 * H4
    const r = tradesZoomRange([trade(t0, t0 + 10 * H4), trade(t0 + 50 * H4, t0 + 60 * H4)], WIN, H4)
    expect(r).toEqual({ from: t0 - 5 * H4, to: t0 + 65 * H4 })
  })

  it('shows the most recent trades: the last 300 bars up to the last exit', () => {
    const trades = [
      trade(WIN.start + DAY, WIN.start + 2 * DAY),
      trade(WIN.end - 30 * DAY, WIN.end - 20 * DAY),
    ]
    const r = tradesZoomRange(trades, WIN, H4)
    expect(r.to).toBe(WIN.end - 20 * DAY + 5 * H4)
    expect(r.to - r.from).toBe(TRADES_ZOOM_MAX_BARS * H4)
  })

  it('runs an open trade to the window end', () => {
    const r = tradesZoomRange([trade(WIN.end - 10 * H4, null)], WIN, H4)
    expect(r).toEqual({ from: WIN.end - 15 * H4, to: WIN.end + 5 * H4 })
  })

  it('shows the last bars of the window when there are no trades', () => {
    expect(tradesZoomRange([], WIN, H4)).toEqual({
      from: WIN.end - TRADES_ZOOM_MAX_BARS * H4,
      to: WIN.end,
    })
  })

  it('handles trades in any order', () => {
    const a = trade(WIN.end - 40 * H4, WIN.end - 30 * H4)
    const b = trade(WIN.end - 20 * H4, WIN.end - 10 * H4)
    expect(tradesZoomRange([b, a], WIN, H4)).toEqual(tradesZoomRange([a, b], WIN, H4))
  })
})

describe('historyAnnouncement (one polite live region)', () => {
  const idle = { loading: false, failed: false }
  const loading = { loading: true, failed: false }
  const failed = { loading: false, failed: true }

  it('announces the start once', () => {
    expect(historyAnnouncement(idle, loading, 0, 137)).toBe(
      'Loading chart history for the backtest…',
    )
  })

  it('announces nothing while pages arrive (same flags)', () => {
    expect(historyAnnouncement(loading, loading, 40, 137)).toBeNull()
  })

  it('announces the end with the trades on chart', () => {
    expect(historyAnnouncement(loading, idle, 137, 137)).toBe(
      'History loading ended: 137 of 137 trades on chart.',
    )
  })

  it('announces a failure', () => {
    expect(historyAnnouncement(loading, failed, 12, 137)).toBe('Loading chart history failed.')
    expect(historyAnnouncement(failed, idle, 12, 137)).toBeNull()
  })
})
