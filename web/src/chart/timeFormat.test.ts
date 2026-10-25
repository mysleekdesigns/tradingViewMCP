import { TickMarkType, type Time, type UTCTimestamp } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import {
  formatDateTime,
  isValidTimezone,
  makeTickMarkFormatter,
  makeTimeFormatter,
  resolveTimezone,
  timeToSeconds,
  timezoneLabel,
} from './timeFormat'

// New York leaves daylight saving on Sun 1 Nov 2026 at 02:00 local = 06:00 UTC (EDT → EST).
const SAT_31_OCT_12Z = 1_793_448_000 // 2026-10-31 12:00:00 UTC (EDT, UTC-4)
const MON_2_NOV_12Z = 1_793_620_800 // 2026-11-02 12:00:00 UTC (EST, UTC-5)
const NOV_1_00Z = 1_793_491_200 // 2026-11-01 00:00:00 UTC (a "new month" tick)
const NOV_1_04Z = 1_793_505_600 // 2026-11-01 04:00:00 UTC = 00:00 EDT in New York
const JAN_1_2026_00Z = 1_767_225_600 // 2026-01-01 00:00:00 UTC (a "new year" tick)
const SUN_25_OCT_140307Z = 1_792_936_987 // 2026-10-25 14:03:07 UTC

const t = (sec: number) => sec as UTCTimestamp
const NY = 'America/New_York'

describe('resolveTimezone', () => {
  it("maps 'local' to the browser zone, keeps valid zones, and falls back to UTC", () => {
    expect(resolveTimezone('local')).toBeUndefined()
    expect(resolveTimezone('UTC')).toBe('UTC')
    expect(resolveTimezone(NY)).toBe(NY)
    expect(resolveTimezone('Mars/Olympus_Mons')).toBe('UTC')
    expect(resolveTimezone('')).toBe('UTC')
  })

  it('validates zones through Intl', () => {
    expect(isValidTimezone('Europe/London')).toBe(true)
    expect(isValidTimezone('not a zone')).toBe(false)
  })
})

describe('formatDateTime', () => {
  it('formats in UTC', () => {
    expect(formatDateTime(SUN_25_OCT_140307Z, 'UTC')).toBe('2026-10-25 14:03')
    expect(formatDateTime(SUN_25_OCT_140307Z, 'UTC', { seconds: true })).toBe('2026-10-25 14:03:07')
    expect(formatDateTime(SUN_25_OCT_140307Z, 'UTC', { dateOnly: true })).toBe('2026-10-25')
  })

  it('shifts by the zone offset on each side of the DST change', () => {
    // EDT: UTC-4
    expect(formatDateTime(SAT_31_OCT_12Z, NY)).toBe('2026-10-31 08:00')
    // EST: UTC-5
    expect(formatDateTime(MON_2_NOV_12Z, NY)).toBe('2026-11-02 07:00')
    // Crosses the date line back into the previous local day.
    expect(formatDateTime(NOV_1_00Z, NY)).toBe('2026-10-31 20:00')
  })

  it("'local' matches the runtime's own local clock", () => {
    const d = new Date(SUN_25_OCT_140307Z * 1000)
    const p = (n: number) => String(n).padStart(2, '0')
    const expected = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
    expect(formatDateTime(SUN_25_OCT_140307Z, 'local')).toBe(expected)
  })

  it('treats an invalid zone as UTC and a non-finite time as unknown', () => {
    expect(formatDateTime(SAT_31_OCT_12Z, 'Bad/Zone')).toBe('2026-10-31 12:00')
    expect(formatDateTime(Number.NaN, 'UTC')).toBe('—')
  })
})

describe('timezoneLabel', () => {
  it('names UTC and the DST state of a zone', () => {
    expect(timezoneLabel('UTC', SAT_31_OCT_12Z)).toBe('UTC')
    expect(timezoneLabel('nope', SAT_31_OCT_12Z)).toBe('UTC')
    expect(timezoneLabel(NY, SAT_31_OCT_12Z)).toBe('EDT')
    expect(timezoneLabel(NY, MON_2_NOV_12Z)).toBe('EST')
  })
})

describe('timeToSeconds', () => {
  it('accepts every LWC Time form', () => {
    expect(timeToSeconds(t(NOV_1_00Z))).toBe(NOV_1_00Z)
    expect(timeToSeconds('2026-11-01' as Time)).toBe(NOV_1_00Z)
    expect(timeToSeconds({ year: 2026, month: 11, day: 1 })).toBe(NOV_1_00Z)
  })
})

describe('makeTickMarkFormatter', () => {
  it('UTC labels match LWC defaults (yyyy / MMM / d / HH:mm / HH:mm:ss)', () => {
    const f = makeTickMarkFormatter('UTC')
    expect(f(t(JAN_1_2026_00Z), TickMarkType.Year, 'en-US')).toBe('2026')
    expect(f(t(NOV_1_00Z), TickMarkType.Month, 'en-US')).toBe('Nov')
    expect(f(t(NOV_1_00Z), TickMarkType.DayOfMonth, 'en-US')).toBe('1')
    expect(f(t(SAT_31_OCT_12Z), TickMarkType.Time, 'en-US')).toBe('12:00')
    expect(f(t(SUN_25_OCT_140307Z), TickMarkType.TimeWithSeconds, 'en-US')).toBe('14:03:07')
  })

  it('labels UTC boundary ticks with the local date when they are not a local midnight', () => {
    const f = makeTickMarkFormatter(NY)
    // UTC "new month" is still 31 Oct in New York: never label it "Nov".
    expect(f(t(NOV_1_00Z), TickMarkType.Month, 'en-US')).toBe('31 Oct')
    expect(f(t(JAN_1_2026_00Z), TickMarkType.Year, 'en-US')).toBe('31 Dec')
    expect(f(t(NOV_1_00Z), TickMarkType.DayOfMonth, 'en-US')).toBe('31 Oct')
    // 04:00 UTC is local midnight (EDT): a plain day number.
    expect(f(t(NOV_1_04Z), TickMarkType.DayOfMonth, 'en-US')).toBe('1')
    expect(f(t(SAT_31_OCT_12Z), TickMarkType.Time, 'en-US')).toBe('08:00')
    expect(f(t(MON_2_NOV_12Z), TickMarkType.Time, 'en-US')).toBe('07:00')
  })

  it('keeps every label within LWC’s 8-character limit', () => {
    const f = makeTickMarkFormatter(NY)
    const types = [
      TickMarkType.Year,
      TickMarkType.Month,
      TickMarkType.DayOfMonth,
      TickMarkType.Time,
      TickMarkType.TimeWithSeconds,
    ]
    for (const sec of [SAT_31_OCT_12Z, MON_2_NOV_12Z, NOV_1_00Z, JAN_1_2026_00Z]) {
      for (const type of types) expect(f(t(sec), type, 'en-US').length).toBeLessThanOrEqual(8)
    }
  })
})

describe('makeTimeFormatter', () => {
  it('formats the crosshair label TradingView style, seconds only when present', () => {
    expect(makeTimeFormatter('UTC')(t(SAT_31_OCT_12Z))).toBe("Sat 31 Oct '26 12:00")
    expect(makeTimeFormatter('UTC')(t(SUN_25_OCT_140307Z))).toBe("Sun 25 Oct '26 14:03:07")
  })

  it('uses the zone, including the weekday across midnight', () => {
    expect(makeTimeFormatter(NY)(t(NOV_1_00Z))).toBe("Sat 31 Oct '26 20:00")
    expect(makeTimeFormatter(NY)(t(MON_2_NOV_12Z))).toBe("Mon 2 Nov '26 07:00")
  })
})

describe('cached zone formatters', () => {
  it('builds no new Intl.DateTimeFormat for a zone it has seen, with identical output', () => {
    const zone = 'Asia/Kathmandu' // not used by the other tests, so the first calls are cold
    const coldValid = isValidTimezone(zone)
    const coldLabel = timezoneLabel(zone, SAT_31_OCT_12Z)
    const coldText = formatDateTime(SAT_31_OCT_12Z, zone, { seconds: true })
    const coldBad = isValidTimezone('Nowhere/Cached')

    const Real = Intl.DateTimeFormat
    let constructed = 0
    class Counting extends Real {
      constructor(...args: ConstructorParameters<typeof Intl.DateTimeFormat>) {
        super(...args)
        constructed++
      }
    }
    Intl.DateTimeFormat = Counting as typeof Intl.DateTimeFormat
    try {
      expect(isValidTimezone(zone)).toBe(coldValid)
      expect(isValidTimezone('Nowhere/Cached')).toBe(coldBad)
      expect(timezoneLabel(zone, SAT_31_OCT_12Z)).toBe(coldLabel)
      expect(formatDateTime(SAT_31_OCT_12Z, zone, { seconds: true })).toBe(coldText)
      expect(resolveTimezone(zone)).toBe(zone)
      expect(constructed).toBe(0)
    } finally {
      Intl.DateTimeFormat = Real
    }
    expect(coldValid).toBe(true)
    expect(coldBad).toBe(false)
    expect(coldLabel).toBe('GMT+5:45')
    expect(coldText).toBe('2026-10-31 17:45:00')
  })
})
