# Costs, Execution, and Portfolio Construction for Small-Account Systematic Crypto (BTC/ETH + a few large caps)

Scope: research notes only (no trading). Researched 2026-10-26. Reader's lab baseline: 0.10% taker fee + 0.05% slippage per side, about 0.30% (30 bps) per round trip. All web content was handled as untrusted data. No prompt-injection attempts were found. One fee source (tradetoolworks.com) is an independent affiliate site that contains a Binance invite link, so its numbers are used only where they agree with other sources. Binance's own fee schedule page (binance.com/en/fee/schedule) renders with JavaScript behind a WAF, so it could not be read directly. Binance announcements were read through a stealth fetch.

## Q1. Realistic all-in costs: Binance spot fees, zero-fee pairs, and measured spreads/slippage for BTC/ETH at retail size

### Takeaway
At VIP0, Binance spot charges 0.10% maker and 0.10% taker, or 0.075%/0.075% when fees are paid in BNB. BTC/FDUSD and ETH/FDUSD have had 0% maker fees for regular users since April 2024, and that is still true after the 2026-01-29 update, which restored standard taker fees for all users. For BTC/ETH at retail size ($1k–$100k), the spread and book impact on Binance are a fraction of 1 bp in normal conditions. That puts realistic taker all-in cost at about 16–22 bps per round trip, and fees make up most of it. The lab's 30 bps is a reasonably conservative stress level, not a wildly pessimistic one. Fee-free maker routes exist but bring the fill and adverse-selection problems covered in Q2.

### Cited Findings
**Fee schedule (Binance.com spot)**
- VIP0 (regular user) spot fee: 0.100% maker / 0.100% taker. Paying in BNB gives 0.075% / 0.075% (the fee page labels this "BNB 25% off"). Checked against Binance's fee page in October 2026 by a third-party guide, updated 2026-10-03. — [Toolworks, "How Binance Trading Fees Work"](https://tradetoolworks.com/en/cost/binance-fees/); agrees with [BitDegree 2026 guide](https://www.bitdegree.org/crypto/tutorials/binance-fees) and [binanceviplevelsfee.com](https://binanceviplevelsfee.com/vip-0-spot-fee.html)
- At VIP0, maker and taker cost the same, so on a new account limit orders control price but give no fee discount. VIP1 is 0.090% maker / 0.100% taker (0.0675% / 0.075% with BNB). The VIP1 row header reads "≥ 1,000,000 USD [30-day volume] and/or ≥ 5 BNB". — [Toolworks](https://tradetoolworks.com/en/cost/binance-fees/)
- USDC pairs have their own rate rows in the fee table (not reproduced by Toolworks). One secondary source says the USDC-pair taker rate "may drop to 0.095% before discounts". This is unverified and comes from a low-authority source. — [TradersUnion](https://tradersunion.com/brokers/crypto/view/binance/fees/)

**Zero-fee BTC pairs: history and current status**
- Binance ran a "Zero-Fee Bitcoin Trading" program. Its announcement was last updated 2023-11-22 "to reflect fee structure change on BTC/FDUSD and BTC/TUSD trading pairs", and it says standard fees apply after programs end. — [Binance announcement (search snippet)](https://www.binance.com/en-TR/support/announcement/updates-on-zero-fee-bitcoin-trading-busd-zero-maker-fee-promotion-be13a645cca643d28eab5b9b34f2dc36)
- From 2024-04-25, regular and VIP1 users pay **0% maker and the standard 0.100% taker** on BTC/FDUSD, ETH/FDUSD, BNB/FDUSD, DOGE/FDUSD, LINK/FDUSD, SOL/FDUSD and XRP/FDUSD. VIP2–9 and Spot Liquidity Providers kept 0%/0% "until further notice". — [Binance: Updates the FDUSD Zero Trading Fee Promotion for Regular and VIP 1 Users (2024-04-17, updated 04-19)](https://www.binance.com/en/support/announcement/binance-updates-the-fdusd-zero-trading-fee-promotion-for-regular-and-vip-1-users-3d8514bbc817423eba52fb23483bcbed)
- From **2026-01-29 00:00 UTC**, "standard taker fees will apply to **all users**" on the same FDUSD pairs. "**Zero Maker fee will remain as it is** for the aforementioned pairs." The volume now counts toward VIP tiers. — [Binance: Updates the Zero Trading Fee Promotion for All Users (published 2026-01-15)](https://www.binance.com/en/support/announcement/detail/4856a6d4e4014d4e8a5a29ec5fb44857)
- Binance also ran USDC-margined *futures* promotions (0% maker, discounted taker) through December 2025. That is not spot. — [AInvest news summary](https://www.ainvest.com/news/binance-extended-usdc-margined-fee-discounts-strategic-implications-crypto-investors-2512/) (secondary)

**Binance.US (relevant if the owner is US-based; the project notes api.binance.com returns HTTP 451)**
- Since 2026-04-22, Binance.US spot is 0% maker / 0.02% taker on all pairs for all users, with no volume tiers. Tier 0 (BNB/USD) is 0% / 0.01%. — [BusinessWire press release 2026-04-22](https://www.businesswire.com/news/home/20260422787826/en/Binance.US-Slashes-Spot-Trading-Fees-to-Near-Zero-for-All-Users); [Binance.US fees page](https://www.binance.us/fees)
- In Kaiko's 2023 spread study, Binance.US had "the highest spreads and most outliers for most USD pairs" because it "has really struggled to attract market makers". — [Kaiko, A Cheatsheet for Bid Ask Spreads (2023-06-01)](https://www.kaiko.com/resources/a-cheatsheet-for-bid-ask-spreads)

**Spreads and depth**
- Kaiko (2023): USDT pairs have the tightest spreads in crypto. "Binance leads the way with the best BTC and ETH spreads, each occurring in a tight range". Spread volatility fell in 2023 compared with 2022. USD-pair spreads sat on a scale about 3× wider than USDT pairs. Spreads widen during volatility as market makers hedge. — [Kaiko](https://www.kaiko.com/resources/a-cheatsheet-for-bid-ask-spreads)
- CoinGecko 2025 CEX liquidity report: Binance leads BTC liquidity at every depth level, with about 32% share and about $8M of depth on each side within ±$100 (about 0.1%). Within ±$10 (about 0.01%), only Binance has more than $1M on each side. Across eight exchanges, ETH median depth is $15–16M within ±$2 (about 0.1%), roughly 60–70% of BTC's. — [CoinGecko, Crypto Liquidity on CEXes 2025](https://www.coingecko.com/research/publications/crypto-liquidity-report-2025)
- Kaiko: at its October 2025 high, 1% market depth on Binance passed $600M. This is a search snippet of the PDF, and the aggregation is unclear. — [Kaiko, Liquidity Flywheel Drives Binance to 300 Million Users (PDF)](https://resources.kaiko.com/hubfs/Liquidity%20Flywheel%20Drives%20Binance%20to%20300%20Million%20Users.pdf)
- Kaiko: after FTX, 0.1% depth recovered more than 1–4% depth. Market makers increasingly concentrate liquidity close to mid. — [Kaiko, Crypto Liquidity Concentration Report](https://www.kaiko.com/resources/the-crypto-liquidity-concentration-report)
- Binance Futures spreads for SOL/DOGE/XRP/BNB are 0.5–1.0 bps, with books 5–10× deeper than Hyperliquid. For taker orders, net edge "barely degrades from $1K to $10K (+2.8 to +2.7 bps) but drops at $100K (+1.7 bps)", which implies about 1 bp of extra impact at $100k on large-cap alts. This is a pseudonymous Substack, not peer-reviewed. — [The Microstructure Lab (2026-09-30)](https://themicrostructurelab.substack.com/p/execution-simulation-for-hf-directional)
- Makarov & Schoar (JFE 2020) document large, persistent cross-exchange and cross-country arbitrage spreads, explained by idiosyncratic order flow and capital controls. That is a venue and quote-currency effect, not a per-trade cost for one venue. — [SSRN 3171204](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3171204); [CFA Digest summary](https://rpc.cfainstitute.org/research/cfa-digest/2020/10/dig-v50-n10-4)

### Inferences
- **Per-side cost decomposition for BTC/USDT taker at $1k–$50k:** fee 10 bps (7.5 with BNB) + half-spread well under 0.1 bp. CoinGecko's "> $1M within ±0.01%" implies a sub-cent-tick book at about $100k BTC. Impact is negligible because the order is under 1% of the ±0.1% depth. Fee-only round trip is therefore about 15–20 bps. The lab's extra 5 bps/side "slippage" (10 bps RT) mostly pays for something else: latency and the gap between bar t+1's open print and the price actually obtainable, plus spread widening in volatile bars. That is defensible as a buffer. For ETH it is similar; for SOL/XRP/DOGE allow 0.5–2 bps/side of spread and impact.
- **Cheapest realistic paths, in descending certainty:** (1) BNB fee payment: 30 → 25 bps RT in the lab's model, or 15 bps fee-only. (2) Binance.US for a US resident: 0% maker / 2 bps taker. Fee-only RT is about 4 bps taker/taker, but spreads are wider and books thinner, so the spread/slippage term dominates and should be measured, not assumed. (3) BTC/FDUSD or ETH/FDUSD with *maker* orders on both legs gives 0 fees, but it is subject to Q2 fill problems. It also carries FDUSD stablecoin risk (not researched here), and those books may be thinner than USDT books.
- A sensible lab cost grid: 30 bps RT as the base and stress case (current), 20 bps RT as "realistic BNB-paid taker on BTC/ETH", and 40–60 bps RT as a 2× stress. A strategy should not be promoted on the 20 bps case alone.

### Gaps
- Binance's live fee schedule page could not be read directly (JavaScript/WAF). VIP0 rates come from third-party guides that checked it in Oct 2026. The USDC-pair rows on Binance.com are unverified.
- FDUSD pair status after 2026-01-29 (whether zero maker continued through October 2026) was not confirmed beyond the January 2026 announcement. Check `binance.com/en/fee/tradingPromote`.
- No 2025–2026 Kaiko figure for Binance BTC-USDT spread in bps was retrievable. Kaiko's 2023 blog gives only relative or graphical results. No academic effective-spread study for retail-size BTC/ETH on Binance spot (2023+) was found.
- FDUSD depeg and counterparty risk was not researched.

## Q2. Maker (limit-order) vs taker execution: fill probability, adverse selection, and honest backtest modelling

### Takeaway
Maker orders save fees and spread, but their fills are adversely selected. Passive orders fill mainly when price moves against the signal and are missed when the signal is right. Empirical work on Binance shows fill rates of 8–25% for directional signals, with filled trades negative on average. A bar-based backtest that assumes a limit fills whenever the bar touches its price is biased upward. Honest modelling needs a strict trade-through rule, explicit modelling of missed trades, and a conservative-versus-optimistic bracket.

### Cited Findings
- Albers, Cucuringu, Howison & Shestopaloff ran a live trading experiment on the Binance BTC perpetual. They found "a negative correlation between a maker order's fill probability and its subsequent return conditioned on filling" (the "negative drift of maker orders"). "It is not at all easy to combine a high fill probability for maker orders with a positive subsequent return, due to one or other of adverse selection if filled, and low fill probability when subsequent returns are good." For taker orders, it is hard "to achieve short-term returns in excess of the taker fee." — [arXiv 2502.18625v2 (rev. Nov 2025)](https://arxiv.org/html/2502.18625v2); [SSRN 5074873](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5074873)
- In the same paper, a naive quoting strategy averaged −0.44 bps per round trip *net of rebates* over about 15,380 fills a day, with an annualised Sharpe of about −109. A model-filtered version was positive, but the authors warn it "is unlikely to persist with larger-than-minimum-sized orders" and assumes the best maker rebate (0.5 bp). — [arXiv 2502.18625v2](https://arxiv.org/html/2502.18625v2)
- Fill-probability estimates depend on method. Lokin & Yu (2024) report under 2% for orders by other participants at a one-tick, balanced book. Albers et al. find about 50% with their no-cancellation orders. Historical estimates are biased low by cancellations. — [arXiv 2502.18625v2](https://arxiv.org/html/2502.18625v2)
- Simulation of 6,790 real short-horizon (30–120 s) signals against L2 books gave passive fill rates of 8–25%. On Binance, taker execution earned +4.9 bps on signals where a limit order would *not* have filled and −4.3 bps on signals where it *would* have filled. "Whether the limit fills is itself an information signal about the trade's future P&L." Passive execution lost despite its 8+ bps fee and spread advantage. — [The Microstructure Lab (Substack, 2026-09-30)](https://themicrostructurelab.substack.com/p/execution-simulation-for-hf-directional) (pseudonymous; sub-minute horizons)
- Fill-model ladder for backtests. Rung 1, next-bar open, is "the minimum honest model for taker logic on bars". For limit orders on bars it "degenerates into touch-fill logic". Touch-fill (fill if low ≤ limit) awards you the fills of queue members ahead of you, and in particular the touch-and-bounce fills that are good for makers. Trade-through (fill only if price trades *through* the level) "never awards a fill you wouldn't have gotten" but is biased the other way. The bracket is: fills(trade-through) ⊆ fills(queue) ⊆ fills(touch), and "a maker strategy is deployable only if it survives at the conservative" end. In one worked example, a queue-aware model deleted 58% of touch-fill fills, disproportionately the profitable ones. Lo, MacKinlay & Zhang (2002) found first-passage (touch) models are poor proxies for actual limit executions. — [marketmaker.cc, "Fill simulation: the ladder…" (2026-07-16)](https://www.marketmaker.cc/en/blog/post/fill-simulation-partial-fills-backtest/) (vendor blog; cites the primary literature)
- Moallemi & Yuan (2016), as summarised by the same source: for large-tick instruments, the value of front-of-queue versus back-of-queue position is comparable to the half-spread. — [marketmaker.cc](https://www.marketmaker.cc/en/blog/post/fill-simulation-partial-fills-backtest/)

### Inferences
- The evidence above comes from sub-minute horizons. For 4h/daily signals, adverse selection per fill is small relative to the expected move, so the main cost of passive execution is **missed or late trades**, not per-fill drift. Missed trades are worst exactly when the signal is right: a breakout that runs away from your bid never fills.
- **Honest bar-level limit-fill model for the lab:**
  - (a) Signal at bar t's close. Post a limit at price p, for example bar t's close or the best bid at t+1's open.
  - (b) It fills on bar t+k (k ≥ 1) only if the bar's low < p − 1 tick for a buy (high > p + 1 tick for a sell). That is trade-through, not touch. The fill is at p with maker fee.
  - (c) After a time-out of K bars, either cancel (the trade is missed and recorded) or chase with a taker order at the next open, paying taker fee plus the drift already incurred.
  - (d) Report fill rate, the average forward return of filled versus unfilled signals, and P&L under both trade-through (conservative) and touch (optimistic). Promote only on the conservative leg.
  - This mirrors the bracket discipline and fits the lab's no-look-ahead rule.
- At VIP0 on Binance.com USDT pairs, maker and taker fees are equal (0.10%). Maker execution there saves only the half-spread, which is tiny for BTC/ETH, while taking on all the fill risk. Maker routing pays only where the maker fee is actually lower: FDUSD pairs (0% maker) or Binance.US (0% maker vs 2 bps taker).
- A hybrid "post, then cross after N minutes" order is a standard compromise. Its expected saving is bounded by (fee difference + half-spread) × fill rate, which is roughly 10 bps × 30–60% for BTC/FDUSD. That is far below the 30 bps that killed the 5m/15m rules.

### Gaps
- No study was found that measures limit-fill rates and adverse selection for *4h/daily* bar-level signals on crypto spot. All quantitative evidence is high-frequency.
- No public data on queue depth at the FDUSD-pair touch was found.

## Q3. Turnover reduction: no-trade bands, buffering, signal smoothing, minimum holding periods, rebalancing thresholds

### Takeaway
The best-supported finding is that trading *partially toward* a target, or not trading inside a band, improves net returns over full rebalancing whenever costs are material. Gârleanu & Pedersen show this is optimal. Novy-Marx & Velikov find a buy/hold spread (band) is the most effective cost mitigation for equity anomalies. Cutting rebalance *frequency* alone is less efficient than banding. These results come from equities and futures; no crypto-specific band study was found.

### Cited Findings
- Gârleanu & Pedersen (JF 2013): under quadratic costs and signals with different decay speeds, the optimal policy is to "aim in front of the target" and "trade partially towards the current aim". The aim portfolio weights the expected future Markowitz portfolios, and "predictors with slower mean-reversion (alpha decay) get more weight". Applied to commodity futures, it gave "superior net returns relative to more naive benchmarks." — [NBER w15205](https://www.nber.org/papers/w15205); [Wiley abstract](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12080); [RePEc](https://ideas.repec.org/a/bla/jfinan/v68y2013i6p2309-2340.html)
- Novy-Marx & Velikov (RFS 2016): "Introducing a buy/hold spread" is the most effective cost-mitigation technique they test. "Most of the anomalies that we consider with one-sided monthly turnover lower than 50% continue to generate statistically significant net spreads, at least when designed to mitigate transaction costs"; few higher-turnover anomalies do. — [SSRN 2535173](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2535173); [Oxford RFS](https://academic.oup.com/rfs/article/29/1/104/1844518)
- Novy-Marx & Velikov: staggered or less frequent rebalancing helps less than expected: "a two-thirds reduction in trading frequency generally yields roughly only a one-third reduction in turnover and transaction costs." — [NBER w20721 PDF (snippet)](https://www.nber.org/system/files/working_papers/w20721/w20721.pdf)
- NBIM compares no-trade-band rebalancing rules: letting weights drift inside a band "achieves a reduction in trading and transaction costs". — [NBIM note (snippet; PDF not parsed)](https://www.nbim.no/contentassets/8cb41f89dce345f5a6a295238f7872fb/no-trade-band-rebalancing-rules-expected-returns-and-transaction-costs.pdf)
- Dimensional: with a 1% band, rebalancing gradually over 20 trading days instead of immediately cut annualised one-way turnover from 7.1% to 4.7%. — [Dimensional, Portfolio Rebalancing: All at Once or Little by Little?](https://www.dimensional.com/us-en/insights/portfolio-rebalancing-all-at-once-or-little-by-little)
- Zarattini, Pagani & Barbon (2025) say their crypto trend paper "proposes a simple yet effective portfolio construction technique designed to reduce trading costs while preserving trend signals". The details were not on the landing page. — [Concretum landing page](https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/); [SSRN 5209907 PDF](https://papers.ssrn.com/sol3/Delivery.cfm/5209907.pdf?abstractid=5209907&mirid=1)
- An intraday Bitcoin time-series momentum study found returns "significantly positive … but still not … profitable once the returns are adjusted for transaction costs". — [Bitcoin intraday time-series momentum (accepted manuscript, Univ. of Reading repository)](https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf)

### Inferences
- **Lab techniques, in rough order of expected benefit per unit of overfitting risk:**
  - (1) Continuous target position (e.g., vol-scaled signal strength in [−1, 1] or [0, 1]) with a **no-trade band**: trade only when |target − current| > b, with b around 10–25% of full position. Fix b ex ante from costs, not by sweep.
  - (2) **Partial adjustment**, which is Gârleanu–Pedersen in spirit: new = current + λ·(target − current), with λ around 0.2–0.5 per rebalance.
  - (3) **Signal smoothing**: an EMA of the signal, or averaging across lookbacks (Q5). This lowers flip frequency without a hard minimum hold.
  - (4) **Hysteresis thresholds** (enter at +x, exit at −x instead of crossing 0).
  - (5) **Minimum holding period**: effective but blunt, and it adds a tunable parameter.
- Each is a turnover/lag trade-off. The lab should report turnover (one-way, × per year) and the cost drag (turnover × cost/side) next to Sharpe, so the improvement can be attributed.
- Banding helps only if a gross edge exists. It turns "edge destroyed by costs" into "smaller edge kept", but it cannot create edge. For the 5m/15m rules, banding at the 5m horizon will probably just make them slower versions of 1h–4h rules (see Q6 arithmetic).
- Every band, λ, or hysteresis width added and swept counts as trials in the DSR ledger. Prefer values fixed from the cost model.

### Gaps
- No crypto-specific study quantifying net Sharpe gains from no-trade bands or partial adjustment on BTC/ETH trend signals was found.
- The full-text Gârleanu–Pedersen commodity table (gross and net Sharpe by policy) was not retrieved. Only the abstract-level claims are cited.

## Q4. Volatility targeting / volatility-managed portfolios for crypto: does it beat buy-and-hold net?

### Takeaway
Volatility scaling reliably *reshapes risk* in crypto: it gives much smaller drawdowns and thinner tails. The evidence that it raises Sharpe *net and out of sample* is mixed in equities and thin in crypto. One 2017–2025 BTC/ETH/SOL test found an equal-risk vol-targeted portfolio with no trend signal beat both a multi-horizon trend strategy and BTC buy-and-hold on Sharpe (1.24 vs 0.79 vs 1.13). That test assumed costs far below Binance retail fees, and its Sharpe confidence interval is very wide. Treat vol-targeted buy-and-hold as the benchmark to beat. The lab already uses it as a benchmark.

### Cited Findings
- Moreira & Muir (JF 2017): scaling factor exposure by inverse lagged variance raises Sharpe ratios and produces alpha on market, momentum, value and other factors. They test cost assumptions of 1 bp, 10 bps, and +4 bps in high-vol episodes. — [NBER w22208](https://www.nber.org/papers/w22208); [working paper PDF](https://www.stern.nyu.edu/sites/default/files/assets/documents/Volatility%20Managed%20Portfolios.pdf)
- Cederburg, O'Doherty, Wang & Yan (JFE 2020), over 103 equity strategies: "Volatility-managed portfolios do not systematically outperform their corresponding unmanaged portfolios in direct comparisons." The spanning-regression alphas are not achievable in real time "due to structural instability". — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0304405X2030132X); [RePEc](https://ideas.repec.org/a/eee/jfinec/v138y2020i1p95-117.html)
- DeMiguel et al. note that "the gains from volatility managing individual factors do not survive out of sample or in the presence of transaction costs" (citing Cederburg et al. and Barroso & Detzel 2020). They argue for a multifactor approach instead. — [DeMiguel et al., A Multifactor Perspective on Volatility-Managed Portfolios](http://wp.lancs.ac.uk/fofi2022/files/2022/08/FoFI-2022-076-Alberto-Martin-Utrera.pdf)
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (JPM 2018, Man Group): volatility targeting raises Sharpe for *risk assets* (US equities 0.40 → 0.48–0.51, insensitive to the vol estimator), but not for non-risk assets such as commodities. It reduces extreme returns and vol-of-vol across all assets. For risk assets it "effectively introduces some momentum". — [Alpha Architect summary](https://alphaarchitect.com/volatility-targeting-improves-risk-adjusted-returns/); [paper PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf)
- Grobys, Kolari, Sandretto, Shahzad & Äijö (FMPM 2025), top-30 large-cap cryptos, weekly, 2016–2023: equal-weight crypto momentum earned 1.74%/week in Jan 2016–Jul 2020 but was "only nominally significant at the 10% level". After July 2020 it was negative and insignificant. Volatility management "is a useful tool for mitigating cryptocurrency momentum crashes" and enhances risk-adjusted returns, but "does not change the tail risk behavior". They use Barroso & Santa-Clara scaling, which avoids the look-ahead in Moreira–Muir's constant. — [Springer, open access](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- Single-author test of BTC/ETH/SOL Coinbase daily data, Jan 2017–Jun 2025, 10% vol target, estimated costs of 1–4 bps of traded value:

  | Strategy | Sharpe | Max drawdown | CAGR |
  |---|---|---|---|
  | BTC buy-and-hold | 1.13 | −83.8% | 73.5% |
  | Vol-managed equal-risk, no trend | 1.24 | −19.9% | 12.9% |
  | 1/3/12-month trend + vol scaling | 0.79 | −14.6% | 6.3% |
  | 200-day long/flat on BTC | 0.89 | −10.4% | — |

  Decomposition: vol scaling alone took BTC's max drawdown from 84% to 17%, and the trend signal added about +0.05 Sharpe on top. The bootstrap Sharpe CI for the trend strategy was about 0.1–1.5, and a 5-day delay cut its Sharpe from 0.79 to 0.60. — [Summitward, "Does Trend Following Work on Crypto?" (2026-08-24)](https://summitward.com/learn/crypto-trend-following) (blog, not peer-reviewed; its 1–4 bps cost assumption is well below Binance VIP0 fees)
- The same source cites Kim, Tse & Wald: a meaningful share of published time-series momentum performance comes from the volatility-scaling step rather than the directional signal. — [Summitward](https://summitward.com/learn/crypto-trend-following)

### Inferences
- For a lab whose 4h/daily rules "found no edge vs buy-and-hold", the relevant question is whether a rule beats **vol-targeted buy-and-hold** on a risk-adjusted, net basis. That benchmark is hard to beat because, in crypto, most of the risk-adjusted improvement attributed to trend comes from scaling.
- Vol targeting has its own turnover. Daily rescaling of a 50%-vol asset produces many small trades, so apply a no-trade band to the leverage (Q3), e.g. rebalance only if the target weight moves by more than 20% relative. At 30 bps RT, unbanded daily vol targeting could cost several percent a year.
- Spot-only, no leverage: with crypto vol of about 50–80%, a 20–40% vol target means holding only 25–80% invested. That gives up beta in bull markets, so in raw CAGR it will usually lose to buy-and-hold even when Sharpe is higher. Report both.
- Use only lagged realised vol (e.g., EWMA through bar t), with the position applied at t+1's open. Barroso & Santa-Clara style scaling avoids the full-sample constant that Moreira–Muir used.

### Gaps
- No peer-reviewed, post-2022 study of vol-managed *BTC/ETH buy-and-hold* (as opposed to momentum portfolios) with realistic 10–20 bps retail costs was found.
- No independent replication of the Summitward numbers exists.

## Q5. Diversifying across weakly correlated signals and assets (multi-horizon ensembles, multi-coin TSMOM) vs single rules

### Takeaway
Diversifying across *lookbacks* (ensembles) mainly buys robustness against parameter choice. Diversifying across *coins* gives limited benefit, because high-frequency pairwise correlation averages about 0.6. Man Group finds trend Sharpe peaks at about 10–15 coins, and fewer for faster models, before costs and illiquidity dominate. With only BTC/ETH plus a handful of large caps, expect modest gains. One sample found a plain 200-day BTC rule matched or beat a 3-coin, 3-horizon ensemble.

### Cited Findings
- Man Group (Dec 2024): crypto pairwise high-frequency correlation averages about 0.6, which is "limited diversification potential". Adding coins in order of liquidity with equal risk, net of costs and shorting constraints, "the optimal Sharpe ratio for a trend-following system occurs with around 10–15 coins". For breakout models it is about 10. "More reactive models require a smaller number of coins". Liquidity "starts to decline significantly" beyond the top 15. Man calls this a simplified static model whose Sharpe level is "probably a function of the short history". — [Man Group, "In Crypto We Trend"](https://www.man.com/insights/in-crypto-we-trend)
- Zarattini, Pagani & Barbon (2025): an ensemble of Donchian-channel models over multiple lookbacks (nine horizons per their Substack), vol-based sizing, and a rotational top-20 universe delivered "Sharpe ratio above 1.5" and "annualized alpha of 10.8% relative to Bitcoin", net of fees. Aggregating lookbacks "reduces sensitivity to any single parameter choice". — [Concretum](https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/); [Concretum Substack](https://concretumgroup.substack.com/p/catching-crypto-trends); [SSRN 5209907](https://papers.ssrn.com/sol3/Delivery.cfm/5209907.pdf?abstractid=5209907&mirid=1) (vendor research; a top-20 rotational universe needs a point-in-time construction to avoid survivorship)
- Practitioners blend 1-, 3- and 12-month horizons "because no single lookback is reliably best". Trend signal families (return momentum, price vs MA, MA crossover, breakout) perform similarly, so diversifying across simple versions is the defensible choice. In the BTC/ETH/SOL 2017–2025 test, "the boring 200-day long/flat rule on Bitcoin alone … posted a slightly better Sharpe ratio and a shallower drawdown than the diversified multi-horizon ensemble." — [Summitward](https://summitward.com/learn/crypto-trend-following)
- Grobys et al. (2025): crypto momentum profits are fragile, "even a single cryptocurrency can cause insignificant momentum portfolio returns", and earlier large momentum results may come from illiquid small caps. — [Springer](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- AQR (Hurst, Ooi, Pedersen): long-run trend evidence rests on diversified portfolios across many uncorrelated markets. — [AQR, A Century of Evidence on Trend-Following Investing](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)

### Inferences
- With N assets at average correlation ρ, the Sharpe of an equal-risk portfolio of same-Sharpe sleeves is about SR·√(N / (1 + (N−1)ρ)). At ρ = 0.6 that is about 1.12× for N = 2, 1.20× for N = 5, and caps near 1.29× as N → ∞. Adding SOL/XRP/BNB to BTC/ETH can add perhaps 10–20% Sharpe at most, and less after their wider spreads.
- Ensembles across horizons should be built *before* testing (fixed lookback set, equal weight). They are not a sweep output. Averaging k positions also smooths turnover (Q3), because the ensemble position changes in steps of 1/k.
- For the lab's DSR accounting, a pre-specified ensemble counts as one plan. Sweeping the ensemble's members re-introduces the multiple-testing problem.

### Gaps
- No peer-reviewed post-2022 study isolating the Sharpe gain from multi-horizon ensembling vs a single horizon on BTC/ETH, net of realistic costs, was found.
- The Concretum paper's cost assumptions and its cost-reduction technique were not retrieved.

## Q6. Why low-frequency (daily/weekly) may be the only viable regime at retail costs; break-even edge per trade

### Takeaway
At 30 bps per round trip, a strategy needs a gross edge of more than 30 bps per round trip just to break even. That is about 2 standard deviations of a 5-minute BTC move, about 1.1σ of a 15-minute move, about 0.28σ of a 4h move, and about 0.11σ of a daily move. Even at an optimistic 15–20 bps RT, intraday rules need implausibly high hit-rates or payoff ratios. Daily and weekly rules with 5–30 round trips a year pay only 1.5–9% a year in cost drag. That explains the reader's result: gross Sharpe above 1 on 5m/15m turning negative after costs.

### Cited Findings
- Short-horizon taker strategies on Binance survive only "on coins with sufficient gross alpha relative to the 10 bps round-trip taker fee". "If your signal's gross alpha is within 2x of the typical spread, taker execution is marginal and passive execution will be adversely selected." — [The Microstructure Lab](https://themicrostructurelab.substack.com/p/execution-simulation-for-hf-directional)
- Albers et al. stress that for taker orders it is hard "to achieve short-term returns in excess of the taker fee". — [arXiv 2502.18625v2](https://arxiv.org/html/2502.18625v2)
- Intraday Bitcoin TSMOM is significantly positive gross but unprofitable after transaction costs. — [Univ. of Reading manuscript](https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf)
- Anomalies with under 50% one-sided monthly turnover generally survive costs; higher-turnover ones generally do not. — [Novy-Marx & Velikov, SSRN 2535173](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2535173)
- Man Group: "more reactive models require a smaller number of coins to maximise the Sharpe ratio", because faster models are more cost-sensitive. — [Man Group](https://www.man.com/insights/in-crypto-we-trend)
- "Any backtest that survives only at exactly its assumed costs is a cost assumption wearing a Sharpe ratio." In one engine, a broken cost model made 9× annual turnover look nearly free. — [Summitward](https://summitward.com/learn/crypto-trend-following)

### Inferences
All of the following is my arithmetic, not a sourced figure.
- **Per-bar volatility**, assuming 50% annualised BTC vol and 365-day annualisation: σ_bar = 0.50/√(bars per year).
  - 5m (105,120 bars/yr): about 15.4 bps
  - 15m (35,040): about 26.7 bps
  - 1h (8,760): about 53 bps
  - 4h (2,190): about 107 bps
  - 1d (365): about 262 bps
  - 1w: about 693 bps

  A 30 bps round trip is therefore 1.95σ of a 5m bar but 0.11σ of a daily bar. For ETH and alts (higher vol), the ratios are somewhat better, but spreads are wider.
- **Annual cost drag** = round trips per year × RT cost. Sharpe drag ≈ cost drag / strategy vol.
  - A 5m rule averaging 3 round trips a day (about 1,100/yr) at 30 bps: about 330%/yr drag, which no gross edge survives.
  - Even at 1 round trip a day: about 110%/yr.
  - A 4h rule at 100 RT/yr: 30%/yr, which at 50% vol is a Sharpe hit of about 0.6.
  - A daily trend rule at 15 RT/yr: about 4.5%/yr, a Sharpe hit of about 0.09.
  - A weekly rule at 6 RT/yr: about 1.8%/yr.
- **Break-even per trade:** mean gross return per round trip > RT cost. With win rate w and average win/loss W/L: w·W − (1−w)·L > 0.30%. On 5m bars with a typical holding move of 1–3σ (15–45 bps), costs consume the entire expected move.
  - Cutting cost to 15 bps (BNB-paid, no extra slippage) halves the hurdle but does not change the conclusion for 5m.
  - At 15m it could matter. A 15m rule with gross Sharpe above 1 might plausibly survive at about 15 bps RT if its trades are 4+ bars long. That is a **testable hypothesis**, not a recommendation, and it must be pre-registered and DSR-counted.
- **Practical regime:** daily-to-weekly decision frequency, continuous or banded position sizing, vol scaling with banded leverage, and BTC/ETH (+ at most 3–5 liquid large caps). Benchmark against vol-targeted buy-and-hold, not just raw buy-and-hold. "No edge vs buy-and-hold" at 4h/daily is consistent with the literature: most of the improvement is risk reduction from scaling, not directional alpha.
- **Lab check:** report each candidate's break-even cost (the RT cost at which net Sharpe = 0) and require a margin of at least 2× the base cost. That is a cost-robustness criterion, in the spirit of "never relax criteria".

### Gaps
- No published, post-2022 study directly comparing net Sharpe across 5m/15m/1h/4h/1d decision frequencies on BTC/ETH spot under one cost model was found. The break-even figures above are derived from first principles with an assumed 50% annual vol and should be recomputed from the lab's realised per-bar vol.
