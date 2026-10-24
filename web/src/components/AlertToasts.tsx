import { useEffect, useRef, useState } from 'react'
import { ALERT_CONDITION_LABELS, type AlertEvent } from '../data/alertTypes'
import { formatUtcTime } from './alertsView'
import './Alerts.css'

export interface AlertToast {
  key: string
  event: AlertEvent
}

export interface AlertToastsProps {
  toasts: readonly AlertToast[]
  onDismiss(key: string): void
  onOpen?(event: AlertEvent): void
}

export const TOAST_DURATION_MS = 10_000
export const MAX_VISIBLE_TOASTS = 4

/**
 * In-app alert toasts, bottom-right above the status bar. Each dismisses itself after 10 s;
 * hovering or focusing it pauses the countdown. The newest four are shown.
 */
export function AlertToasts({ toasts, onDismiss, onOpen }: AlertToastsProps) {
  const visible = toasts.slice(-MAX_VISIBLE_TOASTS)
  const hidden = toasts.length - visible.length
  return (
    <div
      className="alert-toasts"
      aria-live="assertive"
      aria-relevant="additions"
      role="region"
      aria-label="Alert notifications"
    >
      {hidden > 0 && <div className="alert-toasts__more">+{hidden} more</div>}
      {visible.map((t) => (
        <Toast key={t.key} toast={t} onDismiss={onDismiss} onOpen={onOpen} />
      ))}
    </div>
  )
}

function Toast({
  toast,
  onDismiss,
  onOpen,
}: {
  toast: AlertToast
  onDismiss(key: string): void
  onOpen?(event: AlertEvent): void
}) {
  const { event, key } = toast
  const [paused, setPaused] = useState(false)
  const remaining = useRef(TOAST_DURATION_MS)
  const onDismissRef = useRef(onDismiss)
  useEffect(() => {
    onDismissRef.current = onDismiss
  })

  // Count down only while not paused; keep the time left across pauses.
  useEffect(() => {
    if (paused) return
    const started = performance.now()
    const id = window.setTimeout(() => onDismissRef.current(key), remaining.current)
    return () => {
      window.clearTimeout(id)
      remaining.current = Math.max(0, remaining.current - (performance.now() - started))
    }
  }, [paused, key])

  const body = (
    <>
      <span className="alert-toast__title">
        <span className="alert-toast__symbol">{event.symbol}</span>
        <span className="alert-toast__cond">{ALERT_CONDITION_LABELS[event.condition]}</span>
      </span>
      <span className="alert-toast__message">{event.message}</span>
      <time
        className="alert-toast__time"
        dateTime={new Date(event.triggeredAt * 1000).toISOString()}
      >
        {formatUtcTime(event.triggeredAt)} UTC
      </time>
    </>
  )

  return (
    <div
      className={`alert-toast${paused ? ' alert-toast--paused' : ''}`}
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPaused(false)
      }}
      style={{ ['--toast-ms' as string]: `${TOAST_DURATION_MS}ms` }}
    >
      <span className="alert-toast__icon" aria-hidden="true">
        <svg
          width="18"
          height="18"
          viewBox="0 0 22 22"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M6 15.5V10a5 5 0 0 1 10 0v5.5l1.5 1.5h-13zM9.2 17.5a1.9 1.9 0 0 0 3.6 0" />
          <path d="M17 4.5l1.5-1.2M5 4.5L3.5 3.3" />
        </svg>
      </span>
      {onOpen ? (
        <button
          type="button"
          className="alert-toast__body alert-toast__body--button"
          title={`Open ${event.symbol}`}
          onClick={() => {
            onOpen(event)
            onDismiss(key)
          }}
        >
          {body}
        </button>
      ) : (
        <div className="alert-toast__body">{body}</div>
      )}
      <button
        type="button"
        className="alert-toast__close"
        aria-label="Dismiss notification"
        title="Dismiss"
        onClick={() => onDismiss(key)}
      >
        <svg width="12" height="12" viewBox="0 0 14 14" aria-hidden="true">
          <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.6" />
        </svg>
      </button>
      <span className="alert-toast__progress" aria-hidden="true" />
    </div>
  )
}
