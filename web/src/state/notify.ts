/**
 * Browser Notification API helpers for triggered alerts. Shown only while a tab is open; the
 * server sends desktop/webhook notifications itself when the browser is closed.
 */

import type { AlertEvent, NotifyPermission } from '../data/alertTypes'

type NotificationCtor = typeof Notification

function notificationApi(): NotificationCtor | null {
  try {
    const N = (globalThis as { Notification?: unknown }).Notification
    return typeof N === 'function' ? (N as NotificationCtor) : null
  } catch {
    return null
  }
}

function normalize(p: unknown): NotifyPermission {
  return p === 'granted' || p === 'denied' ? p : 'default'
}

/** Current permission, or 'unsupported' when the browser has no Notification API. */
export function notificationPermission(): NotifyPermission {
  const N = notificationApi()
  if (N === null) return 'unsupported'
  try {
    return normalize(N.permission)
  } catch {
    return 'default'
  }
}

/**
 * Ask for permission (call it from a click handler). Resolves the resulting permission; an
 * already granted or denied permission is returned without prompting again. Handles both the
 * promise form and the old Safari callback form of `requestPermission`. Never rejects.
 */
export async function requestNotificationPermission(): Promise<NotifyPermission> {
  const N = notificationApi()
  if (N === null) return 'unsupported'
  const current = notificationPermission()
  if (current !== 'default') return current
  try {
    const result = await new Promise<unknown>((resolve) => {
      const maybe: unknown = N.requestPermission((p) => resolve(p))
      if (maybe !== undefined && typeof (maybe as Promise<unknown>).then === 'function') {
        ;(maybe as Promise<unknown>).then(resolve, () => resolve(undefined))
      }
    })
    return result === undefined ? notificationPermission() : normalize(result)
  } catch {
    return notificationPermission()
  }
}

/**
 * Shows a Notification (title "<SYMBOL> alert", body event.message, tag `alert-<event.id>` so
 * several tabs collapse into one). Returns false when unsupported or not granted. Clicking it
 * focuses the window.
 */
export function showBrowserNotification(event: AlertEvent): boolean {
  const N = notificationApi()
  if (N === null || notificationPermission() !== 'granted') return false
  try {
    const n = new N(`${event.symbol} alert`, { body: event.message, tag: `alert-${event.id}` })
    n.onclick = () => {
      try {
        if (typeof window !== 'undefined') window.focus()
      } catch {
        // Focus can be refused; nothing else to do.
      }
      n.close()
    }
    return true
  } catch {
    // e.g. Chrome on Android only allows notifications from a service worker.
    return false
  }
}
