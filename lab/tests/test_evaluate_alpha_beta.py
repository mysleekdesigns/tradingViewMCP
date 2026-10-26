"""Alpha/beta decomposition against BTC: exact OLS cases, closed-form OLS with noise, HAC(0) =
White HC0, significance sanity over seeds, rolling = fresh fit (perturbation-tested), alignment,
start filter, JSON. Offline, synthetic data with explicit seeds."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from lab.evaluate.alpha_beta import AlphaBeta, alpha_beta, alpha_beta_json, default_hac_lags
from lab.types import bars_per_year


def idx(n: int, freq: str = "1D", start: str = "2022-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq=freq, tz="UTC", name="time")


def series(values: np.ndarray, freq: str = "1D", start: str = "2022-01-01") -> pd.Series:
    return pd.Series(values, index=idx(len(values), freq, start), dtype=float)


def ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Closed-form OLS via the normal equations."""
    design = np.column_stack([np.ones(len(x)), x])
    coef = np.linalg.solve(design.T @ design, design.T @ y)
    return float(coef[0]), float(coef[1]), y - design @ coef


def noisy(n: int, seed: int, a: float = 0.0, b: float = 1.0, sd: float = 0.01):
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0005, 0.03, n)
    y = a + b * x + rng.normal(0.0, sd, n)
    return series(y), series(x)


# ---------------------------------------------------------------- exact and closed-form OLS


def test_exact_linear_relation_recovers_alpha_beta_and_r2_one() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.02, 300)
    a, b = 0.0004, 0.8
    ab = alpha_beta(series(a + b * x), series(x), interval="1d")
    assert ab.beta == pytest.approx(b, abs=1e-12)
    assert ab.alpha_annual == pytest.approx(a * 365, abs=1e-10)
    assert ab.r2 == pytest.approx(1.0, abs=1e-12)
    assert ab.n == 300
    assert ab.alpha_p < 1e-6  # alpha known (almost) without error


def test_annualisation_uses_365_day_bars_per_year() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0.0, 0.01, 500)
    a = 1e-5
    ab = alpha_beta(series(a + 1.2 * x, "4h"), series(x, "4h"), interval="4h")
    assert bars_per_year("4h") == 2190.0
    assert ab.alpha_annual == pytest.approx(a * 2190.0, rel=1e-8)


def test_noise_case_matches_closed_form_ols() -> None:
    y, x = noisy(400, seed=3, a=0.0002, b=0.9)
    a, b, e = ols(y.to_numpy(), x.to_numpy())
    ab = alpha_beta(y, x, interval="1d")
    assert ab.beta == pytest.approx(b, rel=1e-10)
    assert ab.alpha_annual == pytest.approx(a * 365, rel=1e-10)
    yd = y.to_numpy() - y.mean()
    assert ab.r2 == pytest.approx(1 - (e @ e) / (yd @ yd), rel=1e-10)
    assert ab.hac_lags == default_hac_lags(400) == 5  # floor(4 * 4 ** (2/9)) = 5


def test_hac_lag_zero_equals_white_hc0() -> None:
    y, x = noisy(250, seed=4, a=0.001, b=0.7)
    # heteroskedastic errors so HC0 differs from the classical SE
    yv = y.to_numpy() + np.random.default_rng(5).normal(0, 1, 250) * np.abs(x.to_numpy())
    y = series(yv)
    a, _, e = ols(yv, x.to_numpy())
    design = np.column_stack([np.ones(250), x.to_numpy()])
    bread = np.linalg.inv(design.T @ design)
    meat = (design * e[:, None] ** 2).T @ design
    se_white = math.sqrt((bread @ meat @ bread)[0, 0])
    ab = alpha_beta(y, x, interval="1d", hac_lags=0)
    assert ab.hac_lags == 0
    assert ab.alpha_t == pytest.approx(a / se_white, rel=1e-10)
    from scipy import stats

    assert ab.alpha_p == pytest.approx(2 * stats.t.sf(abs(a / se_white), df=248), rel=1e-10)


def test_hac_lag_one_matches_hand_newey_west() -> None:
    y, x = noisy(120, seed=6, a=0.0005, b=1.1)
    yv, xv = y.to_numpy(), x.to_numpy()
    a, _, e = ols(yv, xv)
    design = np.column_stack([np.ones(120), xv])
    u = design * e[:, None]
    s = u.T @ u
    g = np.zeros((2, 2))
    for t in range(1, 120):
        g += np.outer(u[t], u[t - 1])
    s += 0.5 * (g + g.T)  # Bartlett weight 1 - 1/2
    bread = np.linalg.inv(design.T @ design)
    se = math.sqrt((bread @ s @ bread)[0, 0])
    assert alpha_beta(y, x, interval="1d", hac_lags=1).alpha_t == pytest.approx(a / se, rel=1e-10)


# ---------------------------------------------------------------- significance sanity


def test_pure_beta_strategy_alpha_not_significant_on_average() -> None:
    ps = [alpha_beta(*noisy(730, seed=s, a=0.0, b=0.9), interval="1d").alpha_p for s in range(40)]
    # under the null p is roughly uniform: mean ~0.5, few small values
    assert 0.35 < float(np.mean(ps)) < 0.65
    assert sum(p < 0.05 for p in ps) <= 6


def test_constant_drift_gives_small_p() -> None:
    sd, n = 0.005, 730
    se_annual = sd / math.sqrt(n) * 365  # sampling SE of the annualised intercept, ~0.068
    for s in range(5):
        ab = alpha_beta(*noisy(n, seed=s, a=0.002, b=0.9, sd=sd), interval="1d")
        assert ab.alpha_p < 0.001
        assert ab.alpha_t > 0
        assert abs(ab.alpha_annual - 0.002 * 365) < 4 * se_annual


# ---------------------------------------------------------------- rolling


def test_rolling_window_is_rolling_days_of_bars_and_matches_fresh_fit() -> None:
    y, x = noisy(200, seed=7, a=0.0003, b=0.8, sd=0.02)
    ab = alpha_beta(y, x, interval="1d", rolling_days=60)
    assert list(ab.rolling.columns) == ["alpha_annual", "beta"]
    assert ab.rolling.index.equals(y.index)
    assert ab.rolling.iloc[:59].isna().all().all()
    assert ab.rolling.iloc[59:].notna().all().all()
    for t in (59, 100, 199):
        a, b, _ = ols(y.to_numpy()[t - 59 : t + 1], x.to_numpy()[t - 59 : t + 1])
        assert ab.rolling["beta"].iloc[t] == pytest.approx(b, rel=1e-8)
        assert ab.rolling["alpha_annual"].iloc[t] == pytest.approx(a * 365, rel=1e-6, abs=1e-9)


def test_rolling_window_rows_scale_with_interval() -> None:
    y, x = noisy(24 * 4, seed=8)
    y, x = series(y.to_numpy(), "1h"), series(x.to_numpy(), "1h")
    ab = alpha_beta(y, x, interval="1h", rolling_days=2)  # 48 rows
    assert ab.rolling["beta"].iloc[:47].isna().all()
    assert ab.rolling["beta"].iloc[47:].notna().all()


def test_rolling_perturbation_later_rows_do_not_change_earlier_values() -> None:
    y, x = noisy(300, seed=9, a=0.0, b=1.0, sd=0.02)
    base = alpha_beta(y, x, interval="1d").rolling
    t = 180
    y2, x2 = y.copy(), x.copy()
    rng = np.random.default_rng(10)
    y2.iloc[t + 1 :] = rng.normal(0.0, 0.5, len(y2) - t - 1)
    x2.iloc[t + 1 :] = rng.normal(0.0, 0.5, len(x2) - t - 1)
    pert = alpha_beta(y2, x2, interval="1d").rolling
    pd.testing.assert_frame_equal(base.iloc[: t + 1], pert.iloc[: t + 1], rtol=1e-9, atol=1e-12)
    assert not np.allclose(base["beta"].iloc[t + 1 :], pert["beta"].iloc[t + 1 :])


def test_rolling_nan_where_benchmark_constant_in_window() -> None:
    rng = np.random.default_rng(11)
    x = rng.normal(0, 0.02, 120)
    x[30:100] = 0.001  # a flat stretch longer than the window
    ab = alpha_beta(series(0.5 * x), series(x), interval="1d", rolling_days=20)
    assert ab.rolling["beta"].iloc[49:100].isna().all()
    assert ab.rolling["beta"].iloc[119] == pytest.approx(0.5, rel=1e-8)


def test_window_longer_than_data_is_all_nan() -> None:
    ab = alpha_beta(*noisy(30, seed=12), interval="1d", rolling_days=60)
    assert ab.rolling.isna().all().all()
    assert alpha_beta_json(ab, rolling_points=10)["rolling"] == []


# ---------------------------------------------------------------- alignment, NaN, start


def test_aligns_on_intersection_and_drops_nan_without_filling() -> None:
    rng = np.random.default_rng(13)
    x_full = rng.normal(0, 0.02, 100)
    y_full = 0.001 + 0.6 * x_full + rng.normal(0, 0.005, 100)
    x = series(x_full)
    y = series(y_full).iloc[10:]  # strategy starts later
    x.iloc[50] = np.nan
    y.iloc[20] = np.nan  # position 30 in the full index
    keep = np.ones(100, bool)
    keep[:10] = False
    keep[[30, 50]] = False
    ab = alpha_beta(y, x, interval="1d")
    a, b, _ = ols(y_full[keep], x_full[keep])
    assert ab.n == int(keep.sum()) == 88
    assert ab.beta == pytest.approx(b, rel=1e-10)
    assert ab.alpha_annual == pytest.approx(a * 365, rel=1e-10)
    assert ab.rolling.index.equals(x.index[keep])


def test_unsorted_input_gives_same_result() -> None:
    y, x = noisy(100, seed=14)
    ab1 = alpha_beta(y, x, interval="1d")
    ab2 = alpha_beta(y.iloc[::-1], x.sample(frac=1.0, random_state=0), interval="1d")
    assert ab1.beta == pytest.approx(ab2.beta, rel=1e-12)
    assert ab1.alpha_t == pytest.approx(ab2.alpha_t, rel=1e-10)
    pd.testing.assert_frame_equal(ab1.rolling, ab2.rolling)


def test_start_drops_earlier_rows() -> None:
    y, x = noisy(200, seed=15, a=0.0005, b=0.7)
    start = y.index[50]
    ab = alpha_beta(y, x, interval="1d", start=start)
    a, b, _ = ols(y.to_numpy()[50:], x.to_numpy()[50:])
    assert ab.n == 150
    assert ab.rolling.index[0] == start
    assert ab.beta == pytest.approx(b, rel=1e-10)
    assert ab.alpha_annual == pytest.approx(a * 365, rel=1e-10)


# ---------------------------------------------------------------- errors


def test_errors_name_the_bad_input() -> None:
    y, x = noisy(50, seed=16)
    with pytest.raises(ValueError, match="interval"):
        alpha_beta(y, x, interval="7d")
    with pytest.raises(ValueError, match="rolling_days"):
        alpha_beta(y, x, interval="1d", rolling_days=1)
    with pytest.raises(ValueError, match="at least 3"):
        alpha_beta(y.iloc[:2], x, interval="1d")
    with pytest.raises(ValueError, match="at least 3"):
        alpha_beta(y, x, interval="1d", start=y.index[-2])
    with pytest.raises(ValueError, match="benchmark has zero variance"):
        alpha_beta(y, x * 0 + 0.01, interval="1d")
    with pytest.raises(ValueError, match="hac_lags"):
        alpha_beta(y, x, interval="1d", hac_lags=-1)
    with pytest.raises(ValueError, match="hac_lags"):
        alpha_beta(y, x, interval="1d", hac_lags=50)
    with pytest.raises(ValueError, match=r"strategy must be a pd\.Series"):
        alpha_beta(y.to_numpy(), x, interval="1d")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="benchmark index has duplicate"):
        alpha_beta(y, pd.concat([x, x.iloc[:1]]), interval="1d")
    with pytest.raises(ValueError, match="rolling_points"):
        alpha_beta_json(alpha_beta(y, x, interval="1d"), rolling_points=-1)


def test_default_hac_lags_rule_of_thumb() -> None:
    assert default_hac_lags(100) == 4
    assert default_hac_lags(1000) == math.floor(4 * 10 ** (2 / 9)) == 6
    assert default_hac_lags(3) == 1  # floor(4 * 0.03 ** (2/9)) = floor(1.83)
    ab = alpha_beta(*noisy(3, seed=17), interval="1d")
    assert ab.hac_lags == 1


# ---------------------------------------------------------------- JSON


def test_json_is_safe_and_nan_becomes_none() -> None:
    y, x = noisy(200, seed=18, a=0.0002, b=0.9)
    ab = alpha_beta(y, x, interval="1d")
    out = alpha_beta_json(ab, rolling_points=5)
    json.dumps(out, allow_nan=False)
    assert out["n"] == 200 and out["beta"] == pytest.approx(ab.beta)
    assert len(out["rolling"]) == 5
    full = ab.rolling.dropna()
    assert out["rolling"][0]["time"] == full.index[0].isoformat()
    assert out["rolling"][-1]["time"] == full.index[-1].isoformat()
    assert out["rolling"][-1]["beta"] == pytest.approx(full["beta"].iloc[-1])
    assert out["rolling_summary"]["rows"] == 141
    assert alpha_beta_json(ab)["rolling"] == []

    nan_ab = AlphaBeta(
        alpha_annual=math.nan,
        beta=math.inf,
        alpha_t=-math.inf,
        alpha_p=math.nan,
        r2=math.nan,
        n=3,
        hac_lags=0,
        rolling=pd.DataFrame({"alpha_annual": [math.nan], "beta": [math.nan]}, index=idx(1)),
    )
    nj = alpha_beta_json(nan_ab, rolling_points=3)
    json.dumps(nj, allow_nan=False)
    assert nj["alpha_annual"] is None and nj["beta"] is None and nj["alpha_t"] is None
    assert nj["alpha_p"] is None and nj["r2"] is None
    assert nj["rolling_summary"] == {"rows": 0} and nj["rolling"] == []


def test_json_rolling_points_capped_by_available_rows() -> None:
    ab = alpha_beta(*noisy(65, seed=19), interval="1d")
    out = alpha_beta_json(ab, rolling_points=100)
    assert len(out["rolling"]) == 6  # rows 59..64
