import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export interface Config {
  port: number
  dbPath: string
}

const SERVER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const DEFAULT_PORT = 8787
export const DEFAULT_DB_PATH = resolve(SERVER_ROOT, 'data', 'app.db')

/** Reads TV_PORT and TV_DB_PATH. The bind host is fixed to 127.0.0.1 and is not configurable. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const rawPort = env.TV_PORT
  let port = DEFAULT_PORT
  if (rawPort !== undefined && rawPort !== '') {
    port = Number(rawPort)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`TV_PORT must be an integer in 1..65535, got "${rawPort}"`)
    }
  }
  const rawDb = env.TV_DB_PATH
  const dbPath = rawDb !== undefined && rawDb !== '' ? rawDb : DEFAULT_DB_PATH
  return { port, dbPath: dbPath === ':memory:' ? dbPath : resolve(dbPath) }
}
