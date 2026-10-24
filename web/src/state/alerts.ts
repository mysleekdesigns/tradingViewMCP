import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  Alert,
  AlertEvent,
  AlertPatch,
  AlertsStatus,
  AlertStreamMessage,
  NewAlert,
} from '../data/alertTypes'
import {
  DEFAULT_EVENTS_LIMIT,
  openAlertStream,
  type AlertsClient,
  type AlertStreamOptions,
} from '../data/alertsApi'
import type { ConnectionStatus } from '../types'

/**
 * Price alerts (all symbols) and their trigger history, kept in step with the local server:
 * REST for the lists and edits, `/ws/alerts` for pushes. Alerts are evaluated on the server.
 */

export interface AlertsApi {
  /** All symbols, newest first. */
  alerts: readonly Alert[]
  /** Newest first, up to 100. */
  events: readonly AlertEvent[]
  status: AlertsStatus
  /** Rejects with an Error whose message is user-readable. */
  create(a: NewAlert): Promise<Alert>
  update(id: string, patch: AlertPatch): Promise<Alert>
  remove(id: string): Promise<void>
  clearHistory(): Promise<void>
  /** Triggers received since markSeen(). */
  unseen: number
  markSeen(): void
}

/** While the initial load keeps failing, retry it this often. */
export const ALERTS_RETRY_MS = 30_000
export const MAX_ALERT_EVENTS = DEFAULT_EVENTS_LIMIT
/** Remembered trigger ids for onTriggered de-duplication. */
const MAX_NOTIFIED_IDS = 1000

export interface AlertsSnapshot {
  alerts: readonly Alert[]
  events: readonly AlertEvent[]
  status: AlertsStatus
  unseen: number
}

export const UNAVAILABLE_ALERTS_SNAPSHOT: AlertsSnapshot = Object.freeze({
  alerts: [],
  events: [],
  status: 'unavailable',
  unseen: 0,
})

export const LOADING_ALERTS_SNAPSHOT: AlertsSnapshot = Object.freeze({
  alerts: [],
  events: [],
  status: 'loading',
  unseen: 0,
})

/** Newest first: createdAt desc, then id desc for ties. */
export function sortAlerts(alerts: readonly Alert[]): Alert[] {
  return alerts
    .slice()
    .sort((a, b) => b.createdAt - a.createdAt || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0))
}

/** `alert` replacing the entry with its id, or added in newest-first order. */
export function upsertAlert(alerts: readonly Alert[], alert: Alert): Alert[] {
  const i = alerts.findIndex((a) => a.id === alert.id)
  if (i >= 0) {
    const out = alerts.slice()
    out[i] = alert
    return out
  }
  return sortAlerts([...alerts, alert])
}

/** Union by id (the first occurrence wins), newest first, at most MAX_ALERT_EVENTS. */
export function mergeEvents(
  primary: readonly AlertEvent[],
  extra: readonly AlertEvent[] = [],
): AlertEvent[] {
  const byId = new Map<number, AlertEvent>()
  for (const e of [...primary, ...extra]) if (!byId.has(e.id)) byId.set(e.id, e)
  return [...byId.values()]
    .sort((a, b) => b.triggeredAt - a.triggeredAt || b.id - a.id)
    .slice(0, MAX_ALERT_EVENTS)
}

/** A user-readable Error for a failed request (the client already surfaces server messages). */
function readable(e: unknown): Error {
  if (e instanceof Error && e.message !== '') return new Error(e.message)
  return new Error('The alert request failed')
}

export interface AlertsStoreOptions {
  /** null → status 'unavailable', no requests, no socket. */
  client: AlertsClient | null
  /** Once per triggered event id received on the stream. */
  onTriggered?: (event: AlertEvent, alert: Alert) => void
  /** Called after every snapshot change (never after dispose()). */
  onChange?: (snapshot: AlertsSnapshot) => void
  WebSocketImpl?: typeof WebSocket
  /** Stream opener; defaults to `openAlertStream`. Tests may inject a fake. */
  openStream?: (opts: AlertStreamOptions) => () => void
  /** Defaults to ALERTS_RETRY_MS. */
  retryMs?: number
}

export interface AlertsStore {
  readonly snapshot: AlertsSnapshot
  /** Initial load + stream. Idempotent. */
  start(): void
  create(a: NewAlert): Promise<Alert>
  update(id: string, patch: AlertPatch): Promise<Alert>
  remove(id: string): Promise<void>
  clearHistory(): Promise<void>
  markSeen(): void
  /** Closes the socket, cancels timers, ignores in-flight results. */
  dispose(): void
}

/**
 * The non-React core of `useAlerts`:
 *
 * - start → list() + listEvents(100) and open the stream. Initial load failure → 'unavailable'
 *   and a retry every `retryMs` until one succeeds (or the stream comes back live).
 * - 'changed' → refetch both, coalesced: at most one load in flight; messages during it queue
 *   exactly one more load.
 * - 'triggered' → prepend the event (dedupe by id), replace the alert, unseen += 1, and call
 *   onTriggered once per event id. Triggers that arrive while a load is in flight are merged
 *   into its result so a slower response cannot drop them.
 * - The stream reading 'live' again after it had been live (a reconnect) → refetch.
 * - Mutations apply their response to local state straight away.
 */
export function createAlertsStore(opts: AlertsStoreOptions): AlertsStore {
  const client = opts.client
  const retryMs = opts.retryMs ?? ALERTS_RETRY_MS
  const openStream = opts.openStream ?? openAlertStream

  let snapshot: AlertsSnapshot =
    client === null ? UNAVAILABLE_ALERTS_SNAPSHOT : LOADING_ALERTS_SNAPSHOT
  let started = false
  let disposed = false
  let loaded = false
  let loadInFlight = false
  let reloadQueued = false
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let closeStream: (() => void) | null = null
  let streamStatus: ConnectionStatus | null = null
  let wasLive = false
  /** Triggers received while the current load is in flight. */
  let duringLoad: { event: AlertEvent; alert: Alert }[] = []
  const notified = new Set<number>()

  function set(patch: Partial<AlertsSnapshot>): void {
    if (disposed) return
    snapshot = { ...snapshot, ...patch }
    opts.onChange?.(snapshot)
  }

  function clearRetry(): void {
    if (retryTimer !== null) clearTimeout(retryTimer)
    retryTimer = null
  }

  function scheduleRetry(): void {
    if (disposed || retryTimer !== null) return
    retryTimer = setTimeout(() => {
      retryTimer = null
      load()
    }, retryMs)
  }

  function load(): void {
    if (disposed || client === null) return
    if (loadInFlight) {
      reloadQueued = true
      return
    }
    loadInFlight = true
    duringLoad = []
    let req: Promise<[Alert[], AlertEvent[]]>
    try {
      req = Promise.all([client.list(), client.listEvents(MAX_ALERT_EVENTS)])
    } catch (e) {
      req = Promise.reject(e instanceof Error ? e : new Error(String(e)))
    }
    req.then(
      ([alerts, events]) => {
        if (disposed) return
        loadInFlight = false
        let nextAlerts = sortAlerts(alerts)
        for (const { alert } of duringLoad) {
          const i = nextAlerts.findIndex((a) => a.id === alert.id)
          if (i >= 0 && nextAlerts[i].updatedAt <= alert.updatedAt) {
            nextAlerts = upsertAlert(nextAlerts, alert)
          }
        }
        const nextEvents = mergeEvents(
          events,
          duringLoad.map((t) => t.event),
        )
        duringLoad = []
        loaded = true
        clearRetry()
        set({ alerts: nextAlerts, events: nextEvents, status: 'ready' })
        afterLoad()
      },
      () => {
        if (disposed) return
        loadInFlight = false
        duringLoad = []
        if (!loaded) {
          set({ status: 'unavailable' })
          scheduleRetry()
        }
        afterLoad()
      },
    )
  }

  function afterLoad(): void {
    if (!reloadQueued) return
    reloadQueued = false
    load()
  }

  function onTrigger(event: AlertEvent, alert: Alert): void {
    if (notified.has(event.id)) return
    notified.add(event.id)
    if (notified.size > MAX_NOTIFIED_IDS) {
      const oldest = notified.values().next().value
      if (oldest !== undefined) notified.delete(oldest)
    }
    if (loadInFlight) duringLoad.push({ event, alert })
    set({
      events: mergeEvents([event], snapshot.events),
      alerts: upsertAlert(snapshot.alerts, alert),
      unseen: snapshot.unseen + 1,
    })
    try {
      opts.onTriggered?.(event, alert)
    } catch (err) {
      console.error('useAlerts: onTriggered threw', err)
    }
  }

  function onMessage(m: AlertStreamMessage): void {
    if (disposed) return
    switch (m.type) {
      case 'heartbeat':
        return
      case 'changed':
        load()
        return
      case 'triggered':
        onTrigger(m.event, m.alert)
        return
    }
  }

  function onStatus(s: ConnectionStatus): void {
    if (disposed) return
    const prev = streamStatus
    streamStatus = s
    if (s !== 'live' || prev === 'live') return
    // Back to live after a drop: anything may have changed meanwhile. Also a quick recovery
    // when the server returns while the initial load is waiting for its retry.
    if (wasLive || snapshot.status === 'unavailable') load()
    wasLive = true
  }

  /** A successful mutation proves the server is up: recover from 'unavailable' at once. */
  function serverIsUp(): void {
    if (!loaded) {
      clearRetry()
      load()
    }
  }

  function requireClient(): AlertsClient {
    if (client === null)
      throw new Error('Alerts need the local server (npm run dev at the repo root)')
    return client
  }

  return {
    get snapshot() {
      return snapshot
    },
    start() {
      if (started || disposed || client === null) return
      started = true
      load()
      closeStream = openStream({
        onMessage,
        onStatus,
        WebSocketImpl: opts.WebSocketImpl,
      })
    },
    async create(a) {
      const c = requireClient()
      let alert: Alert
      try {
        alert = await c.create(a)
      } catch (e) {
        throw readable(e)
      }
      set({ alerts: upsertAlert(snapshot.alerts, alert) })
      serverIsUp()
      return alert
    },
    async update(id, patch) {
      const c = requireClient()
      let alert: Alert
      try {
        alert = await c.update(id, patch)
      } catch (e) {
        throw readable(e)
      }
      set({ alerts: upsertAlert(snapshot.alerts, alert) })
      serverIsUp()
      return alert
    },
    async remove(id) {
      const c = requireClient()
      try {
        await c.remove(id)
      } catch (e) {
        throw readable(e)
      }
      set({ alerts: snapshot.alerts.filter((a) => a.id !== id) })
      serverIsUp()
    },
    async clearHistory() {
      const c = requireClient()
      try {
        await c.clearEvents()
      } catch (e) {
        throw readable(e)
      }
      set({ events: [], unseen: 0 })
      serverIsUp()
    },
    markSeen() {
      if (snapshot.unseen !== 0) set({ unseen: 0 })
    },
    dispose() {
      if (disposed) return
      disposed = true
      clearRetry()
      const close = closeStream
      closeStream = null
      close?.()
    },
  }
}

interface Entry {
  client: AlertsClient | null
  snapshot: AlertsSnapshot
}

/** client null → status 'unavailable', no requests, no socket. */
export function useAlerts(opts: {
  client: AlertsClient | null
  onTriggered?: (event: AlertEvent, alert: Alert) => void
  WebSocketImpl?: typeof WebSocket
}): AlertsApi {
  const { client, WebSocketImpl } = opts
  const [entry, setEntry] = useState<Entry | null>(null)
  const storeRef = useRef<AlertsStore | null>(null)
  const onTriggeredRef = useRef(opts.onTriggered)

  useEffect(() => {
    onTriggeredRef.current = opts.onTriggered
  }, [opts.onTriggered])

  useEffect(() => {
    const store = createAlertsStore({
      client,
      WebSocketImpl,
      onTriggered: (event, alert) => onTriggeredRef.current?.(event, alert),
      onChange: (snapshot) => setEntry({ client, snapshot }),
    })
    storeRef.current = store
    store.start()
    return () => {
      store.dispose()
      if (storeRef.current === store) storeRef.current = null
    }
  }, [client, WebSocketImpl])

  const snapshot =
    entry !== null && entry.client === client
      ? entry.snapshot
      : client === null
        ? UNAVAILABLE_ALERTS_SNAPSHOT
        : LOADING_ALERTS_SNAPSHOT

  const withStore = useCallback(<T>(fn: (s: AlertsStore) => Promise<T>): Promise<T> => {
    const store = storeRef.current
    if (store === null) return Promise.reject(new Error('Alerts are not available right now'))
    return fn(store)
  }, [])

  const create = useCallback((a: NewAlert) => withStore((s) => s.create(a)), [withStore])
  const update = useCallback(
    (id: string, patch: AlertPatch) => withStore((s) => s.update(id, patch)),
    [withStore],
  )
  const remove = useCallback((id: string) => withStore((s) => s.remove(id)), [withStore])
  const clearHistory = useCallback(() => withStore((s) => s.clearHistory()), [withStore])
  const markSeen = useCallback(() => storeRef.current?.markSeen(), [])

  return useMemo(
    () => ({
      alerts: snapshot.alerts,
      events: snapshot.events,
      status: snapshot.status,
      unseen: snapshot.unseen,
      create,
      update,
      remove,
      clearHistory,
      markSeen,
    }),
    [snapshot, create, update, remove, clearHistory, markSeen],
  )
}
