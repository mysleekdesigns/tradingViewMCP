# Academic evidence on crypto return predictors: what survives out of sample and after costs

Reader context: a personal Binance-spot backtesting lab that has already found **no edge** in single-symbol long/flat technical rules (MA trend, breakouts, vol-targeting, Markov regime filters, short-horizon reversal, volume climax) on 5m–4h bars after 0.3% round-trip costs. These notes focus on what is structurally different: cross-sectional (multi-coin, relative) signals, daily/weekly time-series momentum, cross-asset lead-lag, attention, carry, and the replication literature.

Source-quality notes: most journal full texts (ScienceDirect, SSRN, Cambridge PDF, the AUT/ACFR PDF) were paywalled, robots-blocked or Cloudflare-blocked for this session. Many findings below therefore come from **abstracts and search snippets of primary sources**, not full-text tables. Effect sizes quoted from secondary press (e.g. Google-search effect) are flagged. I saw no injected instructions in any fetched page. Several search results were hype or unverified backtests (flagged in the relevant Gaps).

---

## 1. Cross-sectional factors (momentum, reversal, size, liquidity, volatility, value/network, beta): which survive post-2020 data and realistic costs?

### Takeaway
Cross-sectional **momentum among large, liquid coins** is the most consistently replicated crypto factor, and a newer multi-indicator "trend factor" (CTREND, JFQA 2025) claims to survive costs in big, liquid coins. The **size premium is largely an artefact** of microcaps and survivorship. Short-term reversal is a **liquidity-provision premium concentrated in illiquid coins** (it flips to momentum in the largest coins). Value/network-adoption factors exist on paper but need on-chain data and have little post-2022 replication. Momentum is crash-prone, and one 2024 working paper finds momentum and beta no longer price returns once delisting returns are included, so even momentum is contested.

### Cited Findings
**Foundational (sample ends ≤2020, older-only):**
- Liu, Tsyvinski & Wu (J. Finance 2022): three factors (crypto market, size, momentum) capture the cross-section; ten characteristics form significant long-short strategies, and all are explained by the 3-factor model. The NBER version groups candidate predictors into size, momentum, volume and volatility. — [IDEAS/RePEc abstract](https://ideas.repec.org/a/bla/jfinan/v77y2022i2p1133-1177.html); [NBER w25882](https://www.nber.org/system/files/working_papers/w25882/w25882.pdf)
- Grobys et al. summarise the earlier literature: Liu et al. (2022) report weekly momentum payoffs of about 3% per week (2014–2020); Grobys & Sapkota (2019) found **no** momentum in monthly data 2014–2018; Shen et al. (2020) found insignificant negative weekly momentum 2013–2019. "No consensus exists on cryptocurrency momentum profits." — [Grobys, Kolari, Sandretto, Shahzad, Äijö, "Cryptocurrency momentum has (not) its moments", FMPM 2025](https://link.springer.com/article/10.1007/s11408-025-00474-9)

**Robustness / replication (samples to 2022–2023):**
- Fieberg, Günther, Poddig & Zaremba (IRFA 2024) ran 20,736 research designs over 43 sorting variables. Non-standard errors (dispersion from analyst choices) **exceed standard errors** and are larger than in equities. Nevertheless "the most prominent cryptocurrency factors, such as size and momentum, remain consistently robust across numerous specifications"; down-weighting the smallest coins reduces the dispersion. — [IDEAS/RePEc abstract](https://ideas.repec.org/a/eee/finana/v92y2024ics1057521924000383.html)
- Open Crypto Pricing (Stoeckl & Pukrop, Univ. of Liechtenstein, 2026; data 30 Dec 2013 to 11 Oct 2026, 667 weeks, survivorship-bias-free via the crypto2 package) evaluated 249,024 factor-construction variants. Against the original Liu et al. factors, **Bitcoin earns ~13%/yr alpha**, because all-coin breakpoints sit among untradeable microcaps. With breakpoints restricted to coins ≥$100M, "**the size premium vanishes, and momentum remains, next to the market, the factor that prices investable coins**." Weekly CMKT/CSIZE/CMOM series are downloadable as CSV. — [opencryptopricing.com](https://opencryptopricing.com/)
- Ammann, Burdorf, Liebi & Stöckl (WP, 3,904 coins, 2014–2021): annualised survivorship bias of 0.93% (value-weighted) and **62.19% (equal-weighted)** buy-and-hold. After correction, the size effect is confirmed but substantially overstated (snippet: by ~50%), "while **momentum and market beta show no positive relationship with returns** once delisting returns are accounted for." — [Stöckl publication page](https://www.sebastianstoeckl.com/publications/wp2022_liebi_survivorship/); [SSRN 4287573](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4287573). **Conflicts with** Fieberg et al. (2024) and Open Crypto Pricing (2026), which both find momentum robust; the difference appears to come from universe and breakpoint choices and from how delisting returns are treated.
- Grobys et al. (FMPM 2025; top-30 coins by market cap, weekly, 2016–2023): crypto momentum "is subject to severe crashes. Even a single cryptocurrency can cause insignificant momentum portfolio returns." Volatility management mitigates crashes, and momentum "appears to be a phenomenon associated with large-cap cryptocurrencies." — [Springer](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- Cong, Karolyi, Tang & Zhao (WP 2022): momentum in the **largest-cap group, reversals in other size groups**, plus strong crypto **value** and **network-adoption** premia. Value is built as on-chain ratios (total addresses / price, addresses-with-balance / price, on-chain volume / price), giving a C-5 model. — [abstract via finexpert newsletter](https://www.finexpert.info/newsletter/newsletter-april822); [factor definitions via ResearchGate snippet](https://www.researchgate.net/publication/357052966_Value_Premium_Network_Adoption_and_Factor_Pricing_of_Crypto_Assets); [ABFER paper PDF](https://abfer.org/media/abfer-events-2022/annual-conference/papers-investfin/AC22P3048_Value-Premium-Network-Adoption-and-Factor-Pricing-of-Crypto-Assets.pdf)
- Liu & Tsyvinski (RFS 2021): crypto returns load on **network (user-adoption) factors, not production-cost factors**; strong time-series momentum; attention proxies forecast returns. — [IDEAS/RePEc abstract](https://ideas.repec.org/a/oup/rfinst/v34y2021i6p2689-2727..html)

**Short-term reversal and liquidity:**
- Zaremba, Bilgin, Long, Mercik & Szczygielski (IRFA 2021; >3,600 coins, daily): coins with a low previous-day return outperform those with a high one. Daily reversal comes from illiquidity, so "the handful of largest and most tradeable coins exhibit **daily momentum rather than a reversal**." — [IDEAS/RePEc abstract](https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html)
- Bianchi, Babiak & Dickerson (JBF 2022; USD pairs on several centralised exchanges, Mar 2017 to Mar 2022): returns to liquidity provision, proxied by short-term reversal, are "primarily concentrated in trading pairs with **lower levels of market activity**", consistent with inventory-risk and adverse-selection theories. — [Lancaster research directory](https://research.lancaster-university.uk/en/publications/trading-volume-and-liquidity-provision-in-cryptocurrency-markets/)
- Xue & Zhou (JEDC 2026, abstract seen as a related-article snippet only; 319 Binance USDT pairs): past 1-hour returns negatively predict the next 1–4 hours for most coins, including BTC and ETH. — [ScienceDirect related-article snippet](https://www.sciencedirect.com/science/article/abs/pii/S0165188924000551)
- Kitron & Wengrowicz (arXiv, Aug 2026; 183 Binance pairs, strictly out-of-sample, frozen 6-month holdout): 15-minute directional reversal is significant in 90% of pairs in every coin-year since 2021, but "**the gross edge peaks near 1.3 bp per trade against a 5 bp round-trip cost**: large enough to detect, too small to clear" costs. — [arXiv 2608.21888](https://arxiv.org/abs/2608.21888)

**Newer composite / ML signals (data to 2022–2023):**
- Fieberg, Liedtke, Metko, Poddig & Zaremba, "A Trend Factor for the Cross Section of Cryptocurrency Returns" (JFQA, Nov 2025, open access; >3,000 coins): **CTREND** aggregates price and volume technical indicators across horizons using ML. It "remains robust across different subperiods, market states, and alternative research designs… **survives the impact of transaction costs and persists in big and liquid coins**", and is not subsumed by known factors. — [Cambridge Core](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178)
- Mercik, Będowska-Sójka, Karim & Zaremba (IRFA 2025; >500 major coins, 2017–2023, 40 characteristics, double sorts): the strongest interactions combine liquidity, risk and past returns. An out-of-sample long-short of top/bottom interactions has **Sharpe >1**, but low liquidity (higher costs) "can dampen trading activity and contribute to the persistence of these anomalies." — [IDEAS/RePEc](https://ideas.repec.org/a/eee/finana/v97y2025ics1057521924007415.html)
- "Machine learning and the cross-section of cryptocurrency returns" (IRFA vol. 94, 2024) is another ML paper in this line. I did not retrieve its abstract or results. — [IDEAS/RePEc](https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001765.html)
- "Taming crypto anomalies: A Lasso-type factor model" (Research in International Business and Finance, 2026) examines 49 crypto anomalies in-sample, out-of-sample and full-sample, following Liu et al. Only the abstract stub was available; the out-of-sample survival rate is not known. — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0275531926000255)

### Inferences
- For a Binance-spot lab, the testable candidate with the best evidence is **cross-sectional momentum (about 1–4 week lookback, weekly rebalance) inside a liquid universe** (e.g. the top 20–50 Binance USDT pairs by point-in-time volume or market cap). This is a different hypothesis from everything already tested: it ranks coins against each other rather than timing one coin.
- Spot-only means **long-only top-quintile vs. an equal- or value-weighted universe benchmark**, not long-short. Most published long-short Sharpes do not apply directly. The short leg of reversal and size strategies sits mostly in illiquid coins that cannot be shorted on spot.
- Size and short-term reversal should be **deprioritised**: their premia sit in microcaps and illiquid pairs that a Binance-liquid universe excludes by construction, and the 15-minute reversal evidence directly confirms the lab's own "no edge after costs" result.
- Survivorship matters: Binance's listing history is itself survivor-biased (delisted pairs disappear). The lab needs a point-in-time universe that includes delisted symbols, or results will be inflated (62% per year for equal-weighted in Ammann et al.).
- Given Fieberg et al.'s non-standard errors, any cross-sectional test should fix the construction (breakpoints, weighting, rebalance day, lookback) **before** looking and count every variant in the trial ledger and DSR N.

### Gaps
- Net-of-cost Sharpe for long-only top-quintile momentum restricted to Binance-listed liquid coins, post-2022: I found no paper reporting exactly this.
- CTREND's net-of-cost Sharpe, turnover and exact post-2022 performance: the JFQA PDF was not retrievable as text. The abstract claims cost survival but gives no numbers.
- No independent post-2022 replication found for Cong et al.'s value/network factors. They need on-chain address data, which Binance klines do not provide.
- Volatility (low-vol) and beta anomalies: no specific post-2020 crypto evidence retrieved beyond Ammann et al.'s "beta does not price" claim and Fieberg et al.'s note that factor momentum comes mainly from the size and volatility anomalies.

---

## 2. Time-series momentum at daily/weekly horizons: what horizons work, strength after 2021, vs. buy-and-hold risk-adjusted

### Takeaway
The most careful recent paper (Han, Kang & Ryu, SSRN Dec 2023; data Jan 2014 to Aug 2023; 15 bp cost; liquidation risk modelled) finds **strong time-series momentum at the market level**. A lookback of about 4 weeks (28 days) with about 5-day holding gives a Sharpe of about 1.5 vs. 0.84 for the market, while many cross-sectional momentum portfolios "yield insignificant profits" once realistic frictions are included. The horizon that works is **days to weeks on daily bars, applied to a market or diversified portfolio**, which is a different regime from the intraday 5m–4h rules the lab already rejected.

### Cited Findings
- Han, Kang & Ryu, "Time-Series and Cross-Sectional Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions": sample 26 Jan 2014 to 28 Aug 2023, **15 bps transaction cost**. "A strategy that buys the market when its look-back period return falls within the top third of the historical returns outperforms the market across a wide range of look-back and holding periods. The strategy performs best when the look-back period is **twenty-eight days and the holding period is five days**: It yields a **Sharpe ratio of 1.51, while the market portfolio yields 0.84**." — [ACFR/AUT PDF snippet](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf); [SSRN 4675565](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565)
- The same paper (summary on a document-sharing site, secondary): "many momentum portfolios are liquidated and that significant returns often yield insignificant profits when accounting for real-world factors like transaction costs." It also notes extreme events such as LUNA's +349.75% rebound on 14 May 2022 after a 99.99% loss. — [Scribd summary](https://www.scribd.com/document/950646356/Time-Series-and-Cross-Sectional-Momentum-in-the-Cryptocurrency-Market-With-IA); [ACFR PDF snippet](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- Liu & Tsyvinski (RFS 2021, sample ends ~2018, older-only): "there is a strong time-series momentum effect" in crypto. — [IDEAS/RePEc](https://ideas.repec.org/a/oup/rfinst/v34y2021i6p2689-2727..html)
- Fieberg et al. (Quant. Finance 2023; >3,900 coins, 2014–2022, 34 anomalies): **factor momentum** exists (past-winner factors beat past-loser factors), its size is similar to equities, but it mostly comes from size and volatility anomalies and "originates from price momentum." — [Bremen SuUB abstract](https://media.suub.uni-bremen.de/handle/elib/7569)
- Industry, single-asset and in-sample (low weight): Grayscale reports a BTC 50-day-MA strategy with Sharpe 1.9 vs. 1.3 for buy-and-hold, Jan 2012 to Jul 2023, with most of the gain from avoiding Q4-2021 and Q2-2022 drawdowns. — [Grayscale Research](https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals)
- Unvetted / likely overfit (flagged): an arXiv preprint claims Sharpe 2.41 and max drawdown −12.7% for "AdaptiveTrend" across 150+ pairs, 2022–2024 ([arXiv 2602.11708](https://arxiv.org/html/2602.11708v1)). A Zenodo deposit claims TSMOM Sharpe of 0.82 pre-ETF and 1.22 post-ETF ([Zenodo](https://zenodo.org/records/19671502)). Neither is peer-reviewed. Treat both as unverified.

### Inferences
- The lab's single-symbol 5m–4h rules failing is **consistent with** the literature: published TSMOM edges use daily data with multi-week lookbacks and multi-day holds, i.e. far lower turnover. At 0.3% round trip, a 5-day hold costs roughly 0.3% × 73 trades/yr ≈ 22%/yr if always flipping, which is why holding period matters more than signal design.
- Much of the published TSMOM Sharpe advantage over buy-and-hold comes from **side-stepping a few large drawdowns** (2018, 2022). That makes the result regime-dependent and fragile in a short holdout. A vol-targeted buy-and-hold benchmark, which the lab already uses, absorbs part of this advantage.
- The lab's earlier "trend-following MA" tests may already overlap with daily TSMOM. A cleaner test is a portfolio-level TSMOM on a diversified basket (equal-weighted top-N liquid coins), with a 28-day lookback and weekly rebalance fixed in advance rather than swept.

### Gaps
- Han et al.'s exact post-2021 subperiod Sharpe and their cross-sectional momentum numbers net of costs: the full text was Cloudflare-blocked.
- No peer-reviewed paper found that reports BTC- or ETH-only daily TSMOM performance separately for 2022–2025 with costs and a proper out-of-sample split.

---

## 3. Cross-asset lead-lag (BTC leading altcoins / ETH): is it tradeable after costs?

### Takeaway
There is **peer-reviewed evidence on Binance data** (Guo, Sang, Tu & Wang, JEDC 2024) that lagged returns of BTC and other coins predict altcoin returns, with an out-of-sample long-short that is profitable after costs. However, another peer-reviewed paper finds the **opposite sign** (large coins *negatively* predict others), and a 2026 working paper argues BTC→altcoin predictability does not translate into profitability. The evidence is mixed, and the effect is likely strongest in small caps at short horizons, where costs bite hardest.

### Cited Findings
- Guo, Sang, Tu & Wang, "Cross-cryptocurrency return predictability" (J. Economic Dynamics & Control, vol. 163, June 2024; **Binance data**): lagged returns of other coins significantly predict focal coins; robust to adaptive LASSO and PCA; "a long-short portfolio formed on the past returns of cryptocurrencies can generate a **sizable return out-of-sample after accounting for transaction costs**"; the mechanism is slow information diffusion and limited attention. A related snippet says BTC's lagged return "is a strong predictor for other cryptocurrencies", and lagged BTC, BNB and TRX returns positively predict altcoins. — [SMU research portal](https://smusg.elsevierpure.com/en/publications/cross-cryptocurrency-return-predictability/); [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0165188924000551); [ResearchGate snippet](https://www.researchgate.net/publication/380164431_Cross-cryptocurrency_Return_Predictability)
- **Contradicting sign:** a Journal of Empirical Finance paper (2023, "…Understanding the return cross predictability of cryptocurrencies"): "In contrast to the positive lead–lag effect for stocks, we document a **negative lead–lag effect** in the cryptocurrency market. Specifically, the large coins negatively predict the other coins." — [ScienceDirect snippet](https://www.sciencedirect.com/science/article/abs/pii/S0927539823000956) (full text robots-blocked).
- Khan (SSRN WP, Sep 2026), "Co-movement Without Leadership: Bitcoin's Regime-Level Influence on Altcoins, and Why Its Predictability Is Not Profitability": "Predictability and profitability are distinct properties, and classification metrics without economic decomposition systematically overstate cryptocurrency forecastability." — [SSRN 7485359](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7485359)
- Asia-Pacific Financial Markets (2026), high-frequency BTC→altcoin transmission: "small-cap cryptocurrencies exhibit significant delayed responses to BTC price movements", and the authors report trading strategies built on this. Only the abstract was seen; net-of-cost figures were not retrieved. — [Springer](https://link.springer.com/article/10.1007/s10690-026-09589-z)

### Inferences
- The Guo et al. result is the most relevant to this lab: it uses Binance data, is peer-reviewed, and claims out-of-sample, net-of-cost profits. But it is a **long-short cross-sectional** portfolio, and the sign conflict with the JEF paper means the lab should treat the direction as unknown and pre-register it.
- At 0.3% round trip, an hourly or intraday BTC→alt rule on spot is unlikely to clear costs (see the 1.3 bp vs. 5 bp result in section 1). A **daily** version (rank alts by lagged BTC beta-adjusted residual, hold several days) is the only plausibly cost-viable variant.

### Gaps
- Guo et al.'s sample period, rebalancing frequency, cost assumption and net Sharpe were not retrievable (paywalled).
- The JEF negative lead-lag paper's authors, sample and horizon were not retrievable.
- No evidence found on whether BTC→ETH lead-lag specifically (two very liquid assets) remains exploitable post-2022.

---

## 4. Investor attention / Google Trends / social-media predictors: robustness

### Takeaway
The headline evidence (Liu & Tsyvinski) is **older-only (to ~2018)**: Google-search attention predicted 1–2-week BTC returns. Later work is mostly weaker, inconsistent, or about volatility rather than returns. Robust, post-2022, cost-aware evidence is lacking. Treat attention as unproven.

### Cited Findings
- Liu & Tsyvinski (NBER w24877 / RFS 2021): "high investor attention predicts high future returns over **1-2 week horizons for Bitcoin**, a 1-week horizon for Ripple, and 1-, 3-, and 6-week horizons for Ethereum." — [NBER PDF snippet](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf); [IDEAS/RePEc](https://ideas.repec.org/a/oup/rfinst/v34y2021i6p2689-2727..html)
- Effect size, **secondary press only, and the two reports differ**: one reports that a 1-SD rise in "Bitcoin" Google searches predicts +2.3% over the next two weeks ([Moneycontrol, 2018](https://www.moneycontrol.com/news/business/cryptocurrency/google-search-volume-can-predict-price-movement-of-cryptocurrency-suggests-study-2828931.html)); another reports +1.84% weekly ([Bioreports, 2019](https://bioreports.net/google-trends-bitcoin-searches-a-key-price-indicator-turn-mega-bullish/)). Verify against the RFS tables before use.
- Later evidence is mixed: a ResearchGate review snippet cites Barrantes (2021) as finding that Google Trends "can predict Bitcoin values but **inconsistently**" ([ResearchGate](https://www.researchgate.net/publication/380764000_Exploring_the_Relationship_Between_Google_Trends_and_Cryptocurrency_Metrics)). Some newer work targets **volatility**, not returns (Google Trends and Bitcoin volatility forecast, 2018–2022) ([IDEAS](https://ideas.repec.org/a/nea/journl/y2024i64p118-135.html)). Transfer-entropy work links a Google-Trends uncertainty index to BTC ([arXiv 2106.07104](https://arxiv.org/pdf/2106.07104)).

### Inferences
- Google Trends data is weekly, revised, and sampled differently on each download, which creates look-ahead and reproducibility risk. It is a poor fit for this lab's no-look-ahead rules unless snapshots are archived point-in-time.
- A Binance-native attention proxy (abnormal volume or number of trades relative to its own history) is easy to compute but overlaps the lab's already-failed "volume climax" idea.

### Gaps
- No post-2022, out-of-sample, cost-adjusted test of Google-Trends or social-media signals for BTC/ETH returns found in peer-reviewed sources.
- Twitter/X- and Reddit-sentiment predictors: not covered in what I retrieved.

---

## 5. Decay over time and after publication; replication and meta-studies

### Takeaway
The crypto "factor zoo" shows the same pathologies as equities: **huge specification sensitivity** (non-standard errors larger than standard errors), **survivorship and delisting bias** that inflates size and possibly momentum, and premia concentrated in untradeable microcaps. In equities, anomaly returns fall by roughly 26% out of sample and 50–58% after publication. I found no crypto-specific post-publication decay estimate, but the robustness papers point the same way. After cleaning, the survivors are **market + momentum (in investable coins)**, perhaps CTREND and cross-coin lead-lag. Size and reversal do not survive in tradeable form.

### Cited Findings
- Equity benchmark for decay: anomalies are "26% lower out-of-sample and 58% lower post-publication" (McLean–Pontiff, via QuantPedia) ([QuantPedia](https://quantpedia.com/in-sample-vs-out-of-sample-analysis-of-trading-strategies/)). A JFQA paper: anomalies "decay by roughly 50% post-publication" ([Cambridge PDF snippet](https://www.cambridge.org/core/services/aop-cambridge-core/content/view/945133D5A3ECEEAF466AEE91551FD225/S0022109022000874a.pdf/zeroing-in-on-the-expected-returns-of-anomalies.pdf)).
- Crypto non-standard errors exceed both equity NSEs and standard errors; size and momentum are the robust exceptions. — [Fieberg et al. 2024](https://ideas.repec.org/a/eee/finana/v92y2024ics1057521924000383.html)
- The paper motivates its study with the growth of a crypto "factor zoo", citing equity evidence that many anomalies fail out of sample (Feng et al.; Hou, Xue & Zhang; Linnainmaa & Roberts; McLean & Pontiff). — [ScienceDirect snippet, Fieberg et al.](https://www.sciencedirect.com/science/article/abs/pii/S1057521924000383)
- Survivorship: a 62.19%/yr bias for equal-weighted portfolios; size overstated; momentum and beta not priced after delisting correction. — [Ammann et al.](https://www.sebastianstoeckl.com/publications/wp2022_liebi_survivorship/)
- Original LTW all-coin factors give BTC a ~13%/yr alpha; with investable breakpoints, size disappears and momentum remains. — [Open Crypto Pricing](https://opencryptopricing.com/)
- Momentum crashes: a single coin can make momentum insignificant in a top-30 universe (2016–2023). — [Grobys et al. 2025](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- A 49-anomaly in-sample vs. out-of-sample study exists (RIBAF 2026), but its survival rates were not retrieved. — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0275531926000255)

### Inferences
- When adapting a published crypto anomaly, the lab should expect roughly half (or less) of the published effect, and to lose more of it again to the move from long-short to spot long-only.
- The Open Crypto Pricing weekly factor CSV (CMKT, CSIZE, CMOM; investable, survivorship-free, updated weekly through Oct 2026) is a free external **cross-check** for any momentum result the lab produces.

### Gaps
- No crypto-specific estimate of post-publication decay (e.g., LTW momentum before vs. after 2019/2022) was found.
- The RIBAF 2026 "Taming crypto anomalies" out-of-sample results were not retrievable.

---

## 6. Realistic net-of-cost Sharpe ratios for the robust predictors

### Takeaway
Credible published figures sit around **Sharpe 1.0–1.5 gross or lightly costed**, mostly for long-short or market-timing portfolios with samples that include 2017–2021. Realistic expectations for a spot, long-only, 0.3%-round-trip implementation after 2022 are materially lower, plausibly **below 1 and possibly not distinguishable from vol-targeted buy-and-hold**. Claims above 2 come from unvetted preprints.

### Cited Findings
- TSMOM on the crypto market: Sharpe 1.51 vs. 0.84 for the market (28-day lookback, 5-day hold, 15 bp costs, 2014 to Aug 2023). — [Han, Kang & Ryu](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- Cross-sectional interaction long-short, out-of-sample: Sharpe >1 (2017–2023), with low liquidity, i.e. costs, the reason it persists. — [Mercik et al. 2025](https://ideas.repec.org/a/eee/finana/v97y2025ics1057521924007415.html)
- CTREND: "survives the impact of transaction costs" in big, liquid coins; no Sharpe figure retrieved. — [Fieberg et al., JFQA 2025](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178)
- Cross-coin lead-lag: "sizable return out-of-sample after accounting for transaction costs"; no Sharpe figure retrieved. — [Guo et al. 2024](https://smusg.elsevierpure.com/en/publications/cross-cryptocurrency-return-predictability/)
- Intraday reversal: gross 1.3 bp per trade vs. 5 bp cost, so **negative net**. — [Kitron & Wengrowicz 2026](https://arxiv.org/abs/2608.21888)
- Crypto carry (futures basis), a structurally different premium that needs futures: the basis can exceed **40% per annum** and varies a lot over time. It is driven by trend-chasing retail demand for leverage and limits to arbitrage. — [Schmeling, Schrimpf & Todorov, BIS WP 1087 (2023)](https://econpapers.repec.org/RePEc:bis:biswps:1087). Not implementable on spot alone; note it as a known return source outside the lab's spot-only scope.
- Unverified / not credible as stated: AdaptiveTrend Sharpe 2.41, 2022–2024 ([arXiv 2602.11708](https://arxiv.org/html/2602.11708v1)); Zenodo TSMOM Sharpe 1.22 post-ETF ([Zenodo](https://zenodo.org/records/19671502)).

### Inferences
- Ranking of what to test next in a Binance-spot lab, by strength of evidence and fit (my judgement):
  1. **Cross-sectional momentum, long-only, liquid point-in-time universe** (top-N by volume, including delisted), 1–4 week lookback, weekly rebalance. Best replicated. Expect crash risk; a vol-scaled variant is supported by Grobys et al.
  2. **Portfolio-level daily TSMOM** (28-day lookback, about weekly hold) on an equal-weighted liquid basket, benchmarked against vol-targeted buy-and-hold of the same basket.
  3. **CTREND-style composite trend score** (multi-horizon price and volume indicators) as a cross-sectional ranker. Peer-reviewed and cost-aware, but ML-built and so needs strict ledgering.
  4. **Daily BTC→alt lead-lag ranking.** Peer-reviewed on Binance but with a disputed sign; pre-register the direction.
  - Deprioritise: size, short-term reversal (any horizon), Google Trends, and anything intraday.
- All of these are relative or multi-asset portfolio strategies, so the lab needs multi-symbol plan support. That is a different engine path from the single-symbol long/flat rules tested so far.

### Gaps
- No paper found reporting net-of-cost Sharpe for **spot long-only** implementations at 0.3% round-trip costs on a post-2022 Binance universe; this would need to be measured in the lab.
- AQR, Man and Robeco crypto-factor notes: searches surfaced only generic factor pages, and AQR states it does not offer crypto investment. No quant-firm crypto factor research was retrieved.
