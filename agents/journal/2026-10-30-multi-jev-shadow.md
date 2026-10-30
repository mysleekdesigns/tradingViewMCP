---
run_id: "jev-shadow"
date: "2026-10-30"
outcome: "pending"
idea_ids: []
tags: [jev-shadow, paper-trade]
---

# Jev shadow judge: pre-registration (PLAN.md Phase 17)

Written before the first decision and never edited afterwards. Reports are written to
separate files: `agents/data/paper/jev-shadow/interim-*.md` before the evaluation time,
`agents/data/paper/jev-shadow/report-*.md` at or after it.

## Why forward only

An LLM judge cannot be backtested honestly: a model asked about past bars may remember
them, and a filter tuned on the same history only fits that history. This test asks
Jev only about bars that close after this entry was written.

## Watches, never steers

The shadow process recomputes each registered HC candidate's entry signal with the
lab's `compile_plan` on the same candles, warm-up window and data source as `compare`.
It writes no strategy, config, protocol or journal file of the paper bots, never opens
their dry-run databases, and cannot change their trades or verdicts. No Jev verdict is
ever acted on.

## Start and candidates

- Start (first judged fill bar open): 2026-10-30 04:00 UTC (`1793332800`)
- Interval: 4h
- Requested Jev model: `jev-latest`. The model id Jev reports serving is recorded on every
  decision row next to the requested one; the analysis pins the served model of the
  first judged decision (see the model rule below).
- Environment at genesis: python `3.12.13`, numpy `2.5.4`, pandas `3.1.0`, uv_lock_sha256 `0d77363b73381bdd467f704571b262bd76dd60a16abe8d4bc5599095ed0d3281`. The permutation stream and the snapshot
  formulas depend on numpy and pandas; a report run under other versions says so.

| Candidate | Symbol | plan_hash | protocol_sha256 | Protocol window (signal bars) |
| --- | --- | --- | --- | --- |
| hc-macd-rsi-aave-4h-exploratory | AAVEUSDT | `5116f2e8894fc747925c905652018173d3c8bb46a7603a18a47387d9da8c608c` | `7d2a195b35bbb4b229be27c82fa34268d164a484f1566030393921b58113da3f` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-btc-4h-exploratory | BTCUSDT | `724ce614e3c334ed0b0d26c6176c7f82c6302da606866985bf57f95f99342180` | `20e006dc6d6e7e37f83e65536ad82fc437a5ee14921f33bd22e539280f5e750f` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-eth-4h-exploratory | ETHUSDT | `445ebfc6213e57d8497b5f33c46d4e03a0cf91b10bc8b9e373a27e061255e7d7` | `d54166ffbeec212fa8077592799cf8aad3f25aaf4bab09c2373053b82e25daf1` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-near-4h-exploratory | NEARUSDT | `4e6a23d73f899b2e37e5efa4f095a2a5a87653ef8bfa110b9d43ff839839a0fc` | `fad112aaf87f5e0ed0d3c0529256abd32a8606d14a437f7b74baeb7e5c28d1d4` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-ondo-4h-exploratory | ONDOUSDT | `51edd19f64fb7f7ef954e5efeab375b4d7ca86b4d97d2589efff2cbd062d1e7b` | `c317569e7618d2ca6f589d41e186c54121b77ee4c92bd83bd0103d8d10c00b57` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-sol-4h-exploratory | SOLUSDT | `0fffbea85080c9d543942f484860fa462d17b66a815b43a409d33ab09ea41a5b` | `47baa26a53a054ff9345185ea6efb21bac49bc154c16c6f31485f4974c3c0423` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |
| hc-macd-rsi-sui-4h-exploratory | SUIUSDT | `04efe1413229126531f44429534ff4550f2541ce368cbe0e198dbc83814bd258` | `909f4bfab2d93b7acf3462d871fa016978925d825181bf20cdcebcaa9f81929b` | 2026-10-30 00:00 UTC to 2026-12-25 00:00 UTC (end excluded) |

A candidate is checked on signal bar t only when its protocol start <= t < its
decision end. A candidate registered after this entry is not part of this test.
The entry signal uses exactly `compare`'s closed candles from the protocol's
`warm_from`; the snapshot alone reads 300 more closed bars before it
(`snapshot_since`, frozen per candidate in the genesis row).

## Blind snapshot

Snapshot version 1, fingerprint `6c4a2d0d683e20d8e325cd13563485b156ae735c8bd4f04cd35f3787913197b6` (the formulas'
behaviour on fixed synthetic inputs; a changed formula is refused). Rounded ratios
only, from bars closed at the signal bar (no symbol, no dates, no prices or volume
levels):

- `ret_1_pct`
- `ret_6_pct`
- `ret_42_pct`
- `atr14_pct`
- `dist_ema20_pct`
- `dist_ema50_pct`
- `dist_ema200_pct`
- `rsi14`
- `vol_ratio_20`
- `btc_trend_pct`
- `btc_ret_42_pct`

Every signalled setup of one bar goes to Jev in ONE batched call; setups are labelled
s1..sk by snapshot hash, so a label does not stand for one coin.

Known limitation: the snapshot is blind to symbol, dates and prices, but the BTC
candidate's setup can be recognised from its BTC fields (its `ret_42_pct` equals
`btc_ret_42_pct` and its `dist_ema50_pct` equals `btc_trend_pct`). The fields are kept
as they are. The bars are forward (closed after this entry), so this is not look-ahead;
it only means the judge may tell the BTC setup from the others.

## Jev context and questions (verbatim)

Context:

```text
Long-only, rule-generated entry setups on crypto 4h bars. Each figure is a rounded ratio at the signal bar's close: ret_N_pct = percent return over the last N bars; atr14_pct = 14-bar average true range as a percent of close; dist_emaN_pct = percent distance of close above its N-bar EMA; rsi14 = 14-bar RSI (0-100); vol_ratio_20 = this bar's volume over the mean of the previous 20 bars; btc_trend_pct = BTC close vs its 50-bar EMA in percent; btc_ret_42_pct = BTC's 42-bar return in percent. A null figure is unavailable. No symbol, dates or prices are given. Judge each setup independently.
```

Per setup `{label}`:

- `{label}_take` (choice):

```text
For setup {label} in setups: should this long entry be taken or skipped, judged only from its figures?
```

  - criterion `take`: The figures favour taking this long entry: it is more likely to work than to fail.
  - criterion `skip`: The figures favour skipping this long entry: it is more likely to fail than to work.

- `{label}_fakeout` (noul, probability of a fake-out):

```text
For setup {label} in setups: is this long setup a fake-out that fails soon after entry (price reverses against the long within a few bars)?
```

- `{label}_conf` (score, confidence 0 to 1):

```text
For setup {label} in setups: how confident are you in your take/skip call for it, from 0 (no confidence) to 1 (fully confident)?
```

QUESTIONS_SHA256 (context + the three questions for label `L`): `f3e5d4f4ccc5e8ea3774627781ca8038260b2c1fe5884532206e6583d541883f`

## Deadline rule

The signal needs bar t's close, which is the instant the fill bar t+1 opens; the
snapshot holds only bars closed at t. Every verdict row is written before the fill
bar's open + 900 s grace. A bar that could not be judged by then is logged
`missed` (never judged later, never filled in). Jev down, a timeout or an invalid
answer is logged `unavailable`. Neither `missed` nor `unavailable` is ever counted as
a skip. The log is append-only and hash-chained; no row is edited.

## Tamper anchor

The hash chain is unkeyed, so on its own it does not stop a full re-chain. After every
append the shadow process adds the new chain head to `agents/journal/jev-shadow-heads.md`
(append-only, committed with each push). `shadow-report` and every later shadow run
check every recorded head against the log and refuse if the log was rewritten. The
final report (at or after the evaluation time) is refused unless the committed heads
file anchors the log's last row and every committed version extends the one before
it, so the anchor the final read relies on is the one in the shared git history.

## Pre-registered analysis

- Evaluation time: 2027-01-08 00:00 UTC (`1799366400`), the latest candidate
  decision end plus 14 days so trades opened near the end can
  close. Any report before it is an interim look: descriptive numbers only, no test,
  no p-value and no outcome. At or after it the cutoff is the evaluation time itself
  (later rows and trades are ignored; a trade still open then is excluded), so every
  later report is the same report.
- Metric (`mean_net_trade_return_take_minus_skip`): mean net trade return of Jev-take trades minus Jev-skip
  trades. The net return is the freqtrade dry-run trade's net return, the number
  `compare` reads. A judged decision is matched to the candidate's paper trade whose
  open time is in [fill_open, fill_open + interval); each trade is used once. Only
  closed matched trades enter an arm.
- Test (`cluster_permutation_two_sided`, cluster `fill_open`, strata `same_size_within_block`, block
  7 days, statistic `centred_within_block_diff`): two-sided stratified
  cluster permutation test of the centred within-block difference T defined below.
  One Jev call judges every setup of a bar together and the coins are
  correlated, so the trades of one fill bar are one cluster. Market regimes also
  persist across bars (trades overlap, the snapshot's BTC fields span 42 bars), so
  a plain cluster test would let one good week against one bad week look like skill.
  The test is therefore stratified: the block of a fill bar is (fill_open - start) //
  (7 x 86400 s), and label vectors are permuted as whole fill-bar
  clusters only among clusters of the same size in the same block (and shuffled
  inside the cluster). It asks whether Jev discriminates between setups of the same
  week, not whether one week beat another. The report gives the number of blocks and
  of informative strata (same-size clusters of a block holding both labels).
- Minimum sample counts only trades that carry weight in T: at least
  30 closed judged trades inside informative strata and at least 8 per arm
  there (as well as overall), else the outcome is "inconclusive", never "no information".
- Statistic T (centred within-block difference). Let S be the sum of take returns and
  d(S) = S / n_take - (total - S) / n_skip the pooled difference (n_take, n_skip and the
  total are fixed under the permutation). Under the stratified permutation every trade
  of stratum s (n_s trades, n_take_s takes, returns summing to R_s) is a take with
  probability n_take_s / n_s, so exactly E0[S] = sum over s of (n_take_s / n_s) * R_s.
  T = (d(S) - d(E0[S])) / c with the constant c = (1/n_take + 1/n_skip) * W, W = sum
  over s of n_take_s * n_skip_s / n_s. Dividing by c changes no p-value; it makes T the
  weighted mean of the within-stratum differences mean_s(take) - mean_s(skip) (weights
  n_take_s * n_skip_s / n_s), so with a single stratum T is the plain difference. Why
  centred: when weeks differ in take share or in mean return the uncentred d has a null
  mean away from 0 (a judge that takes more in good weeks), and folding |d| at 0 would
  make the two-sided test one-sided and blind to a within-week effect of the other
  sign. T is computed the same way for the observed and every permuted assignment;
  p = (1 + #{|T*| >= |T_obs|}) / (1 + n_permutations), and p = 1 when no stratum holds
  both labels. T_obs is the headline effect; the pooled difference is shown as a
  description only.
  n_permutations 10000, seed 17, alpha 0.05.
- Model rule (`first_served_model_major_minor`): every decision row logs the served model id (or
  none, when Jev names none) and the requested one. The pin is the served id of the
  first judged decision (by fill bar, then candidate) that has one, normalised to
  major.minor when it has the form `<name>-<major>.<minor>.<patch>` (`jev-1.13.0` and
  `jev-1.13.1` both pin as `jev-1.13`), else the full id. Judged decisions with a
  different pin, or with no served id, are excluded from both arms and counted
  separately in the report; the requested alias never stands in for a served id. A
  served id equal to the requested alias (when that alias is not a full
  `<name>-<major>.<minor>.<patch>` id, e.g. `jev-latest`) names no version and counts
  as no served id; the report warns loudly.
- Descriptive only (no test): the mean return of matched trades whose snapshot
  `btc_trend_pct` > 0 versus <= 0, so a trivial regime rule can be set beside Jev's
  split. A final report is refused while any candidate's dry-run DB cannot be read,
  and it prints the sha256 of its matched-trade list so a later final report can be
  checked to be the same report.
- Minimum sample: at least 30 closed judged trades and at least
  8 per arm, else the outcome is "inconclusive" and no test is run.
- Outcomes: "inconclusive", "no information" (p >= alpha), "take better" or "take
  worse" (p < alpha, by the sign of T_obs). Every one of them is a correct
  outcome and is written up as carefully as the others.
- A positive result justifies only a second, separately pre-registered forward test,
  never trading on Jev verdicts. No edge claim.

> Results are historical, not forward-looking.
