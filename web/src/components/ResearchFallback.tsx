/*
 * Research's render-error panel. It lives outside ResearchPage.tsx so it is in the main chunk:
 * App shows it from an error boundary, and it must not depend on the lazily loaded chunk. It is
 * only shown after the Research view rendered and threw, so Research.css (in that chunk) has
 * loaded by then; this module does not import it, which would put it back in the main stylesheet.
 * A failed download of the Research chunk is not shown here but by `LazyBoundary`.
 */
import { useEffect, useId, useRef } from 'react'
import { ResearchIcon } from './icons'
import { useEscapeToClose } from './useEscapeToClose'

export interface ResearchFallbackProps {
  error: Error
  /** Mount the Research view again. */
  onReload(): void
  onClose(): void
}

/**
 * Shown in the Research view's place when it threw while rendering (via `ErrorBoundary`), so a
 * bad answer from the lab never blanks the whole app. Same slot, same Esc / Back behaviour.
 */
export function ResearchFallback({ error, onReload, onClose }: ResearchFallbackProps) {
  const ids = useId()
  const reloadRef = useRef<HTMLButtonElement>(null)
  useEscapeToClose(onClose)
  // Focus was inside the view that just failed; keep the keyboard in this panel.
  useEffect(() => {
    reloadRef.current?.focus()
  }, [])
  return (
    <section className="research" aria-labelledby={`${ids}-title`}>
      <header className="research__head">
        <span className="research__head-icon" aria-hidden="true">
          <ResearchIcon size={16} />
        </span>
        <h2 id={`${ids}-title`} className="research__title">
          Research
        </h2>
        <span className="research__spacer" />
        <button
          type="button"
          className="research__back"
          onClick={onClose}
          title="Back to the chart (Esc)"
        >
          <ChartIcon />
          Back to chart
        </button>
      </header>
      <div className="research__body">
        <div className="research__state research__state--bad" role="alert">
          <span className="research__state-icon" aria-hidden="true">
            <WarnIcon />
          </span>
          <p className="research__state-title">Research view failed to render</p>
          <p className="research__state-text">
            Part of the lab&apos;s answer could not be shown. The chart and the Strategy Tester are
            unaffected.
          </p>
          <p className="research__state-text research__mono research__fallback-detail">
            {error.message}
          </p>
          <button
            ref={reloadRef}
            type="button"
            className="research__btn research__btn--primary"
            onClick={onReload}
          >
            Reload
          </button>
        </div>
      </div>
    </section>
  )
}

// Shared with ResearchPage (same 16 px stroke icons as its own).
const SMALL = {
  width: 16,
  height: 16,
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const

/** A small line chart: "Back to chart". */
export function ChartIcon() {
  return (
    <svg {...SMALL}>
      <path d="M2.5 13.5h11M4 11l3-3.5 2.5 2L13 5" />
    </svg>
  )
}

/** A warning triangle: failed states. */
export function WarnIcon() {
  return (
    <svg {...SMALL} width={14} height={14}>
      <path d="M8 2.5 14 13H2L8 2.5ZM8 6.5v3M8 11.3v.01" />
    </svg>
  )
}
