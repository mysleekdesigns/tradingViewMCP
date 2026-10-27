"""Start research runs and measure their budget use. Shared by the MCP server, the lab API, and
the ``agents/`` orchestrator, so every entry point counts the same way.

A run freezes two things when it starts: the owner's success criteria + budget, and (when the
caller passes the :class:`~lab.config.LabConfig`) a snapshot of the result-affecting lab.toml
values with their sha256 fingerprint. :func:`check_lab_fingerprint` refuses to keep working on a
run once lab.toml changed (costs, splits, benchmarks, stats, the Optuna seed and minimum trades,
the ``[research]`` family threshold, overlap, and DSR / PBO floors, ...), because evidence recorded
under the old values would no longer be commensurable with new trials.

Two dataset snapshots are frozen beside the fingerprint (not inside it, so runs started before
they existed keep their fingerprint): the built universe's manifest (portfolio plans,
:func:`check_universe_manifest`) and the derivatives snapshot (plans that use a derivatives
feature, :func:`check_derivatives_snapshot`). A plan that needs a snapshot the run did not
freeze, or whose snapshot changed on disk, is refused before anything runs or is recorded.

The derivatives snapshot is PER SERIES: the manifest's ``schema_version``, every series entry
(``content_sha256``, ``first``, ``last``, ``rows``, and its ``gaps`` for the holdout's coverage
check), the ``[derivatives]`` staleness rule, gate threshold and ``warmup_days``, plus the whole
manifest's sha256 (reported by ``start_run``). Every derivatives plan on a symbol/interval is
scored from one start computed from the FROZEN ``first`` values + the frozen ``warmup_days``
(:func:`lab.runner.derivatives_series`), so the start never moves mid-run. A check compares only
the series the plan reads for its symbol (and interval, for klines), so downloading ETHUSDT
mid-run does not refuse BTCUSDT plans, and it recomputes each of those series' content sha256
from the Parquet files on disk (cached in-process per file list, size and mtime), so a Parquet
edit without a manifest rebuild is refused too. A series the run did not freeze is refused.
Snapshots frozen before this layout (no ``series``) fall back to comparing the whole manifest's
sha256.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.config import LabConfig
from lab.data import derivatives as drv_data
from lab.data.derivatives import manifest_sha256 as derivatives_manifest_sha256
from lab.data.derivatives_columns import COLUMN_SOURCES, plan_derivative_columns
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
    ledger under it, plus ``config["lab"] = lab_fingerprint(cfg)`` when ``cfg`` is given (and
    the universe manifest / derivatives snapshot, when they exist on disk). Returns the run id.
    Runs started without ``cfg`` fail :func:`check_lab_fingerprint`."""
    run_id = new_run_id(now)
    config = json.loads(research.to_json())
    if cfg is not None:
        config["lab"] = lab_fingerprint(cfg)
        manifest = universe_manifest(cfg)
        if manifest is not None:
            config["universe_manifest"] = manifest
        derivatives = derivatives_snapshot(cfg)
        if derivatives is not None:
            config["derivatives"] = derivatives
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


# fields of a manifest series entry frozen into a run and compared by the check
SNAPSHOT_SERIES_FIELDS: tuple[str, ...] = ("content_sha256", "first", "last", "rows")

# (data_dir, series key) -> (file signature, content sha256): a series is re-hashed only when one
# of its Parquet files is added, removed, resized or touched (MCP server lifetime)
_CONTENT_CACHE: dict[tuple[str, str], tuple[tuple[tuple[str, int, int], ...], str]] = {}


def _read_manifest(data_dir: Path) -> dict[str, Any] | None:
    path = Path(data_dir) / drv_data.MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def derivatives_snapshot(cfg: LabConfig) -> dict[str, Any] | None:
    """The derivatives snapshot frozen at ``start_run`` (module docstring): ``{"manifest_sha256",
    "schema_version", "series": {key: {content_sha256, first, last, rows, gaps}},
    "stale_after_intervals", "gate_base_max_true_frac", "warmup_days"}``, or None when no
    manifest has been built. Kept outside :func:`lab_values`, so the lab.toml fingerprint of
    earlier runs is unchanged."""
    digest = derivatives_manifest_sha256(cfg.derivatives_dir)
    if digest is None:
        return None
    manifest = _read_manifest(cfg.derivatives_dir) or {}
    raw_series = manifest.get("series")
    series: dict[str, dict[str, Any]] = {}
    if isinstance(raw_series, Mapping):
        for key, entry in raw_series.items():
            if isinstance(entry, Mapping):
                series[str(key)] = {f: entry.get(f) for f in SNAPSHOT_SERIES_FIELDS}
                gaps = entry.get("gaps")
                series[str(key)]["gaps"] = list(gaps) if isinstance(gaps, list) else []
    return {
        "manifest_sha256": digest,
        "schema_version": manifest.get("schema_version"),
        "series": series,
        "stale_after_intervals": float(cfg.derivatives.stale_after_intervals),
        "gate_base_max_true_frac": float(cfg.derivatives.gate_base_max_true_frac),
        "warmup_days": float(cfg.derivatives.warmup_days),
    }


def _plan_series(plan: Any) -> tuple[list[tuple[str, str, str | None]], str]:
    """``([(dataset, symbol, interval-or-None), ...], symbol)`` of the archive series ``plan``
    reads (a Plan, plan dict or plan JSON)."""
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            plan = {}
    if isinstance(plan, Mapping):
        symbol, interval = str(plan.get("symbol", "")), str(plan.get("interval", ""))
    else:
        symbol, interval = str(plan.symbol), str(plan.interval)
    out: list[tuple[str, str, str | None]] = []
    for column in plan_derivative_columns(plan):
        dataset = COLUMN_SOURCES[column][0]
        iv = interval if dataset in drv_data.KLINE_DATASETS else None
        if (dataset, symbol, iv) not in out:
            out.append((dataset, symbol, iv))
    return out, symbol


def series_content_sha256(data_dir: Path, dataset: str, symbol: str, interval: str | None) -> str:
    """``lab.data.derivatives.content_sha256`` of the WHOLE series as loaded from disk now (the
    value the manifest records), cached per (file names, sizes, mtimes)."""
    folder = drv_data.series_dir(data_dir, dataset, symbol, interval)
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    sig = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files if p.is_file())
    key = (str(Path(data_dir).resolve()), drv_data.series_key(dataset, symbol, interval))
    hit = _CONTENT_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    frame = drv_data.load_series(dataset, symbol, data_dir=data_dir, interval=interval)
    digest = drv_data.content_sha256(frame)
    _CONTENT_CACHE[key] = (sig, digest)
    return digest


def check_derivatives_snapshot(ledger: Ledger, run_id: str, cfg: LabConfig, plan: Any) -> str:
    """The derivatives manifest sha256 frozen into ``run_id``, after checking the series ``plan``
    (a Plan, plan dict or plan JSON) reads; ``ValueError`` (message says to start a new run)
    when the run never started, froze no derivatives snapshot, the staleness rule or gate
    threshold changed, the manifest is gone, a series the plan reads was not frozen, its
    manifest entry or schema_version changed, ``warmup_days`` changed (or was never frozen), or
    its Parquet content on disk no longer matches the frozen ``content_sha256``. Only the plan's
    own series are compared (module docstring); plans without derivatives features never call
    it."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    frozen = run["config"].get("derivatives") if isinstance(run["config"], dict) else None
    if not isinstance(frozen, dict) or not isinstance(frozen.get("manifest_sha256"), str):
        raise ValueError(
            f"research run {run_id!r} froze no derivatives snapshot (no derivatives manifest "
            "existed when it started, or it predates derivatives features), so plans using a "
            "derivatives feature cannot run in it; download the archive (`cd lab && uv run "
            "python -m lab.data.derivatives download ...`) and start a new run"
        )
    if frozen.get("stale_after_intervals") != float(cfg.derivatives.stale_after_intervals):
        raise ValueError(
            "lab.toml [derivatives] stale_after_intervals changed since research run "
            f"{run_id!r} started ({frozen.get('stale_after_intervals')!r} -> "
            f"{float(cfg.derivatives.stale_after_intervals)!r}); the alignment rule changes "
            "every gated signal, so start a new run"
        )
    gate = frozen.get("gate_base_max_true_frac")
    if gate is not None and gate != float(cfg.derivatives.gate_base_max_true_frac):
        raise ValueError(
            "lab.toml [derivatives] gate_base_max_true_frac changed since research run "
            f"{run_id!r} started ({gate!r} -> {float(cfg.derivatives.gate_base_max_true_frac)!r}); "
            "the bar for which gated plans may run cannot move mid-run, so start a new run"
        )
    warm = frozen.get("warmup_days")
    if warm != float(cfg.derivatives.warmup_days):
        raise ValueError(
            "lab.toml [derivatives] warmup_days changed since research run "
            f"{run_id!r} started ({warm!r} -> {float(cfg.derivatives.warmup_days)!r}; None: the "
            "run predates the shared derivatives scored start); every derivatives plan's scored "
            "start depends on it and cannot move mid-run, so start a new run"
        )
    data_dir = Path(cfg.derivatives_dir)
    now_sha = derivatives_manifest_sha256(data_dir)
    if now_sha is None:
        raise ValueError(
            f"the derivatives manifest is missing since research run {run_id!r} started "
            f"(frozen sha256 {frozen['manifest_sha256'][:12]}); restore it or start a new run"
        )
    frozen_series = frozen.get("series")
    if not isinstance(frozen_series, Mapping):  # a snapshot frozen before per-series entries
        if now_sha != frozen["manifest_sha256"]:
            raise ValueError(
                f"the derivatives manifest changed since research run {run_id!r} started "
                f"(frozen sha256 {frozen['manifest_sha256'][:12]}, now {now_sha[:12]}); a "
                "re-downloaded archive is a different dataset, so start a new run"
            )
        return str(frozen["manifest_sha256"])
    manifest = _read_manifest(data_dir) or {}
    if manifest.get("schema_version") != frozen.get("schema_version"):
        raise ValueError(
            f"the derivatives manifest changed since research run {run_id!r} started "
            f"(schema_version {frozen.get('schema_version')!r} -> "
            f"{manifest.get('schema_version')!r}); start a new run"
        )
    now_series = manifest.get("series") if isinstance(manifest.get("series"), Mapping) else {}
    series, symbol = _plan_series(plan)
    for dataset, sym, interval in series:
        key = drv_data.series_key(dataset, sym, interval)
        want = frozen_series.get(key)
        if not isinstance(want, Mapping):
            raise ValueError(
                f"derivatives series {key} was not in the archive when research run {run_id!r} "
                f"started (frozen series: {', '.join(sorted(frozen_series)) or 'none'}), so "
                f"{symbol} plans that read it cannot run in this run; download it and start a "
                "new run"
            )
        entry = now_series.get(key)
        have = (
            {f: entry.get(f) for f in SNAPSHOT_SERIES_FIELDS}
            if isinstance(entry, Mapping)
            else None
        )
        if have != {f: want.get(f) for f in SNAPSHOT_SERIES_FIELDS}:
            raise ValueError(
                f"the derivatives manifest changed for {key} since research run {run_id!r} "
                f"started (frozen rows {want.get('rows')!r}, last {want.get('last')!r}); a "
                "re-downloaded series is a different dataset, so start a new run"
            )
        try:
            content = series_content_sha256(data_dir, dataset, sym, interval)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"derivatives series {key} cannot be read to verify it against research run "
                f"{run_id!r}'s snapshot: {exc}; restore it or start a new run"
            ) from exc
        if content != want.get("content_sha256"):
            raise ValueError(
                f"the derivatives data on disk for {key} changed since research run {run_id!r} "
                f"started (content sha256 {str(want.get('content_sha256'))[:12]} frozen, "
                f"{content[:12]} now: a Parquet file was edited or replaced without a manifest "
                "rebuild); restore it or start a new run"
            )
    return str(frozen["manifest_sha256"])


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
