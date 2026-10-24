"""Event-driven cross-check of the vectorbt engine (nautilus_trader, imported lazily).

Results are historical and not forward-looking.
"""

from lab.crosscheck.nautilus import (
    ENGINE_NAME,
    NOT_INSTALLED,
    CrossCheckResult,
    TradeComparison,
    crosscheck,
    price_precision,
    slipped_ticks,
    split_symbol,
)

__all__ = [
    "ENGINE_NAME",
    "NOT_INSTALLED",
    "CrossCheckResult",
    "TradeComparison",
    "crosscheck",
    "price_precision",
    "slipped_ticks",
    "split_symbol",
]
