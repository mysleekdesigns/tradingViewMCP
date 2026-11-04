---
date: 2026-11-04
phase: 21
kind: browser checks
---

# Phase 21 browser checks

Run by Claude on 2026-11-04, about 19:50 to 20:10 UTC. Headless Chrome for Testing over CDP, a throwaway server on
8790 with a scratch database, an unminified production build. Live public Binance data. The build carried two debug
lines added to the scratch bundle only, to read the layer's output and its paint calls.

Every expected value below came from a separate script that reads the raw candles from `/api/candles` and imports
nothing from `web/src`.

The checks ran twice. The first pass found no fault in the app. The code and quant reviews then found problems the
browser could not, and those fixes changed behaviour, so the level, forming-bar and edge checks were run again on the
final code. Those are the results for 1h, 5m, 1m, gap and failure. The ATR, switching, toggle, two-chart and drawing
order results are from the first pass, on code the fixes did not change in those paths; only the label rule and the
closed-row rule moved.

## Levels against the 1d candles

| Check | 1h | 5m |
|---|---|---|
| PDH/PDL steps compared with the previous day's 1d candle | 42, 0 mismatches | 4, 0 mismatches |
| Steps starting exactly at 00:00 UTC | 42 of 42 | 4 of 4 |
| PWH/PWL steps compared with the previous Monday-to-Sunday week | 8, 0 mismatches | 2, 0 mismatches |
| Forming 1d row excluded | Last closed day 2026-11-03 | Same |

## Legend against a hand calculation

For 2026-11-04, from the closed 1d candles and that day's closed intraday bars:

| Value | Hand | App |
|---|---|---|
| Daily ATR(14) as of the 2026-11-03 close | 1949.8858 | 1949.8858 |
| SMA(50) | 81966.5512 | 81966.5512 |
| Bias: close 77453.86 against that SMA | Below | Below SMA 50 |
| Range used: (81196.01 − 75736.38) ÷ 1949.8858 | 279.997% | 279.997%, shown as 280% |

1h and 5m agree on all four.

## Nothing moves while a candle forms

Sampled every 3 s for 45 s. A change means any difference in the level result or the legend text.

| Interval | Live WebSocket frames | Changes | Layer recomputes | Bars pushed |
|---|---|---|---|---|
| 1h | 92 | 0 | 1 | 499, unchanged |
| 5m | 92 | 0 | 1 | 499, unchanged |

## ATR pane

Legend against an independent Wilder calculation on the same 500 bars:

| Interval | App | Hand |
|---|---|---|
| 5m | 120.19 | 120.19 |
| 1h | 531.08 | 531.08 |
| 1d | 2200.58 | 2200.58 |

The last closed 1d value, 1949.89, agrees with the Key Levels ATR.

## Hidden on 1D, and switching

- On 1D the legend reads "KL 14 50 Not shown on 1D and above", no lines or labels appear, and no daily request is made
  for it. The screenshot was inspected. Zero level paints in 6 s.
- 1h to 1D to 5m to ETHUSDT on one mounted page: levels return on 5m, and after the symbol switch the labels are ETH's
  (2309.00 / 2241.15, matching ETH's 1d candle). No BTC value remained.
- No page exceptions or console errors in any run.

## Toggles

Seeded `showDaily`, `showRange`, `showBias` and `showWeekly` off in turn. Each removed only its own part: the PD pair,
the range text, the bias chip, the PW pair.

## Two charts

BTCUSDT 1h beside ETHUSDT 5m: one daily request each, with each chart showing its own symbol's levels.

## Paint order, both themes

Inspected at 3× and 4× zoom. A real trendline was drawn through the PDH label with the drawing tool. The line and
handle paint over the label, the label's plate paints over candles and stays readable, and the level line runs under
the candles. Dark and light both pass.

The first attempt at this check missed the label, so its result was thrown away and the line redrawn.

## Cases added after review, final code

| Case | Result |
|---|---|
| 1m at 20:09 UTC: 500 bars do not reach midnight | `Range used — · live`, zero points, levels still drawn |
| Yesterday's 1d row removed from the answer | Today has no PDH/PDL line or label; legend shows dashes; PWH/PWL stay, as their week is complete |
| The 1d request fails | Legend note "Daily candles unavailable", nothing drawn |

## Not checked in a browser

- A real UTC midnight rollover. The refresh timing is covered by fake-timer tests only.
- The 30-minute slow retry.
- Coinbase as the provider.
- A real clock-skewed machine.
