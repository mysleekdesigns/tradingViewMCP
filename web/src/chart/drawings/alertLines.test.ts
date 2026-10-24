import type { CreatePriceLineOptions, IPriceLine, PriceLineOptions } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { AlertLinesManager, diffAlertLines, type PriceLineHost } from './alertLines'
import type { AlertLine } from './types'

const a = (id: string, price: number, title = 'Alert'): AlertLine => ({ id, price, title })

describe('diffAlertLines', () => {
  it('adds new ids, updates changed prices or titles, removes missing ids', () => {
    const prev = new Map([
      ['1', a('1', 100)],
      ['2', a('2', 200)],
      ['3', a('3', 300)],
    ])
    const diff = diffAlertLines(prev, [a('1', 100), a('2', 250), a('3', 300, 'Above'), a('4', 400)])
    expect(diff.add.map((l) => l.id)).toEqual(['4'])
    expect(diff.update.map((l) => l.id)).toEqual(['2', '3'])
    expect(diff.remove).toEqual([])
    expect(diffAlertLines(prev, [a('2', 200)]).remove).toEqual(['1', '3'])
  })

  it('ignores duplicate ids in the input', () => {
    expect(diffAlertLines(new Map(), [a('1', 1), a('1', 2)]).add).toHaveLength(1)
  })
})

class FakeLine {
  options: Partial<PriceLineOptions>
  constructor(options: CreatePriceLineOptions) {
    this.options = { ...options }
  }
  applyOptions(o: Partial<PriceLineOptions>) {
    this.options = { ...this.options, ...o }
  }
}

function fakeHost() {
  const lines = new Set<FakeLine>()
  const host: PriceLineHost = {
    createPriceLine: (o) => {
      const line = new FakeLine(o)
      lines.add(line)
      return line as unknown as IPriceLine
    },
    removePriceLine: (l) => {
      lines.delete(l as unknown as FakeLine)
    },
  }
  return { host, lines }
}

const opts = (price: number, title: string): CreatePriceLineOptions => ({ price, title })

describe('AlertLinesManager', () => {
  it('creates, moves and removes price lines by id', () => {
    const m = new AlertLinesManager(opts)
    const { host, lines } = fakeHost()
    m.attach(host)
    m.sync([a('1', 100), a('2', 200)])
    expect(lines.size).toBe(2)
    m.sync([a('1', 110)])
    expect(lines.size).toBe(1)
    expect([...lines][0].options.price).toBe(110)
    expect(m.ids()).toEqual(['1'])
  })

  it('re-creates the lines on a new series and removes them from the old one', () => {
    const m = new AlertLinesManager(opts)
    const first = fakeHost()
    m.attach(first.host)
    m.sync([a('1', 100)])
    const second = fakeHost()
    m.attach(second.host)
    expect(first.lines.size).toBe(0)
    expect(second.lines.size).toBe(1)
  })

  it('keeps alerts synced before a series exists and draws them on attach', () => {
    const m = new AlertLinesManager(opts)
    m.sync([a('1', 100)])
    const { host, lines } = fakeHost()
    m.attach(host)
    expect(lines.size).toBe(1)
  })

  it('detach without removal forgets lines of a chart that is already gone', () => {
    const m = new AlertLinesManager(opts)
    const { host, lines } = fakeHost()
    m.attach(host)
    m.sync([a('1', 100)])
    m.detach(false)
    expect(lines.size).toBe(1)
    expect(m.ids()).toEqual([])
  })
})
