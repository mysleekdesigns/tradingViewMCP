import { memo, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import type { Alert, AlertEvent, AlertsStatus, NotifyPermission } from '../data/alertTypes'
import {
  ALERT_STATE_LABELS,
  FREQUENCY_LABELS,
  alertState,
  describeAlert,
  eventsNewestFirst,
  formatUtcSeconds,
  groupAlerts,
  relativeTime,
} from './alertsView'
import { formatUtc } from './quote'
import './Alerts.css'

export interface AlertsPanelProps {
  alerts: readonly Alert[]
  events: readonly AlertEvent[]
  status: AlertsStatus
  activeSymbol: string
  notificationPermission: NotifyPermission
  onRequestPermission(): void
  onCreate(): void
  onSelectSymbol(symbol: string): void
  onEdit(alert: Alert): void
  onToggleActive(alert: Alert): void
  onDelete(alert: Alert): void
  onClearLog(): void
  formatPrice(symbol: string, price: number): string
}

type Tab = 'alerts' | 'log'

/** Current UNIX seconds, refreshed every `periodMs` (for "2 min ago" labels). */
function useNowSeconds(periodMs: number): number {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const id = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), periodMs)
    return () => window.clearInterval(id)
  }, [periodMs])
  return now
}

/**
 * Sidebar panel: the user's price alerts (Active, then Stopped) and the log of triggered
 * alerts. Alerts are evaluated on the local server; without it the panel explains how to start it.
 */
export const AlertsPanel = memo(function AlertsPanel({
  alerts,
  events,
  status,
  activeSymbol,
  notificationPermission,
  onRequestPermission,
  onCreate,
  onSelectSymbol,
  onEdit,
  onToggleActive,
  onDelete,
  onClearLog,
  formatPrice,
}: AlertsPanelProps) {
  'use memo'
  const [tab, setTab] = useState<Tab>('alerts')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [confirmClear, setConfirmClear] = useState(false)
  const ids = useId()
  const tabId = (t: Tab) => `${ids}-tab-${t}`
  const panelId = (t: Tab) => `${ids}-panel-${t}`
  const now = useNowSeconds(30_000)

  const grouped = useMemo(() => groupAlerts(alerts, activeSymbol), [alerts, activeSymbol])
  const sortedEvents = useMemo(() => eventsNewestFirst(events), [events])
  const ready = status === 'ready'

  const tabs: readonly { id: Tab; label: string; count: number }[] = [
    { id: 'alerts', label: 'Alerts', count: alerts.length },
    { id: 'log', label: 'Log', count: events.length },
  ]

  const onTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const next: Tab =
      e.key === 'Home' ? 'alerts' : e.key === 'End' ? 'log' : tab === 'alerts' ? 'log' : 'alerts'
    setTab(next)
    document.getElementById(tabId(next))?.focus()
  }

  return (
    <section className="alerts" aria-labelledby={`${ids}-title`}>
      <header className="alerts__header">
        <h2 id={`${ids}-title`} className="alerts__title">
          Alerts
        </h2>
        {ready && grouped.active.length > 0 && (
          <span className="alerts__count" title="Active alerts">
            {grouped.active.length}
          </span>
        )}
        <span className="alerts__spacer" />
        <button
          type="button"
          className="alerts__icon-btn"
          aria-label={`Create alert on ${activeSymbol}`}
          aria-keyshortcuts="Alt+A"
          title={ready ? 'Create alert (Alt+A)' : 'Alerts need the local server'}
          disabled={!ready}
          onClick={onCreate}
        >
          <PlusIcon />
        </button>
      </header>

      {status === 'unavailable' ? (
        <UnavailableState />
      ) : (
        <>
          <div className="alerts__tabs" role="tablist" aria-label="Alerts view">
            {tabs.map((t) => (
              <button
                key={t.id}
                id={tabId(t.id)}
                type="button"
                role="tab"
                className="alerts__tab"
                aria-selected={tab === t.id}
                aria-controls={tab === t.id ? panelId(t.id) : undefined}
                tabIndex={tab === t.id ? 0 : -1}
                onClick={() => setTab(t.id)}
                onKeyDown={onTabKeyDown}
              >
                {t.label}
                {ready && t.count > 0 && <span className="alerts__tab-count">{t.count}</span>}
              </button>
            ))}
          </div>

          <PermissionBanner permission={notificationPermission} onRequest={onRequestPermission} />

          <div
            id={panelId(tab)}
            className="alerts__body"
            role="tabpanel"
            aria-labelledby={tabId(tab)}
            aria-busy={status === 'loading'}
          >
            {status === 'loading' ? (
              <LoadingRows />
            ) : tab === 'alerts' ? (
              alerts.length === 0 ? (
                <EmptyState
                  title="No alerts yet"
                  text="Right-click the chart or press Alt+A to add an alert."
                  action={{ label: `Create alert on ${activeSymbol}`, run: onCreate }}
                />
              ) : (
                <>
                  <AlertGroup
                    title="Active"
                    alerts={grouped.active}
                    {...{ activeSymbol, formatPrice, onSelectSymbol, onEdit, onToggleActive }}
                    confirmDelete={confirmDelete}
                    setConfirmDelete={setConfirmDelete}
                    onDelete={onDelete}
                  />
                  <AlertGroup
                    title="Stopped"
                    alerts={grouped.stopped}
                    {...{ activeSymbol, formatPrice, onSelectSymbol, onEdit, onToggleActive }}
                    confirmDelete={confirmDelete}
                    setConfirmDelete={setConfirmDelete}
                    onDelete={onDelete}
                  />
                </>
              )
            ) : sortedEvents.length === 0 ? (
              <EmptyState
                title="No triggered alerts"
                text="When an alert fires it is listed here, newest first."
              />
            ) : (
              <ol className="alerts__log" aria-label="Triggered alerts">
                {sortedEvents.map((ev) => (
                  <li key={ev.id} className="alerts__event">
                    <span className="alerts__event-icon" aria-hidden="true">
                      <BellIcon size={14} />
                    </span>
                    <div className="alerts__event-main">
                      <p className="alerts__event-message">{ev.message}</p>
                      <div className="alerts__event-meta">
                        <button
                          type="button"
                          className="alerts__symbol alerts__symbol--small"
                          title={`Open ${ev.symbol}`}
                          onClick={() => onSelectSymbol(ev.symbol)}
                        >
                          {ev.symbol}
                        </button>
                        <time
                          className="alerts__time"
                          dateTime={new Date(ev.triggeredAt * 1000).toISOString()}
                          title={`${formatUtcSeconds(ev.triggeredAt)} UTC`}
                        >
                          {formatUtcSeconds(ev.triggeredAt)} UTC
                        </time>
                        <span className="alerts__ago">{relativeTime(now, ev.triggeredAt)}</span>
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {ready && tab === 'log' && sortedEvents.length > 0 && (
            <ClearLogFooter
              count={sortedEvents.length}
              open={confirmClear}
              setOpen={setConfirmClear}
              onClear={onClearLog}
            />
          )}
        </>
      )}
    </section>
  )
})

interface AlertGroupProps {
  title: string
  alerts: readonly Alert[]
  activeSymbol: string
  formatPrice(symbol: string, price: number): string
  onSelectSymbol(symbol: string): void
  onEdit(alert: Alert): void
  onToggleActive(alert: Alert): void
  onDelete(alert: Alert): void
  confirmDelete: string | null
  setConfirmDelete(id: string | null): void
}

function AlertGroup({ title, alerts, ...rest }: AlertGroupProps) {
  const headId = useId()
  if (alerts.length === 0) return null
  return (
    <div className="alerts__group">
      <h3 id={headId} className="alerts__group-title">
        {title}
        <span className="alerts__group-count">{alerts.length}</span>
      </h3>
      <ul className="alerts__list" aria-labelledby={headId}>
        {alerts.map((a) => (
          <AlertRow key={a.id} alert={a} {...rest} />
        ))}
      </ul>
    </div>
  )
}

function AlertRow({
  alert,
  activeSymbol,
  formatPrice,
  onSelectSymbol,
  onEdit,
  onToggleActive,
  onDelete,
  confirmDelete,
  setConfirmDelete,
}: Omit<AlertGroupProps, 'title' | 'alerts'> & { alert: Alert }) {
  const state = alertState(alert)
  const confirming = confirmDelete === alert.id
  const confirmRef = useRef<HTMLButtonElement>(null)
  const deleteRef = useRef<HTMLButtonElement>(null)
  const description = describeAlert(alert, (p) => formatPrice(alert.symbol, p))

  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
  }, [confirming])

  const cancelConfirm = () => {
    setConfirmDelete(null)
    // The delete button re-renders after the confirm closes.
    requestAnimationFrame(() => deleteRef.current?.focus())
  }

  const classes = [
    'alert-row',
    `alert-row--${state}`,
    alert.symbol === activeSymbol ? 'alert-row--current' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <li className={classes}>
      <div className="alert-row__top">
        <button
          type="button"
          className="alerts__symbol"
          title={`Open ${alert.symbol}`}
          onClick={() => onSelectSymbol(alert.symbol)}
        >
          {alert.symbol}
        </button>
        <span className={`alert-state alert-state--${state}`}>{ALERT_STATE_LABELS[state]}</span>
        <span className="alerts__spacer" />
        {!confirming && (
          <span className="alert-row__actions">
            <button
              type="button"
              className="alert-row__btn"
              aria-label={`${alert.active ? 'Pause' : 'Resume'} alert ${alert.symbol} ${description}`}
              title={alert.active ? 'Pause' : 'Resume (re-arms the alert)'}
              onClick={() => onToggleActive(alert)}
            >
              {alert.active ? <PauseIcon /> : <PlayIcon />}
            </button>
            <button
              type="button"
              className="alert-row__btn"
              aria-label={`Edit alert ${alert.symbol} ${description}`}
              title="Edit"
              onClick={() => onEdit(alert)}
            >
              <EditIcon />
            </button>
            <button
              ref={deleteRef}
              type="button"
              className="alert-row__btn alert-row__btn--danger"
              aria-label={`Delete alert ${alert.symbol} ${description}`}
              title="Delete"
              onClick={() => setConfirmDelete(alert.id)}
            >
              <TrashSmallIcon />
            </button>
          </span>
        )}
      </div>

      <div className="alert-row__cond">
        <span className="alert-row__desc">{description}</span>
        <span className="alert-row__freq">{FREQUENCY_LABELS[alert.frequency]}</span>
      </div>

      {alert.note !== '' && <p className="alert-row__note">{alert.note}</p>}

      {alert.lastTriggeredAt !== null && (
        <p className="alert-row__last">
          Last triggered{' '}
          <time dateTime={new Date(alert.lastTriggeredAt * 1000).toISOString()}>
            {formatUtc(alert.lastTriggeredAt)} UTC
          </time>
        </p>
      )}

      {confirming && (
        <div
          className="inline-confirm"
          role="group"
          aria-label={`Delete alert ${alert.symbol}?`}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
              e.stopPropagation()
              cancelConfirm()
            }
          }}
        >
          <span className="inline-confirm__text">Delete this alert?</span>
          <button type="button" className="inline-confirm__btn" onClick={cancelConfirm}>
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="inline-confirm__btn inline-confirm__btn--danger"
            onClick={() => {
              setConfirmDelete(null)
              onDelete(alert)
            }}
          >
            Delete
          </button>
        </div>
      )}
    </li>
  )
}

function ClearLogFooter({
  count,
  open,
  setOpen,
  onClear,
}: {
  count: number
  open: boolean
  setOpen(open: boolean): void
  onClear(): void
}) {
  const clearRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (open) cancelRef.current?.focus()
  }, [open])

  const close = () => {
    setOpen(false)
    requestAnimationFrame(() => clearRef.current?.focus())
  }

  return (
    <div className="alerts__foot">
      {open ? (
        <div
          className="inline-confirm inline-confirm--foot"
          role="group"
          aria-label="Clear the alert log?"
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
              e.stopPropagation()
              close()
            }
          }}
        >
          <span className="inline-confirm__text">
            Clear {count} {count === 1 ? 'entry' : 'entries'}?
          </span>
          <button ref={cancelRef} type="button" className="inline-confirm__btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="inline-confirm__btn inline-confirm__btn--danger"
            onClick={() => {
              setOpen(false)
              onClear()
            }}
          >
            Clear
          </button>
        </div>
      ) : (
        <button
          ref={clearRef}
          type="button"
          className="alerts__text-btn"
          onClick={() => setOpen(true)}
        >
          Clear log
        </button>
      )}
    </div>
  )
}

function PermissionBanner({
  permission,
  onRequest,
}: {
  permission: NotifyPermission
  onRequest(): void
}) {
  if (permission === 'default') {
    return (
      <div className="alerts__banner">
        <BellIcon size={16} />
        <span className="alerts__banner-text">Get a desktop notification when an alert fires.</span>
        <button type="button" className="alerts__banner-btn" onClick={onRequest}>
          Enable desktop notifications
        </button>
      </div>
    )
  }
  if (permission === 'denied') {
    return (
      <div className="alerts__banner alerts__banner--warn">
        <BellOffIcon />
        <span className="alerts__banner-text">
          Desktop notifications are blocked. Allow them for this site in the browser&apos;s site
          settings (the icon left of the address bar), then reload. In-app toasts still show.
        </span>
      </div>
    )
  }
  return null
}

function UnavailableState() {
  return (
    <div className="alerts__empty alerts__empty--unavailable">
      <span className="alerts__empty-icon" aria-hidden="true">
        <svg
          width="28"
          height="28"
          viewBox="0 0 28 28"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
        >
          <rect x="5" y="5" width="18" height="7" rx="1.5" />
          <rect x="5" y="16" width="18" height="7" rx="1.5" />
          <path d="M9 8.5h.01M9 19.5h.01" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </span>
      <p className="alerts__empty-title">Local server not running</p>
      <p className="alerts__empty-text">
        Alerts run on the local server. Start it with <code>npm run dev</code> at the repo root.
      </p>
    </div>
  )
}

function EmptyState({
  title,
  text,
  action,
}: {
  title: string
  text: string
  action?: { label: string; run(): void }
}) {
  return (
    <div className="alerts__empty">
      <span className="alerts__empty-icon" aria-hidden="true">
        <BellIcon size={26} />
      </span>
      <p className="alerts__empty-title">{title}</p>
      <p className="alerts__empty-text">{text}</p>
      {action && (
        <button type="button" className="btn btn--ghost alerts__empty-btn" onClick={action.run}>
          <PlusIcon />
          {action.label}
        </button>
      )}
    </div>
  )
}

function LoadingRows() {
  return (
    <div className="alerts__loading" role="status">
      <span className="visually-hidden">Loading alerts…</span>
      {[0, 1, 2].map((i) => (
        <div key={i} className="alerts__skeleton" aria-hidden="true">
          <span className="alerts__skeleton-line" style={{ width: '38%' }} />
          <span
            className="alerts__skeleton-line alerts__skeleton-line--faint"
            style={{ width: '64%' }}
          />
        </div>
      ))}
    </div>
  )
}

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

function BellIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" {...STROKE}>
      <path d="M6 15.5V10a5 5 0 0 1 10 0v5.5l1.5 1.5h-13zM9.2 17.5a1.9 1.9 0 0 0 3.6 0" />
    </svg>
  )
}

function BellOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 22 22" {...STROKE}>
      <path d="M6 15.5V10a5 5 0 0 1 8.2-3.8M16 10v5.5l1.5 1.5h-13M9.2 17.5a1.9 1.9 0 0 0 3.6 0M4 4l14 14" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" {...STROKE}>
      <path d="M8 3v10M3 8h10" />
    </svg>
  )
}

function PauseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M5 3.5v7M9 3.5v7" />
    </svg>
  )
}

function PlayIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M4.5 3.2v7.6L10.8 7z" />
    </svg>
  )
}

function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M2.5 11.5l.6-2.6 6.3-6.3 2 2-6.3 6.3zM8.2 3.8l2 2" />
    </svg>
  )
}

function TrashSmallIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" {...STROKE}>
      <path d="M2.5 4h9M5.5 4V2.5h3V4M3.8 4l.6 7.5h5.2l.6-7.5" />
    </svg>
  )
}
