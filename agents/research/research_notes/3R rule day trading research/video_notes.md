# Video notes: "If You Only Have $50, Do This Every Morning (The 3R Rule)"

> [!warning] Untrusted source
> YouTube, channel The Rumers (presenter "Doug", says he has 26 years of day trading), 24:44 long, about
> 1.2M views when read on 2026-11-01. URL: https://www.youtube.com/watch?v=XBOGd67BJks.
> It promotes a paid live-trading Zoom room ("OTO squad") and a free weekly watchlist. The description says
> the author's results "are not typical". No trading record, sample size or backtest of the method is shown;
> the evidence is hand-picked chart examples (TSLL 5m, YM futures).
> These notes summarise the claims. They are not findings of this project. I saw no text aimed at AI agents.
> Transcript read from YouTube's own transcript panel in the owner's Chrome; title, description and chapters
> via CrawlForge `extract_embedded_state`.

## Topic

Intraday day trading of US stocks/ETFs and index futures on 5-minute charts, with $20–$50 risk per trade.
Despite the title, the "3R rule" is not a rule to always take 3:1 trades. The point is that a fixed 2:1 or
3:1 target is incomplete: you control the risk, not whether the market can reach the target, so you should
only take trades where the reward is realistic.

## Chapters

| Start | Chapter |
|---|---|
| 0:00 | The foundation of consistency |
| 1:36 | Roadmap to professional growth |
| 2:20 | Mastering risk versus reward |
| 6:11 | The probability factor |
| 10:32 | Live application of strategies |
| 18:50 | Analyzing market overextension |
| 20:04 | The Wall Street raise |
| 22:19 | Choosing the best trading asset |

## The method ("probability factor")

| Step | Rule as presented | Precise enough to code? |
|---|---|---|
| Stop placement | Stop goes at a structural level (e.g. below the day's low), not at a dollar amount. Size follows from the stop. | Partly: "structural" is discretionary. |
| 1. Trend bias | Daily chart, 50-day SMA. Price above it: 80–90% of trades long, full size, aggressive 3–6R targets. Counter-trend trades are small, scalps only (30 s to 2 min). Mirror image below it. | Yes. |
| 2. Liquidity zones | Range high/low = previous day's high and low. Swing high/low = the next older level beyond each (look back as many days as needed). Upper levels hold the strongest sellers, lower the strongest buyers. | Range high/low yes; "next level" swing is loosely defined. |
| 3. Range | Daily ATR (default settings, i.e. 14) is the day's expected move. If the day has already used much of it, or the target needs more than 100% of ATR, the target is unrealistic. | Yes. |
| Combined setup | Below the 50-day SMA + price pushed up into the previous day's high / swing high + most of the ATR already used → sell, stop just above the level, target back to the morning's levels (shown as about 4R). YM example: up 981 points vs a daily ATR of about 600 into the prior day's high, below the 50-day SMA → expected to fail. | Mostly yes, with a time-of-day choice for the "morning" target. |

## Scaling ("Wall Street raise")

Example: $5,000 account, 50 shares. Each +10% of account growth adds 10% to share size (50 → 55 → …). Before
sizing up, be profitable 3 of 5 days a week for at least a couple of weeks; start as small as you can. Note
this is close to fixed-fractional sizing applied in steps.

## Asset choice

Recommends index futures (ES, NQ, YM) for beginners: "futures go up about 80% of the time" and an
unpublished backtest claim that buying SPY/ES at the open and selling 1 h / 2 h / at the close wins 51% /
53% / 54% of the time over 15 years. Crypto is listed as tradable but not preferred.

## Sound points

- Size from a structural stop, not a dollar figure; keep risk per trade small.
- A fixed R:R target says nothing by itself; expectancy needs the win rate at that target. The reachability
  check (target vs remaining daily range) is a sensible sanity check.
- Scale slowly; most blow-ups follow a size jump after a short winning streak.

## Weak or unsupported points

- No out-of-sample evidence; every example is chosen after the fact. The first TSLL example "actually did pay
  out" even though the video says it was the wrong trade, which shows how weak single examples are.
- "3 of 5 profitable days for a couple of weeks" (about 10 days) cannot separate skill from luck at any
  realistic Sharpe ratio.
- A 54% open-to-close win rate does not mean positive expectancy after costs; the US-equity literature puts
  most of the index's long-run return overnight, not intraday (see the report).
- Scalps of 30 s–2 min are not testable in this project (smallest bar is 5m, next-bar-open fills, ~0.3%
  round-trip spot costs).
