// draw() is canvas calls only: a repeat paint (the crosshair moving, no updateAllViews in
// between) does no colour, label or number formatting, allocates no dash arrays and computes no
// handle points. Formatting happens in updateAllViews, and only when its inputs changed.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./geometry', async (importOriginal) => {
  const real = await importOriginal<typeof import('./geometry')>()
  return {
    ...real,
    withAlpha: vi.fn(real.withAlpha),
    fibLabel: vi.fn(real.fibLabel),
    formatLevelPrice: vi.fn(real.formatLevelPrice),
    handlePoints: vi.fn(real.handlePoints),
    normalizeRect: vi.fn(real.normalizeRect),
    contrastText: vi.fn(real.contrastText),
  }
})
vi.mock('../theme', async (importOriginal) => {
  const real = await importOriginal<typeof import('../theme')>()
  return { ...real, handleStroke: vi.fn(real.handleStroke) }
})

import * as geometry from './geometry'
import * as theme from '../theme'
import { PIXEL_RATIOS, SCENES, paint, recordingCanvas, sceneSetup } from './recordingCanvas'
import { movableAdapter } from './testAdapter'

const helpers = () => ({
  withAlpha: vi.mocked(geometry.withAlpha),
  fibLabel: vi.mocked(geometry.fibLabel),
  formatLevelPrice: vi.mocked(geometry.formatLevelPrice),
  handlePoints: vi.mocked(geometry.handlePoints),
  normalizeRect: vi.mocked(geometry.normalizeRect),
  contrastText: vi.mocked(geometry.contrastText),
  handleStroke: vi.mocked(theme.handleStroke),
})

function totalCalls(): Record<string, number> {
  return Object.fromEntries(Object.entries(helpers()).map(([k, f]) => [k, f.mock.calls.length]))
}

const zero = () => Object.fromEntries(Object.keys(helpers()).map((k) => [k, 0]))

describe('drawings paint path', () => {
  let toLocale: ReturnType<typeof vi.spyOn>
  let numberFormat: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    for (const f of Object.values(helpers())) f.mockClear()
    toLocale = vi.spyOn(Number.prototype, 'toLocaleString')
    numberFormat = vi.spyOn(Intl, 'NumberFormat')
  })
  afterEach(() => vi.restoreAllMocks())

  it('the first frame does its formatting before draw, in updateAllViews', () => {
    const scene = SCENES.find((s) => s.selected === 'x1' && s.hovered === 'f1')!
    sceneSetup(scene)
    const c = totalCalls()
    // Three fibs x seven labels, rectangle + fib fills, handle points of the selection + hover.
    expect(c.fibLabel).toBe(21)
    expect(c.withAlpha).toBeGreaterThan(0)
    expect(c.handlePoints).toBe(2)
    expect(toLocale).toHaveBeenCalled()
  })

  it('a repeat draw formats nothing, calls no helper and reuses the same dash arrays', () => {
    for (const scene of SCENES) {
      const { primitive } = sceneSetup(scene)
      for (const [hr, vr] of PIXEL_RATIOS) {
        const first = recordingCanvas()
        paint(primitive, first, hr, vr)
        for (const f of Object.values(helpers())) f.mockClear()
        toLocale.mockClear()
        numberFormat.mockClear()

        const again = recordingCanvas()
        paint(primitive, again, hr, vr)
        expect(totalCalls(), scene.name).toEqual(zero())
        expect(toLocale).not.toHaveBeenCalled()
        expect(numberFormat).not.toHaveBeenCalled()
        // Same array objects passed to setLineDash: nothing was allocated for dashes.
        expect(again.dashArgs.length).toBe(first.dashArgs.length)
        again.dashArgs.forEach((a, i) => expect(a).toBe(first.dashArgs[i]))
        // And every string set on the context is the very same string as last frame.
        const strings = (ops: readonly (readonly unknown[])[]) =>
          ops.flatMap((op) => op.filter((v) => typeof v === 'string'))
        expect(strings(again.ops)).toEqual(strings(first.ops))
      }
    }
  })

  it('an updateAllViews with nothing changed rebuilds no labels, fills or handle points', () => {
    const scene = SCENES.find((s) => s.selected === 'f3')!
    const { primitive } = sceneSetup(scene)
    for (const f of Object.values(helpers())) f.mockClear()
    toLocale.mockClear()
    primitive.updateAllViews()
    const c = totalCalls()
    expect(c.fibLabel + c.withAlpha + c.handlePoints + c.normalizeRect).toBe(0)
    expect(toLocale).not.toHaveBeenCalled()
  })

  it('a scroll re-projects but keeps the fib labels and colours (they do not depend on the view)', () => {
    const scene = SCENES.find((s) => s.selected === 'f3')!
    const m = movableAdapter()
    const { primitive } = sceneSetup(scene, m.adapter)
    const before = recordingCanvas()
    paint(primitive, before, 1)
    m.view.offset = 4.5
    m.view.spacing = 7
    for (const f of Object.values(helpers())) f.mockClear()
    toLocale.mockClear()
    primitive.updateAllViews()
    const c = totalCalls()
    expect(c.fibLabel).toBe(0)
    expect(c.withAlpha).toBe(0)
    expect(toLocale).not.toHaveBeenCalled()
    // Only the handle points move: the selected fib's and the hovered rectangle's.
    expect(c.handlePoints).toBe(2)
    const after = recordingCanvas()
    paint(primitive, after, 1)
    expect(after.ops).not.toEqual(before.ops)
  })

  it('the axis label text colour is computed once per colour, not per axis paint', () => {
    const { primitive } = sceneSetup(SCENES[0])
    const views = primitive.priceAxisViews()
    expect(views.length).toBe(3)
    vi.mocked(geometry.contrastText).mockClear()
    const first = views.map((v) => v.textColor())
    for (let k = 0; k < 5; k++) expect(views.map((v) => v.textColor())).toEqual(first)
    expect(vi.mocked(geometry.contrastText).mock.calls.length).toBe(views.length)
  })
})
