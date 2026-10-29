# web

Vite + React + TypeScript frontend for tradingView (Lightweight Charts v5). Market data is
Binance global spot from Binance's market-data-only hosts (`data-api.binance.vision`,
`data-stream.binance.vision`), through the local server by default; hosts and the venue name
live in `src/data/venue.ts`.

```sh
npm run dev        # http://localhost:5173
npx tsc -b         # typecheck
npm run lint       # ESLint (type-aware)
npx vitest run     # tests
npm run format     # Prettier
```

See `../PLAN.md` for phases and scope.

## Third-party chart plugins (Phase 14)

- `@tradingview/lwc-plugin-accessibility` 1.0.0 (Apache-2.0, TradingView; pinned exactly) adds
  keyboard and screen-reader access to the chart through `src/chart/chartAccessibility.ts`. Focus
  the chart with Tab: ←/→ bars, ↑/↓ series, PgUp/PgDn, Home/End, +/− zoom, Enter/Space speaks a
  summary, `,` help, `.` data table. Its default H/T/Escape keys are rebound to `,`/`.`/none so
  they do not clash with type-to-search, Escape for drawings, or bar replay.
- `@tradingview/lwc-plugin-vertical-line` and `lwc-plugin-anchored-text` were evaluated and not
  adopted: there was no hand-written code for them to replace (the watermark uses the built-in
  `createTextWatermark`), and anchored-text is not published on npm.
- The fixed-range volume profile renderer (`src/chart/volumeProfileLayer.ts`) is ported from
  TradingView's plugin-examples `volume-profile` (Apache-2.0).

## Drawing library evaluation (Phase 14)

**Decision: reject for now.** We keep the hand-built drawing tools (horizontal line, horizontal
ray, trend line, Fibonacci retracement, rectangle) and do not add `lightweight-charts-drawing`
beside them.

We will look again, as "adopt beside" loaded on demand through `import()`, when both are true:

- the owner names extra tools they would actually use (long/short position, parallel channel,
  text/note, pitchfork, ...);
- the library reaches 1.0, or goes about two months without a breaking change.

Building one or two of those tools on our own drawing controller is the alternative.

Evaluated 2026-10-28 on a throwaway branch: `lightweight-charts-drawing` 0.6.2 (MIT, deepentropy)
against `lightweight-charts` 5.2.1. The evaluation tests (`src/chart/drawings/libEval.test.ts`, 13
tests, all passing) were not merged.

### Why reject now

1. **It costs more than the chart library itself.** 212 KB minified, about 63 KB gzipped
   (`DrawingManager` import, lightweight-charts external), and most of it cannot be tree-shaken
   because the manager reaches all 86 tools. `lightweight-charts` 5.2.1 is about 59 KB gzipped; our
   whole drawing layer (controller, primitive, geometry, alert lines, adapter) is about 7.7 KB
   gzipped.
2. **The API is still moving.** 0.1 → 0.2 replaced the whole API; 9 releases between 1 and 15
   October 2026 (0.2.4 → 0.6.2); patch 0.6.2 changed point placement (an off-bar time is now drawn
   on the bar holding it instead of interpolated). Single maintainer; unit tests arrived in 0.6.2.
3. **Two interaction layers on one chart is real work.** Both listen for pointer presses in the
   capture phase, both handle `Escape`/`Delete`/`Backspace` on `window`, both draw a `zOrder: 'top'`
   primitive with `hitTest`. "Adopt beside" needs routing for the armed tool, one selection across
   both, shared Delete handling and a hit-test priority.
4. **It does not replace the app-side work**, so replacing our five tools gains little. The host
   still owns undo, storage, settings dialogs, toolbar and context menu. We already have all of
   those except undo: per-symbol storage (localStorage mirror + server SQLite, debounced PUTs,
   reconcile, ≤500 per symbol), strict validation, style bar, right-click menu, alert lines.
5. **Shallow import validation.** `importJSON` loads our kind name `hline`, a one-point trend line,
   a negative price, `NaN` and duplicate ids (all tested); our `parseDrawing` rejects every one.
   Malformed JSON makes it throw.

### What 82 extra tools would give

TradingView-style channels and pitchforks, Gann and more Fibonacci tools, long/short position with
risk/reward, price/date ranges and measures, text/note/callout/table, patterns and Elliott waves,
brush and highlighter, regression trend. The useful ones for a personal crypto chart are mainly
long/short position, parallel channel, price range and text; each still needs host settings UI
(the style model has about 100 optional fields and no dialogs). Anchored VWAP and the fixed-range
volume profile were added in Phase 14 as our own indicators with known-value and no-look-ahead
tests.

### Comparison

| Area                                       | Ours (hand-built)                                                                                     | `lightweight-charts-drawing` 0.6.2                                                                                  |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Kinds                                      | 5: hline, hray, trendline, fib, rectangle                                                             | 86 (TradingView set); ours map to `horizontal-line`, `horizontal-ray`, `trend-line`, `fib-retracement`, `rectangle` |
| Create gestures                            | Press-drag-release, or click-move-click                                                               | Click per point (press-drag only for brush/highlighter), "keep drawing" option                                      |
| Magnet / snap                              | Time snapped to the bar open; no OHLC magnet                                                          | `off` / `weak` / `strong` OHLC magnet, Ctrl/Cmd inverts, Shift for 45° and squares                                  |
| Off-bar anchor (drawn on another interval) | Interpolated between bars                                                                             | Drawn on the bar holding it; stored time kept                                                                       |
| Handles / drag                             | Handles on the selected drawing, body drag, 3 px drag threshold                                       | TradingView anchors, body drag, Ctrl+drag clones, multi-select group drag, lock                                     |
| Selection                                  | Single                                                                                                | Multi-select (Ctrl/Cmd), `select(ids)`                                                                              |
| Delete / Escape                            | Our controller                                                                                        | Its own `window` keydown handler                                                                                    |
| Overlap hit testing                        | Selected handles first, then the **nearest** stroke                                                   | **Topmost** drawing wins: a filled rectangle over a trend line takes the click                                      |
| LWC 5.2 hit testing                        | `hitTest` → `externalId: 'drawing:<id>'`, `zOrder: 'top'`                                             | `hitTest` → `externalId: 'lwcd:<id>:<cursor>'`, `zOrder: 'top'`                                                     |
| Styles                                     | Colour, width 1–4, solid/dashed/dotted; style bar UI                                                  | About 100 style fields; no UI                                                                                       |
| Price-axis label                           | hline/hray always                                                                                     | Only with `showPriceLabels` (not merged in on import)                                                               |
| PNG export                                 | Included (top layer, `takeScreenshot(true, false)`)                                                   | Same layer, so included the same way (browser check)                                                                |
| Persistence                                | Per symbol, localStorage + server SQLite (`data.v: 1`, ≤64 KB each), offline queue, strict validation | Host's job: `exportJSON()` string, `add`/`update`/`remove` events, no symbol scoping                                |
| Undo                                       | None today                                                                                            | Host's job (`gestureEnd` for grouping)                                                                              |
| Alerts / context menu                      | `AlertLinesManager`, `ChartContextMenu`                                                               | None                                                                                                                |
| Bundle                                     | ~7.7 KB gzipped                                                                                       | ~63 KB gzipped                                                                                                      |
| Stability                                  | Ours, 116+ tests in `chart/drawings`                                                                  | 0.x, 9 releases in 15 days, behaviour change in a patch                                                             |

### Conversion findings

- Our stored forms (localStorage `{id, symbol, kind, anchors, style}`, server rows
  `{id, symbol, data: {v: 1, ...}}`) load **0** drawings as they are; a converter is required.
- `exportJSON()` shape: `[{ id, kind, points: [{ time, price }], style: { color, width, lineStyle,
... } }]`, no `symbol`, no version. `anchors` → `points`, `lineWidth` → `width`; colour, line
  style and ids carry over. All five kinds round-trip through convert → import → export → convert
  back (tested).
- Must be supplied by the host: `symbol` (one manager per symbol), fib `reverse: true` (the library
  puts level 0 at `points[1]`), our 7 fib levels and colours (factory has 11 up to 4.236), fills as
  one `transparency`, trend line `extendLeft/Right: false`, hline/hray `showPriceLabels: true`.
  Our label texts and handle look are not carried over.

### Risks if revisited

Breaking 0.x releases; ~63 KB gzipped unless loaded with `import()`; two input layers fighting
over pointer/Delete/Escape; a placement change for multi-interval drawings; one series primitive
to rebuild on chart-type switch, per multi-chart pane and around bar replay; host-side validation
still required.

### Owner browser checks (not verified)

Placement and drag feel versus ours; magnet modes on live 1m candles; look on dark and light
themes; PNG export with library drawings; cursor and hover through LWC 5.2 hit testing;
performance with a few hundred drawings.

## Market structure overlay (Phase 16)

**Visual only, no edge claim.** The overlay draws confirmed swing points, break of structure
(BOS) and change of character (ChoCh) markers, and flip zones with their first retest. The claims
behind these ideas come from NQ/ES futures research ("Price action strategies research",
`../agents/research/reports/`) and are unverified for crypto. Nothing here is a backtest or a
signal. The compute lives in `src/indicators/marketStructure.ts` (`computeMarketStructure`, and
`MarketStructureEngine` for one closed bar at a time).

**Parity source.** Two hand-built fixtures in `web/src/indicators/`, together:

- `marketStructure.fixture.ts`: a 44-bar series. Every pivot, BOS, ChoCh, zone, retest,
  invalidation and drop in it is worked out by hand in its comments.
- `marketStructure.rules.fixture.ts`: 14 short rule cases (5 to 8 bars, `left` = `right` = 1),
  each pinning one rule the long fixture leaves loose: closes not wicks, break ties, retest and
  invalidation edge ties, breaks before same-bar swings, invalidation before the `maxZones` drop,
  an outside bar that is both swings, a zero-height zone, and equal highs further apart than
  `left`. Each has its hand working in comments.

Any future lab feature that reuses these rules (for example a causal swing feature in the lab's
feature registry) must reproduce both fixtures' expected values exactly.

### Inputs and defaults

| Input                             | Default | Range  | Meaning                                                                      |
| --------------------------------- | ------- | ------ | ---------------------------------------------------------------------------- |
| Left bars (`left`)                | 5       | 1–50   | bars before a pivot that it must be strictly beyond                          |
| Right bars (`right`)              | 5       | 1–50   | bars after a pivot that it must be at or beyond; also the confirmation delay |
| Max active zones (`maxZones`)     | 10      | 1–50   | active flip zones kept; a new zone past this ends the oldest                 |
| Swings / BOS / ChoCh / Flip zones | on      | on/off | display toggles only; they never change the computation                      |

### Rules

Only closed bars are used. The forming bar (the last REST row, or a WebSocket kline with
`x: false`) never creates, moves or removes a swing, marker or zone. Comparisons are exact
floating-point comparisons on the given prices. All times are bar open times in UNIX seconds.

**Traded bars only.** The engine runs on the bars the chart plots: bars with volume 0 (Binance
returns a row for an interval without trades) are skipped, never filled or synthesised. So `left`
and `right` count traded bars, and a pivot's `right` bars after it are the next `right` traded
bars, whatever the clock time between them. A lab port must do the same (drop zero-volume rows
first, then apply the rules) to match the fixtures.

**Final values only (live).** The live controller exposes `closedThrough`
(`LiveCandlesSnapshot.closedThrough`): the open time of the latest bar such that it and every
bar before it has final values. A bar is final when its values came from a closed (`x: true`)
tick that is not provisional, from a REST row that was not the newest row of its response, from
an older-history page, or from the newest REST row when the bar was already final before that
response. Its values are not final while they came from a forming (`x: false`) tick, from a
provisional closed tick, or from the newest REST row. It is contiguous: it never passes a bar
that is not final. The overlay uses only plotted bars with time `<= closedThrough`, so:

- After a REST load it is the second-to-last row; it moves onto a bar when that bar's `x: true`
  tick arrives (usually just before the next bar opens).
- **Provisional closes (Coinbase).** Coinbase has no closed-kline message: the stream builds the
  bar from trades, reads it back over REST when the bucket ends, and, unless its live view is
  known complete, reads it back once more about 5 s later and may correct it. That first closed
  bar is passed as provisional (`onCandle(c, true, true)`), so it is not final; the second
  send (closed, no flag, corrected or with the same values, also when that read fails) settles
  it. A provisional close never makes an event or a zone that the correction would remove.
  Binance and the server never mark a close provisional. After a Coinbase stall, the buckets
  it skipped are read from REST; the newest of them is provisional too and is read back once
  more, like any other close.
- **Custom timeframes** (aggregated from base bars, e.g. 3m from 1m, 30s from 1s) pass the
  flag on: a bucket that closes while one of its base bars is a provisional close is sent
  closed and provisional (again only if its values change), and closed without the flag once
  the base source settles that bar. A base tick that skips base bars (missed without any
  reported drop) is handled like a drop: no bucket is closed from the base bars seen, and a
  bucket entered mid-way is not treated as complete; they are read from REST first, and a
  bucket whose read fails is handed to the controller's repair (`onRepair`, below).
- A bar that was forming when the socket dropped keeps its partial values until the reconnect
  gap fill brings the final ones. Ticks for later bars do not move `closedThrough` past it, so
  its partial values never make an event or a zone that the gap fill would later remove.
- **Hole cap.** A drop can also lose whole bars, even when the last bar before it was final.
  When a reconnect needs a gap fill, `closedThrough` is capped at the last bar on screen at that
  moment (and at the bar before a bar an aggregated source gave up on, `onRepair`) until a
  non-empty REST result requested after that moment lands. Ticks for later bars (appended after
  the possible hole) never move it past the cap, so the engine never computes across missing
  bars. A REST window that starts after the cap is not merged over the hole; it replaces the
  window.
- **Skipped bars without a drop.** On Binance a socket that goes quiet reconnects without
  leaving 'live', so a bar can be missed with no drop reported. A tick that opens a bar more
  than one interval after the last bar on screen sets the same cap at that last bar and runs
  one gap fill: queued behind a request already in flight that was sent before the skipped
  bars opened (by the local clock, with 2 s of slack), held through a Retry-After window, and
  at most one such fill per 30 s. A failed one is not retried on a timer; the recovery below
  takes over. An interval without trades costs that one REST call: Binance returns its
  zero-volume row, which settles it (Coinbase has no row for it, and a non-empty answer still
  lifts the cap).
- **Retries and recovery.** A failed gap fill (or missed-close repair) is retried automatically
  after 2, 4 and 8 s (after the Retry-After window on a 429/418). While those run, the
  structure stays at the last confirmed bar. If they all fail, the controller recovers by
  itself: whenever a new bar opens while `closedThrough` is stuck behind a non-final bar older
  than the previous last bar, or behind a hole cap, and no request is in flight, scheduled or
  blocked, it runs one more gap fill (no timer retries of its own; at most one per bar interval
  and never more often than every 30 s). A repair queued behind a request that then failed
  does not block it. This also repairs a missed `x: true` right after a
  REST load, one bar later. A stalled overlay is honest; a repainting one is not.
- The last bar of a halted pair (no more ticks, no closed tick) is never used: it waits.

Bar replay passes the replay cursor bar (replay shows closed bars only), capped at the live
`closedThrough` of the same stream (`structureClosedThrough` in `src/chart/structureBars.ts`).
Any pushed bar that is no longer the same object (a revised bar anywhere in the pushed history,
older history, another window, a replay step back) starts the computation over.

**Swings (fractal pivots).**

- Swing high at bar `p`: `high[p] > high[p-i]` for every `i` in `1..left` (strict) **and**
  `high[p] >= high[p+j]` for every `j` in `1..right` (ties allowed). A pivot needs `p >= left`.
- Swing low at bar `p`, mirrored: `low[p] < low[p-i]` for every `i` in `1..left` (strict) and
  `low[p] <= low[p+j]` for every `j` in `1..right`.
- Ties: of an equal-high pair (or equal-low pair) **within `left` bars of each other**, the
  **earlier** bar is the pivot; the later bar fails the strict left test. Equal highs (or lows)
  further apart than `left` bars can both be pivots. One bar can be both a swing high and a swing
  low (an outside bar); the high is recorded first.
- A swing is confirmed on the close of bar `p + right`. Its `confirmTime` is that bar's time.
  Nothing about it exists before then: a swing level is drawn from its confirmation bar, and a
  pivot dot is only shown once the swing is confirmed.

**Per bar `t`, processed in this order:**

1. **Zones** created before `t` that are still active:
   - First retest: if the zone has no retest yet and `high[t] >= bottom` and `low[t] <= top`,
     then `retestTime = t`.
   - Invalidation: a support zone with `close[t] < bottom`, or a resistance zone with
     `close[t] > top`, ends at `t` with reason `invalidated`. One bar can be both the first
     retest and the invalidation.
2. **Breaks**, against `H` = the most recently confirmed swing high and `L` = the most recently
   confirmed swing low (both confirmed at bars before `t`, because step 3 runs after this one).
   Only the latest swing of each kind can be broken. An older swing superseded by a newer one is
   never broken, even if a later close goes through it. Each swing breaks at most once. Closes
   only, never wicks; strict comparisons.
   - `H` first: if `H` is unbroken and `close[t] > H.price`, an `up` event is recorded. It is a
     `choch` if the structure was `down`, otherwise a `bos` (the first break from `none` is a
     `bos`). The structure becomes `up`, `H` is marked broken at `t`, and a **support** zone is
     created from `H`'s pivot bar: `top = high[p_H]`, `bottom = max(open[p_H], close[p_H])`.
   - Then `L`: if `L` is unbroken and `close[t] < L.price`, a `down` event is recorded. It is a
     `choch` if the structure was `up`, otherwise a `bos`. The structure becomes `down`, `L` is
     marked broken at `t`, and a **resistance** zone is created from `L`'s pivot bar:
     `bottom = low[p_L]`, `top = min(open[p_L], close[p_L])`.
   - Zones of zero height are kept.
   - Zone creation: if `maxZones` zones are already active (after step 1's invalidations on
     this bar), the oldest active zone (the one created earliest) ends at `t` with reason
     `dropped`, and then the new zone is added.
   - Two breaks on one bar cannot happen. It would need `H.price < close[t] < L.price` with both
     unbroken, but a swing low's confirmation bar closes at or above `L.price` and a swing high's
     at or below `H.price`, so whichever was confirmed later would already have broken the other
     (and one pivot bar gives `H.price >= L.price`). So the `H`-then-`L` order never changes the
     output, and no zone ever ends on its own creation bar.
3. **Swings confirmed at `t`** (pivot `p = t - right`): swing high first, then swing low. Each
   becomes the new `H` / `L`.

The structure state is `none` until the first break. After that it is `up` or `down`, following
the latest event. Touch counts are not shown or scored.

**Timing guarantees (tested).** Every output dated at bar `t` uses only bars `<= t`: changing any
bar after `t` leaves every swing, event and zone dated `<= t` unchanged. Each swing's
`confirmTime` is exactly `right` bars after its `pivotTime`. Each event is later than its swing's
confirmation bar, and each retest or invalidation is later than its zone's creation bar.
Pushing closed bars one at a time into `MarketStructureEngine` gives the same result as a full
recompute. 100k bars compute in well under 1.5 s at the defaults.
