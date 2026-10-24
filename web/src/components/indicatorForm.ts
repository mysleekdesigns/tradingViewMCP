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
  for (const i of def.inputs) inputs[i.key] = formatInputValue(config.inputs[i.key] ?? i.default)
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = config.colors[l.key] ?? l.color
  return { inputs, source: def.source === null ? 'close' : config.source, colors }
}

/** Draft holding `def`'s default inputs, source and line colours (the "Defaults" button). */
export function defaultDraft(def: IndicatorDef): SettingsDraft {
  const inputs: Record<string, string> = {}
  for (const i of def.inputs) inputs[i.key] = formatInputValue(i.default)
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = l.color
  return { inputs, source: def.source ?? 'close', colors }
}

/** Parses one field. No silent clamping or rounding: out-of-range or fractional values are errors. */
export function checkInput(input: InputDef, raw: string): InputCheck {
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

/** Per-field error messages keyed by input key (and `color:<line>` for colours); empty = valid. */
export function draftErrors(def: IndicatorDef, draft: SettingsDraft): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const i of def.inputs) {
    const r = checkInput(i, draft.inputs[i.key] ?? '')
    if (!r.ok) errors[i.key] = r.error
  }
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
  for (const i of def.inputs) inputs[i.key] = Number((draft.inputs[i.key] ?? '').trim())
  const colors: Record<string, string> = {}
  for (const l of def.lines) colors[l.key] = (draft.colors[l.key] ?? l.color).toLowerCase()
  return {
    ok: true,
    value: { inputs, source: def.source === null ? 'close' : draft.source, colors },
  }
}
