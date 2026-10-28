import { PRICE_SOURCES } from '../indicators/sources'
import type { IndicatorDef, InputDef, PriceSource } from '../indicators/types'

/** The settings dialog's editable state: raw field text, so invalid entries can be shown. */
export interface SettingsDraft {
  inputs: Record<string, string>
  source: PriceSource
  colors: Record<string, string>
}

/** What the dialog submits; matches `IndicatorPatch` in state/indicators. */
export interface SettingsResult {
  inputs: Record<string, number>
  source: PriceSource
  colors: Record<string, string>
}

export type ParseResult =
  { ok: true; value: SettingsResult } | { ok: false; errors: Readonly<Record<string, string>> }

export type InputCheck = { ok: true; value: number } | { ok: false; error: string }

const NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i
const COLOR_RE = /^#[0-9a-f]{6}$/i

/** Plain decimal text for a number (no exponent for the ranges indicators use). */
export function formatInputValue(n: number): string {
  return String(n)
}

const pad2 = (n: number): string => String(n).padStart(2, '0')

/**
 * `<input type="datetime-local">` text for a time input, in UTC: "YYYY-MM-DDTHH:MM" (with ":SS"
 * when the seconds are not zero). 0, negative or non-finite (not set) is the empty field.
 */
export function timeInputText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const d = new Date(Math.round(seconds) * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const date = `${String(d.getUTCFullYear()).padStart(4, '0')}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`
  const time = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
  const sec = d.getUTCSeconds()
  return `${date}T${time}${sec === 0 ? '' : `:${pad2(sec)}`}`
}

const DATETIME_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/

/**
 * UNIX seconds for a datetime-local text read as UTC; the empty field is 0 (not set). Null for
 * anything else that is not a real calendar date and time (e.g. "2024-02-30T10:00").
 */
export function parseTimeInputText(text: string): number | null {
  const t = text.trim()
  if (t === '') return 0
  const m = DATETIME_RE.exec(t)
  if (!m) return null
  const [y, mo, d, h, mi] = [m[1], m[2], m[3], m[4], m[5]].map(Number)
  const sec = m[6] === undefined ? 0 : Number(m[6])
  if (mo < 1 || mo > 12 || d < 1 || h > 23 || mi > 59 || sec > 59) return null
  const date = new Date(0)
  date.setUTCFullYear(y, mo - 1, d)
  date.setUTCHours(h, mi, sec, 0)
  // Date rolls 30 Feb over to March: a mismatch means the day does not exist.
  if (date.getUTCMonth() !== mo - 1 || date.getUTCDate() !== d) return null
  return Math.floor(date.getTime() / 1000)
}

/** "2024-03-01 14:30 UTC" for messages; "Not set" for 0. */
export function formatTimeLabel(seconds: number): string {
  const text = timeInputText(seconds)
  return text === '' ? 'Not set' : `${text.replace('T', ' ')} UTC`
}

/** Field text for one input value (datetime-local text for time inputs). */
export function inputText(input: InputDef, value: number): string {
  return input.kind === 'time' ? timeInputText(value) : formatInputValue(value)
}

/** Draft for an existing instance. */
export function draftFromConfig(
  def: IndicatorDef,
  config: {
    inputs: Readonly<Record<string, number>>
    source: PriceSource
    colors: Readonly<Record<string, string>>
  },
): SettingsDraft {
  const inputs: Record<string, string> = {}
  for (const i of def.inputs) inputs[i.key] = inputText(i, config.inputs[i.key] ?? i.default)
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = config.colors[l.key] ?? l.color
  return { inputs, source: def.source === null ? 'close' : config.source, colors }
}

/** Draft holding `def`'s default inputs, source and line colours (the "Defaults" button). */
export function defaultDraft(def: IndicatorDef): SettingsDraft {
  const inputs: Record<string, string> = {}
  for (const i of def.inputs) inputs[i.key] = inputText(i, i.default)
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = l.color
  return { inputs, source: def.source ?? 'close', colors }
}

/** A time field (UTC datetime-local text); empty = 0 (not set), valid when min allows 0. */
function checkTimeInput(input: InputDef, raw: string): InputCheck {
  const value = parseTimeInputText(raw)
  if (value === null) return { ok: false, error: 'Enter a date and time' }
  if (value === 0 && input.min > 0) return { ok: false, error: 'Choose a date and time' }
  if (value < input.min || value > input.max) {
    return {
      ok: false,
      error: `Must be between ${formatTimeLabel(Math.max(1, input.min))} and ${formatTimeLabel(input.max)}`,
    }
  }
  return { ok: true, value }
}

/**
 * Parses one field. No silent clamping or rounding: out-of-range or fractional values are errors.
 * Time inputs (`kind: 'time'`) take datetime-local text in UTC (see `parseTimeInputText`).
 */
export function checkInput(input: InputDef, raw: string): InputCheck {
  if (input.kind === 'time') return checkTimeInput(input, raw)
  const text = raw.trim()
  if (text === '') return { ok: false, error: 'Enter a number' }
  if (!NUMBER_RE.test(text)) return { ok: false, error: 'Enter a number' }
  const value = Number(text)
  if (!Number.isFinite(value)) return { ok: false, error: 'Enter a number' }
  if (input.integer && !Number.isInteger(value)) {
    return { ok: false, error: 'Enter a whole number' }
  }
  if (value < input.min || value > input.max) {
    return { ok: false, error: `Must be between ${input.min} and ${input.max}` }
  }
  return { ok: true, value }
}

/**
 * Cross-field check for range indicators (time inputs keyed `from` and `to`, e.g. the volume
 * profile): a set "To" (non-zero) must not come before "From". Null when valid or when either
 * field does not parse (its own error is shown instead).
 */
function rangeError(def: IndicatorDef, draft: SettingsDraft): string | null {
  const from = def.inputs.find((i) => i.key === 'from' && i.kind === 'time')
  const to = def.inputs.find((i) => i.key === 'to' && i.kind === 'time')
  if (from === undefined || to === undefined) return null
  const f = checkInput(from, draft.inputs.from ?? '')
  const t = checkInput(to, draft.inputs.to ?? '')
  if (!f.ok || !t.ok) return null
  return t.value !== 0 && t.value < f.value ? 'Must be after From' : null
}

/** Per-field error messages keyed by input key (and `color:<line>` for colours); empty = valid. */
export function draftErrors(def: IndicatorDef, draft: SettingsDraft): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const i of def.inputs) {
    const r = checkInput(i, draft.inputs[i.key] ?? '')
    if (!r.ok) errors[i.key] = r.error
  }
  const range = rangeError(def, draft)
  if (range !== null && errors.to === undefined) errors.to = range
  if (def.source !== null && !(PRICE_SOURCES as readonly string[]).includes(draft.source)) {
    errors.source = 'Choose a source'
  }
  for (const l of def.lines) {
    if (!COLOR_RE.test(draft.colors[l.key] ?? '')) errors[`color:${l.key}`] = 'Invalid colour'
  }
  return errors
}

/** Validates the whole draft; on success returns the numbers/colours to save. */
export function parseDraft(def: IndicatorDef, draft: SettingsDraft): ParseResult {
  const errors = draftErrors(def, draft)
  if (Object.keys(errors).length > 0) return { ok: false, errors }
  const inputs: Record<string, number> = {}
  for (const i of def.inputs) {
    const r = checkInput(i, draft.inputs[i.key] ?? '')
    // draftErrors passed, so every field parses.
    inputs[i.key] = r.ok ? r.value : i.default
  }
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = (draft.colors[l.key] ?? l.color).toLowerCase()
  return {
    ok: true,
    value: { inputs, source: def.source === null ? 'close' : draft.source, colors },
  }
}
