"""Per-run budgets, orchestrator side: hypotheses and dollars (PLAN.md Phase 8 Guardrails)."""

from __future__ import annotations

import pytest

from lab.research.contract import RunBudget
from research.budget import BudgetExceeded, BudgetExceededError, BudgetTracker


def _budget(**kw: float) -> RunBudget:
    base = {
        "max_hypotheses": 2,
        "max_proposals": 3,
        "max_backtests": 100,
        "max_holdout_checks": 1,
        "max_usd": 10.0,
    }
    base.update(kw)
    return RunBudget(**base)  # type: ignore[arg-type]


def test_per_call_cap_holds_back_a_fixed_reserve() -> None:
    t = BudgetTracker(_budget(max_usd=10.0))
    assert t.per_call_cap_usd() == pytest.approx(8.0)  # 10 - 20% of 10
    t.charge_usd(5.0)
    assert t.remaining_usd == pytest.approx(5.0)
    assert t.per_call_cap_usd() == pytest.approx(3.0)


def test_call_that_cannot_fit_is_refused_before_it_starts() -> None:
    t = BudgetTracker(_budget(max_usd=10.0))
    t.charge_usd(7.97)  # remaining 2.03, reserve 2.0 -> cap 0.03 < min 0.05
    with pytest.raises(BudgetExceededError) as exc:
        t.per_call_cap_usd()
    assert exc.value.reason == "usd"
    assert t.exhausted() == "usd"
    assert BudgetExceeded is BudgetExceededError


def test_overshoot_is_recorded_and_exhausts() -> None:
    t = BudgetTracker(_budget(max_usd=1.0))
    t.charge_usd(1.3)  # an SDK call overshot its cap
    assert t.remaining_usd == 0.0
    assert t.usage()["usd"] == pytest.approx(1.3)
    assert t.exhausted() == "usd"


def test_hypotheses_budget() -> None:
    t = BudgetTracker(_budget(max_hypotheses=2))
    t.start_hypothesis()
    t.start_hypothesis()
    assert t.hypotheses_left() == 0
    with pytest.raises(BudgetExceededError) as exc:
        t.start_hypothesis()
    assert exc.value.reason == "hypotheses"
    assert t.usage()["hypotheses"] == 2


def test_server_usage_mirror_reports_proposals_and_backtests() -> None:
    t = BudgetTracker(_budget(max_proposals=3, max_backtests=100))
    assert t.exhausted() is None
    t.update_server_usage({"proposals": 3, "backtests": 10, "holdout_checks": 0})
    assert t.exhausted() == "proposals"
    t.update_server_usage({"proposals": 1, "backtests": 100})
    assert t.exhausted() == "backtests"
    assert t.usage() == {
        "hypotheses": 0,
        "proposals": 1,
        "backtests": 100,
        "holdout_checks": 0,
        "usd": 0.0,
    }


def test_holdout_limit_defaults_to_frozen_budget_and_can_be_capped() -> None:
    assert BudgetTracker(_budget(max_holdout_checks=2)).holdout_left() == 2
    t = BudgetTracker(_budget(max_holdout_checks=2), holdout_limit=1)
    t.update_server_usage({"holdout_checks": 1})
    assert t.holdout_left() == 0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True, "1"])
def test_charge_rejects_bad_amounts(bad: object) -> None:
    with pytest.raises(ValueError, match="cost"):
        BudgetTracker(_budget()).charge_usd(bad)  # type: ignore[arg-type]


def test_zero_dollar_budget_refuses_every_call() -> None:
    with pytest.raises(BudgetExceededError):
        BudgetTracker(_budget(max_usd=0.0)).per_call_cap_usd()


def test_unknown_cost_is_charged_at_the_cap_never_zero() -> None:
    t = BudgetTracker(_budget(max_usd=10.0))
    assert t.charge_or_cap(0.4, 8.0) == pytest.approx(0.4)
    assert t.charge_or_cap(None, 3.0) == pytest.approx(3.0)
    assert t.charge_or_cap(float("nan"), 1.0) == pytest.approx(1.0)
    assert t.charge_or_cap(-1.0, 0.5) == pytest.approx(0.5)
    assert t.usd_spent == pytest.approx(4.9)
    with pytest.raises(ValueError, match="cap"):
        t.charge_or_cap(None, float("inf"))
