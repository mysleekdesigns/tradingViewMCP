import type { Candle } from '../types'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorStepper,
  SessionBox,
  SessionKey,
  SessionRanges,
} from './types'

/**
 * Session ranges overlay (Phase 18): per-session high/low boxes for the Asia, London and New York
 * sessions plus the UTC-day opening range, from closed bars only. Visual only, no edge claim. The
 * rules are written out in `web/README.md` ("Session ranges overlay") and pinned by the
 * hand-worked fixture in `sessionRanges.fixture.ts`, the parity source for any future lab feature.
 *
 * Rules (all UTC, times are bar open times in UNIX seconds):
 * - A window is a start hour (0..23) and an end hour (1..24). Its duration is
 *   ((end - start + 24) % 24) hours, or 24 when that is 0: end > start stays on the same UTC day,
 *   end <= start wraps past UTC midnight into the next day.
 * - An instance starts at D + start * 3600 for every UTC day D. A bar at t belongs to the latest
 *   instance start s <= t when t < s + duration (membership by open time; a bar that straddles
 *   the end counts for the session it opens in).
 * - The opening range is the window [D, D + N * 3600), N = 1..12.
 * - Every session is evaluated on its own, so one bar can be in several (overlaps).
 * - The compute takes any bar length. Drawing is limited to sessions that line up with the bars
 *   (`sessionAligned`, a display rule applied by the chart layer).
 * - A box is the highest high and lowest low of its instance's closed bars so far. A session with
 *   no bars has no box (no forward fill); a gap inside a session just means fewer bars.
 */

export interface SessionWindow {
  startHour: number
  endHour: number
}

export interface SessionRangesParams {
  asia: SessionWindow
  london: SessionWindow
  newyork: SessionWindow
  openingHours: number
}

export const SESSION_RANGES_DEFAULTS: Readonly<SessionRangesParams> = {
  asia: { startHour: 0, endHour: 8 },
  london: { startHour: 7, endHour: 16 },
  newyork: { startHour: 13, endHour: 22 },
  openingHours: 1,
}

/** Session order: also the tie order of boxes with equal starts. */
export const SESSION_KEYS: readonly SessionKey[] = ['asia', 'london', 'newyork', 'opening']

export const SESSION_SHOW_KEYS = ['showAsia', 'showLondon', 'showNewYork', 'showOpening'] as const

const START_MIN = 0
const START_MAX = 23
const END_MIN = 1
const END_MAX = 24
const OR_MIN = 1
const OR_MAX = 12

const DAY = 86_400
const HOUR = 3_600

function whole(v: number | undefined, fallback: number, min: number, max: number): number {
  if (v === undefined || !Number.isFinite(v)) return fallback
  return Math.min(max, Math.max(min, Math.round(v)))
}

function toWindow(
  start: number | undefined,
  end: number | undefined,
  fallback: SessionWindow,
): SessionWindow {
  return {
    startHour: whole(start, fallback.startHour, START_MIN, START_MAX),
    endHour: whole(end, fallback.endHour, END_MIN, END_MAX),
  }
}

function normalize(p: SessionRangesParams): SessionRangesParams {
  const d = SESSION_RANGES_DEFAULTS
  return {
    asia: toWindow(p.asia.startHour, p.asia.endHour, d.asia),
    london: toWindow(p.london.startHour, p.london.endHour, d.london),
    newyork: toWindow(p.newyork.startHour, p.newyork.endHour, d.newyork),
    openingHours: whole(p.openingHours, d.openingHours, OR_MIN, OR_MAX),
  }
}

/** Reads the session hours and N from inputs: rounded, clamped, defaults when missing. */
export function sessionParams(inputs: IndicatorInputs): SessionRangesParams {
  const d = SESSION_RANGES_DEFAULTS
  return {
    asia: toWindow(inputs.asiaStart, inputs.asiaEnd, d.asia),
    london: toWindow(inputs.londonStart, inputs.londonEnd, d.london),
    newyork: toWindow(inputs.nyStart, inputs.nyEnd, d.newyork),
    openingHours: whole(inputs.orHours, d.openingHours, OR_MIN, OR_MAX),
  }
}

/** Compute-relevant key: changes only with the session hours and N (not the show* toggles). */
export function sessionParamsKey(p: SessionRangesParams): string {
  const w = (s: SessionWindow) => `${s.startHour}-${s.endHour}`
  return `${w(p.asia)}:${w(p.london)}:${w(p.newyork)}:${p.openingHours}`
}

/** Window duration in hours: ((end - start + 24) % 24), or 24 when that is 0. */
export function sessionDurationHours(w: SessionWindow): number {
  const h = (w.endHour - w.startHour + 24) % 24
  return h === 0 ? 24 : h
}

/**
 * Display rule (the compute is interval-agnostic): a session lines up with bars of
 * `intervalSeconds` (L, a whole number > 0) when the bars tile the UTC day and both the session's
 * start and its length are whole bars: 86400 % L === 0, (startHour * 3600) % L === 0 and
 * (durationHours * 3600) % L === 0. Otherwise its box would cover other hours than the session's
 * (on 4h the New York box 13-22 would hold the 16:00 and 20:00 bars, i.e. 16:00-24:00), so the
 * chart hides it. Bars below 1d open on the epoch grid, so 86400 % L === 0 also means every UTC
 * day starts on a bar.
 */
export function sessionAligned(
  startHour: number,
  durationHours: number,
  intervalSeconds: number,
): boolean {
  const L = intervalSeconds
  if (!Number.isInteger(L) || L <= 0) return false
  return DAY % L === 0 && (startHour * HOUR) % L === 0 && (durationHours * HOUR) % L === 0
}

/** The four session windows (the opening range is start 0, duration N) after normalizing. */
function sessionWindows(params: SessionRangesParams): Record<SessionKey, SessionWindow> {
  const p = normalize(params)
  return {
    asia: p.asia,
    london: p.london,
    newyork: p.newyork,
    opening: { startHour: 0, endHour: p.openingHours },
  }
}

/** Which sessions line up with bars of `intervalSeconds` (see `sessionAligned`). */
export function alignedSessions(
  params: SessionRangesParams,
  intervalSeconds: number,
): Readonly<Record<SessionKey, boolean>> {
  const windows = sessionWindows(params)
  const out = {} as Record<SessionKey, boolean>
  for (const key of SESSION_KEYS) {
    const w = windows[key]
    out[key] = sessionAligned(w.startHour, sessionDurationHours(w), intervalSeconds)
  }
  return out
}

interface SessionTrack {
  key: SessionKey
  /** Seconds after UTC midnight that an instance starts. */
  offset: number
  /** Instance length in seconds (1h..24h). */
  duration: number
  /** Open box of the latest instance seen, and its index in `boxes`. */
  cur: SessionBox | null
  curIdx: number
}

export class SessionRangesEngine {
  private readonly tracks: SessionTrack[]
  private readonly boxes: SessionBox[] = []
  private readonly fresh: SessionBox[] = []
  private n = 0
  private lastTime = -Infinity

  constructor(params: SessionRangesParams) {
    const windows = sessionWindows(params)
    this.tracks = SESSION_KEYS.map((key) => ({
      key,
      offset: windows[key].startHour * HOUR,
      duration: sessionDurationHours(windows[key]) * HOUR,
      cur: null,
      curIdx: -1,
    }))
  }

  /** Closed bars pushed so far. */
  get count(): number {
    return this.n
  }

  /**
   * Push the next CLOSED bar (ascending time, strictly after the previous). O(1). Throws on a bar
   * that is not after the previous one: a revised or older bar needs a fresh engine.
   */
  push(candle: Candle): void {
    const t = candle.time
    if (!(t > this.lastTime)) {
      throw new RangeError(`SessionRangesEngine: bar ${t} is not after ${this.lastTime}`)
    }
    this.lastTime = t
    this.n++
    const day = Math.floor(t / DAY) * DAY
    const fresh = this.fresh
    fresh.length = 0
    for (const tr of this.tracks) {
      let start = day + tr.offset
      if (start > t) start -= DAY
      if (t >= start + tr.duration) continue
      const cur = tr.cur
      if (cur !== null && cur.start === start) {
        // Replace, never mutate: a box handed out by result() keeps its values.
        const next: SessionBox = {
          ...cur,
          high: candle.high > cur.high ? candle.high : cur.high,
          low: candle.low < cur.low ? candle.low : cur.low,
          lastTime: t,
          bars: cur.bars + 1,
        }
        tr.cur = next
        this.boxes[tr.curIdx] = next
      } else {
        const box: SessionBox = {
          session: tr.key,
          start,
          end: start + tr.duration,
          high: candle.high,
          low: candle.low,
          firstTime: t,
          lastTime: t,
          bars: 1,
        }
        tr.cur = box
        fresh.push(box)
      }
    }
    if (fresh.length === 0) return
    // Boxes opened by earlier bars always start before boxes opened by this bar (an earlier start
    // would contain the earlier bar too), so only this bar's new boxes need ordering: by start,
    // then session order (tracks are visited in that order, and the sort is stable).
    if (fresh.length > 1) fresh.sort((a, b) => a.start - b.start)
    for (const box of fresh) {
      const tr = this.tracks[SESSION_KEYS.indexOf(box.session)]
      tr.curIdx = this.boxes.length
      this.boxes.push(box)
    }
  }

  /** Fresh array each call. Boxes returned before are never mutated; a finished box keeps its identity. */
  result(): SessionRanges {
    return { boxes: this.boxes.slice() }
  }
}

/** Full computation over closed bars (ascending). One O(n) pass. */
export function computeSessionRanges(
  closed: readonly Candle[],
  params: SessionRangesParams,
): SessionRanges {
  const engine = new SessionRangesEngine(params)
  for (const c of closed) engine.push(c)
  return engine.result()
}

function emptyStepper(): IndicatorStepper {
  return {
    next() {
      return {}
    },
    clone() {
      return emptyStepper()
    },
  }
}

function hour(key: string, label: string, def: number, min: number, max: number) {
  return { key, label, default: def, min, max, step: 1, integer: true }
}

function toggle(key: string, label: string) {
  return {
    key,
    label,
    kind: 'toggle' as const,
    default: 1,
    min: 0,
    max: 1,
    step: 1,
    integer: true,
  }
}

const D = SESSION_RANGES_DEFAULTS

/**
 * Session ranges (Phase 18): drawn by a primitive from `sessions()`; it has no per-bar lines.
 * Visual only: it makes no edge claim.
 */
export const sessionRangesDef: IndicatorDef = {
  id: 'sessions',
  name: 'Session Ranges',
  shortName: 'SR',
  description: 'Asia / London / New York high-low boxes and the UTC opening range (visual only)',
  placement: 'overlay',
  inputs: [
    hour('asiaStart', 'Asia start (UTC h)', D.asia.startHour, START_MIN, START_MAX),
    hour('asiaEnd', 'Asia end (UTC h)', D.asia.endHour, END_MIN, END_MAX),
    hour('londonStart', 'London start (UTC h)', D.london.startHour, START_MIN, START_MAX),
    hour('londonEnd', 'London end (UTC h)', D.london.endHour, END_MIN, END_MAX),
    hour('nyStart', 'New York start (UTC h)', D.newyork.startHour, START_MIN, START_MAX),
    hour('nyEnd', 'New York end (UTC h)', D.newyork.endHour, END_MIN, END_MAX),
    hour('orHours', 'Opening range (h)', D.openingHours, OR_MIN, OR_MAX),
    toggle('showAsia', 'Asia'),
    toggle('showLondon', 'London'),
    toggle('showNewYork', 'New York'),
    toggle('showOpening', 'Opening range'),
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper() {
    return emptyStepper()
  },
  sessions(closed: readonly Candle[], inputs: IndicatorInputs) {
    return computeSessionRanges(closed, sessionParams(inputs))
  },
}
