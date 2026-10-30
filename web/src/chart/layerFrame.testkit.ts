// Test kit (no runtime use): a fake 2D context that records what each paint operation would
// put on the canvas, i.e. every fill / stroke / text call together with the context state it
// reads (styles, alpha, line width, dash, font). Two draws with the same recording paint the same
// pixels, however many redundant state sets either made. `setLineDash` arguments are kept by
// reference so a test can tell whether a draw allocated new dash arrays.

export type PathCommand =
  | readonly ['M', number, number]
  | readonly ['L', number, number]
  | readonly ['A', number, number, number, number, number]

interface PaintState {
  fillStyle: string
  strokeStyle: string
  globalAlpha: number
  lineWidth: number
  dash: number[]
  font: string
  textAlign: string
  textBaseline: string
}

export type PaintOp =
  | { op: 'save' | 'restore' }
  | { op: 'fillRect' | 'strokeRect'; rect: number[]; state: Partial<PaintState> }
  | { op: 'fill' | 'stroke'; path: PathCommand[]; state: Partial<PaintState> }
  | { op: 'fillText'; text: string; at: number[]; state: Partial<PaintState> }

/** Deliberately odd starting state, so a draw that relies on an inherited value shows up. */
const INITIAL: PaintState = {
  fillStyle: '#123456',
  strokeStyle: '#654321',
  globalAlpha: 0.33,
  lineWidth: 7,
  dash: [5, 5],
  font: '13px initial',
  textAlign: 'right',
  textBaseline: 'middle',
}

export interface CanvasRecording {
  /** Pass as `scope.context`. */
  ctx: CanvasRenderingContext2D
  ops: PaintOp[]
  /** Every array given to `setLineDash`, by reference, in call order. */
  dashArgs: number[][]
  /** Property writes and state calls (redundant ones included), by name. */
  writes: Map<string, number>
  /** The state after the draw (equals the initial state when save/restore balance). */
  finalState(): PaintState
  initialState(): PaintState
}

export function canvasRecording(): CanvasRecording {
  let state: PaintState = { ...INITIAL, dash: INITIAL.dash.slice() }
  const stack: PaintState[] = []
  let path: PathCommand[] = []
  const ops: PaintOp[] = []
  const dashArgs: number[][] = []
  const writes = new Map<string, number>()
  const count = (name: string) => writes.set(name, (writes.get(name) ?? 0) + 1)
  const fillState = (): Partial<PaintState> => ({
    fillStyle: state.fillStyle,
    globalAlpha: state.globalAlpha,
  })
  const strokeState = (): Partial<PaintState> => ({
    strokeStyle: state.strokeStyle,
    globalAlpha: state.globalAlpha,
    lineWidth: state.lineWidth,
    dash: state.dash.slice(),
  })
  const ctx = {
    save() {
      stack.push({ ...state, dash: state.dash.slice() })
      ops.push({ op: 'save' })
    },
    restore() {
      const s = stack.pop()
      if (s !== undefined) state = s
      ops.push({ op: 'restore' })
    },
    setLineDash(segments: number[]) {
      count('setLineDash')
      dashArgs.push(segments)
      state.dash = segments.slice()
    },
    beginPath() {
      path = []
    },
    moveTo(x: number, y: number) {
      path.push(['M', x, y])
    },
    lineTo(x: number, y: number) {
      path.push(['L', x, y])
    },
    arc(x: number, y: number, r: number, a0: number, a1: number) {
      path.push(['A', x, y, r, a0, a1])
    },
    stroke() {
      ops.push({ op: 'stroke', path: path.slice(), state: strokeState() })
    },
    fill() {
      ops.push({ op: 'fill', path: path.slice(), state: fillState() })
    },
    fillRect(x: number, y: number, w: number, h: number) {
      ops.push({ op: 'fillRect', rect: [x, y, w, h], state: fillState() })
    },
    strokeRect(x: number, y: number, w: number, h: number) {
      ops.push({ op: 'strokeRect', rect: [x, y, w, h], state: strokeState() })
    },
    fillText(text: string, x: number, y: number) {
      ops.push({
        op: 'fillText',
        text,
        at: [x, y],
        state: {
          fillStyle: state.fillStyle,
          globalAlpha: state.globalAlpha,
          font: state.font,
          textAlign: state.textAlign,
          textBaseline: state.textBaseline,
        },
      })
    },
  }
  for (const key of [
    'fillStyle',
    'strokeStyle',
    'globalAlpha',
    'lineWidth',
    'font',
    'textAlign',
    'textBaseline',
  ] as const) {
    Object.defineProperty(ctx, key, {
      get: () => state[key],
      set: (v: never) => {
        count(key)
        ;(state as unknown as Record<string, unknown>)[key] = v
      },
    })
  }
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    ops,
    dashArgs,
    writes,
    finalState: () => ({ ...state, dash: state.dash.slice() }),
    initialState: () => ({ ...INITIAL, dash: INITIAL.dash.slice() }),
  }
}

/** A bitmap scope over `ctx` with one pixel ratio for both axes (or two). */
export function bitmapScope(
  ctx: CanvasRenderingContext2D,
  horizontalPixelRatio: number,
  verticalPixelRatio = horizontalPixelRatio,
) {
  return {
    context: ctx,
    horizontalPixelRatio,
    verticalPixelRatio,
    mediaSize: { width: 800, height: 600 },
    bitmapSize: {
      width: Math.round(800 * horizontalPixelRatio),
      height: Math.round(600 * verticalPixelRatio),
    },
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Fake chart                                                                                  */
/* ------------------------------------------------------------------------------------------ */

/**
 * Everything the fake chart's coordinates depend on. Horizontal: logical index i sits at
 * `x0 + i * spacing` in a pane `width` wide; the visible bars are the plotted `times` whose x is
 * inside the pane. Vertical: y = offset - scale * price (or * ln(price) on a log scale).
 */
export interface FakeView {
  x0: number
  spacing: number
  width: number
  offset: number
  scale: number
  log: boolean
  /** Plotted bar times, ascending (the forming bar included). */
  times: number[]
}

/** First index in `times` whose value is >= t. */
function lowerBoundTimes(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (times[mid] < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** The fake's visible range: open times of the first and last bar inside the pane. */
export function fakeVisibleRange(v: FakeView): { from: number; to: number } | null {
  const n = v.times.length
  if (n === 0 || v.spacing <= 0) return null
  const first = Math.max(0, Math.ceil(-v.x0 / v.spacing))
  const last = Math.min(n - 1, Math.floor((v.width - v.x0) / v.spacing))
  if (first > last) return null
  return { from: v.times[first], to: v.times[last] }
}

export function fakeTimeToIndex(v: FakeView, t: number, nearest: boolean): number | null {
  const times = v.times
  if (times.length === 0) return null
  const i = lowerBoundTimes(times, t)
  if (i < times.length && times[i] === t) return i
  if (!nearest) return null
  if (i === 0) return 0
  if (i === times.length) return times.length - 1
  return t - times[i - 1] <= times[i] - t ? i - 1 : i
}

export function fakePriceToCoordinate(v: FakeView, p: number): number | null {
  if (!v.log) return v.offset - v.scale * p
  return p > 0 ? v.offset - v.scale * Math.log(p) : null
}

/**
 * A chart + series pair over `view` (read live, so a test mutates `view` to scroll, zoom,
 * resize or rescale) and a host that attaches primitives to them. `timeScale()` returns a fresh
 * object per call and `getVisibleRange()` a fresh range, as Lightweight Charts does.
 */
export function fakeChartHost(view: FakeView) {
  const attached: { attached?: (p: never) => void; detached?: () => void }[] = []
  let redraws = 0
  const chart = {
    timeScale: () => ({
      getVisibleRange: () => fakeVisibleRange(view),
      timeToIndex: (t: number, nearest?: boolean) => fakeTimeToIndex(view, t, nearest === true),
      timeToCoordinate: (t: number) => {
        const i = fakeTimeToIndex(view, t, false)
        return i === null ? null : view.x0 + i * view.spacing
      },
      logicalToCoordinate: (l: number) => view.x0 + l * view.spacing,
      width: () => view.width,
    }),
  }
  const series = { priceToCoordinate: (p: number) => fakePriceToCoordinate(view, p) }
  const host = {
    attachPrimitive(p: { attached?: (param: never) => void }) {
      attached.push(p)
      p.attached?.({ chart, series, requestUpdate: () => redraws++ } as never)
    },
    detachPrimitive(p: { detached?: () => void }) {
      const i = attached.indexOf(p)
      if (i >= 0) attached.splice(i, 1)
      p.detached?.()
    },
  }
  return { host, chart, series, redraws: () => redraws }
}
