// Display-only time zone handling. Candles stay UNIX seconds UTC and bars stay UTC-aligned (a
// 1d bar opens at 00:00 UTC in every zone); only the labels on the time axis, the crosshair, and
// the legend are rendered in the chosen zone.

import { TickMarkType, type Time } from 'lightweight-charts'

/** 'UTC', 'local' (the browser's zone), or an IANA zone such as 'America/New_York'. */
export type TimezoneSetting = string

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
] as const

/** Zone string → whether Intl accepts it (constructing a formatter to find out is costly). */
const validZones = new Map<string, boolean>()
/** Bounds the validity cache: user input (Settings) can probe arbitrary strings. */
const MAX_CACHED_ZONES = 256

/** True when Intl accepts `tz` as a time zone. Cached per zone string. */
export function isValidTimezone(tz: string): boolean {
  if (tz === 'UTC') return true
  const cached = validZones.get(tz)
  if (cached !== undefined) return cached
  let ok: boolean
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    ok = true
  } catch {
    ok = false
  }
  if (validZones.size >= MAX_CACHED_ZONES) validZones.clear()
  validZones.set(tz, ok)
  return ok
}

/**
 * The `timeZone` option for Intl: 'local' → undefined (the browser's zone), an invalid zone →
 * 'UTC', anything else unchanged.
 */
export function resolveTimezone(tz: string): string | undefined {
  if (tz === 'local') return undefined
  return isValidTimezone(tz) ? tz : 'UTC'
}

interface ZonedParts {
  year: number
  /** 1..12 */
  month: number
  day: number
  hour: number
  minute: number
  second: number
}

/** One cached formatter per resolved zone (formatters are costly; the crosshair calls often). */
const partsFormatters = new Map<string, Intl.DateTimeFormat>()

function partsFormatter(zone: string | undefined): Intl.DateTimeFormat {
  const key = zone ?? '\u0000local'
  let f = partsFormatters.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
      numberingSystem: 'latn',
    })
    partsFormatters.set(key, f)
  }
  return f
}

/** One cached short-zone-name formatter per resolved zone ("EDT", "GMT+1"), for timezoneLabel. */
const nameFormatters = new Map<string, Intl.DateTimeFormat>()

function nameFormatter(zone: string | undefined): Intl.DateTimeFormat {
  const key = zone ?? '\u0000local'
  let f = nameFormatters.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'short' })
    nameFormatters.set(key, f)
  }
  return f
}

function zonedParts(sec: number, tz: string): ZonedParts {
  const zone = resolveTimezone(tz)
  const date = new Date(sec * 1000)
  if (zone === 'UTC') {
    return {
      year: date.getUTCFullYear(),
      month: date.getUTCMonth() + 1,
      day: date.getUTCDate(),
      hour: date.getUTCHours(),
      minute: date.getUTCMinutes(),
      second: date.getUTCSeconds(),
    }
  }
  const out: ZonedParts = { year: 0, month: 1, day: 1, hour: 0, minute: 0, second: 0 }
  for (const part of partsFormatter(zone).formatToParts(date)) {
    switch (part.type) {
      case 'year':
      case 'month':
      case 'day':
      case 'hour':
      case 'minute':
      case 'second':
        // Some engines print midnight as "24" even with h23; normalise.
        out[part.type] = part.type === 'hour' ? Number(part.value) % 24 : Number(part.value)
        break
    }
  }
  return out
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/**
 * "2026-10-25 14:03" in the zone (`seconds` adds ":07"; `dateOnly` gives "2026-10-25").
 * Locale-independent so the legend and the screenshot header line up across machines.
 */
export function formatDateTime(
  sec: number,
  tz: string,
  opts: { seconds?: boolean; dateOnly?: boolean } = {},
): string {
  if (!Number.isFinite(sec)) return '—'
  const p = zonedParts(sec, tz)
  const date = `${p.year}-${pad2(p.month)}-${pad2(p.day)}`
  if (opts.dateOnly) return date
  const time = `${pad2(p.hour)}:${pad2(p.minute)}`
  return opts.seconds ? `${date} ${time}:${pad2(p.second)}` : `${date} ${time}`
}

/**
 * Short zone name for an instant: "UTC", "EDT", "GMT+1", ... Depends on the browser's Intl data,
 * so it is for display only.
 */
export function timezoneLabel(tz: string, sec: number = Date.now() / 1000): string {
  const zone = resolveTimezone(tz)
  if (zone === 'UTC') return 'UTC'
  try {
    const parts = nameFormatter(zone).formatToParts(new Date(sec * 1000))
    return parts.find((p) => p.type === 'timeZoneName')?.value ?? zone ?? 'Local'
  } catch {
    return zone ?? 'Local'
  }
}

/** LWC `Time` → UNIX seconds. Our series only use UTCTimestamp; the rest is for completeness. */
export function timeToSeconds(time: Time): number {
  if (typeof time === 'number') return time
  if (typeof time === 'string') return Date.parse(`${time}T00:00:00Z`) / 1000
  return Date.UTC(time.year, time.month - 1, time.day) / 1000
}

/**
 * Time-axis labels in the zone, at most 8 characters (LWC's limit).
 *
 * LWC places tick marks on UTC boundaries (the bars are UTC-aligned), so in another zone a
 * "new day/month/year" tick may not fall on a local midnight. Such ticks are labelled with the
 * local date ("31 Oct") rather than a bare "Nov" or "1" that would name the wrong day.
 */
export function makeTickMarkFormatter(
  tz: string,
): (time: Time, type: TickMarkType, locale: string) => string {
  return (time, type) => {
    const p = zonedParts(timeToSeconds(time), tz)
    const atMidnight = p.hour === 0 && p.minute === 0 && p.second === 0
    const dayMonth = `${p.day} ${MONTHS[p.month - 1]}`
    switch (type) {
      case TickMarkType.Year:
        if (atMidnight && p.month === 1 && p.day === 1) return String(p.year)
        return atMidnight && p.day === 1 ? MONTHS[p.month - 1] : dayMonth
      case TickMarkType.Month:
        return atMidnight && p.day === 1 ? MONTHS[p.month - 1] : dayMonth
      case TickMarkType.DayOfMonth:
        return atMidnight ? String(p.day) : dayMonth
      case TickMarkType.Time:
        return `${pad2(p.hour)}:${pad2(p.minute)}`
      case TickMarkType.TimeWithSeconds:
        return `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}`
    }
  }
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const

/**
 * Crosshair time label, TradingView style: "Sun 25 Oct '26 14:00" in the zone, with seconds only
 * when the bar has them.
 */
export function makeTimeFormatter(tz: string): (time: Time) => string {
  return (time) => {
    const sec = timeToSeconds(time)
    if (!Number.isFinite(sec)) return ''
    const p = zonedParts(sec, tz)
    const weekday = WEEKDAYS[new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay()]
    const year = pad2(p.year % 100)
    const clock =
      sec % 60 !== 0
        ? `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}`
        : `${pad2(p.hour)}:${pad2(p.minute)}`
    return `${weekday} ${p.day} ${MONTHS[p.month - 1]} '${year} ${clock}`
  }
}
