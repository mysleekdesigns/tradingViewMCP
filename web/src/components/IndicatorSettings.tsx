import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { PRICE_SOURCES } from '../indicators/sources'
import type { IndicatorDef, PriceSource } from '../indicators/types'
import type { IndicatorConfig, IndicatorPatch } from '../state/indicators'
import {
  defaultDraft,
  draftErrors,
  draftFromConfig,
  formatTimeLabel,
  hasStyleTab,
  isToggleOn,
  parseDraft,
  splitInputs,
  TOGGLE_OFF,
  TOGGLE_ON,
  type SettingsDraft,
} from './indicatorForm'
import './Indicators.css'

const SOURCE_LABELS: Record<PriceSource, string> = {
  close: 'Close',
  open: 'Open',
  high: 'High',
  low: 'Low',
  hl2: '(H + L) / 2',
  hlc3: '(H + L + C) / 3',
  ohlc4: '(O + H + L + C) / 4',
}

type Tab = 'inputs' | 'style'
const ALL_TABS: readonly { id: Tab; label: string }[] = [
  { id: 'inputs', label: 'Inputs' },
  { id: 'style', label: 'Style' },
]
/** Inputs only: defs drawn by a primitive (`lines: []`) have no line colours to edit. */
const INPUTS_ONLY = ALL_TABS.filter((t) => t.id === 'inputs')

interface IndicatorSettingsProps {
  def: IndicatorDef
  config: IndicatorConfig
  /** Called once on OK with the validated values; the dialog then closes. */
  onApply: (patch: IndicatorPatch) => void
  onClose: () => void
  /**
   * A seconds chart is open (1s/5s/…): time inputs then accept seconds (step 1) so a bar time
   * there can be entered exactly. Otherwise they step by minutes.
   */
  secondsCharts?: boolean
}

/**
 * Per-instance settings dialog (TradingView style): Inputs and Style tabs, applied on OK only.
 * On/off inputs (`kind: 'toggle'`) are checkboxes grouped under "Show" after the value fields.
 * Defs without lines (volume profile, market structure) have no Style tab.
 * Mount with `key={config.uid}`; it captures the previously focused element on mount and
 * restores it on unmount.
 */
export function IndicatorSettings({
  def,
  config,
  onApply,
  onClose,
  secondsCharts = false,
}: IndicatorSettingsProps) {
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFromConfig(def, config))
  const hasInputs = def.inputs.length > 0 || def.source !== null
  const styleTab = hasStyleTab(def)
  const tabs = styleTab ? ALL_TABS : INPUTS_ONLY
  const { fields, toggles } = useMemo(() => splitInputs(def), [def])
  const [tab, setTab] = useState<Tab>(hasInputs || !styleTab ? 'inputs' : 'style')
  /** Fields the user has edited; errors show for these (and all fields after a failed OK). */
  const [touched, setTouched] = useState<ReadonlySet<string>>(() => new Set())
  const dialogRef = useRef<HTMLDivElement>(null)
  const ids = useId()
  const titleId = `${ids}-title`
  const tabId = (t: Tab) => `${ids}-tab-${t}`
  const panelId = (t: Tab) => `${ids}-panel-${t}`

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const first = dialogRef.current?.querySelector<HTMLElement>(
      '.ind-settings__body input, .ind-settings__body select',
    )
    ;(first ?? dialogRef.current)?.focus()
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  const errors = useMemo(() => draftErrors(def, draft), [def, draft])
  const valid = Object.keys(errors).length === 0
  const inputErrorCount = def.inputs.filter((i) => errors[i.key] !== undefined).length

  const setInput = (key: string, value: string) => {
    setDraft((d) => ({ ...d, inputs: { ...d.inputs, [key]: value } }))
    setTouched((t) => new Set(t).add(key))
  }

  const setColor = (key: string, value: string) =>
    setDraft((d) => ({ ...d, colors: { ...d.colors, [key]: value } }))

  const submit = () => {
    const r = parseDraft(def, draft)
    if (!r.ok) {
      setTouched(new Set(Object.keys(r.errors)))
      if (def.inputs.some((i) => r.errors[i.key] !== undefined)) setTab('inputs')
      return
    }
    onApply({ inputs: r.value.inputs, source: r.value.source, colors: r.value.colors })
    onClose()
  }

  const resetDefaults = () => {
    setDraft(defaultDraft(def))
    setTouched(new Set())
  }

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') {
      return
    }
    e.preventDefault()
    const i = tabs.findIndex((t) => t.id === tab)
    const n = tabs.length
    const next =
      e.key === 'Home'
        ? 0
        : e.key === 'End'
          ? n - 1
          : (i + (e.key === 'ArrowRight' ? 1 : -1) + n) % n
    setTab(tabs[next].id)
    document.getElementById(tabId(tabs[next].id))?.focus()
  }

  const onDialogKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !dialogRef.current) return
    const focusables = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]):not([tabindex="-1"]), input, select, [tabindex="0"]',
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

  const showError = (key: string) => (touched.has(key) ? errors[key] : undefined)

  return (
    <div
      className="modal-backdrop modal-backdrop--center"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="ind-dialog ind-settings"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <form
          className="ind-settings__form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <div className="ind-dialog__head">
            <h2 id={titleId} className="ind-dialog__title">
              {def.name}
            </h2>
            <button
              type="button"
              className="icon-btn"
              aria-label={`Close ${def.name} settings`}
              title="Cancel (Esc)"
              onClick={onClose}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
              </svg>
            </button>
          </div>

          <div className="ind-tabs" role="tablist" aria-label={`${def.name} settings`}>
            {tabs.map((t) => (
              <button
                key={t.id}
                id={tabId(t.id)}
                type="button"
                role="tab"
                className="ind-tabs__tab"
                aria-selected={tab === t.id}
                // Only the active panel is rendered, so only its tab may reference it.
                aria-controls={tab === t.id ? panelId(t.id) : undefined}
                tabIndex={tab === t.id ? 0 : -1}
                onClick={() => setTab(t.id)}
                onKeyDown={onTabKeyDown}
              >
                {t.label}
                {t.id === 'inputs' && inputErrorCount > 0 && (
                  <span className="ind-tabs__badge" aria-label={`${inputErrorCount} errors`}>
                    !
                  </span>
                )}
              </button>
            ))}
          </div>

          <div
            id={panelId(tab)}
            className="ind-settings__body"
            role="tabpanel"
            aria-labelledby={tabId(tab)}
          >
            {tab === 'inputs' && (
              <div className="ind-fields">
                {!hasInputs && <p className="ind-fields__none">This indicator has no inputs.</p>}
                {fields.map((input) => {
                  const fieldId = `${ids}-in-${input.key}`
                  const errId = `${fieldId}-err`
                  const error = showError(input.key)
                  if (input.kind === 'time') {
                    const text = draft.inputs[input.key] ?? ''
                    const hintId = `${fieldId}-hint`
                    return (
                      <div key={input.key} className="ind-field">
                        <label className="ind-field__label" htmlFor={fieldId}>
                          {input.label}
                          <span className="ind-field__kind">Date and time (UTC)</span>
                        </label>
                        <div className="ind-field__control">
                          <div className="ind-field__control--row">
                            <input
                              id={fieldId}
                              className={`ind-input${error ? ' ind-input--invalid' : ''}`}
                              type="datetime-local"
                              // Shrinks beside the clear button (the native field is wide).
                              style={{ minWidth: 0 }}
                              // A value with seconds (e.g. picked on a 1s chart) needs step 1
                              // too, or the browser flags it as a step mismatch.
                              step={secondsCharts || /T\d\d:\d\d:\d\d/.test(text) ? 1 : 60}
                              value={text}
                              aria-invalid={error !== undefined}
                              aria-describedby={error ? errId : text === '' ? hintId : undefined}
                              onChange={(e) => setInput(input.key, e.target.value)}
                            />
                            {text !== '' && (
                              <button
                                type="button"
                                className="icon-btn icon-btn--small"
                                aria-label={`Clear ${input.label}`}
                                title="Clear (not set)"
                                onClick={() => setInput(input.key, '')}
                              >
                                <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                                  <path
                                    d="M2 2l6 6M8 2l-6 6"
                                    stroke="currentColor"
                                    strokeWidth="1.4"
                                  />
                                </svg>
                              </button>
                            )}
                          </div>
                          {error ? (
                            <span id={errId} className="ind-field__error" role="alert">
                              {error}
                            </span>
                          ) : (
                            text === '' && (
                              <span id={hintId} className="ind-field__kind">
                                {formatTimeLabel(0)}
                              </span>
                            )
                          )}
                        </div>
                      </div>
                    )
                  }
                  return (
                    <div key={input.key} className="ind-field">
                      <label className="ind-field__label" htmlFor={fieldId}>
                        {input.label}
                      </label>
                      <div className="ind-field__control">
                        <input
                          id={fieldId}
                          className={`ind-input${error ? ' ind-input--invalid' : ''}`}
                          type="number"
                          inputMode={input.integer ? 'numeric' : 'decimal'}
                          min={input.min}
                          max={input.max}
                          step={input.integer ? 1 : input.step}
                          value={draft.inputs[input.key] ?? ''}
                          aria-invalid={error !== undefined}
                          aria-describedby={error ? errId : undefined}
                          onChange={(e) => setInput(input.key, e.target.value)}
                        />
                        {error && (
                          <span id={errId} className="ind-field__error" role="alert">
                            {error}
                          </span>
                        )}
                      </div>
                    </div>
                  )
                })}
                {def.source !== null && (
                  <div className="ind-field">
                    <label className="ind-field__label" htmlFor={`${ids}-source`}>
                      Source
                    </label>
                    <div className="ind-field__control">
                      <select
                        id={`${ids}-source`}
                        className="ind-input ind-select"
                        value={draft.source}
                        onChange={(e) =>
                          setDraft((d) => ({ ...d, source: e.target.value as PriceSource }))
                        }
                      >
                        {PRICE_SOURCES.map((s) => (
                          <option key={s} value={s}>
                            {SOURCE_LABELS[s]}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}
                {toggles.length > 0 && (
                  <fieldset className="ind-toggles">
                    <legend className="ind-toggles__title">Show</legend>
                    <div className="ind-toggles__grid">
                      {toggles.map((input) => {
                        const fieldId = `${ids}-in-${input.key}`
                        const errId = `${fieldId}-err`
                        const error = showError(input.key)
                        return (
                          <div key={input.key} className="ind-toggle">
                            <label className="ind-toggle__label" htmlFor={fieldId}>
                              <input
                                id={fieldId}
                                className={`ind-check${error ? ' ind-check--invalid' : ''}`}
                                type="checkbox"
                                checked={isToggleOn(draft.inputs[input.key])}
                                aria-invalid={error !== undefined}
                                aria-describedby={error ? errId : undefined}
                                onChange={(e) =>
                                  setInput(input.key, e.target.checked ? TOGGLE_ON : TOGGLE_OFF)
                                }
                              />
                              <span>{input.label}</span>
                            </label>
                            {error && (
                              <span id={errId} className="ind-field__error" role="alert">
                                {error}
                              </span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  </fieldset>
                )}
              </div>
            )}

            {tab === 'style' && styleTab && (
              <div className="ind-fields">
                {def.lines.map((line) => {
                  const fieldId = `${ids}-color-${line.key}`
                  const color = draft.colors[line.key] ?? line.color
                  return (
                    <div key={line.key} className="ind-field">
                      <label className="ind-field__label" htmlFor={fieldId}>
                        {line.label}
                        <span className="ind-field__kind">
                          {line.kind === 'histogram' ? 'Histogram' : 'Line'}
                        </span>
                      </label>
                      <div className="ind-field__control ind-field__control--row">
                        <span className="ind-swatch">
                          <input
                            id={fieldId}
                            className="ind-swatch__input"
                            type="color"
                            value={color}
                            aria-label={`${line.label} colour`}
                            onChange={(e) => setColor(line.key, e.target.value)}
                          />
                        </span>
                        <span className="ind-swatch__hex">{color.toUpperCase()}</span>
                        <LinePreview color={color} kind={line.kind} width={line.lineWidth} />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="ind-settings__foot">
            <button
              type="button"
              className="btn btn--ghost ind-settings__defaults"
              onClick={resetDefaults}
              title={
                styleTab
                  ? 'Reset inputs and colours to the defaults (applied on OK)'
                  : 'Reset inputs to the defaults (applied on OK)'
              }
            >
              Defaults
            </button>
            <span className="ind-settings__spacer" />
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn--primary" disabled={!valid}>
              OK
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function LinePreview({
  color,
  kind,
  width,
}: {
  color: string
  kind: 'line' | 'histogram'
  width: 1 | 2
}) {
  return (
    <svg className="ind-preview" width="48" height="16" viewBox="0 0 48 16" aria-hidden="true">
      {kind === 'histogram' ? (
        [3, 9, 15, 21, 27, 33, 39].map((x, i) => {
          const h = [4, 7, 10, 6, 3, 8, 11][i]
          return <rect key={x} x={x} y={16 - h} width="4" height={h} fill={color} />
        })
      ) : (
        <path
          d="M1 12 9 8l7 3 9-7 8 5 8-4 6 2"
          fill="none"
          stroke={color}
          strokeWidth={width + 0.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      )}
    </svg>
  )
}
