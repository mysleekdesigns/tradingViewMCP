"""Monte Carlo trade reshuffle: order-invariant final return, drawdown distribution."""

from __future__ import annotations

import numpy as np
import pytest

from lab.stats.montecarlo import MonteCarloResult, trade_reshuffle

TRADES = np.array([0.05, -0.03, 0.02, -0.08, 0.04, 0.01, -0.02, 0.06, -0.04, 0.03])


def test_final_return_is_identical_on_every_order() -> None:
    out = trade_reshuffle(TRADES, n_paths=500, seed=0)
    expected = float(np.prod(1.0 + TRADES) - 1.0)
    for q in ("p5", "p50", "p95"):  # float rounding across orders only: rel 1e-12
        assert out.final_return[q] == pytest.approx(expected, rel=1e-12)


def test_seeded_determinism_and_different_seeds_differ() -> None:
    a = trade_reshuffle(TRADES, n_paths=200, seed=1)
    b = trade_reshuffle(TRADES, n_paths=200, seed=1)
    c = trade_reshuffle(TRADES, n_paths=200, seed=2)
    assert isinstance(a, MonteCarloResult)
    assert a == b
    assert a.max_drawdown != c.max_drawdown


def test_drawdown_percentiles_ordered_and_bounded() -> None:
    out = trade_reshuffle(TRADES, n_paths=1000, seed=3)
    dd = out.max_drawdown
    assert set(dd) == {"p5", "p50", "p95"}
    assert dd["p5"] <= dd["p50"] <= dd["p95"] <= 0.0
    # Shallowest possible: the single worst trade; deepest: all losers in a row.
    worst_single = TRADES.min()
    all_losers = np.prod(1.0 + TRADES[TRADES < 0]) - 1.0
    assert dd["p95"] <= worst_single + 1e-12
    assert dd["p5"] >= all_losers - 1e-12
    assert out.n_paths == 1000


def test_drawdown_spread_shows_sequencing_risk() -> None:
    out = trade_reshuffle(TRADES, n_paths=1000, seed=4)
    assert out.max_drawdown["p5"] < out.max_drawdown["p95"]


def test_all_winners_have_zero_drawdown() -> None:
    out = trade_reshuffle(np.array([0.01, 0.02, 0.03]), n_paths=50, seed=0)
    assert out.max_drawdown == {"p5": 0.0, "p50": 0.0, "p95": 0.0}


def test_no_trades_gives_zeros() -> None:
    out = trade_reshuffle(np.array([]), n_paths=10, seed=0)
    assert out.final_return == {"p5": 0.0, "p50": 0.0, "p95": 0.0}
    assert out.max_drawdown == {"p5": 0.0, "p50": 0.0, "p95": 0.0}


@pytest.mark.parametrize(
    ("trades", "paths", "match"),
    [
        (TRADES, 0, "n_paths"),
        (np.array([0.1, np.nan]), 5, "NaN"),
        (np.array([0.1, -1.5]), 5, ">= -1"),
        (np.zeros((2, 2)), 5, "1-D"),
    ],
)
def test_bad_inputs_raise(trades: np.ndarray, paths: int, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        trade_reshuffle(trades, n_paths=paths, seed=0)
