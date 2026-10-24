---
paths:
  - "lab/lab/engine/**"
  - "lab/lab/registry/**"
  - "lab/lab/stats/**"
  - "lab/tests/**"
  - "web/src/indicators/**"
---

# No look-ahead, honest crypto backtests

- A signal at bar t uses data through the **close** of bar t only. The engine fills on bar t+1's **open**. Indicators, scalers, regime fits, thresholds, and universe eligibility used at t are computed from data at or before t.
- Every registry feature and every indicator ships with the perturbation test: change prices after bar t, assert every output up to t is bit-identical. Statistics cannot catch look-ahead (a leaky Sharpe-35 strategy passed every overfitting test), so this test is mandatory.
- Annualize with **365 days** (crypto never closes) and the bar count per year for the interval, never √252.
- Costs: taker fee (0.1% default) plus slippage, charged on traded value per side. Never report a gross-only metric; net sits beside gross with the assumption stated.
- Gaps (delistings, suspensions) are marked untradeable, never forward-filled. Use a point-in-time universe (a coin is eligible only after its history requirement is met).
- The locked holdout is never read by search, Optuna, or tuning code. Only `holdout_check` touches it, and it enforces its budget server-side.
- Every backtest is written to the trial ledger, including failed and Optuna trials. DSR uses the run's full trial count.
- Benchmarks always reported: buy-and-hold BTC, volatility-targeted buy-and-hold, and a random-entry null with the same trade count.
- Use the `honest-backtesting` skill for the full checklist and report template.
