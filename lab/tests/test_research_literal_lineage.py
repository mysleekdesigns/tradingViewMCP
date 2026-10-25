"""Literal lineage over a broad family (quant-review round 4, W2 / recurring issue 29).

At the family threshold (0.8) genuinely different trend followers merge (EMA cross vs
``roc > 0`` ~0.88 on BTC 4h), so one hand-tuned ``roc > 0`` / ``roc > 1`` used to block the
walk-forward of every trend follower for the ledger's life. The cross-logic literal rule now
applies only to family logics with a plan correlated >= lab.toml ``[research]
literal_lineage_corr`` (0.95), while the plan's own logic keeps it unconditionally. A no-op or
cosmetic copy of the tuned logic correlates ~1.0, so it is still blocked. Offline: hand-built
ledger trials whose returns have known correlations."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lab.config import DEFAULT_CONFIG_PATH, LabConfig, load_config
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash
from lab.research.family import (
    FamilyInfo,
    behaviour_family,
    clear_family_cache,
    tried_literals,
    tried_ranges,
    wf_lineage_problems,
)
from lab.research.logic import literal_vector, logic_hash
from tests.test_mcp_server import TREND_PLAN, small_cfg
from tests.test_research_criteria import trial
from tests.test_research_family import start

N = 600
EMA_SPACE = {"ema_fast.period": (10, 30), "ema_slow.period": (30, 80)}


def roc(threshold: float, exit_: str = "m < 0") -> dict[str, Any]:
    return {
        **TREND_PLAN,
        "name": f"roc_{threshold:g}",
        "features": {"m": {"roc": {"period": 10}}},
        "long_entry": f"m > {threshold:g}",
        "long_exit": exit_,
    }


ROC_NOOP = roc(1, exit_="m < 0 or m < -1000")  # roc >= -100, so the clause never fires


def _h(p: dict[str, Any]) -> str:
    return plan_hash(parse_plan(p))


def mix(base: np.ndarray, corr: float, rng: np.random.Generator) -> np.ndarray:
    """A series whose population correlation with ``base`` (unit-scaled) is ``corr``."""
    noise = rng.normal(0.0, base.std(), base.size)
    return corr * base + np.sqrt(1.0 - corr**2) * noise


def corr(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.corrcoef(a, b)[0, 1])


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    clear_family_cache()
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def seed_trend_family(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> dict[str, Any]:
    """EMA trend plan + ``roc > 0`` and ``roc > 1`` (one logic, two hand-tuned literals), each
    roc plan correlated ~0.88 with the EMA plan: in its family, below the literal bar."""
    run = start(ledger, tmp_path)
    rng = np.random.default_rng(29)
    r_ema = rng.normal(0.0005, 0.01, N)
    r0, r1 = mix(r_ema, 0.88, rng), mix(r_ema, 0.88, rng)
    trial(ledger, run, r_ema, plan_dict=TREND_PLAN, cfg=cfg)
    trial(ledger, run, r0, plan_dict=roc(0), cfg=cfg)
    trial(ledger, run, r1, plan_dict=roc(1), cfg=cfg)
    return {"run": run, "ema": r_ema, "roc0": r0, "roc1": r1}


def test_lab_toml_literal_lineage_bar_is_point_nine_five() -> None:
    raw = tomllib.loads(DEFAULT_CONFIG_PATH.read_text())
    assert raw["research"]["literal_lineage_corr"] == 0.95
    assert load_config().research.literal_lineage_corr == 0.95


@pytest.mark.parametrize("bad", ["0", "1.5", "-0.2", "true", '"high"'])
def test_literal_lineage_corr_must_be_in_the_unit_interval(tmp_path: Path, bad: str) -> None:
    raw = DEFAULT_CONFIG_PATH.read_text()
    path = tmp_path / "lab.toml"
    path.write_text(raw.replace("literal_lineage_corr = 0.95", f"literal_lineage_corr = {bad}"))
    with pytest.raises(ValueError, match=r"research\.literal_lineage_corr"):
        load_config(path)


def test_a_tuned_roc_literal_no_longer_blocks_an_ema_walk_forward(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    s = seed_trend_family(ledger, tmp_path, cfg)
    assert 0.8 <= corr(s["ema"], s["roc0"]) < 0.95 and 0.8 <= corr(s["ema"], s["roc1"]) < 0.95
    plan = parse_plan(TREND_PLAN)
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=plan)
    lh_roc = logic_hash(roc(0))
    assert lh_roc == logic_hash(roc(1)) and literal_vector(roc(0)) != literal_vector(roc(1))
    assert lh_roc in fam.logic_hashes  # still ONE hypothesis for the holdout and the WF row
    assert fam.literal_kin == {logic_hash(plan)}  # but its literals are not this plan's
    lits = tried_literals(ledger, fam, plan)
    assert lits == {logic_hash(plan): {()}}
    assert wf_lineage_problems(plan, EMA_SPACE, tried_ranges(ledger, fam, plan), lits) == []


def test_the_old_all_family_rule_still_applies_when_the_bar_is_the_family_threshold(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """Control: with literal_lineage_corr at 0.8 the same ledger blocks the walk-forward, so the
    previous test passes because of the new key, not because the roc plans left the family."""
    seed_trend_family(ledger, tmp_path, cfg)
    loose = replace(cfg, research=replace(cfg.research, literal_lineage_corr=0.8))
    plan = parse_plan(TREND_PLAN)
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=loose, plan=plan)
    assert logic_hash(roc(0)) in (fam.literal_kin or set())
    problems = wf_lineage_problems(
        plan, EMA_SPACE, tried_ranges(ledger, fam, plan), tried_literals(ledger, fam, plan)
    )
    assert len(problems) == 1 and problems[0].startswith("thresholds were hand-tuned")


def test_a_no_op_copy_of_the_tuned_roc_logic_is_still_blocked(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    s = seed_trend_family(ledger, tmp_path, cfg)
    # the no-op copy trades exactly like roc > 1
    trial(ledger, s["run"], s["roc1"], plan_dict=ROC_NOOP, cfg=cfg)
    plan = parse_plan(ROC_NOOP)
    assert logic_hash(plan) != logic_hash(roc(1))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(ROC_NOOP), cfg=cfg, plan=plan)
    assert logic_hash(roc(1)) in (fam.literal_kin or set())
    lits = tried_literals(ledger, fam, plan)
    assert lits[logic_hash(roc(1))] == {literal_vector(roc(0)), literal_vector(roc(1))}
    problems = wf_lineage_problems(
        plan, {"m.period": (5, 20)}, tried_ranges(ledger, fam, plan), lits
    )
    assert len(problems) == 1 and problems[0].startswith("thresholds were hand-tuned")


def test_the_literal_bar_uses_the_best_plan_of_a_logic_not_the_first_one_compared(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """The newest roc plan (compared first) admits the logic at ~0.88; an older plan of the
    same logic is the exact twin. The family keeps comparing until one reaches the bar."""
    run = start(ledger, tmp_path)
    rng = np.random.default_rng(30)
    r_twin = rng.normal(0.0005, 0.01, N)
    trial(ledger, run, r_twin, plan_dict=roc(1), cfg=cfg)  # older: exact twin of the no-op
    trial(ledger, run, mix(r_twin, 0.88, rng), plan_dict=roc(0), cfg=cfg)  # newer: 0.88
    trial(ledger, run, r_twin, plan_dict=ROC_NOOP, cfg=cfg)
    plan = parse_plan(ROC_NOOP)
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(ROC_NOOP), cfg=cfg, plan=plan)
    lh = logic_hash(roc(0))
    assert fam.correlated[lh] < 0.95  # admitted by the first (newest) plan compared
    assert lh in (fam.literal_kin or set())  # but the twin reached the literal bar
    lits = tried_literals(ledger, fam, plan)
    assert "hand-tuned" in wf_lineage_problems(plan, {"m.period": (5, 20)}, {}, lits)[0]


def test_the_plans_own_logic_keeps_the_rule_unconditionally(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """roc > 0 then roc > 1 with unrelated returns (correlation ~0): a walk-forward of roc > 1
    is refused anyway, because its own logic was hand-tuned."""
    run = start(ledger, tmp_path)
    rng = np.random.default_rng(31)
    trial(ledger, run, rng.normal(0, 0.01, N), plan_dict=roc(0), cfg=cfg)
    trial(ledger, run, rng.normal(0, 0.01, N), plan_dict=roc(1), cfg=cfg)
    plan = parse_plan(roc(1))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(roc(1)), cfg=cfg, plan=plan)
    assert fam.literal_kin == {logic_hash(plan)}
    lits = tried_literals(ledger, fam, plan)
    assert lits == {logic_hash(plan): {literal_vector(roc(0)), literal_vector(roc(1))}}
    assert "hand-tuned" in wf_lineage_problems(plan, {"m.period": (5, 20)}, {}, lits)[0]


def test_a_hand_built_family_without_literal_kin_keeps_every_logic(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """literal_kin None (the dataclass default) is the conservative old rule."""
    seed_trend_family(ledger, tmp_path, cfg)
    plan = parse_plan(TREND_PLAN)
    own, other = logic_hash(plan), logic_hash(roc(0))
    fam = FamilyInfo(logic_hashes={own, other}, plan_hashes={_h(TREND_PLAN)})
    assert set(tried_literals(ledger, fam, plan)) == {own, other}
