"""Backtest engine: next-bar fills, per-side costs, intrabar stops (pure; no I/O)."""

from lab.engine.backtest import run_signals, run_weights
from lab.engine.costs import slippage_series, validate_costs

__all__ = ["run_signals", "run_weights", "slippage_series", "validate_costs"]
