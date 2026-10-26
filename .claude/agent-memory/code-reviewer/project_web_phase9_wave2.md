---
name: web-phase9-wave2
description: Phase 9 wave 2 (multi-chart, seconds/custom timeframes, bar replay, paper trading, regime ribbon) - confirmed design, live-verified upstream/LWC facts, round-1..3 fixes verified 2026-10-26, remaining minor gaps
metadata:
  type: project
---

Confirmed design (verified 2026-10-26; do not re-flag):
- Each ChartPanel owns its own useLiveCandles; App stores reports via onPanelLive with sameLive() shallow compare (no render loop) and panelLive() market guard. Layout shrink unmounts panels -> controller.stop closes sockets, onLive(id,null) drops the entry.
- ChartPanel is memo(); every App prop is stable between other panels' ticks (module-level providers/clients, useCallback/useMemo elements: historyChip, replayBarEl rebuilt from useReplay parts, styleBarEl; drawingsOf returns memoised stores). App itself still re-renders per tick (Header, sidebar) - accepted.
- createTimeframeProvider: native passthrough; seconds via Binance 1s. fetchAggregated pages spaced BASE_PAGE*base apart, trims partial oldest bucket. Out-of-order late closed bucket -> applyLiveCandle replaces in place (safe).
- Seed retries: SEED_RETRY_DELAYS_MS (5 tries), shared per-provider seedBlock for 429/418, `banned` until reconnect, superseded bucket one try then abandon -> onRepair -> controller gap fill. No onRepair -> gap fill loop (gap fill never re-subscribes or seeds).
- crosshairSync: publishes only with sourceEvent; no sourceEvent + no point = leave. Stale-leave guard by source id.
- Paper: quoteMismatchReason (SymbolInfo.quoteAsset, else parsed), marketable limits judged at placement and blocked like market orders when feed not live (marketBlocked), replay high-water book, live ticks stamped with wall-clock first-seen time (priceStamp), only 'live' panels feed quotes/marks.
- Regime: off on seconds TFs (regimeSupported), requests spaced >= one bar capped at 60 s (regimeRequestDelay); replay sends raw bars up to cursor (regimeSourceBars).
- venuePending: seconds panels in server mode wait (never-resolving abortable source) until the venue is known.

Live-verified facts:
- Binance `kline_1s` stream pushes ONE message per second, already `x: true`.
- LWC 5.2.1 (read dist source): setCrosshairPosition/clearCrosshairPosition skip the event; mouse leave fires crosshairMove with point and sourceEvent undefined; every series update/setData calls updateCrosshair, which re-fires (point set, no sourceEvent) ONLY while a crosshair position is set (clear sets pane null -> no re-fire). Re-fire reuses the saved x pixel, so after a new bar shifts the range the synthetic crosshair lands one bar later.

Round 3 (2026-10-26) verified, do not re-flag:
- Forming bucket: fast retries then seedSlowRetryMs = min(tfSec/2, 300 s) while forming; retryTimer only set when null (no overwrite leak); cleared by abandon/onDrop/dispose.
- Shared RequestGate per createTimeframeProvider (time-based, no timer, never shortened); gated rejects at once with retryAfter -> controller retryBlocked; seeds wait the hold without spending attempts. Two gates exist in server mode (TIMEFRAMES vs SERVER_SECONDS_TIMEFRAMES) by design.
- liveCandles repairPending: set only with inflight; cleared by any fetch(); a stale true after a given-up gap fill is inert.
- useReplay stores reconcile() result during render (converges in one pass; exit is idempotent on 'off'). paperFeeds treats picking->active at the same cursor as a resume (no reset).
- useRegime: candlesRef effect is declared before the request effect; throttle keyed on last SENT newest bar, so prepends during a pending throttle stay throttled.
- LWC: series.update/setData fires crosshairMove SYNCHRONOUSLY; Chart.tsx sets appliedRef after the updates, so CrosshairSync.refresh reads the previous bars.

Open after round 3 (minor):
- `banned` flag in subscribeAggregated is now redundant with the gate: a seed 418 still freezes the forming bucket until supersede/reconnect.
- refresh during a data update uses stale appliedRef (time in a just-appended bar clears the follower); source chart's own re-fire never republishes.
- Re-picking the cursor bar itself is treated as resume (no replay paper reset).

**Why:** saves re-deriving lifecycle/closure semantics and re-measuring upstream behaviour.
**How to apply:** start Phase 9 follow-up reviews here; related [[web-conventions]], [[web-no-trade-bars]], [[market-venue]].
