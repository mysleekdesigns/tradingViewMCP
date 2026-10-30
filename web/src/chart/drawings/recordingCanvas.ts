// Test helpers for the drawings renderer: a fake 2D context that records every property set
// and method call in order, and a representative set of scenes (every drawing kind, line
// styles, selection and hover handles, light and dark handle colours, several pixel ratios).
// Two renderers paint the same pixels when they produce the same recording.

import { DrawingController } from './controller'
import { DrawingsPrimitive } from './primitives'
import { barTime, fakeAdapter } from './testAdapter'
import { handleColorsFor, paletteFor, DEFAULT_APPEARANCE, type HandleColors } from '../theme'
import type { CoordinateAdapter } from './geometry'
import type { Drawing, DrawingStyle } from './types'

/** One recorded operation: `['set', key, value]` or `['call', name, ...args]`. */
export type CanvasOp = readonly unknown[]

export interface RecordingCanvas {
  readonly ctx: CanvasRenderingContext2D
  readonly ops: CanvasOp[]
  /** The exact array objects passed to setLineDash, in call order (for allocation checks). */
  readonly dashArgs: unknown[]
}

/** A context whose every set and call is appended to `ops` (arrays copied, so later edits show). */
export function recordingCanvas(): RecordingCanvas {
  const ops: CanvasOp[] = []
  const dashArgs: unknown[] = []
  const state: Record<string | symbol, unknown> = {}
  const copy = (v: unknown) => (Array.isArray(v) ? [...(v as unknown[])] : v)
  const ctx = new Proxy(state, {
    get(target, key) {
      if (key in target) return target[key]
      return (...args: unknown[]) => {
        if (key === 'setLineDash') dashArgs.push(args[0])
        ops.push(['call', String(key), ...args.map(copy)])
      }
    },
    set(target, key, value) {
      target[key] = value
      ops.push(['set', String(key), copy(value)])
      return true
    },
  })
  return { ctx: ctx as unknown as CanvasRenderingContext2D, ops, dashArgs }
}

type Renderer = NonNullable<ReturnType<ReturnType<DrawingsPrimitive['paneViews']>[0]['renderer']>>
type Target = Parameters<Renderer['draw']>[0]

/** Paint the primitive's pane view once onto `canvas` at the given pixel ratios. */
export function paint(
  primitive: DrawingsPrimitive,
  canvas: RecordingCanvas,
  horizontalPixelRatio: number,
  verticalPixelRatio: number = horizontalPixelRatio,
): void {
  const scope = { context: canvas.ctx, horizontalPixelRatio, verticalPixelRatio }
  const target = {
    useBitmapCoordinateSpace: (f: (s: typeof scope) => void) => f(scope),
  } as unknown as Target
  primitive.paneViews()[0].renderer()?.draw(target)
}

const style = (
  color: string,
  lineWidth: DrawingStyle['lineWidth'],
  lineStyle: DrawingStyle['lineStyle'],
) => ({ color, lineWidth, lineStyle }) satisfies DrawingStyle

function d(
  id: string,
  kind: Drawing['kind'],
  anchors: Drawing['anchors'],
  s: DrawingStyle,
): Drawing {
  return { id, symbol: 'BTCUSDT', kind, anchors, style: s }
}

/** Every kind and style the renderer has a branch for. Fake chart: x = 10 * bar, y = (200 - p) * 2. */
export const SCENE_DRAWINGS: readonly Drawing[] = [
  d('h1', 'hline', [{ time: barTime(3), price: 150 }], style('#ff9800', 1, 'solid')),
  // Anchor far right of the pane: the handle is clamped inside it.
  d('h2', 'hline', [{ time: barTime(500), price: 120.5 }], style('#ffffff', 4, 'dotted')),
  d('r1', 'hray', [{ time: barTime(12), price: 140 }], style('#26a69a', 2, 'dashed')),
  d(
    't1',
    'trendline',
    [
      { time: barTime(1), price: 100 },
      { time: barTime(5), price: 110 },
    ],
    style('#2962ff', 3, 'solid'),
  ),
  d(
    't2',
    'trendline',
    [
      { time: barTime(20), price: 90 },
      { time: barTime(40), price: 130.25 },
    ],
    style('#e91e63', 1, 'dotted'),
  ),
  d(
    't3',
    'trendline',
    [
      { time: barTime(8), price: 60 },
      { time: barTime(30) + 17, price: 75 },
    ],
    style('#131722', 2, 'dashed'),
  ),
  d(
    'x1',
    'rectangle',
    [
      { time: barTime(30), price: 120 },
      { time: barTime(45), price: 95 },
    ],
    style('#f23645', 2, 'dashed'),
  ),
  // Three-digit hex: withAlpha's expansion branch.
  d(
    'x2',
    'rectangle',
    [
      { time: barTime(60), price: 80 },
      { time: barTime(52), price: 105 },
    ],
    style('#abc', 1, 'solid'),
  ),
  d(
    'f1',
    'fib',
    [
      { time: barTime(10), price: 100 },
      { time: barTime(25), price: 180 },
    ],
    style('#2962ff', 2, 'solid'),
  ),
  // Reversed, with small prices (8-decimal labels) and a dashed style.
  d(
    'f2',
    'fib',
    [
      { time: barTime(70), price: 0.004 },
      { time: barTime(55), price: 0.0071 },
    ],
    style('#9c27b0', 1, 'dashed'),
  ),
  // Large prices: thousands separators.
  d(
    'f3',
    'fib',
    [
      { time: barTime(65), price: 65012.5 },
      { time: barTime(75), price: 64000 },
    ],
    style('#ffeb3b', 3, 'dotted'),
  ),
]

export interface Scene {
  name: string
  selected: string | null
  hovered: string | null
  handles: HandleColors
}

const DARK = handleColorsFor(paletteFor(DEFAULT_APPEARANCE))
const LIGHT = handleColorsFor(paletteFor({ ...DEFAULT_APPEARANCE, theme: 'light' }))

const SELECTIONS: readonly (readonly [string | null, string | null])[] = [
  [null, null],
  ['t1', null],
  ['x1', 'f1'],
  ['h2', 'r1'],
  ['f3', 'x2'],
  ['t3', 'h1'],
  [null, 'f2'],
]

export const SCENES: readonly Scene[] = SELECTIONS.flatMap(([selected, hovered]) =>
  (
    [
      ['dark', DARK],
      ['light', LIGHT],
    ] as const
  ).map(([theme, handles]) => ({
    name: `${theme} sel=${selected} hov=${hovered}`,
    selected,
    hovered,
    handles,
  })),
)

export const PIXEL_RATIOS: readonly (readonly [number, number])[] = [
  [1, 1],
  [1.5, 1.5],
  [2, 2],
  [2, 1.25],
]

/** A controller + primitive showing SCENE_DRAWINGS in `scene`, views updated. */
export function sceneSetup(scene: Scene, adapter: CoordinateAdapter = fakeAdapter()) {
  const controller = new DrawingController(adapter)
  controller.setDrawings(SCENE_DRAWINGS)
  controller.setSelected(scene.selected)
  controller.setHovered(scene.hovered)
  const primitive = new DrawingsPrimitive(controller, adapter)
  primitive.setHandleColors(scene.handles)
  primitive.updateAllViews()
  return { controller, primitive }
}

/** The recording of every scene at every pixel ratio, keyed by scene and ratio. */
export function recordAllScenes(): Record<string, CanvasOp[]> {
  const out: Record<string, CanvasOp[]> = {}
  for (const scene of SCENES) {
    const { primitive } = sceneSetup(scene)
    for (const [hr, vr] of PIXEL_RATIOS) {
      const canvas = recordingCanvas()
      paint(primitive, canvas, hr, vr)
      out[`${scene.name} @${hr}x${vr}`] = canvas.ops
    }
  }
  return out
}
