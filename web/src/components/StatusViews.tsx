/** Faint candle silhouettes while the first data loads. Heights are fixed so it never jumps. */
const SKELETON_BARS = [38, 52, 44, 61, 55, 70, 64, 58, 73, 66, 80, 72, 68, 77, 84, 79, 71, 75]

export function LoadingView({ label }: { label: string }) {
  return (
    <div className="state state--loading" role="status">
      <div className="skeleton-chart" aria-hidden="true">
        {SKELETON_BARS.map((h, i) => (
          <span key={i} className="skeleton-chart__bar" style={{ height: `${h}%` }} />
        ))}
      </div>
      <p className="state__caption">{label}</p>
    </div>
  )
}

interface EmptyViewProps {
  title: string
  message: string
  action: string
  actionDisabled: boolean
  /** Why the action is unavailable, shown under the button. */
  note?: string | null
  onAction: () => void
}

/** Data loaded, but nothing to plot (e.g. no trades in the loaded period). Not an error. */
export function EmptyView({
  title,
  message,
  action,
  actionDisabled,
  note = null,
  onAction,
}: EmptyViewProps) {
  return (
    <div className="state" role="status">
      <div className="error-card empty-card">
        <svg
          className="empty-card__icon"
          width="28"
          height="28"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path
            d="M3 20h18M6 16V9M10 16v-4M14 16V6M18 16v-6"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeDasharray="2 2.5"
          />
        </svg>
        <h2 className="error-card__title">{title}</h2>
        <p className="error-card__message">{message}</p>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => onAction()}
          disabled={actionDisabled}
        >
          {action}
        </button>
        {note !== null && <p className="empty-card__note">{note}</p>}
      </div>
    </div>
  )
}

interface ErrorViewProps {
  title: string
  message: string
  onRetry: () => void
  retryDisabled: boolean
}

export function ErrorView({ title, message, onRetry, retryDisabled }: ErrorViewProps) {
  return (
    <div className="state" role="alert">
      <div className="error-card">
        <svg
          className="error-card__icon"
          width="28"
          height="28"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M12 7v6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          <circle cx="12" cy="16.5" r="1.1" fill="currentColor" />
        </svg>
        <h2 className="error-card__title">{title}</h2>
        <p className="error-card__message">{message}</p>
        <button
          type="button"
          className="btn btn--primary"
          onClick={onRetry}
          disabled={retryDisabled}
        >
          {retryDisabled ? 'Please wait…' : 'Try again'}
        </button>
      </div>
    </div>
  )
}
