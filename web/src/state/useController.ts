// The controller-in-state hook pattern (Phase 19, Wave 5): a framework-free controller created
// in an effect, started, kept in a ref for callbacks, stopped on a deps change or unmount, and
// its snapshots shown only while they belong to the current deps.

import { useEffect, useRef, useState, type DependencyList, type RefObject } from 'react'

/** What useController drives: `start()` after creation, `dispose()` (else `stop()`) at the end. */
export interface ControllerLike {
  start?(): void
  stop?(): void
  dispose?(): void
}

/** A snapshot and the deps (snapshot key) of the controller that emitted it. */
export interface ControllerEntry<S> {
  key: DependencyList
  snapshot: S
}

/** Same length and every item `Object.is`, like React's deps comparison. */
export function sameDeps(a: DependencyList, b: DependencyList): boolean {
  return a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
}

/**
 * One controller's life, framework-free (the effect body of useController): create it with an
 * `emit` that records `{ key, snapshot }`, publish it in `ref`, start it. The returned cleanup
 * disposes (or stops) it and clears `ref` if it still points at it; after that, `emit` is a
 * no-op, so a stopped controller (StrictMode's first mount included) never reaches state.
 */
export function mountController<C extends ControllerLike, S>(
  factory: (emit: (snapshot: S) => void) => C,
  key: DependencyList,
  setEntry: (entry: ControllerEntry<S>) => void,
  ref: { current: C | null },
): () => void {
  let stopped = false
  const controller = factory((snapshot) => {
    if (!stopped) setEntry({ key, snapshot })
  })
  ref.current = controller
  controller.start?.()
  return () => {
    if (controller.dispose) controller.dispose()
    else controller.stop?.()
    stopped = true
    if (ref.current === controller) ref.current = null
  }
}

export interface UseController<C, S> {
  /** The latest snapshot emitted for the current `key`; null before the first one. */
  snapshot: S | null
  /** The running controller (null between a cleanup and the next creation); for callbacks. */
  controllerRef: RefObject<C | null>
}

/**
 * Creates `factory(emit)` after commit whenever `deps` change, starts it, and disposes (or
 * stops) it on the next change or unmount. `snapshot` is the last value passed to `emit`, shown
 * only while `key` (default `deps`) equals the key it was emitted under, so a changed key falls
 * back to null at once instead of showing the old controller's data. The factory reads the
 * render it was created in; read later values through refs.
 */
export function useController<C extends ControllerLike, S = never>(
  factory: (emit: (snapshot: S) => void) => C,
  deps: DependencyList,
  key: DependencyList = deps,
): UseController<C, S> {
  const [entry, setEntry] = useState<ControllerEntry<S> | null>(null)
  const controllerRef = useRef<C | null>(null)
  useEffect(
    () => mountController(factory, key, setEntry, controllerRef),
    // The caller's deps decide when a new controller is made (the useEffect contract).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  )
  const snapshot = entry !== null && sameDeps(entry.key, key) ? entry.snapshot : null
  return { snapshot, controllerRef }
}
