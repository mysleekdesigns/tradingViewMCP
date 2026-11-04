// The polling lifecycle, driven through the hook harness and fake timers (no DOM).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PAPER_MONITOR_NO_SERVER_MESSAGE,
  PAPER_MONITOR_OFFLINE_MESSAGE,
  type PaperMonitorClient,
  type PaperMonitorResponse,
} from '../data/paperMonitor'
import { PersistenceError } from '../data/persistence'
import { fakeReactHooks, mountHook } from './hookHarness.testkit'
import { PAPER_MONITOR_POLL_MS, paperMonitorDelay, usePaperMonitor } from './usePaperMonitor'

vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...fakeReactHooks,
}))

const POLL = PAPER_MONITOR_POLL_MS
const answer = (generatedAt: number): PaperMonitorResponse => ({ generatedAt, tests: [] })

/** A client whose requests stay pending until the test settles them. */
function pendingClient() {
  const calls: {
    signal: AbortSignal | undefined
    resolve(r: PaperMonitorResponse): void
    reject(e: unknown): void
  }[] = []
  const client: PaperMonitorClient = {
    fetchMonitor: (signal) =>
      new Promise((resolve, reject) => {
        calls.push({ signal, resolve, reject })
      }),
  }
  return { client, calls }
}

interface Props {
  client: PaperMonitorClient | null
  active: boolean
}
const useMonitor = (p: Props) => usePaperMonitor(p.client, p.active)

/** A `document` with a visibility state the test flips, firing the real event name. */
function stubDocument(initial: DocumentVisibilityState) {
  const listeners = new Set<() => void>()
  const doc = {
    visibilityState: initial,
    addEventListener(type: string, listener: () => void) {
      if (type === 'visibilitychange') listeners.add(listener)
    },
    removeEventListener(type: string, listener: () => void) {
      if (type === 'visibilitychange') listeners.delete(listener)
    },
  }
  vi.stubGlobal('document', doc)
  const flip = (state: DocumentVisibilityState) => {
    doc.visibilityState = state
    for (const listener of [...listeners]) listener()
  }
  return { listeners, hide: () => flip('hidden'), show: () => flip('visible') }
}

/** Let a settled request's callbacks run. */
const settle = () => vi.advanceTimersByTimeAsync(0)

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('paperMonitorDelay', () => {
  it('asks at once the first time, then waits out the rest of the poll', () => {
    expect(paperMonitorDelay(null, 5000, POLL)).toBe(0)
    expect(paperMonitorDelay(5000, 15_000, POLL)).toBe(POLL - 10_000)
    expect(paperMonitorDelay(5000, 5000 + POLL + 1, POLL)).toBe(0)
  })
})

describe('usePaperMonitor', () => {
  it('makes no request at all without a local server, and says why', async () => {
    const hook = mountHook(useMonitor, { client: null, active: true })
    await vi.advanceTimersByTimeAsync(POLL * 3)
    expect(vi.getTimerCount()).toBe(0)
    expect(hook.result).toEqual({
      data: null,
      error: PAPER_MONITOR_NO_SERVER_MESSAGE,
      loading: false,
    })
  })

  it('stays quiet until the section is visible', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: false })
    await vi.advanceTimersByTimeAsync(POLL * 2)
    expect(calls).toHaveLength(0)
    expect(hook.result.loading).toBe(false)
    hook.rerender({ client, active: true })
    expect(hook.result.loading).toBe(true)
    await settle()
    expect(calls).toHaveLength(1)
  })

  it('polls about once a minute after each answer', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({ data: answer(1), error: null, loading: false })
    await vi.advanceTimersByTimeAsync(POLL - 1)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(2)
  })

  it('never overlaps: a slow request holds the next one back', async () => {
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true })
    await vi.advanceTimersByTimeAsync(POLL * 5)
    expect(calls).toHaveLength(1)
    calls[0].resolve(answer(1))
    await settle()
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(POLL)
    expect(calls).toHaveLength(2)
  })

  it('aborts the request in flight and stops polling on unmount', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    hook.unmount()
    expect(calls[0].signal?.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(POLL * 3)
    expect(calls).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stops when the section is hidden, and does not re-ask early on coming back', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    hook.flush()
    await vi.advanceTimersByTimeAsync(10_000)
    hook.rerender({ client, active: false })
    expect(vi.getTimerCount()).toBe(0)
    hook.rerender({ client, active: true })
    expect(hook.result.data).toEqual(answer(1))
    await vi.advanceTimersByTimeAsync(POLL - 10_001)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(2)
  })

  it('keeps the last good answer when a refresh fails, and clears the error after', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    hook.flush()
    await vi.advanceTimersByTimeAsync(POLL)
    calls[1].reject(new PersistenceError('network', 'Cannot reach the local server'))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({
      data: answer(1),
      error: PAPER_MONITOR_OFFLINE_MESSAGE,
      loading: false,
    })
    await vi.advanceTimersByTimeAsync(POLL)
    calls[2].resolve(answer(2))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({ data: answer(2), error: null, loading: false })
  })

  it('reports a stopped server on the first try and keeps trying gently', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].reject(new PersistenceError('network', 'Cannot reach the local server'))
    await settle()
    hook.flush()
    expect(hook.result).toEqual({
      data: null,
      error: PAPER_MONITOR_OFFLINE_MESSAGE,
      loading: false,
    })
    await vi.advanceTimersByTimeAsync(POLL)
    expect(calls).toHaveLength(2)
  })

  it('makes no request while the browser tab is in the background, then asks on return', async () => {
    const page = stubDocument('hidden')
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true })
    await vi.advanceTimersByTimeAsync(POLL * 3)
    expect(calls).toHaveLength(0)
    page.show()
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(POLL * 3)
    expect(calls).toHaveLength(1)
  })

  it('refreshes at once on returning to the tab once the answer is a poll old', async () => {
    const page = stubDocument('visible')
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    page.hide()
    await vi.advanceTimersByTimeAsync(POLL + 30_000)
    expect(calls).toHaveLength(1)
    page.show()
    expect(calls).toHaveLength(2)
    // The timer the background tick armed is gone: no second request behind this one.
    calls[1].resolve(answer(2))
    await settle()
    await vi.advanceTimersByTimeAsync(POLL - 1)
    expect(calls).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(3)
  })

  it('does not re-ask early on returning to the tab with a fresh answer', async () => {
    const page = stubDocument('visible')
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    page.hide()
    await vi.advanceTimersByTimeAsync(10_000)
    page.show()
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(POLL - 10_000)
    expect(calls).toHaveLength(2)
  })

  it('does not overlap a request still in flight on returning to the tab', async () => {
    const page = stubDocument('visible')
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true })
    await settle()
    calls[0].resolve(answer(1))
    await settle()
    await vi.advanceTimersByTimeAsync(POLL)
    expect(calls).toHaveLength(2)
    page.hide()
    await vi.advanceTimersByTimeAsync(POLL * 2)
    page.show()
    expect(calls).toHaveLength(2)
  })

  it('stops listening for the tab on unmount', async () => {
    const page = stubDocument('hidden')
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    expect(page.listeners.size).toBe(1)
    hook.unmount()
    expect(page.listeners.size).toBe(0)
    page.show()
    await vi.advanceTimersByTimeAsync(POLL)
    expect(calls).toHaveLength(0)
  })

  it('does not listen for the tab while the section is hidden', () => {
    const page = stubDocument('visible')
    const { client } = pendingClient()
    mountHook(useMonitor, { client, active: false })
    expect(page.listeners.size).toBe(0)
  })

  it('ignores an answer that lands after the abort', async () => {
    const { client, calls } = pendingClient()
    const hook = mountHook(useMonitor, { client, active: true })
    await settle()
    hook.rerender({ client, active: false })
    calls[0].resolve(answer(1))
    await settle()
    hook.flush()
    expect(hook.result.data).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("StrictMode's throwaway mount reaches the network once, not twice", async () => {
    const { client, calls } = pendingClient()
    mountHook(useMonitor, { client, active: true }, { strict: true })
    await settle()
    expect(calls).toHaveLength(1)
    expect(calls[0].signal?.aborted).toBe(false)
  })
})
