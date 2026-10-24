import { createMacDesktopNotifier, createWebhookNotifier } from './alerts/notify.ts'
import type { AlertLog, AlertNotifier } from './alerts/types.ts'
import { loadConfig } from './config.ts'
import { DbLockedError } from './dbLock.ts'
import { startServer, type Started } from './startup.ts'

const config = loadConfig()

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

let started: Started
try {
  // Lock the DB, listen on 127.0.0.1, then (only once listening) check the market-data venue.
  started = await startServer({
    config,
    logger: true,
    deps: { alertNotifier, desktopNotifyMode: config.desktopNotify },
  })
} catch (err) {
  if (err instanceof DbLockedError) console.error(`failed to start: ${err.message}`)
  else console.error('failed to start:', err)
  process.exit(1)
}

const { app } = started
logWarn = (obj, msg) => app.log.warn(obj, msg)
app.log.info(
  {
    desktopNotify: desktop ? config.desktopNotify : 'unavailable',
    webhook: alertNotifier.webhook !== undefined,
  },
  'alert notifiers',
)

// A crash still leaves a lock naming a dead pid, which the next start treats as stale.
process.on('exit', () => started.lock.release())

let closing = false
async function shutdown(signal: string): Promise<void> {
  if (closing) return
  closing = true
  app.log.info({ signal }, 'shutting down')
  try {
    await started.close()
  } finally {
    process.exit(0)
  }
}
process.on('SIGINT', () => void shutdown('SIGINT'))
process.on('SIGTERM', () => void shutdown('SIGTERM'))
