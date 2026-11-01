/**
 * Test-only: a tiny stand-in for React's hook runtime, so Node tests (no DOM library here) can
 * call a component or hook as a plain function and drive its handlers and effects. A test mocks
 * 'react' with these hooks (`vi.mock('react', ...)` spreading the real module), then `render`s.
 *
 * Slots are kept by call order like React's. `setState` updates the slot and is recorded, but
 * nothing re-renders by itself: call `render` again. Effects run after `render` when their deps
 * changed (cleanup first); `unmount` runs every cleanup. Not imported by the app.
 */

type Cleanup = void | (() => void)

interface EffectSlot {
  deps: readonly unknown[] | undefined
  cleanup: Cleanup
}

export interface StateUpdate {
  slot: number
  value: unknown
}

const states: { value: unknown }[] = []
const refs: { current: unknown }[] = []
const effects: (EffectSlot | undefined)[] = []
let pending: { slot: number; fn: () => Cleanup; deps: readonly unknown[] | undefined }[] = []
let cursor = { state: 0, ref: 0, effect: 0 }

export const hooks = {
  /** Every setState call since the last `resetHooks`, in order. */
  updates: [] as StateUpdate[],
  /** Picks a state slot's first value (e.g. render a menu already open). Default: `init`. */
  initState: null as ((init: unknown, slot: number) => unknown) | null,
}

const changed = (a: readonly unknown[] | undefined, b: readonly unknown[] | undefined) =>
  a === undefined ||
  b === undefined ||
  a.length !== b.length ||
  a.some((v, i) => !Object.is(v, b[i]))

export function useState<T>(init: T | (() => T)): [T, (next: T | ((prev: T) => T)) => void] {
  const slot = cursor.state++
  if (states[slot] === undefined) {
    const first = typeof init === 'function' ? (init as () => T)() : init
    states[slot] = { value: hooks.initState === null ? first : hooks.initState(first, slot) }
  }
  const cell = states[slot]
  const set = (next: T | ((prev: T) => T)) => {
    const value =
      typeof next === 'function' ? (next as (prev: T) => T)(cell.value as T) : (next as T)
    cell.value = value
    hooks.updates.push({ slot, value })
  }
  return [cell.value as T, set]
}

export function useRef<T>(current: T): { current: T } {
  const slot = cursor.ref++
  refs[slot] ??= { current }
  return refs[slot] as { current: T }
}

export function useEffect(fn: () => Cleanup, deps?: readonly unknown[]): void {
  const slot = cursor.effect++
  if (changed(effects[slot]?.deps, deps)) pending.push({ slot, fn, deps })
}

export const useLayoutEffect = useEffect

export function useMemo<T>(fn: () => T): T {
  return fn()
}

export function useCallback<T>(fn: T): T {
  return fn
}

export function useId(): string {
  return 'id'
}

/** Calls `fn` as one render, then runs the effects whose deps changed. */
export function render<T>(fn: () => T): T {
  cursor = { state: 0, ref: 0, effect: 0 }
  pending = []
  const out = fn()
  const run = pending
  pending = []
  for (const { slot, fn: effect, deps } of run) {
    const prev = effects[slot]?.cleanup
    if (typeof prev === 'function') prev()
    effects[slot] = { deps, cleanup: effect() }
  }
  return out
}

/** Runs every effect cleanup, as on unmount. */
export function unmount(): void {
  for (const e of effects) if (typeof e?.cleanup === 'function') e.cleanup()
  effects.length = 0
}

/** Forgets all state, refs, effects and recorded updates (between tests). */
export function resetHooks(): void {
  states.length = 0
  refs.length = 0
  effects.length = 0
  pending = []
  hooks.updates = []
  hooks.initState = null
}

/** Stand-in for `react/compiler-runtime` (shell components compiled with "use memo"). */
export function c(size: number): unknown[] {
  return new Array<unknown>(size).fill(Symbol.for('react.memo_cache_sentinel'))
}
