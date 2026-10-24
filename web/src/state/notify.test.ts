import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AlertEvent } from '../data/alertTypes'
import {
  notificationPermission,
  requestNotificationPermission,
  showBrowserNotification,
} from './notify'

const EVENT: AlertEvent = {
  id: 42,
  alertId: 'a1',
  symbol: 'ETHUSDT',
  condition: 'crosses_below',
  level: 3000,
  price: 2999.5,
  triggeredAt: 1_700_000_000,
  message: 'ETHUSDT crossing down 3,000 (last 2,999.5)',
}

interface Created {
  title: string
  options: NotificationOptions | undefined
  instance: FakeNotificationInstance
}

interface FakeNotificationInstance {
  onclick: (() => void) | null
  close: ReturnType<typeof vi.fn>
}

function installNotification(
  permission: NotificationPermission,
  opts: {
    requestResult?: NotificationPermission
    callbackOnly?: boolean
    throwOnNew?: boolean
  } = {},
) {
  const created: Created[] = []
  const requestPermission = vi.fn((cb?: (p: NotificationPermission) => void) => {
    const result = opts.requestResult ?? permission
    FakeNotification.permission = result
    if (opts.callbackOnly) {
      cb?.(result)
      return undefined
    }
    return Promise.resolve(result)
  })
  class FakeNotification {
    static permission: NotificationPermission = permission
    static requestPermission = requestPermission
    onclick: (() => void) | null = null
    close = vi.fn()
    constructor(title: string, options?: NotificationOptions) {
      if (opts.throwOnNew) throw new TypeError('Illegal constructor')
      created.push({ title, options, instance: this })
    }
  }
  vi.stubGlobal('Notification', FakeNotification)
  return { created, requestPermission }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('notificationPermission', () => {
  it('is unsupported without the Notification API', () => {
    vi.stubGlobal('Notification', undefined)
    expect(notificationPermission()).toBe('unsupported')
  })

  it.each(['granted', 'denied', 'default'] as const)('reports %s', (p) => {
    installNotification(p)
    expect(notificationPermission()).toBe(p)
  })
})

describe('requestNotificationPermission', () => {
  it('returns unsupported without the API', async () => {
    vi.stubGlobal('Notification', undefined)
    await expect(requestNotificationPermission()).resolves.toBe('unsupported')
  })

  it('prompts from default and resolves the answer (promise form)', async () => {
    const { requestPermission } = installNotification('default', { requestResult: 'granted' })
    await expect(requestNotificationPermission()).resolves.toBe('granted')
    expect(requestPermission).toHaveBeenCalledTimes(1)
  })

  it('supports the old callback form', async () => {
    installNotification('default', { requestResult: 'denied', callbackOnly: true })
    await expect(requestNotificationPermission()).resolves.toBe('denied')
  })

  it('does not prompt again once granted or denied', async () => {
    const granted = installNotification('granted')
    await expect(requestNotificationPermission()).resolves.toBe('granted')
    expect(granted.requestPermission).not.toHaveBeenCalled()
    const denied = installNotification('denied')
    await expect(requestNotificationPermission()).resolves.toBe('denied')
    expect(denied.requestPermission).not.toHaveBeenCalled()
  })
})

describe('showBrowserNotification', () => {
  it('shows "<SYMBOL> alert" with the event message and a per-event tag when granted', () => {
    const { created } = installNotification('granted')
    expect(showBrowserNotification(EVENT)).toBe(true)
    expect(created).toHaveLength(1)
    expect(created[0].title).toBe('ETHUSDT alert')
    expect(created[0].options).toEqual({ body: EVENT.message, tag: 'alert-42' })
  })

  it('focuses the window and closes the notification on click', () => {
    const { created } = installNotification('granted')
    const focus = vi.fn()
    vi.stubGlobal('window', { focus })
    showBrowserNotification(EVENT)
    created[0].instance.onclick?.()
    expect(focus).toHaveBeenCalledTimes(1)
    expect(created[0].instance.close).toHaveBeenCalledTimes(1)
  })

  it.each(['denied', 'default'] as const)('returns false when permission is %s', (p) => {
    const { created } = installNotification(p)
    expect(showBrowserNotification(EVENT)).toBe(false)
    expect(created).toHaveLength(0)
  })

  it('returns false when unsupported', () => {
    vi.stubGlobal('Notification', undefined)
    expect(showBrowserNotification(EVENT)).toBe(false)
  })

  it('returns false when the constructor throws', () => {
    installNotification('granted', { throwOnNew: true })
    expect(showBrowserNotification(EVENT)).toBe(false)
  })
})
