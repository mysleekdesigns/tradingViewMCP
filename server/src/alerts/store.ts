// SQLite persistence for alerts and their trigger history. Every statement is prepared once;
// no string-built SQL. See server/README.md "Alerts".

import type { Db } from '../db/index.ts'
import {
  isAlertCondition,
  isAlertFrequency,
  type Alert,
  type AlertCondition,
  type AlertEvent,
  type AlertFrequency,
} from './types.ts'

/** History is pruned to the newest this-many events on every insert. */
export const MAX_ALERT_EVENTS = 1000

interface AlertRow {
  id: string
  symbol: string
  condition: string
  price: number
  frequency: string
  active: number
  note: string
  created_at: number
  updated_at: number
  last_triggered_at: number | null
}

interface EventRow {
  id: number
  alert_id: string
  symbol: string
  condition: string
  level: number
  price: number
  triggered_at: number
  message: string
}

/** Fields an update may change (already validated). */
export interface AlertUpdate {
  condition?: AlertCondition
  price?: number
  frequency?: AlertFrequency
  active?: boolean
  note?: string
}

export interface TriggerInput {
  alertId: string
  /** Last traded price that triggered it. */
  price: number
  triggeredAt: number
  message: string
}

export interface AlertStore {
  list(symbol?: string): Alert[]
  listActive(): Alert[]
  get(id: string): Alert | undefined
  count(): number
  insert(alert: Alert): void
  /** Applies `patch` and sets `updated_at`; undefined if the alert does not exist. */
  update(id: string, patch: AlertUpdate, updatedAt: number): Alert | undefined
  delete(id: string): boolean
  /**
   * One transaction: insert the event (pruning history), set last_triggered_at/updated_at,
   * and deactivate a `once` alert. Undefined (nothing written) if the alert is gone or inactive.
   */
  recordTrigger(input: TriggerInput): { event: AlertEvent; alert: Alert } | undefined
  /** Newest first. */
  listEvents(limit: number): AlertEvent[]
  clearEvents(): void
}

function rowToAlert(r: AlertRow): Alert {
  if (!isAlertCondition(r.condition) || !isAlertFrequency(r.frequency)) {
    throw new Error(`stored alert ${r.id} has an invalid condition or frequency`)
  }
  return {
    id: r.id,
    symbol: r.symbol,
    condition: r.condition,
    price: r.price,
    frequency: r.frequency,
    active: r.active !== 0,
    note: r.note,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
    lastTriggeredAt: r.last_triggered_at,
  }
}

function rowToEvent(r: EventRow): AlertEvent {
  if (!isAlertCondition(r.condition)) {
    throw new Error(`stored alert event ${r.id} has an invalid condition`)
  }
  return {
    id: r.id,
    alertId: r.alert_id,
    symbol: r.symbol,
    condition: r.condition,
    level: r.level,
    price: r.price,
    triggeredAt: r.triggered_at,
    message: r.message,
  }
}

export function createAlertStore(db: Db): AlertStore {
  const listAll = db.prepare<[], AlertRow>(
    `SELECT id, symbol, condition, price, frequency, active, note, created_at, updated_at, last_triggered_at FROM alerts ORDER BY created_at DESC, rowid DESC`,
  )
  const listBySymbol = db.prepare<[string], AlertRow>(
    `SELECT id, symbol, condition, price, frequency, active, note, created_at, updated_at, last_triggered_at FROM alerts WHERE symbol = ? ORDER BY created_at DESC, rowid DESC`,
  )
  const listActiveStmt = db.prepare<[], AlertRow>(
    `SELECT id, symbol, condition, price, frequency, active, note, created_at, updated_at, last_triggered_at FROM alerts WHERE active = 1 ORDER BY created_at, rowid`,
  )
  const getStmt = db.prepare<[string], AlertRow>(
    `SELECT id, symbol, condition, price, frequency, active, note, created_at, updated_at, last_triggered_at FROM alerts WHERE id = ?`,
  )
  const countStmt = db.prepare<[], { n: number }>('SELECT COUNT(*) AS n FROM alerts')
  const insertStmt = db.prepare<
    [string, string, string, number, string, number, string, number, number, number | null]
  >(
    `INSERT INTO alerts (id, symbol, condition, price, frequency, active, note, created_at, updated_at, last_triggered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
  const updateStmt = db.prepare<[string, number, string, number, string, number, string]>(
    `UPDATE alerts SET condition = ?, price = ?, frequency = ?, active = ?, note = ?,
       updated_at = ? WHERE id = ?`,
  )
  const deleteStmt = db.prepare<[string]>('DELETE FROM alerts WHERE id = ?')
  const insertEvent = db.prepare<[string, string, string, number, number, number, string]>(
    `INSERT INTO alert_events (alert_id, symbol, condition, level, price, triggered_at, message)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  )
  // Keep the newest MAX_ALERT_EVENTS rows (by id). With fewer rows the subquery is NULL and
  // nothing is deleted.
  const pruneEvents = db.prepare<[number]>(
    `DELETE FROM alert_events WHERE id < (
       SELECT id FROM alert_events ORDER BY id DESC LIMIT 1 OFFSET ?
     )`,
  )
  const markTriggered = db.prepare<[number, number, string]>(
    `UPDATE alerts SET last_triggered_at = ?, updated_at = ?,
       active = CASE WHEN frequency = 'once' THEN 0 ELSE active END
     WHERE id = ? AND active = 1`,
  )
  const getEvent = db.prepare<[number], EventRow>(
    `SELECT id, alert_id, symbol, condition, level, price, triggered_at, message FROM alert_events WHERE id = ?`,
  )
  const listEventsStmt = db.prepare<[number], EventRow>(
    `SELECT id, alert_id, symbol, condition, level, price, triggered_at, message FROM alert_events ORDER BY id DESC LIMIT ?`,
  )
  const clearEventsStmt = db.prepare('DELETE FROM alert_events')

  const get = (id: string): Alert | undefined => {
    const row = getStmt.get(id)
    return row ? rowToAlert(row) : undefined
  }

  const trigger = db.transaction((input: TriggerInput) => {
    const before = get(input.alertId)
    if (!before || !before.active) return undefined
    const changed = markTriggered.run(input.triggeredAt, input.triggeredAt, input.alertId)
    if (changed.changes === 0) return undefined
    const info = insertEvent.run(
      before.id,
      before.symbol,
      before.condition,
      before.price,
      input.price,
      input.triggeredAt,
      input.message,
    )
    pruneEvents.run(MAX_ALERT_EVENTS - 1)
    const eventRow = getEvent.get(Number(info.lastInsertRowid))
    const after = get(input.alertId)
    if (!eventRow || !after) throw new Error('alert trigger write did not persist')
    return { event: rowToEvent(eventRow), alert: after }
  })

  return {
    list(symbol) {
      const rows = symbol === undefined ? listAll.all() : listBySymbol.all(symbol)
      return rows.map(rowToAlert)
    },
    listActive() {
      return listActiveStmt.all().map(rowToAlert)
    },
    get,
    count() {
      return countStmt.get()?.n ?? 0
    },
    insert(a) {
      insertStmt.run(
        a.id,
        a.symbol,
        a.condition,
        a.price,
        a.frequency,
        a.active ? 1 : 0,
        a.note,
        a.createdAt,
        a.updatedAt,
        a.lastTriggeredAt,
      )
    },
    update(id, patch, updatedAt) {
      return db.transaction(() => {
        const cur = get(id)
        if (!cur) return undefined
        const next: Alert = {
          ...cur,
          condition: patch.condition ?? cur.condition,
          price: patch.price ?? cur.price,
          frequency: patch.frequency ?? cur.frequency,
          active: patch.active ?? cur.active,
          note: patch.note ?? cur.note,
          updatedAt,
        }
        updateStmt.run(
          next.condition,
          next.price,
          next.frequency,
          next.active ? 1 : 0,
          next.note,
          next.updatedAt,
          id,
        )
        return next
      })()
    },
    delete(id) {
      return deleteStmt.run(id).changes > 0
    },
    recordTrigger(input) {
      return trigger(input)
    },
    listEvents(limit) {
      return listEventsStmt.all(limit).map(rowToEvent)
    },
    clearEvents() {
      clearEventsStmt.run()
    },
  }
}
