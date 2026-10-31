import { createElement as h, Suspense, type ComponentType } from 'react'
import { prerender } from 'react-dom/static'
import { describe, expect, it, vi } from 'vitest'
import { LazyLoadError, panelLoadInFlight, retryableLazy } from './lazyLoad'

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
