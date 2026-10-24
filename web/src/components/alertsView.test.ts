import { describe, expect, it } from 'vitest'
import type { Alert, AlertEvent } from '../data/alertTypes'
import {
  alertState,
  badgeText,
  clampMenuPosition,
  describeAlert,
  eventsNewestFirst,
  formatUtcSeconds,
  formatUtcTime,
  groupAlerts,
  relativeTime,
} from './alertsView'

const alert = (id: string, over: Partial<Alert> = {}): Alert => ({
  id,
  symbol: 'BTCUSDT',
  condition: 'crosses_above',
  price: 65_000,
  frequency: 'once',
  active: true,
  note: '',
  createdAt: 1_700_000_000,
  updatedAt: 1_700_000_000,
  lastTriggeredAt: null,
  ...over,
})

const event = (id: number, triggeredAt: number): AlertEvent => ({
  id,
  alertId: 'a',
  symbol: 'BTCUSDT',
  condition: 'crosses_above',
  level: 65_000,
  price: 65_010,
  triggeredAt,
  message: `event ${id}`,
})

const withCommas = (p: number) =>
  p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

describe('describeAlert', () => {
  it('joins the condition label and the formatted price', () => {
    expect(describeAlert(alert('a'), withCommas)).toBe('Crossing up 65,000.00')
    expect(describeAlert({ condition: 'crosses_below', price: 0.5 }, withCommas)).toBe(
      'Crossing down 0.50',
    )
    expect(describeAlert({ condition: 'greater', price: 10 }, String)).toBe('Greater than 10')
    expect(describeAlert({ condition: 'less', price: 10 }, String)).toBe('Less than 10')
  })
})

describe('groupAlerts', () => {
  it('splits active from stopped and keeps the input order', () => {
    const list = [
      alert('1', { active: false }),
      alert('2'),
      alert('3', { active: false }),
      alert('4'),
    ]
    const g = groupAlerts(list, 'BTCUSDT')
    expect(g.active.map((a) => a.id)).toEqual(['2', '4'])
    expect(g.stopped.map((a) => a.id)).toEqual(['1', '3'])
  })

  it("puts the active symbol's alerts first within each group, stably", () => {
    const list = [
      alert('1', { symbol: 'ETHUSDT' }),
      alert('2'),
      alert('3', { symbol: 'SOLUSDT' }),
      alert('4'),
      alert('5', { symbol: 'ETHUSDT', active: false }),
      alert('6', { active: false }),
    ]
    const g = groupAlerts(list, 'BTCUSDT')
    expect(g.active.map((a) => a.id)).toEqual(['2', '4', '1', '3'])
    expect(g.stopped.map((a) => a.id)).toEqual(['6', '5'])
  })

  it('handles an empty list', () => {
    expect(groupAlerts([], 'BTCUSDT')).toEqual({ active: [], stopped: [] })
  })
})

describe('alertState', () => {
  it('reads active, triggered (fired once), or paused', () => {
    expect(alertState(alert('a'))).toBe('active')
    expect(alertState(alert('a', { active: false, lastTriggeredAt: 1 }))).toBe('triggered')
    expect(alertState(alert('a', { active: false }))).toBe('paused')
    expect(alertState(alert('a', { active: false, frequency: 'every', lastTriggeredAt: 1 }))).toBe(
      'paused',
    )
  })
})

describe('relativeTime', () => {
  const now = 1_700_000_000
  it('reads "just now" under a minute and for future times', () => {
    expect(relativeTime(now, now)).toBe('just now')
    expect(relativeTime(now, now - 59)).toBe('just now')
    expect(relativeTime(now, now + 30)).toBe('just now')
  })

  it('uses whole minutes, hours, then days', () => {
    expect(relativeTime(now, now - 60)).toBe('1 min ago')
    expect(relativeTime(now, now - 5 * 60 - 59)).toBe('5 min ago')
    expect(relativeTime(now, now - 59 * 60)).toBe('59 min ago')
    expect(relativeTime(now, now - 3600)).toBe('1 h ago')
    expect(relativeTime(now, now - 3 * 3600 - 1)).toBe('3 h ago')
    expect(relativeTime(now, now - 23 * 3600 - 3599)).toBe('23 h ago')
    expect(relativeTime(now, now - 86_400)).toBe('1 d ago')
    expect(relativeTime(now, now - 2 * 86_400 - 5)).toBe('2 d ago')
  })
})

describe('UTC formatting', () => {
  it('formats UNIX seconds in UTC', () => {
    // 2023-11-14 22:13:20 UTC
    expect(formatUtcSeconds(1_700_000_000)).toBe('2023-11-14 22:13:20')
    expect(formatUtcTime(1_700_000_000)).toBe('22:13:20')
  })
})

describe('eventsNewestFirst', () => {
  it('sorts by trigger time descending, then id descending, without mutating', () => {
    const input = [event(1, 100), event(3, 300), event(2, 300), event(4, 200)]
    const out = eventsNewestFirst(input)
    expect(out.map((e) => e.id)).toEqual([3, 2, 4, 1])
    expect(input.map((e) => e.id)).toEqual([1, 3, 2, 4])
  })
})

describe('badgeText', () => {
  it('hides zero and caps at 99+', () => {
    expect(badgeText(0)).toBeNull()
    expect(badgeText(-1)).toBeNull()
    expect(badgeText(Number.NaN)).toBeNull()
    expect(badgeText(1)).toBe('1')
    expect(badgeText(99)).toBe('99')
    expect(badgeText(100)).toBe('99+')
  })
})

describe('clampMenuPosition', () => {
  it('keeps a menu that fits at the pointer', () => {
    expect(clampMenuPosition(100, 100, 200, 120, 1000, 800)).toEqual({ x: 100, y: 100 })
  })

  it('flips to the left of / above the pointer when it would overflow', () => {
    expect(clampMenuPosition(900, 750, 200, 120, 1000, 800)).toEqual({ x: 700, y: 630 })
  })

  it('clamps inside the margin when flipping does not fit either', () => {
    // 150 px viewport, 120 px menu at y=100: no room below (100+120>146) or above (100-120<4).
    expect(clampMenuPosition(10, 100, 200, 120, 1000, 150)).toEqual({ x: 10, y: 26 })
    // Menu wider than the viewport pins to the margin.
    expect(clampMenuPosition(50, 10, 400, 50, 300, 800)).toEqual({ x: 4, y: 10 })
    // Negative coordinates clamp to the margin.
    expect(clampMenuPosition(-20, -5, 100, 50, 1000, 800)).toEqual({ x: 4, y: 4 })
  })

  it('honours a custom margin', () => {
    expect(clampMenuPosition(990, 10, 100, 50, 1000, 800, 10)).toEqual({ x: 890, y: 10 })
  })
})
