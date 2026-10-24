import { describe, expect, it } from 'vitest'
import { DEFAULT_DRAWING_STYLE, type DrawingStyle } from '../chart/drawings/types'
import type { Alert, AlertEvent } from '../data/alertTypes'
import {
  DRAWING_STYLE_KEY,
  MAX_TOASTS,
  SIDEBAR_PANEL_KEY,
  alertLinesFor,
  buildHLineDrawing,
  chartAcceptsEscape,
  chartOwnsKeyTarget,
  dismissToast,
  formatPriceByTick,
  loadDrawingStyle,
  loadSidebarPanel,
  priceDecimals,
  pushToast,
  saveDrawingStyle,
  saveSidebarPanel,
  type UiStorage,
} from './chartTools'

function memoryStorage(initial: Record<string, string> = {}): UiStorage & {
  data: Record<string, string>
} {
  const data = { ...initial }
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v
    },
  }
}

const throwingStorage: UiStorage = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
}

const ALERT: Alert = {
  id: 'a1',
  symbol: 'BTCUSDT',
  condition: 'crosses_above',
  price: 65_000,
  frequency: 'once',
  active: true,
  note: '',
  createdAt: 1_700_000_000,
  updatedAt: 1_700_000_000,
  lastTriggeredAt: null,
}

function event(id: number): AlertEvent {
  return {
    id,
    alertId: 'a1',
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    level: 65_000,
    price: 65_012.5,
    triggeredAt: 1_700_000_500 + id,
    message: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
  }
}

describe('priceDecimals', () => {
  it.each([
    [0.01, 2],
    [0.5, 1],
    [1, 0],
    [10, 0],
    [0.00001, 5],
    [0.00000001, 8],
    [0.25, 2],
  ])('tick %d → %i decimals', (tick, decimals) => {
    expect(priceDecimals(tick)).toBe(decimals)
  })

  it('is null for a missing or unusable tick size', () => {
    expect(priceDecimals(undefined)).toBeNull()
    expect(priceDecimals(0)).toBeNull()
    expect(priceDecimals(-0.01)).toBeNull()
    expect(priceDecimals(Number.NaN)).toBeNull()
  })
})

describe('formatPriceByTick', () => {
  it('uses the tick size decimals and groups thousands', () => {
    expect(formatPriceByTick(65_012.3456, 0.01)).toBe('65,012.35')
    expect(formatPriceByTick(1_234_567, 1)).toBe('1,234,567')
    expect(formatPriceByTick(0.123456, 0.0001)).toBe('0.1235')
    expect(formatPriceByTick(999, 0.1)).toBe('999.0')
  })

  it('falls back to magnitude-based precision without a tick size', () => {
    expect(formatPriceByTick(65_012.3456, undefined)).toBe('65,012.35')
    expect(formatPriceByTick(0.05, undefined)).toBe('0.0500')
  })

  it('shows a dash for a non-finite price', () => {
    expect(formatPriceByTick(Number.NaN, 0.01)).toBe('—')
  })
})

describe('buildHLineDrawing', () => {
  const style: DrawingStyle = { color: '#f23645', lineWidth: 3, lineStyle: 'dashed' }
  const base = {
    id: 'd1',
    symbol: 'BTCUSDT',
    price: 64_000,
    style,
    lastCandleTime: 1_700_003_600,
    nowSeconds: 1_700_009_999.7,
  }

  it('anchors at the bar under the cursor with the given style', () => {
    expect(buildHLineDrawing({ ...base, time: 1_700_000_000 })).toEqual({
      id: 'd1',
      symbol: 'BTCUSDT',
      kind: 'hline',
      anchors: [{ time: 1_700_000_000, price: 64_000 }],
      style,
    })
  })

  it('falls back to the last candle time, then to now (whole seconds)', () => {
    expect(buildHLineDrawing({ ...base, time: null })?.anchors[0]?.time).toBe(1_700_003_600)
    expect(buildHLineDrawing({ ...base, time: null, lastCandleTime: null })?.anchors[0]?.time).toBe(
      1_700_009_999,
    )
  })

  it('copies the style so later edits do not alias it', () => {
    const d = buildHLineDrawing({ ...base, time: 1 })
    expect(d?.style).not.toBe(style)
  })

  it('is null for a price an anchor cannot hold', () => {
    expect(buildHLineDrawing({ ...base, time: 1, price: 0 })).toBeNull()
    expect(buildHLineDrawing({ ...base, time: 1, price: -5 })).toBeNull()
    expect(buildHLineDrawing({ ...base, time: 1, price: Number.POSITIVE_INFINITY })).toBeNull()
  })
})

describe('alertLinesFor', () => {
  it('keeps active alerts of the symbol only', () => {
    const alerts: Alert[] = [
      ALERT,
      { ...ALERT, id: 'a2', active: false },
      { ...ALERT, id: 'a3', symbol: 'ETHUSDT' },
      { ...ALERT, id: 'a4', price: 70_000 },
    ]
    expect(alertLinesFor(alerts, 'BTCUSDT')).toEqual([
      { id: 'a1', price: 65_000, title: 'Alert' },
      { id: 'a4', price: 70_000, title: 'Alert' },
    ])
    expect(alertLinesFor(alerts, 'SOLUSDT')).toEqual([])
  })
})

describe('toast queue', () => {
  it('appends and ignores a key already queued', () => {
    const one = pushToast([], { key: '1', event: event(1) })
    const two = pushToast(one, { key: '2', event: event(2) })
    expect(two.map((t) => t.key)).toEqual(['1', '2'])
    expect(pushToast(two, { key: '1', event: event(1) }).map((t) => t.key)).toEqual(['1', '2'])
  })

  it('drops the oldest past the cap', () => {
    let q = pushToast([], { key: '0', event: event(0) })
    for (let i = 1; i <= MAX_TOASTS; i++) q = pushToast(q, { key: String(i), event: event(i) })
    expect(q).toHaveLength(MAX_TOASTS)
    expect(q[0]?.key).toBe('1')
    expect(q[q.length - 1]?.key).toBe(String(MAX_TOASTS))
  })

  it('dismisses by key', () => {
    const q = pushToast(pushToast([], { key: '1', event: event(1) }), {
      key: '2',
      event: event(2),
    })
    expect(dismissToast(q, '1').map((t) => t.key)).toEqual(['2'])
    expect(dismissToast(q, 'nope')).toHaveLength(2)
  })
})

describe('drawing style pref', () => {
  it('round-trips a valid style', () => {
    const s = memoryStorage()
    const style: DrawingStyle = { color: '#089981', lineWidth: 1, lineStyle: 'dotted' }
    saveDrawingStyle(style, s)
    expect(JSON.parse(s.data[DRAWING_STYLE_KEY] ?? 'null')).toEqual(style)
    expect(loadDrawingStyle(DEFAULT_DRAWING_STYLE, s)).toEqual(style)
  })

  it('falls back on missing, invalid, or unreadable storage', () => {
    expect(loadDrawingStyle(DEFAULT_DRAWING_STYLE, memoryStorage())).toEqual(DEFAULT_DRAWING_STYLE)
    expect(
      loadDrawingStyle(
        DEFAULT_DRAWING_STYLE,
        memoryStorage({ [DRAWING_STYLE_KEY]: '{"color":"red","lineWidth":9}' }),
      ),
    ).toEqual(DEFAULT_DRAWING_STYLE)
    expect(
      loadDrawingStyle(DEFAULT_DRAWING_STYLE, memoryStorage({ [DRAWING_STYLE_KEY]: '{oops' })),
    ).toEqual(DEFAULT_DRAWING_STYLE)
    expect(loadDrawingStyle(DEFAULT_DRAWING_STYLE, throwingStorage)).toEqual(DEFAULT_DRAWING_STYLE)
    expect(loadDrawingStyle(DEFAULT_DRAWING_STYLE, null)).toEqual(DEFAULT_DRAWING_STYLE)
  })

  it('never throws when saving to broken storage', () => {
    expect(() => saveDrawingStyle(DEFAULT_DRAWING_STYLE, throwingStorage)).not.toThrow()
  })
})

describe('sidebar panel pref', () => {
  it('round-trips each panel and the collapsed state', () => {
    const s = memoryStorage()
    saveSidebarPanel('alerts', s)
    expect(loadSidebarPanel(s)).toBe('alerts')
    saveSidebarPanel(null, s)
    expect(s.data[SIDEBAR_PANEL_KEY]).toBe('"none"')
    expect(loadSidebarPanel(s)).toBeNull()
    saveSidebarPanel('watchlist', s)
    expect(loadSidebarPanel(s)).toBe('watchlist')
  })

  it('defaults to the watchlist when unset, invalid, or unreadable', () => {
    expect(loadSidebarPanel(memoryStorage())).toBe('watchlist')
    expect(loadSidebarPanel(memoryStorage({ [SIDEBAR_PANEL_KEY]: '"news"' }))).toBe('watchlist')
    expect(loadSidebarPanel(throwingStorage)).toBe('watchlist')
    expect(() => saveSidebarPanel('alerts', throwingStorage)).not.toThrow()
  })
})

describe('chartOwnsKeyTarget', () => {
  // Minimal element tree whose closest() understands the selector forms CHART_KEY_BLOCKERS
  // uses: a tag name, [role="x"], and [contenteditable]:not([contenteditable="false"]).
  interface FakeEl {
    tag: string
    role?: string
    contentEditable?: string
    parent: FakeEl | null
    isContentEditable?: boolean
    closest(selectors: string): FakeEl | null
  }
  function matches(el: FakeEl, selector: string): boolean {
    const role = /^\[role="([^"]+)"\]$/.exec(selector)
    if (role) return el.role === role[1]
    if (selector.startsWith('[contenteditable]')) {
      return el.contentEditable !== undefined && el.contentEditable !== 'false'
    }
    return el.tag === selector
  }
  function el(tag: string, parent: FakeEl | null, attrs: Partial<FakeEl> = {}): FakeEl {
    const node: FakeEl = {
      tag,
      parent,
      ...attrs,
      closest(selectors) {
        const list = selectors.split(',').map((x) => x.trim())
        for (let n: FakeEl | null = node; n !== null; n = n.parent) {
          if (list.some((sel) => matches(n as FakeEl, sel))) return n
        }
        return null
      },
    }
    return node
  }
  const body = el('body', null)
  const chartBox = el('div', body)
  const canvas = el('canvas', chartBox)
  const container = { contains: (other: unknown) => other === chartBox || other === canvas }

  it('accepts body (nothing focused) and targets inside the chart', () => {
    expect(chartOwnsKeyTarget(body, body, container)).toBe(true)
    expect(chartOwnsKeyTarget(canvas, body, container)).toBe(true)
  })

  it('ignores a focused button outside the chart (toolbar, alerts panel)', () => {
    const panel = el('aside', body)
    expect(chartOwnsKeyTarget(el('button', panel), body, container)).toBe(false)
  })

  it('ignores the clear-all confirm (role=alertdialog) and its buttons', () => {
    const confirm = el('div', body, { role: 'alertdialog' })
    expect(chartOwnsKeyTarget(confirm, body, container)).toBe(false)
    expect(chartOwnsKeyTarget(el('button', confirm), body, container)).toBe(false)
  })

  it('ignores dialogs, menus and listboxes, even inside the chart', () => {
    for (const role of ['dialog', 'menu', 'listbox']) {
      const box = el('div', chartBox, { role })
      const inner = el('button', box)
      const inChart = { contains: (o: unknown) => o === box || o === inner || o === chartBox }
      expect(chartOwnsKeyTarget(inner, body, inChart)).toBe(false)
    }
  })

  it('ignores inputs, textareas, selects and editable content', () => {
    for (const tag of ['input', 'textarea', 'select']) {
      expect(chartOwnsKeyTarget(el(tag, body), body, container)).toBe(false)
    }
    expect(chartOwnsKeyTarget(el('div', body, { contentEditable: 'true' }), body, container)).toBe(
      false,
    )
    expect(chartOwnsKeyTarget(el('div', body, { isContentEditable: true }), body, container)).toBe(
      false,
    )
  })

  it('rejects non-element targets and a missing container', () => {
    expect(chartOwnsKeyTarget(null, body, container)).toBe(false)
    expect(chartOwnsKeyTarget({}, body, container)).toBe(false)
    expect(chartOwnsKeyTarget(canvas, body, null)).toBe(false)
  })

  describe('chartAcceptsEscape', () => {
    it('lets Escape through from a focused drawing-rail button outside the chart', () => {
      const rail = el('nav', body)
      expect(chartAcceptsEscape(el('button', rail))).toBe(true)
      expect(chartAcceptsEscape(body)).toBe(true)
      expect(chartAcceptsEscape(canvas)).toBe(true)
    })

    it('keeps Escape for dialogs, menus, listboxes, confirms and editables', () => {
      for (const role of ['dialog', 'alertdialog', 'menu', 'listbox']) {
        expect(chartAcceptsEscape(el('button', el('div', body, { role })))).toBe(false)
      }
      for (const tag of ['input', 'textarea', 'select']) {
        expect(chartAcceptsEscape(el(tag, body))).toBe(false)
      }
      expect(chartAcceptsEscape(el('div', body, { isContentEditable: true }))).toBe(false)
      expect(chartAcceptsEscape(null)).toBe(false)
    })
  })
})
