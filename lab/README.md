# lab — honest crypto backtesting

The Phase 7 research lab: declarative strategy plans, a vectorbt engine with next-bar fills and
costs, a trial ledger, and the statistics that say whether a result is real (DSR, PBO, bootstrap
CIs, benchmarks, adversarial checks). The web app's Strategy Tester and the Phase 8 agents both
use it. **Results are historical simulations, not forecasts. Nothing here places orders.**

## Setup

```bash
cd lab
uv sync                                                    # Python 3.12, hedgeFund as an editable dep
uv run python -m lab.data.archive BTCUSDT 4h 2018-01 2026-09   # Binance public archive -> data/klines/*.parquet
```

Data comes from `data.binance.vision` (Binance global spot), verified against the published
SHA-256 checksums. Missing bars stay missing (never forward-filled); zero-volume bars are kept but
marked untradeable. The server's SQLite cache holds Binance.US bars and is never mixed in.

## Run

| What | Command |
|---|---|
| API for the web app (127.0.0.1:8788, proxied at `/lab`) | `uv run python -m lab.api` (root `npm run dev` starts it too) |
| Optuna search on the design window | `uv run python -m lab.search --plan plan.json --run-id my-run [--walk-forward]` |
| Event-driven cross-check (nautilus_trader) | `uv run python -m lab.crosscheck --plan plan.json` |
| Tests / lint | `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format .` |

## A plan

```json
{
  "name": "ema_cross_20_50", "symbol": "BTCUSDT", "interval": "4h",
  "features": { "ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}} },
  "long_entry": "crosses_above(ema_fast, ema_slow)",
  "long_exit":  "crosses_below(ema_fast, ema_slow)",
  "risk": { "position_size_pct": 100, "stop_loss_pct": null }
}
```

Features come only from the registry (`GET /lab/registry`); expressions are parsed by a strict
whitelist (`crosses_above`, `crosses_below`, `prev`, comparisons, `and/or/not`, arithmetic) and
never `eval`-ed. Long/flat only (spot).

## Rules the lab enforces

- **No look-ahead:** a signal at bar t uses data through t's close and fills at the next tradable
  bar's open. Every registry feature passes the perturbation test.
- **Costs:** 0.1% taker fee + slippage (0.05% base, widened on unusually wide bars, capped at 1%)
  per side; net is always shown beside gross; cost stress x2/x4.
- **Calendar:** 365-day annualisation.
- **Splits:** design window 2019-01-01 → 2025-10-01; everything from 2025-10-01 is a locked
  holdout that the API, search and tuning code never read (`config/lab.toml`).
- **Ledger:** every backtest (including failures, neighbours, adversarial variants and Optuna
  trials) is a row in `data/ledger.db`, append-only. DSR's N = distinct plans tried in the run.
- **Benchmarks:** buy-and-hold, volatility-targeted buy-and-hold, random-entry null with the same
  trade count.

Constants that change results live in `config/lab.toml`, each with a one-line justification.
