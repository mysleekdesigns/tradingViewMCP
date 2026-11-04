"""Correlation across concurrent paper tests (PLAN.md Phase 20 Part F). Information only.

Pairwise Pearson correlations of daily UTC returns and the effective number of independent bets,
``N_eff = (sum lambda)^2 / sum lambda^2`` over the correlation matrix's eigenvalues. Nothing here
changes a verdict. Passing tests on correlated coins are one piece of evidence, not N.

Every series is read on the dates common to all usable series; gaps are never filled. A series
that is too short or has zero variance has no defined correlation, so it is excluded and named,
never treated as correlated or independent.

A bot is flat most days, and two flat series share nothing to correlate on: seven coins with
pairwise correlation 0.8 read N_eff 5.03 at 25% in market, against about 1.45 for the coins
themselves. So the figure to quote comes from the coins' own daily returns over the same window;
the bots' figure stays beside it, with the days each pair was in the market together.

With d common days a correlation matrix has rank at most d - 1, so with d <= n series it cannot
show n independent bets: N_eff then reports the window, not the coins, and the report says "too
few days to quote" instead of giving a figure to quote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import combinations

import numpy as np
import pandas as pd

METHOD = "Pearson, daily UTC returns, common dates"
MIN_COMMON_DAYS = 3  # two points always correlate at +/-1, so three is the first informative count
FEW_DAYS_WARNING = 30  # below a month of days a sample correlation is mostly noise
ZERO_VARIANCE_TOL = 1e-12  # sd of daily returns; a flat book differs from zero only by rounding
EIGENVALUE_TOL = 1e-8  # a valid correlation matrix goes this far negative on rounding alone


def n_eff(corr: np.ndarray) -> float:
    """Effective number of independent bets of a symmetric correlation matrix.

    Identity of size n gives n; all ones gives 1. Eigenvalues slightly below zero are rounding
    and clamp to zero; more negative ones mean ``corr`` is not a correlation matrix.
    """
    matrix = np.asarray(corr, dtype="float64")
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.size == 0:
        raise ValueError(f"corr must be a non-empty square matrix, got shape {matrix.shape}")
    if not np.isfinite(matrix).all():
        raise ValueError("corr has non-finite entries")
    if not np.allclose(matrix, matrix.T):
        raise ValueError("corr must be symmetric")
    eigenvalues = np.linalg.eigvalsh(matrix)
    if eigenvalues.min() < -EIGENVALUE_TOL:
        raise ValueError(f"corr has a negative eigenvalue {eigenvalues.min():g}")
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    squares = float(np.sum(eigenvalues**2))
    if squares == 0:
        raise ValueError("corr has no positive eigenvalue")
    return float(np.sum(eigenvalues)) ** 2 / squares


@dataclass(frozen=True)
class PortfolioReport:
    names: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]  # (name, why), e.g. zero variance
    start: str
    end: str
    n_days: int
    method: str
    correlation: tuple[tuple[float, ...], ...]
    n_eff: float | None  # None with fewer than 2 usable series
    coins: PortfolioReport | None = None  # the same tests' coins, same window: the one to quote
    days_both_in_market: tuple[tuple[str, str, int], ...] = ()  # (name, name, days) in the window

    @property
    def too_few_days(self) -> bool:
        """The matrix's rank cannot reach the series count, so ``n_eff`` is not to be quoted."""
        return bool(self.names) and self.n_days <= len(self.names)


def _daily_series(name: str, returns: pd.Series) -> pd.Series:
    """One return per UTC date, NaN days dropped (absent from the common dates, never filled)."""
    if not isinstance(returns, pd.Series) or not isinstance(returns.index, pd.DatetimeIndex):
        raise ValueError(f"daily_returns[{name!r}] must be a Series with a DatetimeIndex")
    index = returns.index
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    index = index.normalize()
    if not index.is_unique:
        raise ValueError(f"daily_returns[{name!r}] has more than one return for a UTC date")
    values = returns.to_numpy(dtype="float64", na_value=np.nan)
    if np.isinf(values).any():
        raise ValueError(f"daily_returns[{name!r}] has an infinite return")
    return pd.Series(values, index=index).dropna().sort_index()


def _common(series: Mapping[str, pd.Series]) -> pd.DatetimeIndex:
    columns = iter(series.values())
    common = next(columns).index
    for column in columns:
        common = common.intersection(column.index)
    return common


def _report(
    daily_returns: Mapping[str, pd.Series], excluded: Mapping[str, str]
) -> tuple[PortfolioReport, pd.DatetimeIndex]:
    """Correlations and N_eff of the usable series on their common dates, and those dates.

    Exclusion repeats until stable, because dropping one series can widen the common dates. A
    too-short overlap drops the series with the fewest days first (ties: the later name)."""
    usable: dict[str, pd.Series] = {}
    dropped = dict(excluded)
    for name in sorted(daily_returns):
        series = _daily_series(name, daily_returns[name])
        if len(series) < MIN_COMMON_DAYS:
            dropped[name] = f"too short: {len(series)} days, need {MIN_COMMON_DAYS}"
        else:
            usable[name] = series

    common = pd.DatetimeIndex([])
    while usable:
        common = _common(usable)
        if len(common) < MIN_COMMON_DAYS:
            drop = min(reversed(list(usable)), key=lambda n: len(usable[n]))
            dropped[drop] = (
                f"too short: {len(common)} days in common with the others, need {MIN_COMMON_DAYS}"
            )
            del usable[drop]
            continue
        flat = [n for n, s in usable.items() if float(s.loc[common].std()) <= ZERO_VARIANCE_TOL]
        if not flat:
            break
        for name in flat:
            dropped[name] = f"zero variance over {len(common)} common days"
            del usable[name]

    names = tuple(usable)
    if not names:
        correlation: tuple[tuple[float, ...], ...] = ()
        common = pd.DatetimeIndex([])
    else:
        table = np.column_stack([usable[n].loc[common].to_numpy() for n in names])
        matrix = np.clip(np.atleast_2d(np.corrcoef(table, rowvar=False)), -1.0, 1.0)
        correlation = tuple(tuple(float(x) for x in row) for row in matrix)
    report = PortfolioReport(
        names=names,
        excluded=tuple(sorted(dropped.items())),
        start=common[0].date().isoformat() if len(common) else "",
        end=common[-1].date().isoformat() if len(common) else "",
        n_days=len(common),
        method=METHOD,
        correlation=correlation,
        n_eff=n_eff(np.array(correlation)) if len(names) >= 2 else None,
    )
    return report, common


def _on_dates(name: str, series: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """``series`` on ``dates`` only (every date it has, when ``dates`` is empty)."""
    daily = _daily_series(name, series)
    return daily if dates.empty else daily.loc[daily.index.isin(dates)]


def portfolio_report(
    daily_returns: Mapping[str, pd.Series],
    *,
    coin_returns: Mapping[str, pd.Series] | None = None,
    in_market: Mapping[str, pd.Series] | None = None,
    excluded: Mapping[str, str] | None = None,
) -> PortfolioReport:
    """Correlations and N_eff of the bots' series, plus (given ``coin_returns``) the same for each
    test's coin over the bots' window, plus (given ``in_market``, per test a daily bool) the days
    each pair of tests was in the market together. ``excluded`` names tests left out upfront. A
    test whose bot never moved is out of the bots' figure but still has a coin; with no usable
    bot at all the coins use their own common dates."""
    bots, common = _report(daily_returns, excluded or {})
    coins = None
    window = common
    if coin_returns is not None:
        clipped = {n: _on_dates(n, s, common) for n, s in coin_returns.items()}
        coins, coin_days = _report(clipped, {})
        window = coin_days
    days: list[tuple[str, str, int]] = []
    if in_market is not None:
        held = {n: _in_market_dates(n, s) for n, s in in_market.items()}
        for one, two in combinations(sorted(held), 2):
            days.append((one, two, len(held[one].intersection(held[two]).intersection(window))))
    return replace(bots, coins=coins, days_both_in_market=tuple(days))


def _in_market_dates(name: str, held: pd.Series) -> pd.DatetimeIndex:
    daily = _daily_series(name, held.astype("float64"))
    return pd.DatetimeIndex(daily.index[daily.to_numpy() > 0])


def _too_few_days(report: PortfolioReport, what: str) -> str:
    return (
        f"too few days to quote: {report.n_days} common days for {len(report.names)} {what}, "
        "and the matrix's rank cannot pass the days minus one"
    )


def _matrix(names: tuple[str, ...], correlation: tuple[tuple[float, ...], ...]) -> list[str]:
    width = max(*(len(n) for n in names), 6)
    lines = [" " * width + "".join(f"  {n:>{width}}" for n in names)]
    for name, row in zip(names, correlation, strict=True):
        lines.append(f"{name:<{width}}" + "".join(f"  {x:>{width}.2f}" for x in row))
    return lines


def render_portfolio(report: PortfolioReport) -> str:
    """Plain-text report. Information only: it changes no verdict."""
    lines = ["Portfolio correlation (information only; no verdict changes)"]
    window = report if report.names or report.coins is None else report.coins
    if window.names:
        lines.append(f"Window: {window.start} to {window.end} ({window.n_days} days)")
    else:
        lines.append("Window: none (no usable series)")
    lines.append(f"Method: {report.method}")
    if (coins := report.coins) is not None:
        if coins.n_eff is None:
            lines.append("N_eff to quote: not available (fewer than 2 usable coins)")
        elif coins.too_few_days:
            lines.append(f"N_eff to quote: none, {_too_few_days(coins, 'coins')}")
            lines.extend(_matrix(coins.names, coins.correlation))
        else:
            lines.append(
                f"N_eff to quote (the coins' own daily returns): {coins.n_eff:.2f} of "
                f"{len(coins.names)} coins, {coins.n_days} days"
            )
            lines.extend(_matrix(coins.names, coins.correlation))
        for name, why in coins.excluded:
            lines.append(f"Coin of {name} excluded: {why}")
    if report.n_eff is None:
        lines.append("N_eff: not available (fewer than 2 usable series)")
    elif coins is None and report.too_few_days:
        lines.append(f"N_eff: {_too_few_days(report, 'series')}")
        lines.extend(_matrix(report.names, report.correlation))
    else:
        label = "N_eff" if coins is None else "N_eff of the bots' returns (do not quote)"
        lines.append(f"{label}: {report.n_eff:.2f} of {len(report.names)} series")
        if coins is not None:
            lines.append(
                "A mostly flat bot shares few days with any other, so its series reads as "
                "independent whatever its coin does."
            )
        lines.extend(_matrix(report.names, report.correlation))
    if report.days_both_in_market:
        lines.append("Days both in the market:")
        for one, two, days in report.days_both_in_market:
            lines.append(f"  {one} / {two}: {days}")
    if window.names and window.n_days < FEW_DAYS_WARNING:
        lines.append(f"Caution: only {window.n_days} common days, so these estimates are noisy.")
    for name, why in report.excluded:
        lines.append(f"Excluded {name}: {why}")
    lines.append(
        "Short windows bias N_eff low: even independent series read below their count until the "
        "window is several times longer than the number of series."
    )
    lines.append(
        "Information only: confirmations across correlated coins are one piece of evidence, not N."
    )
    return "\n".join(lines) + "\n"
