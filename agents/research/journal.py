"""The research journal: one Obsidian-friendly Markdown file per run (PLAN.md Phase 8, step 7).

Layout of ``<journal_dir>/<YYYY-MM-DD>-<symbols>-<slug>.md``::

    ---                      YAML frontmatter: run_id, date, symbols, interval, outcome,
    ...                      idea_ids, criteria_sha256, tags
    ---
    # <title>
    ## Success criteria and budget (fixed before any backtest)   <- written at create time
    ## Log                                                        <- timestamped bullets
    ## End-of-run report                                          <- appended by finish()

Rules enforced here:

* **Holdout results are never recorded.** Every public write path runs :func:`guard_holdout`,
  which raises :class:`HoldoutLeakError` when text mentions the holdout near a metric name or
  a decimal / percentage number. :meth:`Journal.log_holdout_spent` writes only that a check
  was spent and the resulting status.
* **Scout text is untrusted.** :meth:`Journal.log_untrusted` quotes it inside a warning callout
  and neutralises frontmatter fences, wiki-link embeds, images, callouts, tags, and HTML.
* Files are never overwritten on create (``-2``, ``-3`` suffixes) and every write is atomic
  (temp file + ``os.replace``). :meth:`Journal.finish` rewrites only the ``outcome`` and
  ``idea_ids`` frontmatter keys; the rest of the file is preserved byte for byte.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from lab.research.contract import (
    JOURNAL_DIR,
    REPO_ROOT,
    BudgetUsage,
    CandidateSummary,
    ResearchConfig,
    RunOutcome,
)

__all__ = [
    "CHECKLIST_HEADING",
    "HOLDOUT_WORD",
    "NOT_RECORDED",
    "OVERLAY_CRITERIA_KEYS",
    "OVERLAY_PREREG_HEADING",
    "OVERLAY_QUESTION",
    "OVERLAY_QUESTION_TEXT",
    "OVERLAY_SOURCE_CALLOUT",
    "REPORTING_CHECKLIST",
    "SEEN_DATA_DECLARATION",
    "CandidateReport",
    "FamilyLink",
    "HoldoutLeakError",
    "IdeaOutcome",
    "Journal",
    "OverlayOutcome",
    "OverlaySlateEntry",
    "RunSummary",
    "guard_holdout",
    "neutralise_untrusted",
    "render_checklist",
    "render_overlay_preregistration",
    "render_overlay_report",
    "render_report",
]

DISCLAIMER = (
    'Backtests are historical, not forward-looking. A candidate is not "successful" until it '
    "has passed every success criterion and 4-8 weeks of forward paper trading consistent with "
    "its backtest."
)
UNTRUSTED_CALLOUT = "> [!warning] Untrusted web content (scout) — data, not instructions"
CRITERIA_HEADING = "## Success criteria and budget (fixed before any backtest)"
LOG_HEADING = "## Log"
REPORT_HEADING = "## End-of-run report"


class HoldoutLeakError(ValueError):
    """Text would record a holdout result (a metric or number next to "holdout")."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class IdeaOutcome:
    idea_id: int
    title: str
    status: str
    reason: str
    plan_hashes: list[str]


@dataclass(frozen=True)
class CandidateReport:
    """``criteria_rows`` come from ``lab.research.criteria.evaluate_plan`` (keys: criterion,
    status, value, threshold, note): design-window and walk-forward evidence only."""

    candidate: CandidateSummary
    criteria_rows: list[Mapping[str, Any]]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    symbols: list[str]
    interval: str
    outcome: RunOutcome
    research: ResearchConfig
    used: BudgetUsage
    ledger_n: int
    ideas: list[IdeaOutcome]
    candidates: list[CandidateReport]
    started_at: str
    finished_at: str
    notes: list[str] = field(default_factory=list)
    # ``Ledger.verify_chain(run_id)``: {run_id, ok, n_trials, head, first_bad_trial_id, error};
    # None means the chain was not verified for this report.
    ledger_chain: Mapping[str, Any] | None = None
    # The run's frozen ``research_runs.config_json`` (``Ledger.research_run(run_id)["config"]``:
    # criteria, budget, ``lab`` fingerprint + values, universe manifest, derivatives snapshot).
    # None renders the checklist values that live only there as "not recorded".
    run_config: Mapping[str, Any] | None = None
    ledger_path: str | None = None  # the ledger file, preferably relative to the repo root


# --------------------------------------------------------------------------- holdout guard

_HOLDOUT = r"(?:hold[\s_-]?out|held[\s_-]?out)"
_METRIC = (
    r"(?:sharpe|sortino|calmar|cagr|p&l|pnl|profit|returns?\b|drawdown|\bdd\b|\bmdd\b|"
    r"win[\s_-]?rate|expectancy|\bdsr\b|\bpbo\b|\bbps\b|"
    r"[-+]?\d*\.\d+|[-+]?\d+(?:\.\d+)?\s*%)"
)
_WINDOW = 80
HOLDOUT_WORD = re.compile(_HOLDOUT, re.IGNORECASE)  # the guard's spelling of the holdout
_LEAK = re.compile(
    rf"{_HOLDOUT}.{{0,{_WINDOW}}}?{_METRIC}|{_METRIC}.{{0,{_WINDOW}}}?{_HOLDOUT}",
    re.IGNORECASE | re.DOTALL,
)


def guard_holdout(text: str, *, per_line: bool = False) -> None:
    """Raise :class:`HoldoutLeakError` if ``text`` mentions the holdout within ~80 characters
    of a metric name or a decimal / percentage number. With ``per_line`` each line is checked
    on its own (used for rendered tables, whose neighbouring rows are unrelated)."""
    chunks = text.splitlines() if per_line else [" ".join(text.split())]
    for chunk in chunks:
        match = _LEAK.search(chunk)
        if match:
            raise HoldoutLeakError(
                "refusing to journal a holdout result (holdout metrics are never recorded): "
                f"{match.group(0)!r}"
            )


# --------------------------------------------------------------------------- text helpers

_SLUG = re.compile(r"[^0-9a-z]+")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PLAN_HASH = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_IDEA_LINK = re.compile(r"\[\[idea-(\d+)\]\]")
_THEMATIC_BREAK = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_SETEXT = re.compile(r"^\s*=+\s*$")


def _slug(text: str, max_len: int = 60) -> str:
    slug = _SLUG.sub("-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "run"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _escape_links(text: str) -> str:
    """Stop text from creating wiki links, embeds, images, callouts, or tags."""
    text = text.replace("[[", r"\[\[").replace("]]", r"\]\]")
    text = text.replace("![", r"!\[").replace("[!", r"\[!")
    return re.sub(r"(^|\s)#(?=\w)", r"\1\\#", text)


def neutralise_untrusted(text: str) -> list[str]:
    """Make scraped text inert Markdown: HTML escaped, no frontmatter fences or rules, no
    wiki links / embeds / images / nested callouts / tags. Returns the lines (unprefixed)."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = html.escape(raw, quote=False)
        line = _escape_links(line)
        if _THEMATIC_BREAK.match(line) or _SETEXT.match(line):
            line = "".join("\\" + ch if ch in "-*_=" else ch for ch in line)
        elif line.lstrip().startswith("#"):
            line = line.replace("#", "\\#", 1)
        lines.append(line)
    return lines


def _inline(text: str) -> str:
    """Trusted free text inside a bullet: continuation lines indented, links escaped."""
    lines = [_escape_links(html.escape(x, quote=False)) for x in text.strip().splitlines()]
    return "\n  ".join(lines) if lines else ""


def _cell(value: object) -> str:
    """A Markdown table cell: no pipes, newlines, HTML, or wiki links."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4g}"
    text = " ".join(str(value).split())
    return _escape_links(html.escape(text, quote=False)).replace("|", r"\|") or "—"


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML scalars


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _create_exclusive(path: Path, data: bytes) -> None:
    """Atomically create ``path``; FileExistsError if it already exists (never overwrites)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)  # fails if path exists, unlike os.replace
    finally:
        Path(tmp).unlink(missing_ok=True)


# --------------------------------------------------------------------------- frontmatter


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter block including both fences, rest of the file)."""
    if not text.startswith("---\n"):
        raise ValueError("journal file has no YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("journal frontmatter is not closed")
    cut = end + len("\n---\n")
    return text[:cut], text[cut:]


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"'):
        return json.loads(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_scalar(x) for x in inner.split(",")] if inner else []
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the small YAML subset this module writes (scalars, flow and block lists)."""
    block, _ = _split_frontmatter(text)
    data: dict[str, Any] = {}
    key: str | None = None
    for line in block.split("\n")[1:-2]:
        if line.startswith("  - ") and key is not None:
            data[key].append(_parse_scalar(line[4:]))
            continue
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        key = name.strip()
        data[key] = [] if not rest.strip() else _parse_scalar(rest)
    return data


def _block_list(key: str, values: Sequence[object]) -> str:
    if not values:
        return f"{key}: []\n"
    items = "".join(
        f"  - {v}\n" if isinstance(v, int) else f"  - {_yaml_str(str(v))}\n" for v in values
    )
    return f"{key}:\n{items}"


def _replace_key(block: str, key: str, rendered: str) -> str:
    """Replace one top-level key (and its block-list items) inside a frontmatter block."""
    pattern = re.compile(rf"^{re.escape(key)}:.*\n(?:  - .*\n)*", re.MULTILINE)
    if pattern.search(block):
        return pattern.sub(lambda _: rendered, block, count=1)
    return block[: -len("---\n")] + rendered + "---\n"


# --------------------------------------------------------------------------- rendering

_CRITERIA_LABELS: dict[str, str] = {
    "wf_oos_sharpe_min": "Walk-forward OOS Sharpe after costs (365-day), at least",
    "dsr_min": "Deflated Sharpe Ratio (full trial count), at least",
    "pbo_max": "Probability of backtest overfitting, at most",
    "beat_buy_hold": "Beats buy-and-hold (risk-adjusted)",
    "beat_vol_target": "Beats volatility-targeted buy-and-hold (risk-adjusted)",
    "random_entry_percentile_min": "Percentile vs the random-entry null, at least",
    "cost_stress_factor": "Net result stays positive at this cost multiple",
    "require_gradual_delay_decay": "Edge decays gradually under signal delay (no cliff)",
    "max_drawdown_max": "Max drawdown (fraction), at most",
    "min_trades": "Trades, at least",
    "min_profitable_regimes": "Profitable regimes of 3 (unless regime-gated), at least",
    "regime_window_days": "Regime label window (trailing days)",
    "regime_threshold": "Regime threshold (± simple return over the window)",
    "neighbourhood_pct": "Parameter neighbourhood (±%) that must stay profitable",
    "require_holdout_pass": "Passes the locked holdout",
    "require_crosscheck_match": "Matches the event-driven cross-check engine",
}
_BUDGET_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Hypotheses", "hypotheses", "max_hypotheses"),
    ("LLM proposals", "proposals", "max_proposals"),
    ("Backtests (ledger trials)", "backtests", "max_backtests"),
    ("Holdout checks", "holdout_checks", "max_holdout_checks"),
    ("Model spend (USD)", "usd", "max_usd"),
)


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "required" if value else "not required"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render_research(research: ResearchConfig) -> str:
    lines = [
        f"Criteria file sha256: `{research.sha256}`. These values were fixed before the first "
        "backtest of this run and are never relaxed mid-run.",
        "",
        "| criterion | key | value |",
        "|---|---|---|",
    ]
    for key, value in asdict(research.criteria).items():
        lines.append(f"| {_CRITERIA_LABELS.get(key, key)} | `{key}` | {_fmt(value)} |")
    lines += ["", "| budget | key | limit |", "|---|---|---|"]
    budget = asdict(research.budget)
    for label, _used_key, limit_key in _BUDGET_ROWS:
        lines.append(f"| {label} | `{limit_key}` | {_fmt(budget[limit_key])} |")
    return "\n".join(lines) + "\n"


_OUTCOME_TEXT: dict[str, str] = {
    "candidate": (
        "**Candidate found.** {n} plan(s) passed every success criterion on the design window "
        "and walk-forward, the locked holdout check, and the event-driven cross-check. A "
        'candidate is not yet "successful": it needs 4-8 weeks of forward paper trading with '
        "results consistent with its backtest."
    ),
    "no_edge": (
        "**No edge found.** No hypothesis tested in this run passed every success criterion. "
        "This is a correct, complete outcome: the loop has a fixed bar, a fixed budget, and an "
        'honest exit, and a run that ends with "no edge found" has worked correctly. The '
        "criteria and budget were not relaxed."
    ),
    "budget_exhausted": (
        "**Budget exhausted — no edge found within this budget.** The run hit a fixed limit "
        "before any plan passed every success criterion. This is a correct outcome, not a "
        "failure; the budget and criteria were not extended or relaxed to keep searching."
    ),
    "error": (
        "**Run ended with an error.** The run did not complete, so these results are partial "
        'and support no conclusion either way (neither a candidate nor "no edge").'
    ),
    "stopped": (
        "**Run stopped before completion.** These results are partial and support no "
        'conclusion either way (neither a candidate nor "no edge").'
    ),
}
_FAILED_STATUSES = frozenset({"dead", "abandoned", "duplicate"})


def _criteria_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out = ["| criterion | status | value | threshold | note |", "|---|---|---|---|---|"]
    for row in rows:
        criterion = str(row.get("criterion", ""))
        if re.search(_HOLDOUT, criterion, re.IGNORECASE):
            # Holdout evidence is never journaled: status only, no numbers.
            out.append(
                f"| {_cell(criterion)} | {_cell(row.get('status'))} | — | — | "
                "holdout metrics are never recorded in the journal |"
            )
            continue
        out.append(
            f"| {_cell(criterion)} | {_cell(row.get('status'))} | {_cell(row.get('value'))} | "
            f"{_cell(row.get('threshold'))} | {_cell(row.get('note'))} |"
        )
    return out


# --------------------------------------------------------------------------- reporting checklist
#
# The "Agentic Trading" survey (arXiv 2605.19337) found few closed-loop studies report a
# time-consistent split or a cost model and proposes this seven-item checklist. Every line
# states the fact and names the ledger / run fields that prove it. Values come only from the
# RunSummary (never invented): a missing one renders as NOT_RECORDED.

REPORTING_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("data_universe", "Data and universe"),
    ("time_split", "Time split and leakage"),
    ("action_semantics", "Action semantics"),
    ("execution_costs", "Execution and costs"),
    ("leakage_audit", "Leakage audit"),
    ("artifacts", "Artifacts"),
    ("adaptation", "Adaptation"),
)
CHECKLIST_HEADING = "### Reporting checklist"
NOT_RECORDED = "not recorded"
_CFG = "research_runs.config_json"
_LAB = f"{_CFG}.lab.values"
_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(?:\.0+)?(?:Z|\+00:00)?$")
_INVALID_NOTE = re.compile(r"invalid (?:plan|output)", re.IGNORECASE)


def _dig(data: Mapping[str, Any] | None, path: str) -> Any:
    """``data["a"]["b"]...`` for ``path`` "a.b...", or None when any step is missing."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _show(value: object) -> str:
    """One inline value: NOT_RECORDED when missing, single line, no links or HTML."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return NOT_RECORDED
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list | tuple):
        return ", ".join(_show(v) for v in value) if value else NOT_RECORDED
    text = " ".join(str(value).split())
    midnight = _MIDNIGHT.match(text)
    if midnight:
        text = midnight.group(1)
    return _escape_links(html.escape(text, quote=False)).replace("`", "'")


def _code(value: object) -> str:
    """A value in backticks, or NOT_RECORDED (never backticked, so it reads as missing)."""
    text = _show(value)
    return text if text == NOT_RECORDED else f"`{text}`"


def _pct(value: object) -> str:
    """A fraction with its percentage, e.g. ``0.001 (0.1%)``."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:g} ({value * 100:g}%)"
    return NOT_RECORDED


def _fields(*names: str) -> str:
    return "fields: " + ", ".join(f"`{n}`" for n in names)


def _repo_relative(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return p.name  # never write a home-directory path into a committed file


def _chain_status(chain: Mapping[str, Any] | None) -> str:
    """Integers and hex only: this text sits on a line that names the holdout guard."""
    if chain is None:
        return "ledger hash chain not verified for this report"
    if chain.get("ok") is True:
        return (
            f"ledger hash chain verified over {_show(chain.get('n_trials'))} trial(s), head "
            f"{_code(chain.get('head'))}"
        )
    bad = chain.get("first_bad_trial_id")
    where = f"trial {int(bad)}" if isinstance(bad, int) else f"trial {_show(bad)}"
    return f"**CHAIN BROKEN at {where}**: the ledger was altered after it was written"


def _chain_error(chain: Mapping[str, Any] | None) -> str | None:
    """The verifier's error text for the Artifacts line, withheld if the leak guard trips."""
    if chain is None or chain.get("ok") is True or not chain.get("error"):
        return None
    text = _show(chain.get("error"))[:200]
    return "withheld by the journal leak guard" if _LEAK.search(text) else text


def _checklist_lines(s: RunSummary, journal_path: str | Path | None) -> dict[str, str]:
    cfg = s.run_config
    budget = asdict(s.research.budget)
    used: Mapping[str, Any] = s.used
    chain = s.ledger_chain

    # Data and universe
    universe = _dig(cfg, "universe_manifest.sha256")
    derivs = _dig(cfg, "derivatives.manifest_sha256")
    series = _dig(cfg, "derivatives.series")
    rule = [_dig(cfg, f"lab.values.universe.{k}") for k in ("top_n", "quote", "volume_window_days")]
    rule_txt = (
        f"point-in-time top {_show(rule[0])} {_show(rule[1])} coins by trailing "
        f"{_show(rule[2])}-day dollar volume"
        if all(v is not None for v in rule)
        else f"selection rule {NOT_RECORDED}"
    )
    n_series = f"{len(series)} series" if isinstance(series, Mapping) else f"series {NOT_RECORDED}"
    data = (
        f"{', '.join(_show(x) for x in s.symbols)} on {_show(s.interval)} bars; multi-coin "
        f"universe: {rule_txt}, manifest sha256 {_code(universe)}; derivatives snapshot "
        f"manifest sha256 {_code(derivs)} ({n_series} frozen) — "
        + _fields(
            "RunSummary.symbols",
            "RunSummary.interval",
            "trials.symbol",
            "trials.interval",
            f"{_CFG}.universe_manifest.sha256",
            f"{_LAB}.universe",
            f"{_CFG}.derivatives.manifest_sha256",
        )
    )

    # Time split and leakage: dates and integers only (the line names the holdout).
    design_start = _dig(cfg, "lab.values.splits.design_start")
    holdout_start = _dig(cfg, "lab.values.splits.holdout_start")
    anchored = _dig(cfg, "lab.values.splits.wf_anchored")
    wf_kind = NOT_RECORDED if anchored is None else ("anchored" if anchored else "rolling")
    time_split = (
        f"design window from {_code(design_start)} up to {_code(holdout_start)}; the holdout "
        f"from {_code(holdout_start)} on is locked, and only that a check was spent is recorded "
        f"here (holdout checks used {_show(used.get('holdout_checks'))} of "
        f"{_show(budget.get('max_holdout_checks'))}); walk-forward {wf_kind}, train "
        f"{_show(_dig(cfg, 'lab.values.splits.wf_train_months'))} / test "
        f"{_show(_dig(cfg, 'lab.values.splits.wf_test_months'))} / step "
        f"{_show(_dig(cfg, 'lab.values.splits.wf_step_months'))} months — "
        + _fields(
            f"{_LAB}.splits.design_start",
            f"{_LAB}.splits.holdout_start",
            f"{_LAB}.splits.wf_train_months",
            f"{_LAB}.splits.wf_test_months",
            "trials.window_kind",
            "trials.window_start",
            "trials.window_end",
            "holdout_checks",
        )
    )

    # Action semantics: an engine rule, not a config value.
    action = (
        "spot, long-only, no leverage or shorting: single-symbol plans are long/flat "
        "(`long_entry` / `long_exit`), portfolio plans hold a long-only top quantile; a signal "
        "uses bar t's close and fills at bar t+1's open (engine rule, not a config field) — "
        + _fields("trials.plan_json", "trials.params_json", "trials.n_trades")
    )

    # Execution and costs
    stress = _dig(cfg, "lab.values.stress_factors")
    stress_txt = (
        ", ".join(f"x{_show(x)}" for x in stress)
        if isinstance(stress, list) and stress
        else NOT_RECORDED
    )
    impact = _dig(cfg, "lab.values.universe.portfolio.impact_coef")
    costs = (
        f"fee {_pct(_dig(cfg, 'lab.values.costs.fee_rate'))} per side; slippage "
        f"{_pct(_dig(cfg, 'lab.values.costs.slippage_rate'))} per side, widened by "
        f"{_show(_dig(cfg, 'lab.values.costs.slippage_vol_mult'))} x excess bar range, capped at "
        f"{_pct(_dig(cfg, 'lab.values.costs.slippage_cap'))}; cost stress {stress_txt}; "
        f"portfolio square-root impact coefficient {_show(impact)} — "
        + _fields(
            f"{_LAB}.costs",
            f"{_LAB}.stress_factors",
            f"{_LAB}.universe.portfolio.impact_coef",
            "trials.costs_json",
        )
    )

    # Leakage audit: integers and hex only (the line names the holdout guard).
    leakage = (
        "every registry feature ships a perturbation test (changing future bars must not change "
        "past signals) in the lab test suite; the journal refuses any metric next to the "
        f"holdout and keeps only spent checks; {_chain_status(chain)} — "
        + _fields(
            "ledger_chain.ok",
            "ledger_chain.n_trials",
            "ledger_chain.head",
            "ledger_chain.first_bad_trial_id",
            "holdout_checks",
        )
    )

    # Artifacts
    error = _chain_error(chain)
    artifacts = (
        f"run {_code(s.run_id)}; ledger {_code(_repo_relative(s.ledger_path))}; chain head "
        f"{_code(_dig(chain, 'head'))}"
        + (f" (verifier error: {error})" if error else "")
        + f"; journal {_code(_repo_relative(journal_path))}; criteria sha256 "
        f"{_code(s.research.sha256)}; lab.toml fingerprint "
        f"{_code(_dig(cfg, 'lab.fingerprint'))} — "
        + _fields(
            "research_runs.run_id",
            "research_runs.config_sha256",
            f"{_CFG}.lab.fingerprint",
            "ledger_chain.head",
            "RunState.journal_path",
        )
    )

    # Adaptation: no holdout wording on this line (it carries the decimal spend).
    plans = sum(len(i.plan_hashes) for i in s.ideas)
    redesigns = sum(max(0, len(i.plan_hashes) - 1) for i in s.ideas)
    invalid_keys = sorted(k for k in used if "invalid" in str(k))
    invalid_used = sum(int(used[k] or 0) for k in invalid_keys)
    invalid_notes = sum(1 for n in s.notes if _INVALID_NOTE.search(n))
    invalid_fields = ", ".join(f"`used.{k}`" for k in invalid_keys)
    invalid_txt = (
        f"{invalid_used} invalid designer output(s) ({invalid_fields})"
        if invalid_keys
        else f"{invalid_notes} invalid designer output(s) named in the notes"
    )
    usd_used, usd_max = used.get("usd"), budget.get("max_usd")
    adaptation = (
        f"{len(s.ideas)} idea(s), {plans} plan(s) designed ({redesigns} beyond each idea's "
        f"first); {invalid_txt}; hypotheses {_show(used.get('hypotheses'))} of "
        f"{_show(budget.get('max_hypotheses'))}, LLM proposals {_show(used.get('proposals'))} of "
        f"{_show(budget.get('max_proposals'))}, backtests {_show(used.get('backtests'))} of "
        f"{_show(budget.get('max_backtests'))}, model spend USD "
        f"{f'{usd_used:.2f}' if isinstance(usd_used, int | float) else NOT_RECORDED} of "
        f"{f'{usd_max:.2f}' if isinstance(usd_max, int | float) else NOT_RECORDED}; DSR N "
        f"{_show(s.ledger_n)} across every research run; Python drives the loop and the "
        "criteria and budget were frozen at start_run — "
        + _fields(
            "ideas.plan_hashes",
            "RunState.used",
            f"{_CFG}.budget",
            "research_runs.config_sha256",
            "trials.plan_hash",
        )
    )
    return {
        "data_universe": data,
        "time_split": time_split,
        "action_semantics": action,
        "execution_costs": costs,
        "leakage_audit": leakage,
        "artifacts": artifacts,
        "adaptation": adaptation,
    }


def render_checklist(summary: RunSummary, *, journal_path: str | Path | None = None) -> str:
    """The reporting checklist block: one bullet per :data:`REPORTING_CHECKLIST` item, each a
    single line (the leak guard checks rendered reports line by line)."""
    lines = _checklist_lines(summary, journal_path)
    out = [
        CHECKLIST_HEADING,
        "",
        "Each line states what this run did and names the ledger / run fields that prove it. "
        f'"{NOT_RECORDED}" means the value was not passed to the journal; nothing is inferred.',
        "",
    ]
    out += [f"- **{title}**: {lines[key]}" for key, title in REPORTING_CHECKLIST]
    return "\n".join(out)


def render_report(summary: RunSummary, *, journal_path: str | Path | None = None) -> str:
    """The end-of-run report (Markdown, starting with ``## End-of-run report``). Its first
    section is the reporting checklist (:func:`render_checklist`)."""
    if summary.outcome not in _OUTCOME_TEXT:
        raise ValueError(f"cannot report outcome {summary.outcome!r}; finish the run first")
    s = summary
    out: list[str] = [
        REPORT_HEADING,
        "",
        f"Run [[run-{s.run_id}]] · {', '.join(s.symbols)} {s.interval} · "
        f"started {s.started_at} · finished {s.finished_at}",
        "",
        render_checklist(s, journal_path=journal_path),
        "",
        "### Outcome",
        "",
        _OUTCOME_TEXT[s.outcome].format(n=len(s.candidates)),
        "",
        "### Success criteria and budget",
        "",
        _render_research(s.research),
        "### Budget used vs limits",
        "",
        "| resource | used | limit | |",
        "|---|---|---|---|",
    ]
    budget = asdict(s.research.budget)
    used: Mapping[str, Any] = s.used
    for label, used_key, limit_key in _BUDGET_ROWS:
        u, lim = used.get(used_key, 0), budget[limit_key]
        flag = "over limit" if u > lim else ("limit reached" if u >= lim else "")
        u_txt = f"{u:.2f}" if used_key == "usd" else str(u)
        lim_txt = f"{lim:.2f}" if used_key == "usd" else str(lim)
        out.append(f"| {label} | {u_txt} | {lim_txt} | {flag} |")
    out += [
        "",
        "### Ledger size used for DSR",
        "",
        f"N = {s.ledger_n}: distinct plans tried across every research run in the ledger "
        f"(this run is [[run-{s.run_id}]]), Optuna and neighbourhood variants included. Every "
        "Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.",
        "",
        "### Ideas tried",
        "",
    ]
    if s.ideas:
        out += [
            "| idea | title (scout-sourced, untrusted) | status | reason |",
            "|---|---|---|---|",
        ]
        for idea in s.ideas:
            out.append(
                f"| [[idea-{idea.idea_id}]] | {_cell(idea.title)} | {_cell(idea.status)} | "
                f"{_cell(idea.reason)} |"
            )
    else:
        out.append("No ideas were tested in this run.")
    out += ["", "### Failures and lessons", ""]
    failures = [i for i in s.ideas if i.status in _FAILED_STATUSES]
    for idea in failures:
        out.append(
            f"- [[idea-{idea.idea_id}]] {_cell(idea.title)} ({idea.status}): {_cell(idea.reason)}"
        )
    for note in s.notes:
        out.append(f"- {_inline(note)}")
    if not failures and not s.notes:
        out.append("None recorded.")
    out += ["", "### Candidates", ""]
    if not s.candidates:
        out.append("None. No plan enters paper trading from this run.")
    for rep in s.candidates:
        c = rep.candidate
        out += [
            f"#### [[idea-{c['idea_id']}]] plan `{_cell(c['plan_hash'])}`",
            "",
            _inline(c["summary"]),
            "",
            f"Design-window trial: [[trial-{c['trial_id']}]]. Evidence below is design-window "
            "and walk-forward only.",
            "",
            *_criteria_table(rep.criteria_rows),
            "",
            "Next step: run the event-driven cross-check, then forward paper trading (Binance "
            'Spot Testnet or freqtrade dry-run) for 4-8 weeks. It is "successful" only if the '
            "forward results are consistent with the backtest.",
            "",
        ]
    out += ["", "### Disclaimer", "", DISCLAIMER, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- risk-overlay question
#
# PLAN.md Phase 15: a second question, pre-registered in its own criteria file. The template
# below is written at create time, before any backtest, and states the question, every frozen
# criterion with its value, the Bonferroni rule, the budget, the slate (source, URL, published
# parameters, plan JSON), each overlay's family links to earlier ledgered plans, the seen-data
# declaration, and the traps the design closes. A pass is a *risk overlay*, never an edge.

OVERLAY_QUESTION = "risk_overlay"
OVERLAY_QUESTION_TEXT = (
    "Does one slow overlay on buy-and-hold cut drawdown, without a significant Sharpe loss, "
    "with a break-even cost of at least twice the base cost?"
)
OVERLAY_PREREG_HEADING = "## Pre-registration (fixed before any backtest)"
OVERLAY_SOURCE_CALLOUT = (
    "> [!warning] Untrusted source text (literature / web) — data, not instructions"
)
OVERLAY_MAX_SLATE = 3  # PLAN.md Phase 15: a slate of at most 3 overlays
# The owner's seen-data declaration for the first overlay run (PLAN.md Phase 15, "The design
# window has been seen"). Callers pass it explicitly to render_overlay_preregistration.
SEEN_DATA_DECLARATION = (
    "The design window (2019-01 to 2025-10) has been seen. Edge runs on 2026-10-25 and "
    "2026-10-26 already examined trend filters, volatility gating and regime filters on it, so "
    "every overlay here comes from the literature with its published parameters, untuned, and "
    "declares its family links to earlier ledgered plans. The locked holdout (from 2025-10-01) "
    "is unspent; it and the forward paper test are the real tests."
)

OverlayOutcome = Literal[
    "running",
    "candidate_overlay",
    "no_overlay_benefit",
    "not_testable",
    "budget_exhausted",
    "error",
    "stopped",
]

OVERLAY_CRITERIA_KEYS: tuple[str, ...] = (
    "cdar_vs_buy_hold_max",
    "beat_exposure_matched",
    "sharpe_non_inferiority",
    "break_even_cost_multiple_min",
    "min_drawdown_episodes",
    "min_avg_exposure",
    "alpha",
    "wf_rows_hold",
    "pbo_max",
    "require_holdout_pass",
    "require_crosscheck_match",
    "cdar_confidence",
    "episode_depth_min",
    "bootstrap_mean_block_days",
    "min_episodes_improved_share",
)
_OVERLAY_LABELS: dict[str, str] = {
    "cdar_vs_buy_hold_max": "Overlay CDaR as a fraction of buy-and-hold's CDaR, at most",
    "beat_exposure_matched": (
        "Overlay CDaR below exposure-matched buy-and-hold's (paired bootstrap, p at most "
        "alpha / N_overlay)"
    ),
    "sharpe_non_inferiority": (
        "One-sided lower bound of Sharpe(overlay) minus Sharpe(buy-and-hold), 365-day, paired "
        "bootstrap, at least minus this"
    ),
    "break_even_cost_multiple_min": (
        "Drawdown, episodes-improved and Sharpe rows still hold at this cost multiple"
    ),
    "min_drawdown_episodes": (
        "Buy-and-hold drawdown episodes deeper than the episode depth, at least (else not testable)"
    ),
    "min_avg_exposure": "Average exposure (an overlay, not a timing strategy), at least",
    "alpha": "Significance level before the Bonferroni division",
    "wf_rows_hold": (
        "Rows also hold on stitched fixed-parameter test folds (a sub-window robustness check on "
        "the same design bars, not out-of-sample)"
    ),
    "pbo_max": "Probability of backtest overfitting, at most (nothing swept: untuned by rule)",
    "require_holdout_pass": "Passes the locked holdout check (pass/fail only)",
    "require_crosscheck_match": "Matches the event-driven cross-check engine",
    "cdar_confidence": "CDaR confidence level a (mean of the worst 1 - a share of bar drawdowns)",
    "episode_depth_min": "Depth that makes a buy-and-hold drawdown an episode (strictly deeper)",
    "bootstrap_mean_block_days": "Stationary-bootstrap mean block length (days)",
    "min_episodes_improved_share": (
        "Share of buy-and-hold's deep episodes in which the overlay's drawdown is below "
        "exposure-matched buy-and-hold's, strictly more than"
    ),
}
_BUDGET_LIMIT_KEYS = tuple(limit for _label, _used, limit in _BUDGET_ROWS)
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_JOURNAL_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_URL = re.compile(r"^https?://\S+$")


@dataclass(frozen=True)
class FamilyLink:
    """An earlier ledgered plan in the same family as a slate overlay. ``relation`` says how
    it relates (e.g. "same 200-day trend filter, tested as an edge entry gate"); ``journal`` is
    the Obsidian note stem of the run that tested it (``.md`` optional), or None."""

    plan_hash: str
    run_id: str
    relation: str
    journal: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan_hash, str) or not _PLAN_HASH.match(self.plan_hash):
            raise ValueError(f"family link plan_hash {self.plan_hash!r} must match a plan hash")
        if not isinstance(self.run_id, str) or not _RUN_ID.match(self.run_id):
            raise ValueError(f"family link run_id {self.run_id!r} must match {_RUN_ID.pattern}")
        if not isinstance(self.relation, str) or not self.relation.strip():
            raise ValueError(f"family link to plan {self.plan_hash} needs a relation")
        if self.journal is not None:
            stem = self.journal.removesuffix(".md") if isinstance(self.journal, str) else ""
            if not _JOURNAL_STEM.match(stem):
                raise ValueError(
                    f"family link journal {self.journal!r} must be a journal note stem "
                    f"matching {_JOURNAL_STEM.pattern}"
                )


@dataclass(frozen=True)
class OverlaySlateEntry:
    """One pre-registered overlay. ``source`` is the citation text (untrusted: it is quoted
    in a warning callout), ``source_url`` the http(s) URL, ``published_params`` the parameters
    as published, ``plan`` the overlay plan JSON exactly as it will be submitted."""

    name: str
    source: str
    source_url: str
    published_params: Mapping[str, Any]
    plan: Mapping[str, Any]
    plan_hash: str
    family_links: Sequence[FamilyLink] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("slate entry name must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError(f"slate entry {self.name!r} needs its source")
        if not isinstance(self.source_url, str) or not _URL.match(self.source_url.strip()):
            raise ValueError(
                f"slate entry {self.name!r} source_url {self.source_url!r} must be an http(s) URL"
            )
        if not isinstance(self.published_params, Mapping) or not self.published_params:
            raise ValueError(f"slate entry {self.name!r} needs its published parameters")
        if not isinstance(self.plan, Mapping) or self.plan.get("kind") != "overlay":
            raise ValueError(f'slate entry {self.name!r} plan must be a {{"kind": "overlay"}} plan')
        if not isinstance(self.plan_hash, str) or not _PLAN_HASH.match(self.plan_hash):
            raise ValueError(
                f"slate entry {self.name!r} plan_hash {self.plan_hash!r} must match "
                f"{_PLAN_HASH.pattern}"
            )
        if isinstance(self.family_links, str | bytes) or not all(
            isinstance(link, FamilyLink) for link in self.family_links
        ):
            raise ValueError(f"slate entry {self.name!r} family_links must be FamilyLink items")


def _overlay_criteria(research: object) -> dict[str, Any]:
    """The 15 frozen overlay criteria from a duck-typed config, in a fixed order. Fails
    closed: a missing or unknown key is a ValueError naming it."""
    crit = getattr(research, "criteria", None)
    if crit is None:
        raise ValueError("overlay research config has no criteria")
    if isinstance(crit, Mapping):
        values = dict(crit)
    elif hasattr(crit, "__dataclass_fields__"):
        values = asdict(crit)  # type: ignore[call-overload]
    else:
        values = {k: getattr(crit, k) for k in OVERLAY_CRITERIA_KEYS if hasattr(crit, k)}
    missing = [k for k in OVERLAY_CRITERIA_KEYS if k not in values]
    unknown = sorted(set(values) - set(OVERLAY_CRITERIA_KEYS))
    if missing or unknown:
        raise ValueError(
            f"overlay criteria must have exactly the {len(OVERLAY_CRITERIA_KEYS)} keys; "
            f"missing {missing}, unknown {unknown}"
        )
    return {k: values[k] for k in OVERLAY_CRITERIA_KEYS}


def _overlay_budget(research: object) -> dict[str, Any]:
    budget = getattr(research, "budget", None)
    if budget is None:
        raise ValueError("overlay research config has no budget")
    data = dict(budget) if isinstance(budget, Mapping) else {}
    for key in _BUDGET_LIMIT_KEYS:
        if key not in data and hasattr(budget, key):
            data[key] = getattr(budget, key)
    missing = [k for k in _BUDGET_LIMIT_KEYS if k not in data]
    if missing:
        raise ValueError(f"overlay budget is missing {missing}")
    return {k: data[k] for k in _BUDGET_LIMIT_KEYS}


def _check_overlay_research(research: object) -> str:
    question = getattr(research, "question", None)
    if question != OVERLAY_QUESTION:
        raise ValueError(
            f"research.question must be {OVERLAY_QUESTION!r} for an overlay journal, "
            f"got {question!r}"
        )
    sha = getattr(research, "sha256", None)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError(f"research.sha256 must be a 64-hex sha256, got {sha!r}")
    return sha


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _param_value(value: object) -> str:
    if isinstance(value, Mapping | list | tuple):
        return _cell(json.dumps(value, sort_keys=True, ensure_ascii=True))
    return _cell(value)


def _plan_block(plan: Mapping[str, Any]) -> list[str]:
    """The plan JSON in a fenced block. ``json.dumps`` escapes newlines and non-ASCII, and
    every line starts with whitespace or a brace, so nothing inside can close the fence."""
    text = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=True)
    return ["```json", *text.splitlines(), "```"]


def _slate_entry_lines(i: int, entry: OverlaySlateEntry) -> list[str]:
    url = " ".join(entry.source_url.split()).replace("`", "%60")
    out = [
        f"#### {i}. {_cell(entry.name)} · plan `{entry.plan_hash}`",
        "",
        OVERLAY_SOURCE_CALLOUT,
        f"> Source URL: `{url}`",
        ">",
    ]
    out += [f"> {line}" if line else ">" for line in neutralise_untrusted(entry.source.strip())]
    out += [
        "",
        "Published parameters (used as published, untuned):",
        "",
        "| parameter | published value |",
        "|---|---|",
    ]
    out += [f"| {_cell(k)} | {_param_value(v)} |" for k, v in entry.published_params.items()]
    out += ["", "Plan JSON, exactly as submitted:", "", *_plan_block(entry.plan), ""]
    out += ["Family links to earlier ledgered plans:", ""]
    if not entry.family_links:
        out.append(
            "- none declared: the designer states that no earlier ledgered plan is in this "
            "overlay's family. The critic checks this against `get_ledger`."
        )
    for link in entry.family_links:
        note = f" · [[{link.journal.removesuffix('.md')}]]" if link.journal else ""
        out.append(
            f"- plan `{link.plan_hash}` in [[run-{link.run_id}]]{note}: {_inline(link.relation)}"
        )
    out.append("")
    return out


def render_overlay_preregistration(
    research: object,
    slate: Sequence[OverlaySlateEntry],
    *,
    seen_data: str,
    holdout_budget: int,
) -> str:
    """The overlay pre-registration section (Markdown, starting with
    :data:`OVERLAY_PREREG_HEADING`), written before the first backtest.

    ``research`` is duck-typed like ``lab.research.contract.OverlayResearchConfig``:
    ``.question == "risk_overlay"``, ``.criteria`` with the 15 overlay keys, ``.budget`` with
    the five ``RunBudget`` limits, ``.sha256``. ``holdout_budget`` is how many holdout checks
    this run may spend (at most the frozen ``max_holdout_checks``). Raises ValueError on any
    missing piece and :class:`HoldoutLeakError` if text would record a holdout result."""
    sha = _check_overlay_research(research)
    criteria = _overlay_criteria(research)
    budget = _overlay_budget(research)
    if isinstance(slate, str | bytes) or not slate:
        raise ValueError("the overlay slate must list at least one OverlaySlateEntry")
    if len(slate) > OVERLAY_MAX_SLATE:
        raise ValueError(f"the overlay slate has {len(slate)} entries; at most {OVERLAY_MAX_SLATE}")
    if not all(isinstance(e, OverlaySlateEntry) for e in slate):
        raise ValueError("every slate entry must be an OverlaySlateEntry")
    hashes = [e.plan_hash for e in slate]
    if len(set(hashes)) != len(hashes):
        raise ValueError(f"the overlay slate repeats a plan hash: {hashes}")
    if not isinstance(seen_data, str) or not seen_data.strip():
        raise ValueError("seen_data must state what the design window has already been used for")
    max_checks = budget["max_holdout_checks"]
    if (
        not isinstance(holdout_budget, int)
        or isinstance(holdout_budget, bool)
        or not 0 <= holdout_budget <= max_checks
    ):
        raise ValueError(
            f"holdout_budget must be an int between 0 and max_holdout_checks ({max_checks}), "
            f"got {holdout_budget!r}"
        )
    for entry in slate:
        guard_holdout(entry.source)
        for link in entry.family_links:
            guard_holdout(link.relation)
    guard_holdout(seen_data)

    alpha = criteria["alpha"]
    per_test = f"{alpha / len(slate):g}" if _number(alpha) else NOT_RECORDED
    lines = [
        OVERLAY_PREREG_HEADING,
        "",
        "Everything in this section was written before the first backtest of this run and is "
        "never edited afterwards.",
        "",
        "### Question",
        "",
        f"**{OVERLAY_QUESTION_TEXT}**",
        "",
        "A pass is reported as a *risk overlay*, never as an edge. The base is buy-and-hold of "
        "one symbol; a rule decided on bar t's close sets the exposure for bar t+1's open to 1 "
        "or 0. Overlays use their published parameters, untuned: `run_sweep` and "
        "`walk_forward` are refused in overlay runs, and each overlay logic may be tried with "
        "one published parameter set only, across every overlay run. Before anything is "
        "recorded the lab refuses another parameter set or a renamed copy of a submitted "
        "overlay logic, the same registry features with other parameters (or another interval) "
        "as an earlier overlay submission, and a logic whose parameters were already tried "
        "outside the overlay runs. A copy on other features is not refused; alike overlays "
        "(design-window returns correlated at or above `family_return_corr`) make `pbo_max` "
        "fail.",
        "",
        "### Criteria (frozen at start_run)",
        "",
        f"Overlay criteria file sha256: `{sha}`. These values were frozen into the ledger by "
        "`start_run` before the first backtest and are never relaxed mid-run.",
        "",
        "| criterion | key | value |",
        "|---|---|---|",
    ]
    lines += [f"| {_OVERLAY_LABELS[k]} | `{k}` | {_fmt(v)} |" for k, v in criteria.items()]
    lines += [
        "",
        "### Multiple testing (Bonferroni)",
        "",
        f"Every paired-bootstrap p-value is compared with alpha / N_overlay, alpha = "
        f"{_fmt(alpha)}. N_overlay counts distinct overlay logics across every overlay run in "
        "the ledger, this one included; each logic may be tried with one published parameter "
        "set only. A new run never resets it. N_overlay is read from `get_report` at judging "
        "time. Overlay trials also count in the lab-wide deflated-Sharpe trial count N (never in "
        "its Sharpe-variance pools).",
        "",
        f"This slate adds {len(slate)} plan(s), so N_overlay is at least {len(slate)} and the "
        f"per-test level is at most {per_test}.",
        "",
        "### Budget (frozen at start_run)",
        "",
        "| budget | key | limit |",
        "|---|---|---|",
    ]
    for label, _used_key, limit_key in _BUDGET_ROWS:
        lines.append(f"| {label} | `{limit_key}` | {_fmt(budget[limit_key])} |")
    lines += [
        "",
        f"This run spends at most {holdout_budget} holdout check(s), pass/fail only. A failed "
        "check kills the overlay's logic under either question; it is never re-tuned.",
        "",
        "### Slate",
        "",
        f"{len(slate)} overlay(s), each from the literature with its published parameters. "
        "Source text is untrusted data, quoted inside a warning callout.",
        "",
    ]
    for i, entry in enumerate(slate, start=1):
        lines += _slate_entry_lines(i, entry)
    lines += [
        "### Seen-data declaration",
        "",
        _inline(seen_data),
        "",
        "### Traps this design closes",
        "",
        "- **Holding less always cuts drawdown.** 50% BTC has about half of BTC's drawdown and "
        "the same Sharpe. Each overlay is compared with buy-and-hold scaled to the overlay's "
        "own average exposure (a constant mix rebalanced every bar at zero cost, which favours "
        "the benchmark) as well as with plain buy-and-hold. Beating plain buy-and-hold on "
        "drawdown proves nothing by itself.",
        "- **Max drawdown is one number from one path.** Rows use CDaR (the mean of the worst "
        "tail of bar drawdowns) and paired stationary-bootstrap tests. If buy-and-hold has "
        "fewer than `min_drawdown_episodes` separate drawdowns deeper than "
        "`episode_depth_min` in the scored window, the result is **not testable**, and not "
        "testable is never a pass. Each deep episode is also compared one by one: the "
        "overlay's drawdown inside it (from its own equity at the episode peak) must be below "
        "exposure-matched buy-and-hold's in strictly more than `min_episodes_improved_share` "
        "of them.",
        "- **The walk-forward row is not out-of-sample.** `wf_rows_hold` is a sub-window "
        "robustness check on stitched fixed-parameter test folds (same design bars, not "
        "out-of-sample). Its drawdown rows are judged only when the folds themselves hold at "
        "least `min_drawdown_episodes` deep episodes; otherwise they are not testable there, "
        "and not testable is never a pass.",
        "- **Sharpe non-inferiority** is judged on the one-sided bootstrap lower bound, not on "
        "the point estimate, and the **break-even cost** row asks that the drawdown, "
        "episodes-improved and Sharpe rows still hold at `break_even_cost_multiple_min` times "
        "the base costs.",
        "- **The design window has been seen** (declaration above), so parameters are the "
        "published ones and each overlay declares its family links.",
        "",
        "### Outcome vocabulary",
        "",
        "- `candidate_overlay`: passed every overlay row and the locked check; still needs "
        "4-8 weeks of `freqtrade --dry-run` before it is called a working risk overlay.",
        "- `no_overlay_benefit`: no overlay passed every row (a correct, complete outcome).",
        "- `not_testable`: too few drawdown episodes to judge; never a pass.",
        "- `budget_exhausted`, `error`, `stopped`: partial; no conclusion either way.",
        "",
    ]
    text = "\n".join(lines)
    guard_holdout(text, per_line=True)
    return text


_OVERLAY_OUTCOME_TEXT: dict[str, str] = {
    "candidate_overlay": (
        "**Candidate risk overlay.** {n} overlay(s) passed every pre-registered overlay row on "
        "the design window and the stitched fixed-parameter test folds (a sub-window "
        "robustness check, not out-of-sample), the cross-check and the locked check. This is "
        "a risk result, not a return result. It is not a working risk overlay "
        "until 4-8 weeks of `freqtrade --dry-run` paper trading under the Phase 13 protocol "
        "agree with it."
    ),
    "no_overlay_benefit": (
        "**No overlay benefit found.** No overlay in this run passed every pre-registered "
        "overlay row. This is a correct, complete outcome; the criteria and budget were not "
        "relaxed."
    ),
    "not_testable": (
        "**Not testable.** Buy-and-hold had too few separate drawdown episodes in the scored "
        "window to judge the drawdown rows. Not testable is never a pass, and it is not "
        "evidence of benefit either."
    ),
    "budget_exhausted": (
        "**Budget exhausted — no overlay benefit found within this budget.** The budget and "
        "criteria were not extended or relaxed."
    ),
    "error": (
        "**Run ended with an error.** The results are partial and support no conclusion either way."
    ),
    "stopped": (
        "**Run stopped before completion.** The results are partial and support no conclusion "
        "either way."
    ),
}
OVERLAY_DISCLAIMER = (
    "Backtests are historical, not forward-looking. A risk overlay is a drawdown result "
    "measured against exposure-matched buy-and-hold, not a return forecast."
)


def _overlay_rows_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Overlay rows; a ``not_testable`` status says plainly that it is never a pass."""
    shown: list[Mapping[str, Any]] = []
    for row in rows:
        status = row.get("status")
        if not isinstance(status, str):
            raise ValueError(f"criteria row {row.get('criterion')!r} has no status")
        if status == "not_testable":
            row = {**row, "status": "not testable (never a pass)"}
        shown.append(row)
    return _criteria_table(shown)


def render_overlay_report(
    outcome: OverlayOutcome,
    *,
    run_id: str,
    plan_rows: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    candidates: Sequence[str] = (),
    n_overlay: int | None = None,
    notes: Sequence[str] = (),
) -> str:
    """The overlay end-of-run report. ``plan_rows`` maps plan hash to its overlay criteria
    rows (from ``evaluate_overlay_plan``: criterion, status, value, threshold, note); holdout
    rows keep their status only. ``candidates`` are the plan hashes that passed."""
    if outcome not in _OVERLAY_OUTCOME_TEXT:
        raise ValueError(
            f"overlay outcome must be one of {sorted(_OVERLAY_OUTCOME_TEXT)}, got {outcome!r}"
        )
    plan_rows = dict(plan_rows or {})
    for h in [*plan_rows, *candidates]:
        if not isinstance(h, str) or not _PLAN_HASH.match(h):
            raise ValueError(f"plan hash {h!r} must match {_PLAN_HASH.pattern}")
    if (outcome == "candidate_overlay") != bool(candidates):
        raise ValueError("candidates are required for candidate_overlay and only for it")
    out = [
        REPORT_HEADING,
        "",
        f"Run [[run-{run_id}]] · question `{OVERLAY_QUESTION}`",
        "",
        "### Outcome",
        "",
        _OVERLAY_OUTCOME_TEXT[outcome].format(n=len(candidates)),
        "",
        "### N_overlay",
        "",
        (
            f"N_overlay = {int(n_overlay)}: distinct overlay logics across every overlay run "
            "(each logic may be tried with one published parameter set only); each p-value was "
            "compared with alpha / N_overlay."
            if isinstance(n_overlay, int) and not isinstance(n_overlay, bool)
            else f"N_overlay {NOT_RECORDED}."
        ),
        "",
        "### Overlay rows",
        "",
    ]
    if not plan_rows:
        out.append("No overlay was scored in this run.")
    for h, rows in plan_rows.items():
        tag = " (candidate risk overlay)" if h in candidates else ""
        out += [f"#### plan `{h}`{tag}", "", *_overlay_rows_table(rows), ""]
    out += ["", "### Notes", ""]
    out += [f"- {_inline(n)}" for n in notes] or ["None recorded."]
    out += ["", "### Disclaimer", "", OVERLAY_DISCLAIMER, ""]
    return "\n".join(out)


def _guard_summary(summary: RunSummary) -> None:
    """Free-form inputs are checked as whole texts; the rendered report line by line."""
    texts: list[str] = list(summary.notes)
    for idea in summary.ideas:
        texts += [idea.title, idea.reason]
    for rep in summary.candidates:
        texts.append(rep.candidate["summary"])
        for row in rep.criteria_rows:
            if not re.search(_HOLDOUT, str(row.get("criterion", "")), re.IGNORECASE):
                texts.append(str(row.get("note") or ""))
    for text in texts:
        guard_holdout(text)


# --------------------------------------------------------------------------- journal


class Journal:
    """Append-only research journal for one run. The file is the source of truth, so a
    :meth:`open` on a crashed run's file resumes it."""

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = Path(path)
        self._run_id = run_id

    # ------------------------------------------------------------------ construction

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        symbols: Sequence[str],
        interval: str,
        research: ResearchConfig,
        title: str,
        journal_dir: Path = JOURNAL_DIR,
        today: date | None = None,
    ) -> Journal:
        if not run_id or not isinstance(run_id, str):
            raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
        if not symbols:
            raise ValueError("symbols must not be empty")
        guard_holdout(title)
        day = (today or datetime.now(UTC).date()).isoformat()
        symbol_part = "-".join(_slug(s, 20) for s in symbols)
        stem = f"{day}-{symbol_part}-{_slug(title)}"
        frontmatter = (
            "---\n"
            f"run_id: {_yaml_str(run_id)}\n"
            f"date: {day}\n"
            + _block_list("symbols", list(symbols))
            + f"interval: {_yaml_str(interval)}\n"
            "outcome: running\n"
            "idea_ids: []\n"
            f"criteria_sha256: {_yaml_str(research.sha256)}\n"
            "tags:\n  - research\n  - phase-8\n"
            "---\n"
        )
        body = (
            f"# {_inline(title)}\n\n"
            f"Run [[run-{run_id}]] · {', '.join(symbols)} {interval}\n\n"
            "Holdout metrics are never written to this journal; only that a check was spent and "
            "the resulting status.\n\n"
            f"{CRITERIA_HEADING}\n\n"
            f"{_render_research(research)}\n"
            f"{LOG_HEADING}\n\n"
        )
        data = (frontmatter + body).encode("utf-8")
        guard_holdout(body, per_line=True)
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            path = journal_dir / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
            try:
                _create_exclusive(path, data)
            except FileExistsError:
                n += 1
                continue
            return cls(path, run_id)

    @classmethod
    def create_overlay(
        cls,
        *,
        run_id: str,
        symbols: Sequence[str],
        interval: str,
        research: object,
        slate: Sequence[OverlaySlateEntry],
        seen_data: str,
        holdout_budget: int,
        title: str,
        idea_ids: Sequence[int] = (),
        journal_dir: Path = JOURNAL_DIR,
        today: date | None = None,
    ) -> Journal:
        """Create a risk-overlay journal (PLAN.md Phase 15) whose body opens with the
        pre-registration (:func:`render_overlay_preregistration`), written before any backtest.
        Frontmatter adds ``question: risk_overlay``; finish it with :meth:`finish_overlay`."""
        if not run_id or not isinstance(run_id, str):
            raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
        if not symbols:
            raise ValueError("symbols must not be empty")
        guard_holdout(title)
        prereg = render_overlay_preregistration(
            research, slate, seen_data=seen_data, holdout_budget=holdout_budget
        )
        sha = _check_overlay_research(research)
        ids = sorted({int(i) for i in idea_ids})
        day = (today or datetime.now(UTC).date()).isoformat()
        symbol_part = "-".join(_slug(s, 20) for s in symbols)
        stem = f"{day}-{symbol_part}-{_slug(title)}"
        frontmatter = (
            "---\n"
            f"run_id: {_yaml_str(run_id)}\n"
            f"date: {day}\n"
            f"question: {OVERLAY_QUESTION}\n"
            + _block_list("symbols", list(symbols))
            + f"interval: {_yaml_str(interval)}\n"
            "outcome: running\n"
            + _block_list("idea_ids", ids)
            + f"criteria_sha256: {_yaml_str(sha)}\n"
            + _block_list("plan_hashes", [e.plan_hash for e in slate])
            + "tags:\n  - research\n  - phase-15\n  - risk-overlay\n"
            "---\n"
        )
        body = (
            f"# {_inline(title)}\n\n"
            f"Run [[run-{run_id}]] · {', '.join(symbols)} {interval} · question "
            f"`{OVERLAY_QUESTION}`\n\n"
            "Holdout metrics are never written to this journal; only that a check was spent and "
            "the resulting status.\n\n"
            f"{prereg}\n"
            f"{LOG_HEADING}\n\n"
        )
        guard_holdout(body, per_line=True)
        data = (frontmatter + body).encode("utf-8")
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            path = journal_dir / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
            try:
                _create_exclusive(path, data)
            except FileExistsError:
                n += 1
                continue
            return cls(path, run_id)

    @classmethod
    def open(cls, path: Path) -> Journal:
        path = Path(path)
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        run_id = meta.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"{path} has no run_id in its frontmatter")
        return cls(path, run_id)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    # ------------------------------------------------------------------ writes

    def _read(self) -> str:
        return self._path.read_bytes().decode("utf-8")

    def _append(self, chunk: str) -> None:
        text = self._read()
        if parse_frontmatter(text).get("outcome") != "running":
            raise RuntimeError(f"journal {self._path.name} is finished; no further writes")
        if not text.endswith("\n"):
            text += "\n"
        _atomic_write(self._path, (text + chunk).encode("utf-8"))

    def log(
        self,
        kind: str,
        text: str,
        *,
        idea_ids: Sequence[int] = (),
        trial_ids: Sequence[int] = (),
        plan_hashes: Sequence[str] = (),
    ) -> None:
        """Append a timestamped bullet with Obsidian links to ideas, trials, and the run."""
        if not _KIND.match(kind):
            raise ValueError(f"kind must match {_KIND.pattern}, got {kind!r}")
        guard_holdout(text)
        for h in plan_hashes:
            if not _PLAN_HASH.match(h):
                raise ValueError(f"plan hash {h!r} must match {_PLAN_HASH.pattern}")
        links = [f"[[idea-{int(i)}]]" for i in idea_ids]
        links += [f"[[trial-{int(t)}]]" for t in trial_ids]
        if trial_ids:
            links.append(f"[[run-{self._run_id}]]")
        links += [f"plan `{h}`" for h in plan_hashes]
        suffix = f" · {' '.join(links)}" if links else ""
        self._append(f"- {_now()} **{kind}** — {_inline(text)}{suffix}\n")

    def log_untrusted(self, text: str, *, source_url: str) -> None:
        """Quote scout-sourced text inside a warning callout, neutralised."""
        guard_holdout(text)
        url = " ".join(source_url.split()).replace("`", "%60")
        lines = [UNTRUSTED_CALLOUT, f"> Source: `{url}`", ">"]
        lines += [f"> {line}" if line else ">" for line in neutralise_untrusted(text)]
        self._append(
            f"- {_now()} **scout** — untrusted source text:\n\n" + "\n".join(lines) + "\n\n"
        )

    def log_holdout_spent(
        self, idea_id: int, plan_hash: str, outcome: Literal["candidate", "dead"]
    ) -> None:
        """Record that a holdout check was spent and the resulting status. No numbers."""
        if outcome not in ("candidate", "dead"):
            raise ValueError(f"outcome must be 'candidate' or 'dead', got {outcome!r}")
        if not _PLAN_HASH.match(plan_hash):
            raise ValueError(f"plan hash {plan_hash!r} must match {_PLAN_HASH.pattern}")
        verdict = (
            "idea marked candidate"
            if outcome == "candidate"
            else "idea marked dead (no re-tuning on the holdout)"
        )
        self.log(
            "holdout",
            f"Holdout check spent; {verdict}. Holdout metrics are not recorded.",
            idea_ids=[idea_id],
            plan_hashes=[plan_hash],
        )

    def finish(self, summary: RunSummary) -> None:
        """Set ``outcome`` and ``idea_ids`` in the frontmatter and append the report."""
        if summary.run_id != self._run_id:
            raise ValueError(f"summary run_id {summary.run_id!r} != journal {self._run_id!r}")
        if summary.outcome == "running":
            raise ValueError("cannot finish a run whose outcome is still 'running'")
        if parse_frontmatter(self._read()).get("question") == OVERLAY_QUESTION:
            raise ValueError("this is a risk-overlay journal; finish it with finish_overlay()")
        _guard_summary(summary)
        report = render_report(summary, journal_path=self._path)
        guard_holdout(report, per_line=True)

        text = self._read()
        block, rest = _split_frontmatter(text)
        meta = parse_frontmatter(text)
        if meta.get("outcome") != "running":
            raise RuntimeError(f"journal {self._path.name} is already finished")
        ids = {int(i) for i in meta.get("idea_ids") or []}
        ids |= {int(m) for m in _IDEA_LINK.findall(rest)}
        ids |= {i.idea_id for i in summary.ideas}
        ids |= {int(r.candidate["idea_id"]) for r in summary.candidates}
        block = _replace_key(block, "outcome", f"outcome: {summary.outcome}\n")
        block = _replace_key(block, "idea_ids", _block_list("idea_ids", sorted(ids)))
        if not rest.endswith("\n"):
            rest += "\n"
        _atomic_write(self._path, (block + rest + "\n" + report).encode("utf-8"))

    def finish_overlay(
        self,
        outcome: OverlayOutcome,
        *,
        plan_rows: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        candidates: Sequence[str] = (),
        n_overlay: int | None = None,
        notes: Sequence[str] = (),
    ) -> None:
        """Finish a risk-overlay journal: set ``outcome`` (overlay vocabulary) and
        ``idea_ids``, then append :func:`render_overlay_report`. Holdout numbers are refused."""
        if outcome == "running":
            raise ValueError("cannot finish a run whose outcome is still 'running'")
        text = self._read()
        meta = parse_frontmatter(text)
        if meta.get("question") != OVERLAY_QUESTION:
            raise ValueError("not a risk-overlay journal; finish it with finish()")
        if meta.get("outcome") != "running":
            raise RuntimeError(f"journal {self._path.name} is already finished")
        for note in notes:
            guard_holdout(note)
        for rows in (plan_rows or {}).values():
            for row in rows:
                if not re.search(_HOLDOUT, str(row.get("criterion", "")), re.IGNORECASE):
                    guard_holdout(str(row.get("note") or ""))
        report = render_overlay_report(
            outcome,
            run_id=self._run_id,
            plan_rows=plan_rows,
            candidates=candidates,
            n_overlay=n_overlay,
            notes=notes,
        )
        guard_holdout(report, per_line=True)
        block, rest = _split_frontmatter(text)
        ids = {int(i) for i in meta.get("idea_ids") or []}
        ids |= {int(m) for m in _IDEA_LINK.findall(rest)}
        block = _replace_key(block, "outcome", f"outcome: {outcome}\n")
        block = _replace_key(block, "idea_ids", _block_list("idea_ids", sorted(ids)))
        if not rest.endswith("\n"):
            rest += "\n"
        _atomic_write(self._path, (block + rest + "\n" + report).encode("utf-8"))
