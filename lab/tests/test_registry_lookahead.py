"""Perturbation tests for every registry feature (ported from hedgeFund's
``tests/test_no_lookahead.py``).

The pattern: change every candle strictly after bar t and assert that every output at or
before t is bit-identical. It iterates ``REGISTRY``, so a feature added later is covered
without editing this file. The candles carry the six aligned ``drv_*`` derivatives columns
(with NaN gaps) so the derivatives features run the same test. Like hedgeFund's version it
also checks that the perturbation has teeth (later outputs do change) and that computation
is deterministic.

Statistics cannot catch look-ahead, so this test is mandatory (.claude/rules/no-lookahead.md).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.registry import REGISTRY, fill_params
from lab.types import FeatureSpec
from tests.synthetic import make_candles, perturb_after, with_derivatives

N_BARS = 600
SEED = 20261024


def alt_params(spec: FeatureSpec) -> dict[str, float]:
    """A non-default but valid param set: each param moved halfway towards its lower bound
    (or one step up when that rounds back to the default)."""
    out: dict[str, float] = {}
    for p in spec.params:
        value = p.low + (p.default - p.low) / 2
        if p.kind == "int":
            value = round(value)
            if value == p.default:
                value = min(p.default + 1, p.high)
        out[p.name] = value
    return fill_params(spec, out)


def param_sets(spec: FeatureSpec) -> list[dict[str, float]]:
    sets = [fill_params(spec, {}), alt_params(spec)]
    assert sets[0] != sets[1] or not spec.params, spec.name
    return sets


CASES = [
    pytest.param(name, params, id=f"{name}-{'default' if i == 0 else 'alt'}")
    for name, spec in REGISTRY.items()
    for i, params in enumerate(param_sets(spec))
]


def cut_points(warmup: int) -> list[int]:
    raw = [0, 3, warmup - 1, warmup, warmup + 1, N_BARS // 2, N_BARS - 2]
    return sorted({t for t in raw if 0 <= t <= N_BARS - 2})


@pytest.fixture(scope="module")
def candles() -> pd.DataFrame:
    """Prices plus derivatives columns with archive gaps (NaN rows) in every drv_* column."""
    return with_derivatives(make_candles(N_BARS, seed=SEED, vol=0.02), seed=SEED)


@pytest.fixture(scope="module")
def clean_candles() -> pd.DataFrame:
    """The same frame without derivatives gaps, for the warm-up contract (NaN only in the
    declared warm-up holds on gap-free data only)."""
    return with_derivatives(make_candles(N_BARS, seed=SEED, vol=0.02), seed=SEED, gaps=False)


@pytest.mark.parametrize(("name", "params"), CASES)
def test_outputs_through_t_are_bit_identical_after_perturbing_the_future(
    candles: pd.DataFrame, name: str, params: dict[str, float]
) -> None:
    spec = REGISTRY[name]
    base = spec.compute(candles, params)
    for t in cut_points(spec.warmup(params)):
        shocked = spec.compute(perturb_after(candles, t), params)
        assert list(shocked.columns) == list(base.columns)
        assert shocked.index.equals(base.index)
        for col in spec.outputs:
            before = base[col].to_numpy()[: t + 1]
            after = shocked[col].to_numpy()[: t + 1]
            assert np.array_equal(before, after, equal_nan=True), (
                f"{name}.{col} {params} changed at or before t={t} after perturbing the future"
            )


@pytest.mark.parametrize(("name", "params"), CASES)
def test_perturbation_actually_changes_later_outputs(
    candles: pd.DataFrame, name: str, params: dict[str, float]
) -> None:
    """Sanity: the shock is big enough that something after t moves, so the test has teeth."""
    spec = REGISTRY[name]
    t = N_BARS // 2
    base = spec.compute(candles, params).to_numpy()[t + 1 :]
    shocked = spec.compute(perturb_after(candles, t), params).to_numpy()[t + 1 :]
    assert not np.array_equal(base, shocked, equal_nan=True), name


@pytest.mark.parametrize(("name", "params"), CASES)
def test_output_shape_and_warmup_contract(
    clean_candles: pd.DataFrame, name: str, params: dict[str, float]
) -> None:
    candles = clean_candles
    spec = REGISTRY[name]
    out = spec.compute(candles, params)
    warmup = spec.warmup(params)
    assert tuple(out.columns) == spec.outputs
    assert out.index.equals(candles.index)
    assert all(out[c].dtype == np.float64 for c in spec.outputs)
    assert 0 <= warmup < N_BARS
    # NaN only inside the declared warm-up on clean data ...
    assert out.iloc[warmup:].notna().all().all(), f"{name}: NaN after warmup={warmup}"
    # ... and the warm-up is not overstated (the engine forces signals False during it).
    if warmup > 0:
        assert out.iloc[warmup - 1].isna().any(), f"{name}: warmup={warmup} is too long"


@pytest.mark.parametrize(("name", "params"), CASES)
def test_compute_is_deterministic_and_does_not_mutate_input(
    candles: pd.DataFrame, name: str, params: dict[str, float]
) -> None:
    spec = REGISTRY[name]
    snapshot = candles.copy()
    a = spec.compute(candles, params)
    b = spec.compute(candles.copy(), dict(params))
    pd.testing.assert_frame_equal(candles, snapshot)
    assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True)


def test_fixture_has_derivatives_gaps_before_and_after_the_midpoint(candles: pd.DataFrame) -> None:
    drv = candles.filter(like="drv_")
    assert drv.shape[1] == 6
    assert drv.isna().any().all()  # every drv_* column has a gap ...
    # ... before the midpoint, so trailing windows clear of them still exist at the cut points
    assert drv.iloc[N_BARS // 2 :].notna().all().all()


def test_perturbation_also_moves_future_derivatives(candles: pd.DataFrame) -> None:
    t = N_BARS // 2
    shocked = perturb_after(candles, t)
    for col in candles.filter(like="drv_").columns:
        before = candles[col].to_numpy()
        after = shocked[col].to_numpy()
        assert np.array_equal(before[: t + 1], after[: t + 1], equal_nan=True), col
        assert not np.array_equal(before[t + 1 :], after[t + 1 :], equal_nan=True), col


def test_truncated_history_matches_full_history_prefix(candles: pd.DataFrame) -> None:
    """The same property seen the other way: computing on rows <= t only gives the full
    run's first t+1 rows exactly (no global normalisation, no full-sample fit)."""
    t = 350
    for name, spec in REGISTRY.items():
        full = spec.compute(candles, {}).to_numpy()[: t + 1]
        cut = spec.compute(candles.iloc[: t + 1], {}).to_numpy()
        assert np.array_equal(full, cut, equal_nan=True), name
