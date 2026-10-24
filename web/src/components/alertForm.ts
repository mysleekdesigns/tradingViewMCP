// Pure helpers for the alert create/edit dialog.

import type { AlertCondition, AlertFrequency } from '../data/alertTypes'

export const NOTE_MAX = 200

/** What the dialog hands to its `onSubmit`. */
export interface AlertDialogValues {
  condition: AlertCondition
  price: number
  frequency: AlertFrequency
  note: string
}

/** The dialog's editable state; the price stays text until submit. */
export interface AlertFormDraft {
  condition: AlertCondition
  priceText: string
  frequency: AlertFrequency
  note: string
}

export interface AlertFormErrors {
  price?: string
  note?: string
}

/**
 * A level above (or at) the last price is usually a breakout up, below it a breakdown.
 * Without a last price, default to crossing up.
 */
export function defaultCondition(price: number, lastPrice: number | null): AlertCondition {
  if (lastPrice === null || !Number.isFinite(lastPrice)) return 'crosses_above'
  return price >= lastPrice ? 'crosses_above' : 'crosses_below'
}

const DECIMAL = /^(\d+\.?\d*|\.\d+)$/

/**
 * Parses a typed price. Spaces and thousands separators ("65,000.5") are ignored. Returns
 * null for anything that is not a plain positive decimal (no signs, exponents, hex).
 */
export function parsePriceInput(text: string): number | null {
  const s = text.replace(/[\s,]/g, '')
  if (!DECIMAL.test(s)) return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

/** Input text for a price formatted for display (drops thousands separators). */
export function priceInputText(formatted: string): string {
  return formatted.replace(/[\s,]/g, '')
}

export function validateAlertForm(draft: AlertFormDraft): AlertFormErrors {
  const errors: AlertFormErrors = {}
  if (draft.priceText.trim() === '') {
    errors.price = 'Enter a price'
  } else {
    const price = parsePriceInput(draft.priceText)
    if (price === null) errors.price = 'Enter a number'
    else if (!(price > 0)) errors.price = 'Price must be greater than 0'
  }
  if (draft.note.length > NOTE_MAX) {
    errors.note = `Note must be at most ${NOTE_MAX} characters`
  }
  return errors
}

export type AlertFormResult =
  { ok: true; value: AlertDialogValues } | { ok: false; errors: AlertFormErrors }

/** Validates and converts the draft. The note is trimmed. */
export function parseAlertForm(draft: AlertFormDraft): AlertFormResult {
  const errors = validateAlertForm(draft)
  const price = parsePriceInput(draft.priceText)
  if (Object.keys(errors).length > 0 || price === null) {
    return { ok: false, errors }
  }
  return {
    ok: true,
    value: {
      condition: draft.condition,
      price,
      frequency: draft.frequency,
      note: draft.note.trim(),
    },
  }
}

/** Message to show when `onSubmit` rejects with anything (Error or not). */
export function submitErrorMessage(err: unknown): string {
  if (err instanceof Error && err.message.trim() !== '') return err.message
  if (typeof err === 'string' && err.trim() !== '') return err
  return 'Could not save the alert. Try again.'
}
