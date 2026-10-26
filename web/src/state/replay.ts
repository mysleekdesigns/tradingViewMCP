// Bar replay: a pure state machine over one panel's candle array. Replay hides every bar after a
// cursor so history can be stepped or played forward bar by bar; indicators computed on the
// visible bars only is the manual look-ahead check (PLAN.md Phase 9).
//
// - Only CLOSED bars are ever shown. The source's last row is treated as still forming unless
//   the caller says otherwise (`lastBarClosed`).
// - The cursor is a bar TIME (UNIX seconds), not an index: older history is prepended
//   (pagination) and live bars are appended while replaying, which would shift an index.
// - `visible` = every closed bar with time <= cursorTime, never more. That holds while picking a
//   new start from a running replay too: the chart stays on the replay view, so a re-pick can
//   only land on the cursor bar or an earlier one (to start later, exit and start again).
//
// Every function returns the same state object when nothing changes, so React can skip renders.

import type { ShortcutKey } from '../components/drawingTools'
import type { Candle } from '../types'

/** Playback speeds in bars per second, slowest first. */
export const REPLAY_SPEEDS: readonly number[] = [1, 2, 5, 10]

export const DEFAULT_REPLAY_SPEED = 1

export interface ReplayState {
  /**
   * 'picking': waiting for a click on the chart. A fresh pick (from off) shows the live view
   * meanwhile; a re-pick from a running replay keeps the replay view (see `cursorTime`).
   */
  status: 'off' | 'picking' | 'active'
  /**
   * Time of the last visible bar. Kept while picking: a re-pick still shows only the bars up to
   * it, may only land at or before it, and cancelling resumes the replay there. Null when off
   * and during a fresh pick.
   */
  cursorTime: number | null
  playing: boolean
  /** Bars per second while playing; one of REPLAY_SPEEDS. */
  speed: number
  /** Bumped on every change of what replay shows (start, step, exit). Never decreases. */
  revision: number
}

export interface ReplayOptions {
  /**
   * The source's last row is a closed bar (e.g. a static history). Default false: the last
   * row may still be forming, so replay never shows it.
   */
  lastBarClosed?: boolean
}

export const REPLAY_OFF: ReplayState = {
  status: 'off',
  cursorTime: null,
  playing: false,
  speed: DEFAULT_REPLAY_SPEED,
  revision: 0,
}

/** The bars replay may show: the source without its (possibly) forming last row. */
export function closedBars(
  candles: readonly Candle[],
  opts: ReplayOptions = {},
): readonly Candle[] {
  return opts.lastBarClosed === true ? candles : candles.slice(0, -1)
}

/** Index of the last bar with time <= t in ascending `bars`, or -1 when every bar is later. */
export function lastIndexAtOrBefore(bars: readonly Candle[], t: number): number {
  let lo = 0
  let hi = bars.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (bars[mid].time <= t) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/** Closed-bar index of the cursor, or -1 when replay is not active or nothing is visible. */
export function replayCursorIndex(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): number {
  if (state.status !== 'active' || state.cursorTime === null) return -1
  return lastIndexAtOrBefore(closedBars(candles, opts), state.cursorTime)
}

/**
 * The bars a replaying chart shows (closed bars with time <= cursorTime), also while re-picking
 * from a running replay; null when replay is off or on a fresh pick, and the live candles apply.
 */
export function replayVisible(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): readonly Candle[] | null {
  if (state.status === 'off' || state.cursorTime === null) return null
  const closed = closedBars(candles, opts)
  return closed.slice(0, lastIndexAtOrBefore(closed, state.cursorTime) + 1)
}

/** A re-pick from a running replay: the replay view stays, and the pick is capped at the cursor. */
export function isRepicking(state: ReplayState): boolean {
  return state.status === 'picking' && state.cursorTime !== null
}

/** The last visible bar (a paper-trading price feed during replay), or null. */
export function replayCurrentBar(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): Candle | null {
  const i = replayCursorIndex(candles, state, opts)
  return i < 0 ? null : closedBars(candles, opts)[i]
}

/** True when the cursor is on (or past) the latest closed bar: nothing left to step to. */
export function replayAtEnd(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): boolean {
  if (state.status !== 'active') return false
  return replayCursorIndex(candles, state, opts) >= closedBars(candles, opts).length - 1
}

/** Closed bars still hidden after the cursor (0 at the end, and when not active). */
export function replayBarsAhead(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): number {
  if (state.status !== 'active') return 0
  const closed = closedBars(candles, opts)
  return Math.max(0, closed.length - 1 - replayCursorIndex(candles, state, opts))
}

/** The nearest allowed speed to `n` (DEFAULT_REPLAY_SPEED for non-finite input). */
export function normalizeSpeed(n: number): number {
  if (!Number.isFinite(n)) return DEFAULT_REPLAY_SPEED
  let best = REPLAY_SPEEDS[0]
  for (const s of REPLAY_SPEEDS) if (Math.abs(s - n) < Math.abs(best - n)) best = s
  return best
}

/** Milliseconds between playback steps at `speed` bars per second. */
export function replayIntervalMs(speed: number): number {
  return Math.round(1000 / normalizeSpeed(speed))
}

function activeAt(state: ReplayState, cursorTime: number, playing: boolean): ReplayState {
  if (state.status === 'active' && state.cursorTime === cursorTime && state.playing === playing) {
    return state
  }
  const moved = state.status !== 'active' || state.cursorTime !== cursorTime
  return {
    status: 'active',
    cursorTime,
    playing,
    speed: state.speed,
    revision: moved ? state.revision + 1 : state.revision,
  }
}

/**
 * Start (or restart) replay at the last closed bar with time <= `time`; a time before the
 * first bar starts at the first bar, so at least one bar is visible. Paused. With no closed
 * bars there is nothing to replay and the state turns off.
 */
export function startAt(
  candles: readonly Candle[],
  state: ReplayState,
  time: number,
  opts: ReplayOptions = {},
): ReplayState {
  const closed = closedBars(candles, opts)
  if (closed.length === 0 || !Number.isFinite(time)) return exit(state)
  const i = Math.max(0, lastIndexAtOrBefore(closed, time))
  return activeAt(state, closed[i].time, false)
}

/**
 * Start replay `n` bars back from the latest closed bar (n = 0 starts on it), clamped to the
 * first bar. Paused.
 */
export function startBarsBack(
  candles: readonly Candle[],
  state: ReplayState,
  n: number,
  opts: ReplayOptions = {},
): ReplayState {
  const closed = closedBars(candles, opts)
  if (closed.length === 0) return exit(state)
  const back = Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 0
  return activeAt(state, closed[Math.max(0, closed.length - 1 - back)].time, false)
}

/** Enter pick mode (the next chart click starts replay). Pauses playback. */
export function startPicking(state: ReplayState): ReplayState {
  if (state.status === 'picking' && !state.playing) return state
  return { ...state, status: 'picking', playing: false }
}

/** Leave pick mode: back to the replay that was running, or off. */
export function cancelPicking(state: ReplayState): ReplayState {
  if (state.status !== 'picking') return state
  return state.cursorTime === null ? { ...state, status: 'off' } : { ...state, status: 'active' }
}

/**
 * A chart click while picking (no-op otherwise). A re-pick from a running replay is capped at
 * its cursor: only bars already shown can be picked, so nothing past the cursor is revealed.
 */
export function pickAt(
  candles: readonly Candle[],
  state: ReplayState,
  time: number,
  opts: ReplayOptions = {},
): ReplayState {
  if (state.status !== 'picking') return state
  const capped = state.cursorTime !== null ? Math.min(time, state.cursorTime) : time
  return startAt(candles, state, capped, opts)
}

/**
 * Keep a cursor valid after the source changed: with no closed bars replay turns off, and so
 * does a cursor before the first bar (the source was replaced by a window that does not reach
 * back to it): snapping forward would skip the bars in between, and the open paper orders with
 * them. Prepending or appending bars leaves the cursor alone. Applies to an active replay and to
 * a re-pick. Idempotent.
 */
export function reconcile(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): ReplayState {
  if (state.status === 'off' || state.cursorTime === null) return state
  const closed = closedBars(candles, opts)
  if (closed.length === 0 || state.cursorTime < closed[0].time) return exit(state)
  return state
}

/**
 * Move the cursor `n` bars forward (default 1), clamped to the latest closed bar. Playback
 * stops on reaching it. No-op unless active.
 */
export function stepForward(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
  n = 1,
): ReplayState {
  const s = reconcile(candles, state, opts)
  if (s.status !== 'active' || s.cursorTime === null) return s
  const closed = closedBars(candles, opts)
  const last = closed.length - 1
  const step = Number.isFinite(n) ? Math.max(1, Math.floor(n)) : 1
  const i = Math.min(last, lastIndexAtOrBefore(closed, s.cursorTime) + step)
  return activeAt(s, closed[i].time, s.playing && i < last)
}

/**
 * Move the cursor `n` bars back (default 1), never before the first bar. Stepping back pauses
 * playback. No-op unless active.
 */
export function stepBack(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
  n = 1,
): ReplayState {
  const s = reconcile(candles, state, opts)
  if (s.status !== 'active' || s.cursorTime === null) return s
  const closed = closedBars(candles, opts)
  const step = Number.isFinite(n) ? Math.max(1, Math.floor(n)) : 1
  const i = Math.max(0, lastIndexAtOrBefore(closed, s.cursorTime) - step)
  return activeAt(s, closed[i].time, false)
}

/** One playback step: like stepForward(1), but a no-op unless playing. */
export function tick(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): ReplayState {
  return state.status === 'active' && state.playing ? stepForward(candles, state, opts) : state
}

/** Start playback. A no-op at the latest closed bar (nothing to play) or unless active. */
export function play(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): ReplayState {
  const s = reconcile(candles, state, opts)
  if (s.status !== 'active' || s.playing || replayAtEnd(candles, s, opts)) return s
  return { ...s, playing: true }
}

export function pause(state: ReplayState): ReplayState {
  return state.playing ? { ...state, playing: false } : state
}

export function togglePlay(
  candles: readonly Candle[],
  state: ReplayState,
  opts: ReplayOptions = {},
): ReplayState {
  return state.playing ? pause(state) : play(candles, state, opts)
}

/** Set the playback speed (snapped to REPLAY_SPEEDS). Kept across exits. */
export function setSpeed(state: ReplayState, n: number): ReplayState {
  const speed = normalizeSpeed(n)
  return speed === state.speed ? state : { ...state, speed }
}

/** Leave replay (the live view returns). Keeps the speed; bumps the revision. */
export function exit(state: ReplayState): ReplayState {
  if (state.status === 'off') return state
  return { ...REPLAY_OFF, speed: state.speed, revision: state.revision + 1 }
}

/** Element-wise identity of two bar arrays (lets a hook keep `visible` stable across ticks). */
export function sameBars(a: readonly Candle[] | null, b: readonly Candle[] | null): boolean {
  if (a === b) return true
  if (a === null || b === null || a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}

export type ReplayKeyAction = 'step-forward' | 'step-back' | 'toggle-play' | 'exit' | 'cancel-pick'

/** The parts of a keydown replayKeyAction reads. */
export interface ReplayKey extends ShortcutKey {
  repeat?: boolean
}

/**
 * Replay keys, TradingView style, or null:
 * - Shift+→ step forward, Shift+← step back (held keys repeat)
 * - Shift+↓ play / pause (once per press)
 * - Esc: cancel pick mode while picking, exit replay while active (once per press)
 * Nothing fires while replay is off, or with Ctrl/Meta/Alt held. None of these overlap the app
 * shortcuts (Alt+digit, '[' ']', Alt+S, Alt+Shift+L, '?', Alt+H/J/T/F/R/A, Delete). Esc is
 * shared with "cancel drawing / deselect" and dialogs: the caller lets those claim it first.
 * The caller also skips text fields and composing/defaultPrevented events.
 */
export function replayKeyAction(
  e: ReplayKey,
  status: ReplayState['status'],
): ReplayKeyAction | null {
  if (status === 'off' || e.ctrlKey || e.metaKey || e.altKey) return null
  if (e.key === 'Escape') {
    if (e.shiftKey || e.repeat === true) return null
    return status === 'picking' ? 'cancel-pick' : 'exit'
  }
  if (status !== 'active' || !e.shiftKey) return null
  switch (e.key) {
    case 'ArrowRight':
      return 'step-forward'
    case 'ArrowLeft':
      return 'step-back'
    case 'ArrowDown':
      return e.repeat === true ? null : 'toggle-play'
    default:
      return null
  }
}
