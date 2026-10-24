import { describe, expect, it } from 'vitest'
import { DrawingController, type ControllerCallbacks } from './controller'
import { barTime, fakeAdapter, yOf } from './testAdapter'
import {
  DEFAULT_DRAWING_STYLE,
  type Drawing,
  type DrawingPatch,
  type DrawingStyle,
  type ToolMode,
} from './types'

// Fake chart: bar i at x = 10 * i, price p at y = (200 - p) * 2.

interface Log {
  created: Drawing[]
  selected: (string | null)[]
  toolDone: number
  changes: { id: string; patch: DrawingPatch }[]
  deleted: string[]
  busy: boolean[]
}

/** What the fake store answers to a create / change (undefined = the old void callbacks). */
interface StoreAnswers {
  create?: boolean
  change?: boolean
}

function setup(
  tool: ToolMode = 'cursor',
  drawings: Drawing[] = [],
  style?: DrawingStyle,
  answers: StoreAnswers = {},
) {
  const log: Log = { created: [], selected: [], toolDone: 0, changes: [], deleted: [], busy: [] }
  let n = 0
  const callbacks: ControllerCallbacks = {
    onCreate: (d) => {
      log.created.push(d)
      return answers.create
    },
    onSelect: (id) => log.selected.push(id),
    onToolDone: () => log.toolDone++,
    onChange: (id, patch) => {
      log.changes.push({ id, patch })
      return answers.change
    },
    onDelete: (id) => log.deleted.push(id),
    onBusyChange: (b) => log.busy.push(b),
  }
  const c = new DrawingController(fakeAdapter(), callbacks, {
    newId: () => `new-${++n}`,
    symbol: 'BTCUSDT',
    tool,
    style,
  })
  c.setDrawings(drawings)
  return { c, log }
}

function make(id: string, kind: Drawing['kind'], anchors: Drawing['anchors']): Drawing {
  return { id, symbol: 'BTCUSDT', kind, anchors, style: DEFAULT_DRAWING_STYLE }
}

const trend = make('t1', 'trendline', [
  { time: barTime(10), price: 150 },
  { time: barTime(20), price: 150 },
])

describe('creating', () => {
  it('creates an hline on a single click with snapped time and free price', () => {
    const { c, log } = setup('hline')
    expect(c.pointerDown({ x: 52, y: yOf(123.25) })).toBe(true)
    expect(c.pointerUp({ x: 52, y: yOf(123.25) })).toBe(true)
    expect(log.created).toEqual([
      {
        id: 'new-1',
        symbol: 'BTCUSDT',
        kind: 'hline',
        anchors: [{ time: barTime(5), price: 123.25 }],
        style: DEFAULT_DRAWING_STYLE,
      },
    ])
    expect(log.selected).toEqual(['new-1'])
    expect(log.toolDone).toBe(1)
    expect(c.busy).toBe(false)
  })

  it('creates an hray on a single click', () => {
    const { c, log } = setup('hray')
    c.pointerDown({ x: 100, y: yOf(150) })
    c.pointerUp({ x: 100, y: yOf(150) })
    expect(log.created[0].kind).toBe('hray')
    expect(log.created[0].anchors).toEqual([{ time: barTime(10), price: 150 }])
  })

  for (const kind of ['trendline', 'fib', 'rectangle'] as const) {
    it(`creates a ${kind} by press-drag-release`, () => {
      const { c, log } = setup(kind)
      expect(c.pointerDown({ x: 100, y: yOf(150) })).toBe(true)
      expect(c.busy).toBe(true)
      expect(c.pointerMove({ x: 151, y: yOf(140) })).toBe(true)
      expect(c.pointerUp({ x: 151, y: yOf(140) })).toBe(true)
      expect(log.created).toHaveLength(1)
      expect(log.created[0].kind).toBe(kind)
      expect(log.created[0].anchors).toEqual([
        { time: barTime(10), price: 150 },
        { time: barTime(15), price: 140 },
      ])
      expect(log.selected).toEqual(['new-1'])
      expect(log.toolDone).toBe(1)
      expect(log.busy).toEqual([true, false])
    })

    it(`creates a ${kind} by click, move, click`, () => {
      const { c, log } = setup(kind)
      c.pointerDown({ x: 100, y: yOf(150) })
      c.pointerUp({ x: 100, y: yOf(150) })
      expect(log.created).toHaveLength(0)
      // Live preview follows the pointer and carries handles.
      c.pointerMove({ x: 200, y: yOf(120) })
      const preview = c.view()
      expect(preview.drawings.at(-1)?.anchors[1]).toEqual({ time: barTime(20), price: 120 })
      expect(preview.selectedId).toBe(preview.drawings.at(-1)?.id)
      expect(c.pointerDown({ x: 210, y: yOf(110) })).toBe(true)
      expect(log.created).toHaveLength(1)
      expect(log.created[0].anchors).toEqual([
        { time: barTime(10), price: 150 },
        { time: barTime(21), price: 110 },
      ])
      // The second click's release is swallowed, then the chart is free again.
      expect(c.busy).toBe(true)
      expect(c.pointerUp({ x: 210, y: yOf(110) })).toBe(true)
      expect(c.busy).toBe(false)
    })
  }

  it('uses the configured style and a copy of it', () => {
    const style: DrawingStyle = { color: '#ff9800', lineWidth: 3, lineStyle: 'dashed' }
    const { c, log } = setup('hline', [], style)
    c.pointerDown({ x: 10, y: 10 })
    expect(log.created[0].style).toEqual(style)
    expect(log.created[0].style).not.toBe(style)
  })

  it('shows a created drawing until the drawings prop catches up', () => {
    const { c, log } = setup('hline')
    c.pointerDown({ x: 10, y: 10 })
    expect(c.view().drawings.map((d) => d.id)).toEqual(['new-1'])
    c.setDrawings([log.created[0]])
    expect(c.view().drawings).toEqual([log.created[0]])
  })

  it('Escape cancels a creation in progress and ends the tool', () => {
    const { c, log } = setup('trendline')
    c.pointerDown({ x: 100, y: 100 })
    c.pointerUp({ x: 100, y: 100 })
    c.pointerMove({ x: 150, y: 120 })
    expect(c.keyDown('Escape')).toBe(true)
    expect(log.created).toHaveLength(0)
    expect(log.toolDone).toBe(1)
    expect(c.busy).toBe(false)
    expect(c.view().drawings).toHaveLength(0)
  })

  it('Escape with a tool but nothing started ends the tool', () => {
    const { c, log } = setup('rectangle')
    expect(c.keyDown('Escape')).toBe(true)
    expect(log.toolDone).toBe(1)
  })

  it('a right-click cancels a creation in progress and asks for no menu', () => {
    const { c, log } = setup('fib')
    c.pointerDown({ x: 100, y: 100 })
    c.pointerUp({ x: 100, y: 100 })
    expect(c.contextMenu({ x: 120, y: 100 })).toBeNull()
    expect(log.created).toHaveLength(0)
    expect(log.toolDone).toBe(1)
  })

  it('switching tool mid-creation drops the preview', () => {
    const { c, log } = setup('trendline')
    c.pointerDown({ x: 100, y: 100 })
    c.pointerUp({ x: 100, y: 100 })
    c.setTool('cursor')
    expect(c.view().drawings).toHaveLength(0)
    expect(c.busy).toBe(false)
    expect(log.created).toHaveLength(0)
  })

  it('ignores non-primary buttons', () => {
    const { c, log } = setup('hline')
    expect(c.pointerDown({ x: 10, y: 10, button: 2 })).toBe(false)
    expect(log.created).toHaveLength(0)
  })

  it('does not consume presses when the chart has no data', () => {
    const c = new DrawingController(fakeAdapter(0), {}, { tool: 'hline' })
    expect(c.pointerDown({ x: 10, y: 10 })).toBe(false)
  })
})

describe('selecting, moving, deleting', () => {
  it('selects a drawing on press without changing it on a plain click', () => {
    const { c, log } = setup('cursor', [trend])
    expect(c.pointerDown({ x: 150, y: yOf(150) + 3 })).toBe(true)
    expect(c.pointerUp({ x: 150, y: yOf(150) + 3 })).toBe(true)
    expect(log.selected).toEqual(['t1'])
    expect(c.selectedId).toBe('t1')
    expect(log.changes).toHaveLength(0)
  })

  it('moves the body with a bar-snapped time delta and a free price delta', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 174, y: yOf(147.5) }) // 2.4 bars → 2
    // Preview is local while dragging.
    expect(c.view().drawings[0].anchors[0]).toEqual({ time: barTime(12), price: 147.5 })
    expect(log.changes).toHaveLength(0)
    c.pointerUp({ x: 176, y: yOf(147.5) }) // 2.6 bars → 3
    expect(log.changes).toEqual([
      {
        id: 't1',
        patch: {
          anchors: [
            { time: barTime(13), price: 147.5 },
            { time: barTime(23), price: 147.5 },
          ],
        },
      },
    ])
    expect(c.busy).toBe(false)
    // Kept on screen until the prop comes back.
    expect(c.view().drawings[0].anchors[0].time).toBe(barTime(13))
  })

  it('drags a handle of the selected drawing to move only that anchor', () => {
    const { c, log } = setup('cursor', [trend])
    c.setSelected('t1')
    expect(c.cursorAt({ x: 200, y: yOf(150) })).toBe('grab')
    c.pointerDown({ x: 200, y: yOf(150) })
    c.pointerMove({ x: 251, y: yOf(170) })
    c.pointerUp({ x: 251, y: yOf(170) })
    expect(log.changes).toEqual([
      {
        id: 't1',
        patch: {
          anchors: [
            { time: barTime(10), price: 150 },
            { time: barTime(25), price: 170 },
          ],
        },
      },
    ])
  })

  it('fires onChange once per drag, on release', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    for (let x = 155; x < 220; x += 5) c.pointerMove({ x, y: yOf(150) })
    expect(log.changes).toHaveLength(0)
    c.pointerUp({ x: 220, y: yOf(150) })
    expect(log.changes).toHaveLength(1)
  })

  it('deselects on an empty press and leaves the press to the chart', () => {
    const { c, log } = setup('cursor', [trend])
    c.setSelected('t1')
    expect(c.pointerDown({ x: 500, y: 20 })).toBe(false)
    expect(log.selected).toEqual([null])
    expect(c.busy).toBe(false)
    expect(c.pointerUp({ x: 500, y: 20 })).toBe(false)
  })

  it('does nothing on an empty press with no selection', () => {
    const { c, log } = setup('cursor', [trend])
    expect(c.pointerDown({ x: 500, y: 20 })).toBe(false)
    expect(c.pointerMove({ x: 520, y: 20 })).toBe(false)
    expect(c.pointerUp({ x: 520, y: 20 })).toBe(false)
    expect(log).toEqual({
      created: [],
      selected: [],
      toolDone: 0,
      changes: [],
      deleted: [],
      busy: [],
    })
  })

  it('Delete removes the selected drawing', () => {
    const { c, log } = setup('cursor', [trend])
    c.setSelected('t1')
    expect(c.keyDown('Delete')).toBe(true)
    expect(log.deleted).toEqual(['t1'])
    expect(c.selectedId).toBeNull()
  })

  it('Backspace also deletes; nothing happens without a selection', () => {
    const { c, log } = setup('cursor', [trend])
    expect(c.keyDown('Backspace')).toBe(false)
    c.setSelected('t1')
    expect(c.keyDown('Backspace')).toBe(true)
    expect(log.deleted).toEqual(['t1'])
  })

  it('Escape deselects', () => {
    const { c, log } = setup('cursor', [trend])
    c.setSelected('t1')
    expect(c.keyDown('Escape')).toBe(true)
    expect(log.selected).toEqual([null])
    expect(c.keyDown('Escape')).toBe(false)
  })

  it('Escape during a drag reverts it without a change', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 200, y: yOf(150) })
    c.keyDown('Escape')
    expect(c.view().drawings[0]).toBe(trend)
    c.pointerUp({ x: 200, y: yOf(150) })
    expect(log.changes).toHaveLength(0)
  })

  it('pointer cancel abandons a drag', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 200, y: yOf(150) })
    c.pointerCancel()
    expect(c.busy).toBe(false)
    expect(c.view().drawings[0]).toBe(trend)
    expect(log.changes).toHaveLength(0)
  })

  it('prefers the closer of two overlapping drawings', () => {
    const near = make('h1', 'hline', [{ time: barTime(0), price: 149 }])
    const { c } = setup('cursor', [trend, near])
    // trendline at y=100, hline at y=102.
    expect(c.hitTest({ x: 150, y: 101.5 })?.id).toBe('h1')
    expect(c.hitTest({ x: 150, y: 100.5 })?.id).toBe('t1')
  })

  it('reports cursors for tool, drawing body and empty space', () => {
    const { c } = setup('cursor', [trend])
    expect(c.cursorAt({ x: 150, y: yOf(150) })).toBe('pointer')
    expect(c.cursorAt({ x: 500, y: 20 })).toBeNull()
    c.setTool('trendline')
    expect(c.cursorAt({ x: 500, y: 20 })).toBe('crosshair')
  })
})

describe('context menu', () => {
  it('returns price, snapped time and the drawing under the cursor, selecting it', () => {
    const { c, log } = setup('cursor', [trend])
    expect(c.contextMenu({ x: 148, y: yOf(150) })).toEqual({
      price: 150,
      time: barTime(15),
      drawingId: 't1',
    })
    expect(log.selected).toEqual(['t1'])
  })

  it('returns no drawing over empty chart', () => {
    const { c } = setup('cursor', [trend])
    expect(c.contextMenu({ x: 300, y: yOf(100) })).toEqual({
      price: 100,
      time: barTime(30),
      drawingId: null,
    })
  })
})

describe('store rejections', () => {
  it('a rejected one-click create leaves no pending drawing and no selection', () => {
    const { c, log } = setup('hline', [], undefined, { create: false })
    expect(c.pointerDown({ x: 52, y: yOf(120) })).toBe(true)
    c.pointerUp({ x: 52, y: yOf(120) })
    expect(log.created).toHaveLength(1)
    expect(c.view().drawings).toEqual([])
    expect(c.view().selectedId).toBeNull()
    expect(c.selectedId).toBeNull()
    expect(log.selected).toEqual([])
    // The tool still finishes.
    expect(log.toolDone).toBe(1)
    expect(c.busy).toBe(false)
  })

  it('a rejected two-anchor create drops the drawing and keeps the previous selection', () => {
    const { c, log } = setup('trendline', [trend], undefined, { create: false })
    c.setSelected('t1')
    c.pointerDown({ x: 300, y: yOf(150) })
    c.pointerMove({ x: 400, y: yOf(140) })
    c.pointerUp({ x: 400, y: yOf(140) })
    expect(log.created).toHaveLength(1)
    expect(c.view().drawings).toEqual([trend])
    expect(c.selectedId).toBe('t1')
    expect(log.selected).toEqual([])
    expect(log.toolDone).toBe(1)
  })

  it('an accepted create (true) behaves like the void callback', () => {
    const { c, log } = setup('hline', [], undefined, { create: true })
    c.pointerDown({ x: 52, y: yOf(120) })
    expect(c.view().drawings.map((d) => d.id)).toEqual(['new-1'])
    expect(log.selected).toEqual(['new-1'])
  })

  it('a rejected move reverts to the committed drawing at once', () => {
    const { c, log } = setup('cursor', [trend], undefined, { change: false })
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 200, y: yOf(140) })
    c.pointerUp({ x: 200, y: yOf(140) })
    expect(log.changes).toHaveLength(1)
    expect(c.view().drawings[0]).toBe(trend)
    // The selection still points at the (unchanged) drawing.
    expect(c.selectedId).toBe('t1')
  })

  it('an accepted move (true) stays on screen until the prop catches up', () => {
    const { c } = setup('cursor', [trend], undefined, { change: true })
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 200, y: yOf(140) })
    c.pointerUp({ x: 200, y: yOf(140) })
    expect(c.view().drawings[0].anchors[0]).toEqual({ time: barTime(15), price: 140 })
  })
})

describe('prices stay positive', () => {
  it('a body drag below price 0 keeps the last valid position', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 150, y: yOf(100) }) // -50: valid
    expect(c.view().drawings[0].anchors[0].price).toBe(100)
    c.pointerMove({ x: 150, y: yOf(-10) }) // -160: would put both anchors at -10
    expect(c.view().drawings[0].anchors.map((a) => a.price)).toEqual([100, 100])
    c.pointerUp({ x: 150, y: yOf(-10) })
    expect(log.changes).toHaveLength(1)
    const anchors = log.changes[0].patch.anchors ?? []
    expect(anchors.map((a) => a.price)).toEqual([100, 100])
    expect(anchors.every((a) => a.price > 0)).toBe(true)
  })

  it('a move landing exactly on price 0 is refused', () => {
    const { c, log } = setup('cursor', [trend])
    c.pointerDown({ x: 150, y: yOf(150) })
    c.pointerMove({ x: 150, y: yOf(0) })
    c.pointerUp({ x: 150, y: yOf(0) })
    // No valid position was ever reached, so nothing changed.
    expect(log.changes).toHaveLength(0)
    expect(c.view().drawings[0]).toBe(trend)
  })

  it('a handle dragged below price 0 keeps its last valid price', () => {
    const { c, log } = setup('cursor', [trend])
    c.setSelected('t1')
    c.pointerDown({ x: 200, y: yOf(150) })
    c.pointerMove({ x: 250, y: yOf(20) })
    c.pointerMove({ x: 260, y: yOf(-5) })
    c.pointerUp({ x: 260, y: yOf(-5) })
    expect(log.changes).toEqual([
      {
        id: 't1',
        patch: {
          anchors: [
            { time: barTime(10), price: 150 },
            { time: barTime(25), price: 20 },
          ],
        },
      },
    ])
  })

  it('a creation preview never takes its second anchor below price 0', () => {
    const { c, log } = setup('trendline')
    c.pointerDown({ x: 100, y: yOf(50) })
    c.pointerMove({ x: 150, y: yOf(10) })
    c.pointerMove({ x: 200, y: yOf(-20) })
    expect(c.view().drawings.at(-1)?.anchors[1]).toEqual({ time: barTime(15), price: 10 })
    c.pointerUp({ x: 200, y: yOf(-20) })
    expect(log.created[0].anchors).toEqual([
      { time: barTime(10), price: 50 },
      { time: barTime(15), price: 10 },
    ])
  })
})
