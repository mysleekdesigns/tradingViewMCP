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
 * The module URL a failed dynamic `import()` names, without a `retry` query this module added:
 * Chrome "Failed to fetch dynamically imported module: <url>", Firefox "error loading dynamically
 * imported module: <url>". Null when the error names none (Safari, or a failure that was not the
 * fetch).
 */
export function failedModuleUrl(err: unknown): string | null {
  const message = err instanceof Error ? err.message : typeof err === 'string' ? err : ''
  const match = /dynamically imported module:?\s*(\S+)/i.exec(message)
  if (match === null) return null
  return match[1].replace(/[?&]retry=\d+$/, '')
}

export interface RetryableLazyOptions {
  /**
   * The component's export name in the module `load` imports. With it, a retry after a failed
   * fetch imports the module again under a fresh URL (see `retryableLazy`).
   */
  exportName?: string
  /** Imports a module by URL. Tests pass a fake; the default is the browser's `import()`. */
  importUrl?: (url: string) => Promise<Record<string, unknown>>
  /** Runs before every retry. Tests pass a fake; the default is `reloadFailedStylesheets`. */
  reloadStylesheets?: (host: StylesheetHost | undefined, n: number) => Promise<void>
}

/** The page's document, or undefined outside a browser (tests, server rendering). */
function globalDocument(): StylesheetHost | undefined {
  return typeof document === 'undefined' ? undefined : document
}

const importByUrl = (url: string): Promise<Record<string, unknown>> =>
  import(/* @vite-ignore */ url) as Promise<Record<string, unknown>>

/** `url` with a `retry=n` query (replacing one added before), keeping any other query. */
function withRetryQuery(url: string, n: number): string {
  const base = url.replace(/[?&]retry=\d+$/, '')
  return `${base}${base.includes('?') ? '&' : '?'}retry=${n}`
}

/** The parts of `document` the stylesheet repair uses (tests pass a fake). */
export interface StylesheetHost {
  querySelectorAll(selector: string): ArrayLike<HTMLLinkElement>
}

/** Longest wait for a re-requested stylesheet before the retry goes on without it. */
export const STYLESHEET_RETRY_TIMEOUT_MS = 10_000

/**
 * True when a stylesheet link did not load. Chrome keeps a `sheet` object even for a failed load,
 * but reading its rules throws (SecurityError); a loaded same-origin sheet's rules read fine.
 */
export function stylesheetFailed(link: HTMLLinkElement): boolean {
  const sheet = link.sheet
  if (sheet === null) return true
  try {
    void sheet.cssRules
    return false
  } catch {
    return true
  }
}

/**
 * Re-requests every same-origin stylesheet `<link>` whose load failed (`stylesheetFailed`) under a
 * fresh `?retry=n` URL, and resolves once each settles (or after STYLESHEET_RETRY_TIMEOUT_MS).
 * Vite adds a lazy chunk's CSS link once per page and never again, so after an outage a retried
 * panel would otherwise render unstyled. Cross-origin sheets are left alone: their rules are
 * unreadable even when they loaded. The old link is removed when the new one settles, so a link
 * that fails again is retried once, not twice, next time.
 */
export function reloadFailedStylesheets(
  host: StylesheetHost | undefined,
  n: number,
  timeoutMs = STYLESHEET_RETRY_TIMEOUT_MS,
  origin: string | null = typeof location === 'undefined' ? null : location.origin,
): Promise<void> {
  if (host === undefined) return Promise.resolve()
  const failedLinks = Array.from(host.querySelectorAll('link[rel="stylesheet"]')).filter(
    (link) =>
      link.href !== '' &&
      (origin === null || link.href.startsWith(`${origin}/`)) &&
      stylesheetFailed(link),
  )
  return Promise.all(
    failedLinks.map(
      (old) =>
        new Promise<void>((resolve) => {
          const fresh = old.cloneNode() as HTMLLinkElement
          const settle = () => {
            clearTimeout(timer)
            old.remove()
            resolve()
          }
          const timer = setTimeout(settle, timeoutMs)
          fresh.addEventListener('load', settle, { once: true })
          fresh.addEventListener('error', settle, { once: true })
          fresh.href = withRetryQuery(old.href, n)
          old.after(fresh)
        }),
    ),
  ).then(() => undefined)
}

/**
 * Tells main.tsx's chunk-error handler that a panel's code failed again. Its first import went
 * through Vite's preload helper, which fires `vite:preloadError` itself; the fresh-URL import does
 * not, so without this a chunk that is gone for good (the app was rebuilt) could never trigger the
 * reload that recovers it. The handler's once-a-minute guard decides.
 */
function reportChunkFailure(): void {
  const target = globalThis as { dispatchEvent?: (e: Event) => boolean }
  if (typeof target.dispatchEvent === 'function' && typeof Event === 'function')
    target.dispatchEvent(new Event('vite:preloadError'))
}

/**
 * `load` resolves to the component (e.g. `() => import('./X').then((m) => m.X)`). Any failure
 * (the fetch, the module, a missing export) rejects as a `LazyLoadError`, which `LazyBoundary`
 * shows in the panel's slot.
 *
 * Chrome keeps a failed dynamic `import()` for the life of the page: importing the same URL again
 * rejects at once without a request, so a plain retry can never recover. When the failure named
 * the chunk's URL and `opts.exportName` is given, each retry imports that URL with a fresh
 * `?retry=n` query instead, which the browser fetches as a new module (its own imports still
 * resolve to the modules already loaded).
 */
export function retryableLazy<P extends object>(
  what: string,
  load: () => Promise<ComponentType<P>>,
  opts: RetryableLazyOptions = {},
): RetryableLazy<P> {
  let failed = false
  let failedUrl: string | null = null
  let retries = 0
  const loadAgain = async (url: string, exportName: string): Promise<ComponentType<P>> => {
    const mod = await (opts.importUrl ?? importByUrl)(withRetryQuery(url, retries))
    const component = mod[exportName]
    if (typeof component !== 'function' && (typeof component !== 'object' || component === null))
      throw new Error(`${url} has no export ${exportName}`)
    return component as ComponentType<P>
  }
  let started = 0
  const attempt = () =>
    lazy(async () => {
      realLoads += 1
      const isRetry = started > 0
      started += 1
      if (isRetry) retries += 1
      const fresh = isRetry && failedUrl !== null && opts.exportName !== undefined
      try {
        if (isRetry)
          await (opts.reloadStylesheets ?? reloadFailedStylesheets)(globalDocument(), retries)
        const component = fresh
          ? await loadAgain(failedUrl as string, opts.exportName as string)
          : await load()
        return { default: component }
      } catch (err) {
        failed = true
        failedUrl = failedModuleUrl(err) ?? failedUrl
        if (fresh) reportChunkFailure()
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
