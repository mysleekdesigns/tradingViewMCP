// Pure helpers for the alerts panel, toasts, sidebar rail, and chart context menu.

import { ALERT_CONDITION_LABELS, type Alert, type AlertEvent } from '../data/alertTypes'

/** "Crossing up 65,000.00" */
export function describeAlert(
  alert: Pick<Alert, 'condition' | 'price'>,
  formatPrice: (price: number) => string,
): string {
  return `${ALERT_CONDITION_LABELS[alert.condition]} ${formatPrice(alert.price)}`
}

export interface GroupedAlerts {
  active: Alert[]
  stopped: Alert[]
}

/**
 * Active alerts first, then stopped (paused or already triggered once). Within each group the
 * active symbol's alerts come first; otherwise the input order is kept.
 */
export function groupAlerts(alerts: readonly Alert[], activeSymbol: string): GroupedAlerts {
  const bySymbolFirst = (list: Alert[]) => [
    ...list.filter((a) => a.symbol === activeSymbol),
    ...list.filter((a) => a.symbol !== activeSymbol),
  ]
  return {
    active: bySymbolFirst(alerts.filter((a) => a.active)),
    stopped: bySymbolFirst(alerts.filter((a) => !a.active)),
  }
}

export type AlertState = 'active' | 'paused' | 'triggered'

/** A stopped `once` alert that has fired reads "Triggered"; any other stopped alert is paused. */
export function alertState(
  alert: Pick<Alert, 'active' | 'frequency' | 'lastTriggeredAt'>,
): AlertState {
  if (alert.active) return 'active'
  return alert.frequency === 'once' && alert.lastTriggeredAt !== null ? 'triggered' : 'paused'
}

export const ALERT_STATE_LABELS: Readonly<Record<AlertState, string>> = {
  active: 'Active',
  paused: 'Paused',
  triggered: 'Triggered',
}

export const FREQUENCY_LABELS = { once: 'Only once', every: 'Every time' } as const

/** "just now", "5 min ago", "3 h ago", "2 d ago". Times are UNIX seconds; future reads "just now". */
export function relativeTime(nowSec: number, thenSec: number): string {
  const diff = Math.floor(nowSec - thenSec)
  if (!Number.isFinite(diff) || diff < 60) return 'just now'
  const min = Math.floor(diff / 60)
  if (min < 60) return `${min} min ago`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} h ago`
  return `${Math.floor(h / 24)} d ago`
}

/** "2026-10-24 14:08:31" in UTC. */
export function formatUtcSeconds(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(0, 19).replace('T', ' ')
}

/** "14:08:31" in UTC. */
export function formatUtcTime(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(11, 19)
}

/** Newest first; ties broken by the higher (later) event id. Returns a new array. */
export function eventsNewestFirst(events: readonly AlertEvent[]): AlertEvent[] {
  return [...events].sort((a, b) => b.triggeredAt - a.triggeredAt || b.id - a.id)
}

/** Badge text for a count: null for none, "99+" above 99. */
export function badgeText(count: number): string | null {
  if (!Number.isFinite(count) || count <= 0) return null
  return count > 99 ? '99+' : String(Math.floor(count))
}

/**
 * Top-left corner for a `w`×`h` menu opened at (x, y) so it stays inside the viewport: it
 * flips to the other side of the pointer when it would overflow, then clamps to `margin`.
 */
export function clampMenuPosition(
  x: number,
  y: number,
  w: number,
  h: number,
  viewportW: number,
  viewportH: number,
  margin = 4,
): { x: number; y: number } {
  const place = (p: number, size: number, viewport: number) => {
    let v = p
    if (v + size > viewport - margin && p - size >= margin) v = p - size
    v = Math.min(v, viewport - margin - size)
    return Math.max(margin, v)
  }
  return { x: place(x, w, viewportW), y: place(y, h, viewportH) }
}
