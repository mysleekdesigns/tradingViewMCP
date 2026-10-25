"""Orchestrator-side budget enforcement for one research run (PLAN.md Phase 8 Guardrails).

The MCP server enforces proposals, backtests, and holdout checks on its own (agents cannot
bypass it). This tracker enforces the two dimensions only the orchestrator can see,
**hypotheses** and **model dollars**, and mirrors the server-side counts (read from the ledger
with :func:`lab.research.runs.run_usage`) so the loop can stop cleanly before the server refuses.

The limits always come from the run's frozen snapshot (:func:`lab.research.runs.frozen_budget`),
never from CLI flags, so nothing can raise a budget mid-run.

Dollar rule: before every model call the orchestrator asks :meth:`BudgetTracker.per_call_cap_usd`
for the call's ``max_budget_usd``. The cap is ``remaining - headroom_fraction * max_usd``: a
fixed reserve is held back because the SDK's limit is a client-side estimate that can overshoot
by up to one turn. If the cap is below ``min_call_usd`` the call is refused **before** it starts
(:class:`BudgetExceededError`).

Unknown costs fail closed: a call whose cost was not reported (or that raised after starting)
is charged its full cap (:meth:`BudgetTracker.charge_or_cap`), never zero. The orchestrator also
persists each call's cap as a reservation before the call, so a crash mid-call is charged at the
cap on ``--resume``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from lab.research.contract import BudgetUsage, RunBudget

DEFAULT_HEADROOM_FRACTION = 0.2
DEFAULT_MIN_CALL_USD = 0.05


class BudgetExceededError(RuntimeError):
    """A step would exceed the run's frozen budget. Nothing was started."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# Name used in the work-package spec.
BudgetExceeded = BudgetExceededError


def _check_amount(x: float, name: str) -> float:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) or x < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {x!r}")
    return float(x)


@dataclass
class BudgetTracker:
    """Tracks one run's spend against its frozen :class:`RunBudget`."""

    budget: RunBudget
    usd_spent: float = 0.0
    hypotheses_started: int = 0
    # Effective holdout limit (frozen budget capped by lab.toml); defaults to the frozen budget.
    holdout_limit: int | None = None
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION
    min_call_usd: float = DEFAULT_MIN_CALL_USD
    server_usage: dict[str, int] = field(
        default_factory=lambda: {"proposals": 0, "backtests": 0, "holdout_checks": 0}
    )

    def __post_init__(self) -> None:
        _check_amount(self.usd_spent, "usd_spent")
        if not 0.0 <= self.headroom_fraction < 1.0:
            raise ValueError(f"headroom_fraction must be in [0, 1), got {self.headroom_fraction}")
        _check_amount(self.min_call_usd, "min_call_usd")
        if self.holdout_limit is None:
            self.holdout_limit = self.budget.max_holdout_checks

    # ------------------------------------------------------------------ dollars

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget.max_usd - self.usd_spent)

    @property
    def reserve_usd(self) -> float:
        return self.headroom_fraction * self.budget.max_usd

    def charge_usd(self, x: float) -> None:
        """Record the actual cost of a finished call (may exceed the cap; then exhausted)."""
        self.usd_spent += _check_amount(x, "cost")

    def charge_or_cap(self, cost: float | None, cap: float) -> float:
        """Charge ``cost`` when it is a finite number >= 0, otherwise ``cap`` (an unknown cost is
        charged in full). Returns the amount charged."""
        known = (
            float(cost)
            if isinstance(cost, int | float)
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
            else None
        )
        amount = _check_amount(cap, "cap") if known is None else known
        self.usd_spent += amount
        return amount

    def per_call_cap_usd(self) -> float:
        """The ``max_budget_usd`` for the next model call. Raises :class:`BudgetExceededError`
        when the call cannot fit inside the remaining budget minus the reserve."""
        cap = self.remaining_usd - self.reserve_usd
        if cap < self.min_call_usd:
            raise BudgetExceededError(
                "usd",
                f"model budget exhausted: spent ${self.usd_spent:.2f} of "
                f"${self.budget.max_usd:.2f} (reserve ${self.reserve_usd:.2f} held back for "
                "SDK overshoot); refusing to start another call",
            )
        return cap

    def can_afford_call(self) -> bool:
        try:
            self.per_call_cap_usd()
        except BudgetExceededError:
            return False
        return True

    # ------------------------------------------------------------------ hypotheses

    def hypotheses_left(self) -> int:
        return max(0, self.budget.max_hypotheses - self.hypotheses_started)

    def start_hypothesis(self) -> None:
        if self.hypotheses_left() <= 0:
            raise BudgetExceededError(
                "hypotheses",
                f"hypothesis budget exhausted ({self.budget.max_hypotheses} per run)",
            )
        self.hypotheses_started += 1

    # ------------------------------------------------------------------ server-side mirror

    def update_server_usage(self, usage: Mapping[str, int]) -> None:
        """Mirror :func:`lab.research.runs.run_usage` (the ledger is the source of truth)."""
        for key in ("proposals", "backtests", "holdout_checks"):
            if key in usage:
                self.server_usage[key] = int(usage[key])

    def proposals_left(self) -> int:
        return max(0, self.budget.max_proposals - self.server_usage["proposals"])

    def backtests_left(self) -> int:
        return max(0, self.budget.max_backtests - self.server_usage["backtests"])

    def holdout_left(self) -> int:
        limit = self.holdout_limit if self.holdout_limit is not None else 0
        return max(0, limit - self.server_usage["holdout_checks"])

    def exhausted(self) -> str | None:
        """The reason the run cannot start another design step, or None. Order: dollars,
        proposals, backtests. (Hypotheses and holdout checks are checked separately: running out
        of them ends the run's scope rather than cutting it short.)"""
        if not self.can_afford_call():
            return "usd"
        if self.proposals_left() <= 0:
            return "proposals"
        if self.backtests_left() <= 0:
            return "backtests"
        return None

    def usage(self) -> BudgetUsage:
        return BudgetUsage(
            hypotheses=self.hypotheses_started,
            proposals=self.server_usage["proposals"],
            backtests=self.server_usage["backtests"],
            holdout_checks=self.server_usage["holdout_checks"],
            usd=round(self.usd_spent, 6),
        )
