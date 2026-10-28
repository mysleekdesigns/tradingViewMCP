"""Phase 11: the research run's software record (Optuna / vectorbt / numpy / pandas / Python
versions and the explicit TPE sampler settings), frozen beside the lab.toml fingerprint at
start_run and checked wherever the fingerprint is checked."""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import numpy as np
import optuna
import pandas as pd
import pytest
import vectorbt as vbt

from lab.ledger import Ledger
from lab.research import runs
from lab.research.contract import load_research_config
from lab.research.runs import (
    check_lab_fingerprint,
    check_software,
    lab_fingerprint,
    lab_values,
    software_values,
    start_research_run,
)
from lab.search.optuna_search import TPE_SAMPLER_SETTINGS
from tests.test_mcp_server import small_cfg, write_criteria


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start(ledger: Ledger, tmp_path: Path) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml")
    return start_research_run(
        ledger, load_research_config(path), label="lbl", cfg=small_cfg(tmp_path)
    )


def start_old_run(ledger: Ledger, tmp_path: Path) -> str:
    """A run as started before the software record existed: same criteria, budget and lab
    fingerprint, no ``software`` key."""
    research = load_research_config(write_criteria(tmp_path / "old.toml"))
    config = json.loads(research.to_json())
    config["lab"] = lab_fingerprint(small_cfg(tmp_path))
    ledger.start_research_run(
        "r-20261001-000000-abcdef",
        label="old",
        config_json=json.dumps(config, sort_keys=True),
        config_sha256=research.sha256,
    )
    return "r-20261001-000000-abcdef"


def test_software_values_record_the_installed_versions_and_sampler_settings() -> None:
    assert software_values() == {
        "optuna": optuna.__version__,
        "vectorbt": vbt.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "python": platform.python_version(),
        "tpe_sampler": {
            "multivariate": True,
            "constant_liar": True,
            "n_startup_trials": 10,
            "n_ei_candidates": 24,
            "group": False,
        },
    }
    assert software_values()["tpe_sampler"] == dict(TPE_SAMPLER_SETTINGS)
    json.dumps(software_values())  # JSON-safe, so it can be frozen into the ledger


def test_a_new_run_freezes_the_software_record(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path)
    run = ledger.research_run(run_id)
    assert run is not None
    assert run["config"]["software"] == software_values()


def test_same_software_passes_the_check(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path)
    check_lab_fingerprint(ledger, run_id, small_cfg(tmp_path))  # no raise


def test_a_changed_optuna_version_is_refused_naming_optuna(
    ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start(ledger, tmp_path)
    installed = optuna.__version__
    monkeypatch.setattr(runs.optuna, "__version__", "9.9.9")
    with pytest.raises(ValueError) as info:
        check_lab_fingerprint(ledger, run_id, small_cfg(tmp_path))
    message = str(info.value)
    assert "software changed since this run started; start a new run" in message
    assert f"changed: optuna {installed!r} -> '9.9.9'" in message
    assert "vectorbt" not in message  # only the changed entry is named


def test_changed_sampler_settings_are_refused_naming_tpe_sampler(
    ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start(ledger, tmp_path)
    monkeypatch.setattr(
        runs, "TPE_SAMPLER_SETTINGS", {**TPE_SAMPLER_SETTINGS, "multivariate": False}
    )
    with pytest.raises(ValueError, match=r"changed: tpe_sampler "):
        check_lab_fingerprint(ledger, run_id, small_cfg(tmp_path))


def test_a_run_without_a_software_record_is_not_checked(
    ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_old_run(ledger, tmp_path)
    monkeypatch.setattr(runs.optuna, "__version__", "9.9.9")
    check_lab_fingerprint(ledger, run_id, small_cfg(tmp_path))  # no raise: nothing frozen


def test_an_unreadable_software_record_is_refused() -> None:
    with pytest.raises(ValueError, match=r"unreadable software record.*start a new run"):
        check_software({"software": "5.0.0"}, "r-x")


def test_the_lab_fingerprint_is_unchanged_by_the_software_record(
    ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = small_cfg(tmp_path)
    before = lab_fingerprint(cfg)
    assert "software" not in lab_values(cfg)
    monkeypatch.setattr(runs.optuna, "__version__", "9.9.9")
    monkeypatch.setattr(runs.vbt, "__version__", "9.9.9")
    assert lab_fingerprint(cfg) == before  # versions never enter the hashed values
    # an old run and a new run started from the same lab.toml carry the same fingerprint
    old = ledger.research_run(start_old_run(ledger, tmp_path))
    monkeypatch.undo()
    new = ledger.research_run(start(ledger, tmp_path))
    assert old is not None and new is not None
    assert old["config"]["lab"] == new["config"]["lab"] == lab_fingerprint(cfg)


def test_a_run_started_without_the_lab_config_freezes_no_software_record(
    ledger: Ledger, tmp_path: Path
) -> None:
    research = load_research_config(write_criteria(tmp_path / "c.toml"))
    run_id = start_research_run(ledger, research, label="no cfg")
    run = ledger.research_run(run_id)
    assert run is not None and "software" not in run["config"]
