"""``[criteria] dsr_trial_count`` (PLAN.md Phase 11): parsed from the success-criteria file
(absent -> "nominal"), frozen into the ledger at start_run, read back by ``frozen_criteria``
(an old snapshot without the key stays on nominal). Only "nominal" is accepted: "effective"
(the participation-ratio N) is refused by the loader and a forged frozen "effective" snapshot
fails closed (Phase 11 review C1: as the DSR's N it double-counts the shared market factor
and falls as trials are added; ``tests/test_stats_effective_n.py`` pins both facts). The
``dsr_min`` row always deflates with the nominal N, and the effective-N diagnostic in the
statistics block can never break a report.

Offline: synthetic seeded returns and candles, tmp ledgers."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.plan import plan_to_dict, with_params
from lab.research import criteria as criteria_mod
from lab.research.contract import (
    DSR_EFFECTIVE_REFUSED,
    DSR_TRIAL_COUNTS,
    ResearchConfigError,
    SuccessCriteria,
    load_research_config,
)
from lab.research.criteria import CRITERION_PARAMS, evaluate_plan, frozen_criteria
from lab.research.evidence import record_report
from lab.research.runs import lab_fingerprint, new_run_id
from lab.research.trial_count import trial_counts
from lab.stats import deflated_sharpe, sharpe_per_bar
from tests.test_mcp_server import (
    EMA_PLAN,
    PROPOSED_CRITERIA,
    Lab,
    error_text,
    ok,
    small_cfg,
    write_criteria,
)
from tests.test_research_criteria import PLAN, W_END, W_START, H, backtest_payload, rows, start
from tests.test_research_criteria import trial as record_trial

# 4h bars filling [W_START, W_END) exactly, so the effective count can place them on its daily
# grid without candles (lab.research.trial_count: contiguous series).
N_BARS = int((W_END - W_START).total_seconds() // (4 * 3600))


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


def _text_without_key() -> str:
    lines = PROPOSED_CRITERIA.read_text().splitlines(keepends=True)
    kept = [ln for ln in lines if not ln.startswith("dsr_trial_count")]
    assert len(kept) == len(lines) - 1  # the proposed file carries the key exactly once
    return "".join(kept)


# --------------------------------------------------------------------------- contract


def test_proposed_file_states_nominal() -> None:
    assert load_research_config(PROPOSED_CRITERIA).criteria.dsr_trial_count == "nominal"


def test_absent_key_reads_nominal(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(_text_without_key())
    assert load_research_config(path).criteria.dsr_trial_count == "nominal"


def test_effective_is_refused_with_the_reason(tmp_path: Path) -> None:
    path = write_criteria(tmp_path / "c.toml", dsr_trial_count="effective")
    with pytest.raises(ResearchConfigError) as err:
        load_research_config(path)
    msg = str(err.value)
    assert msg.startswith('[criteria] dsr_trial_count = "effective" is not accepted: ')
    assert "diagnostic only" in msg and "double-counts the shared market factor" in msg
    assert "falls as trials are added" in msg and msg.endswith('use "nominal"')
    assert msg == f"[criteria] {DSR_EFFECTIVE_REFUSED}"


def test_explicit_nominal_is_accepted_and_serialised(tmp_path: Path) -> None:
    research = load_research_config(write_criteria(tmp_path / "c.toml", dsr_trial_count="nominal"))
    assert research.criteria.dsr_trial_count == "nominal"
    assert json.loads(research.to_json())["criteria"]["dsr_trial_count"] == "nominal"


@pytest.mark.parametrize("value", ["bogus", "Nominal", "", 1, True])
def test_any_other_value_is_rejected(tmp_path: Path, value: object) -> None:
    path = write_criteria(tmp_path / "c.toml", dsr_trial_count=value)
    with pytest.raises(ResearchConfigError, match=r"dsr_trial_count"):
        load_research_config(path)


def test_the_error_names_the_allowed_values(tmp_path: Path) -> None:
    path = write_criteria(tmp_path / "c.toml", dsr_trial_count="bogus")
    with pytest.raises(ResearchConfigError, match=r'must be one of "nominal"$'):
        load_research_config(path)


def test_unknown_and_missing_required_keys_are_still_rejected(tmp_path: Path) -> None:
    text = PROPOSED_CRITERIA.read_text()
    (tmp_path / "a.toml").write_text(text.replace("[criteria]", "[criteria]\ndsr_n_kind = 1", 1))
    with pytest.raises(ResearchConfigError, match=r"unknown keys \['dsr_n_kind'\]"):
        load_research_config(tmp_path / "a.toml")
    (tmp_path / "b.toml").write_text(text.replace("dsr_min = 0.95", ""))
    with pytest.raises(ResearchConfigError, match=r"missing keys \['dsr_min'\]"):
        load_research_config(tmp_path / "b.toml")


def test_dsr_trial_count_parameterises_a_row_and_is_not_one() -> None:
    assert "dsr_trial_count" in CRITERION_PARAMS
    assert DSR_TRIAL_COUNTS == ("nominal",)
    assert SuccessCriteria.__dataclass_fields__["dsr_trial_count"].default == "nominal"


# --------------------------------------------------------------------------- frozen snapshot


def _start_old_run(ledger: Ledger, tmp_path: Path, cfg: LabConfig, **snapshot_edits: Any) -> str:
    """A run frozen the way runs were before the key existed (or with ``snapshot_edits``
    applied to its criteria): research_runs rows are immutable, so it is inserted as such."""
    research = load_research_config(write_criteria(tmp_path / "old.toml"))
    config = json.loads(research.to_json())
    del config["criteria"]["dsr_trial_count"]
    config["criteria"].update(snapshot_edits)
    config["lab"] = lab_fingerprint(cfg)
    run_id = new_run_id()
    ledger.start_research_run(
        run_id, label="old", config_json=json.dumps(config), config_sha256=research.sha256
    )
    return run_id


def test_old_snapshot_without_the_key_reads_nominal(tmp_path: Path, cfg: LabConfig) -> None:
    with Ledger(tmp_path / "l.db") as led:
        run_id = _start_old_run(led, tmp_path, cfg)
        run = led.research_run(run_id)
        assert run is not None and "dsr_trial_count" not in run["config"]["criteria"]
        assert frozen_criteria(led, run_id).dsr_trial_count == "nominal"


def test_frozen_bogus_choice_fails_closed(tmp_path: Path, cfg: LabConfig) -> None:
    with Ledger(tmp_path / "l.db") as led:
        run_id = _start_old_run(led, tmp_path, cfg, dsr_trial_count="median")
        with pytest.raises(ValueError, match=r"dsr_trial_count = 'median'"):
            frozen_criteria(led, run_id)


def test_frozen_effective_choice_fails_closed_with_the_reason(
    tmp_path: Path, cfg: LabConfig
) -> None:
    """No real run froze "effective"; a hand-forged snapshot must still never be judged."""
    with Ledger(tmp_path / "l.db") as led:
        run_id = _start_old_run(led, tmp_path, cfg, dsr_trial_count="effective")
        with pytest.raises(ValueError) as err:
            frozen_criteria(led, run_id)
        assert DSR_EFFECTIVE_REFUSED in str(err.value)
        assert str(err.value).endswith("start a new run")
        with pytest.raises(ValueError, match="is not accepted"):
            evaluate_plan(led, run_id, H, cfg)


def test_frozen_unknown_key_still_fails_closed(tmp_path: Path, cfg: LabConfig) -> None:
    with Ledger(tmp_path / "l.db") as led:
        run_id = _start_old_run(led, tmp_path, cfg, n_kind="effective")
        with pytest.raises(ValueError, match=r"unknown \['n_kind'\]"):
            frozen_criteria(led, run_id)


def test_start_run_freezes_the_choice_in_research_runs_config_json(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"dsr_trial_count": "nominal"})
    out = ok(lab.server, "start_run", {"label": "nominal-n"})
    assert out["criteria"]["dsr_trial_count"] == "nominal"
    # the raw row, as stored: the choice is part of the frozen, immutable config
    with sqlite3.connect(lab.ledger_path) as conn:
        (config_json,) = conn.execute(
            "SELECT config_json FROM research_runs WHERE run_id = ?", (out["run_id"],)
        ).fetchone()
    assert json.loads(config_json)["criteria"]["dsr_trial_count"] == "nominal"
    # a later edit of the file to "effective" neither starts a run nor moves this one
    write_criteria(lab.criteria_path, dsr_trial_count="effective")
    assert "is not accepted" in error_text(lab.server, "start_run", {"label": "again"})
    with lab.ledger() as led:
        assert frozen_criteria(led, out["run_id"]).dsr_trial_count == "nominal"
        assert len(led.research_runs()) == 1


def test_an_existing_run_without_the_key_is_judged_on_nominal_end_to_end(tmp_path: Path) -> None:
    """Done-when: get_report for an existing ledgered plan (a run frozen before the key)."""
    lab = Lab(tmp_path)
    with lab.ledger() as led:
        run_id = _start_old_run(led, tmp_path, lab.cfg)
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    assert len(rep["rows"]) == 14
    tc = rep["statistics"]["trial_count"]
    assert tc["frozen_choice"] == "nominal" and tc["used_for_dsr"] == "nominal"
    assert tc["dsr_n"] == tc["nominal"] == rep["ledger_n"]
    assert 1.0 <= tc["effective_diagnostic"] <= tc["nominal"]


# --------------------------------------------------------------------------- the DSR's N


def _correlated_ledger(path: Path, tmp_path: Path, cfg: LabConfig, choice: str) -> str:
    """Ten commensurable plans (the lab's min_variance_plans) that share one market factor
    (correlation ~0.98) and differ only in drift: nominal N = 10, effective N close to 1."""
    rng = np.random.default_rng(11)
    factor = rng.normal(0.0, 0.01, N_BARS)
    mus = [0.0004, *np.linspace(-0.0003, 0.0003, 9)]
    rets = [factor + mu + rng.normal(0.0, 0.0015, N_BARS) for mu in mus]
    with Ledger(path) as led:
        run_id = start(led, tmp_path, dsr_trial_count=choice)
        base = record_trial(led, run_id, rets[0], cfg=cfg)
        for k, r in enumerate(rets[1:]):
            variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 30 + k}))
            record_trial(led, run_id, r, plan_dict=variant, source="optuna", cfg=cfg)
        record_report(led, run_id, H, "backtest", backtest_payload(base))
    return run_id


def test_dsr_deflates_with_nominal_n_even_when_the_diagnostic_is_near_one(
    tmp_path: Path, cfg: LabConfig
) -> None:
    """Ten plans on one market factor: the effective-N diagnostic says ~1 bet, but the DSR
    still deflates with the nominal N = 10 (the diagnostic never lowers the bar)."""
    run_id = _correlated_ledger(tmp_path / "n.db", tmp_path, cfg, "nominal")
    with Ledger(tmp_path / "n.db") as led:
        tc = trial_counts(led, cfg)
        returns = led.get(1, include_returns=True).returns
        sharpes = [sharpe_per_bar(led.get(i, include_returns=True).returns) for i in range(1, 11)]
        rep = evaluate_plan(led, run_id, H, cfg)
    assert tc.nominal == 10 and tc.effective < 2.0  # the ten plans are about one bet
    assert returns is not None
    variance = float(np.var(sharpes, ddof=1))
    want = deflated_sharpe(returns, n_trials=10, sharpe_variance=variance).dsr
    row = rows(rep)["dsr_min"]
    assert row["value"] == pytest.approx(want, rel=1e-12)  # same computation (float noise only)
    assert row["status"] == "fail"  # an effective N of 2 would have passed this evidence
    assert deflated_sharpe(returns, n_trials=2, sharpe_variance=variance).dsr >= 0.95
    assert "N = 10 distinct plans" in row["note"] and "never the DSR's N" in row["note"]
    stats = rep["statistics"]["trial_count"]
    assert stats["used_for_dsr"] == "nominal" and stats["dsr_n"] == 10
    assert stats["nominal"] == stats["run_nominal"] == 10
    assert stats["effective_diagnostic"] == pytest.approx(tc.effective)
    assert stats["run_effective_diagnostic"] == pytest.approx(tc.effective)
    # the shuffled null of the same ten columns: about ten independent plans
    assert stats["effective_null"] == pytest.approx(tc.effective_null)
    assert 8.0 < stats["effective_null"] <= 10.0
    assert (stats["plans"], stats["rows"]) == (10, tc.rows) and tc.rows > 0
    assert rep["statistics"]["min_backtest_length"]["n_trials"] == 10
    assert rep["ledger_n"] == 10


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("no candles"),
        sqlite3.OperationalError("database is locked"),
        zlib.error("Error -3 while decompressing data"),
        MemoryError("matrix"),
    ],
)
def test_a_failing_diagnostic_never_breaks_the_report_or_moves_the_dsr(
    tmp_path: Path, cfg: LabConfig, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    run_id = _correlated_ledger(tmp_path / "e.db", tmp_path, cfg, "nominal")
    with Ledger(tmp_path / "e.db") as led:
        healthy = rows(evaluate_plan(led, run_id, H, cfg))["dsr_min"]

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(criteria_mod, "trial_counts", broken)
    with Ledger(tmp_path / "e.db") as led:
        rep = evaluate_plan(led, run_id, H, cfg)
    row = rows(rep)["dsr_min"]
    assert (row["status"], row["value"], row["note"]) == (
        healthy["status"],
        healthy["value"],
        healthy["note"],
    )
    tc = rep["statistics"]["trial_count"]
    assert f"{type(exc).__name__}: {exc}" in tc["missing"]
    assert "effective_diagnostic" not in tc
    assert tc["frozen_choice"] == tc["used_for_dsr"] == "nominal"
    assert tc["dsr_n"] == tc["nominal"] == 10
