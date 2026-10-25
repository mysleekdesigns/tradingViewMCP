---
name: phase9-review-notes
description: Phase 9 wave 1 web review (Coinbase provider, settings/theme, shortcuts, screenshot, seasonals) - rounds 1-4 status, residual risks and confirmed conventions
metadata:
  type: project
---

Round 2 (2026-10-25): every round-1 item verified FIXED (stall gap, empty older page, failed
subscribe, modal key leak, e.repeat, colour coalescing, timezone caching). Do not re-flag them.

Round 3 (2026-10-25): round-2 "silent stall backfill" FIXED (retry once, then a
'reconnecting'->'live' status pulse if the socket is live). Field-identical applyLiveCandle is a
no-op (same array); liveCandles.trackRollover runs BEFORE applyLiveCandle, so a closed flag on an
identical bar is not lost.

Still open (low, carry into later waves):
- (Round 4, 2026-10-25: FIXED) Status-pulse gap fill vs noBarMissed: a gap whose backfill fails
  while the socket is down gets `awaitLive`, holds the queue head (forming bar hidden), and the
  socket's next 'live' retries once, then pulses and drops. Hold only lasts while the socket is
  not live (no data flows then), so it is not a deadlock; permanent-close 'offline' leaves it held
  harmlessly. On that 'live' the consumer also gap-fills itself (pre-gap bar last): double REST,
  accepted. Any future "pulse status to force a gap fill" trick must keep the pre-gap bar last.
- (Round 4: FIXED) 'rest' close items now always get a second read-back.
- Backfill rows still arrive one onCandle each: appended rows cost O(n) spread + plottableCandles
  each (O(k*n), k <= 1000). Identical rows are now free. Batch API would fix.
- useModalKeys: with two useModalKeys dialogs mounted, the FIRST registered (outer) handles Esc/Tab.
  Not reachable in wave 1 (openers are blocked while dialogOpen); re-check if a dialog can open
  another dialog (multi-chart, paper panel).
- Ticker stream uses staleIsFailure but subscribes no heartbeat channel; fine while hundreds of
  products trade, would flicker 'reconnecting' on a tiny product set.

Confirmed conventions (do not re-flag):
- `openReconnectingSocket` default (Binance/server) unchanged: idle stale = silent reconnect.
  Coinbase streams pass `staleIsFailure` + `liveOnFirstMessage`; `SocketControl.fail()` keeps the
  pre-socket failure count so rejected subscribes escalate the backoff.
- Coinbase candle stream: one reconcile loop (guarded by `reconciling`), single `delayTimer` +
  `timers` set both cleared in onDispose; every await re-checks `disposed`; startStream's emit
  drops statuses after dispose. Queue items are only replaced in place or pushed at the tail, so
  head identity holds across awaits (head is re-read via shift()).
- Legacy dialogs (AlertDialog, IndicatorSettings, IndicatorMenu, SymbolSearch) call React
  stopPropagation on their root, so the document-level useModalKeys listener never sees their keys.
- Chart keys go through `chartKey(e, keyboardBlocked)`; keyboardBlocked = App `dialogOpen` (incl.
  context menu and Research view).
- Older closed bars re-emitted by Coinbase are replaced in place by applyLiveCandle; Chart falls
  back to one setData ('set' plan), never series.update on an older time.
- Coinbase getCandles steps back past empty windows (<= 12 extra requests, stops at
  COINBASE_EARLIEST_S); the live stream's read-backs use stepBack=false. Auto-older paging is
  bounded by MAX_AUTO_OLDER_PAGES (5).
- The provider is chosen once at module load; a provider change saves and reloads (ref set first,
  pending debounced save cleared, pagehide flush writes it). Symbol fallback runs once
  (`symbolsChecked`).
- Appearance changes: one Chart effect (applyOptions only). Colours throttled 100 ms
  (useThrottledValue), saves debounced 400 ms. backtestOverlay.setData with `sameEquity` re-sets
  markers only.
- `takeScreenshot(true, false)` is correct for LWC 5.2.1.
- Seasonals: isNotable compares to the pooled mean with a t(0.975, n-1) band; tQuantile975 is a
  df<=30 table + A&S 26.7.5 Cornish-Fisher (checked df=31 -> 2.039513).

**Why:** these are the gaps most likely to come back with bar replay, multi-chart and the paper
panel in later Phase 9 waves.
**How to apply:** grep coinbaseStream.ts requestConsumerGapFill / useModalKeys / applyLiveCandle
before new findings. Related: [[review-watchlist]].
