import { MAX_AUTO_OLDER_PAGES } from '../chart/seriesSync'
import type { Candle, Timeframe } from '../types'
import { formatUtc } from './quote'

/** Copy for the "no trades in this period" state (loaded bars exist, none had a trade). */
export interface NoTradesCopy {
  title: string
  message: string
  /** Label of the older-history button. */
  action: string
  /** The button cannot do anything right now; `note` says why (null when it is enabled). */
  actionDisabled: boolean
  /** Why the button is disabled, under it; null when enabled or the label says it all. */
  note: string | null
}

/** Why older history cannot be requested right now. */
export interface OlderHistoryState {
  /** An older page is running. */
  loadingOlder: boolean
  /** The exchange has nothing older. */
  historyExhausted: boolean
  /** The loaded raw bars reached the chart's bar limit. */
  atLimit: boolean
  /** The exchange asked us to wait (429/418 Retry-After window). */
  retryBlocked: boolean
  /** The last older page failed; a short cooldown runs before the next try. */
  olderCooldown: boolean
  /** The connection is offline. */
  offline: boolean
}

/** True when a loadOlder call would do something. */
export function canLoadOlder(s: OlderHistoryState): boolean {
  return (
    !s.loadingOlder &&
    !s.historyExhausted &&
    !s.atLimit &&
    !s.retryBlocked &&
    !s.olderCooldown &&
    !s.offline
  )
}

/**
 * The empty view requests older pages by itself while it is shown: one request per call while
 * `pagesSoFar` (requests since the view appeared or the user last clicked) is under
 * MAX_AUTO_OLDER_PAGES and a request can run (`canLoadOlder`). The caller calls this when the
 * history start or the availability changes, so a finished page or an ended cooldown leads to
 * the next one.
 */
export function shouldAutoLoadOlder(pagesSoFar: number, canLoad: boolean): boolean {
  return canLoad && pagesSoFar < MAX_AUTO_OLDER_PAGES
}

/**
 * Text for the chart area when every loaded bar of `symbol`/`interval` had no trade (so none
 * is plotted). `raw` are the loaded bars, traded or not; their span is quoted so the user sees
 * which period was empty.
 */
export function noTradesCopy(
  opts: {
    symbol: string
    interval: Timeframe
    exchange: string
    raw: readonly Candle[]
    /** The chart's raw-bar limit, quoted when it is reached. */
    maxBars: number
  } & OlderHistoryState,
): NoTradesCopy {
  const { symbol, interval, exchange, raw, maxBars } = opts
  const span =
    raw.length > 0
      ? ` between ${formatUtc(raw[0].time)} and ${formatUtc(raw[raw.length - 1].time)} UTC`
      : ''
  const bars = raw.length === 1 ? '1 bar' : `${raw.length} bars`
  const tail = opts.historyExhausted
    ? ' There is no older history.'
    : ' Load older history to find the last trades.'
  const base = {
    title: `No trades in this period on ${exchange}`,
    message: `${symbol} had no trades in the last ${bars} on ${interval}${span}. Bars without trades are not drawn.${tail}`,
  }
  const disabled = (action: string, note: string | null): NoTradesCopy => ({
    ...base,
    action,
    actionDisabled: true,
    note,
  })
  if (opts.loadingOlder) return disabled('Loading older bars…', null)
  if (opts.historyExhausted) return disabled('No older history', null)
  if (opts.atLimit) {
    return disabled(
      'History limit reached',
      `The chart holds at most ${maxBars.toLocaleString('en-US')} bars. Try a longer interval.`,
    )
  }
  if (opts.offline) return disabled('Load older history', 'Offline. Check your connection.')
  if (opts.retryBlocked) {
    return disabled('Load older history', `${exchange} asked us to wait before the next request.`)
  }
  if (opts.olderCooldown) {
    return disabled('Load older history', 'The last request failed. Retrying in a few seconds.')
  }
  return { ...base, action: 'Load older history', actionDisabled: false, note: null }
}
