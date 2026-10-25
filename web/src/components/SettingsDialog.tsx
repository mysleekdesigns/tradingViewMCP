import { useEffect, useId, useRef, useState, type JSX, type KeyboardEvent } from 'react'
import { formatDateTime, timezoneLabel } from '../chart/timeFormat'
import {
  PROVIDER_IDS,
  PROVIDER_LABELS,
  isDirectProvider,
  type ProviderId,
} from '../data/providerIds'
import {
  DEFAULT_DOWN_COLOR,
  DEFAULT_UP_COLOR,
  TIMEZONE_CHOICES,
  type Settings,
  type ThemeName,
} from '../state/settings'
import { DRAWING_PALETTE, colorName, gridMove } from './drawingTools'
import { useModalKeys } from './useModalKeys'
import './Settings.css'

export interface SettingsDialogProps {
  settings: Settings
  /** Every change except the provider applies at once; the provider waits for "Apply & reload". */
  onChange: (next: Settings) => void
  onClose: () => void
  /** True when switching to `next.provider` needs a reload (the integrator then reloads). */
  providerNeedsReload: (next: Settings) => boolean
}

type Section = 'appearance' | 'data' | 'time'

const SECTIONS: readonly { id: Section; label: string }[] = [
  { id: 'appearance', label: 'Appearance' },
  { id: 'data', label: 'Data' },
  { id: 'time', label: 'Time' },
]

const THEME_OPTIONS: readonly { id: ThemeName; label: string }[] = [
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
]

const PROVIDER_NOTES: Readonly<Record<ProviderId, string>> = {
  server:
    'Through the local server: cached history, drawings saved to the database, price alerts, and the lab.',
  binance:
    'Straight from Binance’s public market-data API. No server features: no alerts, no cache, drawings stay in this browser.',
  coinbase:
    'Straight from Coinbase Exchange. USD pairs such as BTCUSD and ETHUSD. No server features: no alerts, no cache, drawings stay in this browser.',
}

const SWATCH_COLUMNS = 10

/**
 * Settings: theme, candle colours, default data provider, display time zone. A modal: keys go
 * to `useModalKeys` (Esc closes, Tab stays inside, nothing reaches the chart behind it, also
 * after focus falls to <body>); a click on the backdrop closes it and focus returns to the
 * opener. The dialog never reloads the page itself: "Apply & reload" only calls `onChange`.
 */
export function SettingsDialog({
  settings,
  onChange,
  onClose,
  providerNeedsReload,
}: SettingsDialogProps): JSX.Element {
  const [section, setSection] = useState<Section>('appearance')
  /** A provider picked but not applied yet (applying reloads the app). */
  const [pendingProvider, setPendingProvider] = useState<ProviderId | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const ids = useId()
  const titleId = `${ids}-title`

  useModalKeys(dialogRef, onClose)

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    focusSelectedTab(dialogRef.current)
    return () => {
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  // A focused control that unmounts or becomes disabled (the reload box's buttons, "Reset to
  // defaults") drops focus to <body>. Bring it back into the dialog after every render.
  useEffect(() => {
    const root = dialogRef.current
    if (!root) return
    const active = document.activeElement
    const lost =
      active === null ||
      active === document.body ||
      (root.contains(active) && active instanceof HTMLButtonElement && active.disabled)
    if (lost) focusSelectedTab(root)
  })

  const update = (patch: Partial<Settings>) => onChange({ ...settings, ...patch })

  const chooseProvider = (id: ProviderId) => {
    const next = { ...settings, provider: id }
    if (id === settings.provider) setPendingProvider(null)
    else if (providerNeedsReload(next)) setPendingProvider(id)
    else {
      setPendingProvider(null)
      onChange(next)
    }
  }

  const staged = pendingProvider !== null ? { ...settings, provider: pendingProvider } : null
  const needsReload = staged !== null && providerNeedsReload(staged)

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = SECTIONS.findIndex((s) => s.id === section)
    const next = gridMove(i, e.key, SECTIONS.length, 1)
    if (next === null) return
    e.preventDefault()
    const id = SECTIONS[next].id
    setSection(id)
    document.getElementById(`${ids}-tab-${id}`)?.focus()
  }

  return (
    <div
      className="modal-backdrop modal-backdrop--center set-backdrop"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="set-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="set-dialog__head">
          <h2 id={titleId} className="set-dialog__title">
            Settings
          </h2>
          <button
            type="button"
            className="icon-btn"
            aria-label="Close settings"
            title="Close (Esc)"
            onClick={onClose}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </button>
        </div>

        <div className="set-dialog__main">
          <div
            className="set-nav"
            role="tablist"
            aria-orientation="vertical"
            aria-label="Settings sections"
          >
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                id={`${ids}-tab-${s.id}`}
                type="button"
                role="tab"
                className="set-nav__tab"
                aria-selected={section === s.id}
                aria-controls={`${ids}-panel`}
                tabIndex={section === s.id ? 0 : -1}
                onClick={() => setSection(s.id)}
                onKeyDown={onTabKeyDown}
              >
                <SectionIcon section={s.id} />
                {s.label}
              </button>
            ))}
          </div>

          <div
            id={`${ids}-panel`}
            className="set-panel"
            role="tabpanel"
            aria-labelledby={`${ids}-tab-${section}`}
          >
            {section === 'appearance' && <AppearanceSection settings={settings} update={update} />}
            {section === 'data' && (
              <DataSection
                current={settings.provider}
                pending={pendingProvider}
                needsReload={needsReload}
                onChoose={chooseProvider}
                onCancel={() => setPendingProvider(null)}
                onApply={() => {
                  if (staged) onChange(staged)
                }}
              />
            )}
            {section === 'time' && (
              <TimeSection
                timezone={settings.timezone}
                onChange={(tz) => update({ timezone: tz })}
              />
            )}
          </div>
        </div>

        <div className="set-dialog__foot">
          <span className="set-dialog__foot-note">
            {needsReload
              ? 'Data provider change waiting: apply it under Data.'
              : 'Changes apply as you make them.'}
          </span>
          <button type="button" className="btn btn--primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  )
}

/* ---------------------------------------- Appearance ---------------------------------------- */

function AppearanceSection({
  settings,
  update,
}: {
  settings: Settings
  update: (patch: Partial<Settings>) => void
}) {
  const ids = useId()
  const isDefault =
    settings.upColor === DEFAULT_UP_COLOR && settings.downColor === DEFAULT_DOWN_COLOR
  return (
    <>
      <section className="set-group" aria-labelledby={`${ids}-theme`}>
        <h3 id={`${ids}-theme`} className="set-group__title">
          Theme
        </h3>
        <ThemeSwitch value={settings.theme} onChange={(theme) => update({ theme })} />
      </section>

      <section className="set-group" aria-labelledby={`${ids}-candles`}>
        <div className="set-group__head">
          <h3 id={`${ids}-candles`} className="set-group__title">
            Candles
          </h3>
          <button
            type="button"
            className="set-link"
            disabled={isDefault}
            onClick={() => update({ upColor: DEFAULT_UP_COLOR, downColor: DEFAULT_DOWN_COLOR })}
          >
            Reset to defaults
          </button>
        </div>
        <ColorRow
          label="Up"
          hint="Rising bars, volume and gains"
          value={settings.upColor}
          onChange={(upColor) => update({ upColor })}
        />
        <ColorRow
          label="Down"
          hint="Falling bars, volume and losses"
          value={settings.downColor}
          onChange={(downColor) => update({ downColor })}
        />
        <p className="set-hint">
          Used by candles, hollow candles, Heikin-Ashi, volume, and the legend.
        </p>
      </section>

      <section className="set-group" aria-labelledby={`${ids}-preview`}>
        <h3 id={`${ids}-preview`} className="set-group__title">
          Preview
        </h3>
        <CandlePreview up={settings.upColor} down={settings.downColor} />
      </section>
    </>
  )
}

function ThemeSwitch({ value, onChange }: { value: ThemeName; onChange: (t: ThemeName) => void }) {
  const ids = useId()
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = THEME_OPTIONS.findIndex((t) => t.id === value)
    const next = gridMove(i, e.key, THEME_OPTIONS.length, THEME_OPTIONS.length)
    if (next === null) return
    e.preventDefault()
    const id = THEME_OPTIONS[next].id
    onChange(id)
    document.getElementById(`${ids}-${id}`)?.focus()
  }
  return (
    <div className="set-segmented" role="radiogroup" aria-label="Theme">
      {THEME_OPTIONS.map((t) => (
        <button
          key={t.id}
          id={`${ids}-${t.id}`}
          type="button"
          role="radio"
          className="set-segmented__opt"
          aria-checked={value === t.id}
          tabIndex={value === t.id ? 0 : -1}
          onClick={() => onChange(t.id)}
          onKeyDown={onKeyDown}
        >
          <span className={`set-theme-chip set-theme-chip--${t.id}`} aria-hidden="true">
            <span />
            <span />
          </span>
          {t.label}
        </button>
      ))}
    </div>
  )
}

function ColorRow({
  label,
  hint,
  value,
  onChange,
}: {
  label: string
  hint: string
  value: string
  onChange: (color: string) => void
}) {
  const ids = useId()
  const current = value.toLowerCase()
  const index = DRAWING_PALETTE.findIndex((p) => p.color === current)
  const custom = index === -1
  // Roving focus: the selected swatch (or the first one for a custom colour) is the tab stop.
  const tabStop = custom ? 0 : index

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const next = gridMove(i, e.key, DRAWING_PALETTE.length, SWATCH_COLUMNS)
    if (next === null) return
    e.preventDefault()
    onChange(DRAWING_PALETTE[next].color)
    document.getElementById(`${ids}-sw-${next}`)?.focus()
  }

  return (
    <div className="set-color-row">
      <div className="set-color-row__label">
        <span id={`${ids}-label`} className="set-color-row__name">
          {label}
        </span>
        <span className="set-color-row__hint">{hint}</span>
      </div>
      <div className="set-color-row__control">
        <div className="set-swatches" role="radiogroup" aria-labelledby={`${ids}-label`}>
          {DRAWING_PALETTE.map((p, i) => (
            <button
              key={p.color}
              id={`${ids}-sw-${i}`}
              type="button"
              role="radio"
              className="set-swatch"
              style={{ background: p.color }}
              aria-checked={i === index}
              aria-label={p.name}
              title={p.name}
              tabIndex={i === tabStop ? 0 : -1}
              onClick={() => onChange(p.color)}
              onKeyDown={(e) => onKeyDown(e, i)}
            />
          ))}
        </div>
        <label
          className={`set-custom-color${custom ? ' set-custom-color--active' : ''}`}
          title="Custom colour"
        >
          <input
            type="color"
            className="set-custom-color__input"
            value={current}
            aria-label={`Custom ${label.toLowerCase()} colour`}
            onChange={(e) => onChange(e.target.value.toLowerCase())}
          />
          <span className="set-custom-color__hex">{colorName(current)}</span>
        </label>
      </div>
    </div>
  )
}

/** Fixed bars for the preview: [open, high, low, close, volume 0..1]. */
const PREVIEW_BARS: readonly (readonly [number, number, number, number, number])[] = [
  [52, 58, 48, 56, 0.45],
  [56, 60, 53, 54, 0.35],
  [54, 55, 44, 46, 0.7],
  [46, 50, 42, 49, 0.5],
  [49, 57, 48, 56, 0.6],
  [56, 63, 55, 62, 0.8],
  [62, 64, 57, 58, 0.4],
  [58, 61, 52, 53, 0.55],
  [53, 59, 51, 58, 0.45],
  [58, 67, 57, 66, 0.9],
  [66, 69, 62, 63, 0.5],
  [63, 66, 59, 65, 0.4],
  [65, 72, 64, 71, 0.75],
  [71, 73, 66, 67, 0.6],
  [67, 70, 63, 69, 0.45],
  [69, 76, 68, 75, 0.85],
]

/** A miniature chart in the chosen colours on the current theme's background. */
function CandlePreview({ up, down }: { up: string; down: string }) {
  const width = 320
  const height = 112
  const priceTop = 8
  const priceBottom = 80
  const volBottom = height - 6
  const volHeight = 22
  const step = width / (PREVIEW_BARS.length + 1)
  const body = Math.max(3, step * 0.62)
  const lo = 40
  const hi = 78
  const y = (p: number) => priceBottom - ((p - lo) / (hi - lo)) * (priceBottom - priceTop)
  return (
    <svg
      className="set-preview"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Preview of candles and volume in the chosen colours"
      preserveAspectRatio="none"
    >
      {[0.25, 0.5, 0.75].map((f) => (
        <line
          key={f}
          className="set-preview__grid"
          x1={0}
          x2={width}
          y1={height * f}
          y2={height * f}
        />
      ))}
      {PREVIEW_BARS.map(([o, h, l, c, v], i) => {
        const x = step * (i + 1)
        const color = c >= o ? up : down
        const top = y(Math.max(o, c))
        const bottom = y(Math.min(o, c))
        return (
          <g key={i}>
            <rect
              x={x - body / 2}
              y={volBottom - v * volHeight}
              width={body}
              height={v * volHeight}
              fill={color}
              fillOpacity={0.45}
            />
            <line x1={x} x2={x} y1={y(h)} y2={y(l)} stroke={color} strokeWidth={1} />
            <rect
              x={x - body / 2}
              y={top}
              width={body}
              height={Math.max(1, bottom - top)}
              fill={color}
            />
          </g>
        )
      })}
    </svg>
  )
}

/* ------------------------------------------- Data ------------------------------------------- */

/** Focus the selected section tab (or the dialog itself). */
function focusSelectedTab(root: HTMLElement | null): void {
  if (!root) return
  const tab = root.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
  ;(tab ?? root).focus()
}

function DataSection({
  current,
  pending,
  needsReload,
  onChoose,
  onCancel,
  onApply,
}: {
  current: ProviderId
  pending: ProviderId | null
  needsReload: boolean
  onChoose: (id: ProviderId) => void
  onCancel: () => void
  onApply: () => void
}) {
  const ids = useId()
  const selected = pending ?? current
  const radioId = (id: ProviderId) => `${ids}-radio-${id}`
  const cancel = () => {
    // The focused Cancel button unmounts with the reload box: hand focus to the provider that
    // stays in use, before it goes.
    document.getElementById(radioId(current))?.focus()
    onCancel()
  }
  return (
    <>
      <section className="set-group" aria-labelledby={`${ids}-provider`}>
        <h3 id={`${ids}-provider`} className="set-group__title">
          Default data provider
        </h3>
        <div className="set-providers" role="radiogroup" aria-labelledby={`${ids}-provider`}>
          {PROVIDER_IDS.map((id) => (
            <label
              key={id}
              className={`set-provider${selected === id ? ' set-provider--selected' : ''}`}
            >
              <input
                id={radioId(id)}
                type="radio"
                className="set-provider__radio"
                name={`${ids}-provider`}
                value={id}
                checked={selected === id}
                onChange={() => onChoose(id)}
              />
              <span className="set-provider__body">
                <span className="set-provider__name">
                  {PROVIDER_LABELS[id]}
                  {id === current && <span className="set-tag">In use</span>}
                  {isDirectProvider(id) && <span className="set-tag set-tag--muted">Direct</span>}
                </span>
                <span className="set-provider__note">{PROVIDER_NOTES[id]}</span>
              </span>
            </label>
          ))}
        </div>
        <p className="set-hint">
          Direct providers turn off server features (drawings sync, alerts, cache). Changing the
          provider reloads the app; your other settings are kept.
        </p>
      </section>

      {needsReload && pending !== null && (
        <div className="set-reload" role="status">
          <span className="set-reload__text">
            Switch to <strong>{PROVIDER_LABELS[pending]}</strong>? The app reloads to apply it.
          </span>
          <span className="set-reload__actions">
            <button type="button" className="btn btn--ghost" onClick={cancel}>
              Cancel
            </button>
            <button type="button" className="btn btn--primary" onClick={onApply}>
              Apply &amp; reload
            </button>
          </span>
        </div>
      )}
    </>
  )
}

/* ------------------------------------------- Time ------------------------------------------- */

function TimeSection({ timezone, onChange }: { timezone: string; onChange: (tz: string) => void }) {
  const ids = useId()
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const known = TIMEZONE_CHOICES.some((c) => c.value === timezone)
  const choices = known
    ? TIMEZONE_CHOICES
    : [...TIMEZONE_CHOICES, { value: timezone, label: timezone }]
  return (
    <section className="set-group" aria-labelledby={`${ids}-tz`}>
      <h3 id={`${ids}-tz`} className="set-group__title">
        Time zone
      </h3>
      <div className="set-field">
        <label className="set-field__label" htmlFor={`${ids}-tz-select`}>
          Show times in
        </label>
        <select
          id={`${ids}-tz-select`}
          className="set-select"
          value={timezone}
          onChange={(e) => onChange(e.target.value)}
        >
          {choices.map((c) => (
            <option key={c.value} value={c.value}>
              {c.value === 'UTC' || c.value === 'local' ? c.label : `${c.label} (${c.value})`}
            </option>
          ))}
        </select>
      </div>
      <div className="set-clock" aria-live="off">
        <span className="set-clock__label">Now</span>
        <span className="set-clock__time">{formatDateTime(now, timezone, { seconds: true })}</span>
        <span className="set-clock__zone">{timezoneLabel(timezone, now)}</span>
      </div>
      <p className="set-hint">
        Bars stay aligned to UTC, so a daily bar opens at 00:00 UTC in every zone. Only the time
        axis, crosshair, and legend labels change.
      </p>
    </section>
  )
}

/* ------------------------------------------ Icons ------------------------------------------- */

function SectionIcon({ section }: { section: Section }) {
  const common = {
    width: 18,
    height: 18,
    viewBox: '0 0 18 18',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.3,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': true,
    focusable: false,
  } as const
  switch (section) {
    case 'appearance':
      return (
        <svg {...common}>
          <path d="M5 3.5v11M13 3.5v11" />
          <rect x="3.5" y="6" width="3" height="5" rx="0.5" />
          <rect x="11.5" y="5" width="3" height="6.5" rx="0.5" />
        </svg>
      )
    case 'data':
      return (
        <svg {...common}>
          <ellipse cx="9" cy="4.5" rx="5.5" ry="2" />
          <path d="M3.5 4.5v9c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2v-9M3.5 9c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2" />
        </svg>
      )
    case 'time':
      return (
        <svg {...common}>
          <circle cx="9" cy="9" r="6.5" />
          <path d="M9 5.2V9l2.6 1.6" />
        </svg>
      )
  }
}
