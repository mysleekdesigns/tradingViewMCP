// Server-side alert engine: evaluates active alerts against the shared `!miniTicker@arr`
// stream (live last-traded price), records triggers, pushes them to `/ws/alerts` clients, and
// fires the optional desktop/webhook notifiers. Notification only; never touches orders.

import type { Ticker } from '../types.ts'
import { TICKERS_KEY, type Hub, type HubEvent } from '../upstream/hub.ts'
import {
  DEFAULT_ALERT_COOLDOWN_SEC,
  evaluateTick,
  formatAlertMessage,
  initialState,
  isValidPrice,
  type AlertState,
} from './evaluate.ts'
import type { AlertStore } from './store.ts'
import {
  NOOP_LOG,
  type Alert,
  type AlertEvent,
  type AlertLog,
  type AlertNotifier,
  type AlertStreamMessage,
  type DesktopNotifyMode,
} from './types.ts'
import type { AlertWebhookPayload } from './notify.ts'

export type AlertClient = (msg: AlertStreamMessage) => void

export interface AlertEngineOptions {
  store: AlertStore
  /** Only `acquire` is used, and only for TICKERS_KEY. */
  hub: Pick<Hub, 'acquire'>
  /** Millisecond clock. */
  now: () => number
  /** Minimum seconds between two triggers of the same `every` alert. Default 60. */
  cooldownSec?: number
  notifier?: AlertNotifier
  desktopMode?: DesktopNotifyMode
  log?: AlertLog
}

export interface AlertEngine {
  /**
   * Reloads active alerts from the store, drops state of alerts no longer active, and holds a
   * hub acquire on TICKERS_KEY only while at least one active alert exists. Call after every
   * mutation.
   */
  sync(): void
  /**
   * Forgets one alert's in-memory state and marks it for an explicit re-arm: its next state
   * starts armed even if it fired before. Call before `sync()` on re-activation/edit/delete.
   */
  reset(id: string): void
  /** Attach a `/ws/alerts` client. Returns an idempotent detach. */
  subscribe(client: AlertClient): () => void
  readonly clientCount: number
  /** True while the engine holds the tickers stream. */
  readonly watching: boolean
  broadcast(msg: AlertStreamMessage): void
  /** One upstream ticker batch. Wired to the hub; exposed for tests. */
  onTickers(tickers: readonly Ticker[]): void
  /** Releases the hub and detaches every client. Further calls are no-ops. */
  close(): void
}

export interface FiredAlert {
  event: AlertEvent
  alert: Alert
}

/** Lines listed in a merged desktop notification before "+M more". */
export const DESKTOP_BATCH_LINES = 5
/** Discord rejects `content` over 2000 chars; merged webhook text stays under this. */
export const WEBHOOK_TEXT_MAX_CHARS = 1900

function notificationText(event: AlertEvent, alert: Alert): string {
  return alert.note === '' ? event.message : `${event.message}\nNote: ${alert.note}`
}

/**
 * Desktop title/body for one ticker batch. One trigger: `Alert: SYM` + message (and note).
 * Several: `Alerts: N triggered` + up to 5 messages, then `+M more`.
 */
export function desktopNotification(fired: readonly FiredAlert[]): {
  title: string
  body: string
} {
  const first = fired[0]
  if (first === undefined) return { title: 'Alerts: 0 triggered', body: '' }
  if (fired.length === 1) {
    return {
      title: `Alert: ${first.alert.symbol}`,
      body: notificationText(first.event, first.alert),
    }
  }
  const lines = fired.slice(0, DESKTOP_BATCH_LINES).map((f) => f.event.message)
  const more = fired.length - lines.length
  if (more > 0) lines.push(`+${more} more`)
  return { title: `Alerts: ${fired.length} triggered`, body: lines.join('\n') }
}

/**
 * Webhook body for one ticker batch: at most one POST per batch. `event`/`alert` are the first
 * trigger; with several triggers `events` lists all of them and `text`/`content` join each
 * trigger's text with newlines (capped below Discord's 2000-char limit with `+M more`).
 * `allowed_mentions: { parse: [] }` stops a note like `@everyone` pinging a Discord channel.
 */
export function webhookPayload(fired: readonly FiredAlert[]): AlertWebhookPayload {
  const first = fired[0]
  if (first === undefined) throw new Error('webhookPayload needs at least one trigger')
  if (fired.length === 1) {
    const text = notificationText(first.event, first.alert)
    return {
      text,
      content: text,
      event: first.event,
      alert: first.alert,
      allowed_mentions: { parse: [] },
    }
  }
  const parts: string[] = []
  let len = 0
  for (const [i, f] of fired.entries()) {
    const part = notificationText(f.event, f.alert)
    const tail = `+${fired.length - i} more`
    // Room for this part, its newline, and a possible "+M more" line after it.
    const budget = i === fired.length - 1 ? 0 : tail.length + 1
    if (len + (parts.length > 0 ? 1 : 0) + part.length + budget > WEBHOOK_TEXT_MAX_CHARS) {
      parts.push(tail)
      break
    }
    len += (parts.length > 0 ? 1 : 0) + part.length
    parts.push(part)
  }
  const text = parts.join('\n')
  return {
    text,
    content: text,
    event: first.event,
    alert: first.alert,
    events: fired.map((f) => f.event),
    allowed_mentions: { parse: [] },
  }
}

export function createAlertEngine(opts: AlertEngineOptions): AlertEngine {
  const { store, hub, now } = opts
  const log = opts.log ?? NOOP_LOG
  const cooldownMs = Math.max(0, opts.cooldownSec ?? DEFAULT_ALERT_COOLDOWN_SEC) * 1000
  const notifier = opts.notifier ?? {}
  const desktopMode = opts.desktopMode ?? 'auto'

  /** Active alerts by symbol. */
  let bySymbol = new Map<string, Alert[]>()
  const states = new Map<string, AlertState>()
  /** Ids reset explicitly (PATCH active/price/condition): their next state starts armed. */
  const rearm = new Set<string>()
  const clients = new Set<AlertClient>()
  let release: (() => void) | undefined
  let closed = false

  const send = (client: AlertClient, msg: AlertStreamMessage): void => {
    try {
      client(msg)
    } catch {
      // One broken client must not affect the others.
    }
  }

  const broadcast = (msg: AlertStreamMessage): void => {
    for (const c of [...clients]) if (clients.has(c)) send(c, msg)
  }

  const fireAndForget = (what: string, fn: () => Promise<void>): void => {
    try {
      fn().catch((err: unknown) => {
        log.warn({ err: err instanceof Error ? err.name : 'error' }, `${what} notifier failed`)
      })
    } catch (err) {
      log.warn({ err: err instanceof Error ? err.name : 'error' }, `${what} notifier failed`)
    }
  }

  /** One desktop notification and at most one webhook POST per ticker batch. */
  const notify = (fired: readonly FiredAlert[]): void => {
    if (fired.length === 0) return
    const desktop = notifier.desktop
    const wantDesktop = desktopMode === 'always' || (desktopMode === 'auto' && clients.size === 0)
    if (desktop && wantDesktop) {
      const { title, body } = desktopNotification(fired)
      fireAndForget('desktop', () => desktop(title, body))
    }
    const webhook = notifier.webhook
    if (webhook) {
      const payload = webhookPayload(fired)
      fireAndForget('webhook', () => webhook(payload))
    }
  }

  const setWatching = (want: boolean): void => {
    if (want && release === undefined && !closed) {
      release = hub.acquire(TICKERS_KEY, onHubEvent)
    } else if (!want && release !== undefined) {
      const r = release
      release = undefined
      r()
    }
  }

  const rebuild = (active: readonly Alert[]): void => {
    const next = new Map<string, Alert[]>()
    const ids = new Set<string>()
    for (const a of active) {
      ids.add(a.id)
      const list = next.get(a.symbol)
      if (list) list.push(a)
      else next.set(a.symbol, [a])
    }
    for (const id of [...states.keys()]) if (!ids.has(id)) states.delete(id)
    for (const id of [...rearm]) if (!ids.has(id)) rearm.delete(id)
    bySymbol = next
  }

  /**
   * State for an alert seen for the first time (new, re-armed, or after a restart). An alert
   * that already fired (`lastTriggeredAt !== null`) and was not explicitly reset starts
   * disarmed, so a restart (e.g. `tsx watch`) never re-fires a still-true greater/less alert;
   * it re-arms once the condition is seen false.
   */
  const freshState = (alert: Alert): AlertState => {
    const armed = rearm.has(alert.id) || alert.lastTriggeredAt === null
    rearm.delete(alert.id)
    return initialState(alert.lastTriggeredAt, { armed })
  }

  const sync = (): void => {
    if (closed) return
    rebuild(store.listActive())
    setWatching(bySymbol.size > 0)
  }

  const onTickers = (tickers: readonly Ticker[]): void => {
    if (closed || bySymbol.size === 0) return
    const fired: FiredAlert[] = []
    const nowMs = now()
    for (const t of tickers) {
      const list = bySymbol.get(t.symbol)
      if (!list || !isValidPrice(t.last)) continue
      for (const alert of list) {
        const r = evaluateTick(
          { condition: alert.condition, level: alert.price, frequency: alert.frequency },
          states.get(alert.id) ?? freshState(alert),
          t.last,
          nowMs,
          cooldownMs,
        )
        states.set(alert.id, r.state)
        if (!r.fired) continue
        const triggeredAt = Math.floor(nowMs / 1000)
        try {
          const res = store.recordTrigger({
            alertId: alert.id,
            price: t.last,
            triggeredAt,
            message: formatAlertMessage(alert.symbol, alert.condition, alert.price, t.last),
          })
          if (res) fired.push(res)
        } catch (err) {
          log.warn(
            { err: err instanceof Error ? err.message : 'error' },
            'alert trigger write failed',
          )
        }
      }
    }
    if (fired.length === 0) return
    // The DB is the source of truth after a trigger (once alerts are now inactive).
    sync()
    for (const f of fired) broadcast({ type: 'triggered', event: f.event, alert: f.alert })
    broadcast({ type: 'changed' })
    notify(fired)
  }

  function onHubEvent(ev: HubEvent): void {
    if (ev.type === 'tickers') onTickers(ev.tickers)
  }

  return {
    sync,
    reset(id) {
      states.delete(id)
      rearm.add(id)
    },
    subscribe(client) {
      if (closed) return () => {}
      const wrapped: AlertClient = (m) => client(m)
      clients.add(wrapped)
      return () => {
        clients.delete(wrapped)
      }
    },
    get clientCount() {
      return clients.size
    },
    get watching() {
      return release !== undefined
    },
    broadcast,
    onTickers,
    close() {
      if (closed) return
      setWatching(false)
      closed = true
      clients.clear()
      states.clear()
      rearm.clear()
      bySymbol = new Map()
    },
  }
}
