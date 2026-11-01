// Phase 19 Wave 5: useController, the shared controller-in-state lifecycle. The effect runs
// through the hook harness (no DOM); mountController is also tested on its own.
import { describe, expect, it, vi } from 'vitest'
import { mountHook } from './hookHarness.testkit'
import { mountController, sameDeps, useController, type ControllerEntry } from './useController'

// The harness is imported inside the (hoisted) factory, before the hooks under test load.
vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...(await import('./hookHarness.testkit')).fakeReactHooks,
}))

/** A controller that records its calls and emits on demand. */
class Fake {
  static made: Fake[] = []
  log: string[] = []
  readonly name: string
  readonly emit: (s: string) => void
  constructor(name: string, emit: (s: string) => void) {
    this.name = name
    this.emit = emit
    Fake.made.push(this)
  }
  start() {
    this.log.push('start')
  }
  stop() {
    this.log.push('stop')
  }
}

class Disposable extends Fake {
  dispose() {
    this.log.push('dispose')
  }
}

describe('sameDeps', () => {
  it('compares length and each item with Object.is', () => {
    const o = {}
    expect(sameDeps([o, 'a'], [o, 'a'])).toBe(true)
    expect(sameDeps([o], [{}])).toBe(false)
    expect(sameDeps(['a'], ['a', 'b'])).toBe(false)
    expect(sameDeps([NaN], [NaN])).toBe(true)
    expect(sameDeps([], [])).toBe(true)
  })
})

describe('mountController', () => {
  it('creates, publishes and starts; cleanup stops, clears the ref and mutes emit', () => {
    const entries: ControllerEntry<string>[] = []
    const ref: { current: Fake | null } = { current: null }
    let c!: Fake
    const cleanup = mountController<Fake, string>(
      (emit) => (c = new Fake('a', emit)),
      ['k'],
      (e) => entries.push(e),
      ref,
    )
    expect(ref.current).toBe(c)
    expect(c.log).toEqual(['start'])
    c.emit('one')
    expect(entries).toEqual([{ key: ['k'], snapshot: 'one' }])
    cleanup()
    expect(c.log).toEqual(['start', 'stop'])
    expect(ref.current).toBeNull()
    c.emit('late')
    expect(entries).toHaveLength(1)
  })

  it('prefers dispose() over stop() and keeps a newer controller in the ref', () => {
    const ref: { current: Fake | null } = { current: null }
    let c!: Disposable
    const cleanup = mountController(
      (emit) => (c = new Disposable('a', emit)),
      [],
      () => {},
      ref,
    )
    const newer = new Fake('b', () => {})
    ref.current = newer
    cleanup()
    expect(c.log).toEqual(['start', 'dispose'])
    expect(ref.current).toBe(newer)
  })

  it('lets an emit made while stopping through (as the hand-written effects did)', () => {
    const entries: string[] = []
    class EmitsOnStop extends Fake {
      stop() {
        super.stop()
        this.emit('final')
      }
    }
    const cleanup = mountController<Fake, string>(
      (emit) => new EmitsOnStop('a', emit),
      [],
      (e) => entries.push(e.snapshot),
      { current: null },
    )
    cleanup()
    expect(entries).toEqual(['final'])
  })

  it('a factory with no start/stop is fine', () => {
    const cleanup = mountController(
      () => ({}),
      [],
      () => {},
      { current: null },
    )
    expect(() => cleanup()).not.toThrow()
  })
})

describe('useController', () => {
  type Props = { name: string; other?: number }
  const useProbe = ({ name, other = 0 }: Props) =>
    useController<Fake, string>((emit) => new Fake(name, emit), [name, other], [name])

  it('emits into the snapshot; a new key shows null until the new controller emits', () => {
    Fake.made = []
    const h = mountHook(useProbe, { name: 'a' })
    expect(h.result.snapshot).toBeNull()
    const [a] = Fake.made
    expect(h.result.controllerRef.current).toBe(a)
    a.emit('a1')
    h.flush()
    expect(h.result.snapshot).toBe('a1')

    h.rerender({ name: 'b' })
    const b = Fake.made[1]
    expect(a.log).toEqual(['start', 'stop'])
    expect(b.log).toEqual(['start'])
    expect(h.result.snapshot).toBeNull()
    expect(h.result.controllerRef.current).toBe(b)
    a.emit('stale')
    h.flush()
    expect(h.result.snapshot).toBeNull()
    b.emit('b1')
    h.flush()
    expect(h.result.snapshot).toBe('b1')

    h.unmount()
    expect(b.log).toEqual(['start', 'stop'])
    expect(h.result.controllerRef.current).toBeNull()
  })

  it('a deps change outside the key recreates the controller but keeps the snapshot', () => {
    Fake.made = []
    const h = mountHook(useProbe, { name: 'a', other: 1 })
    Fake.made[0].emit('a1')
    h.flush()
    h.rerender({ name: 'a', other: 2 })
    expect(Fake.made).toHaveLength(2)
    expect(Fake.made[0].log).toEqual(['start', 'stop'])
    expect(h.result.snapshot).toBe('a1')
  })

  it('unchanged deps keep the same controller across renders', () => {
    Fake.made = []
    const h = mountHook(useProbe, { name: 'a' })
    h.rerender({ name: 'a' })
    h.rerender({ name: 'a' })
    expect(Fake.made).toHaveLength(1)
    expect(Fake.made[0].log).toEqual(['start'])
  })

  it('StrictMode: the first (stopped) controller never reaches state', () => {
    Fake.made = []
    const h = mountHook(useProbe, { name: 'a' }, { strict: true })
    const [first, second] = Fake.made
    expect(Fake.made).toHaveLength(2)
    expect(first.log).toEqual(['start', 'stop'])
    expect(second.log).toEqual(['start'])
    expect(h.result.controllerRef.current).toBe(second)
    first.emit('ghost')
    h.flush()
    expect(h.result.snapshot).toBeNull()
    second.emit('real')
    h.flush()
    expect(h.result.snapshot).toBe('real')
  })

  it('the key defaults to deps', () => {
    Fake.made = []
    const h = mountHook(
      ({ n }: { n: number }) => useController<Fake, string>((emit) => new Fake('x', emit), [n]),
      { n: 1 },
    )
    Fake.made[0].emit('one')
    h.flush()
    expect(h.result.snapshot).toBe('one')
    h.rerender({ n: 2 })
    expect(h.result.snapshot).toBeNull()
  })
})
