---
title: Price action strategies research
date: 2026-10-29
source: https://www.youtube.com/watch?v=z97PnFIPoFg
status: research only (no code, no lab runs, no PLAN.md changes)
---

# Price action strategies research

The source is one YouTube video (AI Pathways, 2026-10-28). It turns discretionary price action into rules
and tests about 101k variants on NQ and ES futures. Claim-by-claim notes, with the untrusted-source caveat,
are in [[research_notes/Price action strategies research/video_notes]]. This report asks what carries over
to this project's crypto lab and chart, and how much the claims should be trusted.

## 1. What the video gets right (and we already do)

Its method rhymes with this lab, which is a good sign for the method and says nothing yet about the result:

| Video practice | This project |
|---|---|
| Turn "feel" into explicit dials | Declarative plan JSON, registry features only |
| Real costs and slippage; same-bar stop+target counts as a loss | Per-side costs, intrabar stops, conservative ordering (`lab/lab/engine/backtest.py`) |
| Random-line / random-trade controls | Random-entry null percentile, vol-targeted buy-and-hold benchmark |
| Locked 2-year holdout | Locked holdout from 2025-10-01, budgeted `holdout_check` |
| "Higher timeframes are easier: 1m costs eat about 40% of gross" | Our own 5m/15m ports died on costs (squeeze breakout BTC 15m: gross Sharpe 0.44 → net −0.88) |

## 2. Where the claims are weak

1. **The funnel looks like no edge.** About 8% of variants beat Sharpe 0.5. With a design window of about
   3 years, the standard error of an annual Sharpe is about 0.58, so roughly 13–19% of *zero-edge*
   strategies clear 0.5 by luck. Costs pull that lower. The headline funnel is what noise would give.
   Our DSR bar (N ≈ 4,200, a bar near Sharpe 1.7) exists to catch exactly this.
2. **No benchmark on the holdout.** "+$62k on one NQ contract over 2 years" comes with no buy-and-hold,
   no long/short split, and no exposure-matched null. In an index bull market, long reversal trades
   carry beta. Our own idea 8 (rolling-high breakout) and the ETH 4h Donchian work looked good the same
   way and turned out to be "ETH beta with smaller drawdowns" that lost to vol-targeted buy-and-hold.
3. **One hypothesis, not 48 wins.** The 48 "usual versions" share one logic. Under our family rule
   (`family_return_corr` 0.8) they are one hypothesis with one holdout.
4. **The key dial is hidden.** ChoCh depends on how a swing high or low is defined. Most swing, fractal
   and zigzag indicators *repaint*: a pivot at bar t is only known k bars later. The video doesn't say
   how it handled this. If any of its swings were confirmed early, the result is look-ahead.
5. **Some findings don't transfer.** The NY-session filter is about futures cash hours; crypto trades
   24/7. "Sweeps of stop clusters" depend on venue microstructure. The lab is long-only spot
   (shorts exist only in the adversarial sign flip), so half of the ChoCh setup (bearish ChoCh shorts)
   can't be tested.
6. **Promotional, and can't be reproduced.** No code, data, or per-trade statistics. Treat each claim as
   a hypothesis to pre-register, not as evidence.

## 3. Findings worth keeping as hypotheses or design priors

Ranked by how useful and testable they are here:

| # | Idea from the video | Crypto translation (long-only) | Testable today? |
|---|---|---|---|
| A | **4h bullish ChoCh**: in a downtrend (lower highs and lows), the first close above the last confirmed lower high. Stop below the last swing low, target 1R, no sweep filter. | Same rule on BTCUSDT/ETHUSDT 4h, entry at next open (the lab default). | **No.** It needs a causal swing feature and a structure-based stop (§4). |
| B | **Flip zones**: a broken resistance retested from above holds more often than random. | Feature: distance from close to the most recently *broken* confirmed swing high, in ATR units, plus a "first retest" flag. Use as an entry filter. | No. Needs the swing feature. |
| C | **Skip trades in the direction of a 3-day run** | On 4h, 3 days = 18 bars. A conjunct `roc(18) < 0` on long entries (don't buy after a 3-day rally). | **Yes**, with the existing `roc` feature. But on its own it is a short-term reversal filter, close to ideas already screened. |
| D | **Touch count doesn't matter** | Don't build "level strength = touches". If we build levels, use recency and the flip state instead. | Design prior. Nothing to test. |
| E | **Hard stops beat close-based stops** | Prefer intrabar stops (which the engine already has) over `long_exit: close < x` exits. | Design prior. Our current pct stops are already hard. |
| F | **Higher timeframes** | Keep research at 4h/1d. This agrees with our 5m/15m results. | Already in practice. |
| G | **Win rate is a poor proxy for profit** (r ≈ 0.27) | Never rank or screen on win rate. Our criteria don't use it, so keep it that way. | Already in practice. |

**Overlap with past work.** BOS ("close above the last high") is close to the Donchian / rolling-high
breakouts we have already abandoned (idea 3, idea 8, the ETH 4h Donchian family). ChoCh is different
because it is a *reversal* entry after a downtrend. A bullish-ChoCh-entry / bearish-ChoCh-exit system,
though, behaves like a slow trend filter and may land in the same return family as the EMA and Donchian
filters. `family_return_corr` will decide that. Expect it to merge with an old family.

## 4. What implementing it would take

None of this is approved work. Most of it touches lab paths that are owner territory (`lab/lab/registry/`
and `lab/lab/engine/` are read-only to agents under the lab lock, and new registry features come from the
owner with look-ahead tests).

1. **Causal swing-pivot feature** (`lab/lab/registry/`, owner). A fractal pivot with left/right lookback
   `k` that is only *published* `k` bars after the pivot bar. Outputs: `last_swing_high`,
   `last_swing_low` (as ratios to close or in ATR units, since the validator rejects raw price levels),
   `bars_since_swing`, and a `structure_state` (up / down / none). It must ship the perturbation test:
   changing bars after t must not change values at t. Repainting is the main risk.
2. **Structure events feature**: `choch_up` and `bos_up` event flags, plus `flip_retest_up` (B), built on
   (1). 0/1 outputs compared with `== 1` stay within plan rules.
3. **Structure-based stop and R-multiple target** (`lab/lab/engine/` + `plan/schema.py`, owner). Today
   `RiskSpec` only has `stop_loss_pct` and `take_profit_pct` relative to the entry fill. The video's setup
   needs "stop at the feature value frozen at entry" and "target = entry + m × (entry − stop)". This is the
   biggest change. Approximating it with a fixed % stop or a `close < swing_low` exit would test a
   *different* hypothesis (and the video says close-based stops are worse), so a proxy failure would not
   reject the idea.
4. **Pre-register hypothesis H4 before any run.** Bullish ChoCh, 4h, BTC+ETH, no sweep, 1R, with `k`
   fixed *before* looking (for example the common fractal `k = 2`; the video doesn't publish its value).
   At most a couple of tunable params. It uses the edge criteria unchanged, and DSR's N carries on from
   about 4,200. Our honest prior: likely "no edge", like H1 and H3, given the beta problem in §2.2.
5. **Chart side (web, no lab lock): a "Market Structure" overlay** via `/add-indicator`. It draws
   confirmed swing points, BOS/ChoCh markers (placed on the *confirmation* bar, never back-dated), and
   flip zones as boxes or price lines. It is useful for discretionary reading whatever the lab finds, and
   it is cheap. It needs the same no-look-ahead Vitest, and the marker shows only once the pivot is
   confirmed.

## 5. Recommendation

1. **Build the chart overlay first** (§4.5). It is the only item with value even if the edge question
   comes back "no", it sits outside the lab lock, and it lets the owner check by eye that the swing and
   ChoCh definitions match how price action traders draw them, before any backtest uses them.
2. **If the owner wants the lab test**, add §4.1–4.3 as an owner phase (like Phase 11/15) after Phase 15
   finishes, then run H4 pre-registered. Don't run a Donchian proxy now: it would raise N, test a
   different rule, and prove nothing either way.
3. **Skip** the session filter (futures-specific) and sweep-based entries (the video itself found them
   weak).
4. Item C (`roc(18) < 0` skip filter) is testable today but has little prior support. It is a short-term
   reversal filter, and our intraday reversal ports already failed. It is not worth a separate run;
   include it, if at all, as one fixed conjunct inside H4.

*Results in the video are historical, on a different asset class, and were not reproduced here. Nothing
in this report is investment advice.*
