import { useCallback, useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE =
  'button:not([disabled]):not([tabindex="-1"]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'

/** The parts of a keydown read here: a DOM `KeyboardEvent` or React's synthetic one. */
export interface ModalKeyEvent {
  key: string
  shiftKey: boolean
  defaultPrevented: boolean
  isComposing?: boolean
  preventDefault(): void
  stopPropagation(): void
}

/** How one keydown is handled. Every caller's differences are spelled out here. */
export interface ModalKeyRules {
  /** 'all': every keydown stops propagating here. 'handled': only a key that closes. */
  stop: 'all' | 'handled'
  /** Leave keys already handled (`defaultPrevented`) or mid-IME-composition alone. */
  skipHandled: boolean
  /** Escape (and Tab with `tabCloses`) also calls `preventDefault`. */
  preventDefault: boolean
  /** Tab closes, like Escape. */
  tabCloses: boolean
  /** Keep Tab inside the root: focusables matching this selector wrap around. Null: no trap. */
  trap: string | null
  /**
   * Document-level trap: when focus is outside the root Tab moves it in, and with nothing
   * focusable Tab focuses the root. Off for element handlers (the key already came from inside).
   */
  strictTrap: boolean
}

/** Options for an element handler (`listen: 'element'`); defaults match the common case. */
export interface ElementKeysOptions {
  /** No document listener: attach the returned handler as the element's React `onKeyDown`. */
  listen: 'element'
  /** Default 'handled': only Escape (or a closing Tab) stops propagating. */
  stop?: 'all' | 'handled'
  /** Default true. */
  preventDefault?: boolean
  /** Default false. */
  tabCloses?: boolean
  /** Focus-trap selector; default none. */
  trap?: string
}

const DOCUMENT_RULES: ModalKeyRules = {
  stop: 'all',
  skipHandled: true,
  preventDefault: true,
  tabCloses: false,
  trap: FOCUSABLE,
  strictTrap: true,
}

/** The rules an element handler with these options uses. */
export function elementKeyRules(o: Omit<ElementKeysOptions, 'listen'> = {}): ModalKeyRules {
  return {
    stop: o.stop ?? 'handled',
    skipHandled: false,
    preventDefault: o.preventDefault ?? true,
    tabCloses: o.tabCloses ?? false,
    trap: o.trap ?? null,
    strictTrap: false,
  }
}

function trapTab(e: ModalKeyEvent, root: HTMLElement, selector: string, strict: boolean): void {
  const focusables = Array.from(root.querySelectorAll<HTMLElement>(selector))
  const active = document.activeElement
  if (focusables.length === 0) {
    if (strict) {
      e.preventDefault()
      root.focus()
    }
    return
  }
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (strict && !root.contains(active)) {
    e.preventDefault()
    ;(e.shiftKey ? last : first).focus()
  } else if (e.shiftKey && active === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

/**
 * Handles one keydown by `rules`: Escape (and Tab with `tabCloses`) calls `onClose`; Tab wraps
 * focus inside `root` when there is a trap. Returns true when the key closed.
 */
export function handleModalKey(
  e: ModalKeyEvent,
  root: HTMLElement | null,
  onClose: () => void,
  rules: ModalKeyRules,
): boolean {
  if (rules.stop === 'all') e.stopPropagation()
  if (rules.skipHandled && (e.defaultPrevented || e.isComposing === true)) return false
  if (e.key === 'Escape' || (rules.tabCloses && e.key === 'Tab')) {
    if (rules.preventDefault) e.preventDefault()
    if (rules.stop === 'handled') e.stopPropagation()
    onClose()
    return true
  }
  if (e.key === 'Tab' && rules.trap !== null && root) trapTab(e, root, rules.trap, rules.strictTrap)
  return false
}

/**
 * Keyboard ownership for a modal dialog, while it is mounted.
 *
 * A bubble-phase `document` keydown listener runs after the dialog's own React handlers (React
 * listens on #root, inside document) and before `window` listeners. It stops every keydown from
 * reaching window listeners behind the modal (chart Delete/Escape, app shortcuts). Unless the
 * dialog already handled the key (`defaultPrevented`), it also handles Escape (calls `onClose`)
 * and Tab (keeps focus inside `ref`). Because it listens on the document, it still works after
 * the focused node unmounts and focus falls back to <body>: Escape closes, and Tab moves focus
 * back into the dialog.
 *
 * With `{ listen: 'element' }` nothing is added to the document: the hook returns a keydown
 * handler for the element (dialog, popover, inline confirm) to use as its React `onKeyDown`.
 * Escape calls `onClose` and stops there (only that key, or every key with `stop: 'all'`);
 * `trap` keeps Tab inside `ref`; `tabCloses` closes on Tab too. The handler returns true when the
 * key closed, so a caller with its own keys (arrows) can stop there. It uses this render's
 * `onClose`, and keeps its identity while `ref`, `onClose` and the options do.
 */
export function useModalKeys(ref: RefObject<HTMLElement | null>, onClose: () => void): void
export function useModalKeys(
  ref: RefObject<HTMLElement | null> | null,
  onClose: () => void,
  options: ElementKeysOptions,
): (e: ModalKeyEvent) => boolean
export function useModalKeys(
  ref: RefObject<HTMLElement | null> | null,
  onClose: () => void,
  options?: ElementKeysOptions,
): ((e: ModalKeyEvent) => boolean) | undefined {
  const element = options !== undefined
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (element) return
    const onKeyDown = (e: KeyboardEvent) => {
      const root = ref?.current
      if (!root) return
      handleModalKey(e, root, onCloseRef.current, DOCUMENT_RULES)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [ref, element])

  const stop = options?.stop
  const preventDefault = options?.preventDefault
  const tabCloses = options?.tabCloses
  const trap = options?.trap
  const onElementKeyDown = useCallback(
    (e: ModalKeyEvent) =>
      handleModalKey(
        e,
        ref?.current ?? null,
        onClose,
        elementKeyRules({ stop, preventDefault, tabCloses, trap }),
      ),
    [ref, onClose, stop, preventDefault, tabCloses, trap],
  )
  return element ? onElementKeyDown : undefined
}
