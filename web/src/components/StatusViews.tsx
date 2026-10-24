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
