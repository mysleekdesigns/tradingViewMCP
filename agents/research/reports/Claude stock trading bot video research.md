---
date: 2026-11-04
source: https://www.youtube.com/watch?v=lH5wrfNwL3k
outcome: one idea worth testing (trailing stop exit); copy trading and ladder buys rejected; wheel not buildable here without changing the project's constraints
---

# Claude stock trading bot video research

Review of Samin Yasar, "Claude Just Changed the Stock Market Forever! (Tutorial)" (April 2026, 35 min). The full
transcript was read; notes are in `research_notes/Claude stock trading bot video research/video_notes.md`. This is
research only: no code changed, no backtest ran and no ledger trial was spent. Results quoted are historical and not
forward-looking.

> [!warning] Untrusted source
> The video promotes a paid community and shows no track record. Everything about it below is the presenter's claim.
> Outside sources are mixed in quality and each is graded in section 6. No page contained instructions aimed at this
> agent.

## 1. Bottom line

The strict rules in the video are three stock and options recipes run through Alpaca paper accounts. Only one part
fits this project:

| Strategy | Verdict | Why |
|---|---|---|
| Trailing stop | **Test it** | Real, simple, never tested here, and a first version needs no code |
| Ladder buys | Reject | Averaging down; banned by Phase 20, and it contradicts the video's own stop |
| Copy trading Congress | Reject | Stocks only; the best recent evidence says it fails even with perfect timing |
| Wheel | Do not build now | No options anywhere in the project; the evidence for BTC is weak until filters are added |

Expect "no edge found" from the trailing stop test as well. The value is closing a known gap, not a likely win.

## 2. What the video proposes

| | Trailing stop | Copy trading | Wheel |
|---|---|---|---|
| Market | Tesla | Whatever Michael McCaul trades | Tesla options |
| Entry | Market buy, 10 shares | Copy disclosed trades, read from Capitol Trades | Sell a put about 10% below price, 2 to 4 weeks out |
| Exit | −10% stop. From +10%, trail 5% below price | Copy disclosed sales | Sell a call about 10% above cost; close early at 50% of premium |
| Adds | −15% buy 10, −50% buy 50 | n/a | n/a |
| Checks | Every 5 minutes, market days | Scheduled | Every 15 minutes in market hours |
| Evidence shown | None | One politician, one year: 34.8% against 15.5% | None |

In all three, Claude itself is the running process, on a local schedule that stops when the computer sleeps.

## 3. Problems in the video

**Rules that conflict.** The −10% stop sells the whole position before a −15% ladder level can be reached, so the ladder
either never fires or is really a new entry after a loss. The role-play at 16:20 never catches this.

**The McCaul figure cannot be used.** One politician was picked by recent results, over one year, with no costs, and
compared after the fact. Choosing the winner first is look-ahead. A proper test would pick the politician using only
data from before each year.

**The wheel claim at 31:27 is false.** Direction does matter. A wheel is mostly a long position with a capped upside.
Per share, using the video's own numbers (entry idea at $250, put strike $230, $5 premium):

| Tesla ends at | Buy and hold | Wheel |
|---|---|---|
| $150 | −$100 | −$75, and the "never below cost" rule leaves no good call to sell |
| $225 | −$25 | $0 |
| $260 | +$10 | +$5 (put only) |
| $400 | +$150 | +$45 at best, if assigned and then called at $260 |

**The second $5 premium is optimistic.** My Black-Scholes check (no rates, no skew, 21 days) puts the $230 put at $4.14
to $6.01 at 50 to 60% volatility, which is fair. The $260 call with Tesla at $225 is only $1.61 to $2.87 at those
volatilities, and reaches about $6 at 80%. The $4,500 example is the best path, not a typical one.

**Unsafe habits, none of which belong here.** API keys pasted into chat and saved to a file, `bypass permissions` mode,
and an LLM deciding strikes, expiries and rolls with no deterministic rules underneath. Alpaca has a native
`trailing_stop` order type, yet the video has Claude poll and move stops by hand.

## 4. Evidence check

**Copy trading.** The Equibles study (17,859 disclosure events, 2020 to 2025) is the most relevant source. One year
after disclosure, purchases trailed the S&P 500 by a median 5.36 points and sales by 6.39. Only about 43% beat the
index. Buys were not reliably better than sells (1.13 points, t = 1.15). The important part: starting from the private
trade date, which no follower can do, buys still trailed by 5.71 points. So the delay is not the main problem. Its
limits are that it uses price returns with no risk adjustment, and that it is a vendor note rather than a journal paper.
Chen and Sacerdote (2026) reach the same place for 2012 to 2023: portfolios underperform or at best match benchmarks.

The real counter-evidence is Wei and Zhou (2025): members who become congressional leaders outperform matched peers by
47 percentage points a year. It is a working paper about a narrow group, and does not rescue "copy a top trader".

**Trailing stops.** Kaminski and Lo show that under a random walk a stop always lowers expected return, while with
momentum it can add value. So a stop only helps if the entry has a trend behind it. Han, Zhou and Zhu report large
cuts to the worst monthly loss on momentum portfolios with a 10% stop. That result is about crash protection on a
portfolio strategy, and this project's own momentum run (H1) found no edge.

**The wheel.** It has a real source of return: implied volatility tends to exceed realized volatility. Cboe's put-write
work documents that for the S&P 500, and Almeida et al. find a larger variance premium in BTC, higher in calm regimes
and lower in volatile ones. But harvesting it has not beaten holding:

| Source | Test | Result |
|---|---|---|
| The Options Bench | 21 years | Wheel 6.8%/yr, cash-secured puts 6.9%/yr, index 10.8 to 12.8%/yr |
| Days to Expiry | 40 tickers, two years, modelled prices | Wheel beat holding on 17 of 40; 1 of 12 where the stock rose over 60%; 7 of 8 where it fell |
| Anchorage, BTC | 20-delta, 30-day covered calls, Oct 2021 to Apr 2026 | Unfiltered overlay −0.1%/yr |
| Anchorage, BTC | Same, with trend and IV filters | +5.2%/yr, in the market 44% of the time; max drawdown −76.7% to −73.5% |

Tesla happens to be the best row in the Days to Expiry table (+44 points), which is one ticker in one window. The
Anchorage filtered figure comes from a study that reports tens of thousands of backtests, so this lab would discount it
heavily. Selling calls barely reduces BTC's drawdown either.

## 5. What this means for the project

### Already covered, and better

- Rules run by deterministic code, not an LLM: the lab and Freqtrade dry-run.
- Paper trading first, with pre-registered verdicts.
- No averaging down, enforced in `agents/research/paper/risk.py` and `freqtrade.py`.
- Stop-loss and take-profit percentages in `RiskSpec` (`lab/lab/types.py:180`).

### The gap the video does show

The lab has **no trailing stop**. `RiskSpec` holds only fixed percentages, the 2026-10-25 seed calibration had to drop
trailing stops for that reason, and idea-11 (ATR trailing stop) has been untested since the run stopped first. The
generated Freqtrade strategy also hard-codes `trailing_stop = False` (`agents/research/paper/freqtrade.py:429`).

### Proposals, in order

**P1. One Chandelier exit run. No code needed.** A trailing exit can already be written with existing features. This
passed the plan validator offline, without a backtest or a ledger entry:

```text
long_exit: close < dc.upper - 3 * a     (dc = donchian 22, a = atr 22)
```

It is checked on bar t's close and fills at t+1's open, so it is causal. Two limits: the channel is a rolling window
and not anchored to the entry, so unlike the video's floor it can drift down; and it is a close-based exit, not an
intrabar stop. The 22 and 3 are the usual Chandelier settings and would be fixed up front, in line with the 2 to 3 ATR
a viewer suggests. Trailing by ATR also suits crypto better than a flat percentage, because coin volatility differs so
widely. Costs: ledger N is already 5885, and with a trend entry this may simply join the exhausted trend family
(`family_return_corr` 0.8).

**P2. A real trailing stop in the lab. Needs lab changes.** Add activation and trail fields to `RiskSpec`, so the
video's exact rule (−10%, activate at +10%, trail 5%) can be tested. Things to get right:

- **Look-ahead.** A bar's high must not raise the stop that the same bar's low then hits, because the order inside a
  bar is unknown. The stop raised by bar t should apply from bar t+1. This needs the perturbation test.
- **Cross-check.** The NautilusTrader cross-check must match, and vectorbt's trailing behaviour with this engine's
  two-row bars needs confirming. I have not verified that.
- **Paper mapping.** Freqtrade supports this directly: `stoploss = -0.10`, `trailing_stop = True`,
  `trailing_stop_positive = 0.05`, `trailing_stop_positive_offset = 0.10`, `trailing_only_offset_is_reached = True`.
- **Frozen bots.** The seven HC paper bots stay untouched until their 2026-12-25 decision.

**P3. Trailing stop line on the chart. Web only.** An ATR trailing line via `/add-indicator`, plus a ratcheting floor on
Strategy Tester trades. It shows where a stop would sit, like Phases 16, 18 and 21, and claims no edge.

**P4. Scenario replay. Small.** The video's role-play is a good idea done badly, because an LLM describing what it would
do is not a test. Run a plan and its risk rules over a synthetic path (30% gap down, melt-up, chop) and list the exact
orders. No market data, so no ledger trial. It would have caught the stop and ladder conflict. Optional.

**P5. Implied volatility pane. Medium, information only.** Plot BTC's Deribit DVOL against realized volatility, to show
the premium the wheel tries to collect. The public endpoint answered from this machine with no key, with history from
2021-03-24. One call returned about 1,000 rows, so paging is needed. I only checked availability and did not calculate
any premium, so nothing is contaminated for a later pre-registered test.

### Options study: a decision for the owner, not a recommendation

A wheel cannot be expressed here. Plans are long or flat on spot, and Non-goals exclude stocks. DVOL is one 30-day index,
so it cannot price a put 10% out of the money; that needs a full volatility surface (Anchorage used Deribit surface data).
The Binance archive has only `BVOLIndex` and `EOHSummary` under `data/option/daily/`, and I did not inspect them. Venue
eligibility for the owner is unverified. This would need a new engine, new data, new benchmarks and a change to the
written constraints, for a result whose unfiltered evidence is about zero. Do P5 first if the interest stays.

### Rejected

- **Ladder buys.** Averaging down; `prepare` already refuses it.
- **Copying Congress.** Out of scope, and the evidence is negative. Crypto has no equivalent disclosure feed.
- **Crypto "smart money".** The lab's `long_short_ratio` and `oi_change` are gates only, H3's funding gate was inert, and
  the volume climax idea died. On-chain whale data would be new, and I did not research it.
- **Alpaca.** It conflicts with the written constraints (see the `alpaca-paper-trading` memory), and its MCP order tools
  stay unused.
- **Claude as the bot, keys in files, bypass mode.** All conflict with the hard constraints.

## 6. Source quality

| Source | Read | Standing |
|---|---|---|
| Video transcript | In full | Promotional; no track record |
| Equibles, congressional disclosures | In full | Vendor note, methods stated |
| Chen & Sacerdote (2026), NBER w35041 | Abstract only | Working paper |
| Wei & Zhou (2025), NBER w34524 | Abstract and introduction | Working paper, not peer reviewed |
| Belmont et al. (2022), J. Public Economics | **Not read** (site refused); cited via Equibles and search snippets | Peer reviewed |
| Kaminski & Lo; Han, Zhou & Zhu | Abstract-level snippets only | Published; full text not read |
| Almeida et al., "Risk Premia in the Bitcoin Market" | Abstract | arXiv preprint |
| Cboe put-write material (Bondarenko 2019) | Search snippet only | Exchange-published |
| Anchorage Digital, covered calls | Most of it | Vendor study, hypothetical results |
| Days to Expiry; The Options Bench | Page and summary | Vendor pages, modelled or simulated |
| Freqtrade docs; Alpaca docs | Relevant sections | Primary documentation |

Not checked: the McCaul chart's source, McCaul's actual disclosures, and any Capitol Trades terms of use.

## 7. Recommendation

Run P1 next if the owner wants a strategy test: it is cheap, uses no code, and either closes idea-11 or justifies P2.
P3 is the safest build if the owner would rather have something visible. Leave the wheel and copy trading out.
