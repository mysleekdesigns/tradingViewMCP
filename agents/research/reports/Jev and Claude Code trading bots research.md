---
title: Jev and Claude Code trading bots research
date: 2026-10-29
source: https://www.youtube.com/watch?v=87q_P6iq9mk
status: research only; P1+P2 added to PLAN.md as Phase 17, P3 as Phase 18 (2026-10-29)
---

# Jev and Claude Code trading bots research

The source is one YouTube video (ThinkTrade, 2026-09-28). It builds three stock and futures bots with Claude
Code and uses Jev (TypeSafe) to make the final take/skip call on each setup. Claim-by-claim notes, with
the untrusted-source caveat, are in
[[research_notes/Jev and Claude Code trading bots research/video_notes]]. This report asks how the video
splits work between Claude Code and Jev, what that means for this project, and what is worth adding.

## 1. How the video splits the work between Claude Code and Jev

| Layer | Video | This project today |
|---|---|---|
| Strategy rules find the setup | Python rules on market data; the AI is never asked "what to trade" | Declarative plan JSON compiled by the lab (`compile_plan`); signals on bar close, fills at next open |
| Claude Code | Writes the Python, wires data/broker/backtests/reports/dashboard, iterates from plain English | Same role, plus the research loop (Agent SDK), lab MCP tools, critic and scout subagents |
| Jev | **Trade-time judge**: gets a structured snapshot plus fixed choices (long / short / wait), returns one | **Research-time judge only**: source screening, injection screening, dedupe, sweep triage, done-check (`agents/research/gates.py`) |
| Deterministic guard | Code checks size, stops, allowed direction, risk profile before any order | Hooks deny order endpoints; `render_config` refuses anything but `dry_run: true` with empty keys |

The design principle is the same on both sides, and it is the right one: **the LLM picks from a fixed menu,
and code holds every number and every limit.** The project already does this: Jev gets typed `noul` /
`choice` / `score` questions, and arithmetic stays in Python.

**The one real gap:** Jev never sees a live trade decision here. Every Jev call is about research
artefacts, never about whether a signal on a forming market is worth taking.

## 2. Where the claims are weak

1. **The Jev backtest has look-ahead built in.** The Jev-assisted ORB backtest asks a language model about
   past QQQ/TSLA/NVDA/AMD sessions. The model's training data very likely covers those dates and tickers, so
   it can "know" which breakouts failed. TypeSafe does not publish Jev's training cutoff, as far as I know. A
   backtest of an LLM decision layer is not point-in-time, whatever the code does. This alone is enough to
   discard the "Jev beat the baseline" chart as evidence.
2. **Calibrated on the same history.** Version (c) was built after Claude studied where the baseline lost
   (trend filter, candle body, fake-out score, confidence score), then scored on the same data. That is
   in-sample fitting with an unknown number of tries. There is no holdout, no ledger and no deflation.
3. **No costs, benchmark, or null.** "37% return" over an unstated period, with no buy-and-hold or
   random-entry comparison. ORB on single stocks is high-turnover, and the video itself warns that fees eat
   AI bots.
4. **Fewer trades make luck look like skill.** About 4x fewer trades with a smoother curve is what any
   filter that skips most trades looks like over a short sample. It is weak evidence that it skipped the
   *right* ones.
5. **The other two bots have no numbers.** "Turned a really nice profit" is not a result.
6. **Unsafe key handling in the walkthrough.** Alpaca, TypeSafe and Oracle signing keys are pasted into
   the Claude chat. This project's rule is env vars only, never in chat, files or logs.

Status on the honest-backtesting checklist: next-bar fills, costs, benchmarks, walk-forward, holdout, ledger
and DSR are all unreported. **Verdict: anecdote.** The architecture is worth copying; the performance claims
are not evidence.

## 3. What is worth keeping

1. **Jev as a take/skip judge on rule-generated signals.** It is a sound idea, but it can only be tested
   **forward**, on bars after the model was trained. Backtesting it is ruled out by point 2.1 above.
2. **A decision stream.** Logging each signal with the judge's verdict, confidence and fake-out score, then
   reviewing it next to the outcome, makes a filter auditable. The project logs fills, not decisions.
3. **Fixed choice sets and a tiny job for the model.** We already do this; keep it.
4. **Session awareness.** The futures bot found most of its profit in the Asia session. Crypto is 24/7 but
   liquidity is still uneven across Asia, London and New York hours. That is a hypothesis, not a finding: it
   comes from futures, after Jev "refinement", with no numbers.

Already covered, nothing to add: the paper-first discipline, no withdraw rights (we hold no keys at all),
fees (lab cost model + cost stress), and the guardrails living in code and hooks rather than prompts.

## 4. What I would not adopt

| Video item | Why not |
|---|---|
| Alpaca, stocks, insider/news filings | Outside the crypto scope. Alpaca conflicts with the written paper-trading constraints; that decision is still pending ([[alpaca-paper-trading]]). Insider filings have no crypto equivalent in our public data. |
| Long and short entries | The lab and paper bots are spot, long-only. |
| Running Claude Code on a VPS with exchange keys | The bot itself should not be an AI agent with keys. Our dry-run bots need no keys. If 24/7 uptime is the issue, run freqtrade `--dry-run` on an always-on box, not Claude Code. |
| The "Claude Trading Desk" starter repo | Untrusted code from a promo funnel. Nothing it does is missing here. |
| Jev inside lab backtests | Look-ahead through training data, and not reproducible (`jev-latest` changes under us). |
| Hand-calibrated confidence / fake-out scores | Fit to the same history; the lab's plan rules and Optuna ledger cover tuning honestly. |

## 5. What adding it would take

### P1. Jev shadow judge on the HC paper bots (recommended, `agents/` only, not locked)

Seven HC exploratory paper tests (BTC, ETH, SOL, NEAR, AAVE, SUI, ONDO, 4h) start 2026-10-30 00:00 UTC and
decide on 2026-12-25. Their strategies and protocols are frozen, and `compare` refuses a changed strategy
file. So the judge must **watch, never steer**:

- A separate process (e.g. `uv run python paper_trade.py shadow`) that, after each 4h close, recomputes
  each candidate's entry signal from closed candles with the lab's own `compile_plan` (read-only import, as
  `compare` already does).
- On a signal, Python builds a **blind snapshot**: no symbol, no dates, no prices; only rounded ratios such as
  1/6/42-bar returns, ATR %, distance to EMAs, RSI, volume ratio, BTC 4h trend. It asks Jev one batched
  question set: `choice` take/skip, `noul` fake-out risk, `score` confidence.
- Each verdict is appended to a hash-chained JSONL (`agents/data/paper/jev-shadow/decisions.jsonl`) with the
  signal bar, the snapshot sha256, model id and latency, **before** the fill bar opens. It is committed
  regularly so it can't be quietly rewritten. Jev down = an `unavailable` row (fail open), never a skip.
- No order path, no keys besides `TYPESAFE_API_KEY` from env, no effect on the bots or their verdicts.
- **Pre-registration** (journal entry written before the first decision): metric = mean net trade return of
  Jev-take vs Jev-skip trades, matched to the paper fills `compare` already reads; a permutation test; and
  a minimum sample (e.g. at least 30 judged trades and 8 per arm, else "inconclusive").
- **Expected power is low.** A 4h strategy over 8 weeks (336 bars per symbol) trades rarely, so expect
  roughly 30–80 judged trades across all seven bots. Only a large effect would show up, and "inconclusive" or "no
  information" are correct outcomes. The HC strategies have no known edge, which actually makes this a clean
  test of whether Jev adds information: under no skill, take and skip trades should perform the same.
- Cost: one Jev call per signal, a few hundred calls at most.

### P2. Decision stream panel (web + server, after P1)

A read-only panel beside the paper ticket: per candidate, each signal with the Jev verdict, confidence,
fake-out score, the fill and (once closed) the trade return. The server reads the JSONL and the dry-run DBs;
the web app shows them. No new data source.

### P3. Session ranges overlay (web only)

Asia / London / New York session boxes and an optional UTC-day opening range, drawn from closed candles
only, through `/add-indicator`, with the no-look-ahead test. Useful for looking at the "session" idea on
the chart before spending lab budget on it.

### P4. Lab H5: UTC opening-range breakout (owner, lab lock)

This needs registry features the lab doesn't have (hour-of-day, session high/low over the first N bars of a
UTC day), added by the owner with look-ahead tests. The prior is weak: there is no opening auction in crypto,
our 5m/15m ports died on costs, and every new plan raises the DSR bar (N is 4685 already). Only worth doing
if P3 makes the session structure look real on the chart.

## 6. Proposed PLAN.md items (for the owner to add)

```
## Phase 17 — Jev Trade-Time Judge (forward only)
- [ ] Shadow process recomputes HC entry signals on closed 4h bars with compile_plan (read-only), no effect on the bots
- [ ] Blind snapshot (ratios only: no symbol, dates or prices) + one batched Jev call: take/skip, fake-out risk, confidence
- [ ] Append-only, hash-chained decisions.jsonl written before the fill bar opens; Jev unavailable = logged, fail open
- [ ] Pre-registered analysis in the journal before the first decision (metric, permutation test, minimum N)
- [ ] `paper_trade.py shadow-report`: take vs skip returns matched to paper fills, inconclusive below minimum N
- [ ] Rule: LLM decision layers are evaluated forward only, never in lab backtests
- [ ] (optional) Decision stream panel in the web app
**Done when:** the shadow judge has logged every HC signal since its start with no missing bars it could have
judged, and shadow-report reproduces from the JSONL and dry-run DBs alone.
```

## 7. Recommendation

1. **Build P1 now if you want it to see most of the HC test window.** The bots start at 2026-10-30 00:00
   UTC. A shadow judge started later just judges fewer trades; the start time goes in the pre-registration.
2. P2 after P1 has data. P3 whenever you want chart work. Hold P4.
3. Don't treat any Jev result as a reason to trade. A positive shadow result would justify a pre-registered
   second forward test, nothing more. Every report states results are historical or paper and not
   forward-looking.

> [!note] Check
> The HC bots and a shadow judge only produce clean data if the machine running them stays awake for
> 8 weeks. If they run on a laptop that sleeps, missing bars will push the verdicts toward "inconclusive
> (data)". This is the one legitimate point in the video's VPS section.
