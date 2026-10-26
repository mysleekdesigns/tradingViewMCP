"""Alpha/beta decomposition of a strategy's returns against a benchmark (PLAN.md Phase 10).

A long-only top quintile of liquid coins carries most of BTC's beta, so its raw Sharpe says
little about selection skill. This module regresses the strategy's simple per-bar returns on
the benchmark's (BTC close-to-close) by OLS,

    r_strategy[t] = a + b * r_benchmark[t] + e[t],

and reports:

* ``alpha_annual``: the intercept per bar times ``bars_per_year(interval)`` (365-day year,
  simple arithmetic annualisation, never 252, never compounded).
* ``beta``, ``r2`` and ``n`` (aligned observations).
* ``alpha_t`` / ``alpha_p``: a Newey-West (1987) HAC t-statistic of the intercept, Bartlett
  kernel with weights ``1 - l / (L + 1)``, no small-sample degrees-of-freedom scaling (with
  ``L = 0`` it is exactly White's HC0 heteroskedasticity-robust standard error). The default
  lag ``L = floor(4 * (n / 100) ** (2 / 9))`` is the Newey-West (1994) rule of thumb. The
  p-value is two-sided from a Student-t with ``n - 2`` degrees of freedom (``scipy.stats.t``),
  slightly more conservative than the normal approximation in small samples.
* ``rolling``: the same OLS on a trailing window of ``rolling_days * bars_per_day`` aligned
  rows (1d -> 60 rows for 60 days). Row t uses rows ``t - window + 1 .. t`` only, so a value
  never changes when later rows change; NaN until the window is full and wherever the
  benchmark is constant inside the window. The window counts aligned rows: if the inputs have
  gaps (missing bars are absent, never filled) a window can span more than ``rolling_days``.

Inputs are aligned on the intersection of their indexes; rows where either is NaN are dropped
(never filled). ``start`` drops rows before it, so the fit is scored from the strategy's first
decision row rather than a flat warm-up.

Pure: no I/O, no prints, no module state. Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from lab.types import SECONDS_PER_DAY, bars_per_year, interval_seconds

__all__ = ["AlphaBeta", "alpha_beta", "alpha_beta_json", "default_hac_lags"]


@dataclass(frozen=True)
class AlphaBeta:
    alpha_annual: float  # OLS intercept per bar * bars_per_year(interval), 365-day, arithmetic
    beta: float
    alpha_t: float  # Newey-West (HAC, Bartlett kernel) t-statistic of the intercept
    alpha_p: float  # two-sided p-value, Student-t with n - 2 degrees of freedom
    r2: float
    n: int  # aligned observations used
    hac_lags: int
    rolling: pd.DataFrame  # columns "alpha_annual", "beta"; NaN until the window is full


def default_hac_lags(n: int) -> int:
    """Newey-West (1994) rule of thumb ``floor(4 * (n / 100) ** (2 / 9))``."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))


def _check_series(name: str, s: object) -> pd.Series:
    if not isinstance(s, pd.Series):
        raise ValueError(f"{name} must be a pd.Series of per-bar returns, got {type(s).__name__}")
    if not s.index.is_unique:
        raise ValueError(f"{name} index has duplicate labels")
    if not pd.api.types.is_numeric_dtype(s.dtype):
        raise ValueError(f"{name} must be numeric, got dtype {s.dtype}")
    return s.astype(float)


def _align(
    strategy: pd.Series, benchmark: pd.Series, start: pd.Timestamp | None
) -> tuple[pd.Index, np.ndarray, np.ndarray]:
    y = _check_series("strategy", strategy)
    x = _check_series("benchmark", benchmark)
    both = pd.concat([y.rename("y"), x.rename("x")], axis=1, join="inner").sort_index()
    both = both.replace([np.inf, -np.inf], np.nan).dropna()
    if start is not None:
        both = both.loc[both.index >= start]
    return both.index, both["y"].to_numpy(float), both["x"].to_numpy(float)


def _hac_cov(design: np.ndarray, e: np.ndarray, lags: int) -> np.ndarray:
    """Newey-West sandwich ``(X'X)^-1 S (X'X)^-1`` with Bartlett weights, no dof scaling."""
    u = design * e[:, None]  # score contributions, n x k
    meat = u.T @ u
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = u[lag:].T @ u[:-lag]
        meat += w * (gamma + gamma.T)
    bread = np.linalg.inv(design.T @ design)
    return bread @ meat @ bread


def _rolling(
    index: pd.Index, y: np.ndarray, x: np.ndarray, window: int, bpy: float
) -> pd.DataFrame:
    out = pd.DataFrame({"alpha_annual": np.nan, "beta": np.nan}, index=index, dtype=float)
    if len(y) < window:
        return out
    ys = pd.Series(y, index=index)
    xs = pd.Series(x, index=index)
    roll_x = xs.rolling(window, min_periods=window)
    var_x = roll_x.var(ddof=0)
    cov_xy = xs.rolling(window, min_periods=window).cov(ys, ddof=0)
    mean_x = roll_x.mean()
    mean_y = ys.rolling(window, min_periods=window).mean()
    # A window whose benchmark is constant has no beta. The rolling variance of a constant
    # window comes out as round-off (~1e-20) instead of 0, so test constancy exactly
    # (max == min inside the window) rather than with the variance.
    ok = roll_x.max() > roll_x.min()
    beta = (cov_xy / var_x).where(ok)
    alpha = (mean_y - beta * mean_x) * bpy
    out["beta"] = beta.to_numpy(float)
    out["alpha_annual"] = alpha.to_numpy(float)
    return out


def alpha_beta(
    strategy: pd.Series,
    benchmark: pd.Series,
    *,
    interval: str,
    rolling_days: int = 60,
    hac_lags: int | None = None,
    start: pd.Timestamp | None = None,
) -> AlphaBeta:
    """OLS alpha/beta of ``strategy`` on ``benchmark`` (simple per-bar returns), full period
    with a Newey-West t-statistic, plus a trailing ``rolling_days`` fit. See the module doc."""
    bpy = bars_per_year(interval)  # raises ValueError naming a bad interval
    if isinstance(rolling_days, bool) or not isinstance(rolling_days, int) or rolling_days < 2:
        raise ValueError(f"rolling_days must be an int >= 2, got {rolling_days!r}")
    index, y, x = _align(strategy, benchmark, start)
    n = len(y)
    if n < 3:
        raise ValueError(
            f"strategy and benchmark have {n} aligned non-NaN rows"
            + (f" from start {start}" if start is not None else "")
            + "; need at least 3"
        )
    if np.ptp(x) == 0.0:
        raise ValueError("benchmark has zero variance over the aligned rows; beta is undefined")
    if hac_lags is None:
        lags = min(default_hac_lags(n), n - 1)
    else:
        if isinstance(hac_lags, bool) or not isinstance(hac_lags, int) or not 0 <= hac_lags < n:
            raise ValueError(f"hac_lags must be an int in [0, {n - 1}], got {hac_lags!r}")
        lags = hac_lags

    xm, ym = x.mean(), y.mean()
    dx = x - xm
    beta = float(dx @ (y - ym) / (dx @ dx))
    alpha = float(ym - beta * xm)
    e = y - alpha - beta * x
    sst = float(((y - ym) ** 2).sum())
    r2 = float(1.0 - (e @ e) / sst) if sst > 0 else math.nan

    design = np.column_stack([np.ones(n), x])
    cov = _hac_cov(design, e, lags)
    var_a = float(cov[0, 0])
    se = math.sqrt(var_a) if var_a > 0 else 0.0
    if se > 0:
        t = alpha / se
        p = float(2.0 * sp_stats.t.sf(abs(t), df=n - 2))
    elif alpha != 0:  # exact fit: the intercept is known without error
        t, p = math.copysign(math.inf, alpha), 0.0
    else:
        t, p = math.nan, math.nan

    window = round(rolling_days * SECONDS_PER_DAY / interval_seconds(interval))
    rolling = _rolling(index, y, x, window, bpy)
    return AlphaBeta(
        alpha_annual=alpha * bpy,
        beta=beta,
        alpha_t=float(t),
        alpha_p=p,
        r2=r2,
        n=n,
        hac_lags=lags,
        rolling=rolling,
    )


def _f(v: float) -> float | None:
    v = float(v)
    return v if math.isfinite(v) else None


def _time(label: object) -> object:
    if isinstance(label, pd.Timestamp):
        return label.isoformat()
    if isinstance(label, np.integer | np.floating):
        return label.item()
    return label if isinstance(label, int | float | str) else str(label)


def alpha_beta_json(ab: AlphaBeta, *, rolling_points: int = 0) -> dict[str, Any]:
    """JSON-safe dict (non-finite floats -> ``None``). ``rolling_summary`` describes the
    full-window rolling rows; with ``rolling_points > 0``, ``rolling`` holds at most that many
    of them, evenly spaced and always including the last (time as ISO-8601)."""
    if isinstance(rolling_points, bool) or not isinstance(rolling_points, int):
        raise ValueError(f"rolling_points must be an int >= 0, got {rolling_points!r}")
    if rolling_points < 0:
        raise ValueError(f"rolling_points must be >= 0, got {rolling_points}")
    full = ab.rolling.dropna(subset=["beta"])
    if len(full):
        summary: dict[str, Any] = {
            "rows": len(full),
            "first": _time(full.index[0]),
            "last": _time(full.index[-1]),
            "beta_min": _f(full["beta"].min()),
            "beta_median": _f(full["beta"].median()),
            "beta_max": _f(full["beta"].max()),
            "beta_last": _f(full["beta"].iloc[-1]),
            "alpha_annual_min": _f(full["alpha_annual"].min()),
            "alpha_annual_median": _f(full["alpha_annual"].median()),
            "alpha_annual_max": _f(full["alpha_annual"].max()),
            "alpha_annual_last": _f(full["alpha_annual"].iloc[-1]),
        }
    else:
        summary = {"rows": 0}
    points: list[dict[str, Any]] = []
    if rolling_points > 0 and len(full):
        k = min(rolling_points, len(full))
        pos = np.unique(np.round(np.linspace(0, len(full) - 1, k)).astype(int))
        for i in pos:
            points.append(
                {
                    "time": _time(full.index[i]),
                    "alpha_annual": _f(full["alpha_annual"].iloc[i]),
                    "beta": _f(full["beta"].iloc[i]),
                }
            )
    return {
        "alpha_annual": _f(ab.alpha_annual),
        "beta": _f(ab.beta),
        "alpha_t": _f(ab.alpha_t),
        "alpha_p": _f(ab.alpha_p),
        "r2": _f(ab.r2),
        "n": int(ab.n),
        "hac_lags": int(ab.hac_lags),
        "rolling_summary": summary,
        "rolling": points,
    }
