"""Paper portfolio correlation (PLAN.md Phase 20 Part F): N_eff on known matrices, common dates
only, nothing filled, and excluded series named rather than scored."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.paper.portfolio import (
    METHOD,
    MIN_COMMON_DAYS,
    PortfolioReport,
    n_eff,
    portfolio_report,
    render_portfolio,
)


def daily(values: list[float], start: str = "2026-01-01") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq="D", tz="UTC"),
        dtype="float64",
    )


def noise(n: int, seed: int) -> pd.Series:
    return daily(list(np.random.default_rng(seed).normal(0.0, 0.02, n)))


@pytest.mark.parametrize("n", [1, 2, 5, 12])
def test_identity_gives_n(n: int) -> None:
    assert n_eff(np.eye(n)) == pytest.approx(n)


@pytest.mark.parametrize("n", [2, 5, 12])
def test_all_ones_gives_one(n: int) -> None:
    assert n_eff(np.ones((n, n))) == pytest.approx(1.0)


def test_known_two_by_two() -> None:
    # Eigenvalues 1 + r and 1 - r: N_eff = 4 / (2 + 2 r^2).
    r = 0.6
    assert n_eff(np.array([[1.0, r], [r, 1.0]])) == pytest.approx(4 / (2 + 2 * r**2))


def test_tiny_negative_eigenvalues_clamp_to_zero() -> None:
    # Eigenvalues 2 + e and -e. Clamped gives exactly 1; keeping -e gives about 1 - e, so the
    # tolerance sits well below e.
    e = 1e-9
    matrix = np.array([[1.0, 1.0 + e], [1.0 + e, 1.0]])
    assert np.linalg.eigvalsh(matrix).min() < 0
    assert n_eff(matrix) == pytest.approx(1.0, rel=1e-12, abs=1e-12)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (np.zeros((2, 3)), "square"),
        (np.empty((0, 0)), "square"),
        (np.array([[1.0, 0.5], [0.1, 1.0]]), "symmetric"),
        (np.array([[1.0, np.nan], [np.nan, 1.0]]), "non-finite"),
        (np.array([[1.0, 2.0], [2.0, 1.0]]), "negative eigenvalue"),
        (np.zeros((2, 2)), "no positive eigenvalue"),
    ],
)
def test_n_eff_refuses_a_bad_matrix(bad: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        n_eff(bad)


def test_uncorrelated_and_identical_series() -> None:
    a = noise(400, 1)
    report = portfolio_report({"a": a, "same": a.copy(), "b": noise(400, 2)})
    assert report.names == ("a", "b", "same")
    assert report.n_days == 400
    assert report.method == METHOD == "Pearson, daily UTC returns, common dates"
    table = np.array(report.correlation)
    assert table[0, 2] == pytest.approx(1.0)
    assert abs(table[0, 1]) < 0.2
    # Two identical series and one independent: eigenvalues near 2, 1, 0, so 3^2 / 5.
    assert report.n_eff == pytest.approx(1.8, abs=0.1)


def test_correlation_matches_pandas_on_common_dates() -> None:
    a = noise(50, 3)
    b = noise(50, 4)
    report = portfolio_report({"a": a, "b": b})
    assert report.correlation[0][1] == pytest.approx(a.corr(b))
    assert report.n_eff == pytest.approx(n_eff(np.array(report.correlation)))


def test_only_common_dates_are_used_and_nothing_is_filled() -> None:
    a = noise(30, 5)
    b = noise(30, 6)
    late = b.iloc[10:]  # a starts 10 days earlier
    holed = late.copy()
    holed.iloc[5] = np.nan  # a NaN day leaves the common dates; it is never filled
    report = portfolio_report({"a": a, "b": holed})
    common = late.index.delete(5)
    assert report.n_days == 19
    assert report.start == common[0].date().isoformat()
    assert report.end == common[-1].date().isoformat()
    assert report.correlation[0][1] == pytest.approx(a.loc[common].corr(late.loc[common]))


def test_zero_variance_series_is_excluded_and_named() -> None:
    report = portfolio_report(
        {"a": noise(40, 7), "b": noise(40, 8), "flat": daily([0.0] * 40)},
    )
    assert report.names == ("a", "b")
    assert [name for name, _ in report.excluded] == ["flat"]
    assert "zero variance" in report.excluded[0][1]


def test_series_flat_only_on_the_common_dates_is_excluded() -> None:
    a = noise(40, 9)
    # Moves in its first 20 days, flat afterwards; the other series covers only the flat part.
    part = daily([*list(noise(20, 10)), *[0.0] * 20])
    late = a.iloc[20:]
    report = portfolio_report({"late": late, "part": part})
    assert report.names == ("late",)
    assert report.excluded[0][0] == "part"
    assert "zero variance" in report.excluded[0][1]
    assert report.n_eff is None


def test_excluding_a_flat_series_widens_the_common_dates() -> None:
    a = noise(40, 11)
    b = noise(40, 12)
    report = portfolio_report({"a": a, "b": b, "short_flat": daily([0.0] * 5)})
    assert report.names == ("a", "b")
    assert report.n_days == 40


def test_too_short_series_is_excluded_and_named() -> None:
    assert MIN_COMMON_DAYS == 3
    report = portfolio_report(
        {"a": noise(10, 13), "b": noise(10, 14), "new": daily([0.01, -0.01])},
    )
    assert report.names == ("a", "b")
    assert report.excluded == (("new", "too short: 2 days, need 3"),)


def test_disjoint_series_drop_the_one_with_the_fewest_days() -> None:
    long_one = noise(30, 15)
    other = daily(list(noise(10, 16)), start="2026-06-01")
    report = portfolio_report({"long": long_one, "other": other})
    assert report.names == ("long",)
    assert report.excluded[0][0] == "other"
    assert "in common" in report.excluded[0][1]
    assert report.n_eff is None
    assert report.correlation == ((1.0,),)


def test_disjoint_tie_is_stable_for_a_name_prefix() -> None:
    first = noise(10, 17)
    second = daily(list(noise(10, 18)), start="2026-06-01")
    report = portfolio_report({"ab": second, "a": first})
    assert report.names == ("a",)


def test_no_series_gives_an_empty_report() -> None:
    report = portfolio_report({})
    assert report.names == ()
    assert report.correlation == ()
    assert report.n_eff is None
    assert report.n_days == 0
    assert "fewer than 2" in render_portfolio(report)


def test_order_of_the_mapping_does_not_matter() -> None:
    a, b = noise(20, 19), noise(20, 20)
    assert portfolio_report({"a": a, "b": b}) == portfolio_report({"b": b, "a": a})


def test_utc_days_align_across_time_of_day_and_tz_awareness() -> None:
    a = noise(20, 21)
    naive = a.copy()
    naive.index = naive.index.tz_localize(None) + pd.Timedelta(hours=23)
    report = portfolio_report({"a": a, "b": naive})
    assert report.n_days == 20
    assert report.correlation[0][1] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("series", "match"),
    [
        (pd.Series([0.1, 0.2, 0.3]), "DatetimeIndex"),
        (
            pd.Series(
                [0.1, 0.2],
                index=pd.to_datetime(["2026-01-01 01:00", "2026-01-01 02:00"], utc=True),
            ),
            "more than one return",
        ),
        (daily([0.1, np.inf, 0.2]), "infinite"),
    ],
)
def test_bad_series_is_refused_by_name(series: pd.Series, match: str) -> None:
    with pytest.raises(ValueError, match=match) as err:
        portfolio_report({"bad_one": series})
    assert "bad_one" in str(err.value)


def test_render_is_information_only() -> None:
    text = render_portfolio(
        portfolio_report(
            {"BTC": noise(40, 22), "ETH": noise(40, 23), "flat": daily([0.0] * 40)},
        )
    )
    assert "information only" in text.lower()
    assert "one piece of evidence, not N" in text
    assert METHOD in text
    assert "N_eff: " in text
    assert "Excluded flat: zero variance" in text
    assert "2026-01-01 to 2026-02-09 (40 days)" in text
    assert "Caution" not in text


def test_render_warns_on_a_short_window() -> None:
    text = render_portfolio(portfolio_report({"a": noise(5, 24), "b": noise(5, 25)}))
    assert "Caution: only 5 common days" in text


def correlated(rho: float, n_coins: int, days: int, seed: int) -> list[pd.Series]:
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0, 0.03, days)
    return [
        daily(list(rho**0.5 * market + (1 - rho) ** 0.5 * rng.normal(0.0, 0.03, days)))
        for _ in range(n_coins)
    ]


def spells(share: float, days: int, seed: int) -> np.ndarray:
    """In market on 3-day spells covering about ``share`` of the days."""
    rng = np.random.default_rng(seed)
    held = np.zeros(days, dtype=bool)
    for start in range(0, days, 3):
        held[start : start + 3] = rng.random() < share
    return held


def test_mostly_flat_bots_on_correlated_coins_do_not_read_as_independent() -> None:
    """Seven coins at rho 0.8 have N_eff 49 / (7 + 42 x 0.64) = 1.45 on an exact matrix. The bots,
    in the market a quarter of the time, read far higher. The coins' figure is the one quoted."""
    days = 400
    coins = {f"c{i}": s for i, s in enumerate(correlated(0.8, 7, days, 1))}
    held = {name: spells(0.25, days, 10 + i) for i, name in enumerate(coins)}
    bots = {name: coins[name] * held[name] for name in coins}
    in_market = {name: pd.Series(held[name], index=coins[name].index) for name in coins}
    report = portfolio_report(bots, coin_returns=coins, in_market=in_market)
    assert report.coins is not None and report.coins.n_eff is not None
    assert report.n_eff is not None and report.n_eff > 4.0
    assert report.coins.n_eff == pytest.approx(1.45, abs=0.15)
    text = render_portfolio(report)
    assert text.index("N_eff to quote") < text.index("do not quote")


def test_the_coins_are_read_over_the_bots_window() -> None:
    """Same window: the coins' longer history must not leak into the figure."""
    coins = {f"c{i}": s for i, s in enumerate(correlated(0.5, 3, 200, 2))}
    bots = {name: series.iloc[50:150] for name, series in coins.items()}
    report = portfolio_report(bots, coin_returns=coins)
    assert report.coins is not None
    assert (report.coins.start, report.coins.end) == (report.start, report.end)
    assert report.coins.n_days == report.n_days == 100


def test_a_bot_that_never_traded_keeps_its_coin() -> None:
    coins = {f"c{i}": s for i, s in enumerate(correlated(0.5, 3, 40, 3))}
    bots = {"c0": coins["c0"], "c1": coins["c1"], "c2": coins["c2"] * 0.0}
    report = portfolio_report(bots, coin_returns=coins)
    assert report.names == ("c0", "c1") and report.coins is not None
    assert report.coins.names == ("c0", "c1", "c2")


def test_no_usable_bot_still_gives_the_coin_figure() -> None:
    coins = {f"c{i}": s for i, s in enumerate(correlated(0.5, 3, 40, 4))}
    flat = {name: series * 0.0 for name, series in coins.items()}
    report = portfolio_report(flat, coin_returns=coins)
    assert report.names == () and report.coins is not None and report.coins.n_days == 40
    text = render_portfolio(report)
    assert "N_eff to quote" in text and "Window: none" not in text and "(40 days)" in text


def test_days_both_in_market_counts_the_window_only() -> None:
    index = daily([0.0] * 6).index
    a = pd.Series([True, True, True, False, False, True], index=index)
    b = pd.Series([True, False, True, False, True, True], index=index)
    report = portfolio_report(
        {"a": noise(6, 30), "b": noise(6, 31)},
        coin_returns={"a": noise(6, 32), "b": noise(6, 33)},
        in_market={"a": a, "b": b},
    )
    assert report.days_both_in_market == (("a", "b", 3),)
    assert "a / b: 3" in render_portfolio(report)


def test_days_both_in_market_ignores_days_outside_the_window() -> None:
    index = daily([0.0] * 6).index
    held = pd.Series([True] * 6, index=index)
    report = portfolio_report(
        {"a": noise(6, 37).iloc[2:], "b": noise(6, 38).iloc[2:]},  # the window is the last 4 days
        coin_returns={"a": noise(6, 39), "b": noise(6, 40)},
        in_market={"a": held, "b": held},
    )
    assert report.n_days == 4 and report.days_both_in_market == (("a", "b", 4),)


def test_named_exclusions_reach_the_report() -> None:
    report = portfolio_report({"a": noise(9, 34)}, excluded={"late": "started later"})
    assert ("late", "started later") in report.excluded
    assert "Excluded late: started later" in render_portfolio(report)


def test_the_short_window_bias_is_said_plainly() -> None:
    text = render_portfolio(portfolio_report({"a": noise(9, 35), "b": noise(9, 36)}))
    assert "Short windows bias N_eff low" in text


def _coins(days: int, coins: int = 5) -> PortfolioReport:
    bots = {f"t{i}": noise(days, 50 + i) for i in range(coins)}
    held = {f"t{i}": noise(days, 70 + i) for i in range(coins)}
    return portfolio_report(bots, coin_returns=held)


@pytest.mark.parametrize("days", [3, 5])
def test_days_up_to_the_coin_count_are_flagged_not_quoted(days: int) -> None:
    """Five days of five coins gave a figure the matrix's rank cannot support."""
    report = _coins(days)
    assert report.coins is not None and report.coins.too_few_days
    text = render_portfolio(report)
    assert "N_eff to quote: none, too few days to quote" in text
    assert f"{days} common days for 5 coins" in text
    assert "N_eff to quote (the coins' own daily returns)" not in text
    assert f"{report.coins.n_eff:.2f} of 5 coins" not in text


def test_one_day_more_than_the_coins_is_quoted() -> None:
    report = _coins(6)
    assert report.coins is not None and not report.coins.too_few_days
    assert "N_eff to quote (the coins' own daily returns)" in render_portfolio(report)


def test_the_bots_figure_alone_is_flagged_too() -> None:
    text = render_portfolio(portfolio_report({f"t{i}": noise(3, 60 + i) for i in range(3)}))
    assert "N_eff: too few days to quote" in text
    assert "N_eff: 2." not in text and "of 3 series" not in text
