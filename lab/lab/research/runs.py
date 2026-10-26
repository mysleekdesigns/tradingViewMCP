"""Start research runs and measure their budget use. Shared by the MCP server, the lab API, and
the ``agents/`` orchestrator, so every entry point counts the same way.

A run freezes two things when it starts: the owner's success criteria + budget, and (when the
caller passes the :class:`~lab.config.LabConfig`) a snapshot of the result-affecting lab.toml
values with their sha256 fingerprint. :func:`check_lab_fingerprint` refuses to keep working on a
run once lab.toml changed (costs, splits, benchmarks, stats, the Optuna seed and minimum trades,
the ``[research]`` family threshold, overlap, and DSR / PBO floors, ...), because evidence recorded
under the old values would no longer be commensurable with new trials.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from lab.config import LabConfig
from lab.data.universe_build import manifest_path, manifest_sha256
from lab.ledger import Ledger
from lab.research.contract import ResearchConfig, RunBudget


class ResearchBudgetError(RuntimeError):
    """A call would exceed the research run's frozen budget. Nothing was run or recorded."""


def new_run_id(now: datetime | None = None) -> str:
    """``r-YYYYMMDD-HHMMSS-<6 hex>`` (UTC), matching ``RESEARCH_RUN_ID_PATTERN``."""
    t = (now or datetime.now(UTC)).astimezone(UTC)
    return f"r-{t:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def lab_values(cfg: LabConfig) -> dict[str, Any]:
    """The lab.toml values that change results, as JSON-safe data (dates as ISO strings)."""
    splits = asdict(cfg.splits)
    for key in ("design_start", "holdout_start"):
        splits[key] = getattr(cfg.splits, key).isoformat()
    return {
        "costs": asdict(cfg.costs),
        "stress_factors": [float(x) for x in cfg.stress_factors],
        "splits": splits,
        "benchmarks": asdict(cfg.benchmarks),
        "stats": {
            k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg.stats).items()
        },
        "optuna_min_trades": int(cfg.optuna_min_trades),
        "optuna_seed": int(cfg.optuna_seed),
        "init_cash": float(cfg.init_cash),
        "research": asdict(cfg.research),
        # the multi-coin universe (liquidity_cap_mult included) and, nested under it, the
        # [portfolio] run settings (impact, null paths, alpha window, ...): one section, so the
        # fingerprint's top-level sections are unchanged for single-symbol runs
        "universe": {
            **{
                k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg.universe).items()
            },
            "portfolio": asdict(cfg.portfolio),
        },
    }


def lab_fingerprint(cfg: LabConfig) -> dict[str, Any]:
    """``{"fingerprint": sha256 hex, "values": lab_values(cfg)}``; the fingerprint is over the
    sorted-key compact JSON of the values."""
    values = lab_values(cfg)
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return {"fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest(), "values": values}


def start_research_run(
    ledger: Ledger,
    research: ResearchConfig,
    *,
    label: str,
    cfg: LabConfig | None = None,
    now: datetime | None = None,
) -> str:
    """Create a server-assigned run id and freeze ``research`` (criteria + budget) into the
    ledger under it, plus ``config["lab"] = lab_fingerprint(cfg)`` when ``cfg`` is given.
    Returns the run id. Runs started without ``cfg`` fail :func:`check_lab_fingerprint`."""
    run_id = new_run_id(now)
    config = json.loads(research.to_json())
    if cfg is not None:
        config["lab"] = lab_fingerprint(cfg)
        manifest = universe_manifest(cfg)
        if manifest is not None:
            config["universe_manifest"] = manifest
    ledger.start_research_run(
        run_id,
        label=label,
        config_json=json.dumps(config, sort_keys=True),
        config_sha256=research.sha256,
    )
    return run_id


def check_lab_fingerprint(ledger: Ledger, run_id: str, cfg: LabConfig) -> None:
    """Raise ``ValueError`` unless ``run_id`` exists, froze a lab.toml fingerprint, and that
    fingerprint equals ``cfg``'s (the message names the changed sections)."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    frozen = run["config"].get("lab") if isinstance(run["config"], dict) else None
    if not isinstance(frozen, dict) or not isinstance(frozen.get("fingerprint"), str):
        raise ValueError(
            f"research run {run_id!r} has no lab.toml fingerprint (it was started without the "
            "lab config), so its evidence cannot be trusted to match this lab; start a new run"
        )
    now = lab_fingerprint(cfg)
    if frozen["fingerprint"] != now["fingerprint"]:
        old_values = frozen.get("values") if isinstance(frozen.get("values"), dict) else {}
        changed = sorted(k for k in now["values"] if old_values.get(k) != now["values"][k])
        raise ValueError(
            "lab.toml changed since this run started; start a new run (research run "
            f"{run_id!r}; changed: {', '.join(changed) or 'values'})"
        )


def universe_manifest(cfg: LabConfig) -> dict[str, str] | None:
    """``{"file", "sha256"}`` of the built universe's manifest for ``cfg.universe`` (quote and
    interval), or None when no universe has been built. Frozen into a run at ``start_run``."""
    u = cfg.universe
    digest = manifest_sha256(cfg.universe_dir, u.quote, u.interval)
    if digest is None:
        return None
    return {"file": manifest_path(cfg.universe_dir, u.quote, u.interval).name, "sha256": digest}


def check_universe_manifest(ledger: Ledger, run_id: str, cfg: LabConfig) -> str:
    """The universe manifest sha256 frozen into ``run_id``; ``ValueError`` when the run never
    started, froze no manifest (the universe did not exist when it started), or the manifest
    on disk differs now (the universe was rebuilt: evidence would mix two universes)."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    frozen = run["config"].get("universe_manifest") if isinstance(run["config"], dict) else None
    if not isinstance(frozen, dict) or not isinstance(frozen.get("sha256"), str):
        raise ValueError(
            f"research run {run_id!r} froze no universe manifest (no universe was built when it "
            "started), so portfolio plans cannot run in it; build the universe (`uv run python "
            "-m lab.data.universe_build build`) and start a new run"
        )
    now = universe_manifest(cfg)
    if now is None or now["sha256"] != frozen["sha256"]:
        state = "is missing" if now is None else "changed"
        raise ValueError(
            f"the universe manifest {state} since research run {run_id!r} started (frozen sha256 "
            f"{frozen['sha256'][:12]}); a rebuilt universe is a different dataset, so start a "
            "new run"
        )
    return str(frozen["sha256"])


def frozen_budget(ledger: Ledger, run_id: str) -> RunBudget:
    """The budget snapshotted when ``run_id`` started. ``ValueError`` if it never started."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    return RunBudget(**run["config"]["budget"])


def holdout_budget(ledger: Ledger, run_id: str, cfg: LabConfig) -> int:
    """Effective holdout checks for the run: the frozen budget, capped by lab.toml."""
    return min(frozen_budget(ledger, run_id).max_holdout_checks, cfg.splits.holdout_budget)


def run_usage(ledger: Ledger, run_id: str) -> dict[str, int]:
    """Server-side counts: ``proposals`` (distinct plans submitted for evaluation, i.e. with
    any evidence row; Optuna's tuned variants of one plan are one proposal), ``backtests``
    (every ledger trial under the run, Optuna trials included), ``holdout_checks`` (spent)."""
    return {
        "proposals": ledger.count_evidence_plans(run_id),
        "backtests": ledger.count(run_id),
        "holdout_checks": ledger.holdout_checks_used(run_id),
    }


def budget_status(ledger: Ledger, run_id: str, cfg: LabConfig) -> dict[str, Any]:
    """``{budget, used, remaining}`` for the server-enforced dimensions."""
    b = frozen_budget(ledger, run_id)
    used = run_usage(ledger, run_id)
    limits = {
        "proposals": b.max_proposals,
        "backtests": b.max_backtests,
        "holdout_checks": holdout_budget(ledger, run_id, cfg),
    }
    return {
        "budget": limits,
        "used": used,
        "remaining": {k: max(0, limits[k] - used[k]) for k in limits},
    }
