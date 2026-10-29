"""The report's information-only ``statistics`` block (PLAN.md Phase 11): the nominal N beside
the effective-N diagnostic and its time-shuffled null, MinBTL, MinTRL and the CPCV path
distribution twice (every sweep trial, labelled an upper bound, and TPE's random start-up
trials only); each part ``{"missing": reason}`` when it cannot be computed; the stated "design
window too short" reason on a dsr_min row that does not pass; and MinTRL at the DSR's deflated
benchmark agreeing with the DSR verdict (the same PSR inversion). Offline: seeded synthetic
returns, tmp ledgers."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.plan import plan_to_dict, with_params
from lab.research.criteria import (
    CPCV_UPPER_BOUND_BASIS,
    CriterionRow,
    CriterionStatus,
    evaluate_plan,
    with_length_reason,
)
from lab.research.evidence import record_report
from lab.search.optuna_search import TPE_SAMPLER_SETTINGS
from lab.stats import deflated_sharpe, sharpe_per_bar
from lab.stats.cpcv import cpcv_paths
from lab.stats.sharpe import min_backtest_length, min_track_record_length
from lab.types import bars_per_year
from tests.test_mcp_server import ok, small_cfg
from tests.test_portfolio_pipeline import H1, PLab
from tests.test_research_criteria import (
    LH,
    PLAN,
    W_END,
    W_START,
    WINDOW,
    H,
    backtest_payload,
    rows,
    start,
)
from tests.test_research_criteria import trial as record_trial

N_BARS = int((W_END - W_START).total_seconds() // (4 * 3600))  # 4h bars of the design window
BPY = bars_per_year("4h")
PARTS = ("trial_count", "min_backtest_length", "min_track_record_length", "cpcv")
ALL_PARTS = (*PARTS, "cpcv_startup")


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def _plans(
    ledger: Ledger, run_id: str, cfg: LabConfig, *, base_mu: float, n: int = 10, seed: int = 3
) -> tuple[int, list[np.ndarray]]:
    """The base plan plus ``n - 1`` re-tuned Optuna variants on the full design window
    (commensurable, at least lab.toml min_variance_plans)."""
    rng = np.random.default_rng(seed)
    mus = [base_mu, *np.linspace(-0.0005, 0.0005, n - 1)]
    rets = [rng.normal(mu, 0.01, N_BARS) for mu in mus]
    base = record_trial(ledger, run_id, rets[0], cfg=cfg)
    for k, r in enumerate(rets[1:]):
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 30 + k}))
        record_trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg)
    return base, rets


_OMIT = object()  # a ``numbers`` element: this row records no trial number


def _sweep_trials(
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    study: str,
    n: int,
    seed: int,
    *,
    n_trials: int | None = None,
    errors: frozenset[int] = frozenset(),
    numbers: Sequence[object] | None = None,
) -> np.ndarray:
    """``n`` Optuna rows of ``study`` in trial order; ``n_trials`` goes into the ``_study``
    design (None: omitted, as hand-written rows without it); trial numbers in ``errors`` are
    error rows (they use a number but are not matrix columns); ``numbers[k]`` is row k's
    recorded ``_study.trial`` (None: no ``numbers``, as rows written before it was recorded;
    an element ``_OMIT``: that row alone has none). Returns the ok columns."""
    rng = np.random.default_rng(seed)
    cols = []
    for k in range(n):
        r = rng.normal(0.0002, 0.01, N_BARS)
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 11 + k}))
        meta: dict[str, Any] = {"study": study}
        if n_trials is not None:
            meta["n_trials"] = n_trials
        if numbers is not None and numbers[k] is not _OMIT:
            meta["trial"] = numbers[k]
        params = json.dumps({"ema_fast.period": 11 + k, "_study": meta})
        status = "error" if k in errors else "ok"
        record_trial(
            ledger,
            run_id,
            r,
            plan_dict=variant,
            source="optuna",
            cfg=cfg,
            params_json=params,
            status=status,
        )
        if status == "ok":
            cols.append(r)
    return np.column_stack(cols)


def _sweep(study: str) -> dict[str, Any]:
    return {"study": study, "window": WINDOW, "logic_hash": LH, "space": {}}


# --------------------------------------------------------------------------- presence / values


def test_every_part_is_reported_with_its_numbers(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base, rets = _plans(ledger, run_id, cfg, base_mu=0.0008)
    matrix = _sweep_trials(ledger, run_id, cfg, "S1", 8, seed=21)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    before = ledger.count()
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert ledger.count() == before  # nothing recorded: no new trials, N unchanged
    st = rep["statistics"]
    assert set(PARTS) <= set(st) and not any("missing" in st[p] for p in PARTS)
    assert "information only" in st["note"]
    n = rep["ledger_n"]
    tc = st["trial_count"]
    assert (tc["nominal"], tc["run_nominal"], tc["dsr_n"]) == (n, n, n)
    assert 1.0 <= tc["effective_diagnostic"] <= n and tc["used_for_dsr"] == "nominal"
    assert 1.0 <= tc["effective_null"] <= n and tc["plans"] == n and tc["rows"] > 0
    assert (tc["run_plans"], tc["run_rows"]) == (tc["plans"], tc["rows"])
    assert "NOT used for the DSR" in tc["basis"] and "seed 0" in tc["basis"]

    target = sharpe_per_bar(rets[0]) * math.sqrt(BPY)
    btl = st["min_backtest_length"]
    assert btl["target_sharpe_annual"] == pytest.approx(target)
    assert btl["years"] == pytest.approx(min_backtest_length(n, target))
    assert btl["bars"] == pytest.approx(btl["years"] * BPY)
    assert btl["design_bars"] == N_BARS and btl["design_years"] == pytest.approx(N_BARS / 365 / 6)
    assert btl["n_trials"] == n and btl["ok"] is (btl["design_years"] >= btl["years"])

    sharpes = [sharpe_per_bar(r) for r in [*rets, *matrix.T]]
    dsr = deflated_sharpe(rets[0], n_trials=n, sharpe_variance=float(np.var(sharpes, ddof=1)))
    trl = st["min_track_record_length"]
    assert trl["target_sharpe_per_bar"] == pytest.approx(dsr.sr0_per_bar, rel=1e-12)
    assert trl["confidence"] == 0.95
    assert trl["bars"] == pytest.approx(min_track_record_length(rets[0], dsr.sr0_per_bar, 0.95))
    assert trl["design_bars"] == N_BARS

    want = cpcv_paths(matrix, bars_per_year=BPY)
    cp = st["cpcv"]
    assert cp["n_configs"] == 8 and cp["n_paths"] == want.n_paths
    assert cp["path_sharpes"] == pytest.approx(list(want.path_sharpes))
    assert cp["chosen"] == list(want.chosen)
    assert cp["quantiles"] == pytest.approx(want.quantiles)
    assert "S1" in cp["configs_source"] and "No new backtests" in cp["configs_source"]
    assert cp["basis"].startswith(CPCV_UPPER_BOUND_BASIS)
    # these hand-written rows carry no _study.n_trials: trial numbers are not identifiable
    assert "no _study.n_trials" in st["cpcv_startup"]["missing"]
    json.dumps(st, allow_nan=False)  # plain JSON (no NaN / inf)


def test_no_evidence_leaves_every_part_but_the_trial_count_missing(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert "run_backtest" in st["min_backtest_length"]["missing"]
    assert "run_backtest" in st["min_track_record_length"]["missing"]
    assert "run_sweep" in st["cpcv"]["missing"]
    assert "run_sweep" in st["cpcv_startup"]["missing"]
    assert st["trial_count"]["nominal"] == 0 and st["trial_count"]["dsr_n"] == 1


def test_mintrl_is_missing_when_the_dsr_has_no_deflated_benchmark(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base, _ = _plans(ledger, run_id, cfg, base_mu=0.0008, n=5)  # below min_variance_plans
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert rows(rep)["dsr_min"]["status"] == "missing"
    st = rep["statistics"]
    assert "no deflated benchmark" in st["min_track_record_length"]["missing"]
    assert st["min_backtest_length"]["n_trials"] == 5  # MinBTL needs only N and the Sharpe


def test_mintrl_is_missing_when_dsr_min_is_not_a_confidence(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, dsr_min=0.0)
    base, _ = _plans(ledger, run_id, cfg, base_mu=0.0008)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert "not strictly between 0 and 1" in st["min_track_record_length"]["missing"]


def test_cpcv_is_missing_with_a_sweep_too_small_to_select_from(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    _sweep_trials(ledger, run_id, cfg, "S1", 1, seed=4)
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    cp = evaluate_plan(ledger, run_id, H, cfg)["statistics"]["cpcv"]
    assert "none with at least 2 usable" in cp["missing"]


# --------------------------------------------------------------------------- CPCV start-up

N_STARTUP = int(TPE_SAMPLER_SETTINGS["n_startup_trials"])


def test_cpcv_startup_uses_only_the_random_startup_trials(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """14 trials of one study, trial 3 an error row: trial numbers 0-9 are TPE's random
    start-up draws, so CPCV-startup is over the 9 ok columns among them (the error row still
    uses number 3); ``cpcv`` stays over all 13 ok columns, labelled an upper bound."""
    run_id = start(ledger, tmp_path)
    assert N_STARTUP == 10
    matrix = _sweep_trials(
        ledger, run_id, cfg, "S1", 14, seed=21, n_trials=14, errors=frozenset({3})
    )
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    cp, cs = st["cpcv"], st["cpcv_startup"]
    assert cp["n_configs"] == 13 and cp["basis"] == (
        f"{CPCV_UPPER_BOUND_BASIS} (TPE proposed them from objective values over the whole "
        "design window, so the out-of-sample paths are not out of the search's sample)"
    )
    want = cpcv_paths(matrix[:, :9], bars_per_year=BPY)  # ok columns of trials 0-2, 4-9
    assert cs["n_configs"] == 9 and cs["n_paths"] == want.n_paths
    assert cs["path_sharpes"] == pytest.approx(list(want.path_sharpes))
    assert cs["chosen"] == list(want.chosen)
    assert "random start-up trials only" in cs["basis"]
    assert "9 of the 13 design-window Optuna trials" in cs["configs_source"]
    assert "n_startup_trials = 10" in cs["configs_source"]


def test_cpcv_startup_numbers_restart_when_a_study_name_is_reused(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """The same sweep run twice (same study name, 12 trials each): numbers restart at 0 for
    the second block, so the start-up set is trials 0-9 of EACH block."""
    run_id = start(ledger, tmp_path)
    first = _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=5, n_trials=12)
    second = _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=6, n_trials=12)
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    want = cpcv_paths(np.column_stack([first[:, :10], second[:, :10]]), bars_per_year=BPY)
    assert st["cpcv"]["n_configs"] == 24
    assert st["cpcv_startup"]["n_configs"] == 20
    assert st["cpcv_startup"]["path_sharpes"] == pytest.approx(list(want.path_sharpes))


def test_cpcv_startup_is_missing_for_an_interrupted_study(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    _sweep_trials(ledger, run_id, cfg, "S1", 7, seed=8, n_trials=12)  # 7 of 12 rows
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert "missing" not in st["cpcv"]
    assert "not whole repeats of its n_trials" in st["cpcv_startup"]["missing"]


def test_cpcv_startup_is_missing_with_fewer_than_two_startup_columns(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    errors = frozenset(range(1, 10))  # only trial 0 of the start-up draws is ok
    _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=9, n_trials=12, errors=errors)
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert st["cpcv"]["n_configs"] == 3
    assert "1 usable random start-up trial(s)" in st["cpcv_startup"]["missing"]


def test_cpcv_startup_reads_recorded_trial_numbers_across_interrupted_reruns(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """Two interrupted runs of one 12-trial study (7 rows, then 5) sum to one whole repeat,
    so numbering by row order would call the second run's trials 0-4 numbers 7-11 and drop
    two of its start-up draws. The recorded numbers (0-6, then 0-4) are all below 10."""
    run_id = start(ledger, tmp_path)
    first = _sweep_trials(
        ledger, run_id, cfg, "S1", 7, seed=31, n_trials=12, numbers=list(range(7))
    )
    second = _sweep_trials(
        ledger, run_id, cfg, "S1", 5, seed=32, n_trials=12, numbers=list(range(5))
    )
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    want = cpcv_paths(np.column_stack([first, second]), bars_per_year=BPY)
    cs = st["cpcv_startup"]
    assert cs["n_configs"] == 12
    assert cs["path_sharpes"] == pytest.approx(list(want.path_sharpes))
    assert "the recorded Optuna trial number" in cs["configs_source"]


def test_cpcv_startup_counts_a_reruns_identical_redraws_once(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """A seeded rerun redraws the same start-up configurations (here: identical return
    columns), so CPCV-startup compares each configuration once; ``cpcv`` keeps every row."""
    run_id = start(ledger, tmp_path)
    first = _sweep_trials(
        ledger, run_id, cfg, "S1", 12, seed=36, n_trials=12, numbers=list(range(12))
    )
    _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=36, n_trials=12, numbers=list(range(12)))
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    want = cpcv_paths(first[:, :10], bars_per_year=BPY)
    cs = st["cpcv_startup"]
    assert st["cpcv"]["n_configs"] == 24
    assert cs["n_configs"] == 10
    assert cs["path_sharpes"] == pytest.approx(list(want.path_sharpes))
    assert "10 more with returns identical to one of these" in cs["configs_source"]


def test_cpcv_startup_reads_recorded_numbers_beyond_the_startup_draws(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """Recorded numbers decide, not position: one interrupted run (rows numbered 4-13 of a
    14-trial study, as if 0-3 were never written) has start-up draws 4-9 only."""
    run_id = start(ledger, tmp_path)
    matrix = _sweep_trials(
        ledger, run_id, cfg, "S1", 10, seed=33, n_trials=14, numbers=list(range(4, 14))
    )
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    want = cpcv_paths(matrix[:, :6], bars_per_year=BPY)
    assert st["cpcv_startup"]["n_configs"] == 6
    assert st["cpcv_startup"]["path_sharpes"] == pytest.approx(list(want.path_sharpes))


def test_cpcv_startup_is_missing_when_a_study_mixes_numbered_and_unnumbered_rows(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    numbers: list[object] = [*range(11), _OMIT]
    _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=34, n_trials=12, numbers=numbers)
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert "missing" not in st["cpcv"]
    assert "mixes ledger rows with and without" in st["cpcv_startup"]["missing"]


@pytest.mark.parametrize("bad", [-1, True, 2.0, "3"])
def test_cpcv_startup_is_missing_for_a_malformed_recorded_number(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, bad: object
) -> None:
    run_id = start(ledger, tmp_path)
    numbers: list[object] = [*range(11), bad]
    _sweep_trials(ledger, run_id, cfg, "S1", 12, seed=35, n_trials=12, numbers=numbers)
    record_report(ledger, run_id, H, "sweep", _sweep("S1"))
    st = evaluate_plan(ledger, run_id, H, cfg)["statistics"]
    assert "not a non-negative integer" in st["cpcv_startup"]["missing"]


def test_minbtl_is_unbounded_for_a_non_positive_sharpe(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base, _ = _plans(ledger, run_id, cfg, base_mu=-0.0008)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    rep = evaluate_plan(ledger, run_id, H, cfg)
    btl = rep["statistics"]["min_backtest_length"]
    assert btl["years"] is None and btl["ok"] is False and "infinite" in btl["basis"]
    trl = rep["statistics"]["min_track_record_length"]
    assert trl["bars"] is None and trl["ok"] is False
    # an infinite length is a Sharpe problem, not a window-length one: no length reason
    assert "too short" not in rows(rep)["dsr_min"]["note"]


# --------------------------------------------------------------------------- consistency


@pytest.mark.parametrize("seed", range(12))
def test_mintrl_at_the_deflated_benchmark_agrees_with_the_dsr_verdict(seed: int) -> None:
    """DSR = PSR(SR0) over n bars, and MinTRL inverts the same PSR at SR0: DSR >= c exactly
    when n >= MinTRL(SR0, c), for any series, N, V[SR] and confidence (c > 0.5)."""
    rng = np.random.default_rng(seed)
    n_bars = int(rng.integers(200, 3000))
    returns = rng.standard_t(5, n_bars) * 0.01 + rng.uniform(-0.0002, 0.0012)
    n_trials = int(rng.integers(1, 3000))
    variance = float(rng.uniform(1e-6, 4e-4))
    conf = float(rng.uniform(0.6, 0.99))
    res = deflated_sharpe(returns, n_trials=n_trials, sharpe_variance=variance)
    bars = min_track_record_length(returns, res.sr0_per_bar, conf)
    assert (res.dsr >= conf) == (n_bars >= bars)


def test_a_dsr_pass_reports_enough_bars_and_a_fail_reports_too_few(
    tmp_path: Path, cfg: LabConfig
) -> None:
    for mu, verdict in ((0.0012, "pass"), (0.0002, "fail")):
        with Ledger(tmp_path / f"{verdict}.db") as led:
            run_id = start(led, tmp_path)
            base, _ = _plans(led, run_id, cfg, base_mu=mu)
            record_report(led, run_id, H, "backtest", backtest_payload(base))
            rep = evaluate_plan(led, run_id, H, cfg)
        row = rows(rep)["dsr_min"]
        trl = rep["statistics"]["min_track_record_length"]
        assert row["status"] == verdict
        assert trl["ok"] is (verdict == "pass")


# --------------------------------------------------------------------------- stated reason


def _correlated_plans(ledger: Ledger, run_id: str, cfg: LabConfig) -> int:
    """Ten plans on one market factor (V[SR] small, so SR0 sits just under the base plan's
    Sharpe and MinTRL is finite but long) with a modest Sharpe over 1.25 years."""
    rng = np.random.default_rng(5)
    factor = rng.normal(0.0, 0.01, N_BARS)
    mus = [0.0, *np.linspace(-0.0001, 0.0001, 9)]
    rets = [factor + mu + rng.normal(0.0, 0.002, N_BARS) for mu in mus]
    base = record_trial(ledger, run_id, rets[0], cfg=cfg)
    for k, r in enumerate(rets[1:]):
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 30 + k}))
        record_trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg)
    return base


def test_a_failing_dsr_states_the_too_short_design_window(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base = _correlated_plans(ledger, run_id, cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    rep = evaluate_plan(ledger, run_id, H, cfg)
    row = rows(rep)["dsr_min"]
    st = rep["statistics"]
    trl, btl = st["min_track_record_length"], st["min_backtest_length"]
    assert row["status"] == "fail"
    assert trl["bars"] is not None and btl["years"] is not None  # both finite, both binding
    assert trl["ok"] is False and btl["ok"] is False
    assert "design window too short: " in row["note"]
    assert f"{N_BARS} bars < MinTRL {trl['bars']:.0f} bars at the deflated benchmark" in row["note"]
    assert (
        f"{btl['design_years']:.2f} years < MinBTL {btl['years']:.2f} years for 10 trials"
        in row["note"]
    )


def test_a_passing_dsr_carries_no_length_reason(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base, _ = _plans(ledger, run_id, cfg, base_mu=0.0012)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]
    assert row["status"] == "pass" and "too short" not in row["note"]


def _row(status: CriterionStatus) -> CriterionRow:
    return CriterionRow(criterion="dsr_min", status=status, value=0.5, threshold=0.95, note="x")


SHORT = {
    "min_track_record_length": {"bars": 4000.0, "design_bars": 2742, "ok": False},
    "min_backtest_length": {
        "years": 3.0,
        "design_years": 1.25,
        "n_trials": 2479,
        "ok": False,
    },
}


@pytest.mark.parametrize("status", ["pass", "fail", "missing"])
def test_the_length_reason_never_changes_a_status(status: CriterionStatus) -> None:
    out = with_length_reason(_row(status), SHORT)
    assert out["status"] == status and out["value"] == 0.5
    if status == "pass":
        assert out["note"] == "x"
    else:
        assert out["note"] == (
            "x; design window too short: 2742 bars < MinTRL 4000 bars at the deflated "
            "benchmark; 1.25 years < MinBTL 3.00 years for 2479 trials"
        )


def test_the_length_reason_names_only_the_binding_length() -> None:
    only_btl = {**SHORT, "min_track_record_length": {"missing": "no SR0"}}
    note = with_length_reason(_row("fail"), only_btl)["note"]
    assert "MinTRL" not in note and "MinBTL 3.00 years" in note
    neither = {"min_track_record_length": {"missing": "a"}, "min_backtest_length": {"missing": "b"}}
    assert with_length_reason(_row("fail"), neither)["note"] == "x"


# --------------------------------------------------------------------------- portfolio path


def test_portfolio_reports_carry_the_same_statistics_block(tmp_path: Path) -> None:
    lab = PLab(tmp_path)
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    st = rep["statistics"]
    assert set(ALL_PARTS) <= set(st)
    assert st["trial_count"]["dsr_n"] == rep["ledger_n"]
    assert st["min_backtest_length"]["n_trials"] == rep["ledger_n"]
    assert "run_sweep" in st["cpcv"]["missing"]
    assert "run_sweep" in st["cpcv_startup"]["missing"]
    assert st["trial_count"]["used_for_dsr"] == "nominal"
