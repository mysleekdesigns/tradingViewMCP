import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import { buildApp } from './app.ts'
import { loadConfig } from './config.ts'
import { openDb } from './db/index.ts'

// Single-user local app: always bind to loopback. Not configurable on purpose.
const HOST = '127.0.0.1'

const config = loadConfig()
if (config.dbPath !== ':memory:') mkdirSync(dirname(config.dbPath), { recursive: true })

const db = openDb(config.dbPath)
const app = await buildApp({ db }, { logger: true })

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
