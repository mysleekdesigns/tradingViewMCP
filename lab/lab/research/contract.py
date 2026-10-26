"""The shared Phase 8 contract between the lab (MCP server, API) and ``agents/``.

* Paths: where the success criteria, idea library, run-state files, and journal live.
* :class:`ResearchConfig`: success criteria and per-run budgets, loaded from
  ``lab/config/success_criteria.toml``. That file is owner-controlled (read-only to Claude
  while ``.claude/lab.lock`` exists) and is snapshotted into the ledger when a run starts, so
  the bar cannot move mid-run. A missing file is an error: research never runs without a bar.
* ``IDEAS_SCHEMA``: the idea-library SQLite schema (written by ``agents/``, read by the API).
* :class:`RunState`: the JSON progress file the orchestrator writes for the Research page.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from lab.config import LAB_ROOT

REPO_ROOT = LAB_ROOT.parent
SUCCESS_CRITERIA_PATH = LAB_ROOT / "config" / "success_criteria.toml"
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


def _section(raw: dict[str, Any], name: str, cls: type) -> Any:
    if name not in raw or not isinstance(raw[name], dict):
        raise ResearchConfigError(f"success criteria file has no [{name}] table")
    table = raw[name]
    fields = cls.__dataclass_fields__
    missing = sorted(set(fields) - set(table))
    extra = sorted(set(table) - set(fields))
    if missing or extra:
        raise ResearchConfigError(f"[{name}] missing keys {missing}, unknown keys {extra}")
    values: dict[str, Any] = {}
    for key, field in fields.items():
        v = table[key]
        kind = field.type
        if kind == "bool":
            ok = isinstance(v, bool)
        elif kind == "int":
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
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
