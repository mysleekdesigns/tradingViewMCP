// Drawing model shared by the chart (rendering + interaction), state (persistence) and UI
// (toolbar, style bar). Anchors are `{ time, price }` so a drawing stays put while the user
// zooms, scrolls, or switches interval. `time` is UNIX seconds UTC.

export type DrawingKind = 'hline' | 'hray' | 'trendline' | 'fib' | 'rectangle'

/** What a left-button press on the chart does: pan/select (`cursor`) or start a drawing. */
export type ToolMode = 'cursor' | DrawingKind

export const DRAWING_KINDS: readonly DrawingKind[] = [
  'hline',
  'hray',
  'trendline',
  'fib',
  'rectangle',
]

/** Number of anchors each kind needs. hline uses only the price; hray starts at the time. */
export const ANCHOR_COUNT: Readonly<Record<DrawingKind, 1 | 2>> = {
  hline: 1,
  hray: 1,
  trendline: 2,
  fib: 2,
  rectangle: 2,
}

export interface Anchor {
  /** UNIX seconds UTC. Snapped to a bar open time when placed; may lie past the last bar. */
  time: number
  price: number
}

export type LineStyleName = 'solid' | 'dashed' | 'dotted'

export interface DrawingStyle {
  /** CSS hex colour `#rrggbb`. */
  color: string
  lineWidth: 1 | 2 | 3 | 4
  lineStyle: LineStyleName
}

export const DEFAULT_DRAWING_STYLE: DrawingStyle = {
  color: '#2962ff',
  lineWidth: 2,
  lineStyle: 'solid',
}

/** Fibonacci retracement levels, from anchors[0] (level 0) to anchors[1] (level 1). */
export const FIB_LEVELS: readonly number[] = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]

export interface Drawing {
  /** `^[A-Za-z0-9_-]{1,64}$` (crypto.randomUUID() qualifies). Client-generated. */
  id: string
  /** Upper-case exchange symbol, e.g. BTCUSDT. Drawings are persisted per symbol. */
  symbol: string
  kind: DrawingKind
  /** Exactly ANCHOR_COUNT[kind] anchors. */
  anchors: Anchor[]
  style: DrawingStyle
}

/** The JSON object stored as the server's drawing `data` (and in the localStorage mirror). */
export interface StoredDrawingData {
  v: 1
  kind: DrawingKind
  anchors: Anchor[]
  style: DrawingStyle
}

/** Changes the chart or the style bar can make to an existing drawing. */
export type DrawingPatch = Partial<Pick<Drawing, 'anchors' | 'style'>>

/** Right-click on the chart canvas (main pane). Coordinates are viewport (client) pixels. */
export interface ChartContextMenuRequest {
  clientX: number
  clientY: number
  /** Price under the cursor on the main series' price scale, or null outside it. */
  price: number | null
  /** Bar-snapped time under the cursor (UNIX seconds), or null. */
  time: number | null
  /** Drawing under the cursor, if any (it is also selected). */
  drawingId: string | null
}

/** An active alert drawn as a dashed price line on the chart. */
export interface AlertLine {
  id: string
  price: number
  /** Short axis label, e.g. "Alert". */
  title: string
}
