import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from 'react'
import { elementKeyRules, handleModalKey, type ModalKeyEvent } from './useModalKeys'

/** Where a roving key moves: one step either way, or to the first / last item. */
export type RovingMove = 'next' | 'prev' | 'first' | 'last'

/** The parts of a keydown a roving handler reads or calls. */
export interface RovingKeyEvent {
  key: string
  preventDefault(): void
}

/** The move a key asks for: Up/Down, Home/End, plus Left/Right when `horizontal`. Else null. */
export function rovingMove(key: string, horizontal = false): RovingMove | null {
  switch (key) {
    case 'ArrowDown':
      return 'next'
    case 'ArrowUp':
      return 'prev'
    case 'ArrowRight':
      return horizontal ? 'next' : null
    case 'ArrowLeft':
      return horizontal ? 'prev' : null
    case 'Home':
      return 'first'
    case 'End':
      return 'last'
    default:
      return null
  }
}

/** Next index after `from` in direction `dir` (of `n`) that is `allowed`, wrapping; `from` if none. */
export function stepIndex(
  from: number,
  dir: 1 | -1,
  n: number,
  allowed?: (i: number) => boolean,
): number {
  for (let k = 1; k <= n; k++) {
    const i = (from + dir * k + n * k) % n
    if (allowed === undefined || allowed(i)) return i
  }
  return from
}

/** The index `move` lands on from `from` in a list of `n`, skipping items that are not allowed. */
export function rovingIndex(
  move: RovingMove,
  from: number,
  n: number,
  allowed?: (i: number) => boolean,
): number {
  switch (move) {
    case 'next':
      return stepIndex(from, 1, n, allowed)
    case 'prev':
      return stepIndex(from, -1, n, allowed)
    case 'first':
      return stepIndex(n - 1, 1, n, allowed)
    case 'last':
      return stepIndex(0, -1, n, allowed)
  }
}

/**
 * Roving DOM focus over `items` (a menu of buttons): a roving key moves focus from `focused` (or
 * from before the first item when focus is elsewhere), wrapping. Returns true when it was one.
 */
export function roveFocus(
  e: RovingKeyEvent,
  items: readonly HTMLElement[],
  focused: Element | null,
  horizontal = false,
): boolean {
  const move = rovingMove(e.key, horizontal)
  if (move === null) return false
  e.preventDefault()
  const at = items.indexOf(focused as HTMLElement)
  items[rovingIndex(move, at, items.length)]?.focus()
  return true
}

export interface MenuOptions<M extends HTMLElement> {
  /** The roving index each open starts from (activedescendant lists). Omit to leave `active`. */
  openAt?: () => number
  /** Moves focus into the menu once it is open. Keep it stable (module scope). Default: the menu. */
  focusOnOpen?: (menu: M) => void
  /** Tab closes the menu and lets focus move on (default true). */
  tabCloses?: boolean
}

export interface Menu<M extends HTMLElement> {
  open: boolean
  /** Roving index for an `aria-activedescendant` list. */
  active: number
  setActive: Dispatch<SetStateAction<number>>
  /** Wraps the trigger and the menu: a pointerdown outside it closes the menu. */
  rootRef: RefObject<HTMLDivElement | null>
  /** The trigger: Escape and picks return focus here. */
  buttonRef: RefObject<HTMLButtonElement | null>
  /** The element `focusOnOpen` receives. */
  menuRef: RefObject<M | null>
  /** Opens at `openAt()`. */
  openMenu(): void
  /** Closes; `refocus` moves focus back to the trigger. */
  close(refocus: boolean): void
  /** The trigger's click: closes (focus stays on the trigger) or opens. */
  toggle(): void
  /**
   * Escape closes, returns focus to the trigger and stops there (preventDefault, no further
   * propagation); with `tabCloses`, Tab closes without either. Returns true when it closed.
   */
  onMenuKey(e: ModalKeyEvent): boolean
  /** Moves `active` by a roving key over `n` items, skipping those not `allowed`. True if moved. */
  rove(e: RovingKeyEvent, n: number, allowed?: (i: number) => boolean): boolean
}

const ESCAPE_ONLY = elementKeyRules()

const focusMenu = (menu: HTMLElement): void => menu.focus()

/**
 * A trigger button and its dropdown (TimeframePicker, ChartTypeMenu, LayoutMenu): open state,
 * the refs, a roving index, focus moved in on open, a document pointerdown outside `rootRef`
 * closing it, and Escape closing it with focus back on the trigger.
 */
export function useMenu<M extends HTMLElement>(options: MenuOptions<M> = {}): Menu<M> {
  const { openAt, focusOnOpen = focusMenu, tabCloses = true } = options
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<M>(null)

  useEffect(() => {
    if (!open) return
    const menu = menuRef.current
    if (menu) focusOnOpen(menu)
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open, focusOnOpen])

  const openMenu = () => {
    if (openAt !== undefined) setActive(openAt())
    setOpen(true)
  }

  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) buttonRef.current?.focus()
  }

  return {
    open,
    active,
    setActive,
    rootRef,
    buttonRef,
    menuRef,
    openMenu,
    close,
    toggle: () => (open ? close(false) : openMenu()),
    onMenuKey: (e) => {
      if (handleModalKey(e, null, () => close(true), ESCAPE_ONLY)) return true
      if (!tabCloses || e.key !== 'Tab') return false
      close(false)
      return true
    },
    rove: (e, n, allowed) => {
      const move = rovingMove(e.key)
      if (move === null) return false
      e.preventDefault()
      setActive((i) => rovingIndex(move, i, n, allowed))
      return true
    },
  }
}
