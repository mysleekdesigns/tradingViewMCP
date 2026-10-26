import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelOrder,
  createAccount,
  defaultPaperStorage,
  loadPaperAccount,
  onPrice,
  savePaperAccount,
  type PaperAccount,
  type PaperBar,
  type PaperConfig,
  type PaperMarket,
  type PaperOrder,
  type PaperOrderRequest,
  type PaperScope,
  type PaperStorage,
} from './paper'
import { feedReplayBar, placeReplayOrder, type ReplayBook } from './paperFeeds'

export interface PaperAccountApi {
  scope: PaperScope
  account: PaperAccount
  /**
   * Open time of the latest bar the account has seen (replay); null for the live account and
   * before a replay starts. Orders at an earlier market time are refused (see replayTradeBlock).
   */
  highWater: number | null
  /** Place an order at the given market moment; returns the order (filled, open or rejected). */
  place(req: PaperOrderRequest, market: PaperMarket): PaperOrder
  cancel(orderId: string, time: number): void
  /** Live tick: `time` is the wall-clock UNIX seconds the price was seen. */
  onPrice(symbol: string, price: number, time: number): void
  /** A closed bar (replay step); bars at or before the high-water mark are ignored. */
  onBar(symbol: string, bar: PaperBar): void
  /** Start over with a fresh account (new config optional); the high-water mark stays. */
  reset(cfg?: Partial<PaperConfig>): void
  /** A replay started (`highWater` = its start bar) or ended (null): fresh account, same config. */
  restart(highWater: number | null): void
}

/**
 * A paper-trading account. 'live' is loaded from and saved to localStorage; 'replay' lives in
 * memory only, so replay practice never touches the live account (call `restart()` when a
 * replay session starts or ends). `scope` is fixed for the life of the hook instance.
 *
 * Callbacks are stable and read the latest account from a ref, so feeding ticks never
 * re-renders unless an order actually fills.
 */
export function usePaperAccount(
  scope: PaperScope,
  storage: PaperStorage | null = defaultPaperStorage(),
): PaperAccountApi {
  const [book, setBook] = useState<ReplayBook>(() => ({
    account: scope === 'live' ? loadPaperAccount(storage) : createAccount(),
    highWater: null,
  }))
  const ref = useRef(book)

  const commit = useCallback((next: ReplayBook) => {
    if (next === ref.current) return
    ref.current = next
    setBook(next)
  }, [])
  const commitAccount = useCallback(
    (account: PaperAccount) => {
      if (account !== ref.current.account) commit({ ...ref.current, account })
    },
    [commit],
  )

  const { account, highWater } = book
  useEffect(() => {
    if (scope === 'live') savePaperAccount(account, storage)
  }, [account, scope, storage])

  const place = useCallback(
    (req: PaperOrderRequest, market: PaperMarket) => {
      const result = placeReplayOrder(ref.current, req, market)
      commit(result.book)
      return result.order
    },
    [commit],
  )
  const cancel = useCallback(
    (orderId: string, time: number) =>
      commitAccount(cancelOrder(ref.current.account, orderId, time)),
    [commitAccount],
  )
  const tick = useCallback(
    (symbol: string, price: number, time: number) =>
      commitAccount(onPrice(ref.current.account, symbol, price, time)),
    [commitAccount],
  )
  const bar = useCallback(
    (symbol: string, b: PaperBar) => commit(feedReplayBar(ref.current, symbol, b)),
    [commit],
  )
  const reset = useCallback(
    (cfg?: Partial<PaperConfig>) =>
      commit({ ...ref.current, account: createAccount(cfg ?? ref.current.account.config) }),
    [commit],
  )
  const restart = useCallback(
    (hw: number | null) =>
      commit({ account: createAccount(ref.current.account.config), highWater: hw }),
    [commit],
  )

  return useMemo(
    () => ({
      scope,
      account,
      highWater,
      place,
      cancel,
      onPrice: tick,
      onBar: bar,
      reset,
      restart,
    }),
    [scope, account, highWater, place, cancel, tick, bar, reset, restart],
  )
}
