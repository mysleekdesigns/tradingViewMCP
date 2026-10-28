"""Optuna search + walk-forward optimisation: ledger completeness, determinism, holdout guard,
space validation, min-trades filter, study metadata, cost history, fold-boundary exit charge,
and the tuning perturbation test (test prices never influence selection)."""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import optuna
import pandas as pd
import pytest

from lab.config import LabConfig, load_config
from lab.engine import run_signals, slippage_series
from lab.engine.splits import (
    design_view,
    design_window,
    history_through,
    slice_window,
    walk_forward_folds,
)
from lab.ledger import Ledger, costs_to_json
from lab.plan import compile_plan, parse_plan, with_params
from lab.search import (
    FAILED_OBJECTIVE,
    MIN_TRADES,
    STUDY_KEY,
    evaluate_on_window,
    optuna_search,
    resolve_space,
    walk_forward_optimize,
)
from lab.types import CostModel, Plan, Window
from tests.synthetic import make_candles, perturb_after

# The package re-exports the function `optuna_search`, so fetch the module itself.
search_mod = importlib.import_module("lab.search.optuna_search")

HOLDOUT = pd.Timestamp("2022-09-01", tz="UTC")
SPACE = {"fast.period": (3.0, 15.0), "slow.period": (20.0, 60.0)}
# Low enough that most synthetic trials on the 2-month train windows stay selectable, so the
# behaviour under test is not masked by the min-trades filter (tested on its own below).
TEST_MIN_TRADES = 5


def lab_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    splits = replace(
        base.splits,
        design_start=pd.Timestamp("2022-01-01", tz="UTC"),
        holdout_start=HOLDOUT,
        wf_train_months=2,
        wf_test_months=1,
        wf_step_months=1,
    )
    return replace(
        base, data_dir=tmp_path, splits=splits, costs=CostModel(0.001, 0.0005, 0.0), init_cash=1e4
    )


def ema_plan(entry: str = "crosses_above(fast, slow)", exit_: str = "crosses_below(fast, slow)"):
    return parse_plan(
        {
            "name": "ema_cross",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"fast": {"ema": {"period": 10}}, "slow": {"ema": {"period": 30}}},
            "long_entry": entry,
            "long_exit": exit_,
            "risk": {"position_size_pct": 100},
        }
    )


@pytest.fixture
def candles() -> pd.DataFrame:
    # 2022-01-01 .. ~2022-10-12 on 4h bars; the tail after HOLDOUT is the "locked" part.
    return make_candles(1700, seed=11, vol=0.012, drift=0.0002)


def design_candles(candles: pd.DataFrame, cfg: LabConfig) -> pd.DataFrame:
    return design_view(candles, cfg.splits)


def run_search(
    plan: Plan,
    candles: pd.DataFrame,
    cfg: LabConfig,
    ledger: Ledger,
    *,
    n: int = 6,
    seed: int = 3,
    min_trades: int = TEST_MIN_TRADES,
):
    return optuna_search(
        plan,
        design_candles(candles, cfg),
        design_window(cfg.splits),
        ledger=ledger,
        run_id="r1",
        cfg=cfg,
        n_trials=n,
        seed=seed,
        space=SPACE,
        min_trades=min_trades,
    )


def tuned_params(params_json: str) -> dict:
    """``params_json`` without the reserved study-metadata key."""
    return {k: v for k, v in json.loads(params_json).items() if k != STUDY_KEY}


def test_every_trial_lands_in_the_ledger(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        res = run_search(ema_plan(), candles, cfg, ledger, n=6)
        assert res.n_trials == 6 and len(res.trial_ids) == 6
        assert ledger.count("r1") == 6
        rows = [ledger.get(i, include_returns=True) for i in res.trial_ids]
    design = design_window(cfg.splits)
    n_bars = len(slice_window(candles, design))
    for row in rows:
        assert row.source == "optuna" and row.window_kind == "design"
        assert row.window_start == design.start and row.window_end == design.end
        params = json.loads(row.params_json)
        assert set(params) == set(SPACE) | {STUDY_KEY}
        if row.status == "ok":
            assert row.returns is not None and len(row.returns) == n_bars  # stored for PBO
            assert json.loads(row.metrics_json)["bars"] == n_bars
    plan_hashes = {r.plan_hash for r in rows}
    assert len(plan_hashes) >= 2  # hash of the concrete (tuned) plan, not the template


def test_failed_trials_are_recorded_as_errors(
    tmp_path: Path, candles: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = search_mod.run_signals
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("engine blew up")
        return real(*args, **kwargs)

    monkeypatch.setattr(search_mod, "run_signals", flaky)
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        res = run_search(ema_plan(), candles, cfg, ledger, n=6)
        assert ledger.count("r1") == 6
        assert ledger.count("r1", status="error") == 3
        errors = [r for r in (ledger.get(i) for i in res.trial_ids) if r.status == "error"]
    assert res.n_failed >= 3
    for row in errors:
        assert row.error == "RuntimeError: engine blew up"
        assert set(tuned_params(row.params_json)) == set(SPACE)
        assert row.metrics_json == "{}" and row.sharpe_per_bar is None


def test_undefined_sharpe_counts_as_failed_but_is_an_ok_backtest(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    cfg = lab_cfg(tmp_path)
    never = ema_plan(entry="fast < 0 and slow < 0", exit_="fast < 0")  # never trades
    with Ledger(tmp_path / "l.db") as ledger:
        res = run_search(never, candles, cfg, ledger, n=4)
        assert ledger.count("r1", status="ok") == 4
    assert res.n_failed == 4
    assert res.best_params is None and res.best_plan is None and np.isnan(res.best_value)


def test_same_seed_gives_same_best_params(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        ra = run_search(ema_plan(), candles, cfg, a, n=8, seed=5)
        rb = run_search(ema_plan(), candles, cfg, b, n=8, seed=5)
        pa = [a.get(i).params_json for i in ra.trial_ids]
        pb = [b.get(i).params_json for i in rb.trial_ids]
    assert pa == pb
    assert ra.best_params == rb.best_params and ra.best_value == rb.best_value
    assert ra.best_plan == rb.best_plan
    assert ra.best_plan is not None and ra.best_params is not None
    assert ra.best_plan.features[0].params["period"] == ra.best_params["fast.period"]


def test_holdout_rows_are_refused(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(ValueError, match="locked holdout"):
            optuna_search(
                ema_plan(),
                candles,  # reaches past HOLDOUT
                design_window(cfg.splits),
                ledger=ledger,
                run_id="r1",
                cfg=cfg,
                n_trials=2,
                seed=0,
                space=SPACE,
            )
        folds = walk_forward_folds(
            design_window(cfg.splits), train_months=2, test_months=1, step_months=1, anchored=False
        )
        with pytest.raises(ValueError, match="locked holdout"):
            walk_forward_optimize(
                ema_plan(),
                candles,
                folds,
                ledger=ledger,
                run_id="r1",
                cfg=cfg,
                n_trials_per_fold=2,
                seed=0,
                space=SPACE,
            )
        late = Window(HOLDOUT - pd.Timedelta(days=30), HOLDOUT + pd.Timedelta(days=1), "design")
        with pytest.raises(ValueError, match="after holdout_start"):
            optuna_search(
                ema_plan(),
                design_candles(candles, cfg),
                late,
                ledger=ledger,
                run_id="r1",
                cfg=cfg,
                n_trials=2,
                seed=0,
                space=SPACE,
            )
        assert ledger.count("r1") == 0


@pytest.mark.parametrize(
    ("space", "message"),
    [
        ({"fast.length": (3, 10)}, "'fast.length' is not a tunable param"),
        ({"fast.period": (0, 10)}, r"'fast.period'.*outside the param's bounds"),
        ({"slow.period": (5, 2000)}, r"'slow.period'.*outside the param's bounds"),
        ({"fast.period": (12, 5)}, r"'fast.period'.*low 12 must be <= high 5"),
        ({"fast.period": (2.5, 9)}, r"'fast.period'.*whole numbers"),
        ({"risk.stop_loss_pct": (1, 5)}, "'risk.stop_loss_pct' is not a tunable param"),
        ({}, "non-empty mapping"),
    ],
)
def test_space_validation_names_the_bad_key(space: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_space(ema_plan(), space)


def test_default_space_is_every_tunable_with_its_bounds() -> None:
    space = resolve_space(ema_plan())
    assert set(space) == {"fast.period", "slow.period"}
    assert space["fast.period"].kind == "int" and space["fast.period"].low == 1


def wf_run(
    candles: pd.DataFrame,
    cfg: LabConfig,
    ledger: Ledger,
    n: int = 4,
    min_trades: int = TEST_MIN_TRADES,
):
    folds = walk_forward_folds(
        design_window(cfg.splits), train_months=2, test_months=1, step_months=1, anchored=False
    )
    return folds, walk_forward_optimize(
        ema_plan(),
        design_candles(candles, cfg),
        folds,
        ledger=ledger,
        run_id="wf",
        cfg=cfg,
        n_trials_per_fold=n,
        seed=1,
        space=SPACE,
        min_trades=min_trades,
    )


def test_walk_forward_records_train_and_test_trials(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        folds, wf = wf_run(candles, cfg, ledger, n=4)
        rows = ledger.list(run_id="wf", limit=1000)
    assert len(folds) == 6  # Jan..Aug design: tests Mar, Apr, ..., Aug
    kinds = [r.window_kind for r in rows]
    assert kinds.count("walk_forward_train") == 4 * len(folds) == wf.n_train_trials
    assert kinds.count("walk_forward_test") == len(folds)
    assert all(r.source == "optuna" for r in rows)
    tests = {r.id: r for r in rows if r.window_kind == "walk_forward_test"}
    for f in wf.folds:
        row = tests[f.test_trial_id]
        assert row.window_start == f.test.start and row.window_end == f.test.end
        assert tuned_params(row.params_json) == f.best_params
        assert json.loads(row.params_json)[STUDY_KEY]["study"] == f.search.study_name
        # train trials were recorded on the TRAIN window only
        for tid in f.search.trial_ids:
            train_row = next(r for r in rows if r.id == tid)
            assert (train_row.window_start, train_row.window_end) == (f.train.start, f.train.end)


def test_stitched_oos_returns_cover_exactly_the_test_windows(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        folds, wf = wf_run(candles, cfg, ledger, n=3)
    expected = pd.DatetimeIndex(
        np.concatenate([slice_window(candles, f.test).index.to_numpy() for f in folds])
    )
    assert wf.oos_returns.index.equals(expected)
    assert wf.oos_returns.index.is_unique and wf.oos_returns.index.is_monotonic_increasing
    assert wf.oos_metrics.bars == len(expected)
    assert len(wf.boundary_charges) == len(wf.folds) and wf.boundary_charges[-1] == 0.0
    for f, charge in zip(wf.folds, wf.boundary_charges, strict=True):
        part = wf.oos_returns.loc[slice_window(candles, f.test).index]
        assert part.iloc[0] == 0.0  # each test starts flat
        # The fold's own backtest compounded, less the exit charge at its boundary (if any).
        expected_growth = (1 + f.test_metrics.total_return) * (1 - charge)
        assert np.prod(1 + part.to_numpy()) == pytest.approx(expected_growth, rel=1e-12)


def test_test_window_prices_never_change_the_chosen_params(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    """Perturbation test for tuning: change every price from fold 0's TEST window onwards;
    fold 0's train trials and its chosen params must be bit-identical."""
    cfg = lab_cfg(tmp_path)
    folds = walk_forward_folds(
        design_window(cfg.splits), train_months=2, test_months=1, step_months=1, anchored=False
    )
    last_train_bar = int((candles.index < folds[0].test.start).sum()) - 1
    perturbed = perturb_after(candles, last_train_bar, seed=123)
    assert not np.allclose(
        slice_window(perturbed, folds[0].test)["close"],
        slice_window(candles, folds[0].test)["close"],
    )
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        _, base = wf_run(candles, cfg, a, n=5)
        _, pert = wf_run(perturbed, cfg, b, n=5)
        base_sharpes = [a.get(i).sharpe_per_bar for i in base.folds[0].search.trial_ids]
        pert_sharpes = [b.get(i).sharpe_per_bar for i in pert.folds[0].search.trial_ids]
    assert base.folds[0].selected  # a real selection happened, so the test is not vacuous
    assert base.folds[0].best_params == pert.folds[0].best_params
    assert base.folds[0].train_sharpe == pert.folds[0].train_sharpe
    assert base_sharpes == pert_sharpes
    # ...while the perturbed test window really does change the out-of-sample result.
    assert base.folds[0].test_metrics.total_return != pert.folds[0].test_metrics.total_return


def test_overlapping_test_windows_are_rejected(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    folds = walk_forward_folds(
        design_window(cfg.splits), train_months=2, test_months=2, step_months=1, anchored=False
    )
    with Ledger(tmp_path / "l.db") as ledger, pytest.raises(ValueError, match="overlaps"):
        walk_forward_optimize(
            ema_plan(),
            design_candles(candles, cfg),
            folds,
            ledger=ledger,
            run_id="wf",
            cfg=cfg,
            n_trials_per_fold=2,
            seed=0,
            space=SPACE,
        )


# --------------------------------------------------------------------------- min trades


def test_trials_below_min_trades_are_recorded_but_failed(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        loose = run_search(ema_plan(), candles, cfg, a, n=8, seed=5, min_trades=0)
        strict = run_search(ema_plan(), candles, cfg, b, n=8, seed=5, min_trades=10**6)
        loose_rows = [a.get(i) for i in loose.trial_ids]
        strict_rows = [b.get(i) for i in strict.trial_ids]
    # Same seed, same trials, same real metrics in the ledger; only the objective differs.
    assert [tuned_params(r.params_json) for r in strict_rows] == [
        tuned_params(r.params_json) for r in loose_rows
    ]
    assert [r.metrics_json for r in strict_rows] == [r.metrics_json for r in loose_rows]
    assert all(r.status == "ok" and r.n_trades > 0 for r in strict_rows)
    assert loose.n_failed == 0 and loose.best_params is not None
    assert strict.n_failed == strict.n_trials == 8
    assert strict.best_params is None and strict.best_plan is None
    assert np.isnan(strict.best_value)


def test_min_trades_threshold_is_inclusive_and_drives_selection(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "a.db") as a:
        base = run_search(ema_plan(), candles, cfg, a, n=8, seed=5, min_trades=0)
        rows = [a.get(i) for i in base.trial_ids]
    trades = [r.n_trades for r in rows]
    params = [tuned_params(r.params_json) for r in rows]
    best_trades = trades[params.index(base.best_params)]
    # Exactly at the threshold the best trial is still selectable...
    with Ledger(tmp_path / "b.db") as b:
        at = run_search(ema_plan(), candles, cfg, b, n=8, seed=5, min_trades=best_trades)
    assert at.best_params == base.best_params
    # ...one trade more and it is failed; the new best has enough trades.
    cut = best_trades + 1
    with Ledger(tmp_path / "c.db") as c:
        res = run_search(ema_plan(), candles, cfg, c, n=8, seed=5, min_trades=cut)
    assert res.n_failed == sum(1 for t in trades if t < cut) >= 1
    assert res.best_params != base.best_params
    if res.best_params is not None:
        assert trades[params.index(res.best_params)] >= cut


def test_optuna_sees_the_failure_sentinel_for_too_few_trades(
    tmp_path: Path, candles: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    real_create = search_mod.optuna.create_study

    def spy_create(*args, **kwargs):
        study = real_create(*args, **kwargs)
        real_optimize = study.optimize

        def optimize(func, *a, **k):
            def wrapped(trial):
                value = func(trial)
                seen.append(value)
                return value

            return real_optimize(wrapped, *a, **k)

        study.optimize = optimize
        return study

    monkeypatch.setattr(search_mod.optuna, "create_study", spy_create)
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        run_search(ema_plan(), candles, cfg, ledger, n=4, min_trades=10**6)
    assert seen == [FAILED_OBJECTIVE] * 4


def test_default_min_trades_is_the_module_constant() -> None:
    for fn in (optuna_search, walk_forward_optimize):
        assert inspect.signature(fn).parameters["min_trades"].default == MIN_TRADES == 30


@pytest.mark.parametrize("bad", [-1, 2.5, True, "30"])
def test_bad_min_trades_is_rejected(tmp_path: Path, candles: pd.DataFrame, bad: object) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(ValueError, match="min_trades"):
            run_search(ema_plan(), candles, cfg, ledger, n=2, min_trades=bad)  # type: ignore[arg-type]
        assert ledger.count("r1") == 0


# --------------------------------------------------------------------------- study metadata


def test_every_row_records_the_study_design(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        res = run_search(ema_plan(), candles, cfg, ledger, n=5, seed=9, min_trades=7)
        rows = [ledger.get(i) for i in res.trial_ids]
        n_plans = ledger.count_distinct_plans("r1")
    for row in rows:
        meta = json.loads(row.params_json)[STUDY_KEY]
        assert meta == {
            "study": res.study_name,
            "space": {"fast.period": [3.0, 15.0], "slow.period": [20.0, 60.0]},
            "space_explicit": True,
            "seed": 9,
            "n_trials": 5,
            "min_trades": 7,
        }
    # The metadata lives in params_json, so it adds no pseudo-plan to the DSR's N.
    assert n_plans == len({r.plan_hash for r in rows})


def test_default_space_is_recorded_as_not_explicit(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        res = optuna_search(
            ema_plan(),
            design_candles(candles, cfg),
            design_window(cfg.splits),
            ledger=ledger,
            run_id="r1",
            cfg=cfg,
            n_trials=2,
            seed=0,
            min_trades=0,
        )
        meta = json.loads(ledger.get(res.trial_ids[0]).params_json)[STUDY_KEY]
    bounds = resolve_space(ema_plan())
    assert meta["space_explicit"] is False
    assert meta["space"] == {k: [s.low, s.high] for k, s in bounds.items()}


def test_require_space_refuses_a_missing_space(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = lab_cfg(tmp_path)
    folds = walk_forward_folds(
        design_window(cfg.splits), train_months=2, test_months=1, step_months=1, anchored=False
    )
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(ValueError, match="space is required"):
            optuna_search(
                ema_plan(),
                design_candles(candles, cfg),
                design_window(cfg.splits),
                ledger=ledger,
                run_id="r1",
                cfg=cfg,
                n_trials=2,
                seed=0,
                require_space=True,
            )
        with pytest.raises(ValueError, match="space is required"):
            walk_forward_optimize(
                ema_plan(),
                design_candles(candles, cfg),
                folds,
                ledger=ledger,
                run_id="r1",
                cfg=cfg,
                n_trials_per_fold=2,
                seed=0,
                require_space=True,
            )
        assert ledger.count("r1") == 0
        res = optuna_search(
            ema_plan(),
            design_candles(candles, cfg),
            design_window(cfg.splits),
            ledger=ledger,
            run_id="r1",
            cfg=cfg,
            n_trials=2,
            seed=0,
            space=SPACE,
            require_space=True,
            min_trades=0,
        )
    assert res.n_trials == 2


# --------------------------------------------------------------------------- costs


def fast_plan() -> Plan:
    """EMA 3/8 crossover: trades every few bars, so fills land inside the slippage warm-up."""
    return parse_plan(
        {
            "name": "ema_fast",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"fast": {"ema": {"period": 3}}, "slow": {"ema": {"period": 8}}},
            "long_entry": "crosses_above(fast, slow)",
            "long_exit": "crosses_below(fast, slow)",
            "risk": {"position_size_pct": 100},
        }
    )


def vol_cfg(tmp_path: Path) -> LabConfig:
    base = lab_cfg(tmp_path)
    return replace(base, costs=CostModel(0.001, 0.0005, 0.5, 0.01))


def test_window_runs_warm_up_vol_slippage_on_earlier_history(candles: pd.DataFrame) -> None:
    cfg = vol_cfg(Path("."))
    plan = fast_plan()
    window = Window(candles.index[400], candles.index[700], "custom")
    res = evaluate_on_window(plan, candles, window, cfg)
    history = history_through(candles, window)
    bars = slice_window(history, window)
    signals = compile_plan(plan, history)
    warmed = run_signals(
        bars,
        signals.entries.loc[bars.index],
        signals.exits.loc[bars.index],
        costs=cfg.costs,
        risk=plan.risk,
        init_cash=cfg.init_cash,
        cost_history=history,
    )
    cold = run_signals(
        bars,
        signals.entries.loc[bars.index],
        signals.exits.loc[bars.index],
        costs=cfg.costs,
        risk=plan.risk,
        init_cash=cfg.init_cash,
    )
    assert sum(t.entry_time < bars.index[100] for t in res.trades) >= 5  # fills in the warm-up
    pd.testing.assert_series_equal(res.equity, warmed.equity)
    # The warm-up matters here: the first 100 window bars get the vol-scaled rate.
    assert not np.array_equal(
        slippage_series(history, cfg.costs)[-len(bars) :][:100],
        slippage_series(bars, cfg.costs)[:100],
    )
    assert not res.equity.equals(cold.equity)


def test_every_row_records_the_cost_model(tmp_path: Path, candles: pd.DataFrame) -> None:
    cfg = vol_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        wf_run(candles, cfg, ledger, n=2)
        rows = ledger.list(run_id="wf", limit=1000)
    assert rows and all(r.costs_json == costs_to_json(cfg.costs) for r in rows)
    assert {r.window_kind for r in rows} == {"walk_forward_train", "walk_forward_test"}


def test_error_rows_record_the_cost_model(
    tmp_path: Path, candles: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("engine blew up")

    monkeypatch.setattr(search_mod, "run_signals", boom)
    cfg = lab_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        res = run_search(ema_plan(), candles, cfg, ledger, n=2)
        rows = [ledger.get(i) for i in res.trial_ids]
    assert all(r.status == "error" and r.costs_json == costs_to_json(cfg.costs) for r in rows)


# --------------------------------------------------------------------------- fold boundaries


def test_open_position_at_a_fold_boundary_is_charged_its_exit(
    tmp_path: Path, candles: pd.DataFrame
) -> None:
    # "Always long" enters on the first bar of every test and is still open at its end.
    always = ema_plan(entry="fast > 0 and slow > 0", exit_="fast < 0 and slow < 0")
    cfg = lab_cfg(tmp_path)
    folds = walk_forward_folds(
        design_window(cfg.splits), train_months=2, test_months=1, step_months=1, anchored=False
    )
    dc = design_candles(candles, cfg)
    with Ledger(tmp_path / "l.db") as ledger:
        wf = walk_forward_optimize(
            always,
            dc,
            folds,
            ledger=ledger,
            run_id="wf",
            cfg=cfg,
            n_trials_per_fold=2,
            seed=0,
            space=SPACE,
            min_trades=0,
        )
    f_rate = cfg.costs.fee_rate
    for k, (fold, charge) in enumerate(zip(wf.folds, wf.boundary_charges, strict=True)):
        result = evaluate_on_window(with_fold_params(always, fold.best_params), dc, fold.test, cfg)
        if k == len(wf.folds) - 1:
            assert charge == 0.0  # the final fold's open trade is an ordinary "end" trade
            continue
        w = float(result.position.iloc[-1])
        s = float(slippage_series(history_through(dc, fold.test), cfg.costs)[-1])
        assert w > 0.9  # fully invested at the boundary
        assert charge == pytest.approx(w * (1 - (1 - s) * (1 - f_rate)), rel=1e-12)
        # 100% sizing: ~ fee + slippage = 0.15% of equity
        assert charge == pytest.approx(0.0015, abs=2e-5)
        last = wf.oos_returns.loc[slice_window(dc, fold.test).index[-1]]
        assert last == pytest.approx(
            (1 + float(result.returns.iloc[-1])) * (1 - charge) - 1, rel=1e-12
        )


def with_fold_params(plan: Plan, params: dict) -> Plan:
    return with_params(plan, params)


# --------------------------------------------------------------------------- explicit TPE sampler


def _seeded_proposals(sampler: optuna.samplers.BaseSampler, n: int = 25) -> list[dict]:
    """Params proposed for a fixed single-objective study (int + float space, deterministic
    objective), so two samplers are compared on exactly what they suggest."""
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(
        lambda t: (
            -((t.suggest_float("x", 0.0, 10.0) - 3.0) ** 2)
            - (t.suggest_int("n", 1, 50) - 20) ** 2 / 100.0
            + t.suggest_float("y", -1.0, 1.0)
        ),
        n_trials=n,
    )
    return [t.params for t in study.trials]


@pytest.mark.parametrize("seed", [0, 3, 42])
def test_explicit_tpe_settings_propose_what_the_default_sampler_did(seed: int) -> None:
    # 25 trials = 10 random start-up trials + 15 TPE proposals, so the model-based part is
    # compared too; equality must be exact (same seed, same algorithm).
    explicit = _seeded_proposals(search_mod.make_tpe_sampler(seed))
    default = _seeded_proposals(optuna.samplers.TPESampler(seed=seed))
    assert explicit == default


def test_the_sampler_settings_comparison_can_fail() -> None:
    # guard against a vacuous equality above: flipping one recorded setting changes proposals
    changed = optuna.samplers.TPESampler(
        seed=3, **{**search_mod.TPE_SAMPLER_SETTINGS, "multivariate": False}
    )
    assert _seeded_proposals(changed) != _seeded_proposals(search_mod.make_tpe_sampler(3))


def test_make_tpe_sampler_states_every_recorded_setting() -> None:
    sampler = search_mod.make_tpe_sampler(7)
    settings = search_mod.TPE_SAMPLER_SETTINGS
    assert sampler._multivariate is settings["multivariate"] is True
    assert sampler._constant_liar is settings["constant_liar"] is True
    assert sampler._n_startup_trials == settings["n_startup_trials"] == 10
    assert sampler._n_ei_candidates == settings["n_ei_candidates"] == 24
    assert sampler._group is settings["group"] is False


def test_optuna_search_builds_its_study_with_make_tpe_sampler(
    tmp_path: Path, candles: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds: list[int | None] = []
    real = search_mod.make_tpe_sampler

    def spy(seed: int | None) -> optuna.samplers.TPESampler:
        seeds.append(seed)
        return real(seed)

    monkeypatch.setattr(search_mod, "make_tpe_sampler", spy)
    with Ledger(tmp_path / "l.db") as led:
        run_search(ema_plan(), candles, lab_cfg(tmp_path), led, n=2, seed=9)
    assert seeds == [9]


def test_no_study_builds_a_tpe_sampler_with_implicit_defaults() -> None:
    # the portfolio search is built the same way; only make_tpe_sampler constructs a TPESampler
    portfolio_runner = importlib.import_module("lab.portfolio_runner")
    assert portfolio_runner.make_tpe_sampler is search_mod.make_tpe_sampler
    assert "samplers.TPESampler(" not in inspect.getsource(portfolio_runner)
    assert "sampler=make_tpe_sampler(seed)" in inspect.getsource(portfolio_runner)
    source = inspect.getsource(search_mod)
    assert source.count("optuna.samplers.TPESampler(") == 1  # inside make_tpe_sampler only
    assert "sampler=make_tpe_sampler(seed)" in source
