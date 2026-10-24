import type { ConnectionStatus } from '../types'

const LABELS: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  live: 'Live',
  reconnecting: 'Reconnecting',
  offline: 'Offline',
}

const DESCRIPTIONS: Record<ConnectionStatus, string> = {
  connecting: 'Opening the live price stream…',
  live: 'Streaming live prices',
  reconnecting: 'Live stream dropped; reconnecting',
  offline: 'No connection to the live stream; still retrying',
}

interface ConnectionStatusProps {
  status: ConnectionStatus
  /**
   * 'full': dot + label inside a polite live region (status bar).
   * 'compact': dot only, with a tooltip and accessible name; not announced (header).
   */
  variant: 'full' | 'compact'
}

/** Green when live, amber pulsing while (re)connecting, red when offline. */
export function ConnectionIndicator({ status, variant }: ConnectionStatusProps) {
  const label = LABELS[status]
  if (variant === 'compact') {
    return (
      <span
        className={`conn conn--compact conn--${status}`}
        role="img"
        aria-label={`Connection: ${label}`}
        title={`${label} — ${DESCRIPTIONS[status]}`}
      >
        <span className="conn__dot" aria-hidden="true" />
      </span>
    )
  }
  // Only the label is in the live region, so price ticks are never announced.
  return (
    <span
      className={`conn conn--${status}`}
      role="status"
      aria-live="polite"
      title={DESCRIPTIONS[status]}
    >
      <span className="conn__dot" aria-hidden="true" />
      <span className="conn__label">{label}</span>
    </span>
  )
}
