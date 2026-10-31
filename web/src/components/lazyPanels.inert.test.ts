import { beforeEach, describe, expect, it, vi } from 'vitest'

// No DOM in this suite: run InertWhile's effect by hand. `useEffect` is captured so the test can
// play React's part (run the effect after commit, its cleanup before the next one / on unmount).
const effects = vi.hoisted(() => ({ run: [] as (() => void | (() => void))[] }))
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react')>()
  return { ...actual, useEffect: (fn: () => void | (() => void)) => void effects.run.push(fn) }
})

const { InertWhile } = await import('./lazyPanels')

class FakeElement {
  attrs = new Map<string, string>()
  setAttribute(k: string, v: string) {
    this.attrs.set(k, v)
  }
  removeAttribute(k: string) {
    this.attrs.delete(k)
  }
}

function root(dock: FakeElement | null) {
  const selectors: string[] = []
  const el = {
    querySelector(sel: string) {
      selectors.push(sel)
      return dock
    },
  }
  return { ref: { current: el as unknown as HTMLElement }, selectors }
}

/** Render once and run the effect it registered; returns the effect's cleanup. */
function commit(props: Parameters<typeof InertWhile>[0]): () => void {
  effects.run.length = 0
  expect(InertWhile(props)).toBeNull()
  expect(effects.run).toHaveLength(1)
  return effects.run[0]() ?? (() => {})
}

describe('InertWhile (the tester dock under Research)', () => {
  beforeEach(() => {
    effects.run.length = 0
  })

  it('marks the element inert while active and clears it on cleanup', () => {
    const dock = new FakeElement()
    const { ref, selectors } = root(dock)
    const cleanup = commit({ rootRef: ref, selector: ':scope > .tester', active: true })
    expect(selectors).toEqual([':scope > .tester'])
    expect(dock.attrs.get('inert')).toBe('')
    cleanup()
    expect(dock.attrs.has('inert')).toBe(false)
  })

  it('does nothing while inactive', () => {
    const dock = new FakeElement()
    const { ref, selectors } = root(dock)
    commit({ rootRef: ref, selector: ':scope > .tester', active: false })
    expect(selectors).toEqual([])
    expect(dock.attrs.has('inert')).toBe(false)
  })

  it('tolerates a missing element or root', () => {
    expect(() => commit({ rootRef: root(null).ref, selector: '.x', active: true })()).not.toThrow()
    expect(() =>
      commit({ rootRef: { current: null }, selector: '.x', active: true })(),
    ).not.toThrow()
  })
})
