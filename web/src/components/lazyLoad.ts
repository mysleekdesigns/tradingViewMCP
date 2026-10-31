/*
 * How the lazily loaded panels (lazyPanels.tsx) fetch their code. Kept free of components so
 * main.tsx can ask whether a panel the user opened is loading without importing any UI.
 */
import {
  createElement,
  lazy,
  type ComponentType,
  type LazyExoticComponent,
  type ReactElement,
} from 'react'

/** A lazily loaded panel's code did not arrive (a failed fetch, or its module threw on load). */
export class LazyLoadError extends Error {
  /** What failed, e.g. "the Strategy Tester". */
  readonly what: string

  constructor(what: string, cause: unknown) {
    super(`Couldn't load ${what}`, { cause })
    this.name = 'LazyLoadError'
    this.what = what
  }
}

let realLoads = 0

/**
 * True while the code for a panel the user opened is downloading. A hover or focus preload
 * (SidebarRail) calls `import()` directly and never counts, so main.tsx's chunk-error handler
 * can tell a real open from a head start.
 */
export function panelLoadInFlight(): boolean {
  return realLoads > 0
}

/**
 * A lazily loaded component that can be loaded again after a failure. A `React.lazy` that
 * rejected stays rejected, so each attempt is its own `lazy()`: `retry()` drops a failed one and
 * the next render starts a fresh download. Render it like the real component (same props).
 */
export interface RetryableLazy<P extends object> {
  (props: P): ReactElement
  /** The current attempt (a `React.lazy`). */
  readonly current: LazyExoticComponent<ComponentType<P>>
  /** After a failed load: the next render loads the code again. Does nothing otherwise. */
  retry(): void
  displayName?: string
}

/**
 * `load` resolves to the component (e.g. `() => import('./X').then((m) => m.X)`). Any failure
 * (the fetch, the module, a missing export) rejects as a `LazyLoadError`, which `LazyBoundary`
 * shows in the panel's slot.
 */
export function retryableLazy<P extends object>(
  what: string,
  load: () => Promise<ComponentType<P>>,
): RetryableLazy<P> {
  let failed = false
  const attempt = () =>
    lazy(async () => {
      realLoads += 1
      try {
        return { default: await load() }
      } catch (err) {
        failed = true
        throw new LazyLoadError(what, err)
      } finally {
        realLoads -= 1
      }
    })
  let current = attempt()

  function Retryable(props: P): ReactElement {
    return createElement(current as ComponentType<P>, props)
  }
  Retryable.retry = () => {
    if (!failed) return
    failed = false
    current = attempt()
  }
  Retryable.displayName = `Lazy(${what})`
  Object.defineProperty(Retryable, 'current', { get: () => current })
  return Retryable as RetryableLazy<P>
}
