import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

// Phase 19 wave 5 (W5-C2): `useMenu` holds what TimeframePicker, ChartTypeMenu and LayoutMenu
// each implemented. The roving maths is checked against each menu's old code (copied below), and
// the hook is driven through a stand-in hook runtime (no DOM in this suite).
vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...(await import('./testHooks')),
}))

const { roveFocus, rovingIndex, rovingMove, stepIndex, useMenu } = await import('./useMenu')
const { hooks, render, resetHooks, unmount } = await import('./testHooks')

// ---------- old roving code, verbatim apart from parameters ----------

/** TimeframePicker's `step` (skips unavailable entries). */
function oldStep(flat: readonly boolean[], from: number, dir: 1 | -1): number {
  const n = flat.length
  for (let k = 1; k <= n; k++) {
    const i = (from + dir * k + n * k) % n
    if (flat[i]) return i
  }
  return from
}
function oldTimeframe(key: string, i: number, flat: readonly boolean[]): number | null {
  switch (key) {
    case 'ArrowDown':
      return oldStep(flat, i, 1)
    case 'ArrowUp':
      return oldStep(flat, i, -1)
    case 'Home':
      return oldStep(flat, flat.length - 1, 1)
    case 'End':
      return oldStep(flat, 0, -1)
  }
  return null
}

/** ChartTypeMenu's list keys. */
function oldChartType(key: string, i: number, n: number): number | null {
  switch (key) {
    case 'ArrowDown':
      return (i + 1) % n
    case 'ArrowUp':
      return (i - 1 + n) % n
    case 'Home':
      return 0
    case 'End':
      return n - 1
  }
  return null
}

/** LayoutMenu's `move` target (an index into `items`; may be out of range = no focus). */
function oldLayout(key: string, at: number, len: number): number | null {
  const move = (i: number) => (i + len) % len
  switch (key) {
    case 'ArrowDown':
    case 'ArrowRight':
      return move(at + 1)
    case 'ArrowUp':
    case 'ArrowLeft':
      return move(at - 1)
    case 'Home':
      return move(0)
    case 'End':
      return move(len - 1)
  }
  return null
}

const KEYS = ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', 'a', 'Tab']

describe('roving maths matches each menu it replaced', () => {
  it('TimeframePicker: every key, start and availability pattern (up to 6 entries)', () => {
    for (let n = 1; n <= 6; n++)
      for (let mask = 0; mask < 1 << n; mask++) {
        const flat = Array.from({ length: n }, (_, i) => ((mask >> i) & 1) === 1)
        for (let i = 0; i < n; i++)
          for (const key of KEYS) {
            const move = rovingMove(key)
            const want = oldTimeframe(key, i, flat)
            const got = move === null ? null : rovingIndex(move, i, n, (j) => flat[j])
            expect({ n, mask, i, key, got }).toEqual({ n, mask, i, key, got: want })
          }
      }
  })

  it('ChartTypeMenu: every key and start (5 types, and other lengths)', () => {
    for (let n = 1; n <= 7; n++)
      for (let i = 0; i < n; i++)
        for (const key of KEYS) {
          const move = rovingMove(key)
          const got = move === null ? null : rovingIndex(move, i, n)
          expect({ n, i, key, got }).toEqual({ n, i, key, got: oldChartType(key, i, n) })
        }
  })

  it('LayoutMenu: every key, focused item or none, Left/Right included', () => {
    for (let len = 0; len <= 6; len++)
      for (let at = -1; at < len; at++)
        for (const key of KEYS) {
          const items = Array.from({ length: len }, (_, i) => ({ i, focus: vi.fn() }))
          const e = { key, preventDefault: vi.fn() }
          const handled = roveFocus(
            e,
            items as unknown as HTMLElement[],
            (items[at] ?? null) as unknown as Element | null,
            true,
          )
          const want = oldLayout(key, at, len)
          expect(handled).toBe(want !== null)
          expect(e.preventDefault).toHaveBeenCalledTimes(want === null ? 0 : 1)
          const focused = items.filter((it) => it.focus.mock.calls.length > 0).map((it) => it.i)
          // Old code: items[target]?.focus() — nothing when the target is out of range (no items).
          const wantFocused = want !== null && items[want] !== undefined ? [items[want].i] : []
          expect({ len, at, key, focused }).toEqual({ len, at, key, focused: wantFocused })
        }
  })

  it('stepIndex returns `from` when nothing is allowed', () => {
    expect(stepIndex(2, 1, 4, () => false)).toBe(2)
    expect(stepIndex(0, -1, 0)).toBe(0)
  })

  it('Left/Right only move when horizontal', () => {
    expect(rovingMove('ArrowRight')).toBeNull()
    expect(rovingMove('ArrowLeft')).toBeNull()
    expect(rovingMove('ArrowRight', true)).toBe('next')
    expect(rovingMove('ArrowLeft', true)).toBe('prev')
  })
})

// ---------- the hook ----------

interface Key {
  key: string
  shiftKey: boolean
  defaultPrevented: boolean
  preventDefault: Mock<() => void>
  stopPropagation: Mock<() => void>
}
const key = (k: string): Key => ({
  key: k,
  shiftKey: false,
  defaultPrevented: false,
  preventDefault: vi.fn<() => void>(),
  stopPropagation: vi.fn<() => void>(),
})

describe('useMenu', () => {
  const pointer: ((e: { target: unknown }) => void)[] = []
  const inside = { name: 'inside' }

  beforeEach(() => {
    vi.stubGlobal('document', {
      addEventListener: (type: string, fn: (e: { target: unknown }) => void) => {
        if (type === 'pointerdown') pointer.push(fn)
      },
      removeEventListener: (type: string, fn: (e: { target: unknown }) => void) => {
        const i = pointer.indexOf(fn)
        if (type === 'pointerdown' && i >= 0) pointer.splice(i, 1)
      },
    })
  })
  afterEach(() => {
    unmount()
    resetHooks()
    pointer.length = 0
    vi.unstubAllGlobals()
  })

  /** Renders the hook with fake root / trigger / menu elements attached to its refs. */
  function mount(options: Parameters<typeof useMenu>[0] = {}) {
    const trigger = { focus: vi.fn() }
    const menuEl = { focus: vi.fn() }
    const root = { contains: (n: unknown) => n === inside || n === menuEl || n === trigger }
    const draw = () => {
      const m = render(() => useMenu<HTMLElement>(options))
      m.rootRef.current = root as unknown as HTMLDivElement
      m.buttonRef.current = trigger as unknown as HTMLButtonElement
      m.menuRef.current = menuEl as unknown as HTMLElement
      return m
    }
    return { draw, trigger, menuEl }
  }

  it('starts closed with no outside-click listener', () => {
    const { draw } = mount()
    const m = draw()
    expect(m.open).toBe(false)
    expect(pointer).toEqual([])
  })

  it('opening sets the roving index from openAt, focuses the menu and listens for outside clicks', () => {
    const { draw, menuEl } = mount({ openAt: () => 3 })
    draw().openMenu()
    const m = draw()
    expect(m.open).toBe(true)
    expect(m.active).toBe(3)
    expect(menuEl.focus).toHaveBeenCalledTimes(1)
    expect(pointer).toHaveLength(1)
    // A re-render while open does not refocus (the effect keys on `open` and a stable focus fn).
    draw()
    expect(menuEl.focus).toHaveBeenCalledTimes(1)
  })

  it('focusOnOpen replaces the default focus', () => {
    const focusOnOpen = vi.fn()
    const { draw, menuEl } = mount({ focusOnOpen })
    draw().openMenu()
    draw()
    expect(focusOnOpen).toHaveBeenCalledWith(menuEl)
    expect(menuEl.focus).not.toHaveBeenCalled()
  })

  it('a pointerdown outside the root closes without refocusing; inside does nothing', () => {
    const { draw, trigger } = mount()
    draw().openMenu()
    draw()
    pointer[0]({ target: inside })
    expect(draw().open).toBe(true)
    pointer[0]({ target: { name: 'chart' } })
    const m = draw()
    expect(m.open).toBe(false)
    expect(trigger.focus).not.toHaveBeenCalled()
    expect(pointer).toEqual([])
  })

  it('Escape closes, refocuses the trigger and stops there', () => {
    const { draw, trigger } = mount()
    draw().openMenu()
    const e = key('Escape')
    expect(draw().onMenuKey(e)).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
    expect(e.stopPropagation).toHaveBeenCalledTimes(1)
    expect(trigger.focus).toHaveBeenCalledTimes(1)
    expect(draw().open).toBe(false)
  })

  it('Tab closes without refocus, preventDefault or stopPropagation; not with tabCloses: false', () => {
    const { draw, trigger } = mount()
    draw().openMenu()
    const tab = key('Tab')
    expect(draw().onMenuKey(tab)).toBe(true)
    expect(draw().open).toBe(false)
    expect(tab.preventDefault).not.toHaveBeenCalled()
    expect(tab.stopPropagation).not.toHaveBeenCalled()
    expect(trigger.focus).not.toHaveBeenCalled()

    unmount()
    resetHooks()
    const second = mount({ tabCloses: false })
    second.draw().openMenu()
    expect(second.draw().onMenuKey(key('Tab'))).toBe(false)
    expect(second.draw().open).toBe(true)
  })

  it('other keys are left alone', () => {
    const { draw } = mount()
    draw().openMenu()
    for (const k of ['Enter', ' ', 'ArrowDown', 'a']) {
      const e = key(k)
      expect(draw().onMenuKey(e)).toBe(false)
      expect(e.preventDefault).not.toHaveBeenCalled()
      expect(e.stopPropagation).not.toHaveBeenCalled()
    }
    expect(draw().open).toBe(true)
  })

  it('toggle opens, and closes leaving focus where it is', () => {
    const { draw, trigger } = mount()
    draw().toggle()
    expect(draw().open).toBe(true)
    draw().toggle()
    expect(draw().open).toBe(false)
    expect(trigger.focus).not.toHaveBeenCalled()
  })

  it('rove moves the index with preventDefault, skipping disallowed items, and wraps', () => {
    const { draw } = mount({ openAt: () => 0 })
    draw().openMenu()
    const allowed = (i: number) => i !== 1
    const down = key('ArrowDown')
    expect(draw().rove(down, 4, allowed)).toBe(true)
    expect(down.preventDefault).toHaveBeenCalledTimes(1)
    expect(draw().active).toBe(2)
    draw().rove(key('End'), 4, allowed)
    expect(draw().active).toBe(3)
    draw().rove(key('ArrowDown'), 4, allowed)
    expect(draw().active).toBe(0)
    const enter = key('Enter')
    expect(draw().rove(enter, 4, allowed)).toBe(false)
    expect(enter.preventDefault).not.toHaveBeenCalled()
    expect(hooks.updates.length).toBeGreaterThan(0)
  })

  it('unmounting while open removes the outside-click listener', () => {
    const { draw } = mount()
    draw().openMenu()
    draw()
    expect(pointer).toHaveLength(1)
    unmount()
    expect(pointer).toEqual([])
  })
})
