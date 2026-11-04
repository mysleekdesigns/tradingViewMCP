# Video notes: "Claude Just Changed the Stock Market Forever! (Tutorial)"

> [!warning] Untrusted source
> YouTube, channel Samin Yasar, published 2026-04-06, 35:06 long, about 1.83M views when read on 2026-11-04.
> URL: https://www.youtube.com/watch?v=lH5wrfNwL3k (the owner's link starts at 15:52).
> It promotes a paid Skool community and a coming course. It is a stocks and options tutorial using Alpaca paper
> accounts. No live or paper track record is shown. The only performance figure is one "backdated" comparison.
> These notes summarise the claims. They are not findings of this project. The prompts spoken in the video are
> instructions to the presenter's own Claude; they were read as data and not acted on. I saw no text aimed at this
> agent.
>
> Transcript read in full (0:00 to 35:01, auto-generated English) from YouTube's own transcript panel in the owner's
> Chrome. A scripted caption fetch was refused by the permission check on 2026-11-04 and was not retried by another
> route. Auto-captions garble some words ("clot", "Wales", "Macau"), so spoken numbers below were read in context.

## Chapters

| Start | Chapter |
|---|---|
| 0:00 | Intro: the three levels |
| 0:58 | Context: data, execution, intelligence |
| 2:41 | Level 1: Claude and Alpaca setup, paper trading, API keys, first trade |
| 9:27 | Level 2: automated trading bot |
| 10:05 | How the trailing stop works |
| 12:45 | Trailing stop bot on Tesla |
| 15:21 | Scheduling Claude to run automatically |
| 16:20 | Testing scenarios with Claude |
| 17:09 | Ladder buys |
| 18:19 | Gut feeling, smart money, whales |
| 19:57 | MCP as a plug for insider data |
| 20:38 | McCaul vs S&P 500 |
| 21:30 | Copy trading bot, Capitol Trades |
| 24:58 | Options and the wheel |
| 28:27 | The wheel, step by step |
| 31:32 | Why most people fail at the wheel |
| 32:08 | Wheel bot with Claude |

## Setup (0:00 to 9:27)

- Claude desktop app, Code tab, a `trading` folder, Alpaca paper account with $50,000.
- API endpoint, key and secret are pasted straight into the chat (7:03 to 7:28), then Claude is asked to save them in a
  file in the folder (8:45 to 9:12).
- First test: buy one Apple share, then swap it for Tesla, by chat.
- Stated thesis: Wall Street's edge is data, execution and intelligence, and Claude closes all three.

## Strategy 1: trailing stop with ladder buys (9:27 to 18:13)

Worked example (10:07 to 11:52): buy at $100, floor at $95. At $110 the floor moves to $105. Each rise drags the floor
up, and it never moves down. A stop-out at $95 loses $5 and the capital goes to the next setup.

Rules given to Claude (13:17 to 14:18), on Tesla, 10 shares at market:

| Rule | Value |
|---|---|
| Hard stop | Sell everything at −10% |
| Trail activation | Once price is +10% from entry |
| Trail distance | Stop 5% below current price, only moves up |
| Ladder | Spoken loosely: more shares at −20% to −30% (10 and 20 shares mentioned) |
| Later ladder | Claude proposes levels; the presenter reads −15% buy 10 and −50% buy 50 (17:43) |
| Monitoring | Claude `/schedule` task every 5 minutes, Monday to Friday from 9:00 (15:47), local, runs while the computer is on (16:14) |

Also at 16:20: role-play with Claude ("what would you do if Tesla hit $500?") to learn how the rules behave.

Internal problems:

- The −10% stop sells everything before any ladder level at −15% or lower can be reached. The video never reconciles
  this. Either the ladder never fires or it is a fresh entry after a stop-out.
- At 17:30 the presenter describes the ladder as buying gradually as the price rises. The prompt buys as it falls.
- "Every second of the day" (11:58) against a 5-minute poll that needs the computer awake.
- The 5% stop in the worked example becomes 10% in the prompt.

## Strategy 2: copy trading politicians (18:19 to 24:58)

- Claim: whales and members of Congress know something, leave a trail, and many consistently beat the market.
- Data: Capitol Trades (free site) read by Claude on a cron schedule. No MCP data feed is actually connected in the
  build, despite the "plug" explanation at 19:57.
- Prompt: find a politician with good recent results who is actively trading, copy their trades, including options.
- Claude picked Michael McCaul as the most active top trader.
- Evidence shown: "backdating" one year from $50,000. S&P 500 ends at about $57,750 (15.5%). McCaul ends at about
  $67,400 (34.8%). No source, costs, or test of any other politician or year is shown.
- Run in `bypass permissions` mode (23:28).
- Presenter accepts that disclosures are stale, and argues it does not matter because Congress holds for up to about
  two years and cannot day trade. No support is given.

## Strategy 3: the wheel (24:58 to 34:30)

Worked example on Tesla at $250: sell the $230 put for $5, get assigned, effective cost $225, sell the $260 call for $5,
get called away. Total $45 per share, or $4,500 per contract.

Prompt rules (32:35 to 34:04):

| Rule | Value |
|---|---|
| Stage 1 | Cash-secured put about 10% below price, 2 to 4 weeks out, repeat until assigned |
| Stage 2 | Covered call about 10% above cost, 2 to 4 weeks out, repeat until called away, then back to stage 1 |
| Never | Sell a put without cash for assignment; sell a call below cost basis |
| Early close | At 50% of the premium |
| Monitoring | Every 15 minutes in market hours, daily summary at the close |
| Tracking | Premium across all cycles |

Claim at 31:27: the stock can go up, down or sideways and it does not matter. This is wrong; see the report.
Claude is given strike, expiry and roll management. No result is shown; the video ends as the prompt runs.

## Viewer comments worth noting (unverified)

- Top comments flag the disclosure lag and the risk of pasting keys into Claude.
- One suggests trailing by 2 to 3 ATR rather than a fixed percentage.
- One suggests at least three weeks of paper trading before trusting a bot.
