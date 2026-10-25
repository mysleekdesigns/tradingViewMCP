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
    BudgetUsage,
    CandidateSummary,
    ResearchConfig,
    RunOutcome,
)

__all__ = [
    "HOLDOUT_WORD",
    "CandidateReport",
    "HoldoutLeakError",
    "IdeaOutcome",
    "Journal",
    "RunSummary",
    "guard_holdout",
    "neutralise_untrusted",
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


def render_report(summary: RunSummary) -> str:
    """The end-of-run report (Markdown, starting with ``## End-of-run report``)."""
    if summary.outcome not in _OUTCOME_TEXT:
        raise ValueError(f"cannot report outcome {summary.outcome!r}; finish the run first")
    s = summary
    out: list[str] = [
        REPORT_HEADING,
        "",
        f"Run [[run-{s.run_id}]] · {', '.join(s.symbols)} {s.interval} · "
        f"started {s.started_at} · finished {s.finished_at}",
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
        _guard_summary(summary)
        report = render_report(summary)
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
