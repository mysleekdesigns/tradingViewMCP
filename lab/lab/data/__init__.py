"""Historical market data for the lab: Binance archive -> Parquet, loading, gaps, universe.

This package does I/O (HTTP, Parquet, SQLite). The engine/registry/stats packages stay pure and
receive the candle frames produced here (convention in ``lab.types``).

Re-exports are lazy so ``python -m lab.data.archive`` does not import the module twice.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "archive_url": "lab.data.archive",
    "download_month": "lab.data.archive",
    "download_range": "lab.data.archive",
    "parse_kline_csv": "lab.data.archive",
    "find_gaps": "lab.data.store",
    "load_candles": "lab.data.store",
    "eligibility_mask": "lab.data.universe",
    "eligible_at": "lab.data.universe",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        return getattr(importlib.import_module(_EXPORTS[name]), name)
    raise AttributeError(f"module 'lab.data' has no attribute {name!r}")
