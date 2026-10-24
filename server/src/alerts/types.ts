// Alert wire shapes. Mirrors web/src/data/alertTypes.ts EXACTLY; see server/README.md "Alerts".
// All times are UNIX seconds UTC. Notification-only: nothing here places or touches orders.

export type AlertCondition = 'crosses_above' | 'crosses_below' | 'greater' | 'less'

/** `once`: deactivate after the first trigger. `every`: re-arm once the condition is false again. */
export type AlertFrequency = 'once' | 'every'

export const ALERT_CONDITIONS: readonly AlertCondition[] = [
  'crosses_above',
  'crosses_below',
  'greater',
  'less',
]

export const ALERT_FREQUENCIES: readonly AlertFrequency[] = ['once', 'every']

export const ALERT_CONDITION_LABELS: Readonly<Record<AlertCondition, string>> = {
  crosses_above: 'Crossing up',
  crosses_below: 'Crossing down',
  greater: 'Greater than',
  less: 'Less than',
}

export function isAlertCondition(v: unknown): v is AlertCondition {
  return typeof v === 'string' && (ALERT_CONDITIONS as readonly string[]).includes(v)
}

export function isAlertFrequency(v: unknown): v is AlertFrequency {
  return typeof v === 'string' && (ALERT_FREQUENCIES as readonly string[]).includes(v)
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

// --- server-only ----------------------------------------------------------------------------

/** Body POSTed to the optional webhook. `text` suits Slack, `content` suits Discord. */
export interface WebhookPayload {
  text: string
  content: string
  event: AlertEvent
  alert: Alert
}

/** Injected notifiers. Each must never throw (failures are logged and swallowed). */
export interface AlertNotifier {
  desktop?: (title: string, body: string) => Promise<void>
  webhook?: (payload: WebhookPayload) => Promise<void>
}

/**
 * `auto`: desktop notification only when no `/ws/alerts` client is attached (an open browser
 * shows its own notification). `always`: every trigger. `off`: never.
 */
export type DesktopNotifyMode = 'auto' | 'always' | 'off'

export const DESKTOP_NOTIFY_MODES: readonly DesktopNotifyMode[] = ['auto', 'always', 'off']

export function isDesktopNotifyMode(v: unknown): v is DesktopNotifyMode {
  return typeof v === 'string' && (DESKTOP_NOTIFY_MODES as readonly string[]).includes(v)
}

/** Minimal logger shape (pino-compatible) used by the engine and notifiers. */
export interface AlertLog {
  warn(obj: Record<string, unknown>, msg: string): void
}

export const NOOP_LOG: AlertLog = { warn: () => {} }
