import { describe, expect, it, vi } from 'vitest'
import {
  createMacDesktopNotifier,
  createWebhookNotifier,
  desktopScript,
  escapeAppleScript,
  isAllowedWebhookUrl,
  NOTIFY_TIMEOUT_MS,
  type AlertWebhookPayload,
  type ExecFileLike,
} from '../src/alerts/notify.ts'
import type { WebhookPayload } from '../src/alerts/types.ts'
import { loadConfig } from '../src/config.ts'

const SECRET_URL = 'https://hooks.example.test/services/T000/B000/SECRET-TOKEN'

const payload: WebhookPayload = {
  text: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
  content: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
  event: {
    id: 1,
    alertId: 'a',
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    level: 65000,
    price: 65012.5,
    triggeredAt: 1_700_000_000,
    message: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
  },
  alert: {
    id: 'a',
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    price: 65000,
    frequency: 'once',
    active: false,
    note: '',
    createdAt: 1_700_000_000,
    updatedAt: 1_700_000_000,
    lastTriggeredAt: 1_700_000_000,
  },
}

function logSpy(): { warn: (o: Record<string, unknown>, m: string) => void; lines: string[] } {
  const lines: string[] = []
  return { warn: (o, m) => lines.push(`${m} ${JSON.stringify(o)}`), lines }
}

describe('AppleScript escaping', () => {
  it('escapes backslashes before quotes', () => {
    expect(escapeAppleScript('a "b" c')).toBe('a \\"b\\" c')
    expect(escapeAppleScript('back\\slash')).toBe('back\\\\slash')
    expect(escapeAppleScript('\\"')).toBe('\\\\\\"')
    expect(escapeAppleScript('plain 65,000')).toBe('plain 65,000')
  })
  it('a hostile note cannot terminate the string literal', () => {
    const s = desktopScript('Alert: BTC', 'x" & (do shell script "id") & "')
    expect(s).toBe(
      'display notification "x\\" & (do shell script \\"id\\") & \\"" with title "Alert: BTC"',
    )
    // Every quote inside the body literal is escaped: exactly 4 unescaped quotes remain.
    expect(s.replace(/\\\\|\\"/g, '').match(/"/g)).toHaveLength(4)
  })
  it('flattens control characters and caps length', () => {
    expect(desktopScript('t', 'line1\nline2\ttab')).toBe(
      'display notification "line1 line2 tab" with title "t"',
    )
    const long = desktopScript('t', 'y'.repeat(2000))
    expect(long.length).toBeLessThan(600)
  })
})

describe('createMacDesktopNotifier', () => {
  it('returns undefined off macOS', () => {
    expect(createMacDesktopNotifier({ platform: 'linux' })).toBeUndefined()
    expect(createMacDesktopNotifier({ platform: 'win32' })).toBeUndefined()
  })
  it('runs osascript with args (no shell) and a 5 s timeout', async () => {
    const calls: { file: string; args: readonly string[]; timeout: number }[] = []
    const execFile: ExecFileLike = (file, args, options, cb) => {
      calls.push({ file, args, timeout: options.timeout })
      cb(null)
    }
    const notify = createMacDesktopNotifier({ platform: 'darwin', execFile })!
    await notify('Alert: BTCUSDT', 'say "hi"')
    expect(calls).toEqual([
      {
        file: 'osascript',
        args: ['-e', 'display notification "say \\"hi\\"" with title "Alert: BTCUSDT"'],
        timeout: NOTIFY_TIMEOUT_MS,
      },
    ])
  })
  it('never throws: logs exec errors and synchronous throws', async () => {
    const log = logSpy()
    const failing = createMacDesktopNotifier({
      platform: 'darwin',
      execFile: (_f, _a, _o, cb) => cb(new Error('osascript missing')),
      log,
    })!
    await expect(failing('t', 'b')).resolves.toBeUndefined()
    const throwing = createMacDesktopNotifier({
      platform: 'darwin',
      execFile: () => {
        throw new Error('spawn failed')
      },
      log,
    })!
    await expect(throwing('t', 'b')).resolves.toBeUndefined()
    expect(log.lines).toHaveLength(2)
  })
})

describe('webhook URL policy', () => {
  it.each([
    [SECRET_URL, true],
    ['https://discord.com/api/webhooks/1/abc', true],
    ['http://localhost:9000/hook', true],
    ['http://127.0.0.1/hook', true],
    ['http://[::1]:8080/hook', true],
    ['http://hooks.example.test/x', false],
    ['http://localhost.evil.test/x', false],
    ['ftp://example.test/x', false],
    ['file:///etc/passwd', false],
    ['https://user:pw@example.test/x', false],
    ['not a url', false],
    ['', false],
  ])('%s → %s', (url, ok) => {
    expect(isAllowedWebhookUrl(url)).toBe(ok)
  })
  it('createWebhookNotifier rejects non-https without echoing the URL', () => {
    const bad = 'http://hooks.example.test/SECRET-TOKEN'
    expect(() => createWebhookNotifier(bad, vi.fn())).toThrow(/https/)
    try {
      createWebhookNotifier(bad, vi.fn())
    } catch (err) {
      expect((err as Error).message).not.toContain('SECRET')
    }
  })
})

describe('createWebhookNotifier', () => {
  const ok = (): Response => new Response(null, { status: 204 })

  it('POSTs the payload as JSON', async () => {
    const fetchImpl = vi.fn<typeof fetch>(() => Promise.resolve(ok()))
    await createWebhookNotifier(SECRET_URL, fetchImpl, { retryDelayMs: 0 })(payload)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe(SECRET_URL)
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json')
    expect(JSON.parse(String(init?.body))).toEqual({ ...payload, allowed_mentions: { parse: [] } })
    expect(init?.signal).toBeInstanceOf(AbortSignal)
  })

  it('always disables mentions and passes a batch `events` list through', async () => {
    const fetchImpl = vi.fn<typeof fetch>(() => Promise.resolve(ok()))
    const send = createWebhookNotifier(SECRET_URL, fetchImpl, { retryDelayMs: 0 })
    // A caller-supplied allowed_mentions cannot re-enable pings.
    const hostile = {
      ...payload,
      text: '@everyone',
      allowed_mentions: { parse: ['everyone'] },
    } as unknown as WebhookPayload
    await send(hostile)
    const batch: AlertWebhookPayload = {
      ...payload,
      events: [payload.event, { ...payload.event, id: 2 }],
      allowed_mentions: { parse: [] },
    }
    await send(batch)
    const bodies = fetchImpl.mock.calls.map((c) => JSON.parse(String(c[1]?.body)) as unknown)
    expect(bodies[0]).toMatchObject({ text: '@everyone', allowed_mentions: { parse: [] } })
    expect(bodies[1]).toEqual(batch)
  })

  it('retries once on a 5xx, then succeeds', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('down', { status: 500 }))
      .mockResolvedValueOnce(ok())
    const log = logSpy()
    await createWebhookNotifier(SECRET_URL, fetchImpl, { retryDelayMs: 0, log })(payload)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(log.lines).toEqual([])
  })

  it('retries at most once, never throws, and never logs the URL', async () => {
    const log = logSpy()
    const net = vi.fn<typeof fetch>(() =>
      Promise.reject(new TypeError(`fetch failed for ${SECRET_URL}`)),
    )
    await expect(
      createWebhookNotifier(SECRET_URL, net, { retryDelayMs: 0, log })(payload),
    ).resolves.toBeUndefined()
    expect(net).toHaveBeenCalledTimes(2)

    const err500 = vi.fn<typeof fetch>(() => Promise.resolve(new Response('', { status: 503 })))
    await createWebhookNotifier(SECRET_URL, err500, { retryDelayMs: 0, log })(payload)
    expect(err500).toHaveBeenCalledTimes(2)

    expect(log.lines).toHaveLength(2)
    for (const line of log.lines) {
      expect(line).not.toContain('SECRET')
      expect(line).not.toContain('hooks.example.test')
    }
    expect(log.lines[1]).toContain('HTTP 503')
  })

  it('does not retry a 4xx', async () => {
    const fetchImpl = vi.fn<typeof fetch>(() => Promise.resolve(new Response('', { status: 404 })))
    const log = logSpy()
    await createWebhookNotifier(SECRET_URL, fetchImpl, { retryDelayMs: 0, log })(payload)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(log.lines[0]).toContain('HTTP 404')
  })
})

describe('alert config', () => {
  it('defaults: desktop auto, no webhook', () => {
    const c = loadConfig({})
    expect(c.desktopNotify).toBe('auto')
    expect(c.alertWebhookUrl).toBeUndefined()
  })
  it('reads TV_DESKTOP_NOTIFY and TV_ALERT_WEBHOOK_URL', () => {
    const c = loadConfig({ TV_DESKTOP_NOTIFY: 'Always', TV_ALERT_WEBHOOK_URL: ` ${SECRET_URL} ` })
    expect(c.desktopNotify).toBe('always')
    expect(c.alertWebhookUrl).toBe(SECRET_URL)
    expect(loadConfig({ TV_DESKTOP_NOTIFY: 'off' }).desktopNotify).toBe('off')
  })
  it('rejects a bad mode and a non-https webhook without echoing the URL', () => {
    expect(() => loadConfig({ TV_DESKTOP_NOTIFY: 'loud' })).toThrow(/TV_DESKTOP_NOTIFY/)
    expect(() =>
      loadConfig({ TV_ALERT_WEBHOOK_URL: 'http://hooks.example.test/SECRET-TOKEN' }),
    ).toThrow(/^TV_ALERT_WEBHOOK_URL must be an https URL \(or http to localhost\)$/)
  })
})
