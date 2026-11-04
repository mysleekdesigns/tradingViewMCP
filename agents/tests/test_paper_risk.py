"""Paper risk controls and sizing (PLAN.md Phase 20 Parts E and E2): stake arithmetic on known
values, the perturbation test on realised volatility, every sizing refusal, the no-averaging-down
screen, size steps, the circuit breaker on exact bars across a UTC midnight, and ``halt.lock``.
Offline; files go to tmp_path only."""

from __future__ import annotations

import ast
import inspect
import json
import math
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from research.paper import risk
from research.paper.freqtrade import render_strategy
from research.paper.risk import (
    UNLIMITED_STAKE,
    BreakerTrip,
    CircuitBreaker,
    PaperConfig,
    PriorTest,
    Protections,
    RiskConfig,
    RiskControls,
    Sizing,
    check_frozen_config,
    check_no_averaging_down,
    check_not_halted,
    check_size_step,
    check_sizing_against_plan,
    evaluate_breaker,
    load_risk_config,
    paper_config_from_dict,
    paper_config_to_dict,
    read_halt_lock,
    realized_vol_annual,
    risk_controls_from_dict,
    risk_controls_to_dict,
    sized_stake,
    sizing_from_dict,
    sizing_to_dict,
    write_halt_lock,
)

EXAMPLE_TOML = Path(__file__).resolve().parents[1] / "config" / "paper_risk.example.toml"
BARS_PER_YEAR_4H = 365 * 6.0

PROTECTIONS = Protections(
    cooldown_candles=2,
    stoploss_guard_lookback_candles=24,
    stoploss_guard_trade_limit=3,
    stoploss_guard_stop_candles=12,
    max_drawdown_lookback_candles=42,
    max_drawdown_trade_limit=5,
    max_drawdown_stop_candles=24,
    max_drawdown_allowed=0.06,
    max_blocked_share=0.333,
)
BREAKER = CircuitBreaker(max_daily_loss_pct=3.0, max_drawdown_pct=10.0, wallet=1000.0)


def sizing(**overrides: Any) -> Sizing:
    values: dict[str, Any] = {
        "risk_per_trade_pct": 1.0,
        "justification": "middle of the allowed range",
        "wallet": 1000.0,
        "max_stake_pct": 20.0,
        "min_stake_pct": 2.0,
        "max_total_exposure_pct": 100.0,
        "max_open_trades": 4,
    }
    return Sizing(**{**values, **overrides})


def stake(**overrides: Any) -> float:
    values: dict[str, Any] = {
        "stop_distance": 0.05,
        "wallet": 1000.0,
        "risk_per_trade_pct": 1.0,
        "max_stake_pct": 25.0,
        "min_stake_pct": 2.0,
    }
    return sized_stake(**{**values, **overrides})


def closes_4h(n: int = 200, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(
        30_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n))),
        index=pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
    )


# --------------------------------------------------------------------------- stake arithmetic


def test_stake_known_values_without_a_volatility_target() -> None:
    assert stake() == pytest.approx(200.0)  # 1000 * 1% / 5%
    assert stake(stop_distance=0.1) == pytest.approx(100.0)
    assert stake(risk_per_trade_pct=0.5) == pytest.approx(100.0)
    assert stake(size_multiplier=1.5, stop_distance=0.1) == pytest.approx(150.0)


def test_stake_is_capped_at_max_stake_pct() -> None:
    assert stake(stop_distance=0.01) == pytest.approx(250.0)  # base 1000, cap 25%
    assert stake(size_multiplier=2.0) == pytest.approx(250.0)  # 400 before the cap


def test_stake_has_no_floor_without_a_volatility_target() -> None:
    assert stake(stop_distance=0.9, min_stake_pct=5.0) == pytest.approx(10 / 0.9)


def test_stake_scales_by_target_over_realised_volatility() -> None:
    assert stake(
        stop_distance=0.1, vol_target_annual=0.5, realized_vol_annual=1.0
    ) == pytest.approx(50.0)
    assert stake(
        stop_distance=0.1, vol_target_annual=0.5, realized_vol_annual=0.8, size_multiplier=1.5
    ) == pytest.approx(100.0 * 0.5 / 0.8 * 1.5)


def test_stake_clip_holds_at_both_ends() -> None:
    assert stake(
        stop_distance=0.01, vol_target_annual=0.5, realized_vol_annual=0.5
    ) == pytest.approx(250.0)
    assert stake(vol_target_annual=0.5, realized_vol_annual=500.0) == pytest.approx(20.0)


@pytest.mark.parametrize("vol", [0.5, 0.25, 0.05, 0.001])
def test_low_volatility_never_raises_the_stake_above_the_base(vol: float) -> None:
    """The registered risk is a ceiling: the scale stops at 1, however calm the market."""
    assert stake(vol_target_annual=0.5, realized_vol_annual=vol) == pytest.approx(200.0)


def test_a_volatility_spike_shrinks_the_next_stake() -> None:
    calm = stake(vol_target_annual=0.5, realized_vol_annual=0.6)
    wild = stake(vol_target_annual=0.5, realized_vol_annual=1.5)
    assert wild < calm


@pytest.mark.parametrize("vol", [None, math.nan, math.inf, -math.inf, 0.0, -0.3])
def test_unusable_volatility_gives_the_minimum_stake(vol: float | None) -> None:
    assert stake(vol_target_annual=0.5, realized_vol_annual=vol) == pytest.approx(20.0)


def test_realised_volatility_is_ignored_without_a_target() -> None:
    assert stake(realized_vol_annual=9.0) == stake()


@pytest.mark.parametrize(
    ("overrides", "name"),
    [
        ({"stop_distance": 0.0}, "stop_distance"),
        ({"stop_distance": -0.05}, "stop_distance"),
        ({"stop_distance": math.nan}, "stop_distance"),
        ({"stop_distance": 5.0}, "stop_distance"),
        ({"wallet": 0.0}, "wallet"),
        ({"risk_per_trade_pct": math.inf}, "risk_per_trade_pct"),
        ({"max_stake_pct": 0.0}, "max_stake_pct"),
        ({"min_stake_pct": 30.0}, "min_stake_pct"),
        ({"min_stake_pct": -1.0}, "min_stake_pct"),
        ({"size_multiplier": 3.0}, "size_multiplier"),
        ({"size_multiplier": 0.5}, "size_multiplier"),
        ({"vol_target_annual": 0.0}, "vol_target_annual"),
    ],
)
def test_stake_refuses_bad_input_by_name(overrides: dict[str, float], name: str) -> None:
    with pytest.raises(ValueError, match=name):
        stake(**overrides)


def test_stake_source_runs_alone_with_only_math() -> None:
    namespace: dict[str, Any] = {"math": math}
    exec(inspect.getsource(sized_stake), namespace)
    embedded = namespace["sized_stake"]
    assert embedded is not sized_stake
    grid = [
        {"stop_distance": s, "size_multiplier": m, "vol_target_annual": t, "realized_vol_annual": v}
        for s in (0.01, 0.05, 0.3)
        for m in (1.0, 1.7, 2.0)
        for t in (None, 0.5)
        for v in (None, math.nan, 0.0, 0.2, 1.4, 90.0)
    ]
    shared = {
        "wallet": 1000.0,
        "risk_per_trade_pct": 1.0,
        "max_stake_pct": 25.0,
        "min_stake_pct": 2.0,
    }
    for case in grid:
        assert embedded(**case, **shared) == sized_stake(**case, **shared)
    with pytest.raises(ValueError, match="stop_distance"):
        embedded(stop_distance=0.0, **shared)


def test_stake_source_reads_no_other_module_name() -> None:
    tree = ast.parse(inspect.getsource(sized_stake))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    local = {a.arg for a in function.args.kwonlyargs} | {"name", "value", "base"}
    local |= {"largest", "smallest", "stake"}
    read = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    allowed = {"math", "float", "min", "max", "ValueError"}
    assert read - local - allowed == set()


# --------------------------------------------------------------------------- realised volatility


def test_realised_volatility_known_value() -> None:
    closes = closes_4h(50)
    log_returns = np.log(closes.to_numpy()[1:] / closes.to_numpy()[:-1])
    vol = realized_vol_annual(closes, 10, BARS_PER_YEAR_4H)
    assert vol.iloc[10] == pytest.approx(log_returns[:10].std(ddof=1) * math.sqrt(2190))
    assert vol.iloc[49] == pytest.approx(log_returns[-10:].std(ddof=1) * math.sqrt(2190))
    assert vol.index.equals(closes.index)


def test_realised_volatility_is_nan_until_the_window_is_full() -> None:
    vol = realized_vol_annual(closes_4h(30), 10, BARS_PER_YEAR_4H)
    assert vol.iloc[:10].isna().all()
    assert vol.iloc[10:].notna().all()


@pytest.mark.parametrize("n", [0, 1, 5, 10])
def test_realised_volatility_on_a_short_series_is_all_nan(n: int) -> None:
    vol = realized_vol_annual(closes_4h(max(n, 1)).iloc[:n], 10, BARS_PER_YEAR_4H)
    assert len(vol) == n
    assert vol.isna().all()


@pytest.mark.parametrize("t", [12, 40, 120, 198])
def test_realised_volatility_perturbation(t: int) -> None:
    """Change closes after bar t: every output up to t is bit-identical."""
    closes = closes_4h(200)
    before = realized_vol_annual(closes, 20, BARS_PER_YEAR_4H)
    changed = closes.copy()
    changed.iloc[t + 1 :] = changed.iloc[t + 1 :] * 3.7 + 11.0
    after = realized_vol_annual(changed, 20, BARS_PER_YEAR_4H)
    assert before.iloc[: t + 1].to_numpy().tobytes() == after.iloc[: t + 1].to_numpy().tobytes()
    if t + 1 < 200:
        assert not np.array_equal(before.iloc[t + 1 :], after.iloc[t + 1 :])


def test_stake_at_bar_t_is_unchanged_by_later_prices() -> None:
    closes = closes_4h(200)
    t = 100
    changed = closes.copy()
    changed.iloc[t + 1 :] *= 0.2

    def at_t(series: pd.Series) -> float:
        vol = realized_vol_annual(series, 20, BARS_PER_YEAR_4H)
        return stake(vol_target_annual=0.5, realized_vol_annual=float(vol.iloc[t]))

    assert at_t(closes) == at_t(changed)


def test_a_spike_raises_the_next_value_so_the_next_stake_shrinks() -> None:
    closes = closes_4h(120)
    spiked = closes.copy()
    spiked.iloc[100:] = spiked.iloc[100:] * 1.4
    calm = realized_vol_annual(closes, 20, BARS_PER_YEAR_4H)
    wild = realized_vol_annual(spiked, 20, BARS_PER_YEAR_4H)
    assert wild.iloc[99] == calm.iloc[99]
    assert wild.iloc[100] > calm.iloc[100]
    before = stake(vol_target_annual=0.5, realized_vol_annual=float(wild.iloc[99]))
    after = stake(vol_target_annual=0.5, realized_vol_annual=float(wild.iloc[100]))
    assert after < before


def test_gap_inside_the_window_gives_nan_and_is_never_filled() -> None:
    closes = closes_4h(60).drop(closes_4h(60).index[30])  # bar 30 is missing
    vol = realized_vol_annual(closes, 10, BARS_PER_YEAR_4H)
    # Position 30 is the first bar after the gap; its return spans the gap.
    assert vol.iloc[:30].notna().iloc[10:].all()
    assert vol.iloc[30:40].isna().all()
    assert vol.iloc[40:].notna().all()


def test_nan_and_non_positive_closes_blank_their_windows() -> None:
    for bad in (np.nan, 0.0, -5.0, np.inf):
        closes = closes_4h(40)
        closes.iloc[20] = bad
        vol = realized_vol_annual(closes, 5, BARS_PER_YEAR_4H)
        assert pd.notna(vol.iloc[19])  # the bar before is untouched
        assert vol.iloc[20:26].isna().all()
        assert vol.iloc[26:].notna().all()


def test_flat_closes_have_zero_volatility_and_the_minimum_stake() -> None:
    closes = pd.Series(100.0, index=closes_4h(30).index)
    vol = realized_vol_annual(closes, 5, BARS_PER_YEAR_4H)
    assert (vol.iloc[5:] == 0.0).all()
    assert stake(vol_target_annual=0.5, realized_vol_annual=float(vol.iloc[-1])) == 20.0


def test_annualisation_uses_the_callers_bar_count() -> None:
    closes = closes_4h(40).asfreq("4h")
    hourly = closes.copy()
    hourly.index = pd.date_range("2026-01-01", periods=40, freq="1h", tz="UTC")
    a = realized_vol_annual(closes, 5, 2190.0).iloc[-1]
    b = realized_vol_annual(hourly, 5, 8760.0).iloc[-1]
    assert b == pytest.approx(a * 2)


@pytest.mark.parametrize(
    ("window", "bars", "match"),
    [
        (1, 2190.0, "window"),
        (True, 2190.0, "window"),
        (2.0, 2190.0, "window"),
        (5, 0.0, "bars_per_year"),
        (5, math.nan, "bars_per_year"),
        (5, 8760.0 / 8, "does not match"),  # 8h bars asked of a 4h series
    ],
)
def test_realised_volatility_refuses_bad_input(window: Any, bars: float, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        realized_vol_annual(closes_4h(30), window, bars)


def test_realised_volatility_refuses_a_bad_index() -> None:
    with pytest.raises(ValueError, match="DatetimeIndex"):
        realized_vol_annual(pd.Series([1.0, 2.0, 3.0]), 2, 2190.0)
    with pytest.raises(ValueError, match="ascending"):
        realized_vol_annual(closes_4h(10).iloc[::-1], 2, 2190.0)


# --------------------------------------------------------------------------- dataclass validation


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"risk_per_trade_pct": 0.49}, "0.5-1.5%"),
        ({"risk_per_trade_pct": 1.51}, "0.5-1.5%"),
        ({"risk_per_trade_pct": math.nan}, "risk_per_trade_pct"),
        ({"justification": ""}, "justification"),
        ({"justification": "   "}, "justification"),
        ({"justification": "two\nlines"}, "justification"),
        ({"wallet": 0.0}, "wallet"),
        ({"wallet": -10.0}, "wallet"),
        ({"min_stake_pct": 30.0}, "min_stake_pct"),
        ({"min_stake_pct": -1.0}, "min_stake_pct"),
        ({"max_stake_pct": 0.0}, "max_stake_pct"),
        ({"max_total_exposure_pct": 120.0}, "no leverage"),
        ({"max_open_trades": 6}, "exposure cap"),
        ({"max_open_trades": 5}, "stays fundable"),
        ({"max_stake_pct": 25.0}, "stays fundable"),
        ({"max_open_trades": 1, "max_stake_pct": 25.0, "max_total_exposure_pct": 25.0}, "fundable"),
        ({"min_stake_pct": 0.0}, "min_stake_pct"),
        ({"min_stake_pct": 0.5}, "exchange order"),
        ({"max_open_trades": 0}, "max_open_trades"),
        ({"max_open_trades": 1.5}, "max_open_trades"),
        ({"max_open_trades": True}, "max_open_trades"),
        ({"size_multiplier": 3.0, "prior_test": "small"}, "conviction cap"),
        ({"size_multiplier": 3}, "conviction cap"),
        ({"size_multiplier": 0.5}, "conviction cap"),
        ({"size_multiplier": 1.5}, "prior_test"),
        ({"vol_target_annual": 0.5}, "both be set"),
        ({"vol_window_bars": 42}, "both be set"),
        ({"vol_target_annual": 0.0, "vol_window_bars": 42}, "vol_target_annual"),
        ({"vol_target_annual": 0.5, "vol_window_bars": 1}, "vol_window_bars"),
        ({"prior_test": "  "}, "prior_test"),
    ],
)
def test_sizing_refusals_name_the_rule(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        sizing(**overrides)


@pytest.mark.parametrize("risk_pct", [0.5, 1.5])
def test_sizing_range_is_inclusive(risk_pct: float) -> None:
    assert sizing(risk_per_trade_pct=risk_pct).risk_per_trade_pct == risk_pct


def test_multiplier_two_with_a_prior_test_is_allowed() -> None:
    assert sizing(size_multiplier=2, prior_test="small").size_multiplier == 2.0


def test_sizing_is_frozen_and_ints_become_floats() -> None:
    s = sizing(wallet=1000, risk_per_trade_pct=1)
    assert isinstance(s.wallet, float)
    assert isinstance(s.risk_per_trade_pct, float)
    with pytest.raises(FrozenInstanceError):
        s.wallet = 2000.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cooldown_candles", 0),
        ("stoploss_guard_trade_limit", -1),
        ("max_drawdown_stop_candles", 1.5),
        ("max_drawdown_lookback_candles", True),
        ("max_drawdown_allowed", 0.0),
        ("max_drawdown_allowed", 1.0),
        ("max_drawdown_allowed", 20.0),
    ],
)
def test_protections_refuse_bad_values_by_name(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        replace(PROTECTIONS, **{field: value})


def test_the_breaker_wallet_is_required_and_positive() -> None:
    table = {"max_daily_loss_pct": 3.0, "max_drawdown_pct": 10.0}
    with pytest.raises(ValueError, match="missing keys: wallet"):
        risk_controls_from_dict({"protections": None, "circuit_breaker": table})
    with pytest.raises(ValueError, match=r"circuit_breaker\.wallet"):
        CircuitBreaker(3.0, 10.0, 0.0)


def test_the_breaker_and_the_sizing_block_share_one_wallet() -> None:
    controls = RiskControls(protections=None, circuit_breaker=CircuitBreaker(3.0, 10.0, 2000.0))
    with pytest.raises(ValueError, match="one frozen wallet"):
        RiskConfig(risk_controls=controls, sizing=sizing())


@pytest.mark.parametrize(
    ("daily", "drawdown", "match"),
    [
        (0.0, 10.0, "max_daily_loss_pct"),
        (3.0, -1.0, "max_drawdown_pct"),
        (math.nan, 10.0, "max_daily_loss_pct"),
        (3.0, 101.0, "max_drawdown_pct"),
    ],
)
def test_circuit_breaker_refuses_bad_values(daily: float, drawdown: float, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CircuitBreaker(max_daily_loss_pct=daily, max_drawdown_pct=drawdown, wallet=1000.0)


def test_risk_controls_need_at_least_one_block() -> None:
    with pytest.raises(ValueError, match="risk_controls"):
        RiskControls(protections=None, circuit_breaker=None)
    assert RiskControls(protections=PROTECTIONS, circuit_breaker=None).circuit_breaker is None
    assert RiskControls(protections=None, circuit_breaker=BREAKER).protections is None


# --------------------------------------------------------------------------- (de)serialisation


@pytest.mark.parametrize(
    "controls",
    [
        RiskControls(protections=PROTECTIONS, circuit_breaker=BREAKER),
        RiskControls(protections=None, circuit_breaker=BREAKER),
        RiskControls(protections=PROTECTIONS, circuit_breaker=None),
    ],
)
def test_risk_controls_round_trip_through_json(controls: RiskControls) -> None:
    data = json.loads(json.dumps(risk_controls_to_dict(controls), allow_nan=False))
    assert risk_controls_from_dict(data) == controls


@pytest.mark.parametrize(
    "value",
    [
        sizing(),
        sizing(vol_target_annual=0.5, vol_window_bars=42),
        sizing(size_multiplier=2.0, prior_test="small"),
    ],
)
def test_sizing_round_trip_through_json(value: Sizing) -> None:
    data = json.loads(json.dumps(sizing_to_dict(value), allow_nan=False))
    assert sizing_from_dict(data) == value


def test_sizing_dict_is_stable_for_int_and_float_input() -> None:
    assert json.dumps(sizing_to_dict(sizing(wallet=1000))) == json.dumps(
        sizing_to_dict(sizing(wallet=1000.0))
    )


def test_from_dict_refuses_unknown_and_missing_keys() -> None:
    data = sizing_to_dict(sizing())
    with pytest.raises(ValueError, match="unknown keys: leverage"):
        sizing_from_dict({**data, "leverage": 3})
    without = {k: v for k, v in data.items() if k != "wallet"}
    with pytest.raises(ValueError, match="missing keys: wallet"):
        sizing_from_dict(without)
    controls = risk_controls_to_dict(RiskControls(PROTECTIONS, BREAKER))
    with pytest.raises(ValueError, match="unknown keys: extra"):
        risk_controls_from_dict({**controls, "extra": {}})
    bad = {**controls, "circuit_breaker": {**BREAKER.__dict__, "typo": 1}}
    with pytest.raises(ValueError, match="circuit_breaker has unknown keys: typo"):
        risk_controls_from_dict(bad)
    with pytest.raises(ValueError, match="protections must be a table"):
        risk_controls_from_dict({"protections": 3})


@pytest.mark.parametrize("bad", [0, 1, 1.5, -0.1, True, "a third", math.nan])
def test_max_blocked_share_is_a_fraction_below_one(bad: Any) -> None:
    with pytest.raises(ValueError, match="max_blocked_share"):
        replace(PROTECTIONS, max_blocked_share=bad)


def test_max_blocked_share_is_required() -> None:
    table = {k: v for k, v in asdict(PROTECTIONS).items() if k != "max_blocked_share"}
    with pytest.raises(ValueError, match="missing keys: max_blocked_share"):
        risk_controls_from_dict({"protections": table})


def test_a_frozen_config_round_trips_and_refuses_nonsense() -> None:
    frozen = PaperConfig("binance", "data-api.binance.vision", 100.0, 1000.0, 1)
    assert paper_config_from_dict(paper_config_to_dict(frozen)) == frozen
    with pytest.raises(ValueError, match="above the wallet"):
        replace(frozen, stake_amount=2000.0)
    with pytest.raises(ValueError, match="stake_amount"):
        replace(frozen, stake_amount="all of it")
    with pytest.raises(ValueError, match="max_open_trades"):
        replace(frozen, max_open_trades=0)
    with pytest.raises(ValueError, match="unknown keys"):
        paper_config_from_dict({**paper_config_to_dict(frozen), "fee": 0.01})


def test_a_frozen_config_must_say_what_the_blocks_say() -> None:
    fixed = PaperConfig("binance", None, 100.0, 1000.0, 1)
    sized = PaperConfig("binance", None, UNLIMITED_STAKE, 1000.0, 4)
    controls = RiskControls(protections=None, circuit_breaker=BREAKER)
    check_frozen_config(fixed, controls, None)
    check_frozen_config(sized, None, sizing())
    with pytest.raises(ValueError, match="one frozen wallet"):
        check_frozen_config(replace(fixed, dry_run_wallet=2000.0), controls, None)
    with pytest.raises(ValueError, match="must be 4"):
        check_frozen_config(replace(sized, max_open_trades=2), None, sizing())
    with pytest.raises(ValueError, match="goes with a sizing block"):
        check_frozen_config(fixed, None, replace(sizing(), max_open_trades=1))
    with pytest.raises(ValueError, match="goes with a sizing block"):
        check_frozen_config(replace(fixed, stake_amount=UNLIMITED_STAKE), controls, None)


def test_example_toml_loads() -> None:
    config = load_risk_config(EXAMPLE_TOML)
    assert config.risk_controls is not None
    assert config.risk_controls.protections is not None
    assert config.risk_controls.circuit_breaker == BREAKER
    assert config.risk_controls.protections == PROTECTIONS
    assert config.sizing is not None
    assert config.sizing.size_multiplier == 1.0
    assert config.sizing.vol_window_bars == 42
    assert config.sizing.prior_test is None


def test_example_protection_can_fire_before_the_breaker() -> None:
    config = load_risk_config(EXAMPLE_TOML)
    assert config.risk_controls is not None
    protections = config.risk_controls.protections
    breaker = config.risk_controls.circuit_breaker
    assert protections is not None
    assert breaker is not None
    assert protections.max_drawdown_allowed * 100 < breaker.max_drawdown_pct


def test_example_says_the_sosnoff_rule_is_not_adopted() -> None:
    text = EXAMPLE_TOML.read_text(encoding="utf-8")
    assert "Not adopted" in text
    assert "may never change mid-test" in text
    assert "a different size is a new test" in text


def write_toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "risk.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_accepts_a_single_block(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        "[risk_controls.circuit_breaker]\nmax_daily_loss_pct = 3\nmax_drawdown_pct = 10\n"
        "wallet = 1000\n",
    )
    assert load_risk_config(path) == RiskConfig(
        risk_controls=RiskControls(protections=None, circuit_breaker=BREAKER), sizing=None
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[leverage]\nmax = 3\n", "unknown keys: leverage"),
        ("[risk_controls.typo]\na = 1\n", "unknown keys: typo"),
        ("", "neither risk_controls nor sizing"),
        ("not toml", "cannot read risk config"),
        ("[risk_controls]\n", "risk_controls needs"),
        (
            "[risk_controls.circuit_breaker]\nmax_daily_loss_pct = inf\nmax_drawdown_pct = 1\n"
            "wallet = 1000\n",
            "max_daily_loss_pct",
        ),
    ],
)
def test_load_refuses(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_risk_config(write_toml(tmp_path, text))


def test_load_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"cannot read risk config .*absent\.toml"):
        load_risk_config(tmp_path / "absent.toml")


# --------------------------------------------------------------------------- pre-start checks


@pytest.mark.parametrize("stop", [None, 0, 0.0, -5.0, math.nan, 100.0, True])
def test_sizing_without_a_stop_is_refused(stop: Any) -> None:
    with pytest.raises(ValueError, match="sized stakes need a stop"):
        check_sizing_against_plan(sizing(), stop)


def test_sizing_with_a_stop_passes() -> None:
    check_sizing_against_plan(sizing(), 5)
    check_sizing_against_plan(sizing(), 0.5)


PLAIN_STRATEGY = '''
"""A plain strategy. It never uses adjust_trade_position or martingale sizing."""
from freqtrade.strategy import IStrategy

# position_adjustment_enable = True would be refused; this comment is not.


class Plain(IStrategy):
    stoploss = -0.05
    use_custom_stoploss = False
    exit_profit_only = False
    position_adjustment_enable = False

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, **kwargs):
        stop_distance = abs(self.stoploss)
        closing_price = current_rate
        return min(proposed_stake, 10.0 / stop_distance + closing_price * 0)

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if current_profit > 0.1:
            return "take_it"
        return None
'''

DCA_ON_LOSS = """
from freqtrade.strategy import IStrategy


class Dca(IStrategy):
    position_adjustment_enable = True

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, **kwargs):
        if current_profit < -0.05:
            return trade.stake_amount
        return None
"""


@pytest.mark.parametrize(
    ("source", "match"),
    [
        (DCA_ON_LOSS, "position_adjustment_enable"),
        ("class S:\n    position_adjustment_enable: bool = True\n", "position_adjustment"),
        ("class S:\n    def f(self):\n        self.position_adjustment_enable = 1\n", "position"),
        ('def f(c):\n    c["position_adjustment_enable"] = True\n', "position_adjustment"),
        ('C = {"position_adjustment_enable": True}\n', "position_adjustment"),
        ("position_adjustment_enable = flag()\n", "position_adjustment"),
        ("class S:\n    def adjust_trade_position(self):\n        return 1\n", "defines adjust"),
        ("class S:\n    async def adjust_trade_position(self):\n        return 1\n", "defines"),
        ("class S:\n    adjust_trade_position = helper\n", "assigns adjust"),
        ("def martingale_stake(x):\n    return x\n", "Martingale"),
        ("class MartingaleBot:\n    pass\n", "Martingale"),
        ("double_down = 2\n", "Martingale"),
        ("averaging_down = 1\n", "Martingale"),
        ("dca_levels = 3\n", "Martingale"),
        (
            "def custom_stake_amount(self, proposed_stake, **kw):\n"
            "    if self.last_close_profit < 0:\n"
            "        return proposed_stake * 2\n"
            "    return proposed_stake\n",
            "custom_stake_amount reads a trade result",
        ),
        (
            "def custom_stake_amount(self, proposed_stake, **kw):\n"
            "    return proposed_stake * (2 ** self.losses)\n",
            "custom_stake_amount reads a trade result",
        ),
        (
            "def custom_stake_amount(self, proposed_stake, **kw):\n"
            '    return proposed_stake * trades[-1]["pnl"]\n',
            "custom_stake_amount reads a trade result",
        ),
        (
            "def on_exit(self, trade):\n"
            "    if trade.close_profit < 0:\n"
            "        self.stake_multiplier *= 2\n",
            "changes stake_multiplier on a trade result",
        ),
        (
            "def on_exit(self, trade):\n"
            "    if trade.close_profit >= 0:\n"
            "        pass\n"
            "    else:\n"
            "        self.next_size = self.next_size * 2\n",
            "changes next_size on a trade result",
        ),
    ],
)
def test_averaging_down_is_refused(source: str, match: str) -> None:
    with pytest.raises(ValueError, match="no averaging down rule") as err:
        check_no_averaging_down(source, {})
    assert match in str(err.value)


def test_config_turning_it_on_is_refused() -> None:
    with pytest.raises(ValueError, match="no averaging down rule: config"):
        check_no_averaging_down(PLAIN_STRATEGY, {"position_adjustment_enable": True})


def test_plain_strategy_passes() -> None:
    check_no_averaging_down(PLAIN_STRATEGY, {"position_adjustment_enable": False})
    check_no_averaging_down(PLAIN_STRATEGY, {})


def test_source_that_does_not_parse_is_refused() -> None:
    with pytest.raises(ValueError, match="does not parse"):
        check_no_averaging_down("def broken(:\n", {})


def test_generated_strategy_passes() -> None:
    plan = {
        "name": "ema_paper",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "features": {"fast": {"ema": {"period": 12}}, "slow": {"ema": {"period": 40}}},
        "long_entry": "crosses_above(fast, slow)",
        "long_exit": "crosses_below(fast, slow)",
        "risk": {"position_size_pct": 100, "stop_loss_pct": 5, "take_profit_pct": 8},
    }
    check_no_averaging_down(render_strategy(plan, class_name="TvPaperTest"), {})


def test_embedded_stake_function_passes_the_screen() -> None:
    """The integration embeds sized_stake and calls it from custom_stake_amount."""
    body = "\n".join(f"    {line}" for line in inspect.getsource(sized_stake).splitlines())
    source = (
        "import math\n\n"
        "class Sized:\n"
        "    stoploss = -0.05\n\n"
        f"{body}\n\n"
        "    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake,\n"
        "                            min_stake, max_stake, leverage, entry_tag, side, **kwargs):\n"
        "        return sized_stake(stop_distance=abs(self.stoploss), wallet=1000.0,\n"
        "                           risk_per_trade_pct=1.0, max_stake_pct=25.0,\n"
        "                           min_stake_pct=2.0)\n"
    )
    check_no_averaging_down(source, {})


LOGIC = "a" * 64
PLAN_HASH = "c" * 64
SAME_PLAN: dict[str, Any] = {"plan_hash": PLAN_HASH, "logic_hash": LOGIC, "stop_loss_pct": 5.0}


def prior(**overrides: Any) -> PriorTest:
    values: dict[str, Any] = {
        "name": "small",
        "sizing": sizing(risk_per_trade_pct=0.5),
        "decision_days": 90.0,
        "final_verdict": "pass",
        "logic_hash": LOGIC,
        "plan_hash": PLAN_HASH,
        "mode": "sharpe_and_implementation",
        "locked_split_check": "passed",
        "halted": False,
        "unlocked": (),
    }
    return PriorTest(**{**values, **overrides})


def step_up(**overrides: Any) -> None:
    new = sizing(risk_per_trade_pct=1.0, prior_test="small")
    check_size_step(new, prior(**overrides), **SAME_PLAN)


def test_first_test_at_base_size_passes() -> None:
    check_size_step(sizing(), None, **SAME_PLAN)


def test_ninety_days_with_a_pass_is_accepted() -> None:
    step_up()


def test_eighty_nine_days_is_refused() -> None:
    with pytest.raises(ValueError, match=r"size step rule.*at least 90 days"):
        step_up(decision_days=89.99)
    with pytest.raises(ValueError, match="at least 90 days"):
        step_up(decision_days=89)


def test_default_protocol_horizon_cannot_unlock_a_step() -> None:
    for days in (28, 56):
        with pytest.raises(ValueError, match="at least 90 days"):
            step_up(decision_days=days)
    assert "28-56 day" in " ".join((check_size_step.__doc__ or "").split())


@pytest.mark.parametrize("verdict", ["fail", "inconclusive", "continue", None])
def test_a_prior_that_did_not_pass_is_refused(verdict: str | None) -> None:
    with pytest.raises(ValueError, match=r"size step rule.*must have passed"):
        step_up(final_verdict=verdict)


def test_a_step_above_two_times_is_refused() -> None:
    new = sizing(risk_per_trade_pct=1.25, prior_test="small")
    with pytest.raises(ValueError, match=r"size step rule.*2\.5x"):
        check_size_step(new, prior(), **SAME_PLAN)


def test_level_counts_the_multiplier() -> None:
    # Level 1.0 x 2 = 2.0 against 0.75 x 1 = 0.75 is 2.67x, though each factor looks modest.
    new = sizing(size_multiplier=2.0, prior_test="small")
    wide_stop = {**SAME_PLAN, "stop_loss_pct": 10.0}  # at 5% the 20% stake cap binds both tests
    with pytest.raises(ValueError, match="size step rule"):
        check_size_step(new, prior(sizing=sizing(risk_per_trade_pct=0.75)), **wide_stop)
    check_size_step(new, prior(sizing=sizing(risk_per_trade_pct=1.0)), **wide_stop)


@pytest.mark.parametrize("prior_pct", [0.5, 0.55, 0.6, 0.65, 0.7, 0.75])
def test_an_exact_double_is_not_refused_on_rounding(prior_pct: float) -> None:
    new = sizing(risk_per_trade_pct=prior_pct * 2, prior_test="small")
    check_size_step(new, prior(sizing=sizing(risk_per_trade_pct=prior_pct)), **SAME_PLAN)


def test_missing_prior_sizing_is_refused() -> None:
    with pytest.raises(ValueError, match="no frozen sizing block"):
        step_up(sizing=None)


def test_missing_duration_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 90 days"):
        step_up(decision_days=None)
    with pytest.raises(ValueError, match="at least 90 days"):
        step_up(decision_days=math.nan)


def test_a_registered_prior_cannot_be_skipped_or_swapped() -> None:
    registered = sizing(size_multiplier=2.0, prior_test="small")
    with pytest.raises(ValueError, match="is not the sizing block's prior_test"):
        check_size_step(registered, None, **SAME_PLAN)
    with pytest.raises(ValueError, match="is not the sizing block's prior_test"):
        check_size_step(registered, prior(name="other"), **SAME_PLAN)


def test_a_pass_of_another_plan_unlocks_nothing() -> None:
    """Any passing test used to unlock any plan, size_multiplier 2 on day one included."""
    new = sizing(size_multiplier=2.0, prior_test="small")
    with pytest.raises(ValueError, match="other logic"):
        check_size_step(new, prior(logic_hash="b" * 64), **SAME_PLAN)


def test_a_re_tuned_plan_cannot_borrow_a_pass() -> None:
    """logic_hash drops parameters, literals, stops and the interval: EMA 5/60 at a 2.5% stop
    borrowed the 90-day pass of EMA 8/30 at 10%, and its "2x" step staked 4x as much."""
    new = sizing(risk_per_trade_pct=1.0, prior_test="small")
    with pytest.raises(ValueError, match=r"size step rule.*is another plan.*re-tune"):
        check_size_step(new, prior(plan_hash="d" * 64), **SAME_PLAN)


GUARDS = Protections(2, 24, 3, 12, 42, 5, 24, 0.06, 1 / 3)
TRIP = CircuitBreaker(max_daily_loss_pct=3.0, max_drawdown_pct=10.0, wallet=1000.0)


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (RiskControls(GUARDS, TRIP), None, "protections this step does not keep"),
        (RiskControls(GUARDS, None), RiskControls(None, TRIP), "does not keep"),
        (
            RiskControls(GUARDS, None),
            RiskControls(replace(GUARDS, max_drawdown_allowed=0.5), None),
            "does not keep",
        ),
        (RiskControls(None, TRIP), None, "circuit breaker this step drops"),
        (RiskControls(None, TRIP), RiskControls(GUARDS, None), "this step drops"),
        (
            RiskControls(None, TRIP),
            RiskControls(None, replace(TRIP, max_drawdown_pct=50.0)),
            "looser",
        ),
        (
            RiskControls(None, TRIP),
            RiskControls(None, replace(TRIP, max_daily_loss_pct=4.0)),
            "looser",
        ),
    ],
)
def test_a_step_keeps_what_the_prior_passed_under(
    before: RiskControls, after: RiskControls | None, message: str
) -> None:
    new = sizing(risk_per_trade_pct=1.0, prior_test="small")
    with pytest.raises(ValueError, match=message):
        check_size_step(new, prior(risk_controls=before), **SAME_PLAN, risk_controls=after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, None),
        (None, RiskControls(GUARDS, TRIP)),  # a step may add a guard
        (RiskControls(GUARDS, TRIP), RiskControls(GUARDS, TRIP)),
        (RiskControls(None, TRIP), RiskControls(GUARDS, TRIP)),
        (RiskControls(None, TRIP), RiskControls(None, replace(TRIP, max_drawdown_pct=5.0))),
    ],
)
def test_a_step_that_keeps_its_guards_stands(
    before: RiskControls | None, after: RiskControls | None
) -> None:
    new = sizing(risk_per_trade_pct=1.0, prior_test="small")
    check_size_step(new, prior(risk_controls=before), **SAME_PLAN, risk_controls=after)


def test_the_same_size_is_no_step() -> None:
    same = sizing(prior_test="small")
    with pytest.raises(ValueError, match="a step must be a step"):
        check_size_step(same, prior(sizing=sizing()), **SAME_PLAN)


@pytest.mark.parametrize(
    "raised",
    [
        {"min_stake_pct": 3.0},  # no volatility target: the floor is never read
        {"max_stake_pct": 39.0, "max_open_trades": 2},  # the 20% base stake never reaches it
    ],
)
def test_a_number_no_stake_can_reach_is_no_step(raised: dict[str, Any]) -> None:
    """1% risk at a 5% stop stakes 200, or 20% of the wallet, whatever these two say."""
    with pytest.raises(ValueError, match="a step must be a step"):
        check_size_step(sizing(prior_test="small", **raised), prior(sizing=sizing()), **SAME_PLAN)


def test_a_higher_floor_is_a_step_when_a_volatility_target_can_reach_it() -> None:
    vol = {"vol_target_annual": 0.5, "vol_window_bars": 42}
    check_size_step(
        sizing(prior_test="small", min_stake_pct=4.0, **vol),
        prior(sizing=sizing(**vol)),
        **SAME_PLAN,
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"max_stake_pct": 5.0}, {"max_stake_pct": 20.0}),  # the stake really used, at a tight stop
        ({"wallet": 400.0, "min_stake_pct": 5.0}, {}),  # same percent, a 2.5x wallet
        ({"min_stake_pct": 1.0}, {"min_stake_pct": 5.0}),  # the floor is a size too
    ],
)
def test_a_step_is_measured_on_real_size(old: dict[str, Any], new: dict[str, Any]) -> None:
    """risk x multiplier alone let every other number grow without limit."""
    stepped = sizing(risk_per_trade_pct=1.0, prior_test="small", **new)
    earlier = prior(sizing=sizing(risk_per_trade_pct=1.0, **old))
    with pytest.raises(ValueError, match=r"size step rule.*at most one 2x step"):
        check_size_step(stepped, earlier, **SAME_PLAN)


def test_a_step_keeps_the_volatility_target_and_window() -> None:
    new = sizing(prior_test="small", vol_target_annual=2.0, vol_window_bars=42)
    base = sizing(risk_per_trade_pct=0.5, vol_target_annual=0.2, vol_window_bars=42)
    with pytest.raises(ValueError, match="volatility target and window must match"):
        check_size_step(new, prior(sizing=base), **SAME_PLAN)
    other_window = replace(base, vol_target_annual=2.0, vol_window_bars=21)
    with pytest.raises(ValueError, match="volatility target and window must match"):
        check_size_step(new, prior(sizing=other_window), **SAME_PLAN)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"mode": "implementation_only"}, "implementation_only"),
        ({"locked_split_check": "unchecked"}, "NOT a success candidate"),
        ({"locked_split_check": "passed_other_ledger"}, "NOT a success candidate"),
        ({"locked_split_check": "family_checked_not_this_plan"}, "NOT a success candidate"),
        ({"halted": True}, "halt.lock"),
        ({"unlocked": ("earlier-step",)}, "one proof, one step"),
    ],
)
def test_a_pass_that_is_no_proof_is_refused(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        step_up(**overrides)


def test_the_minimum_stake_may_not_risk_more_than_the_registered_value() -> None:
    """With a 50% stop the base stake is 20 of 1000, so a 3% floor would risk 1.5%, not 1%."""
    block = sizing(min_stake_pct=3.0, vol_target_annual=0.5, vol_window_bars=42)
    with pytest.raises(ValueError, match="floor would risk more"):
        check_sizing_against_plan(block, 50.0)
    check_sizing_against_plan(block, 5.0)


def test_a_plain_stake_must_reach_the_exchange_minimum() -> None:
    small = sizing(wallet=400.0, min_stake_pct=2.0, risk_per_trade_pct=0.5)
    with pytest.raises(ValueError, match="exchange order"):
        check_sizing_against_plan(small, 50.0)  # 400 x 0.5% / 50% = 4
    check_sizing_against_plan(small, 5.0)


# --------------------------------------------------------------------------- circuit breaker


def equity_at(values: list[float], start: str = "2026-03-01 16:00") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq="4h", tz="UTC"),
        dtype="float64",
    )


def test_quiet_equity_does_not_trip() -> None:
    assert evaluate_breaker(equity_at([100.0, 100.5, 99.9, 100.2]), BREAKER) is None


def test_empty_equity_does_not_trip() -> None:
    empty = pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    assert evaluate_breaker(empty, BREAKER) is None


def test_daily_loss_trips_on_the_exact_bar_across_midnight() -> None:
    # 16:00 and 20:00 on day 1; 00:00, 04:00, 08:00 on day 2. Day 1 ends at 100. Measured from
    # the first value, 98 would already be 3.9%; the new day resets the reference.
    equity = equity_at([102.0, 100.0, 99.0, 98.0, 97.0])
    breaker = CircuitBreaker(max_daily_loss_pct=3.0, max_drawdown_pct=50.0, wallet=1000.0)
    trip = evaluate_breaker(equity, breaker)
    assert trip == BreakerTrip("daily_loss", 3.0, 3.0, "2026-03-02T08:00:00+00:00")
    assert evaluate_breaker(equity.iloc[:-1], breaker) is None


def test_loss_before_midnight_does_not_carry_into_the_new_day() -> None:
    # A 3.9% fall on day 1 stays below a 4% limit. Day 2 is measured from the day 1 close, so
    # the next 3.9% fall does not trip either, though it is 7.6% from the first value.
    equity = equity_at([100.0, 96.1, 92.4])
    assert evaluate_breaker(equity, CircuitBreaker(4.0, 50.0, 1000.0)) is None


def test_first_utc_day_measures_from_the_first_value() -> None:
    equity = equity_at([100.0, 99.0, 97.0], start="2026-03-01 00:00")
    trip = evaluate_breaker(equity, CircuitBreaker(3.0, 50.0, 1000.0))
    assert trip is not None
    assert trip.reason == "daily_loss"
    assert trip.time == "2026-03-01T08:00:00+00:00"


def test_daily_reference_skips_a_day_with_no_bars() -> None:
    index = pd.to_datetime(["2026-03-01 20:00", "2026-03-03 04:00", "2026-03-03 08:00"], utc=True)
    equity = pd.Series([100.0, 98.0, 97.0], index=index)
    trip = evaluate_breaker(equity, CircuitBreaker(3.0, 50.0, 1000.0))
    assert trip is not None
    assert trip.time == "2026-03-03T08:00:00+00:00"


def test_drawdown_trips_on_the_exact_bar_across_midnight() -> None:
    # Small daily falls add up. Peak 100 at 16:00 on day 1; 90 arrives at 04:00 on day 3.
    values = [100.0, 98.0, 96.0, 95.0, 94.0, 93.0, 92.0, 91.5, 91.0, 90.5, 90.2, 90.1, 90.05, 90.0]
    equity = equity_at(values)
    breaker = CircuitBreaker(max_daily_loss_pct=50.0, max_drawdown_pct=10.0, wallet=1000.0)
    trip = evaluate_breaker(equity, breaker)
    assert trip is not None
    assert trip.reason == "drawdown"
    assert trip.threshold == 10.0
    assert trip.value == 10.0
    assert trip.time == equity.index[-1].isoformat()
    assert evaluate_breaker(equity.iloc[:-1], breaker) is None


def test_exact_threshold_trips_despite_binary_rounding() -> None:
    # 1 - 90 / 100 is 0.09999999999999998 in floats; at-or-beyond must still hold.
    assert 1 - 90.0 / 100.0 < 0.1
    trip = evaluate_breaker(equity_at([100.0, 90.0]), CircuitBreaker(50.0, 10.0, 1000.0))
    assert trip is not None
    assert trip.value == 10.0


def test_running_peak_includes_new_highs() -> None:
    equity = equity_at([100.0, 120.0, 110.0, 108.0])
    trip = evaluate_breaker(equity, CircuitBreaker(50.0, 10.0, 1000.0))
    assert trip is not None
    assert trip.time == equity.index[3].isoformat()


def test_daily_loss_wins_a_tie_on_one_bar() -> None:
    trip = evaluate_breaker(equity_at([100.0, 90.0]), CircuitBreaker(3.0, 10.0, 1000.0))
    assert trip is not None
    assert trip.reason == "daily_loss"
    assert trip.threshold == 3.0
    assert trip.value == 10.0


def test_breaker_is_causal() -> None:
    equity = equity_at([100.0, 99.0, 96.0, 95.0])
    first = evaluate_breaker(equity, CircuitBreaker(3.0, 50.0, 1000.0))
    later = equity.copy()
    later.iloc[3:] = 5.0
    assert evaluate_breaker(later, CircuitBreaker(3.0, 50.0, 1000.0)) == first


def test_wipeout_after_a_trip_does_not_break_the_walk() -> None:
    trip = evaluate_breaker(equity_at([100.0, 90.0, 0.0, 0.0]), BREAKER)
    assert trip is not None
    assert trip.time == equity_at([100.0, 90.0]).index[1].isoformat()


@pytest.mark.parametrize(
    ("equity", "match"),
    [
        (pd.Series([100.0, 99.0]), "DatetimeIndex"),
        (
            pd.Series([100.0], index=pd.date_range("2026-01-01", periods=1, freq="4h")),
            "tz-aware UTC",
        ),
        (
            pd.Series(
                [100.0], index=pd.date_range("2026-01-01", periods=1, freq="4h", tz="Asia/Tokyo")
            ),
            "tz-aware UTC",
        ),
        (equity_at([100.0, 99.0]).iloc[::-1], "ascending"),
        (equity_at([100.0, np.nan]), "non-finite"),
        (equity_at([0.0, 99.0]), "start positive"),
    ],
)
def test_breaker_refuses_bad_equity(equity: pd.Series, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        evaluate_breaker(equity, BREAKER)


# --------------------------------------------------------------------------- halt.lock

TRIP = BreakerTrip("drawdown", 10.0, 10.5, "2026-03-02T08:00:00+00:00")


def test_write_creates_a_lock_with_exactly_the_four_keys(tmp_path: Path) -> None:
    path = write_halt_lock(tmp_path, TRIP)
    assert path == tmp_path / "halt.lock"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "reason": "drawdown",
        "threshold": 10.0,
        "value": 10.5,
        "time": "2026-03-02T08:00:00+00:00",
    }
    assert read_halt_lock(tmp_path) == json.loads(path.read_text(encoding="utf-8"))


def test_a_failed_write_leaves_no_partial_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty lock left by a crash would halt the test with no reason in it."""

    def crash(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(risk.os, "fsync", crash, raising=False)
    with pytest.raises(OSError, match="disk full"):
        write_halt_lock(tmp_path, TRIP)
    assert list(tmp_path.iterdir()) == []


def test_the_lock_appears_whole_and_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = risk.os.link

    def link(source: str, target: Path) -> None:
        seen.append(Path(source).read_text(encoding="utf-8"))
        assert not Path(target).exists()
        real(source, target)

    monkeypatch.setattr(risk.os, "link", link, raising=False)
    path = write_halt_lock(tmp_path, TRIP)
    assert seen == [path.read_text(encoding="utf-8")]
    assert [p.name for p in tmp_path.iterdir()] == ["halt.lock"]


def test_write_creates_a_missing_test_folder(tmp_path: Path) -> None:
    path = write_halt_lock(tmp_path / "a" / "b", TRIP)
    assert path.exists()


def test_an_existing_lock_is_left_byte_identical(tmp_path: Path) -> None:
    path = write_halt_lock(tmp_path, TRIP)
    first = path.read_bytes()
    other = BreakerTrip("daily_loss", 3.0, 4.0, "2026-03-03T00:00:00+00:00")
    assert write_halt_lock(tmp_path, other) == path
    assert path.read_bytes() == first


def test_a_hand_edited_lock_is_also_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "halt.lock"
    path.write_bytes(b"owner note\n")
    assert write_halt_lock(tmp_path, TRIP) == path
    assert path.read_bytes() == b"owner note\n"


def test_read_without_a_lock_is_none(tmp_path: Path) -> None:
    assert read_halt_lock(tmp_path) is None
    assert read_halt_lock(tmp_path / "missing") is None


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_read_refuses_a_bad_lock(tmp_path: Path, text: str) -> None:
    (tmp_path / "halt.lock").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"halt\.lock"):
        read_halt_lock(tmp_path)


def test_no_lock_is_not_halted(tmp_path: Path) -> None:
    check_not_halted(tmp_path)


def test_the_lock_blocks_and_names_the_reason_and_the_owner(tmp_path: Path) -> None:
    write_halt_lock(tmp_path, TRIP)
    with pytest.raises(ValueError, match=r"drawdown.*only the owner removes") as err:
        check_not_halted(tmp_path)
    assert "halt.lock" in str(err.value)
    assert (tmp_path / "halt.lock").exists()


@pytest.mark.parametrize("content", [b"", b"not json", b"\xff\xfe"])
def test_an_unreadable_lock_still_halts(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "halt.lock").write_bytes(content)
    with pytest.raises(ValueError, match="only the owner removes"):
        check_not_halted(tmp_path)


def test_a_lock_written_by_a_real_trip_blocks_restart(tmp_path: Path) -> None:
    trip = evaluate_breaker(equity_at([100.0, 96.0]), BREAKER)
    assert trip is not None
    write_halt_lock(tmp_path, trip)
    with pytest.raises(ValueError, match="daily_loss"):
        check_not_halted(tmp_path)


# --------------------------------------------------------------------------- safety


def test_nothing_in_the_module_can_delete_the_lock_or_reach_an_exchange() -> None:
    source = inspect.getsource(risk)
    tree = ast.parse(source)
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    called |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not called & {"unlink", "remove", "rmdir", "rmtree", "rename", "replace"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for word in ("delete", "remove", "clear", "unlock", "order"):
                assert word not in node.name


def test_module_imports_stay_within_the_allowed_set() -> None:
    tree = ast.parse(inspect.getsource(risk))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    third_party = {r for r in roots if r not in sys.stdlib_module_names}
    assert third_party == {"numpy", "pandas"}
