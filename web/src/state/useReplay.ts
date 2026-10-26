import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Candle } from '../types'
import {
  REPLAY_OFF,
  cancelPicking as cancelPickingState,
  exit as exitState,
  pause as pauseState,
  pickAt as pickAtState,
  play as playState,
  reconcile,
  replayAtEnd,
  replayBarsAhead,
  replayCurrentBar,
  replayIntervalMs,
  replayVisible,
  sameBars,
  setSpeed as setSpeedState,
  startBarsBack as startBarsBackState,
  startPicking as startPickingState,
  stepBack as stepBackState,
  stepForward as stepForwardState,
  tick,
  togglePlay as togglePlayState,
  type ReplayOptions,
  type ReplayState,
} from './replay'

export interface UseReplay {
  state: ReplayState
  /** The bars to chart while replaying (closed, time <= cursor), or null for the live view. */
  visible: readonly Candle[] | null
  /** The last visible bar (replay price feed), or null when not active. */
  currentBar: Candle | null
  /** The cursor is on the latest closed bar: nothing left to step or play to. */
  atEnd: boolean
  /** Closed bars still hidden after the cursor. */
  barsAhead: number
  /** Same as state.revision: bumps on every start, step, and exit. */
  revision: number
  startPicking(): void
  cancelPicking(): void
  /** A chart click in pick mode, as UNIX seconds (ignored unless picking). */
  pickAt(time: number): void
  startBarsBack(n: number): void
  /** Step `n` bars forward (default 1); stops playback at the end. */
  stepForward(n?: number): void
  /** Step `n` bars back (default 1); pauses playback. */
  stepBack(n?: number): void
  play(): void
  pause(): void
  togglePlay(): void
  setSpeed(n: number): void
  exit(): void
}

type Op = (candles: readonly Candle[], state: ReplayState, opts: ReplayOptions) => ReplayState

/**
 * Bar replay over one panel's candles (ascending; the last row may be forming unless
 * `opts.lastBarClosed`). Playback runs on an interval at `state.speed` bars per second and is
 * cleared on pause, speed change, exit, and unmount. The caller exits replay when the symbol or
 * interval changes; a source that no longer reaches back to the cursor ends the replay.
 */
export function useReplay(candles: readonly Candle[], opts: ReplayOptions = {}): UseReplay {
  const lastBarClosed = opts.lastBarClosed === true
  const options = useMemo<ReplayOptions>(() => ({ lastBarClosed }), [lastBarClosed])
  const [raw, setRaw] = useState<ReplayState>(REPLAY_OFF)

  // Actions read the latest candles when they run, not when they were created.
  const candlesRef = useRef(candles)
  const optionsRef = useRef(options)
  useLayoutEffect(() => {
    candlesRef.current = candles
    optionsRef.current = options
  }, [candles, options])

  const apply = useCallback(
    (op: Op) => setRaw((s) => op(candlesRef.current, s, optionsRef.current)),
    [],
  )

  const state = useMemo(() => reconcile(candles, raw, options), [candles, raw, options])
  // A replaced source can end the replay (reconcile): store that, so later actions start from
  // 'off' rather than from the stale cursor. Adjusted during render; reconcile is idempotent.
  if (state !== raw) setRaw(state)

  // Keep `visible` the same array while its bars are the same objects, so a tick of the hidden
  // forming bar does not make consumers recompute indicators.
  const fresh = useMemo(() => replayVisible(candles, state, options), [candles, state, options])
  const [stable, setStable] = useState(fresh)
  let visible = stable
  if (!sameBars(stable, fresh)) {
    setStable(fresh)
    visible = fresh
  }

  const currentBar = useMemo(
    () => replayCurrentBar(candles, state, options),
    [candles, state, options],
  )
  const atEnd = replayAtEnd(candles, state, options)
  const barsAhead = replayBarsAhead(candles, state, options)

  const playing = state.status === 'active' && state.playing
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => apply(tick), replayIntervalMs(state.speed))
    return () => clearInterval(id)
  }, [playing, state.speed, apply])

  const startPicking = useCallback(() => setRaw(startPickingState), [])
  const cancelPicking = useCallback(() => setRaw(cancelPickingState), [])
  const pickAt = useCallback(
    (time: number) => apply((c, s, o) => pickAtState(c, s, time, o)),
    [apply],
  )
  const startBarsBack = useCallback(
    (n: number) => apply((c, s, o) => startBarsBackState(c, s, n, o)),
    [apply],
  )
  const stepForward = useCallback(
    (n = 1) => apply((c, s, o) => stepForwardState(c, s, o, n)),
    [apply],
  )
  const stepBack = useCallback((n = 1) => apply((c, s, o) => stepBackState(c, s, o, n)), [apply])
  const play = useCallback(() => apply(playState), [apply])
  const pause = useCallback(() => setRaw(pauseState), [])
  const togglePlay = useCallback(() => apply(togglePlayState), [apply])
  const setSpeed = useCallback((n: number) => setRaw((s) => setSpeedState(s, n)), [])
  const exit = useCallback(() => setRaw(exitState), [])

  return {
    state,
    visible,
    currentBar,
    atEnd,
    barsAhead,
    revision: state.revision,
    startPicking,
    cancelPicking,
    pickAt,
    startBarsBack,
    stepForward,
    stepBack,
    play,
    pause,
    togglePlay,
    setSpeed,
    exit,
  }
}
