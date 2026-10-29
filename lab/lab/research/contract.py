"""The shared Phase 8 contract between the lab (MCP server, API) and ``agents/``.

* Paths: where the success criteria, idea library, run-state files, and journal live.
* :class:`ResearchConfig`: success criteria and per-run budgets, loaded from
  ``lab/config/success_criteria.toml``. That file is owner-controlled (read-only to Claude
  while ``.claude/lab.lock`` exists) and is snapshotted into the ledger when a run starts, so
  the bar cannot move mid-run. A missing file is an error: research never runs without a bar.
* :class:`OverlayResearchConfig` (PLAN.md Phase 15): the second research question,
  ``"risk_overlay"`` (does one slow overlay on buy-and-hold cut drawdown without a significant
  Sharpe loss?). Its rows and budget come from ``lab/config/overlay_criteria.toml``, which the
  owner installs from ``agents/config/overlay_criteria.proposed.toml``; no file, no overlay
  research. A pass is a risk overlay, never a return claim. The first question (``"edge"``) and
  its ``success_criteria.toml`` are unchanged.
* ``IDEAS_SCHEMA``: the idea-library SQLite schema (written by ``agents/``, read by the API).
* :class:`RunState`: the JSON progress file the orchestrator writes for the Research page.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import MISSING, asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from lab.config import LAB_ROOT

REPO_ROOT = LAB_ROOT.parent
SUCCESS_CRITERIA_PATH = LAB_ROOT / "config" / "success_criteria.toml"
OVERLAY_CRITERIA_PATH = LAB_ROOT / "config" / "overlay_criteria.toml"
AGENTS_DIR = REPO_ROOT / "agents"
JOURNAL_DIR = AGENTS_DIR / "journal"  # committed Markdown, Obsidian-friendly
AGENTS_DATA_DIR = AGENTS_DIR / "data"  # gitignored runtime data
IDEAS_DB_PATH = AGENTS_DATA_DIR / "ideas.sqlite"
RUN_STATE_DIR = AGENTS_DATA_DIR / "runs"  # one <run_id>.json per research run
# One folder per paper-traded candidate: generated freqtrade strategy + config, the dry-run
# SQLite DB, and weekly comparison-<YYYY-MM-DD>.json files (PaperComparison).
PAPER_DIR = AGENTS_DATA_DIR / "paper"

# Server-assigned research run ids: "r-YYYYMMDD-HHMMSS-<6 hex>", also valid for the API's
# RUN_ID_PATTERN.
RESEARCH_RUN_ID_PATTERN = r"^r-\d{8}-\d{6}-[0-9a-f]{6}$"


class ResearchConfigError(RuntimeError):
    """The success-criteria file is missing or malformed. Research must not start."""


# The research questions a run can ask, frozen at ``start_run``. An edge run's frozen config has
# no "question" key (absence means "edge"), so edge runs started before Phase 15 and after it
# freeze byte-identical configs.
Question = Literal["edge", "risk_overlay"]
QUESTIONS: tuple[str, ...] = ("edge", "risk_overlay")


# ``[criteria] dsr_trial_count``: which trial count N the DSR row deflates with. Only "nominal"
# (every distinct plan tried in every run of the lab) is accepted. The eigenvalue
# participation-ratio "effective" N (lab.research.trial_count) is reported as a diagnostic only:
# it counts the shared market factor a second time (V[SR] already reflects it), it FALLS when
# near-copies are added, and it is capped near 1 / (top eigenvalue share)^2 and biased down by
# M / T, so a zero-skill null passes DSR far more often than nominal. Absent -> "nominal".
DSR_TRIAL_COUNTS: tuple[str, ...] = ("nominal",)

# Why "effective" is refused, in the config loader and for a hand-forged frozen snapshot.
DSR_EFFECTIVE_REFUSED = (
    'dsr_trial_count = "effective" is not accepted: the participation-ratio effective N is a '
    "diagnostic only (it double-counts the shared market factor and falls as trials are "
    'added), until an estimator v2 passes its calibration tests; use "nominal"'
)


@dataclass(frozen=True)
class SuccessCriteria:
    """PLAN.md Phase 8 success criteria. A candidate must pass every one."""

    wf_oos_sharpe_min: float  # walk-forward out-of-sample annualised Sharpe, after costs
    dsr_min: float  # Deflated Sharpe Ratio with the run's full trial count
    pbo_max: float  # probability of backtest overfitting
    beat_buy_hold: bool  # risk-adjusted (Sharpe) above buy-and-hold
    beat_vol_target: bool  # risk-adjusted (Sharpe) above vol-targeted buy-and-hold
    random_entry_percentile_min: float  # Sharpe percentile vs the random-entry null
    cost_stress_factor: float  # net return must stay positive at this cost multiple
    require_gradual_delay_decay: bool  # edge decays gradually under signal delay, no cliff
    max_drawdown_max: float  # as a positive fraction, e.g. 0.25
    min_trades: int
    min_profitable_regimes: int  # of 3 (bull / bear / sideways), unless regime-gated
    regime_window_days: int  # trailing window (days) for the bull / bear / sideways labels
    regime_threshold: float  # ± simple return over that window: bull above, bear below
    neighbourhood_pct: float  # ±pct parameter neighbours must stay profitable
    require_holdout_pass: bool
    require_crosscheck_match: bool  # event-driven cross-check engine agrees
    # DSR N: "nominal" (distinct plans) only; see DSR_TRIAL_COUNTS. Last and defaulted, so a
    # run frozen before it existed reads "nominal".
    dsr_trial_count: str = "nominal"


# The allowed values of every string-valued criteria key (``_section`` rejects anything else).
_STR_CHOICES: dict[str, tuple[str, ...]] = {"dsr_trial_count": DSR_TRIAL_COUNTS}


@dataclass(frozen=True)
class RunBudget:
    """Per-run limits. The MCP server enforces proposals, backtests, and holdout checks; the
    orchestrator enforces hypotheses and dollars. All are fixed when the run starts."""

    max_hypotheses: int
    max_proposals: int  # distinct plans submitted for a backtest
    max_backtests: int  # ledger trials recorded under the run (Optuna trials included)
    max_holdout_checks: int  # also capped by lab.toml [splits] holdout_budget
    max_usd: float  # model spend for an autonomous run


@dataclass(frozen=True)
class PortfolioCriteria:
    """Optional ``[portfolio]`` table: extra bars for multi-coin portfolio plans (PLAN.md
    Phase 10). Without it a portfolio plan's portfolio rows are "missing" (fail closed)."""

    beat_equal_weight: bool  # net Sharpe above the equal-weight universe on the same schedule
    alpha_p_max: float  # HAC p-value of the net alpha to BTC must be at or below this
    alpha_min_annual: float  # and the annualised net alpha to BTC above this


@dataclass(frozen=True)
class ResearchConfig:
    criteria: SuccessCriteria
    budget: RunBudget
    sha256: str  # of the file bytes; stored with the run so later edits are visible
    portfolio: PortfolioCriteria | None = None  # the optional [portfolio] table

    def to_json(self) -> str:
        data: dict[str, Any] = {"criteria": asdict(self.criteria), "budget": asdict(self.budget)}
        if self.portfolio is not None:
            data["portfolio"] = asdict(self.portfolio)
        return json.dumps(data, sort_keys=True)


def _section(
    raw: dict[str, Any], name: str, cls: type, *, what: str = "success criteria file"
) -> Any:
    if name not in raw or not isinstance(raw[name], dict):
        raise ResearchConfigError(f"{what} has no [{name}] table")
    table = raw[name]
    fields = cls.__dataclass_fields__
    # a field with a default (``dsr_trial_count``) is optional; every other key is required
    required = {k for k, f in fields.items() if f.default is MISSING}
    missing = sorted(required - set(table))
    extra = sorted(set(table) - set(fields))
    if missing or extra:
        raise ResearchConfigError(f"[{name}] missing keys {missing}, unknown keys {extra}")
    values: dict[str, Any] = {}
    for key, field in fields.items():
        if key not in table:
            continue  # optional: the dataclass default applies
        v = table[key]
        kind = field.type
        if kind == "bool":
            ok = isinstance(v, bool)
        elif kind == "int":
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        elif kind == "str":
            choices = _STR_CHOICES.get(key, ())
            if key == "dsr_trial_count" and v == "effective":
                raise ResearchConfigError(f"[{name}] {DSR_EFFECTIVE_REFUSED}")
            if not (isinstance(v, str) and v in choices):
                allowed = " | ".join(f'"{c}"' for c in choices)
                raise ResearchConfigError(f"[{name}] {key} = {v!r} must be one of {allowed}")
            ok = True
        else:
            ok = isinstance(v, int | float) and not isinstance(v, bool) and v >= 0
            v = float(v)
        if not ok:
            raise ResearchConfigError(f"[{name}] {key} = {table[key]!r} is not a valid {kind}")
        values[key] = v
    return cls(**values)


def load_research_config(path: Path | str = SUCCESS_CRITERIA_PATH) -> ResearchConfig:
    """Parse the owner's success-criteria TOML. Raises :class:`ResearchConfigError` when the
    file is missing or any key is absent, unknown, or mistyped (fail closed)."""
    p = Path(path)
    if not p.is_file():
        raise ResearchConfigError(
            f"{p} not found. The owner installs it (it is read-only to Claude while the lab is "
            "locked): cp agents/config/success_criteria.proposed.toml lab/config/"
            "success_criteria.toml"
        )
    data = p.read_bytes()
    try:
        raw = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ResearchConfigError(f"{p} is not valid TOML: {exc}") from exc
    portfolio: PortfolioCriteria | None = None
    if "portfolio" in raw:
        portfolio = _section(raw, "portfolio", PortfolioCriteria)
        if portfolio.alpha_p_max > 1.0:
            raise ResearchConfigError(
                f"[portfolio] alpha_p_max = {portfolio.alpha_p_max!r} is not a p-value in [0, 1]"
            )
    return ResearchConfig(
        criteria=_section(raw, "criteria", SuccessCriteria),
        budget=_section(raw, "budget", RunBudget),
        sha256=hashlib.sha256(data).hexdigest(),
        portfolio=portfolio,
    )


# --------------------------------------------------------------------------- risk-overlay question


@dataclass(frozen=True)
class OverlayCriteria:
    """PLAN.md Phase 15 rows for the ``"risk_overlay"`` question, all required. A pass means
    "a risk overlay worth paper trading", never a return claim."""

    cdar_vs_buy_hold_max: float  # overlay CDaR at most this fraction of buy-and-hold's
    beat_exposure_matched: bool  # CDaR below exposure-matched buy-and-hold, p <= alpha / N
    sharpe_non_inferiority: float  # one-sided lower bound of the Sharpe gap >= -this
    break_even_cost_multiple_min: float  # the CDaR and Sharpe rows still hold at this cost x
    min_drawdown_episodes: int  # buy-and-hold episodes deeper than episode_depth_min
    min_avg_exposure: float  # an overlay, not a timing strategy
    alpha: float  # family significance level, divided by N_overlay (Bonferroni)
    wf_rows_hold: bool  # the rows also hold on stitched fixed-param walk-forward test folds
    pbo_max: float  # only if anything is swept (overlays are untuned by rule)
    require_holdout_pass: bool
    require_crosscheck_match: bool
    cdar_confidence: float  # CDaR level a (0.95: the mean of the worst 5% of drawdowns)
    episode_depth_min: float  # a drawdown episode counts when deeper than this (0.30)
    bootstrap_mean_block_days: int  # stationary-bootstrap mean block, in days
    # share of buy-and-hold's deep episodes in which the overlay's own drawdown (from its equity
    # at the episode peak, over [peak, recovery]) is smaller than the exposure-matched one's;
    # the row needs strictly more than this share (0.5: 3 episodes need 2, 4 need 3)
    min_episodes_improved_share: float


# (key, low, high, low_inclusive, high_inclusive): range checks beyond "a non-negative number"
_OVERLAY_RANGES: tuple[tuple[str, float, float, bool, bool], ...] = (
    ("cdar_vs_buy_hold_max", 0.0, 1.0, False, True),
    ("sharpe_non_inferiority", 0.0, float("inf"), True, False),
    ("break_even_cost_multiple_min", 1.0, float("inf"), True, False),
    ("min_drawdown_episodes", 1.0, float("inf"), True, False),
    ("min_avg_exposure", 0.0, 1.0, False, True),
    ("alpha", 0.0, 1.0, False, False),
    ("pbo_max", 0.0, 1.0, True, True),
    ("cdar_confidence", 0.0, 1.0, False, False),
    ("episode_depth_min", 0.0, 1.0, False, False),
    ("bootstrap_mean_block_days", 1.0, float("inf"), True, False),
    ("min_episodes_improved_share", 0.0, 1.0, True, False),
)


def _check_overlay_ranges(c: OverlayCriteria) -> OverlayCriteria:
    for key, low, high, low_in, high_in in _OVERLAY_RANGES:
        v = float(getattr(c, key))
        above = v >= low if low_in else v > low
        below = v <= high if high_in else v < high
        if not (above and below):
            lo = f"{low:g} {'<=' if low_in else '<'} "
            hi = "" if high == float("inf") else f" {'<=' if high_in else '<'} {high:g}"
            raise ResearchConfigError(
                f"[overlay] {key} = {getattr(c, key)!r} is out of range (need {lo}{key}{hi})"
            )
    return c


def overlay_criteria_from_table(table: Any, *, what: str = "overlay criteria") -> OverlayCriteria:
    """Build :class:`OverlayCriteria` from a mapping (the TOML ``[overlay]`` table or a frozen
    ledger snapshot). :class:`ResearchConfigError` on a missing, unknown, mistyped or
    out-of-range key (fail closed)."""
    criteria = _section({"overlay": table}, "overlay", OverlayCriteria, what=what)
    return _check_overlay_ranges(criteria)


@dataclass(frozen=True)
class OverlayResearchConfig:
    """The ``"risk_overlay"`` question's frozen rows + budget (``sha256`` of the file bytes)."""

    criteria: OverlayCriteria
    budget: RunBudget
    sha256: str
    question: str = "risk_overlay"

    def to_json(self) -> str:
        return json.dumps(
            {
                "question": self.question,
                "overlay_criteria": asdict(self.criteria),
                "budget": asdict(self.budget),
            },
            sort_keys=True,
        )


OVERLAY_INSTALL_HINT = (
    "cp agents/config/overlay_criteria.proposed.toml lab/config/overlay_criteria.toml"
)


def load_overlay_config(path: Path | str = OVERLAY_CRITERIA_PATH) -> OverlayResearchConfig:
    """Parse the owner's overlay-criteria TOML (``[overlay]`` with every
    :class:`OverlayCriteria` key, ``[budget]`` with every :class:`RunBudget` key, nothing
    else). Raises :class:`ResearchConfigError` when the file is missing, is not TOML, has an
    unknown table, or any key is absent, unknown, mistyped or out of range (fail closed)."""
    p = Path(path)
    what = "overlay criteria file"
    if not p.is_file():
        raise ResearchConfigError(
            f"{p} not found, so there is no risk-overlay research. The owner installs it (in "
            f"their own terminal): {OVERLAY_INSTALL_HINT}"
        )
    data = p.read_bytes()
    try:
        raw = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ResearchConfigError(f"{p} is not valid TOML: {exc}") from exc
    unknown = sorted(set(raw) - {"overlay", "budget"})
    if unknown:
        raise ResearchConfigError(
            f"{what} has unknown tables or keys {unknown}; only [overlay] and [budget] are read"
        )
    if not isinstance(raw.get("overlay"), dict):
        raise ResearchConfigError(f"{what} has no [overlay] table")
    return OverlayResearchConfig(
        criteria=overlay_criteria_from_table(raw["overlay"], what=what),
        budget=_section(raw, "budget", RunBudget, what=what),
        sha256=hashlib.sha256(data).hexdigest(),
    )


# --------------------------------------------------------------------------- idea library

# Written by agents/ (research.ideas), read by lab.api. Append-only for sources; ideas change
# status as they are tested. ``dedupe_key`` is sha256 of the normalised hypothesis text.
IDEAS_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ideas (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL,
        run_id        TEXT,
        family        TEXT NOT NULL,
        title         TEXT NOT NULL,
        hypothesis    TEXT NOT NULL,
        rationale     TEXT NOT NULL,
        dedupe_key    TEXT NOT NULL UNIQUE,
        status        TEXT NOT NULL DEFAULT 'new',
        verdict_note  TEXT,
        plan_hashes   TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        idea_id              INTEGER REFERENCES ideas(id),
        created_at           TEXT NOT NULL,
        url                  TEXT NOT NULL,
        title                TEXT,
        screen_json          TEXT NOT NULL DEFAULT '{}',
        untrusted            INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS sources_idea ON sources (idea_id)",
)

IdeaStatus = Literal["new", "testing", "abandoned", "promising", "candidate", "dead", "duplicate"]
IDEA_STATUSES: tuple[str, ...] = (
    "new",
    "testing",
    "abandoned",
    "promising",
    "candidate",
    "dead",
    "duplicate",
)


# --------------------------------------------------------------------------- run state

RunPhase = Literal["starting", "scouting", "designing", "testing", "critic", "reporting", "done"]
RunOutcome = Literal["running", "candidate", "no_edge", "budget_exhausted", "error", "stopped"]


class BudgetUsage(TypedDict):
    hypotheses: int
    proposals: int
    backtests: int
    holdout_checks: int
    usd: float


class CandidateSummary(TypedDict):
    idea_id: int
    plan_hash: str
    plan: dict[str, Any]  # validated plan JSON, ready for POST /lab/backtest
    trial_id: int  # the design-window ledger trial
    summary: str


class RunEvent(TypedDict):
    at: str  # ISO-8601 UTC
    kind: str  # e.g. "scout", "design", "backtest", "critic", "decide", "holdout", "report"
    message: str


class RunState(TypedDict):
    """``RUN_STATE_DIR/<run_id>.json``, rewritten atomically (write temp + rename)."""

    run_id: str
    symbols: list[str]
    interval: str
    started_at: str
    updated_at: str
    phase: RunPhase
    outcome: RunOutcome
    budget: dict[str, float | int]  # RunBudget as a dict
    used: BudgetUsage
    current_idea_id: int | None
    candidates: list[CandidateSummary]
    journal_path: str | None  # relative to the repo root
    events: list[RunEvent]  # newest last, capped at 200


# --------------------------------------------------------------------------- paper trading


class TradeStats(TypedDict):
    n_trades: int
    total_return: float | None  # compounded, net of fees
    win_rate: float | None
    mean_trade_return: float | None


class PaperComparison(TypedDict):
    """``PAPER_DIR/<candidate>/comparison-<YYYY-MM-DD>.json``: freqtrade dry-run (forward)
    versus the lab engine replaying the same plan over the same period."""

    candidate: str
    plan_hash: str
    symbol: str
    interval: str
    period_start: int  # UNIX seconds
    period_end: int
    generated_at: str  # ISO-8601 UTC
    forward: TradeStats
    backtest: TradeStats
    entry_match_rate: float | None  # share of backtest entries with a forward entry within 1 bar
    mean_entry_slippage_bps: float | None  # forward fill vs the lab's next-bar open
    verdict: Literal["consistent", "diverging", "insufficient_data"]
    notes: list[str]
