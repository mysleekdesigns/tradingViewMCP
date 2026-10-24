import { connect as netConnect } from 'node:net'
import type { AddressInfo } from 'node:net'
import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp, isAllowedHost } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'

// DNS-rebinding guard: only Host localhost / 127.0.0.1 / [::1] (any port) is served. Offline.

class FakeUpstream {
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  readonly url: string
  constructor(url: string) {
    this.url = url
  }
  close(): void {}
}

const FORBIDDEN = { error: 'http', message: 'host not allowed' }

/** Sends raw bytes to a listening server and resolves with everything it returns until close. */
function rawRequest(port: number, text: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const sock = netConnect({ host: '127.0.0.1', port })
    let out = ''
    const timer = setTimeout(() => {
      sock.destroy()
      resolve(out)
    }, 2000)
    sock.setEncoding('utf8')
    sock.on('data', (d: string) => (out += d))
    sock.on('error', (err) => {
      clearTimeout(timer)
      reject(err)
    })
    sock.on('close', () => {
      clearTimeout(timer)
      resolve(out)
    })
    sock.write(text)
  })
}

describe('isAllowedHost', () => {
  it.each([
    ['localhost', true],
    ['localhost:5173', true],
    ['LOCALHOST:5173', true],
    ['127.0.0.1', true],
    ['127.0.0.1:8787', true],
    ['[::1]', true],
    ['[::1]:8787', true],
    ['evil.example.com', false],
    ['evil.example.com:8787', false],
    ['localhost.evil.example', false],
    ['127.0.0.1.nip.io', false],
    ['evil@localhost', false],
    ['::1', false],
    ['[::2]:80', false],
    ['localhost:', false],
    ['localhost:99999', false],
    ['localhost:80:80', false],
    ['0.0.0.0:8787', false],
    ['192.168.1.2:8787', false],
    ['', false],
  ])('%j → %s', (host, ok) => {
    expect(isAllowedHost(host)).toBe(ok)
  })
  it('rejects a missing or non-string Host', () => {
    expect(isAllowedHost(undefined)).toBe(false)
    expect(isAllowedHost(['localhost'])).toBe(false)
  })
})

describe('Host header guard', () => {
  let db: Db
  let app: FastifyInstance

  beforeEach(async () => {
    db = openDb(':memory:')
    app = await buildApp({
      db,
      fetch: () => Promise.reject(new Error('no network in tests')),
      WebSocketImpl: FakeUpstream as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
    })
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const health = (host: string) =>
    app.inject({ method: 'GET', url: '/api/health', headers: { host } })

  it('evil.example.com → 403', async () => {
    const res = await health('evil.example.com')
    expect(res.statusCode).toBe(403)
    expect(res.json()).toEqual(FORBIDDEN)
  })

  it.each(['localhost:5173', '127.0.0.1:8787', '[::1]:8787', 'localhost'])(
    '%s → OK',
    async (host) => {
      const res = await health(host)
      expect(res.statusCode).toBe(200)
      expect(res.json()).toMatchObject({ ok: true })
    },
  )

  it('inject() default Host (localhost:80) is allowed', async () => {
    expect((await app.inject({ url: '/api/health' })).statusCode).toBe(200)
  })

  it('also guards unknown routes and blocks mutations before the body is handled', async () => {
    const miss = await app.inject({ url: '/nope', headers: { host: 'evil.example.com' } })
    expect(miss.statusCode).toBe(403)
    const post = await app.inject({
      method: 'POST',
      url: '/api/alerts',
      headers: { host: 'evil.example.com:8787' },
      payload: { symbol: 'BTCUSDT', condition: 'greater', price: 1, frequency: 'once' },
    })
    expect(post.statusCode).toBe(403)
    expect(post.json()).toEqual(FORBIDDEN)
    const list = await app.inject({ url: '/api/alerts' })
    expect(list.json()).toEqual([])
  })

  it('rejects a WebSocket upgrade with a foreign Host (in-process)', async () => {
    await app.ready()
    await expect(
      app.injectWS('/ws/alerts', { headers: { host: 'evil.example.com' } }),
    ).rejects.toThrow(/403/)
    await expect(
      app.injectWS('/ws/tickers', { headers: { host: 'evil.example.com:8787' } }),
    ).rejects.toThrow(/403/)
  })

  it('accepts a WebSocket upgrade with a loopback Host (Vite proxy forwards localhost:5173)', async () => {
    await app.ready()
    const ok = await app.injectWS('/ws/alerts', { headers: { host: 'localhost:5173' } })
    ok.terminate()
  })

  describe('on a real loopback socket (ephemeral port)', () => {
    let port: number
    beforeEach(async () => {
      await app.listen({ host: '127.0.0.1', port: 0 })
      port = (app.server.address() as AddressInfo).port
    })

    it('a missing Host (HTTP/1.0) → 403', async () => {
      const out = await rawRequest(port, 'GET /api/health HTTP/1.0\r\n\r\n')
      expect(out).toMatch(/^HTTP\/1\.[01] 403/)
      expect(out).toContain('"host not allowed"')
    })

    it('a loopback Host → 200', async () => {
      const out = await rawRequest(
        port,
        `GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nConnection: close\r\n\r\n`,
      )
      expect(out).toMatch(/^HTTP\/1\.1 200/)
    })

    const upgrade = (path: string, host: string): string =>
      [
        `GET ${path} HTTP/1.1`,
        `Host: ${host}`,
        'Connection: Upgrade',
        'Upgrade: websocket',
        'Sec-WebSocket-Version: 13',
        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
        '',
        '',
      ].join('\r\n')

    it.each(['/ws/alerts', '/ws/tickers'])(
      'a WebSocket upgrade of %s with a foreign Host gets 403, never 101',
      async (path) => {
        const out = await rawRequest(port, upgrade(path, 'evil.example.com'))
        expect(out).toMatch(/^HTTP\/1\.1 403/)
        expect(out).toContain('"host not allowed"')
        expect(out).not.toContain('101 Switching Protocols')
      },
    )

    it('a WebSocket upgrade with a loopback Host switches protocols', async () => {
      const out = await new Promise<string>((resolve, reject) => {
        const sock = netConnect({ host: '127.0.0.1', port })
        sock.setEncoding('utf8')
        sock.on('error', reject)
        sock.once('data', (d: string) => {
          sock.destroy()
          resolve(d)
        })
        sock.write(upgrade('/ws/alerts', `localhost:${port}`))
      })
      expect(out).toMatch(/^HTTP\/1\.1 101 Switching Protocols/)
    })
  })
})
