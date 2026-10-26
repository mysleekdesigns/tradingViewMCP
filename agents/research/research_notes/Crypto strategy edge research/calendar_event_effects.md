# Calendar, trading-session, and scheduled-event effects in BTC and ETH returns

Scope: time-of-day, day-of-week, month, turn-of-month, macro releases (FOMC, CPI), options expiry, CME gaps, halving, and spot ETF flows. For each: effect size, robustness, whether it survived publication, and whether it can be traded long/flat on spot after about 0.3% round-trip costs. Researched 2026-10-26. All web content was treated as untrusted data. I saw no prompt-injection attempts. Exchange and blog sources (Phemex, KuCoin, CoinGlass, QuantifiedStrategies) are flagged where they are used and given little weight.

## 1. Intraday and time-of-day effects (sessions, 00:00 UTC, funding timestamps, US equity open/close)

### Takeaway
The best-known intraday effect is "hold BTC 21:00–23:00 UTC" (Padyšák & Vojtko / Quantpedia). It was 33–41% a year gross in-sample (2015–2023) with no transaction costs reported. It means 365 round trips a year, and the mean return per window is roughly 0.1–0.15%, below a 0.3% round-trip cost, so long/flat on spot it loses money after costs. Volume and volatility patterns tied to the NY session, the benchmark fixing window, and quarter-hour marks are strong and stable. Return predictability from them is tiny (single-digit basis points) or unreplicated after 2023.

### Cited Findings
- Padyšák & Vojtko (SSRN 4081000, 2022). Gemini hourly BTC, 9 Oct 2015 – 3 Feb 2022. The returns in the 22:00 and 23:00 UTC hours were the most economically and statistically significant (5% level). No hour was significantly negative. Rule: buy 21:00 UTC, sell 23:00 UTC. Annualized return 33%, volatility 20.93%, max drawdown -22.45% — [Quantpedia article](https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/)
- Quantpedia's strategy profile for the same paper reports Sharpe 1.58, max drawdown **-34.04%** (the article above says -22.45%), "Transaction costs: Not reported", and a backtest period of 2015–2021. It describes the entry as 22:00 UTC with a two-hour hold, not 21:00–23:00. The internal inconsistencies are worth noting — [Quantpedia strategy page](https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin)
- Split by NYSE session: on NYSE-open days, BTC gains accrue mostly "overnight" (outside 15:00–21:00 UTC), and the intraday part is small and volatile. On weekends and holidays the pattern reverses — [Quantpedia](https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/); [PapersWithBacktest summary](https://paperswithbacktest.com/blog/bitcoin-never-sleeps-exploiting-seasonality)
- Quantpedia's own extension to 30 Jun 2023 again finds 22:00 and 23:00 UTC significantly higher. The strategy "had a rough period in 2022 and 2023 and experienced a drawdown". Annualized 40.64%, max drawdown -22.7%, Calmar 1.79. Conditioning on high volatility gives 37.26% and Calmar 1.97. Friday is the best weekday for the window, and uptrend days are stronger. This is the original authors re-testing a mostly overlapping sample, with several new conditioning splits — [Quantpedia, The Seasonality of Bitcoin](https://quantpedia.com/the-seasonality-of-bitcoin/)
- A low-quality blog backtest puts 22:00 UTC as BTC's best hour at about 0.07% average return — [QuantifiedStrategies (snippet only, low weight)](https://www.quantifiedstrategies.com/bitcoin-intraday-seasonality-trading-strategy-backtest-results/)
- Aalipour, Mehdian & Rezvanian (Finance Research Letters 2026, 110634; SSRN 6776934). Hourly data for BTC, ETH, USDT, BNB and 8 less-active coins. Apparent day-of-week effects sit in a few hours: BTC's Monday effect at 06:00 and 18:00 UTC (0.1103% average at 06:00), ETH's Wednesday effect at 01:00, 02:00, 07:00 and 20:00 UTC (0.12% at 20:00). A joint test across all weekdays found no reliable weekday pattern for any coin. The authors conclude the inefficiencies are "limited and short-lived" — [securities.io summary of the FRL paper](https://www.securities.io/crypto-day-of-the-week-effects-intraday-patterns/); [Quantpedia abstract](https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin)
- Jahanshahloo, Corbet & Oxley (FRL 2022, 49:103101). NYSE trading sessions lead Bitcoin trading activity on-chain and on centralized exchanges. The effects strengthened over time, and activity is "at best, 12/5" — [Quantpedia abstract listing](https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin)
- Kaiko (June 2024): after the US spot ETFs launched, volume concentrated in US hours. The 3–4 pm NY benchmark fixing window (20:00–21:00 UTC in winter) took 6.7% of all volume in Q1 2024, up from 4.5% in Q4 2023. 15:00 UTC was the busiest weekday hour and 20:00–21:00 UTC the second — [Kaiko](https://www.kaiko.com/resources/btc-etfs-impact-on-spot-market-structure)
- Wang (SSRN 7526338, Sept 2026) uses DST clock changes. BTC and ETH trading activity rises in the UTC window aligned with the New York open, so session effects follow New York local time, not a fixed UTC hour — [ML-Quant summary](https://www.ml-quant.com/papers/ssrn/7526338/)
- Kim & Hansen (arXiv 2607.09426, Aug 2026). Binance perps for BTC, ETH, XRP, SOL, DOGE and ADA, Jan 2021 – Oct 2024. Volatility and volume spike at the 1-, 5- and 15-minute marks and the top of the hour, driven by algorithms. Order imbalance at quarter-hour openings forecasts 4–12 hour returns. An interquartile move in the lagged-flow component adds about 5–6 bps, and the public component 9.8–16.9 bps at 8–12 hours. Regression R² is 0.05–0.18% — [arXiv](https://arxiv.org/html/2607.09426v1)
- Concretum Group (2026 blog): a volatility-targeted, long-short, high-frequency trend ensemble had gross Sharpe of about 1.6 over 2018–2025, against about 0.8 for vol-targeted long BTC. Its returns cluster from Sunday about 19:00 NY time through Monday (the "Monday Asia Open Effect"). The effect appears mainly after mid-2020. This is the seasonality of *trend-following P&L*, not of passive returns, and it needs shorting and leverage — [Concretum](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/)
- Perp funding on Binance, Bybit and OKX settles at 00:00, 08:00 and 16:00 UTC. Exchanges shorten the interval when the funding cap binds — [DEV Community](https://dev.to/hmwork_2c698b7158d3/funding-settles-every-8-hours-but-four-of-these-five-exchanges-have-no-8-hour-candle-1plc); [Coincub](https://coincub.com/blog/perpetual-futures-funding-rates-78-of-a-quarter-at-0-01/)

### Inferences
- Cost arithmetic for 21:00–23:00 UTC long/flat: 365 round trips × 0.3% ≈ **109.5% a year in cost drag**, against 33–41% a year gross. If the average window return is about 0.1–0.15% (≈ 33–41% / 365, consistent with the ~0.07%/hour blog figure), every trade loses about 0.15–0.2% net. A maker-only, near-zero-fee version would need fills you cannot assume at the bar open. **Not tradeable long/flat at the reader's cost level.**
- The 22:00/23:00 hours were picked in-sample from 24 candidates and then conditioned further (weekday, trend, volatility). Treat it as one hypothesis among at least 24×7 that were looked at. The deflated-Sharpe bar should count that search.
- The window sits just after the US cash close and the 20:00–21:00 UTC benchmark fix (winter). If a mechanism exists, it is likely tied to NY-time events (ETF NAV fixing, the equity close). A test should define the window in America/New_York time, which moves by an hour across DST, not in fixed UTC (supported by Wang 2026 and Kaiko).
- The quarter-hour order-flow signal is 5–17 bps at best, an order of magnitude below 30 bps cost, and needs trade-level (aggTrades) data. It is not a kline feature.
- With a next-bar-open fill rule on 1h bars, a "buy at 21:00" signal has to be generated at the 20:00 bar's close, and both entry and exit pay the spread. Nothing reviewed suggests the edge survives that.

### Gaps
- No independent post-2023 replication of the 21:00–23:00 UTC effect, especially after the January 2024 ETF launch, was found. Quantpedia's extension only reaches June 2023.
- No academic study was found of spot returns around perp funding timestamps (00/08/16 UTC). Only exchange documentation exists. The effect is unknown.
- No source with effect sizes was found for returns (rather than volume) around the US equity open (13:30/14:30 UTC) or around 00:00 UTC for BTC or ETH after 2022.
- ETH-specific hourly return seasonality has no peer-reviewed evidence after 2022 beyond Aalipour et al.'s few hours. There is an unreviewed GitHub heatmap project ([jsholmes7](https://github.com/jsholmes7/ETH-seasonality-analysis)).

## 2. Day-of-week, weekend, month-of-year, turn-of-the-month, holidays

### Takeaway
The early "Bitcoin Monday effect" (2018–2019 papers) did not persist after 2015 in Mueller's 2024 replication. Weekend and weekday mean returns do not differ through 2024. The one robust weekend fact is **lower volume and volatility**, not a return premium. Month-of-year claims rest on about 10–13 yearly observations. Turn-of-month evidence uses pre-2022 data and an in-sample window search. A pre-holiday drift only appears when combined with a 10-day-high momentum filter.

### Cited Findings
- Caporale & Plastun (FRL 2019): most coins showed no day-of-week effect. BTC was the exception, with significantly higher Monday returns. **Older data only** — [ScienceDirect abstract](https://www.sciencedirect.com/science/article/pii/S1544612318304240)
- Mueller (FRL 2024, vol. 64, 105429): "The evidence on seasonality in cryptocurrency returns is not robust. Although the positive Monday effect for Bitcoin is internally valid, it does not persist in data after 2015." No robust return anomalies, but lower weekend trading activity across 500 coins. The cross-sectional Monday effect is typically negative, with wide confidence intervals. January and Halloween effects are not reliable for BTC, including under bootstrap tests, on 2013–2024 data — [EconPapers abstract](https://econpapers.repec.org/article/eeefinlet/v_3a64_3ay_3a2024_3ai_3ac_3as1544612324004598.htm); [Harbourfront Quant summary](https://harbourfrontquant.substack.com/p/calendar-anomalies-in-digital-assets)
- Xu (PBES 2025): daily BTC-USD, Sep 2014 – Jan 2024, Welch tests and HAC OLS with month fixed effects. **No weekend–weekday gap in average returns** in the full sample or the 2016–19, 2020–23 and early-2024 subsamples. Volatility and volume are lower at weekends. Replication files are provided. Low-tier journal and single author, so treat as corroborating only — [PBES](https://ojs.bbwpublisher.com/index.php/PBES/article/view/11691)
- Kaiko: BTC's weekend share of volume fell from 28% (2019) to 16% (2024), an all-time low, and fell faster after the ETFs launched — [Kaiko](https://www.kaiko.com/resources/btc-etfs-impact-on-spot-market-structure); [The Block](https://www.theblock.co/post/302705/bitcoin-sees-all-time-low-weekend-trading-levels-following-etf-approvals-kaiko)
- Aalipour et al. (FRL 2026): DoW effects are a handful of specific hours, with no joint weekday significance (details in section 1) — [securities.io](https://www.securities.io/crypto-day-of-the-week-effects-intraday-patterns/)
- Quantpedia (Beluská & Vojtko, 2024) found no significant daily seasonality in BTC 10-day MIN/MAX strategies over 2015–2024 and called the weekday differences "probably just a random coincidence" — [Quantpedia](https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/)
- Turn of the month: Vasileiou (IJBAF 2023; SSRN 4230561) uses BTC and ETH from 31 Dec 2017 to 31 Dec 2021. Instead of the standard (-1,+3) window, it runs "an optimization algorithm which tests several 4-day intramonth periods" and reports that a TOM effect exists, with leveraged TOM positions beating buy-and-hold. **Pre-2022 only, and the window is chosen in-sample** — [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4230561); [ResearchGate](https://www.researchgate.net/publication/363808047_Is_the_Turn_of_the_Month_an_anomaly_on_which_an_investment_strategy_could_be_based_Evidence_from_BitCoin_and_Ethereum)
- A second TOM study (Managerial Finance) covers BTC, ETH and LTC from Aug 2015 to Aug 2021 with dummy regressions. **Pre-2022 only.** Results could not be retrieved — [Emerald](https://emerald.com/insight/content/doi/10.1108/mf-02-2022-0084/full/html?skipTracking=true)
- Month of year (aggregator, low weight): October was green in 10 of 13 years (median +14.6%), and February 10 of 13 (median +12.2%), over 2013–2025 — [CoinGlass](https://www.coinglass.com/today)
- Pre-holiday (Quantpedia, 2025): BTC futures and BITO, 2018 – Jun 2025, US holidays. A plain D-1 to D+1 holiday hold was "nothing extraordinary… flat for most of the time". Adding an N-day-high filter (N ∈ {5,10,20}, window chosen from D-5..D+5) gave Sharpe above 0.6 and Calmar above 0.7 — [Quantpedia](https://quantpedia.com/surprisingly-profitable-pre-holiday-drift-signal-for-bitcoin/)

### Inferences
- A weekday-only long/flat rule means about 52 round trips a year, or 15.6% a year in cost drag at 0.3%. No study since 2015 shows a mean daily return difference that large.
- Month-of-year has about 12 observations per month across the whole BTC history. A +14.6% median with BTC's monthly volatility is statistically meaningless (see section 6).
- TOM (12 trades a year, 3.6% drag) is the cheapest calendar trade, but the only BTC/ETH evidence picks its window by optimization on 2018–2021. Any test should use the textbook (-1,+3) window fixed in advance.
- The pre-holiday result depends on a momentum filter. That is mostly the 10-day-high trend rule the reader's lab has already tested, applied on about 10 holidays a year with three Ns and several windows tried.
- Lower weekend volume and volatility could be used for **volatility forecasting or sizing**, not for direction.

### Gaps
- Effect sizes and test statistics for "Retail weekends, institutional weekdays" (2026) and "Calendar effects on returns, volatility and higher moments" (2025) were not retrieved. ScienceDirect robots.txt blocked the fetch and only snippets were seen — [link 1](https://www.sciencedirect.com/science/article/abs/pii/S1062976926001407), [link 2](https://www.sciencedirect.com/science/article/pii/S1062940825000816)
- No post-2022 TOM study for BTC or ETH with a pre-specified window was found.

## 3. Scheduled macro events and derivatives calendar (FOMC, CPI, options expiry, CME gaps/expiry)

### Takeaway
FOMC and CPI reliably raise **volatility**, but show no consistent **directional drift** in 2020–2025 samples. The CPI reaction depends on the *surprise sign*, is concentrated in the first hour, and its strength changes with the regime (strong 2022–23, near zero 2024–25, back in 2026). Deribit options expiry produces a documented ~1-hour dip and ~90-minute reversal around 08:00 UTC, but only on top-decile at-the-money open-interest days. The CME weekend gap no longer exists: CME moved to 24/7 trading on 29 May 2026.

### Cited Findings
- FOMC, older sample: BTC rose about 0.96% the day before an announcement and fell about 1% on the day, against +0.26% on non-announcement days (Pyo & Lee, FRL 2020). **Pre-2020 data, flag as older-only** — [ScienceDirect abstract](https://www.sciencedirect.com/science/article/abs/pii/S154461231930159X)
- An unexpected tightening of 1 bp in the 2-year yield goes with about +0.25% for BTC over one day, with "a large drift in Bitcoin price following the FOMC announcement" (Research in International Business and Finance, 2022). Only the snippet was seen. The sign is counter-intuitive and the sample ends before 2022 — [ScienceDirect abstract](https://www.sciencedirect.com/science/article/abs/pii/S027553192200099X)
- FRL 2026, "Scheduled FOMC statements and intraday macro event risk in cryptocurrency markets": in the first hour after the statement (14:00–15:00 ET), mean absolute return rises from 0.66% to 1.25% for BTC (p < 0.001) and from 0.85% to 1.50% for ETH (p = 0.0015). That is a volatility effect, not a directional one — [ScienceDirect abstract](https://www.sciencedirect.com/science/article/abs/pii/S1544612326006021)
- KSE master's thesis (Nazaruk 2025), BTC 2020–2025, daily event study against an S&P 500 market model: "Bitcoin's volatility rises around U.S. macro announcements, while returns show no consistent drift. For CPI, volatility builds up before the release…" A thesis, so moderate weight — [KSE PDF](https://kse.ua/wp-content/uploads/2026/05/illia-nazaruk_268722_assignsubmission_file_nazaruk_final_thesis.pdf)
- Exchange and news claim (promotional, small N): BTC had a negative 48-hour return after 8 of 9 FOMC announcements since May 2025 — [Phemex](https://phemex.com/blogs/btc-drop-fomc-history-analysis); [Stocktwits](https://stocktwits.com/news-articles/markets/cryptocurrency/bitcoin-price-fed-fomc-meeting-history-post-fed-selloff-crypto-analysts-split/cZNRtKKRJRq)
- CPI (Block Scholes, Jul 2026): 2020–2026, a 1 pp upside CPI surprise goes with about -5.5% BTC in the next hour, R² 25%. A 24-release rolling window peaked Dec 2021 – Nov 2023 at β ≈ -13%/pp with R² 52%. The slope and R² "collapsed" through late 2024 and 2025, then came back in 2026 (12-release 1-hour R² near 80%). The effect is "specific to the immediate, one-hour horizon; and not the longer 4-to-24-hour periods". Sensitivity to non-farm payrolls has faded — [Block Scholes](https://www.blockscholes.com/institutional-research/is-bitcoin-showing-greater-sensitivity-to-us-cpi-releases-again)
- Promotional claim, unverified: below-consensus CPI prints averaged +2.8% to +5.8% for BTC on release day over two years — [Phemex](https://phemex.com/blogs/cpi-report-inflation-bitcoin)
- Options expiry: Weiss et al., "Bitcoin option expiration, gamma exposure, and intraday price reversals" (FRL vol. 107, Sept 2026, 110340). Deribit data for 2021–2023, with about 14,000 contracts and about $500M notional expiring on an average day. "A downward price move is observed for the top decile of open interest starting about one hour before option expiration. The price then reverses and stabilizes within the next 90 min around the initial 7 am price level." The effect is significant only above the 90th percentile of at-the-money open interest, and strongest when cumulative gamma exposure is negative. Implied wealth transfers are about $50M a year — [securities.io summary](https://www.securities.io/bitcoin-options-expiration-spot-price-reversals/); [ScienceDirect abstract](https://www.sciencedirect.com/science/article/pii/S1544612326008688)
- Deribit settles at 08:00 UTC on a 30-minute average of its BTC index — [24/7 Wall St.](https://247wallst.com/investing/cryptocurrency/2026/10/16/what-happens-to-bitcoin-when-billions-in-options-expire/); [CoinDesk](https://www.coindesk.com/markets/2026/09/23/bitcoin-s-usd16-billion-quarterly-options-settlement-arrives-with-a-call-heavy-book)
- CME: from 29 May 2026, CME BTC and ETH futures and options trade 24/7 on Globex, with only a 2-hour Saturday maintenance pause (03:00–05:00 UTC). "The long-standing CME weekend gap has effectively disappeared." IBIT options open interest (about $27–30B) dwarfs CME BTC options (about $0.8–0.9B) — [CoinDesk](https://www.coindesk.com/markets/2026/05/28/bitcoin-s-famous-cme-gaps-are-about-to-disappear-though-three-remain-unresolved)
- The often-quoted "about 77% of CME gaps eventually fill" has no defined horizon and comes from an exchange's marketing content. Unverifiable — [Phemex Academy](https://phemex.com/academy/cme-futures-gap)

### Inferences
- FOMC (8 a year) and CPI (12 a year) together are about 20 events a year, or about 6% a year in cost drag for a long/flat event hold at 0.3%. The 2020–2025 evidence points to volatility without a sign, so a direction-agnostic long/flat rule has no expected edge. The "sell the news" streak since May 2025 is 9 observations of a regime-dependent pattern.
- A CPI rule ("go long after a downside surprise") needs (a) the consensus figure as a point-in-time input, which is not in kline data, and (b) entry after the 08:30 ET (12:30/13:30 UTC) release. With next-bar-open fills on 1h bars, the entry is the 13:00/14:00 UTC open, after much of the first-hour move, and Block Scholes finds no effect at 4–24 hours. **Probably not capturable on 1h/4h spot bars.** The beta also changes with the macro regime.
- Options expiry: the documented move is within about 2.5 hours of 08:00 UTC and only on top-decile OI days (roughly 36 a year if expiries are daily). It requires Deribit OI data at a point in time and sub-hour execution. With a 0.3% round trip and an unreported magnitude, it is not a long/flat-spot candidate. The finding is more useful as a "don't trust 07:00–09:30 UTC signals on big expiry days" filter.
- The CME gap is structurally gone after May 2026, so any backtest of it only describes a market that no longer exists. Exclude it.

### Gaps
- Directional FOMC drift estimates with t-stats for 2022–2026 were not found in a peer-reviewed source. The 2026 FRL paper reports absolute returns only, as far as the abstract shows.
- The magnitude (bps) of the options-expiry reversal and whether it survives costs were not in the material retrieved. The full text was blocked (ScienceDirect robots.txt).
- No evidence was found on returns around CME futures expiry (last Friday of the month, 16:00 London) for BTC or ETH spot.
- ETH-specific CPI/FOMC directional effects: only the FRL 2026 volatility numbers were found.

## 4. Halving cycles and spot ETF flows (BTC since Jan 2024, ETH since Jul 2024)

### Takeaway
Halving "effects" rest on 4 events, the latest cycle broke the pattern, and two event studies find *negative* abnormal returns around halving dates. No statistical power. For ETF flows, prior returns predict flows strongly and flows predict next-day returns weakly. One paper finds NW t = 3.12, but a timestamp-corrected VAR finds only "marginal" predictability. Publication timing of the flow data creates a real look-ahead risk.

### Cited Findings
- Liu, Zhao, Li & Wang (Pacific-Basin Finance Journal 2025, vol. 94, 102913): the cumulative average abnormal return around halving dates is negative (a "scarcity narrative curse"), driven by attention-driven buying before the event and later reversal. It is more negative in the Bitcoin ecosystem and for first-time halvings — [IDEAS/RePEc](https://ideas.repec.org/a/eee/pacfin/v94y2025ics0927538x25002501.html)
- Lim (SSRN 6589402): the cycle-four (2024) halving CAR over [-30,+90] is -41.5% (t = -1.86), and over [-30,+365] it is -19.6% (t = -0.49), against cycle three's +172.2% over the same long window (t = 2.34). The paper calls it a structural break — [SSRN snippet](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6589402)
- Wells Fargo Investment Institute: cycle gains fell from about 92x (2012 cycle) to under 2x (current cycle), and peak-to-trough declines from about 84% to 53% — [Wells Fargo](https://www.wellsfargoadvisors.com/research-analysis/reports/digital-assets/bitcoin-halving-cycle.htm)
- Lim (SSRN 6592830): daily spot BTC ETF flows explain 21% of contemporaneous daily return variation (t = 8.99) and predict next-day returns (Newey-West t = 3.12). Granger tests show feedback both ways (returns→flows β = 0.47, t = 9.75) — [SSRN snippet](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6592830)
- Fabus, Vartiak, Stalmasekova & Kicova (SSRN 7545019, Sept 2026) correct the timestamp mismatch between US ETF sessions and 24/7 BTC. "In an information-consistent VAR, lagged returns predict flows, whereas lagged flows predict returns only marginally." The flow→return coefficient does not differ between the cash-creation and in-kind regimes. A summary reports same-day flows predicting next-day returns at 1.67 pp per $1B of inflow — [SSRN snippet](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7545019); [ML-Quant summary](https://www.ml-quant.com/papers/ssrn/7545019/)
- DWF Labs, as reported second-hand via Cointelegraph: over 694 trading days since January 2024, daily ETF flows correlate 0.54 with BTC's previous 5 sessions and 0.03 with the next 5. "ETF flows confirm moves; they don't predict them." — [iTechGuides summary](https://www.itechguides.com/are-bitcoin-etf-buyers-trend-followers-not-setters-dwf-labs-says-flows-react-to-price/)
- Concordia thesis (Jan 2024 – Dec 2025, 494 trading days) runs VAR/Granger tests of flows on returns. The result was not retrieved — [Concordia Spectrum](https://spectrum.library.concordia.ca/id/eprint/997714/)
- Kaiko: ETF creations and redemptions are struck on benchmark prices from 3–4 pm NY, which pulls spot volume into that window — [Kaiko](https://www.kaiko.com/resources/btc-etfs-impact-on-spot-market-structure)

### Inferences
- Halving: 4 events. Even a large CAR cannot reach significance (cycle four's t was -1.86, the wrong sign). Not a testable hypothesis in a backtest lab, and any halving-timed rule is effectively one trade every 4 years.
- ETF flows: "same-day flows predict next-day returns" is only usable if day-t flow is known before the next-day entry. Issuer flow totals usually come out after the US close or the next morning (inference, not verified here), so the honest entry is at least t+1 after publication. Fabus et al.'s timestamp-corrected result ("marginal") is the relevant one for a no-look-ahead lab. The signal is also not in Binance kline data and would need a point-in-time flow archive with publication timestamps.
- At about 1.67 pp per $1B, only very large flow days (top decile) would clear a 0.3% cost. That gives maybe a few dozen trades a year over a 2.75-year history, so statistical power is low (section 6).

### Gaps
- No academic study was found on whether **ETH** spot ETF flows (since Jul 2024) predict ETH returns. Only flow dashboards ([Farside](https://farside.co.uk/eth/), [CoinGlass](https://www.coinglass.com/etf/ethereum)) were found.
- The exact publication times of daily ETF flows (issuer websites versus Farside) were not checked. They matter for a point-in-time design.
- The MDPI 2025 paper on 2024 halving CARs (JRFM 18/5/242) was blocked (403).

## 5. Which effects have weakened since documentation? Evidence of overfitting and data mining

### Takeaway
Most calendar return anomalies in BTC have either disappeared after publication (Monday effect, weekend premium, CME gap) or were never shown out of sample by independent authors (21–23 UTC, TOM). What persists, and has grown, is *activity* seasonality (NY-session volume, the fixing window, lower weekend volume, quarter-hour algorithm bursts) and *volatility* around scheduled events. Those affect risk and execution, not mean returns.

### Cited Findings
- BTC's Monday effect is "internally valid" but "does not persist in data after 2015". The earlier anomalies "should therefore be interpreted as a statistical artifact rather than an anomaly" — [EconPapers (Mueller 2024)](https://econpapers.repec.org/article/eeefinlet/v_3a64_3ay_3a2024_3ai_3ac_3as1544612324004598.htm); [Harbourfront Quant](https://harbourfrontquant.substack.com/p/calendar-anomalies-in-digital-assets)
- DoW effects come from a handful of hours. Joint weekday tests are not significant, and the inefficiencies are "limited and short-lived" — [securities.io (Aalipour et al. 2026)](https://www.securities.io/crypto-day-of-the-week-effects-intraday-patterns/)
- The 21–23 UTC strategy "had a rough period in 2022 and 2023" in the authors' own update. No independent replication was found — [Quantpedia](https://quantpedia.com/the-seasonality-of-bitcoin/)
- In Quantpedia's out-of-sample test (Feb 2022 – Aug 2024) of their 2022 trend and mean-reversion rules, the MIN (buy-the-low) leg "has not performed well… low or even negative returns", while MAX held up. This is an example of in-sample combos decaying — [Quantpedia](https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/)
- CPI sensitivity is regime-dependent. It peaked in 2022–23, collapsed in 2024–25, and returned in 2026 — [Block Scholes](https://www.blockscholes.com/institutional-research/is-bitcoin-showing-greater-sensitivity-to-us-cpi-releases-again)
- Halving abnormal returns flipped sign in cycle four (CAR -41.5% over [-30,+90]) — [SSRN 6589402](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6589402)
- The CME gap was removed by a market-structure change (24/7 trading from 29 May 2026) — [CoinDesk](https://www.coindesk.com/markets/2026/05/28/bitcoin-s-famous-cme-gaps-are-about-to-disappear-though-three-remain-unresolved)
- The Concretum "Monday Asia Open" trend seasonality *strengthened* after mid-2020. That is a counter-example, but it is for long-short trend P&L, not passive long returns — [Concretum](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/)
- Methodology warning: the TOM window was chosen by an "optimization algorithm" over several 4-day windows — [SSRN 4230561](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4230561). Hansen & Lunde (2003) proposed testing against the full set of calendar dummies to avoid data-mining bias — [cited in Quantpedia](https://quantpedia.com/surprisingly-profitable-pre-holiday-drift-signal-for-bitcoin/)
- Related base rate for the reader's lab: a pre-registered sweep of 18,630 crypto and FX technical variants found none with a Deflated Sharpe above 0.95 (SSRN 7594361) — [ML-Quant summary](https://www.ml-quant.com/papers/ssrn/7594361/)
- Activity seasonality has become *more* NY-centric since the ETFs: the fixing-window share of volume rose and the weekend share fell to 16% — [Kaiko](https://www.kaiko.com/resources/btc-etfs-impact-on-spot-market-structure)

### Inferences
- Pattern: return seasonality decays, and activity and volatility seasonality persists and strengthens as institutions arrive. The lab is more likely to find value in using session and event calendars as **regime or volatility filters** (sizing down or skipping signals in event hours) than as standalone long entries.
- Any calendar hypothesis the lab tests should pre-register one window (for example TOM = last day to day +3, NY-time session boundaries) and count all hour, day, month and event variants in the DSR's N. Searching across 24 hours × 7 days alone is 168 cells.

### Gaps
- No systematic post-publication decay study for crypto calendar anomalies (in the style of McLean & Pontiff) was found.

## 6. Trades per year, cost drag, and statistical power

### Takeaway
Calendar effects at intraday or daily frequency cost more at 0.3% per round trip than any documented edge earns. Low-frequency events (TOM, FOMC, CPI, holidays) are cheap to trade but give only 8–20 observations a year, too few to separate a plausible edge from BTC noise within the data available.

### Cited Findings
- Event counts: 8 scheduled FOMC meetings a year and monthly CPI releases — [KSE thesis](https://kse.ua/wp-content/uploads/2026/05/illia-nazaruk_268722_assignsubmission_file_nazaruk_final_thesis.pdf). Average daily Deribit expiry is about $500M notional, and the effect only appears in the top OI decile — [securities.io](https://www.securities.io/bitcoin-options-expiration-spot-price-reversals/). There have been 4 halvings to date — [Lambda Finance](https://www.lambdafin.com/articles/bitcoin-returns-by-halving-cycle)
- Documented per-window magnitudes: BTC's 06:00 UTC Monday hour averages 0.1103%, and ETH's 20:00 UTC Wednesday hour 0.12% — [securities.io](https://www.securities.io/crypto-day-of-the-week-effects-intraday-patterns/). 22:00 UTC averages about 0.07% (low-quality source) — [QuantifiedStrategies](https://www.quantifiedstrategies.com/bitcoin-intraday-seasonality-trading-strategy-backtest-results/). Quarter-hour order-flow signals are worth 5–17 bps — [arXiv 2607.09426](https://arxiv.org/html/2607.09426v1)

### Inferences
The cost table below is arithmetic at 0.3% per round trip. The power estimates assume a BTC daily return volatility of about 3–3.5%, which is an assumption and was not sourced in this research. N_needed ≈ (2σ/μ)² for t ≈ 2.

| Hypothesis | Trades/yr | Cost drag/yr at 0.3% | Edge per trade needed to break even | Power note |
|---|---|---|---|---|
| 21–23 UTC daily hold | 365 | ~110% | >0.3% per 2h window (documented ~0.1–0.15%) | Many trades, but the edge is below cost |
| Single hour-of-week cell (e.g. Mon 06:00) | 52 | ~15.6% | >0.3% per hour (documented ~0.11%) | Fails on costs |
| Weekday hold (e.g. Monday) | 52 | ~15.6% | >0.3% per day | No post-2015 evidence of any gap |
| Turn of month (-1,+3) | 12 | ~3.6% | >0.3% per 4-day window | With σ_4d ≈ 6–7% and a 1% edge, N ≈ 150–200 events ≈ 12–16 years. BTC Binance history (~2017+) is ~9 years, and the design window is shorter |
| FOMC day | 8 | ~2.4% | >0.3% | With σ ≈ 3.5% and a 1% edge, N ≈ 50 ≈ 6 years. The sign has flipped across regimes |
| CPI day (surprise-conditioned) | ~6 (one sign) | ~1.8% | >0.3% after a 1h entry delay | Effect is mostly within 1h; Block Scholes finds no 4–24h effect |
| US pre-holiday D-1..D+1 | ~10 | ~3% | >0.3% per 3-day window | Plain version "flat"; needs a momentum filter (forking paths) |
| Deribit top-decile expiry reversal | ~36 (if daily expiries) | ~11% | Unknown magnitude | Needs Deribit OI and sub-hour fills |
| ETF large-flow days | ~25–50 (top decile/quintile of ~250) | ~7.5–15% | >0.3% next-day (1.67 pp per $1B suggests only very large days) | ~2.75 years of history, and point-in-time data issues |
| Halving | 0.25 | negligible | n/a | N = 4, untestable |

- Overall: no calendar or event effect found combines (a) a post-2022 independent replication, (b) a per-trade edge clearly above 0.3%, and (c) enough events per year for the lab's walk-forward and holdout to have power. "No edge expected" is the prior for long/flat spot calendar rules. The structurally different and better-supported use is as conditioning variables (skip or size down around FOMC and CPI hours and big-expiry mornings, scale by weekend low volatility), judged by the lab on drawdown and Sharpe against vol-targeted buy-and-hold rather than on raw return.
- If the reader tests one calendar hypothesis, the cheapest honest candidate is a **pre-registered TOM (-1,+3) long/flat on BTC and ETH**: 12 trades a year, textbook window, no in-sample window search. Expect low power and probably "no edge".

### Gaps
- BTC and ETH daily and 4-day return volatility was not sourced in this research. The power figures above are illustrative under an assumed σ and should be recomputed from the lab's own design window.
- Realistic all-in costs for 2-hour holds (spread at 21:00 UTC, slippage at the bar open) were not researched.
