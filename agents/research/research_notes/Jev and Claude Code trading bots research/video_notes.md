# Video notes: "I Turned JEV AI Into A 24/7 Stock Trader (Full Tutorial)"

> [!warning] Untrusted source
> YouTube, channel ThinkTrade (about 1.5k subscribers), uploaded 2026-09-28, 35:56 long, about 85k views
> when read on 2026-10-29. URL: https://www.youtube.com/watch?v=87q_P6iq9mk. It promotes a free Gumroad
> "starter pack" (prompt guide + a "Claude Trading Desk" GitHub repo). I did not open or install either.
> No code, data, trade list, cost model or per-strategy statistics are shown in enough detail to reproduce.
> These notes summarise the claims. They are not findings of this project. I saw no text aimed at AI agents.
> I read the transcript from YouTube's own transcript panel in the owner's Chrome.

## The architecture it describes (00:58–3:45)

1. **Rules first.** The strategy code reads market data and decides whether a valid setup exists. The AI is
   not asked "what should I trade?".
2. **Claude Code** writes each idea as Python, connects data and the broker, builds backtests and reports,
   and wires up paper trading. The owner describes the strategy in plain English and iterates.
3. **Jev gets a small job.** When a setup fires, the code sends Jev a structured market snapshot and a
   fixed set of choices (on a breakout: enter long / enter short / wait). Jev returns one choice.
4. **A deterministic engine has the final say.** Before any order, code checks position size, stops,
   allowed directions and the risk profile. Jev "doesn't get free rein".
5. Jev answers in about 0.25 s, so many setups can be checked. Data, program and broker latency come on top.

## Strategies and claimed results

| Bot | What it does | Claim |
|---|---|---|
| ORB (stocks: QQQ, TSLA, NVDA, AMD, ...) | 30-minute opening range; long on a break above, short on a break below | Three versions on $10k each: (a) a baseline Claude wrote from a loose prompt; (b) the owner's hand rules (about 5 if-thens, e.g. no break in 30 min = no trade that day): fewer trades and *less* profit than (a); (c) Jev-assisted: about 4x fewer trades, much lower drawdown, higher profit, "never really in a substantial drawdown". Later quoted as "37% return" and a slightly higher win rate. |
| ORB "calibration" | Claude reviewed where the baseline went wrong, added higher-timeframe trend and breakout-candle body, a risk/outcome comparison, a fake-out risk score and a confidence score that feed Jev's decision | Shown as a decision log per trade (e.g. QQQ short: high confidence, high fake-out risk). |
| Insider/news | Real-time headlines plus new insider filings; filter > $10k and executive-level filers; copy the filer's direction, hold 5 sessions, sell | "Turned a really nice profit, better than something I would do technically." No numbers shown. |
| Futures (ES/NQ) | Owner's "LVL" reversal strategy; Jev refinement found most profit in the Asia session; extra confirmation; cross-index check (if NQ swept a prior high, use it in the ES decision) | No numbers shown. |

All three run at once on separate accounts, then go to 30 days of paper trading.

## Operations and risk advice (12:50–19:40)

- Paper trade first; "don't hand money to an AI blindly".
- For 24/7 running, move the bots to a VPS/cloud (Oracle Cloud free tier) with the same environment as
  the laptop, and run Claude Code there.
- Keys: trading-only permissions, IP-locked to the server, no withdraw/deposit rights.
- Fees: an AI that makes many decisions racks up commission; "you'll get eaten by fees" without discretion.
- Guardrails in the Claude rules: max drawdown, max loss, no-trade days and times. "The AI should not have
  that much freedom."
- A "mission control" dashboard with the decision history, so you can review decisions you disagree with
  and iterate.

## Setup walkthrough (21:00–35:00)

VS Code + Claude Code extension; a new workspace folder; import the "Claude Trading Desk" repo and let
Claude install its dependencies; create an Alpaca paper account ($10k) and API keys; create a TypeSafe
API key; Claude runs the bundled ORB backtest ("37% return"); the owner picks "Jev top's pick" and a stricter
last-entry cutoff of 11:45; Oracle Cloud keys are obtained from Oracle's console AI and pasted back and
forth with Claude; the dashboard goes live, heartbeat every 2 s, $10k equity, no trades yet.

## Things I noticed

- The only numbers are for ORB, and they are one backtest per version. No cost model, no benchmark, no
  out-of-sample period, no count of the variants tried.
- Version (c) was built after looking at where (a) failed, on the same history.
- The Jev decisions in the backtest were made by a model whose training data probably covers that history
  and those tickers (TypeSafe does not publish Jev's training cutoff as far as I know).
- API and cloud signing keys are pasted into the chat in plain text.
