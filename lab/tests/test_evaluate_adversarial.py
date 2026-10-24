"""Adversarial checks: delay, sign flip, cost stress, Monte Carlo, permutation null."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig, load_config
from lab.evaluate.adversarial import (
    OK,
    WARN,
    VariantResult,
    adversarial_checks,
    adversarial_checks_signals,
    cost_stress_verdict,
    delay_check,
    delay_verdict,
    permutation_verdict,
    permute_history,
    run_variant,
    sign_flip_verdict,
    trade_equity_returns,
)
from lab.plan import parse_plan
from lab.stats import permute_candles
from lab.types import ZERO_COSTS, RiskSpec, Window
from tests.synthetic import candles_from_rows, make_candles, perturb_after

EMA_PLAN = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "crosses_above(ema_fast, ema_slow)",
    "long_exit": "crosses_below(ema_fast, ema_slow)",
    "risk": {"position_size_pct": 100},
}


def small_cfg(*, permutation_paths: int = 20) -> LabConfig:
    base = load_config()
    return replace(
        base,
        stats=replace(base.stats, monte_carlo_paths=200, permutation_paths=permutation_paths),
    )


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def leaky_signals(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """DELIBERATELY LEAKY (bypasses the plan language): peeks at the next bar's close."""
    close = candles["close"]
    up_next = (close.shift(-1) > close).fillna(False).astype(bool)
    return up_next, ~up_next


# --------------------------------------------------------------------------- delay


def test_leaky_toy_looks_great_raw_but_the_delay_check_flags_it() -> None:
    c = make_candles(2000, seed=11, start="2022-01-01")
    cfg = small_cfg()
    entries, exits = leaky_signals(c)
    base, delayed, verdict = delay_check(
        c, entries, exits, interval="4h", costs=cfg.costs, risk=RiskSpec(), delays=(1, 3, 5)
    )
    assert base.sharpe > 10  # absurdly good raw result
    assert delayed[0].sharpe < 0.25 * base.sharpe
    assert WARN in verdict and "vanishes after 1 bar" in verdict


def test_leaky_toy_full_suite_flags_delay_while_the_permutation_null_does_not() -> None:
    # Research finding (PLAN.md): statistics alone cannot catch look-ahead; leaky signals are
    # just as "good" on permuted prices. The delay check is what catches it.
    c = make_candles(1500, seed=12, start="2022-01-01")
    window = Window(c.index[200], c.index[-1] + pd.Timedelta(hours=4), "design")
    rep = adversarial_checks_signals(
        leaky_signals, c, window, cfg=small_cfg(), interval="4h", risk=RiskSpec(), seed=3
    )
    assert "vanishes after 1 bar" in rep.verdicts["delay"]
    assert rep.permutation_p_value > 0.05


def test_gradual_decay_gets_a_pass() -> None:
    text = delay_verdict(1.0, {1: 0.8, 3: 0.6, 5: 0.4})
    assert text.startswith(f"delay: {OK} edge decays gradually")


def test_unchanged_edge_at_five_bars_is_suspicious() -> None:
    assert "suspicious, edge unchanged at 5 bars" in delay_verdict(1.0, {1: 1.0, 3: 0.98, 5: 0.95})


def test_no_edge_means_the_delay_check_is_not_informative() -> None:
    assert "no edge to test" in delay_verdict(-0.3, {1: 0.1, 3: 0.0, 5: 0.2})


def test_delay_variants_fill_later() -> None:
    c = make_candles(300, seed=1)
    entries = pd.Series(False, index=c.index)
    entries.iloc[10] = True
    exits = pd.Series(False, index=c.index)
    exits.iloc[40] = True
    _, delayed, _ = delay_check(
        c, entries, exits, interval="4h", costs=ZERO_COSTS, risk=RiskSpec(), delays=(1, 3)
    )
    assert delayed[0].result is not None and delayed[1].result is not None
    assert delayed[0].result.trades[0].entry_time == c.index[12]  # signal 10 + 1 delay + 1
    assert delayed[1].result.trades[0].entry_time == c.index[14]


# --------------------------------------------------------------------------- sign flip


def test_an_uncoverable_short_is_reported_as_blown_not_raised() -> None:
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (200, 200, 200, 200)]
    rows += [(400, 400, 400, 400)] * 3
    c = candles_from_rows(rows)  # type: ignore[arg-type]
    entries = pd.Series([True, False, False, False, False, False], index=c.index)
    exits = pd.Series([False, False, False, True, False, False], index=c.index)
    v = run_variant(
        c, entries, exits, interval="4h", costs=ZERO_COSTS, risk=RiskSpec(), init_cash=10_000.0,
        check="sign_flip", label="sign flip", direction="short",
    )  # fmt: skip
    assert v.status == "blown" and "equity exhausted" in (v.error or "")
    assert math.isnan(v.sharpe) and v.metrics is None
    assert "blew up" in sign_flip_verdict(v, v)


def _variant(sharpe: float) -> VariantResult:
    return VariantResult("base", "base", {}, "ok", None, None, sharpe, sharpe / 50.0)


def test_sign_flip_verdicts() -> None:
    assert OK in sign_flip_verdict(_variant(1.0), _variant(-0.5))
    assert "no information" in sign_flip_verdict(_variant(0.2), _variant(0.4))


# --------------------------------------------------------------------------- cost stress / MC


def test_cost_stress_lowers_returns_monotonically() -> None:
    c = make_candles(2500, seed=21, start="2022-01-01")
    window = Window(c.index[100], c.index[-1] + pd.Timedelta(hours=4), "design")
    rep = adversarial_checks(parse_plan(EMA_PLAN), c, window, cfg=small_cfg(), seed=0)
    returns = [rep.base.metrics.total_return] + [  # type: ignore[union-attr]
        v.metrics.total_return  # type: ignore[union-attr]
        for v in rep.cost_stress
    ]
    assert returns == sorted(returns, reverse=True) and returns[0] > returns[-1]
    assert [v.params["factor"] for v in rep.cost_stress] == [2.0, 4.0]
    assert set(rep.verdicts) == {"delay", "sign_flip", "cost_stress", "monte_carlo", "permutation"}


def test_cost_stress_verdicts() -> None:
    assert OK in cost_stress_verdict(1.0, {2.0: 0.6, 4.0: 0.2})
    assert "disappears at x2" in cost_stress_verdict(1.0, {2.0: -0.1, 4.0: -0.5})
    assert "disappears at x4" in cost_stress_verdict(1.0, {2.0: 0.3, 4.0: -0.5})


def test_monte_carlo_uses_equity_returns_scaled_by_position_size() -> None:
    c = make_candles(300, seed=2)
    entries = pd.Series(False, index=c.index)
    exits = pd.Series(False, index=c.index)
    entries.iloc[[10, 60, 120]] = True
    exits.iloc[[30, 90, 150]] = True
    v = run_variant(
        c, entries, exits, interval="4h", costs=ZERO_COSTS, risk=RiskSpec(position_size_pct=50),
        init_cash=10_000.0, check="base", label="base",
    )  # fmt: skip
    assert v.result is not None
    got = trade_equity_returns(v.result, 50.0)
    np.testing.assert_allclose(got, [t.return_pct * 0.5 for t in v.result.trades], rtol=0)
    # equity-level check: each trade moves equity by half its notional return (no costs)
    eq = v.result.equity
    first = v.result.trades[0]
    assert first.exit_time is not None
    before = eq.loc[eq.index < first.entry_time].iloc[-1]
    after = eq.loc[first.exit_time]
    assert after / before - 1 == pytest.approx(got[0], rel=1e-9)


# --------------------------------------------------------------------------- permutation


def test_a_random_strategy_is_not_significant_against_the_permutation_null() -> None:
    c = make_candles(2500, seed=31, start="2022-01-01")  # driftless random walk
    window = Window(c.index[100], c.index[-1] + pd.Timedelta(hours=4), "design")
    rep = adversarial_checks(
        parse_plan(EMA_PLAN), c, window, cfg=small_cfg(permutation_paths=40), seed=5
    )
    assert rep.permutation_sharpes.shape == (40,)
    assert np.isfinite(rep.permutation_sharpes).all()
    assert rep.permutation_p_value > 0.05
    assert WARN in rep.verdicts["permutation"]


def test_permutation_verdicts() -> None:
    assert OK in permutation_verdict(0.01, 99.0, 100)
    assert WARN in permutation_verdict(0.4, 60.0, 100)
    assert "undefined" in permutation_verdict(math.nan, math.nan, 0)


# --------------------------------------------------------------------------- windows


def test_adversarial_checks_refuse_the_locked_holdout() -> None:
    c = make_candles(800, seed=4, start="2025-08-01")
    window = Window(ts("2025-09-01"), ts("2025-11-01"), "custom")
    with pytest.raises(ValueError, match="locked holdout"):
        adversarial_checks(parse_plan(EMA_PLAN), c, window, cfg=small_cfg(), seed=0)


def test_bars_after_the_window_end_do_not_change_any_check() -> None:
    c = make_candles(1600, seed=41, start="2022-01-01")
    window = Window(c.index[200], c.index[1200], "design")
    cfg = small_cfg(permutation_paths=5)
    plan = parse_plan(EMA_PLAN)
    a = adversarial_checks(plan, c, window, cfg=cfg, seed=1)
    b = adversarial_checks(plan, perturb_after(c, 1199), window, cfg=cfg, seed=1)
    assert a.verdicts == b.verdicts
    np.testing.assert_array_equal(a.permutation_sharpes, b.permutation_sharpes)
    assert [v.sharpe for v in a.delays] == [v.sharpe for v in b.delays]


# --------------------------------------------------------------------------- anchored permutation

BEAR_BARS, BULL_BARS = 600, 900


def bear_then_bull(seed: int = 0) -> pd.DataFrame:
    """A bear-market warm-up (drift -0.4%/bar) followed by a bull window (+0.3%/bar)."""
    rng = np.random.default_rng(seed)
    n = BEAR_BARS + BULL_BARS
    drift = np.r_[np.full(BEAR_BARS, -0.004), np.full(BULL_BARS, 0.003)]
    log_ret = drift + rng.normal(0.0, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(log_ret))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.004, n)) * close
    idx = pd.date_range(ts("2022-01-01"), periods=n, freq=pd.Timedelta(hours=4), name="time")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": np.full(n, 10.0),
            "tradable": np.ones(n, dtype=bool),
        },
        index=idx,
    )


def bull_window(c: pd.DataFrame) -> Window:
    return Window(c.index[BEAR_BARS], c.index[-1] + pd.Timedelta(hours=4), "design")


def trend_follower(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Causal toy: long while the close is above its trailing 50-bar mean."""
    above = (candles["close"] > candles["close"].rolling(50).mean()).astype(bool)
    return above, ~above


def _window_log_return(c: pd.DataFrame, k0: int) -> float:
    return float(np.log(c["close"].iloc[-1] / c["close"].iloc[k0 - 1]))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_permuted_window_keeps_the_real_window_log_return_and_warm_up(seed: int) -> None:
    c = bear_then_bull()
    window = bull_window(c)
    perm = permute_history(c, window, seed=seed)
    assert perm.index.equals(c.index)
    # Warm-up rows (including the anchor bar k0 - 1) are bit-identical; window rows moved.
    pd.testing.assert_frame_equal(perm.iloc[:BEAR_BARS], c.iloc[:BEAR_BARS])
    assert not np.array_equal(
        perm["close"].to_numpy()[BEAR_BARS:], c["close"].to_numpy()[BEAR_BARS:]
    )
    # Tolerance 1e-10 (absolute, on a log return of ~2.7): the permuted path re-sums the same
    # 900 log-ratios in another order, so only float rounding (~1e-13) can differ.
    real = _window_log_return(c, BEAR_BARS)
    assert real > 2.0
    assert _window_log_return(perm, BEAR_BARS) == pytest.approx(real, rel=0, abs=1e-10)


def test_permuting_the_whole_history_would_have_dragged_the_bull_window_down() -> None:
    # The pre-fix behaviour, kept here as the contrast: bear bars leak into the window.
    c = bear_then_bull()
    old = permute_candles(c, seed=1)
    assert _window_log_return(old, BEAR_BARS) < _window_log_return(c, BEAR_BARS) - 1.0


def test_trend_follower_null_is_not_biased_low_by_the_warm_up() -> None:
    c = bear_then_bull()
    window = bull_window(c)
    cfg = small_cfg(permutation_paths=30)
    rep = adversarial_checks_signals(
        trend_follower, c, window, cfg=cfg, interval="4h", risk=RiskSpec(), seed=11
    )
    new_median = float(np.nanmedian(rep.permutation_sharpes))
    # Old behaviour, reproduced: permute the whole history (warm-up included) with the same
    # seeds, recompute the signals, and run the same window.
    old = np.empty(cfg.stats.permutation_paths)
    for k in range(cfg.stats.permutation_paths):
        perm = permute_candles(c, seed=11 + 1 + k)
        win = perm.loc[(perm.index >= window.start) & (perm.index < window.end)]
        ent, ex = trend_follower(perm)
        old[k] = run_variant(
            win, ent.loc[win.index], ex.loc[win.index], interval="4h", costs=cfg.costs,
            risk=RiskSpec(), init_cash=cfg.init_cash, check="permutation", label=f"old {k}",
        ).sharpe  # fmt: skip
    old_median = float(np.nanmedian(old))
    assert new_median > old_median + 0.5
    assert new_median > 0  # a long-biased toy in a bull window earns on permuted bull bars too
