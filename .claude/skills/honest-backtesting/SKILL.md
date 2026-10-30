---
name: honest-backtesting
description: Checklist and report template for evaluating a crypto strategy honestly - next-bar fills, costs and cost stress, 365-day annualization, benchmarks (buy-and-hold, vol-targeted buy-and-hold, random-entry null), walk-forward, locked holdout, trial ledger, Deflated Sharpe, PBO, adversarial checks. Use when writing or reviewing lab engine/stats code, when reporting Sharpe, drawdown, or P&L for any strategy, or when the user asks whether a result is real, significant, or overfit.
---

# Honest backtesting (crypto)

A result is reportable only when every line below is present. Missing lines are reported as **missing**, not skipped. The default conclusion is "no edge after costs" until the evidence says otherwise.

## Checklist

```
Backtest honesty:
- [ ] No look-ahead: signal from bar t close, fill at bar t+1 open; perturbation test passes for every feature in the plan
- [ ] Costs: taker fee + slippage per side on traded value; net beside gross; cost stress ×2 and ×4
- [ ] Annualization: 365 days × bars/day for the interval (never √252)
- [ ] Data: gaps untradeable (no forward fill); point-in-time universe; no survivorship
- [ ] Turnover, exposure (time in market), trade count (≥ ~100 to mean anything)
- [ ] Benchmarks: buy-and-hold BTC, volatility-targeted buy-and-hold, random-entry null with the same trade count
- [ ] Walk-forward out-of-sample metrics, not just in-sample
- [ ] Trial ledger: N trials for this run (every backtest + every Optuna trial); DSR computed with that N
- [ ] PBO via CSCV; stationary-bootstrap CI on returns
- [ ] Adversarial: signal delay 1/3/5 bars (should decay gradually), sign flip, Monte Carlo trade reshuffle, permutation null through the full pipeline
- [ ] Robustness: ±20% parameter neighbours stay profitable; regime split (bull/bear/sideways)
- [ ] Locked holdout: untouched by search; if checked, count against the budget and report separately
- [ ] No LLM decision layer inside the backtest: an LLM take/skip judge (Jev or Claude) is evaluated forward only, on bars after its training cutoff, never on history it may remember
- [ ] Disclaimer: historical, not forward-looking
```

## Success criteria

The candidate bar lives in `PLAN.md` → Phase 8 → **Success criteria** (and in `lab/config/success_criteria*` once it exists; that file wins). Read it from there each time instead of from memory. Never relax it mid-run.

## Rules of thumb

- Strong in-sample + weak walk-forward = overfit. Report the walk-forward number as the headline.
- Win rates of 80–95% with tiny drawdowns are red flags (overfitting or look-ahead), not proof.
- An edge that survives a 5-bar delay **unchanged** is as suspicious as one that vanishes after 1 bar.
- If the edge disappears at ×2 costs, the cost assumption is doing the work.
- Vol-targeted buy-and-hold is the strongest simple benchmark in crypto. Beating plain buy-and-hold is not enough.
- Never tune on the holdout. If you looked at it, it is no longer a holdout; say so.
- Weight results after the model's training cutoff; LLMs remember price history.
- Report distributions (trade P&L, null, bootstrap), not just means.

## Report template

Use [reference/report-template.md](reference/report-template.md) for every strategy write-up and for the end-of-run "no edge found" report.
