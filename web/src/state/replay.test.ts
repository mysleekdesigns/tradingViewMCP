import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import {
  REPLAY_OFF,
  REPLAY_SPEEDS,
  cancelPicking,
  closedBars,
  isRepicking,
  exit,
  lastIndexAtOrBefore,
  normalizeSpeed,
  pause,
  pickAt,
  play,
  reconcile,
  replayAtEnd,
  replayBarsAhead,
  replayCurrentBar,
  replayIntervalMs,
  replayKeyAction,
  replayVisible,
  sameBars,
  setSpeed,
  startAt,
  startBarsBack,
  startPicking,
  stepBack,
  stepForward,
  tick,
  togglePlay,
  type ReplayKey,
  type ReplayState,
} from './replay'

const H = 3600
const T0 = 1_700_000_000 - (1_700_000_000 % H)

/** n hourly bars starting at T0 + offset hours; close = index + offset so bars are traceable. */
function bars(n: number, offset = 0): Candle[] {
  return Array.from({ length: n }, (_, k) => {
    const i = k + offset
    return { time: T0 + i * H, open: i, high: i + 1, low: i - 1, close: i, volume: 1 }
  })
}

/** 10 bars; the last (T0 + 9h) is forming, so 9 closed bars T0 .. T0 + 8h. */
const SRC = bars(10)
const t = (i: number) => T0 + i * H

function active(cursorIndex: number, extra: Partial<ReplayState> = {}): ReplayState {
  return { ...REPLAY_OFF, status: 'active', cursorTime: t(cursorIndex), ...extra }
}

describe('closed bars', () => {
  it('drops the forming last row by default', () => {
    expect(closedBars(SRC).map((c) => c.close)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8])
  })

  it('keeps the last row when the caller says it is closed', () => {
    expect(closedBars(SRC, { lastBarClosed: true })).toHaveLength(10)
  })

  it('finds the last bar at or before a time', () => {
    expect(lastIndexAtOrBefore(SRC, t(3))).toBe(3)
    expect(lastIndexAtOrBefore(SRC, t(3) + 1800)).toBe(3)
    expect(lastIndexAtOrBefore(SRC, t(0) - 1)).toBe(-1)
    expect(lastIndexAtOrBefore(SRC, t(99))).toBe(9)
    expect(lastIndexAtOrBefore([], t(0))).toBe(-1)
  })
})

describe('starting replay', () => {
  it('is off by default and shows the live view', () => {
    expect(replayVisible(SRC, REPLAY_OFF)).toBeNull()
    expect(replayCurrentBar(SRC, REPLAY_OFF)).toBeNull()
    expect(replayAtEnd(SRC, REPLAY_OFF)).toBe(false)
  })

  it('snaps a picked time to the last bar at or before it, paused', () => {
    const s = startAt(SRC, REPLAY_OFF, t(4) + 1234)
    expect(s).toMatchObject({ status: 'active', cursorTime: t(4), playing: false })
    expect(replayVisible(SRC, s)?.map((c) => c.close)).toEqual([0, 1, 2, 3, 4])
    expect(replayCurrentBar(SRC, s)?.close).toBe(4)
  })

  it('never starts before the first bar (at least one bar visible)', () => {
    const s = startAt(SRC, REPLAY_OFF, t(-50))
    expect(s.cursorTime).toBe(t(0))
    expect(replayVisible(SRC, s)).toHaveLength(1)
  })

  it('never starts on the forming bar: a later time lands on the latest closed bar', () => {
    const s = startAt(SRC, REPLAY_OFF, t(9) + 60)
    expect(s.cursorTime).toBe(t(8))
    expect(replayAtEnd(SRC, s)).toBe(true)
  })

  it('stays off when there is nothing closed to replay', () => {
    expect(startAt(bars(1), REPLAY_OFF, t(0)).status).toBe('off')
    expect(startAt([], REPLAY_OFF, t(0)).status).toBe('off')
    expect(startBarsBack(bars(1), REPLAY_OFF, 5).status).toBe('off')
    expect(startAt(SRC, REPLAY_OFF, Number.NaN).status).toBe('off')
  })

  it('starts N bars back from the latest closed bar, clamped to the first', () => {
    expect(startBarsBack(SRC, REPLAY_OFF, 0).cursorTime).toBe(t(8))
    expect(startBarsBack(SRC, REPLAY_OFF, 3).cursorTime).toBe(t(5))
    expect(startBarsBack(SRC, REPLAY_OFF, 500).cursorTime).toBe(t(0))
    expect(startBarsBack(SRC, REPLAY_OFF, -2).cursorTime).toBe(t(8))
  })

  it('pick mode keeps the live view until a click, then starts there', () => {
    const picking = startPicking(REPLAY_OFF)
    expect(picking.status).toBe('picking')
    expect(replayVisible(SRC, picking)).toBeNull()
    expect(pickAt(SRC, picking, t(2)).cursorTime).toBe(t(2))
    // A click outside pick mode does nothing.
    expect(pickAt(SRC, REPLAY_OFF, t(2))).toBe(REPLAY_OFF)
  })

  it('a re-pick from a running replay keeps the replay view: nothing past the cursor shows', () => {
    const picking = startPicking(active(3))
    expect(picking).toMatchObject({ status: 'picking', cursorTime: t(3) })
    expect(isRepicking(picking)).toBe(true)
    expect(replayVisible(SRC, picking)?.map((c) => c.close)).toEqual([0, 1, 2, 3])
    // A fresh pick (from off) is not a re-pick and shows the live view.
    expect(isRepicking(startPicking(REPLAY_OFF))).toBe(false)
  })

  it('a re-pick beyond the cursor is capped at the cursor; earlier picks restart there', () => {
    const picking = startPicking(active(3))
    expect(pickAt(SRC, picking, t(7)).cursorTime).toBe(t(3))
    expect(pickAt(SRC, picking, t(99)).cursorTime).toBe(t(3))
    expect(pickAt(SRC, picking, t(1))).toMatchObject({ status: 'active', cursorTime: t(1) })
    // A fresh pick can land anywhere.
    expect(pickAt(SRC, startPicking(REPLAY_OFF), t(7)).cursorTime).toBe(t(7))
  })

  it('cancelling a re-pick resumes the replay that was running; otherwise turns off', () => {
    const running = active(3, { playing: true })
    const picking = startPicking(running)
    expect(picking.playing).toBe(false)
    expect(cancelPicking(picking)).toMatchObject({ status: 'active', cursorTime: t(3) })
    expect(cancelPicking(startPicking(REPLAY_OFF)).status).toBe('off')
    expect(cancelPicking(running)).toBe(running)
  })
})

describe('stepping', () => {
  it('steps forward and back one bar', () => {
    const s = stepForward(SRC, active(3))
    expect(s.cursorTime).toBe(t(4))
    expect(stepBack(SRC, s).cursorTime).toBe(t(3))
  })

  it('jumps forward N, clamped to the latest closed bar', () => {
    expect(stepForward(SRC, active(1), {}, 4).cursorTime).toBe(t(5))
    expect(stepForward(SRC, active(1), {}, 100).cursorTime).toBe(t(8))
  })

  it('never steps back before the first bar', () => {
    expect(stepBack(SRC, active(1), {}, 10).cursorTime).toBe(t(0))
    const first = active(0)
    expect(stepBack(SRC, first)).toBe(first)
  })

  it('does not move past the latest closed bar and returns the same state there', () => {
    const end = active(8)
    expect(stepForward(SRC, end)).toBe(end)
    expect(replayBarsAhead(SRC, end)).toBe(0)
    expect(replayBarsAhead(SRC, active(5))).toBe(3)
  })

  it('stepping back pauses playback', () => {
    expect(stepBack(SRC, active(5, { playing: true })).playing).toBe(false)
  })

  it('is a no-op unless active', () => {
    expect(stepForward(SRC, REPLAY_OFF)).toBe(REPLAY_OFF)
    const picking = startPicking(active(3))
    expect(stepBack(SRC, picking)).toBe(picking)
  })

  it('bumps the revision on every move and never decreases it', () => {
    let s: ReplayState = REPLAY_OFF
    const seen: number[] = [s.revision]
    s = startAt(SRC, s, t(2))
    seen.push(s.revision)
    s = stepForward(SRC, s)
    seen.push(s.revision)
    s = stepBack(SRC, s)
    seen.push(s.revision)
    s = exit(s)
    seen.push(s.revision)
    expect(seen).toEqual([0, 1, 2, 3, 4])
    // Play/pause/speed do not change what is shown.
    const a = active(2)
    expect(play(SRC, a).revision).toBe(a.revision)
    expect(setSpeed(a, 5).revision).toBe(a.revision)
  })
})

describe('playback', () => {
  it('plays tick by tick and stops automatically at the latest closed bar', () => {
    let s = play(SRC, active(5))
    expect(s.playing).toBe(true)
    s = tick(SRC, s)
    expect(s).toMatchObject({ cursorTime: t(6), playing: true })
    s = tick(SRC, s)
    expect(s).toMatchObject({ cursorTime: t(7), playing: true })
    s = tick(SRC, s)
    expect(s).toMatchObject({ cursorTime: t(8), playing: false })
    expect(tick(SRC, s)).toBe(s)
    // The forming bar is never reached.
    expect(replayVisible(SRC, s)?.at(-1)?.time).toBe(t(8))
  })

  it('refuses to play at the end, and ticks do nothing while paused', () => {
    const end = active(8)
    expect(play(SRC, end)).toBe(end)
    const paused = active(3)
    expect(tick(SRC, paused)).toBe(paused)
  })

  it('toggles and pauses', () => {
    const playing = togglePlay(SRC, active(2))
    expect(playing.playing).toBe(true)
    expect(togglePlay(SRC, playing).playing).toBe(false)
    expect(pause(active(2))).toEqual(active(2))
  })

  it('can play on after new live bars close at the end', () => {
    const s = active(8)
    expect(replayAtEnd(SRC, s)).toBe(true)
    const grown = bars(12) // T0 + 9h and 10h closed, 11h forming
    expect(replayAtEnd(grown, s)).toBe(false)
    const p = play(grown, s)
    expect(p.playing).toBe(true)
    expect(tick(grown, tick(grown, p))).toMatchObject({ cursorTime: t(10), playing: false })
  })

  it('snaps speeds to the allowed list', () => {
    expect(REPLAY_SPEEDS).toEqual([1, 2, 5, 10])
    expect(normalizeSpeed(4)).toBe(5)
    expect(normalizeSpeed(100)).toBe(10)
    expect(normalizeSpeed(0)).toBe(1)
    expect(normalizeSpeed(Number.NaN)).toBe(1)
    expect(replayIntervalMs(2)).toBe(500)
    expect(replayIntervalMs(10)).toBe(100)
  })

  it('keeps the speed across exit', () => {
    const s = exit(setSpeed(active(3), 5))
    expect(s).toMatchObject({ status: 'off', cursorTime: null, playing: false, speed: 5 })
    expect(replayVisible(SRC, s)).toBeNull()
    expect(exit(REPLAY_OFF)).toBe(REPLAY_OFF)
  })
})

describe('source changes mid-replay', () => {
  it('prepending older history keeps the cursor on the same bar', () => {
    const s = active(4)
    const before = replayVisible(SRC, s)
    const prepended = [...bars(5, -5), ...SRC] // T0 - 5h .. T0 + 9h
    const after = replayVisible(prepended, s)
    expect(replayCurrentBar(prepended, s)?.time).toBe(t(4))
    expect(after?.slice(5)).toEqual(before)
    expect(after?.[0].time).toBe(t(-5))
    expect(stepForward(prepended, s).cursorTime).toBe(t(5))
    expect(stepBack(prepended, active(0), {}, 3).cursorTime).toBe(t(-3))
  })

  it('appending live bars does not reveal them', () => {
    const s = active(4)
    const appended = bars(20)
    expect(replayVisible(appended, s)?.map((c) => c.close)).toEqual([0, 1, 2, 3, 4])
    expect(replayBarsAhead(appended, s)).toBe(14)
  })

  it('a forming-bar tick does not change what is visible', () => {
    const s = active(8)
    const ticked = [...SRC.slice(0, -1), { ...SRC[9], close: 999, high: 1000 }]
    expect(replayVisible(ticked, s)).toEqual(replayVisible(SRC, s))
    expect(replayVisible(ticked, s)?.some((c) => c.close === 999)).toBe(false)
  })

  it('reconcile ends the replay when a disjoint reload no longer reaches back to the cursor', () => {
    // The new window starts at bar 50: snapping the cursor from bar 3 to bar 50 would skip 46
    // bars (and every open paper limit they would have touched), so replay exits instead.
    const later = bars(10, 50)
    const r = reconcile(later, active(3))
    expect(r).toMatchObject({ status: 'off', cursorTime: null, playing: false })
    expect(r.revision).toBe(1)
    expect(replayVisible(later, r)).toBeNull()
    expect(reconcile(later, r)).toBe(r)
    // A re-pick on the replaced source ends as well.
    expect(reconcile(later, startPicking(active(3))).status).toBe('off')
    expect(reconcile(bars(1), active(3)).status).toBe('off')
    // A reload that still reaches back to the cursor (or only prepends/appends) keeps it.
    const fine = active(3)
    expect(reconcile(bars(30, -10), fine)).toBe(fine)
    expect(reconcile(bars(10, 3), fine)).toBe(fine)
  })
})

describe('look-ahead', () => {
  it('visible never contains a bar after the cursor or the forming bar', () => {
    const src = bars(40)
    const forming = src[src.length - 1]
    let s = startAt(src, REPLAY_OFF, t(0))
    for (let k = 0; k < 60; k++) {
      const vis = replayVisible(src, s) ?? []
      expect(vis.length).toBeGreaterThan(0)
      expect(vis.every((c) => s.cursorTime !== null && c.time <= s.cursorTime)).toBe(true)
      expect(vis.at(-1)?.time).toBe(s.cursorTime)
      expect(vis.includes(forming)).toBe(false)
      s = k % 7 === 6 ? stepBack(src, s, {}, 2) : stepForward(src, s)
    }
  })

  it('values computed on visible bars do not change when future bars change', () => {
    const s = active(5)
    const sumCloses = (cs: readonly Candle[] | null) => (cs ?? []).reduce((a, c) => a + c.close, 0)
    const perturbed = SRC.map((c, i) => (i > 5 ? { ...c, close: c.close * 100 + 7 } : c))
    expect(sumCloses(replayVisible(perturbed, s))).toBe(sumCloses(replayVisible(SRC, s)))
  })
})

describe('sameBars', () => {
  it('compares element identity', () => {
    const a = SRC.slice(0, 3)
    expect(sameBars(a, SRC.slice(0, 3))).toBe(true)
    expect(sameBars(a, [...SRC.slice(0, 2), { ...SRC[2] }])).toBe(false)
    expect(sameBars(a, SRC.slice(0, 4))).toBe(false)
    expect(sameBars(null, null)).toBe(true)
    expect(sameBars(a, null)).toBe(false)
  })
})

describe('replayKeyAction', () => {
  const key = (k: string, mods: Partial<ReplayKey> = {}): ReplayKey => ({
    key: k,
    code: '',
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    ...mods,
  })

  it('maps Shift+arrows and Esc while active', () => {
    expect(replayKeyAction(key('ArrowRight', { shiftKey: true }), 'active')).toBe('step-forward')
    expect(replayKeyAction(key('ArrowLeft', { shiftKey: true }), 'active')).toBe('step-back')
    expect(replayKeyAction(key('ArrowDown', { shiftKey: true }), 'active')).toBe('toggle-play')
    expect(replayKeyAction(key('Escape'), 'active')).toBe('exit')
  })

  it('repeats steps on a held key but not play/pause or exit', () => {
    const held = { shiftKey: true, repeat: true }
    expect(replayKeyAction(key('ArrowRight', held), 'active')).toBe('step-forward')
    expect(replayKeyAction(key('ArrowDown', held), 'active')).toBeNull()
    expect(replayKeyAction(key('Escape', { repeat: true }), 'active')).toBeNull()
  })

  it('Esc cancels pick mode; steps do nothing while picking', () => {
    expect(replayKeyAction(key('Escape'), 'picking')).toBe('cancel-pick')
    expect(replayKeyAction(key('ArrowRight', { shiftKey: true }), 'picking')).toBeNull()
  })

  it('ignores everything while off, plain arrows, and Ctrl/Meta/Alt combinations', () => {
    expect(replayKeyAction(key('Escape'), 'off')).toBeNull()
    expect(replayKeyAction(key('ArrowRight', { shiftKey: true }), 'off')).toBeNull()
    expect(replayKeyAction(key('ArrowRight'), 'active')).toBeNull()
    expect(replayKeyAction(key('ArrowRight', { shiftKey: true, altKey: true }), 'active')).toBe(
      null,
    )
    expect(replayKeyAction(key('ArrowLeft', { shiftKey: true, metaKey: true }), 'active')).toBe(
      null,
    )
    expect(replayKeyAction(key('Escape', { ctrlKey: true }), 'active')).toBeNull()
  })
})
