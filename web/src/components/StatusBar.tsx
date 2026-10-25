import type { ConnectionStatus } from '../types'
import { ConnectionIndicator } from './ConnectionStatus'

interface StatusBarProps {
  barCount: number | null
  /** Live-stream state. */
  status: ConnectionStatus
  /** "14:08:31" (in `timezoneName`) of the last REST result or live tick, or null before any data. */
  lastUpdate: string | null
  /** Short name of the display time zone (Settings), e.g. "UTC" or "EDT". */
  timezoneName: string
  /** Why the last refresh failed, while the previous candles are still shown. */
  refreshError: string | null
}

export function StatusBar({
  barCount,
  status,
  lastUpdate,
  timezoneName,
  refreshError,
}: StatusBarProps) {
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
      {/* Not a live region: it changes on every tick. */}
      {lastUpdate !== null && (
        <span className="statusbar__item statusbar__clock" title="Time of the last price update">
          Last update {lastUpdate} {timezoneName}
        </span>
      )}
      <ConnectionIndicator status={status} variant="full" />
    </footer>
  )
}
