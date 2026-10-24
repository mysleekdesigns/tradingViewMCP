// Alert notifiers: macOS desktop notification (osascript, no shell) and an optional webhook
// (Slack/Discord-style JSON). Notification only; nothing here talks to an exchange.
// The webhook URL is a secret: it is never logged, echoed in errors, or stored.

import { execFile as nodeExecFile } from 'node:child_process'
import { NOOP_LOG, type AlertEvent, type AlertLog, type WebhookPayload } from './types.ts'

export const NOTIFY_TIMEOUT_MS = 5_000
const MAX_TITLE_CHARS = 120
const MAX_BODY_CHARS = 500
const LOCAL_HTTP_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])

/**
 * What the engine actually POSTs: the `WebhookPayload` from types.ts plus
 * - `allowed_mentions: { parse: [] }` so a note such as `@everyone` cannot ping a Discord
 *   channel (Slack ignores the field), and
 * - `events`: every trigger of the ticker batch, present only when the batch fired more than
 *   one alert (`event`/`alert` are then the first trigger).
 */
export interface AlertWebhookPayload extends WebhookPayload {
  allowed_mentions: { parse: [] }
  events?: AlertEvent[]
}

/** Escapes text for use inside an AppleScript double-quoted string literal. */
export function escapeAppleScript(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
}

/** Control characters (incl. newlines) become spaces; then the text is length-capped. */
function clean(s: string, max: number): string {
  // eslint-disable-next-line no-control-regex
  const flat = s.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

/** The AppleScript passed to `osascript -e`. */
export function desktopScript(title: string, body: string): string {
  const t = escapeAppleScript(clean(title, MAX_TITLE_CHARS))
  const b = escapeAppleScript(clean(body, MAX_BODY_CHARS))
  return `display notification "${b}" with title "${t}"`
}

export type ExecFileLike = (
  file: string,
  args: readonly string[],
  options: { timeout: number },
  callback: (error: Error | null) => void,
) => unknown

export interface DesktopNotifierOptions {
  /** Default `process.platform`. */
  platform?: NodeJS.Platform
  /** Default `node:child_process.execFile` (never a shell). */
  execFile?: ExecFileLike
  log?: AlertLog
}

/**
 * macOS only: `osascript -e 'display notification ...'` with a 5 s timeout. Returns undefined
 * on other platforms. The returned function never rejects; failures are logged.
 */
export function createMacDesktopNotifier(
  opts: DesktopNotifierOptions = {},
): ((title: string, body: string) => Promise<void>) | undefined {
  const platform = opts.platform ?? process.platform
  if (platform !== 'darwin') return undefined
  const run: ExecFileLike =
    opts.execFile ??
    ((file, args, options, cb) => nodeExecFile(file, [...args], options, (err) => cb(err)))
  const log = opts.log ?? NOOP_LOG
  return (title, body) =>
    new Promise<void>((resolve) => {
      try {
        run(
          'osascript',
          ['-e', desktopScript(title, body)],
          { timeout: NOTIFY_TIMEOUT_MS },
          (err) => {
            if (err) log.warn({ err: err.message }, 'desktop notification failed')
            resolve()
          },
        )
      } catch (err) {
        log.warn(
          { err: err instanceof Error ? err.message : String(err) },
          'desktop notification failed',
        )
        resolve()
      }
    })
}

/** https anywhere, or http only to localhost / 127.0.0.1 / [::1]. */
export function isAllowedWebhookUrl(raw: string): boolean {
  let u: URL
  try {
    u = new URL(raw)
  } catch {
    return false
  }
  if (u.username !== '' || u.password !== '') return false
  if (u.protocol === 'https:') return true
  return u.protocol === 'http:' && LOCAL_HTTP_HOSTS.has(u.hostname)
}

export interface WebhookNotifierOptions {
  log?: AlertLog
  /** Per-attempt timeout. Default 5 s. */
  timeoutMs?: number
  /** Delay before the single retry. Default 1 s. */
  retryDelayMs?: number
}

/**
 * POSTs the payload as JSON (always with `allowed_mentions: { parse: [] }`). At most one retry on a network error/timeout or a 5xx. The
 * returned function never rejects. Throws at construction (without echoing the URL) when the
 * URL is not https (or http to localhost).
 */
export function createWebhookNotifier(
  url: string,
  fetchImpl: typeof fetch,
  opts: WebhookNotifierOptions = {},
): (payload: WebhookPayload) => Promise<void> {
  if (!isAllowedWebhookUrl(url)) {
    throw new Error('alert webhook URL must be https (or http to localhost)')
  }
  const log = opts.log ?? NOOP_LOG
  const timeoutMs = opts.timeoutMs ?? NOTIFY_TIMEOUT_MS
  const retryDelayMs = opts.retryDelayMs ?? 1_000

  /** 'ok' | 'retry' | 'fail' plus a loggable reason that never contains the URL. */
  const attempt = async (body: string): Promise<{ kind: 'ok' | 'retry' | 'fail'; why: string }> => {
    try {
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body,
        signal: AbortSignal.timeout(timeoutMs),
        redirect: 'error',
      })
      if (res.ok) return { kind: 'ok', why: '' }
      return { kind: res.status >= 500 ? 'retry' : 'fail', why: `HTTP ${res.status}` }
    } catch (err) {
      // Only the error name: messages/causes from fetch can include the host or URL.
      return { kind: 'retry', why: err instanceof Error ? err.name : 'error' }
    }
  }

  return async (payload) => {
    try {
      // Mentions are always disabled, whoever built the payload.
      const body = JSON.stringify({ ...payload, allowed_mentions: { parse: [] } })
      let r = await attempt(body)
      if (r.kind === 'retry') {
        if (retryDelayMs > 0) await new Promise<void>((res) => setTimeout(res, retryDelayMs))
        r = await attempt(body)
      }
      if (r.kind !== 'ok') log.warn({ reason: r.why }, 'alert webhook failed')
    } catch {
      log.warn({ reason: 'unexpected' }, 'alert webhook failed')
    }
  }
}
