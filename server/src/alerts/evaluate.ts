// Pure alert evaluation against the live last-traded price. No I/O, no clock: callers pass
// `nowMs`. See server/README.md "Alerts" for the semantics.

import { ALERT_CONDITION_LABELS, type AlertCondition, type AlertFrequency } from './types.ts'

/** Default minimum gap between two triggers of the same `every` alert. */
export const DEFAULT_ALERT_COOLDOWN_SEC = 60

/** Per-alert in-memory state. Created when the engine first sees the alert; reset on re-activation or edit. */
export interface AlertState {
  /** Previous price seen for this alert's symbol since the state was created. */
  prev: number | undefined
  /** `greater`/`less` only: may fire; cleared on fire, set again when the condition is false. */
  armed: boolean
  /** Millisecond time of this alert's last trigger (cooldown for `every`), if known. */
  lastFiredMs: number | undefined
}

export interface EvalInput {
  condition: AlertCondition
  /** Threshold price. */
  level: number
  frequency: AlertFrequency
}

export interface EvalResult {
  fired: boolean
  state: AlertState
}

export interface InitialStateOptions {
  /**
   * `greater`/`less` only: whether the first observation may fire. Default true (a new alert,
   * or an explicit reset). The engine passes false when it rebuilds state for an alert that
   * already fired before a restart, so a still-true condition does not fire again until it has
   * been seen false once. Crosses ignore it (they always need a previous price).
   */
  armed?: boolean
}

/** Fresh state. `lastTriggeredAtSec` (from the DB) seeds the cooldown across restarts and edits. */
export function initialState(
  lastTriggeredAtSec: number | null = null,
  opts: InitialStateOptions = {},
): AlertState {
  return {
    prev: undefined,
    armed: opts.armed ?? true,
    lastFiredMs: lastTriggeredAtSec === null ? undefined : lastTriggeredAtSec * 1000,
  }
}

export function isValidPrice(p: unknown): p is number {
  return typeof p === 'number' && Number.isFinite(p) && p > 0
}

/**
 * Evaluates one tick for one alert.
 * - crosses_above: prev < level && cur >= level (needs a previous price).
 * - crosses_below: prev > level && cur <= level (needs a previous price).
 * - greater / less: edge-triggered on cur > level / cur < level while armed; the first
 *   observation may fire. Re-armed when the condition is false.
 * - `every` alerts do not fire again within `cooldownMs` of their last trigger; a suppressed
 *   cross is dropped, while a suppressed greater/less stays armed and fires once the cooldown
 *   ends if the condition still holds. `once` alerts have no cooldown (they deactivate).
 * Invalid prices (non-finite or <= 0) are ignored and leave the state unchanged.
 */
export function evaluateTick(
  input: EvalInput,
  state: AlertState | undefined,
  cur: number,
  nowMs: number,
  cooldownMs: number,
): EvalResult {
  const s = state ?? initialState()
  if (!isValidPrice(cur) || !isValidPrice(input.level)) return { fired: false, state: s }
  const { level } = input
  const prev = s.prev
  let armed = s.armed
  let hit: boolean
  switch (input.condition) {
    case 'crosses_above':
      hit = prev !== undefined && prev < level && cur >= level
      break
    case 'crosses_below':
      hit = prev !== undefined && prev > level && cur <= level
      break
    case 'greater':
    case 'less': {
      const cond = input.condition === 'greater' ? cur > level : cur < level
      if (!cond) armed = true
      hit = cond && armed
      break
    }
  }
  const coolingDown =
    input.frequency === 'every' && s.lastFiredMs !== undefined && nowMs - s.lastFiredMs < cooldownMs
  const fired = hit && !coolingDown
  if (fired && (input.condition === 'greater' || input.condition === 'less')) armed = false
  return {
    fired,
    state: { prev: cur, armed, lastFiredMs: fired ? nowMs : s.lastFiredMs },
  }
}

const BIG = new Intl.NumberFormat('en-US', { maximumFractionDigits: 8, useGrouping: true })
const SMALL = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 8, useGrouping: true })

/**
 * en-US grouping, never scientific notation. |n| >= 1: up to 8 decimals; |n| < 1: up to 8
 * significant digits (so 0.000000012345 stays readable).
 */
export function formatPrice(n: number): string {
  if (!Number.isFinite(n)) return String(n)
  return Math.abs(n) >= 1 || n === 0 ? BIG.format(n) : SMALL.format(n)
}

/** e.g. `BTCUSDT crossing up 65,000 (last 65,012.5)`. */
export function formatAlertMessage(
  symbol: string,
  condition: AlertCondition,
  level: number,
  price: number,
): string {
  const label = ALERT_CONDITION_LABELS[condition].toLowerCase()
  return `${symbol} ${label} ${formatPrice(level)} (last ${formatPrice(price)})`
}
