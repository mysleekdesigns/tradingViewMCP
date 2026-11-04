---
date: 2026-11-04
source: https://www.youtube.com/watch?v=y_bsjZThP0o
outcome: causal HMM overlay worth one pre-registered run (Phase 20 Part D); weak prior; published sources use 2 states, not 3
---

# Claude Code regime trading bot video research

Review of AI Pathways, "How To Actually Build a Trading Bot With Claude Code (Fully Automated)" (April 2026,
34 min), first reviewed 2026-11-01, and a check of the HMM regime-allocation literature behind Phase 20. Results
are historical and not forward-looking.

> [!warning] Untrusted source
> A channel whose description promotes a paid community and a 1:1 AI trading program. The video shows no trading
> results. The transcript could not be fetched, on 2026-11-01 or on 2026-11-04. The first review used the chapter
> list and two third-party summaries, which are not preserved beyond the account in PLAN.md Phase 20; the
> 2026-11-04 attempt returned only the title and the promotional description. Everything about the video below is
> second-hand. No page contained instructions aimed at AI agents.

## 1. What the video proposes (second-hand)

> [!warning] Untrusted source text
> Second-hand account of the video, carried over from PLAN.md Phase 20. No transcript was ever obtained.
>
> - An HMM regime engine: 3 to 7 states sorted by mean return into crash, bear, neutral, bull and euphoria, using the
>   forward algorithm only, with a 3-bar stability filter.
> - Sizing by regime and volatility.
> - Walk-forward tests against buy-and-hold, the 200-day SMA and random allocation.
> - Circuit breakers: 2% daily loss cuts size; 3% daily loss or 10% drawdown closes everything; a lock file needs a
>   manual restart.
> - Correlation checks across positions.

## 2. Already covered here

| Idea | Where | Outcome |
|---|---|---|
| Walk-forward, benchmarks, random nulls | Phase 7 | Stricter here than in the video |
| Markov regime features (`regime_state`, `regime_signal`) | Run r-20261026-142915-4bdd2b, idea 13 | No edge, DD 74% |
| Volatility targeting | Run r-20261025-125744-12221d | No edge |
| Volatility and SMA200 overlays | Phase 15 | No overlay benefit |

## 3. What is new

- **Causal HMM.** Never tested, because `hmm_walk_forward_from_close` takes about 75 s per 15k-bar 4h series. Worth
  one pre-registered overlay run (Phase 20 Parts B to D, lab lock).
- **`persist(c, k)`.** The stability filter, generalised (Part C).
- **Paper circuit breakers and a lock file.** Phase 20 Part E, future paper tests only.
- **Correlation-aware evaluation.** Phase 20 Part F.
- **Named crash episodes.** Phase 20 Part G.

## 4. Rejected, and why

- **Regime as a direction signal or regime-scaled sizing in the lab.** Idea 13 killed the signal family, and sizing
  beyond 0/1 exposure needs an engine change.
- **Choosing the state count by BIC on the design window.** That tunes on the data.
- **Smoothed or Viterbi state paths.** Look-ahead. See the note on the word "smoothing" in section 5.
- **Closing all positions on a daily loss as a performance feature.** It is kept only as a paper safety control, and
  no claim is made that it adds return.
- **Live or Alpaca orders, leverage, 5-minute HMM.** Outside this project's constraints.

## 5. Literature check: published setups

The `researcher` agent found the sources through CrawlForge. It could only read abstracts, so the PDFs it saved were
read directly on 2026-11-04 (text extraction): Nystrup's thesis, Nystrup et al. 2017 (accepted
version), and Turkington's slides on the Kritzman, Page & Turkington work. The published FAJ and JPM articles are
paywalled and were **not** read. Text extraction lost some maths symbols, so the forgetting-factor figures are
cross-checked against the surrounding prose.

| Source | States | Input and frequency | Estimation and cadence | Decision rule | Costs and delay |
|---|---|---|---|---|---|
| Nystrup (2014), M.Sc. thesis, DTU, "Regime-Based Asset Allocation: Do Profitable Strategies Exist?" | **2** Gaussian out of sample. 3 and 4 tested in sample | Daily log-returns of MSCI ACWI, with JPM GBI and S&P GSCI as allocation assets. 15 years in sample, 5 out | Adaptive with exponential forgetting, λ = 0.998 (effective window 500 trading days). Rolling 1,000-day fit did worse | One-step state prediction. State changes only if confidence exceeds a **92%** threshold | 0.1% per trade, one-day delay |
| Nystrup, Madsen & Lindström (2017), J. Forecasting 36(8), doi:10.1002/for.2447 | **2** Gaussian | 21,851 daily log-returns of the S&P 500, 1928 to 2014 | Time-varying parameters, effective memory about **250** days, called "close to being the minimum" | Not an allocation study | n/a |
| Kritzman, Page & Turkington (2012), via Turkington's slides, 16 Aug 2011 | **2** per series (normal, event) | Market turbulence, inflation, growth, per series. Monthly, 1973 to 2009 (thesis's description, not the paper's) | Recalibrated each month on a **growing** window | Tilts defensively when the event regime is probable. The threshold value was not found in the slides | Break-even cost reported (thesis's description) |

What this means for Phase 20:

1. **PLAN.md Part B says "number of states (3, from the source)". The sources read do not support 3.** Every setup
   above that drives a decision uses 2. Nystrup (2014) reports that two-state strategies beat three-state ones in
   sample and argues that "it is easier to distinguish between two than three states", while saying better three-state
   strategies cannot be ruled out. Three states is the video's low end, not a literature value. Owner decision
   2026-11-04: 2 states; see `Phase 20 lab lock proposal.md`.
2. **The published persistence control is a confidence threshold, not a bar count.** Nystrup (2014) uses 92% and notes
   that it "plays the same role as the time lag in the median filter" (Bulla et al. 2011 used the median of six
   predictions). 92% was picked by that author, who remarks a slightly lower value might have been better out of
   sample, so it is a stated value and not a discovered constant. PLAN.md's example, `hmm_bear_prob > 0.5` for 3 bars,
   comes from the video.
3. **"Smoothing" wording.** Nystrup (2014) says out-of-sample work should "consider the smoothing probabilities", but
   in the context of predicting from past returns only. This project's rule stands: filtered probabilities only.
4. **Window translation is an assumption.** Daily equity memories of 250 to 500 observations do not map to 4h crypto
   bars by any published rule. It is either the same calendar span (about 1,500 to 3,000 bars) or the same observation
   count (250 to 500 bars).
5. **Hard to port.** None of these sources uses crypto or intraday bars. All are daily or monthly traditional assets.
6. **A two-state fit is mostly a volatility classifier.** This strengthens the weak prior: the overlay is likely to be
   the same hypothesis as the abandoned `vol_rank` overlay.

Not verified: the Nystrup et al. papers in Journal of Portfolio Management and Quantitative Finance (abstracts only).
A secondary quote gives a 95% confidence rule for one of them. It was not seen in the paper and is not relied on.

## 6. Recommendation

Run one pre-registered overlay, expecting "no overlay benefit", and treat that as a correct result. Everything the
owner has to decide or apply is in `agents/research/reports/Phase 20 lab lock proposal.md`.
