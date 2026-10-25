"""Seed importer: popular Freqtrade strategies translated into plans (PLAN.md Phase 8, step 0).

``agents/seeds/*.json`` holds the translations (see ``agents/seeds/README.md``).
:func:`run_seed_calibration` runs every seed through the honest pipeline under a fresh research
run (design-window backtest + adversarial evidence via ``lab.research.pipeline``, the same path
the MCP server uses, so backtest evidence carries the regime breakdown), judges each
against the run's frozen success criteria, and writes a Markdown calibration report to the
journal.

Popular published strategies are expected to FAIL; that calibrates the pipeline and seeds a
library of known failures. Calibration never calls the holdout check. Results are historical
simulations, not forward-looking.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.plan.validator import parse_plan, plan_hash, validate_plan
from lab.research.contract import AGENTS_DIR, JOURNAL_DIR, ResearchConfig
from lab.research.pipeline import adversarial_evidence, backtest_evidence
from lab.research.runs import start_research_run
from lab.runner import CandleLoader, default_loader

SEED_DIR = AGENTS_DIR / "seeds"
SEED_KEYS: tuple[str, ...] = ("name", "source_url", "translation_notes", "plan")

EXPECTATION = (
    "Popular published strategies are expected to FAIL honest tests. This run calibrates the "
    "pipeline (if well-known strategies passed easily, the tests would be suspect) and seeds a "
    "library of known failures for the research agents."
)
DISCLAIMER = (
    "Results are historical simulations on the design window only; they are not "
    "forward-looking. Calibration never calls the holdout check and records no holdout data."
)


@dataclass(frozen=True)
class Seed:
    """One translated strategy from ``agents/seeds/*.json``."""

    name: str
    source_url: str
    translation_notes: str
    plan: dict[str, Any]


@dataclass(frozen=True)
class SeedOutcome:
    """What happened to one seed. ``error`` is set when any step failed (the run went on)."""

    seed: Seed
    plan_hash: str
    backtest_trial_id: int | None
    criteria: Mapping[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class CalibrationResult:
    run_id: str
    outcomes: list[SeedOutcome]
    report_path: Path


# --------------------------------------------------------------------------- loading


def _load_seed(path: Path) -> Seed:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"seed file {path.name}: cannot read JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"seed file {path.name}: top level must be an object with {SEED_KEYS}")
    missing = [k for k in SEED_KEYS if k not in raw]
    extra = sorted(set(raw) - set(SEED_KEYS))
    if missing or extra:
        raise ValueError(f"seed file {path.name}: missing keys {missing}, unknown keys {extra}")
    for key in ("name", "source_url", "translation_notes"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"seed file {path.name}: {key!r} must be a non-empty string")
    if not isinstance(raw["plan"], dict):
        raise ValueError(f"seed file {path.name}: 'plan' must be an object")
    plan, errors = validate_plan(raw["plan"])
    if plan is None:
        lines = "; ".join(f"{e.path}: {e.message}" for e in errors)
        raise ValueError(f"seed file {path.name}: invalid plan: {lines}")
    return Seed(
        name=raw["name"],
        source_url=raw["source_url"],
        translation_notes=raw["translation_notes"],
        plan=raw["plan"],
    )


def load_seeds(seed_dir: Path = SEED_DIR) -> list[Seed]:
    """Every ``*.json`` seed in ``seed_dir`` (sorted by file name), each validated.

    Raises ``ValueError`` naming the file on the first malformed or invalid seed, and when the
    directory holds no seeds at all.
    """
    paths = sorted(Path(seed_dir).glob("*.json"))
    if not paths:
        raise ValueError(f"no seed files (*.json) in {seed_dir}")
    return [_load_seed(p) for p in paths]


# --------------------------------------------------------------------------- calibration


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _run_one(
    seed: Seed,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str,
    loader: CandleLoader | None,
) -> SeedOutcome:
    from lab.research.criteria import evaluate_plan  # written alongside; imported lazily

    phash = plan_hash(parse_plan(seed.plan))
    load = loader or default_loader(cfg)
    errors: list[str] = []
    trial_id: int | None = None
    recorded = False
    try:
        # Same evidence path as the MCP server: stripped report + frozen-criteria regimes.
        report = backtest_evidence(seed.plan, run_id=run_id, cfg=cfg, ledger=ledger, loader=load)
        trial_id = int(report["trial_id"]) if report.get("trial_id") is not None else None
        recorded = True
    except Exception as exc:  # per-seed failures are reported, never raised
        errors.append(f"backtest: {_error_text(exc)}")
    if recorded:
        try:
            adversarial_evidence(seed.plan, run_id=run_id, cfg=cfg, ledger=ledger, loader=load)
        except Exception as exc:
            errors.append(f"adversarial: {_error_text(exc)}")
    criteria: Mapping[str, Any] | None = None
    if recorded:
        try:
            criteria = evaluate_plan(ledger, run_id, phash, cfg)
        except Exception as exc:
            errors.append(f"criteria: {_error_text(exc)}")
    return SeedOutcome(
        seed=seed,
        plan_hash=phash,
        backtest_trial_id=trial_id,
        criteria=criteria,
        error="; ".join(errors) or None,
    )


def run_seed_calibration(
    *,
    cfg: LabConfig,
    ledger: Ledger,
    research: ResearchConfig,
    loader: CandleLoader | None = None,
    seeds: Sequence[Seed] | None = None,
    journal_dir: Path = JOURNAL_DIR,
    label: str = "seed-calibration",
    now: datetime | None = None,
) -> CalibrationResult:
    """Run every seed through the honest pipeline under a new research run and write the
    calibration report (module docstring). Per-seed errors are captured in the outcomes."""
    seed_list = list(seeds) if seeds is not None else load_seeds()
    # cfg fingerprints the lab config into the run; evaluators refuse runs without it.
    run_id = start_research_run(ledger, research, label=label, cfg=cfg, now=now)
    outcomes = [
        _run_one(s, cfg=cfg, ledger=ledger, run_id=run_id, loader=loader) for s in seed_list
    ]
    path = write_calibration_report(
        run_id, outcomes, journal_dir=journal_dir, now=now, research=research
    )
    return CalibrationResult(run_id=run_id, outcomes=outcomes, report_path=path)


# --------------------------------------------------------------------------- report


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _overall(outcome: SeedOutcome) -> str:
    c = outcome.criteria
    if c is None:
        return "error" if outcome.error else "missing"
    if c.get("pre_holdout_passed"):
        return "passed (pre-holdout)"
    return "failed"


def _criteria_columns(outcomes: Sequence[SeedOutcome]) -> list[str]:
    cols: list[str] = []
    for o in outcomes:
        for row in (o.criteria or {}).get("rows", []) or []:
            name = str(row.get("criterion", ""))
            if name and name not in cols:
                cols.append(name)
    return cols


def _status_by_criterion(outcome: SeedOutcome) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in (outcome.criteria or {}).get("rows", []) or []:
        out[str(row.get("criterion", ""))] = str(row.get("status", "missing"))
    return out


def _report_path(journal_dir: Path, date: str, run_id: str) -> Path:
    path = journal_dir / f"{date}-seed-calibration.md"
    if path.exists():  # a second calibration on the same day never overwrites the first
        path = journal_dir / f"{date}-seed-calibration-{run_id}.md"
    return path


def render_calibration_report(
    run_id: str,
    outcomes: Sequence[SeedOutcome],
    *,
    date: str,
    research: ResearchConfig | None = None,
) -> str:
    """Obsidian-friendly Markdown: YAML frontmatter, expectation, seeds x criteria table,
    per-seed details, disclaimer. Holdout results are never part of it."""
    n_pass = sum(1 for o in outcomes if (o.criteria or {}).get("pre_holdout_passed"))
    n_err = sum(1 for o in outcomes if o.error)
    lines = [
        "---",
        f"run_id: {run_id}",
        f"date: {date}",
        "outcome: calibration",
        "tags: [research, seed-calibration, freqtrade, calibration]",
        f"seeds: {len(outcomes)}",
        f"pre_holdout_passed: {n_pass}",
        "---",
        "",
        f"# Seed calibration {date}",
        "",
        f"Ledger run: [[{run_id}]]",
        "",
        "## Expectation (stated before the results)",
        "",
        EXPECTATION,
        "",
        f"> {DISCLAIMER}",
        "",
    ]
    if research is not None:
        lines += [f"Success criteria file sha256: `{research.sha256}` (frozen into the run).", ""]
    ledger_ns = [o.criteria.get("ledger_n") for o in outcomes if o.criteria is not None]
    ledger_ns = [n for n in ledger_ns if n is not None]
    if ledger_ns:
        lines += [
            "Ledger size used for DSR (distinct plans across every research run): "
            f"{max(ledger_ns)}",
            "",
        ]
    lines += [
        "## Results",
        "",
        f"{len(outcomes)} seeds, {n_pass} passed every pre-holdout criterion, {n_err} with "
        "errors. `pass` / `fail` / `missing` per criterion as reported by the criteria "
        "evaluator; holdout and cross-check criteria are not evaluated here.",
        "",
    ]
    cols = _criteria_columns(outcomes)
    header = ["Seed", "Overall", *cols]
    lines.append("| " + " | ".join(_cell(h) for h in header) + " |")
    lines.append("|" + "|".join(" --- " for _ in header) + "|")
    for o in outcomes:
        status = _status_by_criterion(o)
        row = [o.seed.name, _overall(o), *(status.get(c, "missing") for c in cols)]
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    lines += ["", "## Seeds", ""]
    for o in outcomes:
        lines += [
            f"### {o.seed.name}",
            "",
            f"- Source: {o.seed.source_url}",
            f"- Plan hash: `{o.plan_hash}`",
            f"- Backtest trial: {o.backtest_trial_id if o.backtest_trial_id is not None else '-'}",
            f"- Overall: {_overall(o)}",
        ]
        if o.criteria is not None and o.criteria.get("missing"):
            lines.append(f"- Missing evidence: {_cell(', '.join(map(str, o.criteria['missing'])))}")
        if o.error:
            lines.append(f"- Error: {_cell(o.error)}")
        lines += [f"- Translation notes: {_cell(o.seed.translation_notes)}", ""]
        rows = (o.criteria or {}).get("rows", []) or []
        if rows:
            lines += [
                "| Criterion | Status | Value | Threshold | Note |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in rows:
                lines.append(
                    "| "
                    + " | ".join(
                        _cell("" if r.get(k) is None else r.get(k))
                        for k in ("criterion", "status", "value", "threshold", "note")
                    )
                    + " |"
                )
            lines.append("")
    lines += ["---", "", DISCLAIMER, ""]
    return "\n".join(lines)


def write_calibration_report(
    run_id: str,
    outcomes: Sequence[SeedOutcome],
    *,
    journal_dir: Path = JOURNAL_DIR,
    now: datetime | None = None,
    research: ResearchConfig | None = None,
) -> Path:
    """Write :func:`render_calibration_report` to ``journal_dir/<date>-seed-calibration.md``
    (temp file + rename) and return the path."""
    date = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = _report_path(journal_dir, date, run_id)
    text = render_calibration_report(run_id, outcomes, date=date, research=research)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


__all__ = [
    "DISCLAIMER",
    "EXPECTATION",
    "SEED_DIR",
    "CalibrationResult",
    "Seed",
    "SeedOutcome",
    "load_seeds",
    "render_calibration_report",
    "run_seed_calibration",
    "write_calibration_report",
]
