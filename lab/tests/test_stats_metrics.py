"""Metrics against hand-calculated values (testing rule). Tolerances are rel=1e-12: every
expected number below is the same closed-form arithmetic, so only float rounding differs."""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest
from hedgefund.backtest import metrics as hf_metrics

from lab.stats.metrics import compute_metrics, metrics_from_returns, sharpe_per_bar
from lab.types import BacktestResult, Trade

T0 = pd.Timestamp("2024-01-01", tz="UTC")
R4 = np.array([0.0, 0.1, -0.05, 0.02])  # hand example; 4h bars -> 2190 bars a year


def _trade(pnl: float, reason: str = "signal") -> Trade:
    return Trade(
        direction="long",
        entry_time=T0,
        entry_price=100.0,
        exit_time=None if reason == "end" else T0 + pd.Timedelta(hours=4),
        exit_price=None if reason == "end" else 100.0 + pnl,
        size=1.0,
        pnl=pnl,
        return_pct=pnl / 100.0,
        fees=0.0,
        exit_reason=reason,  # type: ignore[arg-type]
        bars_held=1,
    )


def test_total_return_and_cagr_use_365_day_year() -> None:
    m = metrics_from_returns(R4, "4h")
    assert m.total_return == pytest.approx(1.1 * 0.95 * 1.02 - 1.0, rel=1e-12)  # 0.0659
    # 4 bars of 4h = 4 / 2190 years.
    assert m.cagr == pytest.approx(1.0659 ** (2190 / 4) - 1.0, rel=1e-9)
    assert m.bars == 4


def test_sharpe_volatility_annualised_with_sqrt_2190_for_4h() -> None:
    m = metrics_from_returns(R4, "4h")
    mean = 0.07 / 4  # 0.0175
    std = math.sqrt(0.011675 / 3)  # squared deviations sum to 0.011675, ddof=1
    assert m.volatility == pytest.approx(std * math.sqrt(2190), rel=1e-12)
    assert m.sharpe == pytest.approx(mean / std * math.sqrt(2190), rel=1e-12)


def test_sortino_uses_downside_deviation_over_all_bars() -> None:
    m = metrics_from_returns(R4, "4h")
    downside = math.sqrt(0.05**2 / 4)  # 0.025
    assert m.sortino == pytest.approx(0.0175 / downside * math.sqrt(2190), rel=1e-12)


def test_daily_interval_annualises_with_sqrt_365_not_252() -> None:
    m = metrics_from_returns(R4, "1d")
    assert m.sharpe == pytest.approx(sharpe_per_bar(R4) * math.sqrt(365), rel=1e-12)
    assert m.sharpe != pytest.approx(sharpe_per_bar(R4) * math.sqrt(252))


def test_max_drawdown_peak_to_trough() -> None:
    # equity 1, 1, 1.1, 1.045, 1.0659 -> deepest fall 1.045 / 1.1 - 1
    assert metrics_from_returns(R4, "4h").max_drawdown == pytest.approx(-0.05, rel=1e-12)


def test_max_drawdown_counts_a_loss_on_the_first_bar() -> None:
    # The curve starts at 1.0 before bar 0, so -10% on bar 0 is a drawdown (hedgeFund's
    # max_drawdown starts at bar 0's equity and would report 0 here).
    assert metrics_from_returns([-0.1, 0.05], "1h").max_drawdown == pytest.approx(-0.1)
    assert hf_metrics.max_drawdown(np.array([-0.1, 0.05])) == 0.0


def test_win_rate_and_profit_factor_from_closed_trades_only() -> None:
    trades = [_trade(10.0), _trade(-4.0), _trade(6.0), _trade(-1.0), _trade(-50.0, "end")]
    m = metrics_from_returns(R4, "4h", trades=trades)
    assert m.win_rate == pytest.approx(2 / 4)
    assert m.profit_factor == pytest.approx(16.0 / 5.0)
    assert m.n_trades == 5  # every trade, including the one still open at the end


def test_profit_factor_is_inf_without_losses_and_nan_without_trades() -> None:
    assert metrics_from_returns(R4, "4h", trades=[_trade(3.0)]).profit_factor == math.inf
    m = metrics_from_returns(R4, "4h")
    assert math.isnan(m.profit_factor)
    assert math.isnan(m.win_rate)
    assert m.n_trades == 0


def test_exposure_and_turnover_from_position() -> None:
    pos = np.array([0.0, 1.0, 1.0, 0.5, 0.0, 0.0])
    m = metrics_from_returns(np.zeros(6), "4h", position=pos)
    assert m.exposure == pytest.approx(3 / 6)
    # |Δpos| = 0 + 1 + 0 + 0.5 + 0.5 + 0 = 2 over 6 bars -> 2 * 2190 / 6 per year
    assert m.turnover == pytest.approx(2.0 * 2190 / 6)


def test_position_length_mismatch_names_the_input() -> None:
    with pytest.raises(ValueError, match="position"):
        metrics_from_returns(R4, "4h", position=np.zeros(3))


def test_nan_returns_rejected() -> None:
    with pytest.raises(ValueError, match="returns"):
        metrics_from_returns([0.0, float("nan")], "4h")


def test_empty_returns_give_defined_values_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        m = metrics_from_returns(np.array([]), "4h", position=np.array([]))
    assert m.bars == 0
    assert m.total_return == 0.0
    assert m.max_drawdown == 0.0
    assert m.exposure == 0.0
    assert m.turnover == 0.0
    assert math.isnan(m.cagr)
    assert math.isnan(m.sharpe)
    assert math.isnan(m.volatility)


def test_flat_returns_give_defined_values_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        m = metrics_from_returns(np.zeros(10), "4h", position=np.zeros(10))
    assert m.total_return == 0.0
    assert m.cagr == 0.0
    assert m.volatility == 0.0
    assert m.max_drawdown == 0.0
    assert math.isnan(m.sharpe)
    assert math.isnan(m.sortino)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert math.isnan(sharpe_per_bar([0.01]))
        assert math.isnan(sharpe_per_bar(np.full(5, 0.01)))


def test_wiped_out_account_has_cagr_minus_one() -> None:
    assert metrics_from_returns([0.0, -1.0, 0.0], "1d").cagr == -1.0


def test_sharpe_and_sortino_equal_hedgefund_given_periods_per_year() -> None:
    r = np.random.default_rng(3).normal(0.0005, 0.01, 500)
    m = metrics_from_returns(pd.Series(r), "4h")
    assert m.sharpe == hf_metrics.sharpe_ratio(r, periods_per_year=2190)
    assert m.sortino == hf_metrics.sortino_ratio(r, periods_per_year=2190)
    assert m.sharpe != pytest.approx(hf_metrics.sharpe_ratio(r))  # hedgeFund's 252 default


def test_sharpe_per_bar_is_not_annualised() -> None:
    mean, std = 0.0175, math.sqrt(0.011675 / 3)
    assert sharpe_per_bar(R4) == pytest.approx(mean / std, rel=1e-12)


def test_compute_metrics_uses_net_or_gross_returns() -> None:
    idx = pd.date_range(T0, periods=4, freq="4h", name="time")
    net = pd.Series(R4, index=idx)
    gross = pd.Series(R4 + 0.001, index=idx)
    result = BacktestResult(
        equity=1000.0 * (1.0 + net).cumprod(),
        returns=net,
        gross_returns=gross,
        position=pd.Series([0.0, 1.0, 1.0, 0.0], index=idx),
        trades=(_trade(5.0),),
        fees_paid=1.0,
        init_cash=1000.0,
    )
    m_net = compute_metrics(result, "4h")
    m_gross = compute_metrics(result, "4h", gross=True)
    assert m_net == metrics_from_returns(R4, "4h", position=result.position, trades=result.trades)
    assert m_gross.total_return == pytest.approx(np.prod(1.0 + R4 + 0.001) - 1.0)
    assert m_net.exposure == m_gross.exposure == 0.5
    assert m_net.n_trades == 1
