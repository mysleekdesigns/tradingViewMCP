interface StatusBarProps {
  barCount: number | null
  loadedAt: string | null
}

export function StatusBar({ barCount, loadedAt }: StatusBarProps) {
  return (
    <footer className="statusbar">
      <span className="statusbar__item">Times in UTC</span>
      {barCount !== null && <span className="statusbar__item">{barCount} bars</span>}
      <span className="statusbar__spacer" />
      {loadedAt !== null && (
        <span className="statusbar__item" title="Data is a snapshot; press Refresh for newer bars">
          Snapshot loaded {loadedAt} UTC
        </span>
      )}
    </footer>
  )
}
