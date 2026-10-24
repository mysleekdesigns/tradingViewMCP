import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  ALERT_CONDITIONS,
  ALERT_CONDITION_LABELS,
  type Alert,
  type AlertCondition,
  type AlertFrequency,
} from '../data/alertTypes'
import {
  NOTE_MAX,
  defaultCondition,
  parseAlertForm,
  parsePriceInput,
  priceInputText,
  submitErrorMessage,
  validateAlertForm,
  type AlertDialogValues,
  type AlertFormDraft,
} from './alertForm'
import { FREQUENCY_LABELS } from './alertsView'
import './Alerts.css'

export type { AlertDialogValues } from './alertForm'

export interface AlertDialogProps {
  symbol: string
  initialPrice: number
  lastPrice: number | null
  editing?: Alert
  formatPrice(price: number): string
  onSubmit(values: AlertDialogValues): Promise<void>
  onClose(): void
}

const FREQUENCIES: readonly AlertFrequency[] = ['once', 'every']
const FREQUENCY_HINTS: Record<AlertFrequency, string> = {
  once: 'Stops after it triggers once.',
  every: 'Triggers again each time the condition becomes true.',
}

function initialDraft(
  initialPrice: number,
  lastPrice: number | null,
  editing: Alert | undefined,
  formatPrice: (price: number) => string,
): AlertFormDraft {
  if (editing) {
    return {
      condition: editing.condition,
      priceText: priceInputText(formatPrice(editing.price)),
      frequency: editing.frequency,
      note: editing.note,
    }
  }
  return {
    condition: defaultCondition(initialPrice, lastPrice),
    priceText: Number.isFinite(initialPrice) ? priceInputText(formatPrice(initialPrice)) : '',
    frequency: 'once',
    note: '',
  }
}

/**
 * Create or edit a price alert. Modal with a focus trap; Esc or a click on the backdrop
 * cancels. Mount with a fresh `key` per open; it restores the previous focus on unmount.
 */
export function AlertDialog({
  symbol,
  initialPrice,
  lastPrice,
  editing,
  formatPrice,
  onSubmit,
  onClose,
}: AlertDialogProps) {
  const [draft, setDraft] = useState<AlertFormDraft>(() =>
    initialDraft(initialPrice, lastPrice, editing, formatPrice),
  )
  /** Until the user picks a condition, a new alert's condition follows the typed price. */
  const [conditionTouched, setConditionTouched] = useState(editing !== undefined)
  const [touched, setTouched] = useState<ReadonlySet<keyof AlertFormDraft>>(() => new Set())
  const [pending, setPending] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const priceRef = useRef<HTMLInputElement>(null)
  const mounted = useRef(true)
  const ids = useId()
  const titleId = `${ids}-title`

  useEffect(() => {
    mounted.current = true
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const input = priceRef.current
    if (input) {
      input.focus()
      input.select()
    } else {
      dialogRef.current?.focus()
    }
    return () => {
      mounted.current = false
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  const errors = useMemo(() => validateAlertForm(draft), [draft])

  const update = (patch: Partial<AlertFormDraft>) => {
    setDraft((d) => {
      const next = { ...d, ...patch }
      if (!conditionTouched && patch.priceText !== undefined) {
        const p = parsePriceInput(patch.priceText)
        if (p !== null && p > 0) next.condition = defaultCondition(p, lastPrice)
      }
      return next
    })
    setTouched((t) => {
      const n = new Set(t)
      for (const k of Object.keys(patch) as (keyof AlertFormDraft)[]) n.add(k)
      return n
    })
    setSubmitError(null)
  }

  const submit = async () => {
    if (pending) return
    const r = parseAlertForm(draft)
    if (!r.ok) {
      setTouched(new Set<keyof AlertFormDraft>(['priceText', 'note']))
      if (r.errors.price) priceRef.current?.focus()
      return
    }
    setPending(true)
    setSubmitError(null)
    try {
      await onSubmit(r.value)
      if (mounted.current) onClose()
    } catch (err) {
      if (mounted.current) setSubmitError(submitErrorMessage(err))
    } finally {
      if (mounted.current) setPending(false)
    }
  }

  const onDialogKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // Keep keys away from global chart shortcuts while the dialog is open.
    e.stopPropagation()
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !dialogRef.current) return
    const focusables = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([tabindex="-1"]), input:not([readonly]), select, textarea',
      ),
    )
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  const onFrequencyKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return
    e.preventDefault()
    const i = FREQUENCIES.indexOf(draft.frequency)
    const next = FREQUENCIES[(i + 1) % FREQUENCIES.length]
    update({ frequency: next })
    document.getElementById(`${ids}-freq-${next}`)?.focus()
  }

  const priceError = touched.has('priceText') ? errors.price : undefined
  const noteError = touched.has('note') ? errors.note : undefined
  const parsedPrice = errors.price ? null : parsePriceInput(draft.priceText)
  const priceErrId = `${ids}-price-err`
  const noteCountId = `${ids}-note-count`
  const verb = editing ? 'Save' : 'Create'

  return (
    <div
      className="modal-backdrop modal-backdrop--center alert-dialog__backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="alert-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={pending}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <form
          className="alert-dialog__form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <div className="alert-dialog__head">
            <span className="alert-dialog__head-icon" aria-hidden="true">
              <svg
                width="18"
                height="18"
                viewBox="0 0 22 22"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M6 15.5V10a5 5 0 0 1 10 0v5.5l1.5 1.5h-13zM9.2 17.5a1.9 1.9 0 0 0 3.6 0" />
              </svg>
            </span>
            <h2 id={titleId} className="alert-dialog__title">
              {editing ? 'Edit alert' : 'Create alert'}
              <span className="alert-dialog__title-symbol">on {symbol}</span>
            </h2>
            <button
              type="button"
              className="icon-btn"
              aria-label="Close alert dialog"
              title="Cancel (Esc)"
              onClick={onClose}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
              </svg>
            </button>
          </div>

          <div className="alert-dialog__body">
            <div className="alert-field">
              <label className="alert-field__label" htmlFor={`${ids}-symbol`}>
                Symbol
              </label>
              <input
                id={`${ids}-symbol`}
                className="alert-input alert-input--readonly"
                value={symbol}
                readOnly
                tabIndex={-1}
                aria-readonly="true"
              />
            </div>

            <div className="alert-field">
              <label className="alert-field__label" htmlFor={`${ids}-condition`}>
                Condition
              </label>
              <select
                id={`${ids}-condition`}
                className="alert-input alert-select"
                value={draft.condition}
                onChange={(e) => {
                  setConditionTouched(true)
                  update({ condition: e.target.value as AlertCondition })
                }}
              >
                {ALERT_CONDITIONS.map((c) => (
                  <option key={c} value={c}>
                    {ALERT_CONDITION_LABELS[c]}
                  </option>
                ))}
              </select>
            </div>

            <div className="alert-field">
              <label className="alert-field__label" htmlFor={`${ids}-price`}>
                Price
              </label>
              <div className="alert-field__control">
                <div className="alert-price">
                  <input
                    ref={priceRef}
                    id={`${ids}-price`}
                    className={`alert-input alert-input--num${priceError ? ' alert-input--invalid' : ''}`}
                    type="text"
                    inputMode="decimal"
                    autoComplete="off"
                    spellCheck={false}
                    value={draft.priceText}
                    aria-invalid={priceError !== undefined}
                    aria-describedby={priceError ? priceErrId : undefined}
                    onChange={(e) => update({ priceText: e.target.value })}
                  />
                  {lastPrice !== null && Number.isFinite(lastPrice) && (
                    <button
                      type="button"
                      className="alert-price__last"
                      title="Use the last price"
                      aria-label={`Use last price ${formatPrice(lastPrice)}`}
                      onClick={() => update({ priceText: priceInputText(formatPrice(lastPrice)) })}
                    >
                      <span className="alert-price__last-label">Last</span>
                      <span className="alert-price__last-value">{formatPrice(lastPrice)}</span>
                    </button>
                  )}
                </div>
                {priceError && (
                  <span id={priceErrId} className="alert-field__error" role="alert">
                    {priceError}
                  </span>
                )}
              </div>
            </div>

            <div className="alert-field">
              <span className="alert-field__label" id={`${ids}-freq-label`}>
                Trigger
              </span>
              <div className="alert-field__control">
                <div
                  className="alert-segmented"
                  role="radiogroup"
                  aria-labelledby={`${ids}-freq-label`}
                >
                  {FREQUENCIES.map((f) => (
                    <button
                      key={f}
                      id={`${ids}-freq-${f}`}
                      type="button"
                      role="radio"
                      aria-checked={draft.frequency === f}
                      tabIndex={draft.frequency === f ? 0 : -1}
                      className="alert-segmented__btn"
                      onClick={() => update({ frequency: f })}
                      onKeyDown={onFrequencyKeyDown}
                    >
                      {FREQUENCY_LABELS[f]}
                    </button>
                  ))}
                </div>
                <span className="alert-field__hint">{FREQUENCY_HINTS[draft.frequency]}</span>
              </div>
            </div>

            <div className="alert-field">
              <label className="alert-field__label" htmlFor={`${ids}-note`}>
                Note
                <span className="alert-field__optional">Optional</span>
              </label>
              <div className="alert-field__control">
                <textarea
                  id={`${ids}-note`}
                  className={`alert-input alert-textarea${noteError ? ' alert-input--invalid' : ''}`}
                  rows={2}
                  maxLength={NOTE_MAX}
                  placeholder="e.g. Breakout above the weekly high"
                  value={draft.note}
                  aria-invalid={noteError !== undefined}
                  aria-describedby={noteCountId}
                  onChange={(e) => update({ note: e.target.value })}
                />
                <span className="alert-field__meta">
                  {noteError ? (
                    <span className="alert-field__error" role="alert">
                      {noteError}
                    </span>
                  ) : (
                    <span />
                  )}
                  <span
                    id={noteCountId}
                    className={`alert-field__count${draft.note.length >= NOTE_MAX ? ' alert-field__count--max' : ''}`}
                  >
                    {draft.note.length}/{NOTE_MAX}
                  </span>
                </span>
              </div>
            </div>

            {submitError && (
              <div className="alert-dialog__error" role="alert">
                <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                  <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" strokeWidth="1.3" />
                  <path
                    d="M7 3.8v3.8M7 9.6v.6"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    strokeLinecap="round"
                  />
                </svg>
                <span>{submitError}</span>
              </div>
            )}
          </div>

          <div className="alert-dialog__foot">
            <span className="alert-dialog__summary">
              {parsedPrice !== null &&
                `${symbol} ${ALERT_CONDITION_LABELS[draft.condition].toLowerCase()} ${formatPrice(parsedPrice)}`}
            </span>
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--primary alert-dialog__submit"
              disabled={pending}
            >
              {pending && (
                <svg
                  className="icon--spin"
                  width="14"
                  height="14"
                  viewBox="0 0 14 14"
                  aria-hidden="true"
                >
                  <circle
                    cx="7"
                    cy="7"
                    r="5.5"
                    fill="none"
                    stroke="currentColor"
                    strokeOpacity="0.3"
                    strokeWidth="1.6"
                  />
                  <path
                    d="M7 1.5a5.5 5.5 0 0 1 5.5 5.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                  />
                </svg>
              )}
              {verb}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
