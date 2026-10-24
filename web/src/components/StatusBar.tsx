interface StatusBarProps {
  barCount: number | null
  loadedAt: string | null
  /** Why the last refresh failed, while the previous snapshot is still shown. */
  refreshError: string | null
}

export function StatusBar({ barCount, loadedAt, refreshError }: StatusBarProps) {
  return (
    <footer className="statusbar">
      <span className="statusbar__item">Times in UTC</span>
      {barCount !== null && <span className="statusbar__item">{barCount} bars</span>}
      <span className="statusbar__spacer" />
      {/* Always mounted so screen readers announce the text when it appears. */}
      <span className="statusbar__item statusbar__item--error" role="status">
        {refreshError !== null ? `Refresh failed: ${refreshError}` : ''}
      </span>
      {loadedAt !== null && (
        <span className="statusbar__item" title="Data is a snapshot; press Refresh for newer bars">
          Snapshot loaded {loadedAt} UTC
        </span>
      )}
    </footer>
  )
}
