import { useEffect, useRef, useState } from 'react'
import {
  PAPER_MONITOR_NO_SERVER_MESSAGE,
  paperMonitorErrorMessage,
  type PaperMonitorClient,
  type PaperMonitorResponse,
} from '../data/paperMonitor'

/** About once a minute: dry-run tests trade on 4h bars and each request reads every DB. */
export const PAPER_MONITOR_POLL_MS = 60_000

export interface PaperMonitorState {
  /** The latest good answer; it stays when a later refresh fails. */
  data: PaperMonitorResponse | null
  /** Why the latest request failed (or why none can be made), else null. */
  error: string | null
  /** The first answer is on its way. */
  loading: boolean
}

interface Result {
  data: PaperMonitorResponse | null
  error: string | null
}

const NOTHING_YET: Result = { data: null, error: null }

/**
 * How long to wait before asking: at once the first time, else whatever is left of `pollMs` since
 * the last answer, so flipping back and forth to the section costs no extra requests.
 */
export function paperMonitorDelay(lastAnswer: number | null, now: number, pollMs: number): number {
  return lastAnswer === null ? 0 : Math.max(0, lastAnswer + pollMs - now)
}

/** The browser tab is in the background (always false without a document). */
function pageHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/**
 * Polls the read-only paper monitor while `active` (the section is on screen and the browser tab
 * is in front). The next request is armed only after the previous one settles, so requests never
 * overlap; turning `active` off or unmounting aborts the one in flight. Coming back to the tab
 * asks at once if the last answer is a poll old, instead of waiting out a background timer.
 * `client` null (no local server, e.g. direct Binance) makes no request at all, so that mode logs
 * no network errors.
 */
export function usePaperMonitor(
  client: PaperMonitorClient | null,
  active: boolean,
  pollMs: number = PAPER_MONITOR_POLL_MS,
): PaperMonitorState {
  const [result, setResult] = useState<Result>(NOTHING_YET)
  const lastAnswerRef = useRef<number | null>(null)

  useEffect(() => {
    if (client === null || !active) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let inFlight = false

    const settled = (next: (prev: Result) => Result): void => {
      inFlight = false
      lastAnswerRef.current = Date.now()
      setResult(next)
      timer = setTimeout(poll, pollMs)
    }

    function poll(): void {
      if (pageHidden()) {
        timer = setTimeout(poll, pollMs)
        return
      }
      inFlight = true
      client?.fetchMonitor(controller.signal).then(
        (data) => {
          if (!controller.signal.aborted) settled(() => ({ data, error: null }))
        },
        (err: unknown) => {
          if (controller.signal.aborted) return
          const error = paperMonitorErrorMessage(err)
          settled((prev) => ({ data: prev.data, error }))
        },
      )
    }

    const onVisibilityChange = (): void => {
      if (pageHidden() || inFlight) return
      if (paperMonitorDelay(lastAnswerRef.current, Date.now(), pollMs) > 0) return
      clearTimeout(timer)
      poll()
    }

    // Through a timer even at 0 ms: StrictMode's throwaway mount never reaches the network.
    timer = setTimeout(poll, paperMonitorDelay(lastAnswerRef.current, Date.now(), pollMs))
    const page = typeof document === 'undefined' ? null : document
    page?.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      page?.removeEventListener('visibilitychange', onVisibilityChange)
      clearTimeout(timer)
      controller.abort()
    }
  }, [client, active, pollMs])

  if (client === null) return { data: null, error: PAPER_MONITOR_NO_SERVER_MESSAGE, loading: false }
  const { data, error } = result
  return { data, error, loading: active && data === null && error === null }
}
