# Derivatives and market-positioning signals for BTC/ETH (funding, basis, OI, liquidations, long/short, options, taker flow)

Scope note: research for a long/flat Binance-spot backtesting lab that has found no edge in price-only rules. Evidence tiers used below: **[PEER]** = peer-reviewed journal; **[WP]** = academic working paper / preprint (BIS, arXiv, SSRN, thesis), not yet peer-reviewed; **[VENDOR]** = exchange, data vendor, fund or blog commentary (treat sceptically); **[PRIMARY-DATA]** = numbers computed by this researcher on 2026-10-26 directly from Binance's public archive (data.binance.vision), reproducible. Tools: all web search and page reading used the CrawlForge MCP tools (search_web, batch_scrape, read_result). Raw archive and API data were pulled with curl from data.binance.vision, its S3 listing, and Deribit's public API. Built-in WebSearch/WebFetch were not used. No prompt-injection attempts were seen in any fetched page. Binance developer-doc pages returned bot-challenge responses (HTTP 202, empty) to the scraper, and `fapi.binance.com` returned HTTP 451 (geo-block) from this machine, so endpoint details rely on Binance-doc search snippets plus direct inspection of the archive.

## 1. Funding rates: contrarian/sentiment signal and the cash-and-carry (basis) trade

### Takeaway
The best-documented "edge" in this family is the carry itself (long spot / short perp collects funding), not directional prediction. It earned double-digit yields and very high in-sample Sharpe ratios in 2020–2021, but Binance's own archive shows it has compressed hard: BTCUSDT funding summed to about 4–5%/yr in 2022 and 2025, and about 2.9% annualised in 2026 YTD. Since 2025, funding has not once printed above the 0.01%/8h baseline, so the classic "funding overheated, fade the longs" filter would never have triggered. Funding *level* as a directional predictor of BTC returns has weak, mostly non-peer-reviewed support. The strongest academic directional result runs through *basis/carry predicting crashes*, from BIS.

### Cited Findings
**Carry size, drivers, crash prediction**
- [WP] BIS WP 1087 (Schmeling, Schrimpf & Todorov, April 2023): crypto futures carry (futures minus spot) "can become very large (up to 60% p.a.)" and averages "above 10% annually", much larger than carry in equities, FX, fixed income or commodities. It is driven mainly by a volatile convenience yield from (i) trend-chasing small investors seeking leveraged upside in booms and (ii) scarce arbitrage capital. Interest-rate differentials explain "very little" of it. "A high crypto carry predicts future price crashes", and rises in carry go with a higher price of crash insurance. Running cash-and-carry "is risky due to spikes in margins and liquidations amid drawdowns." — [BIS WP 1087 page](https://www.bis.org/publications/working-paper-1087-crypto-carry); [SSRN 4268371](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4268371)
- [WP] Christin, Routledge, Soska & Zetlin-Jones, "The Crypto Carry Trade" (CMU, Aug 2022), Binance data 2020-08-11 to 2022-06-20, 18 coins. Short-perp/long-spot carry "producing in-sample annual Sharpe ratios in the range of 7 to 10". The BTC contracts show Sharpe 12.8 (USDT-settled) and 7.0 (coin-settled). Median funding is the exchange-set 0.01% per 8h ("about 11% per year") while median basis is near zero, so "it is the funding rate that is driving the profitability". Volume is "driven by the long side". The Sharpe is "largely decoupled from the trend movements in the underlying BTC". Analysis abstracts from margin, leverage and transaction costs. — [Christin et al. PDF](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf)
- [WP] He, Manela, Ross & von Wachter, "Fundamentals of Perpetual Futures" (arXiv 2212.06888, v7 Sept 2026). Derives no-arbitrage perp prices including Binance's clamp. Deviations "are larger than in traditional currency markets, comove across currencies, and diminish over time". After 2022 "the average deviation reduces by almost 80% with volatility shrinking by more than 50%". Deviations "decline on average about 22 percentage points a year". Their random-maturity arbitrage gives BTC Sharpe 3.35 at retail costs and 11.65 for zero-fee market makers (3.27 / 10.46 after effective bid-ask spreads). The funding share of BTC strategy return by year: 19.6 (2020), 30.1 (2021), 4.1 (2022), 5.5 (2023), 18.2 (2024). Binance's April 2022 portfolio margining compressed deviations. Past spot returns *positively predict* perp-over-spot deviations (extrapolative leveraged demand). Effective spreads fell 48–73% after 2022 but spike in stress (BTC perp 5.95 bp on 12 Mar 2020, >10× median). — [arXiv abstract](https://arxiv.org/abs/2212.06888); [HTML v7](https://arxiv.org/html/2212.06888v7)

**Funding history and compression (primary data)**
- [PRIMARY-DATA] Binance USDT-M BTCUSDT funding, from monthly archive files `data/futures/um/monthly/fundingRate/BTCUSDT/` (2020-01 to 2026-09, 7,395 eight-hour prints, all `funding_interval_hours=8`). Simple sum per calendar year: 2020 17.2%; 2021 30.6%; 2022 4.2%; 2023 7.9%; 2024 11.9%; 2025 5.1%; 2026 Jan–Sep 2.2% (≈2.9% annualised). Share of prints that were negative: 14%, 7%, 22%, 10%, 8%, 13%, 26%. Share of prints **above** the 0.01% baseline: 26.8%, 42.9%, **0.0%**, 6.3%, 19.4%, **0.0%**, **0.0%**. Share above 0.03%: 17.8%, 30.0%, 0, 1.6%, 6.6%, 0, 0. — [data.binance.vision fundingRate](https://data.binance.vision/?prefix=data/futures/um/monthly/fundingRate/BTCUSDT/)
- [PRIMARY-DATA] ETHUSDT, same archive: 27.5%, 37.5%, 0.8%, 8.3%, 13.0%, 4.9%, 1.45% YTD (≈1.9% annualised). Negative share reached 34% in 2022 and 30% in 2026 YTD. Above-baseline share was 0.0% in 2022, 2025 and 2026. — [data.binance.vision fundingRate](https://data.binance.vision/?prefix=data/futures/um/monthly/fundingRate/ETHUSDT/)
- [VENDOR] Elm Wealth (Haghani & White, 2026) gives Binance BTC perp annualised funding by year: 2020 ~18.8%, 2021 ~35.9%, 2022 ~4.3%, 2023 ~8.2%, 2024 ~12.7%, 2025 ~5.3%, about 13.7% average against ~3.1% T-bills. They note the 0.01%/8h baseline compounds to 11.6%/yr and the USDT-M BTC cap is 0.30% per 8h. Their figures are slightly above the simple sums above, consistent with compounding (e.g. 0.028%×1095 compounded ≈ 35.9%). Source inconsistency: the article attributes the 0.01% baseline quote to "Sam Hayes" in one place, while the context (BitMEX founder) points to Arthur Hayes. — [Elm Wealth](https://elmwealth.com/perpetual-futures/)
- [VENDOR] Blockworks (June 2026): sUSDe (Ethena, a tokenised basis trade) yields fell from >20% peaks to 3.5–4.5%. "Each major bullish period since the launch of BTC ETFs in early 2024 has produced progressively lower returns for delta neutral strategies". Ethena TVL fell from $16.6bn (Sep 2025) to $5.6bn. The October 2025 liquidation event triggered ~$1.9bn of USDe redemptions in 48 hours. — [Blockworks](https://blockworks.com/newsletter/the-breakdown/issue/post_999b16ee-0fe0-4631-a1d0-2aa15a1d18a7)
- [VENDOR] USDe briefly traded at ~65 cents on Binance on 10 Oct 2025 during the liquidation cascade. — [stablecoininsider](https://stablecoininsider.org/ethenas-staked-usde-stablecoin/)

**Funding as a directional predictor**
- [VENDOR] Presto Research (Aug 2024): 7-day funding-rate changes explain 12.5% of *contemporaneous* 7-day BTC price changes. Funding change at T against price change at T+1 shows "near-zero correlation… zero R-squared", so "price changes for a single asset cannot be reliably predicted using only funding rate changes". They suggest cross-sectional use (a top-50 stat-arb alpha, tested without transaction costs). — [Presto Research](https://www.prestolabs.io/research/can-funding-rate-predict-price-change)
- [WP] Palazzi, Junior & Klotzle (SSRN 6725492; abstract only, full text blocked): "crypto-native valuation and positioning variables, in particular the market-to-realized value ratio, funding rates, and open interest, predict Bitcoin returns in every regime". Direction, horizon and effect size were not visible. — [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6725492)
- [VENDOR/blog] Fulgur Ventures (2021) reports a "slight negative correlation" between BTC returns and funding, framed as mean reversion. — [Medium](https://medium.com/@fulgur.ventures/bitcoin-funding-rates-and-price-predictability-27ce95535af1)
- [PEER] Inan, "Predictability of Funding Rates" (*International Review of Financial Analysis*, per secondary reporting) forecasts the funding rate itself out of sample on Binance/Bybit BTC, not spot returns. — [SSRN 5576424](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5576424); [Securities.io summary](https://www.securities.io/bitcoin-funding-rates-forecasting-trading-costs/)
- [WP] He, Wang & Zhang (SSRN 6185958), 200 Binance USDT perps: "basis reversals are associated with directional liquidation, and rate caps accompany persistent price gaps." — [SSRN PDF](https://papers.ssrn.com/sol3/Delivery.cfm/6185958.pdf?abstractid=6185958&mirid=1)
- [PEER, high-yield outlier] A ScienceDirect study of funding-rate arbitrage on CEX/DEX reports "up to 115.9% over six months" with 1.92% max drawdown for a Drift XRP strategy at **7x leverage**. This is a single cherry-picked best case on a DEX and is not representative of BTC/ETH. — [ScienceDirect S2096720925000818](https://www.sciencedirect.com/science/article/pii/S2096720925000818)

**Mechanics and risks**
- [VENDOR] Binance funding = Premium Index + clamp(0.01% − P, ±0.05%), so when the premium sits within ~5 bp of 0.01% the rate is pinned at the 0.01% baseline. The cap is 0.30%/8h on USDT-M BTC. In stress, exchanges can shorten the interval to 1h. Binance's terms let it deploy insurance-fund excess "for other purposes", and per Celsius/FTX precedent customer pooled assets can fall into a bankruptcy estate (exchange counterparty risk). — [Elm Wealth](https://elmwealth.com/perpetual-futures/)

### Inferences
- **Cash-and-carry is not a "signal" this lab can exploit**: it needs a short perp leg, which a long/flat spot lab cannot hold. At 2025–2026 levels (≈2–5%/yr gross before fees, spread and exchange risk) it pays roughly at or below T-bills, so the carry trade has been arbitraged down, in line with He et al.'s ~22pp/yr decline in deviations.
- **For a long/flat spot filter**, the economically motivated use of funding/basis is as a *risk-off / crash-risk filter*: stay flat when carry or funding is very high (BIS: high carry predicts crashes). But in the archive, BTC and ETH funding never exceeded the 0.01% baseline in all of 2022, 2025 and 2026 YTD. A "flat when funding > baseline" rule would have been always-in during those years, so any backtest result would rest on 2020–2021 and 2024 episodes. That means few independent events and a high risk of overfitting a handful of peaks.
- The *negative* side has become more informative: negative prints are 22–34% of observations in 2022 and 2026. A "negative funding = crowded shorts" contrarian *entry* filter is testable with free data, but I found no peer-reviewed evidence for it on BTC/ETH.
- Funding is clamped and pinned at 0.01% most of the time, so it is a coarse, censored variable. The continuous **premium index** (perp vs index, `premiumIndexKlines`) is a better-behaved feature than funding itself.

### Gaps
- Could not read the BIS PDF body (bis.org returned an HTML page). The horizon and size of the "high carry predicts crashes" effect, and whether it holds after 2022, are unverified.
- No peer-reviewed paper found that tests BTC/ETH funding level as a time-series predictor of *spot* returns net of costs with data from 2022 onward. Palazzi et al. (SSRN) claims predictability, but the effect size and out-of-sample design could not be read.
- Inan's journal status is from a secondary source (Securities.io); the SSRN page was 403.

## 2. Open interest changes and liquidation cascades

### Takeaway
There is little rigorous, peer-reviewed evidence that open interest or liquidation data predict BTC/ETH *returns* at horizons a spot long/flat lab can trade. The literature mostly links OI and futures activity to *volatility*. Liquidation "cascade then reversal" stories are overwhelmingly media and vendor narratives about single events. Free liquidation data on Binance is lossy (1-per-second snapshots) and has no USD-M archive. OI history is free and deep through the `metrics` archive, even though the REST endpoint holds only 30 days.

### Cited Findings
- [PEER] Zhang, Ma, Arkorful & Peng (2023, *International Review of Financial Analysis* 86): in the long run, Bitcoin volatility "covaries positively with unexpected futures trading volume, but negatively with unexpected futures open interest". This is a volatility result, not return direction. — [RePEc](https://ideas.repec.org/a/eee/finana/v86y2023ics1057521923000133.html)
- [WP] Palazzi et al. (SSRN): open interest is among the crypto-native variables that "predict Bitcoin returns in every regime". Details were not accessible. — [SSRN 6725492](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6725492)
- [WP] He, Wang & Zhang: basis reversals on 200 Binance perps "are associated with directional liquidation". — [SSRN PDF](https://papers.ssrn.com/sol3/Delivery.cfm/6185958.pdf?abstractid=6185958&mirid=1)
- [VENDOR/news] A preprint on falling BTC OI after liquidations rests on **seven events**, and its percentages "do not establish a universal threshold". — [publish0x](https://www.publish0x.com/things-i-think-about/what-falling-bitcoin-open-interest-means-after-liquidations-xzrelyj)
- [VENDOR/news] Event narratives: the Oct 2025 tariff shock wiped out ">$19 billion in leveraged crypto positions in a matter of hours" ([Elm Wealth](https://elmwealth.com/perpetual-futures/)); $1.74bn of short liquidations on 20 Aug 2026 ([Bitsgap](https://bitsgap.com/blog/liquidation-cascades-explained)); CoinDesk (Aug 2026) read falling BTC-denominated OI during a rally as short-covering ([CoinDesk](https://www.coindesk.com/markets/2026/08/25/a-bitcoin-short-squeeze-for-the-ages-as-futures-open-interest-collapses)). These are anecdotes, not tests.
- [VENDOR/primary doc] Binance liquidation streams "do not push realtime order data anymore. Instead, they push snapshot order data at a maximum frequency of 1 order push per second", and `GET /dapi/v1/allForceOrders` "stop being maintained and no longer accepts request" — [Binance Change Log](https://developers.binance.com/docs/derivatives/change-log). For USD-M `<symbol>@forceOrder`, "only the largest one liquidation order within 1000ms will be pushed as the snapshot" — [Binance Liquidation Order Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)
- [PRIMARY-DATA] The archive has `liquidationSnapshot` only under **coin-margined** futures (`data/futures/cm/daily/liquidationSnapshot/`). For BTCUSD_PERP it runs 2023-06-25 to 2024-10-14 (472 daily files) and then stops. No `liquidationSnapshot` folder exists under `data/futures/um/` (USDT-M). — [data.binance.vision cm](https://data.binance.vision/?prefix=data/futures/cm/daily/)

### Inferences
- OI is best treated as a **conditioning/regime variable** (leverage build-up → crash-risk, per the BIS convenience-yield story) rather than a direction signal. A testable long/flat hypothesis is "go flat when OI/market-cap or OI growth is in the top decile and funding/premium is elevated". Its economic rationale is the BIS leverage-crash channel, but the number of independent crash events since 2020 is small (dozens at most), so expect very low statistical power.
- Liquidation-based signals cannot be backtested honestly with free Binance data. History would have to be recorded forward from the WebSocket, and even then it is truncated to one order per second per symbol. Proxies such as large 5-minute OI drops combined with large returns (from the metrics archive) are possible but would be an inferred liquidation measure.

### Gaps
- No peer-reviewed BTC/ETH study was found that measures returns after liquidation spikes or OI shocks net of costs on post-2022 data.
- The USDT-M REST liquidation endpoint (`/fapi/v1/allForceOrders`) status was not directly confirmed. The change-log snippet names the `/dapi` (coin-M) version as discontinued; both are believed retired but this is unverified.

## 3. Taker buy/sell (order-flow) imbalance

### Takeaway
Order-flow imbalance has the most consistent academic support for predicting returns, but the predictable part is concentrated at very short horizons (seconds to hours) and is mostly too small to beat taker fees. The newest rigorous Binance study (Kim & Hansen 2026) finds imbalance at quarter-hour openings predicts 4–12h returns. The economic size is a few basis points, and the authors themselves put the boundary-return forecast at ~0.5 bp, about one-twentieth of a round-trip taker fee. Daily-horizon evidence exists (Bitstamp, R² ~1%). Taker-buy volume is free in every Binance spot kline, so this is the cheapest non-price feature to test.

### Cited Findings
- [WP] Kim & Hansen, "The Quarter-Hour Effect" (arXiv 2607.09426, Jul 2026), Binance USDT-M perps (BTC, ETH, XRP, SOL, DOGE, ADA) aggregate trades. Bursts of volume and volatility at 1-, 5- and 15-minute marks are tied to algorithmic trading. "Opening returns are predictable out of sample, while opening order imbalance predicts returns over four to twelve hours, with much weaker effects at finer clock-time frequencies." The combined model reaches average out-of-sample R² 3.4% and AUC 0.60 for opening returns. On economic size, the realized sign-weighted forecast "averages about 0.5 bp per boundary, about one tenth of a single standard-tier taker fee and one twentieth of a round trip" (taker 5.0 bp, maker 2.0 bp). Interquartile effects for 4–12h returns: lagged-flow component ~5–6 bp; public-information component <1 bp at 4h, 9.8 bp at 8h, 16.9 bp at 12h. BTC buyer share 0.499. — [arXiv 2607.09426](https://arxiv.org/abs/2607.09426); [HTML](https://arxiv.org/html/2607.09426v2)
- [PEER] Bitcoin crash-risk nowcasting with order imbalance (*Review of Quantitative Finance and Accounting*, 2023), Bitstamp data April 2013 to Jan 2023. Daily order imbalance predicts BTC returns with t-stats 5.2 (contemporaneous), 6.2 (1-day lag) down to 2.9 (7-day lag), but R² only 1.3%, 1.05%, 0.45%… 0.23%. The relation is strongly nonlinear (linear fit R² = 0.013). Time-varying significance in rolling windows. — [PMC10040314](https://pmc.ncbi.nlm.nih.gov/articles/PMC10040314/)
- [PEER] "Order flow and cryptocurrency returns" (*Journal of Financial Markets*, 2026, special issue on ML/AI and trading): "order flow has a permanent effect on cryptocurrency returns". A linked Guelph thesis abstract claims "order flow has high predictive ability for daily cryptocurrency returns" out of sample. Full text was not accessible (robots-blocked). — [ScienceDirect S1386418126000029](https://www.sciencedirect.com/science/article/pii/S1386418126000029); [Guelph thesis](https://atrium.lib.uoguelph.ca/items/803d3c13-a2cd-4600-8b3a-a48648a6746a)
- [PEER] Microstructure literature: order-flow and book imbalances "contain information about future price changes at intraday and sub-second horizons", a predictability "directly exploited by high-frequency market makers". — [Springer, Decisions in Economics and Finance 2026](https://link.springer.com/article/10.1007/s10203-026-00570-z)
- [PRIMARY-DATA] Binance spot klines on data.binance.vision carry `taker_buy_base_asset_volume` and `taker_buy_quote_asset_volume` (columns 10–11 of 12) on every bar, so a buy-share imbalance (2·takerBuy/volume − 1) can be built for any interval with full kline history. **Gotcha:** 2026 spot archive timestamps are in **microseconds** (16 digits, e.g. `1792454400000000`), while futures and older spot files use milliseconds. — sample `data/spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10-20.zip` on [data.binance.vision](https://data.binance.vision/?prefix=data/spot/daily/klines/BTCUSDT/1h/)
- [PRIMARY-DATA] The futures `metrics` archive includes `sum_taker_long_short_vol_ratio` (perp taker buy/sell volume ratio) at 5-minute steps. — see Section 5.

### Inferences
- For a long/flat spot lab on 1h–1d bars, taker imbalance is the most promising *free* non-price feature on evidence quality, but the plausible effect is a few bp per trade at 4–12h. With ~10 bp spot taker fee per side plus spread, a strategy must trade rarely and hold for hours to days, or the edge is eaten. Use it as a *filter/conditioner* on an existing entry (e.g. only enter when trailing-N-bar imbalance is positive), not as a high-turnover standalone signal.
- Perp taker imbalance (metrics archive) may lead spot, since price discovery is concentrated in perps; this is untested here.
- Because imbalance and contemporaneous return are mechanically correlated, the lab's no-look-ahead rule matters: use bar t's completed imbalance and fill at t+1 open.

### Gaps
- Exact out-of-sample net-of-cost results for daily-horizon order-flow strategies (the JFM 2026 paper) could not be read.
- No study found that uses Binance *spot* taker-buy volume specifically at 1h–1d horizons after 2022.

## 4. Options: implied volatility (DVOL), variance risk premium, 25-delta skew / risk reversal

### Takeaway
Option-implied measures robustly forecast *volatility* and downside risk, but directional return predictability is weak or regime-dependent. A recent journal article states "return direction is not robustly predictable". One unrefereed dissertation finds the 25-delta 90-day risk reversal significant for BTC returns (Deribit 2021–2025). Free history exists: Deribit DVOL via public API since 24 Mar 2021, and Binance BVOL index ticks in the archive since June 2023. Skew history is not freely archived; it must be rebuilt from option chains or recorded forward.

### Cited Findings
- [PEER] "Do hourly Bitcoin options forecast risk beyond a HAR benchmark?" (*Finance Research Letters*, 2026): option-implied signals add to volatility forecasts, but "return direction is not robustly predictable; downside-risk gains are regime-driven". — [ScienceDirect S1544612326013814](https://www.sciencedirect.com/science/article/abs/pii/S1544612326013814)
- [WP] Almeida, Grith & Miftachov, "Risk Premia in the Bitcoin Market" (arXiv 2410.15195): Bitcoin shows a larger variance risk premium than the S&P 500. Clustering risk-neutral densities shows risk premia vary over time by regime. Unlike the S&P 500, where VRP positively predicts returns, they find "a negative relationship between the BP and BVRP" (Bitcoin return premium vs Bitcoin VRP) across clusters. ATM options are "expensive", which is evidence of a VRP. — [arXiv abs](https://arxiv.org/abs/2410.15195); [HTML v2](https://arxiv.org/html/2410.15195v2)
- [Thesis, not peer-reviewed] Neo (SMU dissertation): using daily Deribit data from April 2021 to December 2025 (1,723 observations), the 25-delta 90-day risk-reversal coefficient is "significant at the 1% level in a thirteen-variable baseline regression with Newey–West standard errors". Sign, effect size and out-of-sample performance were not visible (page blocked by Incapsula). — [SMU InK](https://ink.library.smu.edu.sg/etd_coll/887/)
- [PEER] Net buying pressure: risk-neutral skewness of Bitcoin becomes more negative in bearish-opinion periods (information flows into option prices, not necessarily ahead of spot). — [ResearchGate abstract](https://www.researchgate.net/publication/361828193_Net_buying_pressure_and_the_information_in_bitcoin_option_trades)
- [VENDOR primary doc] DVOL is a 30-day forward-looking annualised implied vol built like the VIX. DVOL/20 ≈ expected daily move. — [Deribit Insights](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/)
- [PRIMARY-DATA] Deribit `public/get_volatility_index_data` (params `currency`, `start_timestamp`, `end_timestamp`, `resolution`; returns `[ts, open, high, low, close]`) answered without authentication from this machine. With `resolution=1D` the earliest BTC row returned was **2021-03-24**. — [Deribit API docs](https://docs.deribit.com/api-reference/upcoming/market-data/public-get_volatility_index_data); query `https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&...&resolution=1D`
- [VENDOR] Tardis.dev lists `deribit_volatility_index` "available since 2021-04-01" (paid tick data). — [Tardis docs](https://docs.tardis.dev/historical-data-details/deribit)
- [PRIMARY-DATA] Binance options archive: `data/option/daily/BVOLIndex/{BTCBVOLUSDT,ETHBVOLUSDT}/` runs from 2023-06-20 to 2026-10-25 (1,198 daily files for BTC, so some days are missing). Each file holds 1-second ticks: `calc_time,symbol,base_asset,quote_asset,index_value` (86,400 rows/day). `data/option/daily/EOHSummary/BTCUSDT/` runs only from 2023-05-18 to 2023-10-23 (147 files). — [data.binance.vision option](https://data.binance.vision/?prefix=data/option/daily/)

### Inferences
- For a long/flat spot lab, the best-supported use of options data is **risk/volatility sizing or a regime filter** (e.g. flat or reduced exposure when DVOL is high relative to realised vol, or when skew turns sharply put-rich), not return direction.
- The risk-reversal result is the only directional claim found. It is a single unrefereed thesis with a 13-variable in-sample regression, so treat it as a hypothesis to test in the lab, not evidence. DVOL (from 2021-03) gives about 5.5 years of daily history: enough for a regime filter, thin for anything with tuned thresholds.
- VRP (DVOL² − realised variance) can be built from free data (DVOL plus spot klines) with no option chains.

### Gaps
- No free historical 25-delta skew series was confirmed. It would need Deribit option-chain history (Tardis, paid) or a forward recorder of `public/get_book_summary_by_currency` / ticker IVs. Not verified.
- Effect sizes and the sign of the SMU risk-reversal result are unknown.
- Whether Deribit's public API stays reachable from the owner's location long term was not checked; it worked from this machine on 2026-10-26.

## 5. Free Binance and Deribit data: endpoints, history depth, granularity

### Takeaway
The REST "trading data" endpoints (`openInterestHist`, long/short ratios, `takerlongshortRatio`) hold only the last ~30 days. The **`data.binance.vision` `metrics` archive** holds the same series at 5-minute resolution back to **2020-09-01 for BTCUSDT** (2021-12-01 for ETHUSDT) with no missing days. Funding (8h) and premium-index klines are archived back to 2020-01. Liquidations have no usable free USD-M history. `fapi.binance.com` is geo-blocked (HTTP 451) from this machine, so the archive is the practical path anyway.

### Cited Findings
- [VENDOR primary doc] USD-M `GET /futures/data/openInterestHist`: "If startTime and endTime are not sent, the most recent data is returned. Only the data of the latest 1 month is available. IP rate limit 1000 requests/5min". The coin-M equivalent says "Only the data of the latest 30 days is available." — [Binance USD-M Open Interest Statistics](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics); [coin-M version](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Open-Interest-Statistics)
- [VENDOR primary doc] `GET /dapi/v1/openInterest` returns only *present* open interest. — [Binance Open Interest](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Open-Interest)
- [PRIMARY-DATA] Archive folder inventory (S3 listing, 2026-10-26):
  - `data/futures/um/daily/`: aggTrades, bookDepth, bookTicker, indexPriceKlines, klines, markPriceKlines, **metrics**, premiumIndexKlines, trades.
  - `data/futures/um/monthly/`: aggTrades, bookTicker, **fundingRate**, indexPriceKlines, klines, markPriceKlines, premiumIndexKlines, trades.
  - `data/futures/cm/daily/`: same as um/daily plus **liquidationSnapshot** (coin-M only, ended 2024-10-14 for BTCUSD_PERP).
  - `data/spot/daily/`: aggTrades, klines, trades.
  - `data/option/daily/`: BVOLIndex, EOHSummary.
  — [data.binance.vision](https://data.binance.vision/?prefix=data/futures/um/daily/)
- [PRIMARY-DATA] **metrics** (`data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-DD.zip`):
  - Columns: `create_time, symbol, sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`, i.e. OI in coin and USD, top-trader long/short (accounts and positions), global long/short account ratio, and taker buy/sell volume ratio.
  - Granularity is 5 minutes (288 distinct timestamps/day).
  - BTCUSDT runs 2020-09-01 to 2026-10-25, 2,246 daily files, **0 missing days**. ETHUSDT starts 2021-12-01 (1,790 files).
  - Quirks: the 2020-09-01 file has every row duplicated (577 lines = 2×288+1); the 2026-10-20 file's rows are **not time-sorted**; recent files print 16-decimal values. Deduplicate and sort on load.
  — [data.binance.vision metrics](https://data.binance.vision/?prefix=data/futures/um/daily/metrics/BTCUSDT/)
- [PRIMARY-DATA] **fundingRate** monthly files: columns `calc_time,funding_interval_hours,last_funding_rate`. BTCUSDT and ETHUSDT run from 2020-01. All 2020–2026 BTC/ETH prints were 8-hour intervals. Some `calc_time` values carry a few ms of jitter (e.g. `...800005`), so align to the 8h grid. — [data.binance.vision fundingRate](https://data.binance.vision/?prefix=data/futures/um/monthly/fundingRate/)
- [PRIMARY-DATA] **premiumIndexKlines** (monthly and daily, all kline intervals, from 2020-01 for BTCUSDT 1h): standard kline columns where OHLC are the premium index (e.g. `-0.00045409`). The volume fields are 0 and `count` is the number of samples (720 per 1h bar ≈ every 5s). — [data.binance.vision premiumIndexKlines](https://data.binance.vision/?prefix=data/futures/um/monthly/premiumIndexKlines/BTCUSDT/1h/)
- [PRIMARY-DATA] Liquidations: USD-M has no archive folder; coin-M `liquidationSnapshot` covers 2023-06-25 to 2024-10-14 only. Live streams are 1-order-per-second snapshots ([Binance change log](https://developers.binance.com/docs/derivatives/change-log); [Liquidation streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)).
- [PRIMARY-DATA] Options: Binance BVOLIndex 1-second ticks from 2023-06-20; Deribit DVOL OHLC via the public API from 2021-03-24 (see Section 4).
- [PRIMARY-DATA] `https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1` returned **HTTP 451** from this machine (same geo-block as api.binance.com), while `data.binance.vision` downloads worked.
- [VENDOR] Third-party aggregators with longer or cross-exchange OI, funding and liquidation history: CoinGlass, Coinalyze (free tiers are partly limited) — [CoinGlass](https://www.coinglass.com/BitcoinOpenInterest), [Coinalyze](https://coinalyze.net/)

### Inferences
- **Recommended free feature set for the lab** (all point-in-time, from data.binance.vision):
  - (a) 8h funding, forward-filled *only* to its publication time;
  - (b) premium index (1h/4h/1d klines) as a continuous basis proxy;
  - (c) OI and OI change, both in USD and as OI/spot-volume, from 5-min metrics;
  - (d) top-trader and global long/short ratios;
  - (e) perp taker buy/sell ratio;
  - (f) spot taker-buy share from klines;
  - (g) DVOL daily from Deribit;
  - (h) BVOL (from 2023-06 only).
- Point-in-time care:
  - A metrics row stamped `create_time` T may describe the interval ending at T; whether it was published at T or later is unverified, so lag it by at least one 5-min step, or one bar.
  - Funding `calc_time` is a settlement time; the *predicted* funding during the window is not archived.
- The lab's "no forward-filled gaps" rule conflicts with funding's 8h cadence. Funding is a step function that is genuinely constant between settlements, so carrying the last *published* value forward is point-in-time correct. Document this as an explicit exception, or use premiumIndexKlines instead.

### Gaps
- Not directly verified (docs blocked): `GET /fapi/v1/fundingRate` max `limit` (believed 1000) and full history depth via REST; `GET /fapi/v1/premiumIndexKlines` limits; the exact 30-day text for `/futures/data/takerlongshortRatio`, `globalLongShortAccountRatio`, `topLongShortAccountRatio` and `topLongShortPositionRatio` (believed identical to openInterestHist). The metrics archive makes these moot for backtesting.
- Exact publication latency of daily metrics files, and whether historical files are ever revised, is unknown.
- Intraday row completeness was checked only on two sample metrics files (both 288/288); a full scan was not done.

## 6. Which signals work as filters on long/flat spot vs which need shorting/derivatives

### Takeaway
Most of the documented *returns* in this family (cash-and-carry, funding arbitrage, cross-sectional funding stat-arb) need a short perp leg or a long-short book, so a long/flat spot lab cannot capture them. The documented *information* that a long/flat spot strategy can use is mostly about risk: high carry, funding or leverage before crashes (BIS), and option-implied vol and skew for downside risk. Taker order-flow imbalance is the one directional, spot-native signal with peer-reviewed support, but its size is a few bp. Honest prior: these features are more likely to cut drawdowns or volatility than to raise risk-adjusted return above buy-and-hold, and any gain must clear the lab's DSR bar given small event counts.

### Cited Findings
- Carry/basis returns come from being short the perp and long spot ("going long in the spot market, while selling short in the futures contract") — [BIS WP 1087](https://www.bis.org/publications/working-paper-1087-crypto-carry); Sharpe 7–10 in-sample but abstracting from margin and costs — [Christin et al.](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf); arbitrage Sharpe 3.35 at retail costs, shrinking deviations — [He et al.](https://arxiv.org/html/2212.06888v7)
- Funding is useful "cross-sectionally across multiple assets", a long-short stat-arb tested without costs, while single-asset time-series prediction showed zero R² — [Presto Research](https://www.prestolabs.io/research/can-funding-rate-predict-price-change)
- High carry predicts crashes (a usable go-flat filter) — [BIS WP 1087](https://www.bis.org/publications/working-paper-1087-crypto-carry)
- Option signals: risk forecasting yes, return direction not robust — [Finance Research Letters 2026](https://www.sciencedirect.com/science/article/abs/pii/S1544612326013814)
- Order imbalance predicts 4–12h returns but the per-event size is well below round-trip fees — [Kim & Hansen 2026](https://arxiv.org/html/2607.09426v2); daily OFI R² ≈1% — [RQFA 2023](https://pmc.ncbi.nlm.nih.gov/articles/PMC10040314/)
- Since 2025 BTC/ETH funding has never exceeded the baseline (0.0% of prints above 0.01%) — [PRIMARY-DATA, data.binance.vision fundingRate](https://data.binance.vision/?prefix=data/futures/um/monthly/fundingRate/BTCUSDT/)

### Inferences
Summary matrix (my synthesis of the findings above):

| Signal | Usable as long/flat spot filter? | Evidence tier for BTC/ETH return prediction | Free history (Binance/Deribit) | Main caveat |
|---|---|---|---|---|
| Funding level (8h) | Yes: flat when extreme positive; possible contrarian entry when negative | WP/vendor; weak | fundingRate archive from 2020-01 | Pinned at 0.01% most of the time; never above baseline 2022, 2025, 2026 |
| Premium index / perp basis | Yes: crash-risk filter | WP (BIS: high carry → crashes) | premiumIndexKlines from 2020-01, any interval | Few independent crash events; compressing since 2022 |
| Cash-and-carry yield | **No** (needs short perp) | WP: high in-sample Sharpe 2020–22; compressed | same | ≈2–5%/yr gross in 2025–26; exchange/margin risk |
| Open interest level/change | Yes: leverage-regime filter | Mostly volatility evidence (PEER); return claim only in SSRN WP | metrics 5-min from 2020-09 (BTC), 2021-12 (ETH) | REST only 30 days; regime-dependent |
| Liquidations | Only if recorded forward | Anecdotal/vendor | **None for USD-M**; coin-M 2023-06–2024-10 only | 1 order/s snapshots, truncated |
| Long/short ratios (global, top trader) | Yes, as a contrarian sentiment filter | No rigorous evidence found | metrics 5-min | Binance-only crowd; untested |
| Taker imbalance (spot klines, perp metrics) | Yes, as a conditioner on entries | PEER/WP; small effect | Spot klines (full history); metrics 5-min | Few bp at 4–12h, below round-trip fees if traded often |
| DVOL / VRP | Yes: risk/vol regime filter | PEER: risk yes, direction no | Deribit API daily from 2021-03-24; BVOL from 2023-06 | Short history |
| 25Δ skew / risk reversal | Yes, if data obtained | Thesis only (unrefereed) | Not freely archived | Needs paid chain history or forward recording |

- Practical recommendation for the lab: test at most one or two pre-registered, economically motivated hypotheses rather than sweeping all features, because each adds to DSR's N. Candidates: (1) "flat when 7-day premium index or funding z-score is in its top decile" (BIS crash channel); (2) "require positive trailing taker-buy imbalance to enter" layered on a simple trend rule. Expect "no edge found" to be a likely and correct outcome, given compressed funding and sub-fee microstructure effects.

### Gaps
- No study found that directly tests derivatives-positioning features as *filters on a long/flat spot strategy* net of costs, on BTC/ETH, using data from 2022 onward. The matrix above is synthesis, not a cited result.
- Vendor research (Kaiko, Glassnode, Amberdata) on funding/OI predictive power was not reached in this pass; the Kaiko pages found were factor-model marketing, not signal tests.
