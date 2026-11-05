---
date: 2026-11-04
phase: 20
status: passed
---

# Phase 20 HMM ribbon picker: real-path and browser check

Run by Claude after Part D, so nothing was read from BTC before the pre-registered run. Headless Chrome 151 over CDP, a production build (`--minify false`), a throwaway server on 8790 with its own database, and `/lab` proxied to the lab on 8788. The owner's 8787 and its database were not used.

| check | result |
|---|---|
| BTCUSDT 4h, Markov source | Chip "Regime 20 · ±5%", Markov selected, request has no `model` |
| Click HMM | Chip shows "Fitting HMM, first run can take a minute or two" |
| Request | `POST /lab/regime` with `model: "hmm"`, `symbol`, `interval`, 499 of 500 bars: the forming bar is left out |
| First fit | 200 after 13.4 s, ribbon drawn at 14.1 s. Bars through the holdout and today were fitted here for the first time; the design-window fits were already cached by Part D |
| Response | `n_states` 2, `engine` `lab.registry.hmm.causal_hmm`, 499 points, 499 distinct probabilities from 0.52 to 1.0 |
| Reload | Source stays HMM (`tv.regime.source.v1`), answer in 115 ms from cache |
| Back to Markov | Request without `model`, same chip as before |
| BTCUSDT 15m with HMM saved | Falls back to Markov, HMM segment greyed, no HMM request. HMM is refused below 1h by design |
| Console | Two 404s, for the default watchlist and layout: the scratch database is empty |

**Opacity.** The last 170 real bars all have strength 0.90 to 1.00, so real data cannot show the effect, and a first correlation on them (-0.02) says nothing. To test the rendering, the page's `/lab/regime` response was rewritten to one state with probabilities alternating 1.0 and 0.5 in 10-bar blocks. The rendered ribbon's green excess was a median 81 on the 1.0 bars and 24 on the 0.5 bars, over 160 bars. Opacity follows the probability.

**Not checked.** A truly cold first fit in the browser: Part D had already filled the design-window cache. The cold case was timed earlier at 47.1 s on 15,000 synthetic bars, inside the 75 s budget. The 180 s client timeout and the busy retry are covered by `useRegime.hmm.test.ts`.

The chart showed a regime label for today's bars. That is a display, it was not used for any research decision, and both HMM overlays were already abandoned.
