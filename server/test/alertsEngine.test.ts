import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createAlertEngine,
  desktopNotification,
  webhookPayload,
  WEBHOOK_TEXT_MAX_CHARS,
  type AlertEngine,
  type FiredAlert,
} from '../src/alerts/engine.ts'
import { createAlertStore, type AlertStore } from '../src/alerts/store.ts'
import type {
  Alert,
  AlertNotifier,
  AlertStreamMessage,
  DesktopNotifyMode,
  WebhookPayload,
} from '../src/alerts/types.ts'
import { openDb, type Db } from '../src/db/index.ts'
import type { Ticker } from '../src/types.ts'
import { TICKERS_KEY, type HubListener } from '../src/upstream/hub.ts'

const T0_MS = 1_700_000_000_000

/** Records acquires/releases; the test pushes ticker batches to the held listeners. */
class FakeHub {
  listeners = new Set<HubListener>()
  keys: string[] = []
  acquires = 0
  releases = 0
  acquire(key: string, listener: HubListener): () => void {
    this.keys.push(key)
    this.acquires += 1
    this.listeners.add(listener)
    listener({ type: 'status', status: 'reconnecting' })
    let done = false
    return () => {
      if (done) return
      done = true
      this.releases += 1
      this.listeners.delete(listener)
    }
  }
  get held(): number {
    return this.listeners.size
  }
  push(...prices: [string, number][]): void {
    const tickers: Ticker[] = prices.map(([symbol, last]) => ({
      symbol,
      last,
      open: last,
      high: last,
      low: last,
      volume: 1,
      quoteVolume: last,
      changePct: 0,
      time: 0,
    }))
    for (const l of [...this.listeners]) l({ type: 'tickers', tickers })
  }
}

function newAlert(id: string, over: Partial<Alert> = {}): Alert {
  return {
    id,
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    price: 65000,
    frequency: 'once',
    active: true,
    note: '',
    createdAt: T0_MS / 1000,
    updatedAt: T0_MS / 1000,
    lastTriggeredAt: null,
    ...over,
  }
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await new Promise<void>((r) => setImmediate(r))
}

describe('alert engine', () => {
  let db: Db
  let store: AlertStore
  let hub: FakeHub
  let clock: number
  let engine: AlertEngine
  let desktop: ReturnType<typeof vi.fn<(title: string, body: string) => Promise<void>>>
  let webhook: ReturnType<typeof vi.fn<(p: WebhookPayload) => Promise<void>>>
  let warnings: string[]

  const make = (mode: DesktopNotifyMode = 'auto', notifier?: AlertNotifier, cooldownSec = 60) => {
    engine = createAlertEngine({
      store,
      hub,
      now: () => clock,
      cooldownSec,
      desktopMode: mode,
      notifier: notifier ?? { desktop, webhook },
      log: { warn: (_o, msg) => warnings.push(msg) },
    })
    return engine
  }

  beforeEach(() => {
    db = openDb(':memory:')
    store = createAlertStore(db)
    hub = new FakeHub()
    clock = T0_MS
    desktop = vi.fn<(title: string, body: string) => Promise<void>>(() => Promise.resolve())
    webhook = vi.fn<(p: WebhookPayload) => Promise<void>>(() => Promise.resolve())
    warnings = []
  })

  afterEach(() => {
    engine?.close()
    db.close()
  })

  it('holds the tickers key only while at least one active alert exists', () => {
    make()
    engine.sync()
    expect(hub.acquires).toBe(0)
    expect(engine.watching).toBe(false)

    store.insert(newAlert('a'))
    engine.sync()
    expect(hub.keys).toEqual([TICKERS_KEY])
    expect(hub.held).toBe(1)
    store.insert(newAlert('b'))
    engine.sync()
    expect(hub.acquires).toBe(1) // not re-acquired

    store.update('a', { active: false }, 1)
    engine.sync()
    expect(hub.held).toBe(1)
    store.delete('b')
    engine.sync()
    expect(hub.held).toBe(0)
    expect(hub.releases).toBe(1)
    expect(engine.watching).toBe(false)
  })

  it('starts watching alerts that were already active in the DB', () => {
    store.insert(newAlert('a'))
    make().sync()
    expect(hub.held).toBe(1)
  })

  it('releases the hub on close', () => {
    store.insert(newAlert('a'))
    make().sync()
    engine.close()
    expect(hub.held).toBe(0)
    engine.sync()
    expect(hub.held).toBe(0)
  })

  it('a once trigger records the event, deactivates the alert, pushes triggered + changed, releases at zero', async () => {
    store.insert(newAlert('a', { note: 'take profit' }))
    make('always').sync()
    const msgs: AlertStreamMessage[] = []
    engine.subscribe((m) => msgs.push(m))

    hub.push(['BTCUSDT', 64990])
    expect(msgs).toEqual([])
    clock = T0_MS + 61_500
    hub.push(['ETHUSDT', 70000], ['BTCUSDT', 65012.5])

    const events = store.listEvents(10)
    expect(events).toEqual([
      {
        id: 1,
        alertId: 'a',
        symbol: 'BTCUSDT',
        condition: 'crosses_above',
        level: 65000,
        price: 65012.5,
        triggeredAt: T0_MS / 1000 + 61,
        message: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
      },
    ])
    const after = store.get('a')!
    expect(after.active).toBe(false)
    expect(after.lastTriggeredAt).toBe(T0_MS / 1000 + 61)
    expect(msgs).toEqual([
      { type: 'triggered', event: events[0], alert: after },
      { type: 'changed' },
    ])
    // No active alerts left: the tickers stream is released.
    expect(hub.held).toBe(0)

    await flush()
    expect(desktop).toHaveBeenCalledTimes(1)
    expect(desktop).toHaveBeenCalledWith(
      'Alert: BTCUSDT',
      'BTCUSDT crossing up 65,000 (last 65,012.5)\nNote: take profit',
    )
    expect(webhook).toHaveBeenCalledTimes(1)
    const text = 'BTCUSDT crossing up 65,000 (last 65,012.5)\nNote: take profit'
    // Single trigger: exactly the original shape, plus allowed_mentions (no `events`).
    expect(webhook).toHaveBeenCalledWith({
      text,
      content: text,
      event: events[0],
      alert: after,
      allowed_mentions: { parse: [] },
    })
  })

  it('an alert created after a price was seen does not compare against the old price', () => {
    store.insert(newAlert('other', { symbol: 'BTCUSDT', condition: 'greater', price: 1e9 }))
    make().sync()
    hub.push(['BTCUSDT', 64000])
    store.insert(newAlert('a'))
    engine.sync()
    hub.push(['BTCUSDT', 66000]) // first observation for 'a': no cross
    expect(store.listEvents(10)).toEqual([])
    hub.push(['BTCUSDT', 64000], ['BTCUSDT', 65000])
    expect(store.listEvents(10)).toHaveLength(1)
  })

  it('every alerts stay active and respect the cooldown', () => {
    store.insert(newAlert('a', { frequency: 'every' }))
    make('off', undefined, 60).sync()
    const crossUp = (): void => hub.push(['BTCUSDT', 64000], ['BTCUSDT', 66000])
    crossUp()
    expect(store.listEvents(10)).toHaveLength(1)
    clock += 30_000
    crossUp()
    expect(store.listEvents(10)).toHaveLength(1)
    clock += 30_000
    crossUp()
    expect(store.listEvents(10)).toHaveLength(2)
    expect(store.get('a')!.active).toBe(true)
    expect(hub.held).toBe(1)
  })

  it('reset() re-arms a greater alert', () => {
    store.insert(newAlert('a', { condition: 'greater', frequency: 'every' }))
    make('off', undefined, 0).sync()
    hub.push(['BTCUSDT', 66000])
    hub.push(['BTCUSDT', 66001])
    expect(store.listEvents(10)).toHaveLength(1)
    engine.reset('a')
    hub.push(['BTCUSDT', 66002])
    expect(store.listEvents(10)).toHaveLength(2)
  })

  describe('restart (new engine on the same DB)', () => {
    const fireOnce = (): void => {
      store.insert(newAlert('a', { condition: 'greater', frequency: 'every' }))
      make('off', undefined, 60).sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
      engine.close()
    }

    it('a fired every greater alert whose condition is still true does not fire again', () => {
      fireOnce()
      make('off', undefined, 60).sync()
      clock += 120_000 // well past the cooldown
      hub.push(['BTCUSDT', 66000], ['BTCUSDT', 67000])
      expect(store.listEvents(10)).toHaveLength(1)
      // Seen false once (re-armed), then true again past the cooldown: fires.
      hub.push(['BTCUSDT', 64000], ['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('re-arming inside the cooldown waits for the cooldown, seeded from lastTriggeredAt', () => {
      fireOnce()
      make('off', undefined, 60).sync()
      clock += 10_000
      hub.push(['BTCUSDT', 64000], ['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
      clock += 51_000
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('a less alert behaves the same', () => {
      store.insert(newAlert('a', { condition: 'less', frequency: 'every' }))
      make('off', undefined, 0).sync()
      hub.push(['BTCUSDT', 60000])
      engine.close()
      make('off', undefined, 0).sync()
      hub.push(['BTCUSDT', 60000])
      expect(store.listEvents(10)).toHaveLength(1)
      hub.push(['BTCUSDT', 66000], ['BTCUSDT', 60000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('a never-fired alert still fires on its first observation', () => {
      store.insert(newAlert('a', { condition: 'greater', frequency: 'every' }))
      make('off', undefined, 60).sync()
      engine.close()
      make('off', undefined, 60).sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
    })

    it.each([
      ['active: true', { active: true }],
      ['a price change', { price: 65500 }],
      ['a condition patch', { condition: 'greater' as const }],
    ])('an explicit reset (%s) starts armed and fires at once', (_label, patch) => {
      fireOnce()
      make('off', undefined, 60).sync()
      clock += 120_000
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
      // What PATCH /api/alerts/:id does.
      store.update('a', patch, clock / 1000)
      engine.reset('a')
      engine.sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('an explicit reset still respects the every cooldown', () => {
      fireOnce()
      make('off', undefined, 60).sync()
      clock += 10_000
      store.update('a', { price: 65500 }, clock / 1000)
      engine.reset('a')
      engine.sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
      clock += 51_000
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('a paused-then-reactivated once alert starts armed', () => {
      store.insert(newAlert('a', { condition: 'greater', frequency: 'once' }))
      make('off', undefined, 60).sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.get('a')!.active).toBe(false)
      engine.close()
      make('off', undefined, 60).sync()
      store.update('a', { active: true }, clock / 1000)
      engine.reset('a')
      engine.sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(2)
    })

    it('a note-only edit (no reset) keeps the disarmed state', () => {
      fireOnce()
      make('off', undefined, 60).sync()
      clock += 120_000
      store.update('a', { note: 'n' }, clock / 1000)
      engine.sync()
      hub.push(['BTCUSDT', 66000])
      expect(store.listEvents(10)).toHaveLength(1)
    })
  })

  describe('batched notifications', () => {
    const insertMany = (n: number): void => {
      for (let i = 0; i < n; i++) {
        store.insert(
          newAlert(`a${i}`, {
            condition: 'greater',
            price: 1000 + i,
            createdAt: T0_MS / 1000 + i,
            note: i === 0 ? 'first' : '',
          }),
        )
      }
    }

    it('one batch firing several alerts sends one desktop notification and one webhook', async () => {
      insertMany(7)
      make('always').sync()
      const msgs: AlertStreamMessage[] = []
      engine.subscribe((m) => msgs.push(m))
      hub.push(['BTCUSDT', 70000])
      await flush()
      const events = store.listEvents(10).reverse() // oldest first = firing order
      expect(events).toHaveLength(7)
      // The WebSocket still gets one `triggered` per alert, then one `changed`.
      expect(msgs.map((m) => m.type)).toEqual([...Array(7).fill('triggered'), 'changed'])

      expect(desktop).toHaveBeenCalledTimes(1)
      const lines = events.slice(0, 5).map((e) => e.message)
      expect(desktop).toHaveBeenCalledWith('Alerts: 7 triggered', [...lines, '+2 more'].join('\n'))

      expect(webhook).toHaveBeenCalledTimes(1)
      const p = webhook.mock.calls[0]![0]
      const text = [
        `${events[0]!.message}\nNote: first`,
        ...events.slice(1).map((e) => e.message),
      ].join('\n')
      expect(p).toEqual({
        text,
        content: text,
        event: events[0],
        alert: store.get('a0'),
        events,
        allowed_mentions: { parse: [] },
      })
    })

    it('two triggers list both without "+M more"', async () => {
      insertMany(2)
      make('always').sync()
      hub.push(['BTCUSDT', 70000])
      await flush()
      const events = store.listEvents(10).reverse()
      expect(desktop).toHaveBeenCalledWith(
        'Alerts: 2 triggered',
        `${events[0]!.message}\n${events[1]!.message}`,
      )
      expect(webhook).toHaveBeenCalledTimes(1)
    })

    it('separate batches notify separately', async () => {
      insertMany(2)
      make('always').sync()
      hub.push(['BTCUSDT', 1000.5]) // only a0 (level 1000)
      hub.push(['BTCUSDT', 70000]) // only a1 (a0 is a fired once alert)
      await flush()
      expect(desktop).toHaveBeenCalledTimes(2)
      expect(desktop.mock.calls.map((c) => c[0])).toEqual(['Alert: BTCUSDT', 'Alert: BTCUSDT'])
      expect(webhook).toHaveBeenCalledTimes(2)
      expect(webhook.mock.calls.every((c) => !('events' in c[0]))).toBe(true)
    })
  })

  it('ignores invalid prices and symbols without alerts', () => {
    store.insert(newAlert('a', { condition: 'greater', price: 1 }))
    make().sync()
    hub.push(['BTCUSDT', Number.NaN], ['BTCUSDT', 0], ['BTCUSDT', -3], ['ETHUSDT', 5])
    expect(store.listEvents(10)).toEqual([])
  })

  it.each([
    ['auto', 0, 1],
    ['auto', 1, 0],
    ['always', 1, 1],
    ['off', 0, 0],
  ] as const)('desktop mode %s with %i client(s) notifies %i time(s)', async (mode, clients, n) => {
    store.insert(newAlert('a', { condition: 'greater' }))
    make(mode).sync()
    for (let i = 0; i < clients; i++) engine.subscribe(() => {})
    expect(engine.clientCount).toBe(clients)
    hub.push(['BTCUSDT', 70000])
    await flush()
    expect(desktop).toHaveBeenCalledTimes(n)
    expect(webhook).toHaveBeenCalledTimes(1)
  })

  it('a throwing or rejecting notifier breaks neither the others, the push, nor the DB', async () => {
    store.insert(newAlert('a', { condition: 'greater', frequency: 'every' }))
    const hook = vi.fn<(p: WebhookPayload) => Promise<void>>(() => Promise.resolve())
    make('always', {
      desktop: () => {
        throw new Error('boom')
      },
      webhook: hook,
    }).sync()
    const msgs: AlertStreamMessage[] = []
    engine.subscribe(() => {
      throw new Error('bad client')
    })
    engine.subscribe((m) => msgs.push(m))
    hub.push(['BTCUSDT', 70000])
    await flush()
    expect(store.listEvents(10)).toHaveLength(1)
    expect(msgs.map((m) => m.type)).toEqual(['triggered', 'changed'])
    expect(hook).toHaveBeenCalledTimes(1)
    expect(warnings).toContain('desktop notifier failed')

    engine.close()
    expect(hub.held).toBe(0)
    make('always', { desktop: () => Promise.reject(new Error('later')), webhook: hook })
    engine.sync()
    clock += 120_000
    // A new engine (restart) starts the already-fired alert disarmed: dip below, then above.
    hub.push(['BTCUSDT', 60000], ['BTCUSDT', 70000])
    await flush()
    expect(store.listEvents(10)).toHaveLength(2)
    expect(hook).toHaveBeenCalledTimes(2)
    expect(warnings.filter((w) => w === 'desktop notifier failed')).toHaveLength(2)
  })

  it('does not fire an alert that was deleted (sync drops it)', () => {
    store.insert(newAlert('a', { condition: 'greater' }))
    store.insert(newAlert('b', { condition: 'greater', symbol: 'ETHUSDT' }))
    make().sync()
    store.delete('a')
    engine.sync()
    hub.push(['BTCUSDT', 70000])
    expect(store.listEvents(10)).toEqual([])
  })

  it('a merged webhook text stays under the Discord limit and says how many were cut', () => {
    const fired: FiredAlert[] = Array.from({ length: 40 }, (_, i) => ({
      event: {
        id: i + 1,
        alertId: `a${i}`,
        symbol: 'BTCUSDT',
        condition: 'greater',
        level: 1,
        price: 2,
        triggeredAt: 1,
        message: `BTCUSDT greater than 1 (last 2) #${i}`,
      },
      alert: { ...newAlert(`a${i}`), note: 'n'.repeat(200) },
    }))
    const p = webhookPayload(fired)
    expect(p.text.length).toBeLessThanOrEqual(WEBHOOK_TEXT_MAX_CHARS)
    expect(p.content).toBe(p.text)
    expect(p.events).toHaveLength(40)
    const kept = p.text.split('\n').filter((l) => l.startsWith('BTCUSDT')).length
    expect(kept).toBeGreaterThan(1)
    expect(p.text.endsWith(`\n+${40 - kept} more`)).toBe(true)
    // Fits exactly: no "+M more".
    const two = webhookPayload(fired.slice(0, 2))
    expect(two.text.split('\n')).toHaveLength(4)
    expect(desktopNotification(fired.slice(0, 1)).title).toBe('Alert: BTCUSDT')
  })

  it('a note with @everyone is sent with mentions disabled', async () => {
    store.insert(newAlert('a', { condition: 'greater', note: '@everyone buy' }))
    make('off').sync()
    hub.push(['BTCUSDT', 70000])
    await flush()
    expect(webhook.mock.calls[0]![0]).toMatchObject({
      text: 'BTCUSDT greater than 65,000 (last 70,000)\nNote: @everyone buy',
      allowed_mentions: { parse: [] },
    })
  })

  it('subscribe returns an idempotent detach', () => {
    make()
    const off = engine.subscribe(() => {})
    expect(engine.clientCount).toBe(1)
    off()
    off()
    expect(engine.clientCount).toBe(0)
  })
})
