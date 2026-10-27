import { describe, expect, it } from 'vitest'
import type { DerivativesPoint } from '../data/labTypes'
import {
  DERIVATIVES_PANE_STRETCH,
  DerivativesPanes,
  derivativesColorsFor,
  derivativesValueAt,
  formatCompactUsd,
  formatFundingPct,
  fundingHistogramData,
  hasAnyValue,
  openInterestLineData,
  type DerivativesChart,
  type DerivativesColors,
  type DerivativesHistogram,
  type DerivativesLine,
  type DerivativesOverlay,
} from './derivativesPanes'
import { paletteFor } from './theme'

const H = 3600
const T0 = 1_704_067_200

const COLORS: DerivativesColors = {
  positive: '#00ff00cc',
  negative: '#ff0000cc',
  line: '#2962ff',
  zero: '#78787899',
}

describe('fundingHistogramData', () => {
  const points: DerivativesPoint[] = [
    { time: T0, value: 0.0001 },
    { time: T0 + H, value: -0.00025 },
    { time: T0 + 2 * H, value: null },
    { time: T0 + 3 * H, value: 0 },
  ]

  it('plots percent (value × 100), green at or above zero, red below, null as whitespace', () => {
    const data = fundingHistogramData(points, COLORS)
    expect(data).toHaveLength(4)
    expect(data[0]).toMatchObject({ time: T0, color: COLORS.positive })
    expect((data[0] as { value: number }).value).toBeCloseTo(0.01, 12)
    expect(data[1]).toMatchObject({ time: T0 + H, color: COLORS.negative })
    expect((data[1] as { value: number }).value).toBeCloseTo(-0.025, 12)
    // Whitespace: a time with no value (never interpolated from its neighbours).
    expect(data[2]).toEqual({ time: T0 + 2 * H })
    expect(data[3]).toEqual({ time: T0 + 3 * H, value: 0, color: COLORS.positive })
  })

  it('drops points on bars the chart does not plot', () => {
    const plotted = new Set([T0, T0 + 3 * H])
    expect(fundingHistogramData(points, COLORS, plotted).map((d) => d.time)).toEqual([
      T0,
      T0 + 3 * H,
    ])
  })
})

describe('openInterestLineData', () => {
  it('keeps USD values, null as whitespace, plotted bars only', () => {
    const points: DerivativesPoint[] = [
      { time: T0, value: 3_131_493_738.9 },
      { time: T0 + H, value: null },
      { time: T0 + 2 * H, value: 3.2e9 },
    ]
    expect(openInterestLineData(points)).toEqual([
      { time: T0, value: 3_131_493_738.9 },
      { time: T0 + H },
      { time: T0 + 2 * H, value: 3.2e9 },
    ])
    expect(openInterestLineData(points, new Set([T0 + H]))).toEqual([{ time: T0 + H }])
  })
})

describe('formatting', () => {
  it('shows funding in percent with 4 decimals', () => {
    expect(formatFundingPct(0.00037409)).toBe('0.0374%')
    expect(formatFundingPct(0.0001)).toBe('0.0100%')
    expect(formatFundingPct(-0.00025)).toBe('-0.0250%')
  })

  it('shows open interest as compact USD', () => {
    expect(formatCompactUsd(3_131_493_738.9)).toBe('3.13B')
    expect(formatCompactUsd(12_500_000)).toBe('12.50M')
    expect(formatCompactUsd(1.5e12)).toBe('1.50T')
    expect(formatCompactUsd(4_200)).toBe('4.20K')
    expect(formatCompactUsd(999_999)).toBe('1.00M')
    expect(formatCompactUsd(950)).toBe('950')
    expect(formatCompactUsd(Number.NaN)).toBe('—')
  })
})

describe('derivativesValueAt (legend)', () => {
  const points: DerivativesPoint[] = [
    { time: T0, value: 1 },
    { time: T0 + H, value: null },
    { time: T0 + 2 * H, value: 3 },
  ]

  it('reads the hovered bar exactly, null for a gap or a bar without a point', () => {
    expect(derivativesValueAt(points, T0, undefined)).toBe(1)
    expect(derivativesValueAt(points, T0 + H, undefined)).toBeNull()
    expect(derivativesValueAt(points, T0 + 3 * H, undefined)).toBeNull()
    expect(derivativesValueAt(points, T0 - H, undefined)).toBeNull()
  })

  it('without hover, reads the newest point at or before the last plotted bar (replay-safe)', () => {
    // Bar replay at T0 + H: the later value (3) must not show.
    expect(derivativesValueAt(points, undefined, T0 + H)).toBeNull()
    expect(derivativesValueAt(points, undefined, T0 + 30 * 60)).toBe(1)
    // Live: the forming bar (no point) falls back to the newest closed bar.
    expect(derivativesValueAt(points, undefined, T0 + 3 * H)).toBe(3)
    expect(derivativesValueAt(points, undefined, undefined)).toBeNull()
    expect(derivativesValueAt([], undefined, T0)).toBeNull()
  })

  it('knows when a series has no value at all', () => {
    expect(hasAnyValue(points)).toBe(true)
    expect(hasAnyValue([{ time: T0, value: null }])).toBe(false)
    expect(hasAnyValue([])).toBe(false)
  })
})

describe('derivativesColorsFor', () => {
  it('follows the candle colours and the theme line colour', () => {
    const dark = paletteFor({
      theme: 'dark',
      upColor: '#26a69a',
      downColor: '#ef5350',
      timezone: 'UTC',
    })
    const c = derivativesColorsFor(dark)
    expect(c.positive.startsWith('#26a69a')).toBe(true)
    expect(c.negative.startsWith('#ef5350')).toBe(true)
    expect(c.line).toBe(dark.line)
    const custom = derivativesColorsFor({ ...dark, up: '#112233', down: '#445566' })
    expect(custom.positive.startsWith('#112233')).toBe(true)
    expect(custom.negative.startsWith('#445566')).toBe(true)
  })
})

// ---------------------------------------------------------------------------------------------
// Pane manager against a fake chart that models LWC panes (empty panes are dropped)
// ---------------------------------------------------------------------------------------------

interface FakePane {
  stretch: number
}

class FakeChart implements DerivativesChart {
  /** Pane 0 (price) plus any others; each holds series ids. */
  panes: { pane: FakePane; series: Set<number> }[] = [{ pane: { stretch: 1 }, series: new Set() }]
  private nextId = 1
  readonly data = new Map<number, unknown[]>()
  readonly options = new Map<number, Record<string, unknown>>()
  readonly priceLines: { price: number; color?: string }[] = []

  /** An indicator / equity pane occupying the next index. */
  addOtherPane(): number {
    const id = this.nextId++
    this.panes.push({ pane: { stretch: 0.3 }, series: new Set([id]) })
    return id
  }

  removeOther(id: number): void {
    this.removeById(id)
  }

  paneCount(): number {
    return this.panes.length
  }

  private paneIndexOf(id: number): number {
    return this.panes.findIndex((p) => p.series.has(id))
  }

  private place(paneIndex: number): number {
    const id = this.nextId++
    while (this.panes.length <= paneIndex)
      this.panes.push({ pane: { stretch: 1 }, series: new Set() })
    this.panes[paneIndex].series.add(id)
    return id
  }

  private seriesApi(id: number) {
    const getPane = () => {
      const entry = this.panes[this.paneIndexOf(id)]
      return {
        paneIndex: () => this.paneIndexOf(id),
        getStretchFactor: () => entry.pane.stretch,
        setStretchFactor: (f: number) => {
          entry.pane.stretch = f
        },
      }
    }
    return {
      id,
      setData: (d: unknown[]) => {
        this.data.set(id, d)
      },
      getPane,
    }
  }

  addHistogram(options: object, paneIndex: number): DerivativesHistogram {
    const id = this.place(paneIndex)
    this.options.set(id, { ...options })
    const lines = this.priceLines
    return {
      ...this.seriesApi(id),
      createPriceLine: (o) => {
        const line = { price: o.price, color: o.color }
        lines.push(line)
        return {
          applyOptions: (p) => {
            if (p.color !== undefined) line.color = p.color
          },
        }
      },
    } as DerivativesHistogram
  }

  addLine(options: object, paneIndex: number): DerivativesLine {
    const id = this.place(paneIndex)
    this.options.set(id, { ...options })
    return {
      ...this.seriesApi(id),
      applyOptions: (o: object) => {
        this.options.set(id, { ...this.options.get(id), ...o })
      },
    } as DerivativesLine
  }

  removeSeries(series: DerivativesHistogram | DerivativesLine): void {
    this.removeById((series as unknown as { id: number }).id)
  }

  private removeById(id: number): void {
    const i = this.paneIndexOf(id)
    if (i < 0) throw new Error(`series ${id} is not on the chart`)
    this.panes[i].series.delete(id)
    // LWC drops an emptied pane (never the price pane).
    if (i > 0 && this.panes[i].series.size === 0) this.panes.splice(i, 1)
    this.data.delete(id)
  }

  seriesCount(): number {
    return this.panes.reduce((n, p) => n + p.series.size, 0)
  }
}

const OVERLAY: DerivativesOverlay = {
  funding: [
    { time: T0, value: 0.0001 },
    { time: T0 + H, value: -0.0002 },
    { time: T0 + 2 * H, value: null },
  ],
  openInterest: [
    { time: T0, value: 3e9 },
    { time: T0 + H, value: null },
    { time: T0 + 2 * H, value: 3.1e9 },
  ],
}

describe('DerivativesPanes', () => {
  it('adds a funding pane and an open-interest pane after the existing panes', () => {
    const chart = new FakeChart()
    chart.addOtherPane() // an indicator pane at 1
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    expect(panes.shown()).toBe(false)
    expect(chart.paneCount()).toBe(2)

    panes.setData(OVERLAY)
    expect(panes.shown()).toBe(true)
    expect(panes.paneIndices()).toEqual({ funding: 2, openInterest: 3 })
    expect(chart.paneCount()).toBe(4)
    expect(chart.panes[2].pane.stretch).toBe(DERIVATIVES_PANE_STRETCH)
    // A dotted zero line in the funding pane.
    expect(chart.priceLines).toEqual([{ price: 0, color: COLORS.zero }])
  })

  it('removes both panes and every series when the data becomes null', () => {
    const chart = new FakeChart()
    const indicator = chart.addOtherPane()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    panes.setData(OVERLAY)
    panes.setData(null)
    expect(panes.shown()).toBe(false)
    expect(panes.paneIndices()).toBeNull()
    expect(chart.paneCount()).toBe(2)
    expect(chart.seriesCount()).toBe(1)
    // The indicator pane is untouched.
    chart.removeOther(indicator)
    expect(chart.paneCount()).toBe(1)
  })

  it('draws only plotted bars and re-uses the same series on new data', () => {
    const chart = new FakeChart()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    panes.setData(OVERLAY, new Set([T0, T0 + 2 * H]))
    const [fundingId] = [...chart.panes[1].series]
    expect((chart.data.get(fundingId) ?? []).map((d) => (d as { time: number }).time)).toEqual([
      T0,
      T0 + 2 * H,
    ])
    panes.setData(OVERLAY)
    expect([...chart.panes[1].series]).toEqual([fundingId])
    expect(chart.data.get(fundingId)).toHaveLength(3)
  })

  it('steps out while indicator panes change and comes back after them, sizes kept', () => {
    const chart = new FakeChart()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    panes.setData(OVERLAY)
    expect(panes.paneIndices()).toEqual({ funding: 1, openInterest: 2 })
    chart.panes[1].pane.stretch = 0.5 // the user dragged the funding pane taller

    // An indicator pane is added: without suspend it would land on pane 1 (the funding pane).
    panes.suspend()
    expect(chart.paneCount()).toBe(1)
    chart.addOtherPane()
    panes.setData(OVERLAY) // data arriving while suspended is kept, not drawn
    expect(panes.shown()).toBe(false)
    panes.resume()

    expect(panes.paneIndices()).toEqual({ funding: 2, openInterest: 3 })
    expect(chart.panes[2].pane.stretch).toBe(0.5)
    expect(chart.panes[3].pane.stretch).toBe(DERIVATIVES_PANE_STRETCH)
    expect(chart.panes.every((p, i) => i === 0 || p.series.size === 1)).toBe(true)
  })

  it('is not created while suspended and reports when a setData would create it', () => {
    const panes = new DerivativesPanes(COLORS)
    expect(panes.wouldCreate()).toBe(false) // no chart yet
    const chart = new FakeChart()
    panes.attachChart(chart)
    expect(panes.wouldCreate()).toBe(true)
    panes.setData(OVERLAY)
    expect(panes.wouldCreate()).toBe(false)
  })

  it('recolours the funding bars, zero line and OI line on a theme change', () => {
    const chart = new FakeChart()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    panes.setData(OVERLAY)
    const next = { positive: '#0000ffcc', negative: '#ffff00cc', line: '#abcdef', zero: '#123456' }
    panes.setColors(next)
    const [fundingId] = [...chart.panes[1].series]
    const [oiId] = [...chart.panes[2].series]
    const funding = chart.data.get(fundingId) as { color?: string }[]
    expect(funding[0].color).toBe(next.positive)
    expect(funding[1].color).toBe(next.negative)
    expect(chart.priceLines[0].color).toBe(next.zero)
    expect(chart.options.get(oiId)).toMatchObject({ color: '#abcdef' })
  })

  it('leaves nothing behind on detach and redraws on a new chart', () => {
    const chart = new FakeChart()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    panes.setData(OVERLAY)
    panes.detachChart(true)
    expect(chart.seriesCount()).toBe(0)
    expect(chart.paneCount()).toBe(1)
    const other = new FakeChart()
    panes.attachChart(other)
    expect(panes.paneIndices()).toEqual({ funding: 1, openInterest: 2 })
  })

  it('notifies subscribers when panes come and go (legend positions)', () => {
    const chart = new FakeChart()
    const panes = new DerivativesPanes(COLORS)
    panes.attachChart(chart)
    let calls = 0
    const unsubscribe = panes.subscribe(() => {
      calls += 1
    })
    const before = panes.getSnapshot()
    panes.setData(OVERLAY)
    panes.setData(null)
    expect(calls).toBe(2)
    expect(panes.getSnapshot()).toBe(before + 2)
    unsubscribe()
  })
})
