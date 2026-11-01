// One best-effort localStorage value (Phase 19, Wave 5): the try/catch load/save pair every
// persisted pref used to repeat. A disabled, full or throwing storage never breaks the app: a
// read falls back, a write is dropped.

import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'

/** Minimal storage surface, so tests can pass a fake (or one that throws). */
export type ValueStorage = Pick<Storage, 'getItem' | 'setItem'>

/** `localStorage`, or null when there is none or touching it throws (storage disabled). */
export function browserStorage(): ValueStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // Accessing localStorage itself throws when storage is disabled.
    return null
  }
}

export interface StoredValueOptions<T> {
  /** Validates the decoded value. Runs inside the try: a throw falls back too. */
  parse: (raw: unknown) => T
  /** A fresh fallback per call: no storage, never saved, unreadable or invalid. */
  fallback: () => T
  /** Text written for a value; JSON.stringify by default. Runs inside the try. */
  serialize?: (value: T) => string
  /** Text to raw value; JSON.parse by default (`(text) => text` for a plain string). */
  decode?: (text: string) => unknown
}

export interface StoredValue<T> {
  readonly key: string
  /** The saved value, or `fallback()`. Never throws. `storage` defaults to localStorage. */
  load(storage?: ValueStorage | null): T
  /** Best effort: a full or disabled storage is ignored. `storage` defaults to localStorage. */
  save(value: T, storage?: ValueStorage | null): void
}

const jsonStringify = (value: unknown): string => JSON.stringify(value)
const jsonParse = (text: string): unknown => JSON.parse(text)

export function storedValue<T>(key: string, opts: StoredValueOptions<T>): StoredValue<T> {
  const { parse, fallback } = opts
  const serialize = opts.serialize ?? jsonStringify
  const decode = opts.decode ?? jsonParse
  return {
    key,
    load(storage: ValueStorage | null = browserStorage()): T {
      if (!storage) return fallback()
      try {
        const text = storage.getItem(key)
        if (text === null) return fallback()
        return parse(decode(text))
      } catch {
        return fallback()
      }
    },
    save(value: T, storage: ValueStorage | null = browserStorage()): void {
      if (!storage) return
      try {
        storage.setItem(key, serialize(value))
      } catch {
        // Quota exceeded or storage disabled: keep it in memory only.
      }
    },
  }
}

/**
 * useState backed by a stored value: starts from `stored.load()` and saves after every commit
 * that changed the value (and once after mount, like the effects it replaces).
 */
export function useStoredState<T>(
  stored: Pick<StoredValue<T>, 'load' | 'save'>,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => stored.load())
  useEffect(() => {
    stored.save(value)
  }, [stored, value])
  return [value, setValue]
}
