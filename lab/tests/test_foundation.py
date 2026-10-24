import pytest

from lab.config import load_config
from lab.types import bars_per_year, validate_candles
from tests.synthetic import make_candles, perturb_after


def test_bars_per_year_uses_365_days() -> None:
    assert bars_per_year("4h") == 2190.0
    assert bars_per_year("1d") == 365.0


def test_unknown_interval_names_the_input() -> None:
    with pytest.raises(ValueError, match="'7h'"):
        bars_per_year("7h")


def test_synthetic_candles_follow_the_convention() -> None:
    validate_candles(make_candles(50, seed=1))


def test_perturb_after_leaves_prefix_identical() -> None:
    c = make_candles(50, seed=1)
    p = perturb_after(c, 20)
    assert c.iloc[:21].equals(p.iloc[:21])
    assert not c.iloc[21:].equals(p.iloc[21:])


def test_config_loads_with_crypto_defaults() -> None:
    cfg = load_config()
    assert cfg.costs.fee_rate == 0.001
    assert cfg.splits.holdout_start.isoformat() == "2025-10-01T00:00:00+00:00"
    assert cfg.splits.design_start < cfg.splits.holdout_start
    assert cfg.api_port == 8788
