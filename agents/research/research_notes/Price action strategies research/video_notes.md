# Video notes: "Claude Tested 100,000 Price Action Strategies (Here's What Works)"

> [!warning] Untrusted source
> YouTube, channel AI Pathways, uploaded 2026-10-28, 17:11 long, about 29k views when read on 2026-10-29.
> URL: https://www.youtube.com/watch?v=z97PnFIPoFg. It promotes a paid Skool community and 1:1 consulting.
> No code, data, or per-strategy statistics are published, so nothing here can be reproduced.
> These notes summarise the claims. They are not findings of this project. I saw no text aimed at AI agents.
> I read the transcript from YouTube's own transcript panel in the owner's Chrome. CrawlForge's
> transcript fetch was refused by YouTube's robots.txt, and I did not override that.

## Setup the video describes

- Market and data: NQ and ES futures (equity index), about 5 years of 1-minute data (about 3.6M bars),
  resampled from 1m up to daily.
- Concepts turned into "dials": what counts as a level (touches, line vs zone), trend (higher highs and
  higher lows, break of structure = BOS), change of character (ChoCh = the first close through the last
  swing the other way), sweep (price runs past a high or low and comes back), confirmation candle, where
  the stop and target go, timeframe, and filters (session, recent run).
- 10 base setups. 40,000 random dial combinations (about 4,000 per setup) plus 61,000 "common" versions,
  so about 101,000 variants and "300M+ trades".
- Costs and slippage included. If the stop and the target are both hit in the same bar, it counts as a loss.
- Random-line and random-trade controls.
- The last 2 years are a locked holdout. Before it, a consistency check on the first period across both
  markets. 4h ChoCh was the only setup that passed it on NQ and ES.

## Claimed results

| Topic | Claim |
|---|---|
| Funnel | 88k of 101k had enough trades; 24k were profitable after costs; 8,310 had Sharpe > 0.5 (about 8%). |
| Win rate | Rarely above 80%; correlation of win rate with profit only about 0.27. |
| Timeframe | Costs eat about 40% of gross profit on 1m. Holdout share of common versions that made money: about 9–10% on 1m vs about 45% on 4h. Higher timeframes were better. |
| Levels | Real S/R levels "held" more often than random lines (the 51.1% figure is not clearly defined). |
| Touch count | More touches did **not** make a level stronger. A 1-touch level beat its random line by about 9 pp, a 4-touch level by about 6.1 pp. 3+ touches broke more often. |
| Flip zones | Old resistance turning into support, on the first retest: +11.5 pp vs random (NQ), +10.9 pp (ES). The strongest level concept. |
| Sweeps | Reversed only about half the time. Waiting for a sweep before ChoCh *lost* money on the holdout. |
| Trend rules | BOS/ChoCh on their own don't predict direction, but ChoCh as a trade setup was the best of the 10. |
| Confirmation | Waiting for a confirming close gave a worse price but more edge, mostly on NQ. |
| Stops | Close-based stops won more often (40% vs 29%) but lost more overall. A plain hard stop was better. |
| Session | NY session only (08:00–16:00 ET) raised edge per trade (futures-specific). |
| Run filter | Skipping trades in the same direction as a 3-day run helped "everywhere we checked". |
| Best setup | 4h ChoCh, no sweep, enter at the signal close, stop past the last swing, target = 1R. Holdout: about +$62k on 1 NQ contract and +$39k on 1 ES contract over 2 years. 39 of 48 "usual" versions and about 2/3 of common versions were profitable on the holdout. |
| Framing | The presenter calls it a reversal or mean-reversion setup, in line with his earlier "9,000 strategies" and ICT videos. He says it is "a floor, not a ceiling" and "not a finished strategy". |

## What the video does not show

- No benchmark on the holdout (buy-and-hold one contract, or a random-entry null with the same
  exposure). 2024–2026 was a strong period for equity index futures, so long ChoCh trades may carry beta.
- No long/short split, trade count, Sharpe, drawdown, or DSR for the chosen setup on the holdout.
- No correction for about 101k variants. A Sharpe > 0.5 bar over a roughly 3-year design window is
  passed by about 13–19% of zero-edge strategies just by noise (SE of annual Sharpe ≈ 1/√years).
  So "8% pass" is about what no edge plus costs would give.
- The swing definition (pivot lookback and confirmation lag) is not stated. That is the dial that
  decides whether ChoCh is causal or repaints.
- Holdout honesty: 4h ChoCh was picked by a check on the first period and then tested once on the
  holdout. That is reasonable, but the 48 "usual" versions share one logic, so they are one hypothesis,
  not 48 confirmations.
