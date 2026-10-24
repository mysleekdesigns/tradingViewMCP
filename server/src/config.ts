import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { isDesktopNotifyMode, type DesktopNotifyMode } from './alerts/types.ts'
import { isAllowedWebhookUrl } from './alerts/notify.ts'
import { DEFAULT_VENUE_ID, isVenueId, VENUES, type Venue } from './venue.ts'

export interface Config {
  port: number
  dbPath: string
  /** TV_DESKTOP_NOTIFY: auto | always | off (default auto). */
  desktopNotify: DesktopNotifyMode
  /** TV_ALERT_WEBHOOK_URL (optional, secret: never log or store it). */
  alertWebhookUrl?: string
  /** TV_MARKET_VENUE: binance (default, market-data-only vision hosts) | binance-us. */
  venue: Venue
}

const SERVER_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const DEFAULT_PORT = 8787
export const DEFAULT_DB_PATH = resolve(SERVER_ROOT, 'data', 'app.db')

/**
 * Reads TV_PORT, TV_DB_PATH, TV_DESKTOP_NOTIFY, TV_MARKET_VENUE and TV_ALERT_WEBHOOK_URL. The bind host is fixed
 * to 127.0.0.1 and is not configurable. Errors never echo the webhook URL.
 */
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
  const rawMode = env.TV_DESKTOP_NOTIFY?.trim().toLowerCase()
  let desktopNotify: DesktopNotifyMode = 'auto'
  if (rawMode !== undefined && rawMode !== '') {
    if (!isDesktopNotifyMode(rawMode)) {
      throw new Error(
        `TV_DESKTOP_NOTIFY must be auto, always or off, got "${env.TV_DESKTOP_NOTIFY}"`,
      )
    }
    desktopNotify = rawMode
  }
  const rawVenue = env.TV_MARKET_VENUE?.trim().toLowerCase()
  let venueId = DEFAULT_VENUE_ID
  if (rawVenue !== undefined && rawVenue !== '') {
    if (!isVenueId(rawVenue)) {
      throw new Error(
        `TV_MARKET_VENUE must be ${Object.keys(VENUES).join(' or ')}, got "${env.TV_MARKET_VENUE}"`,
      )
    }
    venueId = rawVenue
  }
  const config: Config = {
    port,
    dbPath: dbPath === ':memory:' ? dbPath : resolve(dbPath),
    desktopNotify,
    venue: VENUES[venueId],
  }
  const rawHook = env.TV_ALERT_WEBHOOK_URL?.trim()
  if (rawHook !== undefined && rawHook !== '') {
    if (!isAllowedWebhookUrl(rawHook)) {
      throw new Error('TV_ALERT_WEBHOOK_URL must be an https URL (or http to localhost)')
    }
    config.alertWebhookUrl = rawHook
  }
  return config
}
