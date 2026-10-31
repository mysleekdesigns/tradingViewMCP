import { memo, useMemo } from 'react'
import { useLiveSelector, type LiveStore, type PanelKey } from '../state/liveStore'
import { lastUpdateSelector } from './quote'

interface LastUpdateTextProps {
  /** "14:08:31" (in the display zone) of the last REST result or live tick, or null. */
  lastUpdate: string | null
  /** Short name of the display time zone, e.g. "UTC" or "EDT". */
  timezoneName: string
}

/** The status bar's "Last update 14:08:31 UTC" item; nothing before any data. */
export const LastUpdateText = memo(function LastUpdateText({
  lastUpdate,
  timezoneName,
}: LastUpdateTextProps) {
  'use memo'
  if (lastUpdate === null) return null
  return (
    // Not a live region: it changes on every tick.
    <span className="statusbar__item statusbar__clock" title="Time of the last price update">
      Last update {lastUpdate} {timezoneName}
    </span>
  )
})

interface LastUpdateClockProps {
  liveStore: LiveStore
  /** The panel whose last update is shown (the active chart). */
  panel: PanelKey
  /** IANA zone (Settings) the clock is formatted in. */
  timezone: string
  /** Its short name, shown after the time. */
  timezoneName: string
}

/**
 * The clock as a leaf that reads the live store itself: a tick re-renders only this, and only
 * when the HH:MM:SS text changes, never the StatusBar around it.
 */
export const LastUpdateClock = memo(function LastUpdateClock({
  liveStore,
  panel,
  timezone,
  timezoneName,
}: LastUpdateClockProps) {
  'use memo'
  const select = useMemo(() => lastUpdateSelector(panel, timezone), [panel, timezone])
  const lastUpdate = useLiveSelector(liveStore, select)
  return <LastUpdateText lastUpdate={lastUpdate} timezoneName={timezoneName} />
})
