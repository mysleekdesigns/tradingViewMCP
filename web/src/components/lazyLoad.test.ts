import { createElement as h, Suspense, type ComponentType } from 'react'
import { prerender } from 'react-dom/static'
import { describe, expect, it, vi } from 'vitest'
import {
  failedModuleUrl,
  LazyLoadError,
  panelLoadInFlight,
  reloadFailedStylesheets,
  retryableLazy,
} from './lazyLoad'

// Phase 19 wave 4 review: a rejected React.lazy stays rejected, so a panel whose chunk failed to
// download could never be opened again without a page reload. Each attempt is its own lazy().

function Panel(props: { symbol: string }) {
  return h('p', { 'data-real': 'Panel' }, props.symbol)
}

/** Render to HTML once everything has loaded (or reject with what the load threw). */
async function html(C: ComponentType<{ symbol: string }>): Promise<string> {
  const { prelude } = await prerender(h(C, { symbol: 'BTCUSDT' }))
  return (await new Response(prelude).text()).replace(/<!--\/?\$-->/g, '')
}

/** A loader that fails `failures` times, then resolves to `Panel`. */
function flaky(failures: number) {
  const calls = { n: 0, inFlight: [] as boolean[] }
  const load = async () => {
    calls.n += 1
    calls.inFlight.push(panelLoadInFlight())
    if (calls.n <= failures) throw new TypeError('Failed to fetch dynamically imported module')
    return Panel
  }
  return { load, calls }
}

describe('retryableLazy', () => {
  it('renders the loaded component with its props, through a React.lazy', async () => {
    const { load, calls } = flaky(0)
    const C = retryableLazy('the panel', load)
    expect((C.current as unknown as { $$typeof: symbol }).$$typeof).toBe(Symbol.for('react.lazy'))
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(calls.n).toBe(1)
  })

  it('a failed load rejects as a LazyLoadError that names the panel and keeps the cause', async () => {
    const { load } = flaky(1)
    const C = retryableLazy('the Strategy Tester', load)
    const err = await html(C).then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(LazyLoadError)
    expect((err as LazyLoadError).message).toBe("Couldn't load the Strategy Tester")
    expect((err as LazyLoadError).what).toBe('the Strategy Tester')
    expect((err as LazyLoadError).cause).toBeInstanceOf(TypeError)
  })

  it('a missing export (the loader throws while picking it) is a load failure too', async () => {
    const C = retryableLazy('Research', () =>
      Promise.resolve({} as Record<string, typeof Panel | undefined>).then((m) => {
        const c = m.ResearchPage
        if (c === undefined) throw new Error('no export')
        return c
      }),
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
  })

  it('stays rejected without retry; Retry makes a fresh lazy that succeeds on the second attempt', async () => {
    const { load, calls } = flaky(1)
    const C = retryableLazy('Seasonals', load)
    const first = C.current
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    // Rendering again reuses the rejected lazy: no new download, same error.
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    expect(calls.n).toBe(1)
    expect(C.current).toBe(first)

    C.retry()
    expect(C.current).not.toBe(first)
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(calls.n).toBe(2)
  })

  it('retry after a successful load changes nothing (no second download, no flash)', async () => {
    const { load, calls } = flaky(0)
    const C = retryableLazy('Settings', load)
    await html(C)
    const loaded = C.current
    C.retry()
    expect(C.current).toBe(loaded)
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(calls.n).toBe(1)
  })

  it('a pending first attempt is not replaced by retry either', () => {
    const C = retryableLazy('Settings', () => new Promise<typeof Panel>(() => {}))
    const pending = C.current
    C.retry()
    expect(C.current).toBe(pending)
  })
})

// Chrome keeps a failed dynamic import() for the life of the page (found in the 2026-11-01
// browser check): importing the same URL again rejects at once with no request, so Retry must
// ask for the chunk under a fresh URL.
describe('retryableLazy re-imports a failed chunk under a fresh URL', () => {
  const CHUNK = 'http://127.0.0.1:5173/assets/SettingsDialog-9Sna1Pil.js'
  const chromeFailure = (url: string) =>
    new TypeError(`Failed to fetch dynamically imported module: ${url}`)

  it('retry imports the URL the failure named with ?retry=1, and renders its export', async () => {
    const load = vi.fn(async (): Promise<typeof Panel> => {
      throw chromeFailure(CHUNK)
    })
    const importUrl = vi.fn(async () => ({ SettingsDialog: Panel }))
    const C = retryableLazy('Settings', load, { exportName: 'SettingsDialog', importUrl })
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)

    C.retry()
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(importUrl).toHaveBeenCalledExactlyOnceWith(`${CHUNK}?retry=1`)
    // The cached failure is not asked again.
    expect(load).toHaveBeenCalledOnce()
  })

  it('a retry that fails again uses the next query, not a stacked one', async () => {
    const importUrl = vi.fn(async (url: string): Promise<Record<string, unknown>> => {
      if (url.endsWith('retry=1')) throw chromeFailure(url)
      return { SettingsDialog: Panel }
    })
    const C = retryableLazy(
      'Settings',
      async () => {
        throw chromeFailure(CHUNK)
      },
      { exportName: 'SettingsDialog', importUrl },
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(importUrl.mock.calls.map(([u]) => u)).toEqual([`${CHUNK}?retry=1`, `${CHUNK}?retry=2`])
  })

  it('a missing export after a fresh import is a load error, not a crash', async () => {
    const C = retryableLazy(
      'Settings',
      async () => {
        throw chromeFailure(CHUNK)
      },
      { exportName: 'SettingsDialog', importUrl: async () => ({}) },
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
  })

  it('falls back to calling load again when the failure names no URL or no export name is given', async () => {
    const importUrl = vi.fn(async () => ({ SettingsDialog: Panel }))
    // Safari: "Importing a module script failed." names no URL.
    const { load, calls } = flaky(1)
    const C = retryableLazy('Settings', load, { exportName: 'SettingsDialog', importUrl })
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(calls.n).toBe(2)
    expect(importUrl).not.toHaveBeenCalled()

    // A URL in the failure but no export name: plain retry, as before.
    let n = 0
    const D = retryableLazy('Settings', async () => {
      n += 1
      if (n === 1) throw chromeFailure(CHUNK)
      return Panel
    })
    await expect(html(D)).rejects.toBeInstanceOf(LazyLoadError)
    D.retry()
    expect(await html(D)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(n).toBe(2)
  })
})

describe('retryableLazy retry details (review of the fresh-URL retry)', () => {
  const noStyles = async () => {}
  const chromeFailure = (url: string) =>
    new TypeError(`Failed to fetch dynamically imported module: ${url}`)

  it('keeps an existing query (dev ?t=) and adds &retry=n', async () => {
    const dev = 'http://127.0.0.1:5173/src/components/SettingsDialog.tsx?t=123'
    const importUrl = vi.fn(async () => ({ SettingsDialog: Panel }))
    const C = retryableLazy(
      'Settings',
      async () => {
        throw chromeFailure(dev)
      },
      { exportName: 'SettingsDialog', importUrl, reloadStylesheets: noStyles },
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    await html(C)
    expect(importUrl).toHaveBeenCalledExactlyOnceWith(`${dev}&retry=1`)
  })

  it('after a fresh import without the export, the next retry asks for retry=2', async () => {
    const urls: string[] = []
    const importUrl = async (url: string): Promise<Record<string, unknown>> => {
      urls.push(url)
      return url.endsWith('retry=1') ? {} : { SettingsDialog: Panel }
    }
    const C = retryableLazy(
      'Settings',
      async () => {
        throw chromeFailure('http://h/assets/S-1.js')
      },
      { exportName: 'SettingsDialog', importUrl, reloadStylesheets: noStyles },
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(urls).toEqual(['http://h/assets/S-1.js?retry=1', 'http://h/assets/S-1.js?retry=2'])
  })

  it('counts the fresh-URL import as a panel load in flight (main.tsx reload guard)', async () => {
    const seen: boolean[] = []
    const C = retryableLazy(
      'Settings',
      async () => {
        throw chromeFailure('http://h/assets/S-1.js')
      },
      {
        exportName: 'SettingsDialog',
        reloadStylesheets: noStyles,
        importUrl: async () => {
          seen.push(panelLoadInFlight())
          return { SettingsDialog: Panel }
        },
      },
    )
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    await html(C)
    expect(seen).toEqual([true])
    expect(panelLoadInFlight()).toBe(false)
  })

  it('a failed fresh-URL import fires vite:preloadError, so a rebuilt-away chunk can still reload the page', async () => {
    const events: string[] = []
    vi.stubGlobal('dispatchEvent', (e: Event) => {
      events.push(`${e.type}:${String(panelLoadInFlight())}`)
      return true
    })
    try {
      const C = retryableLazy(
        'Settings',
        async () => {
          throw chromeFailure('http://h/assets/S-old.js')
        },
        {
          exportName: 'SettingsDialog',
          reloadStylesheets: noStyles,
          importUrl: async (url) => {
            throw chromeFailure(url)
          },
        },
      )
      // The first load goes through Vite's preload helper, which fires the event itself.
      await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
      expect(events).toEqual([])
      C.retry()
      await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
      expect(events).toEqual(['vite:preloadError:true'])
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('re-requests failed stylesheets before every retry, never on the first load', async () => {
    const calls: number[] = []
    const reloadStylesheets = vi.fn(async (_host: unknown, n: number) => {
      calls.push(n)
    })
    const { load } = flaky(2)
    const C = retryableLazy('Settings', load, { reloadStylesheets })
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    expect(calls).toEqual([])
    C.retry()
    await expect(html(C)).rejects.toBeInstanceOf(LazyLoadError)
    C.retry()
    expect(await html(C)).toBe('<p data-real="Panel">BTCUSDT</p>')
    expect(calls).toEqual([1, 2])
  })
})

describe('reloadFailedStylesheets', () => {
  interface FakeLink {
    href: string
    sheet: { readonly cssRules: unknown } | null
    removed: boolean
    inserted: FakeLink[]
    listeners: Map<string, () => void>
    addEventListener(type: string, fn: () => void): void
    remove(): void
    after(node: FakeLink): void
    cloneNode(): FakeLink
  }
  /** A minimal <link>: `sheet` null means its load failed. */
  /** A sheet as Chrome leaves it after a failed load: present, but its rules are unreadable. */
  const unreadable = {
    get cssRules(): unknown {
      throw new DOMException('Cannot access rules', 'SecurityError')
    },
  }
  function fakeLink(href: string, loaded: boolean | 'unreadable'): FakeLink {
    const link: FakeLink = {
      href,
      sheet: loaded === 'unreadable' ? unreadable : loaded ? { cssRules: [] } : null,
      removed: false,
      inserted: [],
      listeners: new Map(),
      addEventListener(type, fn) {
        link.listeners.set(type, fn)
      },
      remove() {
        link.removed = true
      },
      after(node) {
        link.inserted.push(node)
      },
      cloneNode: () => fakeLink(href, false),
    }
    return link
  }
  const host = (links: FakeLink[]) =>
    ({ querySelectorAll: () => links }) as unknown as Parameters<typeof reloadFailedStylesheets>[0]

  it('re-adds only the failed links under ?retry=n and removes the old one once the new one loads', async () => {
    const ok = fakeLink('http://h/assets/Index-1.css', true)
    const bad = fakeLink('http://h/assets/Settings-1.css', false)
    const done = reloadFailedStylesheets(host([ok, bad]), 2, undefined, 'http://h')
    expect(ok.inserted).toEqual([])
    expect(bad.inserted).toHaveLength(1)
    const fresh = bad.inserted[0]
    expect(fresh.href).toBe('http://h/assets/Settings-1.css?retry=2')
    expect(bad.removed).toBe(false)
    fresh.listeners.get('load')?.()
    await done
    expect(bad.removed).toBe(true)
  })

  it('replaces an earlier retry query instead of stacking one, and settles on error too', async () => {
    const bad = fakeLink('http://h/assets/Settings-1.css?retry=1', false)
    const done = reloadFailedStylesheets(host([bad]), 2, undefined, 'http://h')
    expect(bad.inserted[0].href).toBe('http://h/assets/Settings-1.css?retry=2')
    bad.inserted[0].listeners.get('error')?.()
    await done
    expect(bad.removed).toBe(true)
  })

  it('gives up waiting after the timeout, so a retry never hangs on a stylesheet', async () => {
    vi.useFakeTimers()
    try {
      const bad = fakeLink('http://h/assets/Settings-1.css', false)
      const done = reloadFailedStylesheets(host([bad]), 1, 50)
      await vi.advanceTimersByTimeAsync(50)
      await done
      expect(bad.removed).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('treats a sheet with unreadable rules as failed (Chrome) and skips other origins', async () => {
    const chromeFailed = fakeLink('http://h/assets/Settings-1.css', 'unreadable')
    const font = fakeLink('https://fonts.example/css', 'unreadable')
    const done = reloadFailedStylesheets(host([chromeFailed, font]), 1, undefined, 'http://h')
    expect(font.inserted).toEqual([])
    expect(chromeFailed.inserted).toHaveLength(1)
    chromeFailed.inserted[0].listeners.get('load')?.()
    await done
    expect(chromeFailed.removed).toBe(true)
  })

  it('does nothing outside a browser', async () => {
    await expect(reloadFailedStylesheets(undefined, 1)).resolves.toBeUndefined()
  })
})

describe('failedModuleUrl', () => {
  it('strips an &retry=n it added after an existing query', () => {
    expect(
      failedModuleUrl(
        new TypeError(
          'Failed to fetch dynamically imported module: http://h/src/X.tsx?t=123&retry=2',
        ),
      ),
    ).toBe('http://h/src/X.tsx?t=123')
  })
})

describe('failedModuleUrl', () => {
  it('reads the URL from Chrome and Firefox messages and drops a retry query it added', () => {
    expect(
      failedModuleUrl(
        new TypeError('Failed to fetch dynamically imported module: http://h/a/X-1.js'),
      ),
    ).toBe('http://h/a/X-1.js')
    expect(
      failedModuleUrl(
        new TypeError('error loading dynamically imported module: http://h/a/X-1.js'),
      ),
    ).toBe('http://h/a/X-1.js')
    expect(
      failedModuleUrl(
        new TypeError('Failed to fetch dynamically imported module: http://h/a/X-1.js?retry=3'),
      ),
    ).toBe('http://h/a/X-1.js')
  })

  it('is null when the error names no module URL', () => {
    expect(failedModuleUrl(new TypeError('Importing a module script failed.'))).toBeNull()
    expect(failedModuleUrl(new Error('boom'))).toBeNull()
    expect(failedModuleUrl(undefined)).toBeNull()
  })
})

describe('panelLoadInFlight (main.tsx reloads only for a real open)', () => {
  it('is true while a lazy panel loads, false before and after (success or failure)', async () => {
    expect(panelLoadInFlight()).toBe(false)
    const ok = flaky(0)
    await html(retryableLazy('a', ok.load))
    expect(ok.calls.inFlight).toEqual([true])
    expect(panelLoadInFlight()).toBe(false)

    const bad = flaky(1)
    await html(retryableLazy('b', bad.load)).catch(() => {})
    expect(bad.calls.inFlight).toEqual([true])
    expect(panelLoadInFlight()).toBe(false)
  })

  it('counts overlapping loads: still in flight until the last one settles', async () => {
    let finishA: (c: typeof Panel) => void = () => {}
    const A = retryableLazy('a', () => new Promise<typeof Panel>((r) => (finishA = r)))
    const B = retryableLazy('b', async () => Panel)
    const a = prerender(h(Suspense, { fallback: 'loading' }, h(A, { symbol: 'X' })))
    await vi.waitFor(() => expect(panelLoadInFlight()).toBe(true))
    await html(B)
    expect(panelLoadInFlight()).toBe(true)
    finishA(Panel)
    await a
    expect(panelLoadInFlight()).toBe(false)
  })
})
