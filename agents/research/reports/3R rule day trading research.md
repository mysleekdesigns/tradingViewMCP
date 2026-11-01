---
date: 2026-11-01
source: https://www.youtube.com/watch?v=XBOGd67BJks
outcome: chart overlay recommended (PLAN.md Phase 21); lab test optional, weak prior
notes: "[[3R rule day trading research/video_notes]]"
---

# 3R rule day trading research

Review of The Rumers, "If You Only Have $50, Do This Every Morning (The 3R Rule)" (24:44, read 2026-11-01),
plus a literature check of its claims, and whether any of it belongs in this project. The video's own claims
and the presenter's results are untrusted and unverified. Video summary:
`agents/research/research_notes/3R rule day trading research/video_notes.md`.

> [!warning] Untrusted source
> A day-trading education channel selling a paid live room. No record, sample size or backtest is shown; all
> examples are hand-picked 5-minute charts of US stocks/ETFs and index futures. The literature check used
> the `researcher` agent through CrawlForge; it read abstracts and snippets, not full papers. Items it marked
> as background knowledge are flagged below. No page contained instructions aimed at AI agents.

## 1. What the video proposes

A "probability factor" to pick intraday trades whose 3:1+ target is reachable:

1. **Trend bias:** daily close vs the 50-day SMA. Trade with it at full size and wide targets; trade against
   it only as small scalps.
2. **Liquidity zones:** previous day's high/low ("range high/low") and the next older swing beyond each.
   Sellers sit at the upper levels, buyers at the lower ones.
3. **Range:** daily ATR(14) is the day's expected move. Skip targets that need more than 100% of ATR; a
   move that has already used 1–2× ATR into a prior-day level is a fade.
4. **Sizing:** stop at a structural level, small risk; +10% size per +10% account growth once profitable
   3 of 5 days for a couple of weeks.

## 2. Evidence check

| Claim | Verdict | Key evidence |
|---|---|---|
| Daily 50-SMA as trend bias | Mixed | MA/momentum rules on crypto survive data-snooping tests in Hudson & Urquhart (2019/2021), but they found no out-of-sample predictability for BTC itself, only other coins; gains come mostly from avoiding drawdowns. Grobys, Ahmed & Sapkota (2020) is multi-coin, not a 50-SMA test. Nothing tests a daily SMA as a filter for 5m entries. **In this project** trend filters on BTC/ETH are an exhausted family: EMA/SMA trend filters, the Markov regime filter (idea 13) and the SMA200 overlay (Phase 15) all failed to beat (vol-targeted) buy-and-hold. |
| Prior-day high/low as S/R | Mixed, unquantified | Osler (2000, 2003) shows published FX S/R levels interrupt trends, and that stop-order clustering makes price reverse at levels but accelerate once they break. That is FX dealer levels, not prior-day highs/lows. No published reverse-vs-break rate for prior-day levels in equities or crypto was found. |
| ATR used up → fade | Unsupported, partly contradicted | Gao, Han, Li & Zhou (JFE 2018): on SPY, the first half-hour return predicts the last half-hour with the **same** sign, strongest on high-volatility, high-volume days, i.e. continuation when range is already large. Shen et al. (Financial Review 2022) find the same intraday momentum in BTC, in and out of sample. No direct test of "fade after 100–200% of ATR" was found. |
| Fixed 2:1 / 3:1 targets | No edge by themselves | Arithmetic, not literature: with no entry edge, a wider target lowers the win rate and expectancy stays about minus costs. The video's real point (check that the target is reachable) is a sound filter but not a source of edge. |
| SPY open → close wins 51–54%; "futures up 80% of the time" | Framing contradicted; 80% unverified | Cooper, Cliff & Gulen (2008): the 1993–2006 US equity premium came from overnight (close→open) returns, with open→close about zero or negative; Lou, Polk & Skouras (JFE 2019) find strategy profits split between overnight and intraday, often with opposite signs. A 54% win rate is about the up-day base rate (agent's background knowledge) and says nothing about mean return after costs. No source for 80%. BTC has hour-of-day patterns in blog-level, in-sample studies (Quantpedia: 22:00–23:00 UTC; NYSE-hours effects), with heavy snooping risk. |
| "3 of 5 profitable days" gate | Statistically empty | t = 2 needs n = (2 / daily Sharpe)² days with daily Sharpe = annual / √365: about 1,460 days at annual Sharpe 1, 365 at 2, 160 at 3. Two weeks would need annual Sharpe ≈ 10. A coin-flip trader passes 3-of-5 half the time; a Sharpe-1 trader about 54%. The +10%/+10% raise is stepwise fixed-fractional sizing, already covered by Phase 20 Part E2. |

## 3. Fit with this project

What the project already shows, and why it matters here:

- **Costs kill short-horizon crypto rules.** Every 5m/15m port of ideas 1–17 failed after costs (journals
  2026-10-26 intraday ports; ONDO 5m RSI 2026-11-01: about +0.14% gross per trade vs about 0.30% round
  trip). The video's scalps (30 s–2 min) and $20–$50 risk trades sit far below what spot fees and
  next-bar-open fills allow.
- **Trend filtering is done.** Daily SMA/EMA trend gates on BTC/ETH have been tested repeatedly with no edge.
  The 50-day SMA bias adds nothing new to the lab.
- **Crypto has no session open or close.** "Previous day" and "the morning" become UTC-day conventions.
  Crypto has no opening auction or overnight gap, which is where the equity mechanics come from. Phase 18
  already logged this weak prior for an opening-range hypothesis.
- **What is new:** the lab has never tested prior-UTC-day high/low levels or a "share of daily ATR already
  used" feature. The registry has `donchian` (rolling N bars, not calendar-day levels) and `atr` (on the
  bar's own interval, not daily). Both new features would be owner items under the lab lock.
- **The chart lacks these as well.** `web/src/indicators/` has no ATR, no previous-day high/low, and no
  higher-timeframe trend readout. Phase 16 (market structure) and Phase 18 (sessions) cover swings and
  session boxes, but not these.

## 4. Recommendation

**Yes to a small chart overlay. A lab test is optional, low priority, and expected to fail.**

1. **Recommended: "Key Levels & Range" chart overlay (web only, visual, no edge claim)**, the same pattern
   as Phases 16 and 18. On intraday intervals, draw:
   - previous UTC-day high and low (optionally the prior week's), from closed daily bars only;
   - a daily ATR(14) readout: today's high−low and |close − open| as a % of daily ATR ("range used");
   - a daily 50-SMA bias badge (above / below), computed from closed daily bars.

   Plus a standalone ATR pane indicator, which the chart is missing. Cheap, useful for reading the chart,
   and it lets the owner check by eye whether prior-day levels hold or break on BTC/ETH before any lab budget
   is spent. Traps: the current UTC day's bar is forming, so "previous day" must use only closed daily bars;
   the range-used value moves during the day by design and must be labelled as live; known-value and
   perturbation tests as in Phase 18.

2. **Optional owner lab phase (lab lock):** registry features `prev_day_high` / `prev_day_low` (as ratios
   to close, e.g. `close / prev_day_high`) and `day_range_used` (current UTC day's range ÷ prior daily
   ATR), each with a perturbation test. Then one pre-registered run on BTC/ETH 1h (not 5m, for costs):
   (a) fade at prior-day high below the daily trend vs (b) break-and-continue, untuned, with the
   intraday-momentum literature written down as the prior (it predicts continuation, so the video's fade
   should lose). DSR N is already in the thousands; "no edge" is the likely and correct result. Worth doing
   only if the overlay makes the levels look real.

3. **Not adopted:** fixed R:R targets as a rule, the 3-of-5-days scaling gate, the futures/ES drift claim,
   counter-trend scalping, and dollar-based stops. Phase 20 Part E2 already covers the sizing lessons
   (small fixed fraction, structural stop → stake, slow step-ups).

## Sources

1. Hudson & Urquhart, technical trading and cryptocurrencies: https://centaur.reading.ac.uk/85715/8/Hudson-Urquhart2019_Article_TechnicalTradingAndCryptocurre.pdf ; https://ideas.repec.org/a/spr/annopr/v297y2021i1d10.1007_s10479-019-03357-1.html
2. Grobys, Ahmed & Sapkota (2020): https://www.sciencedirect.com/science/article/pii/S1544612319308852
3. Osler (2000), support/resistance in FX: https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html
4. Osler (2003), stop-loss clustering: https://onlinelibrary.wiley.com/doi/abs/10.1111/1540-6261.00588
5. Gao, Han, Li & Zhou (2018), market intraday momentum: https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351
6. Shen et al. (2022), BTC intraday momentum: https://onlinelibrary.wiley.com/doi/abs/10.1111/fire.12290
7. Crypto momentum vs reversal (2022): https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833
8. Zarattini, Aziz & Barbon, SPY intraday momentum: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172
9. Cooper, Cliff & Gulen (2008), overnight vs intraday: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1004081
10. Lou, Polk & Skouras (2019), "A tug of war": https://www.sciencedirect.com/science/article/abs/pii/S0304405X19300650
11. Quantpedia, Bitcoin intraday seasonality: https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/
12. Concretum, BTC intraday seasonality: https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/

Results quoted here are historical and not forward-looking. Nothing in this report is a trading recommendation.
