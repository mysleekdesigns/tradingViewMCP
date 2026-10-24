import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import { createMacDesktopNotifier, createWebhookNotifier } from './alerts/notify.ts'
import type { AlertLog, AlertNotifier } from './alerts/types.ts'
import { buildApp } from './app.ts'
import { loadConfig } from './config.ts'
import { openDb } from './db/index.ts'

// Single-user local app: always bind to loopback. Not configurable on purpose.
const HOST = '127.0.0.1'

const config = loadConfig()
if (config.dbPath !== ':memory:') mkdirSync(dirname(config.dbPath), { recursive: true })

const db = openDb(config.dbPath)

// Notifiers log through the app logger once it exists. The webhook URL is never logged.
let logWarn: AlertLog['warn'] = () => {}
const notifyLog: AlertLog = { warn: (obj, msg) => logWarn(obj, msg) }
const alertNotifier: AlertNotifier = {}
const desktop = createMacDesktopNotifier({ log: notifyLog })
if (desktop) alertNotifier.desktop = desktop
if (config.alertWebhookUrl !== undefined) {
  alertNotifier.webhook = createWebhookNotifier(
    config.alertWebhookUrl,
    globalThis.fetch.bind(globalThis),
    { log: notifyLog },
  )
}

const app = await buildApp(
  { db, alertNotifier, desktopNotifyMode: config.desktopNotify },
  { logger: true },
)
logWarn = (obj, msg) => app.log.warn(obj, msg)
app.log.info(
  {
    desktopNotify: desktop ? config.desktopNotify : 'unavailable',
    webhook: alertNotifier.webhook !== undefined,
  },
  'alert notifiers',
)

let closing = false
async function shutdown(signal: string): Promise<void> {
  if (closing) return
  closing = true
  app.log.info({ signal }, 'shutting down')
  try {
    await app.close()
  } finally {
    db.close()
  }
  process.exit(0)
}
process.on('SIGINT', () => void shutdown('SIGINT'))
process.on('SIGTERM', () => void shutdown('SIGTERM'))

try {
  await app.listen({ host: HOST, port: config.port })
} catch (err) {
  app.log.error({ err }, 'failed to start')
  db.close()
  process.exit(1)
}
