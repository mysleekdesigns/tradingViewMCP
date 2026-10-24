// Alert shapes shared by the web client and mirrored by server/src/alerts/types.ts.
// Contract: server/README.md "Alerts". All times are UNIX seconds UTC.

export type AlertCondition = 'crosses_above' | 'crosses_below' | 'greater' | 'less'

/** `once`: deactivate after the first trigger. `every`: re-arm once the condition is false again. */
export type AlertFrequency = 'once' | 'every'

export const ALERT_CONDITIONS: readonly AlertCondition[] = [
  'crosses_above',
  'crosses_below',
  'greater',
  'less',
]

export const ALERT_CONDITION_LABELS: Readonly<Record<AlertCondition, string>> = {
  crosses_above: 'Crossing up',
  crosses_below: 'Crossing down',
  greater: 'Greater than',
  less: 'Less than',
}

export interface Alert {
  /** Server-generated, `^[A-Za-z0-9_-]{1,64}$`. */
  id: string
  symbol: string
  condition: AlertCondition
  /** Threshold price, finite and > 0. */
  price: number
  frequency: AlertFrequency
  /** False once a `once` alert fired, or when the user paused it. */
  active: boolean
  /** Optional user note, at most 200 characters ('' when none). */
  note: string
  createdAt: number
  updatedAt: number
  lastTriggeredAt: number | null
}

/** Body of `POST /api/alerts`. */
export interface NewAlert {
  symbol: string
  condition: AlertCondition
  price: number
  frequency: AlertFrequency
  note?: string
}

/** Body of `PATCH /api/alerts/:id`. Setting `active: true` re-arms the alert. */
export type AlertPatch = Partial<
  Pick<Alert, 'condition' | 'price' | 'frequency' | 'active' | 'note'>
>

/** One trigger, kept in history even after its alert is deleted. */
export interface AlertEvent {
  id: number
  alertId: string
  symbol: string
  condition: AlertCondition
  /** The alert's threshold at trigger time. */
  level: number
  /** Last traded price that triggered it. */
  price: number
  triggeredAt: number
  /** Human-readable, e.g. "BTCUSDT crossing up 65,000 (last 65,012.5)". */
  message: string
}

/** Server → client messages on `GET /ws/alerts`. */
export type AlertStreamMessage =
  | { type: 'triggered'; event: AlertEvent; alert: Alert }
  /** Alerts were created/updated/deleted or history cleared (any tab): refetch. */
  | { type: 'changed' }
  /** Sent on attach and every 20 s so the client stale timer sees traffic. */
  | { type: 'heartbeat' }

/** `useAlerts` state: `unavailable` = no local server (direct mode) or it cannot be reached. */
export type AlertsStatus = 'loading' | 'ready' | 'unavailable'

/** Browser Notification API permission, or `unsupported` when the API is missing. */
export type NotifyPermission = 'granted' | 'denied' | 'default' | 'unsupported'
