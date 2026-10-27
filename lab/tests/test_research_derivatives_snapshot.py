"""The derivatives snapshot of a research run (PLAN.md Phase 12, item 4: "the derivatives
manifest sha256 joins the research-run fingerprint").

A run freezes ``{"manifest_sha256", "stale_after_intervals"}`` beside (not inside) its lab.toml
fingerprint, so runs started before derivatives existed keep their fingerprint and EMA plans in
them still run; a plan that reads a derivatives column is refused, before any trial, budget or
holdout check is spent, when the run froze no snapshot or the archive / staleness rule changed.

Offline: tmp ledgers, a tmp ``data_dir`` with a small synthetic BTCUSDT funding series on disk
and its built ``derivatives/manifest.json`` (or a hand-written one, to simulate a rebuild), and
seeded synthetic candles (the loader injects their ``drv_*`` columns; the series on disk is what
the snapshot freezes and re-hashes)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lab.config import LabConfig, load_config
from lab.data.derivatives import build_manifest
from lab.ledger import Ledger
from lab.mcp_server import DERIVATIVES_GATE_NOTE, build_server
from lab.plan import parse_plan, plan_hash, plan_to_dict, with_params
from lab.registry.derivatives import DERIVATIVES_FEATURES
from lab.research.contract import load_research_config
from lab.research.criteria import evaluate_plan
from lab.research.evidence import record_report
from lab.research.holdout import HoldoutRefusedError, _load_full, run_holdout_check
from lab.research.logic import logic_hash
from lab.research.runs import (
    check_derivatives_snapshot,
    derivatives_snapshot,
    lab_fingerprint,
    lab_values,
    start_research_run,
)
from tests.synthetic import with_derivatives, write_derivatives_archive
from tests.test_mcp_server import (
    EMA_PLAN,
    LAX,
    Lab,
    counts,
    error_text,
    loader_for,
    ok,
    small_cfg,
    trend_candles,
    write_criteria,
)
from tests.test_research_criteria import WINDOW, adversarial_payload, backtest_payload, trial
from tests.test_research_holdout import crosscheck_payload

# close > ema_slow is the price signal; the funding z-score only gates the entry (validator)
GATED_PLAN: dict[str, Any] = {
    "name": "ema_trend_funding_gate",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {
        "ema_fast": {"ema": {"period": 20}},
        "ema_slow": {"ema": {"period": 50}},
        "fz": {"funding_zscore": {"period": 30}},
    },
    "long_entry": "ema_fast > ema_slow and fz < 2",
    "long_exit": "ema_fast < ema_slow",
    "risk": {"position_size_pct": 100},
}
GATED_SPACE = {"ema_fast.period": {"low": 10, "high": 30}}
SPOT = trend_candles()
WITH_DRV = with_derivatives(SPOT, seed=3)
# the top-level sections of lab_values before Phase 12; adding one would change every in-flight
# run's fingerprint (check_lab_fingerprint refuses any change)
LAB_SECTIONS = {
    "costs",
    "stress_factors",
    "splits",
    "benchmarks",
    "stats",
    "optuna_min_trades",
    "optuna_seed",
    "init_cash",
    "research",
    "universe",
}


# the synthetic derivatives archive starts here: + [derivatives] warmup_days 30 = 2018-07-01, the
# mcp small_cfg design_start, so the shared derivatives scored start is the design start
ARCHIVE_FIRST = "2018-06-01"


def write_manifest(cfg: LabConfig, text: str | None = None) -> str:
    """Build ``{derivatives_dir}/manifest.json`` over a small synthetic BTCUSDT 4h archive (all
    five derivatives series from ``ARCHIVE_FIRST``, funding and klines through 2020-05, written
    once), or, with ``text``, overwrite it with that text (a rebuilt archive); returns its
    sha256."""
    path = cfg.derivatives_dir / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        if not (cfg.derivatives_dir / "fundingRate" / "BTCUSDT").is_dir():
            write_derivatives_archive(
                cfg.derivatives_dir,
                first=ARCHIVE_FIRST,
                end="2020-06-01",
                gap_factor=cfg.derivatives.gap_factor,
            )
        build_manifest(cfg.derivatives_dir, gap_factor=cfg.derivatives.gap_factor)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def drv_lab(tmp_path: Path, *, candles: Any = WITH_DRV, manifest: bool = True, **kw: Any) -> Lab:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lab = Lab(tmp_path, candles=candles, **kw)
    if manifest:
        write_manifest(lab.cfg)
    return lab


def with_stale(lab: Lab, stale: float) -> Any:
    """A second server on the same ledger whose lab.toml [derivatives] staleness differs."""
    cfg = replace(lab.cfg, derivatives=replace(lab.cfg.derivatives, stale_after_intervals=stale))
    return build_server(
        cfg=cfg,
        ledger_path=lab.ledger_path,
        loader=loader_for(lab.candles),
        research_config_path=lab.criteria_path,
    )


# --------------------------------------------------------------------------- runs.py


def test_snapshot_is_none_without_a_manifest(tmp_path: Path) -> None:
    assert derivatives_snapshot(small_cfg(tmp_path)) is None


def test_snapshot_holds_the_manifest_sha_and_staleness(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    sha = write_manifest(cfg)
    manifest = json.loads((cfg.derivatives_dir / "manifest.json").read_text())
    assert derivatives_snapshot(cfg) == {
        "manifest_sha256": sha,
        "schema_version": manifest["schema_version"],
        "series": {
            key: {k: entry[k] for k in ("content_sha256", "first", "last", "rows", "gaps")}
            for key, entry in manifest["series"].items()
        },
        "stale_after_intervals": float(cfg.derivatives.stale_after_intervals),
        "gate_base_max_true_frac": float(cfg.derivatives.gate_base_max_true_frac),
        "warmup_days": float(cfg.derivatives.warmup_days),
    }
    assert "fundingRate/BTCUSDT" in manifest["series"] and len(manifest["series"]) == 5


def test_start_research_run_freezes_the_snapshot(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    sha = write_manifest(cfg)
    research = load_research_config(write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_research_run(led, research, label="t", cfg=cfg)
        run = led.research_run(run_id)
        assert run is not None and run["config"]["derivatives"]["manifest_sha256"] == sha
        assert check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN) == sha


def test_lab_values_and_fingerprint_are_unchanged_by_derivatives(tmp_path: Path) -> None:
    """In-flight runs must keep their lab.toml fingerprint: lab_values has no derivatives
    section, and neither the staleness rule nor a manifest moves the fingerprint."""
    cfg = small_cfg(tmp_path)
    assert set(lab_values(cfg)) == LAB_SECTIONS
    before = lab_fingerprint(cfg)["fingerprint"]
    write_manifest(cfg)
    stale = replace(cfg, derivatives=replace(cfg.derivatives, stale_after_intervals=7.0))
    assert lab_fingerprint(cfg)["fingerprint"] == before
    assert lab_fingerprint(stale)["fingerprint"] == before
    assert lab_fingerprint(load_config())["values"].keys() == LAB_SECTIONS


def test_run_without_a_snapshot_is_refused_with_start_a_new_run(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    research = load_research_config(write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_research_run(led, research, label="old", cfg=cfg)  # no manifest yet
        write_manifest(cfg)
        with pytest.raises(ValueError, match=r"froze no derivatives snapshot.*start a new run"):
            check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("manifest", r"derivatives manifest changed.*start a new run"),
        ("missing", "derivatives manifest is missing"),
        ("stale", r"stale_after_intervals changed.*start a new run"),
    ],
)
def test_changed_snapshot_is_refused(tmp_path: Path, change: str, message: str) -> None:
    cfg = small_cfg(tmp_path)
    write_manifest(cfg)
    research = load_research_config(write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_research_run(led, research, label="t", cfg=cfg)
        now = cfg
        if change == "manifest":
            write_manifest(cfg, '{"series": {"rebuilt": true}}')
        elif change == "missing":
            (cfg.derivatives_dir / "manifest.json").unlink()
        else:
            now = replace(cfg, derivatives=replace(cfg.derivatives, stale_after_intervals=3.0))
        with pytest.raises(ValueError, match=message):
            check_derivatives_snapshot(led, run_id, now, GATED_PLAN)


# --------------------------------------------------------------------------- MCP


def test_start_run_returns_the_manifest_sha_or_null(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path / "a", manifest=False)
    assert ok(lab.server, "start_run", {"label": "x"})["derivatives_manifest_sha256"] is None
    lab = drv_lab(tmp_path / "b", manifest=False)
    sha = write_manifest(lab.cfg)
    assert ok(lab.server, "start_run", {"label": "x"})["derivatives_manifest_sha256"] == sha


def test_list_features_marks_derivatives_features_as_gates(tmp_path: Path) -> None:
    out = ok(Lab(tmp_path).server, "list_features")
    by_name = {f["name"]: f for f in out["features"]}
    assert DERIVATIVES_FEATURES and set(by_name) >= DERIVATIVES_FEATURES
    for name in DERIVATIVES_FEATURES:
        assert by_name[name]["role"] == "gate"
        assert by_name[name]["note"] == DERIVATIVES_GATE_NOTE
    assert "long_entry" in DERIVATIVES_GATE_NOTE and "long_exit" in DERIVATIVES_GATE_NOTE
    assert "role" not in by_name["ema"]


def test_derivatives_plan_runs_when_the_snapshot_matches(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})
    assert out["budget"]["used"]["proposals"] == 1
    ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": out["plan_hash"]})


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("run_backtest", {}),
        ("run_adversarial", {}),
        ("run_crosscheck", {}),
        ("run_sweep", {"space": GATED_SPACE, "n_trials": 2}),
        ("walk_forward", {"space": GATED_SPACE, "n_trials_per_fold": 1}),
        ("holdout_check", {}),
    ],
)
def test_old_run_refuses_a_derivatives_plan_without_spending(
    tmp_path: Path, tool: str, extra: dict[str, Any]
) -> None:
    lab = drv_lab(tmp_path, manifest=False)
    run_id = lab.start()  # started before the archive existed: no snapshot
    write_manifest(lab.cfg)
    msg = error_text(lab.server, tool, {"run_id": run_id, "plan": GATED_PLAN, **extra})
    assert "froze no derivatives snapshot" in msg and "start a new run" in msg
    assert counts(lab, run_id) == (0, 0, 0)  # no trial, no evidence, no holdout check


def test_old_run_still_accepts_an_ema_plan(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path, manifest=False)
    run_id = lab.start()
    write_manifest(lab.cfg)
    assert "froze no derivatives snapshot" in error_text(
        lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN}
    )
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert out["budget"]["used"]["proposals"] == 1


def test_rewritten_manifest_refuses_backtest_and_report(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    h = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})["plan_hash"]
    before = counts(lab, run_id)
    write_manifest(lab.cfg, '{"series": {"redownloaded": 1}}')
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})
    assert "derivatives manifest changed" in msg and "start a new run" in msg
    assert "derivatives manifest changed" in error_text(
        lab.server, "get_report", {"run_id": run_id, "plan_hash": h}
    )
    assert counts(lab, run_id) == before
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})  # EMA plans unaffected


def test_changed_staleness_refuses_a_derivatives_sweep(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    server = with_stale(lab, lab.cfg.derivatives.stale_after_intervals + 1.0)
    msg = error_text(
        server,
        "run_sweep",
        {"run_id": run_id, "plan": GATED_PLAN, "space": GATED_SPACE, "n_trials": 2},
    )
    assert "stale_after_intervals changed" in msg
    assert counts(lab, run_id) == (0, 0, 0)


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("run_backtest", {}),
        ("run_adversarial", {}),
        ("run_sweep", {"space": GATED_SPACE, "n_trials": 2}),
        ("walk_forward", {"space": GATED_SPACE, "n_trials_per_fold": 1}),
    ],
)
def test_missing_derivatives_data_is_a_tool_error_not_a_trial(
    tmp_path: Path, tool: str, extra: dict[str, Any]
) -> None:
    lab = drv_lab(tmp_path, candles=SPOT)  # snapshot frozen, but no drv_* columns loaded
    run_id = lab.start()
    msg = error_text(lab.server, tool, {"run_id": run_id, "plan": GATED_PLAN, **extra})
    assert "drv_funding_rate" in msg and "lab.data.derivatives download" in msg
    assert counts(lab, run_id) == (0, 0, 0)


# --------------------------------------------------------------------------- holdout


def seed_gated_pre_holdout(ledger: Ledger, run_id: str, cfg: LabConfig) -> str:
    """Hand-built evidence that passes every pre-holdout row of ``LAX`` for GATED_PLAN (as
    ``tests.test_research_holdout.seed_pre_holdout`` does for TREND_PLAN)."""
    p = parse_plan(GATED_PLAN)
    rng = np.random.default_rng(11)
    base = trial(ledger, run_id, rng.normal(0.002, 0.01, 600), plan_dict=GATED_PLAN, cfg=cfg)
    study = f"S-{run_id}"
    for k in range(cfg.research.min_pbo_trials):
        variant = plan_to_dict(with_params(p, {"ema_fast.period": 30 + k}))
        params = json.dumps({"ema_fast.period": 30 + k, "_study": {"study": study}})
        trial(
            ledger,
            run_id,
            rng.normal(0.0, 0.01, 600),
            plan_dict=variant,
            source="optuna",
            cfg=cfg,
            params_json=params,
        )
    h = plan_hash(p)
    record_report(
        ledger, run_id, h, "backtest", backtest_payload(base, plan=plan_to_dict(p), plan_hash=h)
    )
    record_report(
        ledger,
        run_id,
        h,
        "sweep",
        {"study": study, "window": WINDOW, "logic_hash": logic_hash(p), "plan": plan_to_dict(p)},
    )
    record_report(ledger, run_id, h, "adversarial", adversarial_payload())
    record_report(ledger, run_id, h, "walk_forward", {"oos_metrics": {"sharpe": 1.0}})
    record_report(ledger, run_id, h, "crosscheck", crosscheck_payload())
    return h


def start_lax(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> str:
    research = load_research_config(write_criteria(tmp_path / "lax.toml", **LAX))
    return start_research_run(ledger, research, label="t", cfg=cfg)


def test_holdout_refuses_a_run_without_snapshot_and_spends_nothing(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)  # no manifest: no snapshot frozen
        h = seed_gated_pre_holdout(led, run_id, cfg)
        write_manifest(cfg)
        n = led.count(run_id)
        with pytest.raises(HoldoutRefusedError, match="froze no derivatives snapshot"):
            run_holdout_check(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(WITH_DRV)
            )
        with pytest.raises(ValueError, match="froze no derivatives snapshot"):
            evaluate_plan(led, run_id, h, cfg, loader=loader_for(WITH_DRV))
        assert led.holdout_checks_used(run_id) == 0 and led.count(run_id) == n
        assert led.evidence(run_id, plan_hash=h, kind="holdout") == []


def test_holdout_refuses_missing_derivatives_data_and_spends_nothing(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    write_manifest(cfg)
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)
        h = seed_gated_pre_holdout(led, run_id, cfg)
        assert evaluate_plan(led, run_id, h, cfg, loader=loader_for(SPOT))["pre_holdout_passed"]
        n = led.count(run_id)
        with pytest.raises(HoldoutRefusedError, match=r"drv_funding_rate.*no holdout check"):
            run_holdout_check(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(SPOT)
            )
        assert led.holdout_checks_used(run_id) == 0 and led.count(run_id) == n
        # with the inputs loaded, the same plan does spend its one check
        out = run_holdout_check(
            GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(WITH_DRV)
        )
        assert out["checks_used"] == 1 and set(out) == {"passed", "checks_used", "checks_left"}


def test_load_full_leaves_plans_without_derivatives_alone(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    candles, window = _load_full(parse_plan(EMA_PLAN), cfg, loader_for(SPOT))
    assert len(candles) == len(SPOT) and window.kind == "holdout"
    with pytest.raises(HoldoutRefusedError, match="no holdout check was spent"):
        _load_full(parse_plan(GATED_PLAN), cfg, loader_for(SPOT))
