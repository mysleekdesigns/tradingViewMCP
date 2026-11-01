// A tiny stand-in for React's hook runtime, for testing effect lifecycles in Node (no DOM).
// A test does `vi.mock('react', async (orig) => ({ ...(await orig()), ...fakeReactHooks }))`
// and drives a hook with `mountHook`. Effects run after each render like React's passive
// effects: changed deps' cleanups first (in order), then their bodies; `strict` replays
// React StrictMode's dev double-invoke (mount, cleanup all, mount again).

import type { DependencyList, EffectCallback } from 'react'

interface EffectSlot {
  deps: DependencyList | undefined
  cleanup: (() => void) | void
}

interface PendingEffect {
  index: number
  fn: EffectCallback
  deps: DependencyList | undefined
}

class Root {
  slots: unknown[] = []
  effects = new Map<number, EffectSlot>()
  pending: PendingEffect[] = []
  index = 0
  dirty = false
  rendering = false
}

let current: Root | null = null

function root(): Root {
  if (current === null) throw new Error('hook called outside mountHook')
  return current
}

function depsChanged(a: DependencyList | undefined, b: DependencyList | undefined): boolean {
  if (a === undefined || b === undefined) return true
  return a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]))
}

function useState<T>(init: T | (() => T)): [T, (next: T | ((prev: T) => T)) => void] {
  const r = root()
  const i = r.index++
  if (!(i in r.slots)) {
    r.slots[i] = typeof init === 'function' ? (init as () => T)() : init
  }
  const set = (next: T | ((prev: T) => T)) => {
    const prev = r.slots[i] as T
    const value = typeof next === 'function' ? (next as (p: T) => T)(prev) : next
    if (!Object.is(value, prev)) {
      r.slots[i] = value
      r.dirty = true
    }
  }
  return [r.slots[i] as T, set]
}

function useRef<T>(init: T): { current: T } {
  const r = root()
  const i = r.index++
  if (!(i in r.slots)) r.slots[i] = { current: init }
  return r.slots[i] as { current: T }
}

function useEffect(fn: EffectCallback, deps?: DependencyList): void {
  const r = root()
  const i = r.index++
  const prev = r.effects.get(i)
  if (prev === undefined || depsChanged(prev.deps, deps)) r.pending.push({ index: i, fn, deps })
}

function memoSlot<T>(fn: () => T, deps: DependencyList): T {
  const r = root()
  const i = r.index++
  const prev = r.slots[i] as { deps: DependencyList; value: T } | undefined
  if (prev !== undefined && !depsChanged(prev.deps, deps)) return prev.value
  const value = fn()
  r.slots[i] = { deps, value }
  return value
}

const useMemo = memoSlot

function useCallback<T>(fn: T, deps: DependencyList): T {
  return memoSlot(() => fn, deps)
}

/** Spread into the `react` mock. */
export const fakeReactHooks = { useState, useRef, useEffect, useMemo, useCallback }

export interface HookHandle<P, R> {
  /** The latest result. */
  readonly result: R
  /** Render with new props (and commit effects). */
  rerender(props: P): void
  /** Re-render with the same props if a state setter ran since the last render. */
  flush(): void
  /** Run every effect cleanup, like an unmount. */
  unmount(): void
}

export function mountHook<P, R>(
  hook: (props: P) => R,
  props: P,
  opts: { strict?: boolean } = {},
): HookHandle<P, R> {
  const r = new Root()
  let lastProps = props
  let result: R
  let mounted = false

  const runCleanup = (i: number) => {
    const slot = r.effects.get(i)
    if (slot !== undefined && typeof slot.cleanup === 'function') slot.cleanup()
  }

  const render = (p: P) => {
    lastProps = p
    current = r
    r.index = 0
    r.pending = []
    r.dirty = false
    try {
      result = hook(p)
    } finally {
      current = null
    }
    const pending = r.pending
    for (const e of pending) runCleanup(e.index)
    for (const e of pending) r.effects.set(e.index, { deps: e.deps, cleanup: e.fn() })
    if (!mounted && opts.strict === true) {
      for (const e of pending) runCleanup(e.index)
      for (const e of pending) r.effects.set(e.index, { deps: e.deps, cleanup: e.fn() })
    }
    mounted = true
  }

  render(props)
  return {
    get result() {
      return result
    },
    rerender: (p) => render(p),
    flush: () => {
      if (r.dirty) render(lastProps)
    },
    unmount: () => {
      for (const i of [...r.effects.keys()]) runCleanup(i)
      r.effects.clear()
    },
  }
}
