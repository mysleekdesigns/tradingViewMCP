import { memo, type ReactNode } from 'react'
import type { ConnectionStatus } from '../types'
import { ConnectionIndicator } from './ConnectionStatus'
import { LastUpdateText } from './LastUpdateClock'

interface StatusBarProps {
  barCount: number | null
  /** Live-stream state. */
  status: ConnectionStatus
  /** "14:08:31" (in `timezoneName`) of the last REST result or live tick, or null before any data. */
  lastUpdate?: string | null
  /**
   * The clock element in its place (e.g. a LastUpdateClock that reads the live store itself, so
   * a tick re-renders only that leaf). Pass a stable element; it wins over `lastUpdate`.
   */
  lastUpdateSlot?: ReactNode
  /** Short name of the display time zone (Settings), e.g. "UTC" or "EDT". */
  timezoneName: string
  /** Why the last refresh failed, while the previous candles are still shown. */
  refreshError: string | null
}

export const StatusBar = memo(function StatusBar({
  barCount,
  status,
  lastUpdate,
  lastUpdateSlot,
  timezoneName,
  refreshError,
}: StatusBarProps) {
  'use memo'
  return (
    <footer className="statusbar">
      <span
        className="statusbar__item"
        title="Time zone of the chart axis, crosshair and legend (change it in Settings). Bars stay aligned to UTC."
      >
        Times in {timezoneName}
      </span>
      {barCount !== null && <span className="statusbar__item">{barCount} bars</span>}
      <span className="statusbar__spacer" />
      {/* Always mounted so screen readers announce the text when it appears. */}
      <span className="statusbar__item statusbar__item--error" role="status">
        {refreshError !== null ? `Refresh failed: ${refreshError}` : ''}
      </span>
      {lastUpdateSlot !== undefined ? (
        lastUpdateSlot
      ) : (
        <LastUpdateText lastUpdate={lastUpdate ?? null} timezoneName={timezoneName} />
      )}
      <ConnectionIndicator status={status} variant="full" />
    </footer>
  )
})
