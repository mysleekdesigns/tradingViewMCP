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
