/*
 * The five heavy views (Research, the Strategy Tester dock, paper trading, Seasonals and
 * Settings), and three small dialogs further down, are only mounted while open, so they load on
 * first open instead of with the app (Phase 19, D1). Each is a retryable `React.lazy` wrapper (`lazyLoad.ts`) with the same props as
 * the real component; its stylesheet travels in the same chunk. Nothing here imports those
 * modules statically: a static import anywhere in the shell would pull them back into the main
 * chunk. If the code does not arrive, `LazyBoundary` shows "Couldn't load …" in the panel's slot.
 */
import {
  Component,
  Fragment,
  Suspense,
  useEffect,
  useId,
  useRef,
  useState,
  type ErrorInfo,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import type { AlertDialogProps } from './AlertDialog'
import type { IndicatorSettingsProps } from './IndicatorSettings'
import { LazyLoadError, retryableLazy } from './lazyLoad'
import type { ShortcutsDialogProps } from './ShortcutsDialog'
import { useEscapeToClose } from './useEscapeToClose'

export const BacktestPanel = retryableLazy('the Strategy Tester', () =>
  import('./BacktestPanel').then((m) => m.BacktestPanel),
)
export const ResearchPage = retryableLazy('Research', () =>
  import('./ResearchPage').then((m) => m.ResearchPage),
)
export const PaperTradingPanel = retryableLazy('paper trading', () =>
  import('./PaperTradingPanel').then((m) => m.PaperTradingPanel),
)
export const SeasonalsView = retryableLazy('Seasonals', () =>
  import('./SeasonalsView').then((m) => m.SeasonalsView),
)
export const SettingsDialog = retryableLazy('Settings', () =>
  import('./SettingsDialog').then((m) => m.SettingsDialog),
)

const LazyIndicatorSettings = retryableLazy('indicator settings', () =>
  import('./IndicatorSettings').then((m) => m.IndicatorSettings),
)
const LazyAlertDialog = retryableLazy('the alert dialog', () =>
  import('./AlertDialog').then((m) => m.AlertDialog),
)
const LazyShortcutsDialog = retryableLazy('keyboard shortcuts', () =>
  import('./ShortcutsDialog').then((m) => m.ShortcutsDialog),
)

/*
 * Three rarely opened dialogs (Phase 19, D4 follow-up). Unlike the views above, each carries its
 * own Suspense boundary with the same 'dialog' placeholder Settings uses (empty for the first
 * `PANEL_LOADING_DELAY_MS`, a backdrop click closes), so App renders them as before. Their
 * stylesheets: Indicators.css and Alerts.css stay in the main chunk (the header, the alerts panel
 * and the toasts import them); Shortcuts.css is used by the shortcuts dialog only and moves with
 * it. Inside the Suspense boundary, a `LazyBoundary` shows the dialog-sized "Couldn't load" card
 * if the code does not arrive.
 */

/** The indicator settings dialog, loaded on first open. Same props as `IndicatorSettings`. */
export function IndicatorSettings(props: IndicatorSettingsProps) {
  return (
    <Suspense
      fallback={
        <PanelLoading
          variant="dialog"
          label="Loading indicator settings…"
          onClose={props.onClose}
        />
      }
    >
      <LazyBoundary
        variant="dialog"
        name="indicator settings"
        retry={LazyIndicatorSettings.retry}
        onClose={props.onClose}
      >
        <LazyIndicatorSettings {...props} />
      </LazyBoundary>
    </Suspense>
  )
}

/** The create/edit alert dialog, loaded on first open. Same props as `AlertDialog`. */
export function AlertDialog(props: AlertDialogProps) {
  return (
    <Suspense
      fallback={<PanelLoading variant="dialog" label="Loading alert…" onClose={props.onClose} />}
    >
      <LazyBoundary
        variant="dialog"
        name="the alert dialog"
        retry={LazyAlertDialog.retry}
        onClose={props.onClose}
      >
        <LazyAlertDialog {...props} />
      </LazyBoundary>
    </Suspense>
  )
}

/** The keyboard shortcuts dialog, loaded on first open. Same props as `ShortcutsDialog`. */
export function ShortcutsDialog(props: ShortcutsDialogProps) {
  return (
    <Suspense
      fallback={
        <PanelLoading variant="dialog" label="Loading shortcuts…" onClose={props.onClose} />
      }
    >
      <LazyBoundary
        variant="dialog"
        name="keyboard shortcuts"
        retry={LazyShortcutsDialog.retry}
        onClose={props.onClose}
      >
        <LazyShortcutsDialog {...props} />
      </LazyBoundary>
    </Suspense>
  )
}

/**
 * How long a panel may take to load before its loading state appears. A local chunk arrives in
 * a few milliseconds, so the usual first open shows nothing extra; only a slow load (a cold
 * disk, a busy machine) gets the spinner.
 */
export const PANEL_LOADING_DELAY_MS = 200

/** Where the panel opens, so the placeholder holds the same slot. */
export type PanelLoadingVariant = 'dock' | 'overlay' | 'sidebar' | 'dialog'

export interface PanelLoadingProps {
  variant: PanelLoadingVariant
  /** e.g. "Loading Research…" */
  label: string
  /** Dialogs only: a click on the backdrop closes, as it does on the real dialog. */
  onClose?(): void
}

function useDelayed(ms: number): boolean {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const id = window.setTimeout(() => setShown(true), ms)
    return () => window.clearTimeout(id)
  }, [ms])
  return shown
}

/**
 * Suspense fallback for a lazily loaded panel. The dock keeps its frame from the first frame
 * (the grid row is already reserved for it), so nothing shifts when the content arrives; the
 * overlay, sidebar and dialog placeholders stay empty until `PANEL_LOADING_DELAY_MS` so a fast
 * load never flashes. The spinner and caption match the panels' own loading rows.
 */
export function PanelLoading(props: PanelLoadingProps) {
  return <PanelLoadingView {...props} shown={useDelayed(PANEL_LOADING_DELAY_MS)} />
}

/** `PanelLoading` before (`shown` false) and after its delay. */
export function PanelLoadingView({
  variant,
  label,
  onClose,
  shown,
}: PanelLoadingProps & { shown: boolean }) {
  const status = shown ? (
    <div className="lazy-panel__status" role="status">
      <span className="lazy-panel__spinner" aria-hidden="true" />
      {label}
    </div>
  ) : null
  if (variant === 'dock') {
    return (
      <section className="tester lazy-panel" aria-busy="true" aria-label={label}>
        {status}
      </section>
    )
  }
  if (!shown) return null
  if (variant === 'dialog') {
    return (
      <div
        className="modal-backdrop modal-backdrop--center"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) onClose?.()
        }}
      >
        <div className="lazy-panel lazy-panel--card">{status}</div>
      </div>
    )
  }
  return (
    <div className={`lazy-panel${variant === 'overlay' ? ' lazy-panel--overlay' : ''}`}>
      {status}
    </div>
  )
}

export interface LazyLoadFailedProps {
  variant: PanelLoadingVariant
  /** What failed, for the title: "Couldn't load {name}". */
  name: string
  onRetry(): void
  /** The panel's own close handler. */
  onClose(): void
}

/** The alert icon of the chart's error card (StatusViews `ErrorView`), at caption size. */
function ErrorIcon() {
  return (
    <svg
      className="lazy-panel__error-icon"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M12 7v6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="12" cy="16.5" r="1.1" fill="currentColor" />
    </svg>
  )
}

/**
 * A panel's code did not arrive: said in the slot its placeholder held (dock frame, Research
 * overlay, sidebar, or a dialog card over the backdrop), with Retry and Close. The overlay and the
 * dialog take focus (on Retry) and close on Escape wherever focus is (also after a click left it on
 * <body>), like the views they stand in for; the dock and the sidebar leave focus where it is.
 */
export function LazyLoadFailed({ variant, name, onRetry, onClose }: LazyLoadFailedProps) {
  const ids = useId()
  const retryRef = useRef<HTMLButtonElement>(null)
  const modal = variant === 'dialog' || variant === 'overlay'
  useEffect(() => {
    if (modal) retryRef.current?.focus()
  }, [modal])
  // Window-level, like the Research view and its render-error panel. It leaves keys from inside a
  // [role="alertdialog"] alone, so the dialog card also handles Escape itself (and keeps it from
  // the chart's shortcuts, as the real dialogs do).
  useEscapeToClose(onClose, modal)
  const isDialog = variant === 'dialog'
  const onDialogKeyDown = (e: KeyboardEvent) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return
    e.preventDefault()
    e.stopPropagation()
    onClose()
  }
  const body = (
    <div className="lazy-panel__error" role={isDialog ? undefined : 'alert'}>
      <ErrorIcon />
      <p id={`${ids}-title`} className="lazy-panel__error-title">
        Couldn&apos;t load {name}
      </p>
      <p id={`${ids}-text`} className="lazy-panel__error-text">
        Its code did not download. Try again, or reload the page if the app was just updated.
      </p>
      <div className="lazy-panel__actions">
        <button ref={retryRef} type="button" className="btn btn--primary" onClick={onRetry}>
          Retry
        </button>
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  )
  if (variant === 'dock') {
    return (
      <section className="tester lazy-panel" aria-labelledby={`${ids}-title`}>
        {body}
      </section>
    )
  }
  if (isDialog) {
    return (
      <div
        className="modal-backdrop modal-backdrop--center"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) onClose()
        }}
      >
        <div
          className="lazy-panel lazy-panel--card"
          role="alertdialog"
          aria-modal="true"
          aria-labelledby={`${ids}-title`}
          aria-describedby={`${ids}-text`}
          onKeyDown={onDialogKeyDown}
        >
          {body}
        </div>
      </div>
    )
  }
  return (
    <div className={`lazy-panel${variant === 'overlay' ? ' lazy-panel--overlay' : ''}`}>{body}</div>
  )
}

export interface LazyBoundaryProps {
  variant: PanelLoadingVariant
  /** What is loading, for "Couldn't load {name}" (e.g. "the Strategy Tester"). */
  name: string
  /** The lazy wrapper's `retry`: drops the failed attempt so the next render loads again. */
  retry(): void
  /** The panel's own close handler. */
  onClose(): void
  children: ReactNode
}

interface LazyBoundaryState {
  /** What a child threw, boxed (a render may throw anything, even null). */
  caught: { value: unknown } | null
  /** Bumped by Retry: the children mount afresh, with fresh state. */
  attempt: number
}

/**
 * Contains a lazily loaded panel whose code failed to load (a `LazyLoadError`), so the chart,
 * the sockets and the rest of the app stay mounted: the panel's slot shows `LazyLoadFailed`.
 * Retry drops the failed `React.lazy` (`retry`) and mounts the children again; Close calls the
 * panel's close handler. However the boundary goes away while failed (Close, the rail button, a
 * sidebar switch, a shortcut), it drops the failed attempt on unmount, so the next open downloads
 * again. Any other error is not a load failure: it is rethrown to the next boundary up
 * (Research's `ErrorBoundary`), as before.
 */
export class LazyBoundary extends Component<LazyBoundaryProps, LazyBoundaryState> {
  state: LazyBoundaryState = { caught: null, attempt: 0 }

  static getDerivedStateFromError(value: unknown): Partial<LazyBoundaryState> {
    return { caught: { value } }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    if (!(error instanceof LazyLoadError)) return
    console.error(error.message, error.cause, info.componentStack)
  }

  retry = (): void => {
    this.props.retry()
    this.setState((s) => ({ caught: null, attempt: s.attempt + 1 }))
  }

  close = (): void => {
    this.props.onClose()
  }

  componentWillUnmount(): void {
    if (this.state.caught?.value instanceof LazyLoadError) this.props.retry()
  }

  render(): ReactNode {
    const { caught, attempt } = this.state
    if (caught === null) return <Fragment key={attempt}>{this.props.children}</Fragment>
    // Not a load failure: not ours to contain. Rethrown from render, it reaches the next boundary.
    if (!(caught.value instanceof LazyLoadError)) throw caught.value
    return (
      <LazyLoadFailed
        variant={this.props.variant}
        name={this.props.name}
        onRetry={this.retry}
        onClose={this.close}
      />
    )
  }
}

export interface InertWhileProps {
  /** The element to search from (App's root). */
  rootRef: RefObject<HTMLElement | null>
  /** `querySelector` for the element to mark, e.g. ':scope > .tester'. */
  selector: string
  active: boolean
}

/**
 * Marks an element `inert` while `active`. Rendered inside a lazy panel's Suspense boundary,
 * next to the panel, it runs once the real panel (not its placeholder) is in the DOM, so a panel
 * that finishes loading while Research covers it still leaves the tab order.
 */
export function InertWhile({ rootRef, selector, active }: InertWhileProps) {
  useEffect(() => {
    if (!active) return
    const el = rootRef.current?.querySelector<HTMLElement>(selector) ?? null
    el?.setAttribute('inert', '')
    return () => el?.removeAttribute('inert')
  }, [rootRef, selector, active])
  return null
}
