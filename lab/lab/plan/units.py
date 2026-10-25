"""Units (scales) of plan expressions, so a rule cannot hide a price or volume level.

A rule like ``close > 50000`` compares a price with a fixed number. On a trending market a
price level is a date switch: it is True only in the periods when price was above 50000, so it
lets a plan pick history by calendar while looking like a technical rule (and it reaches into
the holdout through the price path). The validator therefore tags every numeric expression
with a dimension and rejects comparisons whose two sides have different units.

A dimension is ``(price exponent, volume exponent)``:

* ``PRICE`` ``(1, 0)``: ``open high low close`` and features whose outputs are in quote-price
  units (moving averages, bands, channels, ATR, MACD lines, which are price differences);
* ``VOLUME`` ``(0, 1)``: ``volume`` and its moving average;
* ``DIMENSIONLESS`` ``(0, 0)``: bounded or normalised features (RSI, ROC in percent, z-score,
  realised volatility of log returns, regime outputs) and nonzero numeric literals.

Arithmetic: ``+``/``-`` need equal dimensions; ``*`` adds and ``/`` subtracts exponents, so
``close / ema_slow`` is dimensionless and ``4 * volume_sma`` is volume-scale. The literal
``0`` fits any dimension (a sign test such as ``macd.hist > 0`` is scale-free). ``prev(x, n)``
keeps ``x``'s dimension; ``n`` is a bar count, not a level.

The mapping below is checked against :data:`lab.registry.REGISTRY` by the tests, so a new
registry feature cannot slip in without a declared scale.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

Dim = tuple[int, int]

PRICE: Dim = (1, 0)
VOLUME: Dim = (0, 1)
DIMENSIONLESS: Dim = (0, 0)

INPUT_SCALES: Mapping[str, Dim] = MappingProxyType(
    {"open": PRICE, "high": PRICE, "low": PRICE, "close": PRICE, "volume": VOLUME}
)

# registry feature -> output -> dimension (lab/lab/registry/features.py, regime.py)
FEATURE_SCALES: Mapping[str, Mapping[str, Dim]] = MappingProxyType(
    {
        "sma": {"value": PRICE},  # rolling mean of close
        "ema": {"value": PRICE},  # seeded EMA of close
        "rsi": {"value": DIMENSIONLESS},  # 0..100
        "atr": {"value": PRICE},  # Wilder mean of true range (a price difference)
        "bollinger": {"mid": PRICE, "upper": PRICE, "lower": PRICE},  # SMA +/- k * stdev
        "macd": {"macd": PRICE, "signal": PRICE, "hist": PRICE},  # EMA - EMA differences
        "donchian": {"upper": PRICE, "lower": PRICE},  # highest high / lowest low
        "roc": {"value": DIMENSIONLESS},  # 100 * (close - past) / past
        "realized_vol": {"value": DIMENSIONLESS},  # stdev of log returns
        "zscore": {"value": DIMENSIONLESS},  # (close - mean) / stdev
        "volume_sma": {"value": VOLUME},  # rolling mean of volume
        "regime_state": {"value": DIMENSIONLESS},  # -1 / 0 / +1
        "regime_signal": {"value": DIMENSIONLESS},  # P(Bull) - P(Bear) in [-1, 1]
    }
)


def output_scale(feature: str, output: str) -> Dim | None:
    """The dimension of ``feature``'s ``output``; None when the feature is not in the table
    (a test-only registry), in which case comparisons involving it are not unit-checked."""
    outs = FEATURE_SCALES.get(feature)
    return None if outs is None else outs.get(output)


def describe(dim: Dim) -> str:
    """``"price-scale"``, ``"volume-scale"``, ``"dimensionless"``, or e.g. ``"price x volume"``,
    ``"1/price"``."""
    if dim == PRICE:
        return "price-scale"
    if dim == VOLUME:
        return "volume-scale"
    if dim == DIMENSIONLESS:
        return "dimensionless"
    num: list[str] = []
    den: list[str] = []
    for exp, word in zip(dim, ("price", "volume"), strict=True):
        part = word if abs(exp) == 1 else f"{word}^{abs(exp)}"
        if exp > 0:
            num.append(part)
        elif exp < 0:
            den.append(part)
    top = " x ".join(num) if num else "1"
    return f"{top}/{' x '.join(den)}" if den else top


__all__ = [
    "DIMENSIONLESS",
    "FEATURE_SCALES",
    "INPUT_SCALES",
    "PRICE",
    "VOLUME",
    "Dim",
    "describe",
    "output_scale",
]
