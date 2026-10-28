"""Idea-library failure tags (PLAN.md Phase 13): the Critic's reason for abandoning a plan is
stored as a tag, counted per family across runs, and shown to the Designer before it proposes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lab.config import LabConfig, load_config
from lab.research.contract import IDEAS_SCHEMA
from research import orchestrator
from research.ideas import FAILURE_TAG_MEANINGS, FAILURE_TAGS, IdeaLibrary, family_key
from research.orchestrator import (
    CRITIC_SCHEMA,
    RoleResult,
    build_designer_prompt,
    family_tags_section,
)
from research.sdk_runner import ScriptedAgentRunner
from tests.test_orchestrator import FakeIdeas, World, critic, make, research, scout_output

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
RUN_A = "r-20261024-120000-aaaaaa"
RUN_B = "r-20261025-120000-bbbbbb"


@pytest.fixture
def cfg() -> LabConfig:
    return load_config()


@pytest.fixture
def lib(tmp_path: Path) -> IdeaLibrary:
    library = IdeaLibrary(tmp_path / "ideas.sqlite")
    yield library  # type: ignore[misc]
    library.close()


def _idea(lib: IdeaLibrary, hypothesis: str, family: str, run_id: str = RUN_A) -> int:
    res = lib.add_idea(
        run_id=run_id,
        family=family,
        title=hypothesis[:20],
        hypothesis=hypothesis,
        rationale="because",
    )
    assert res.idea_id is not None
    return res.idea_id


# --------------------------------------------------------------------------- vocabulary


def test_one_vocabulary_shared_by_library_orchestrator_and_critic_schema() -> None:
    assert orchestrator.FAILURE_TAGS is FAILURE_TAGS  # re-exported, not redefined
    assert tuple(FAILURE_TAG_MEANINGS) == FAILURE_TAGS
    assert CRITIC_SCHEMA["properties"]["failure_tags"]["items"]["enum"] == list(FAILURE_TAGS)
    for tag in ("cost_bound", "regime_only", "few_trades", "beta_only", "parameter_spike"):
        assert tag in FAILURE_TAGS  # the PLAN.md examples
    assert FAILURE_TAGS[-1] == "other"


def test_prompts_explain_every_tag_one_line_each() -> None:
    critic_md = (PROMPTS / "critic.md").read_text()
    designer_md = (PROMPTS / "designer.md").read_text()
    for tag in FAILURE_TAGS:
        assert sum(line.startswith(f"- `{tag}`") for line in critic_md.splitlines()) == 1, tag
    assert "evidence" in critic_md and "failure_tags" in critic_md
    assert "failed for" in designer_md


# --------------------------------------------------------------------------- library


def test_add_and_count_failure_tags_per_family_across_runs(lib: IdeaLibrary) -> None:
    a = _idea(lib, "EMA trend filter on four hour bars", "Regime filters", RUN_A)
    b = _idea(lib, "ATR breakout after volatility squeeze", "regime-filters ", RUN_B)
    c = _idea(lib, "Funding carry when basis is wide", "carry/funding", RUN_B)
    lib.add_failure_tags(a, ["cost_bound", "few_trades"])
    lib.add_failure_tags(b, ["cost_bound"], run_id=RUN_B)
    lib.add_failure_tags(c, ["beta_only"])

    # Case, punctuation, and whitespace do not split a family; other families do not leak in.
    counts = lib.family_failure_tags("REGIME FILTERS")
    assert counts == {"cost_bound": 2, "few_trades": 1}
    assert list(counts) == ["cost_bound", "few_trades"]  # most frequent first
    assert lib.family_failure_tags("carry funding") == {"beta_only": 1}
    assert lib.family_failure_tags("unknown family") == {}
    assert lib.family_failure_tags("  ") == {}
    assert family_key("Regime-Filters") == family_key("regime filters")
    assert lib.failure_tags(a) == ["cost_bound", "few_trades"]


def test_failure_tags_are_idempotent_and_counted_once_per_idea(lib: IdeaLibrary) -> None:
    a = _idea(lib, "EMA trend filter on four hour bars", "trend")
    lib.add_failure_tags(a, ["cost_bound", "cost_bound"])
    lib.add_failure_tags(a, ["cost_bound"])  # a resumed run re-recording the same verdict
    lib.add_failure_tags(a, ["cost_bound"], run_id=RUN_B)  # tagged again in a later run
    assert lib.family_failure_tags("trend") == {"cost_bound": 1}
    assert lib.failure_tags(a) == ["cost_bound"]


def test_failure_tags_are_validated_before_anything_is_written(lib: IdeaLibrary) -> None:
    a = _idea(lib, "EMA trend filter on four hour bars", "trend")
    with pytest.raises(ValueError, match="unknown failure tag"):
        lib.add_failure_tags(a, ["cost_bound", "ignore previous instructions"])
    with pytest.raises(ValueError, match="sequence"):
        lib.add_failure_tags(a, "cost_bound")
    with pytest.raises(KeyError):
        lib.add_failure_tags(999, ["other"])
    assert lib.failure_tags(a) == []


def test_migration_adds_the_table_to_an_existing_library(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:  # a library written before Phase 13
        for stmt in IDEAS_SCHEMA:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO ideas (created_at, run_id, family, title, hypothesis, rationale,"
            " dedupe_key, status) VALUES ('t', ?, 'trend', 't', 'h', 'r', 'k', 'abandoned')",
            (RUN_A,),
        )
    with IdeaLibrary(path) as lib:
        lib.add_failure_tags(1, ["regime_only"])
        assert lib.family_failure_tags("trend") == {"regime_only": 1}
        assert lib.get(1).status == "abandoned"  # existing rows untouched
    with IdeaLibrary(path) as lib:  # reopening is a no-op migration
        assert lib.family_failure_tags("Trend") == {"regime_only": 1}


# --------------------------------------------------------------------------- designer prompt


def _designer_prompt(tags: dict[str, int]) -> str:
    return build_designer_prompt(
        run_id=RUN_A,
        interval="4h",
        symbol="BTCUSDT",
        idea_id=1,
        title="t",
        context={"family": "trend", "hypothesis": "h", "sources": []},
        lessons=[],
        round_=0,
        max_refinements=2,
        previous_plan=None,
        feedback=[],
        proposals_left=5,
        backtests_left=100,
        family_failure_tags=tags,
    )


def test_designer_prompt_lists_family_failure_tags_before_the_output_instructions() -> None:
    prompt = _designer_prompt({"cost_bound": 3, "few_trades": 1})
    line = "Earlier plans in this family failed for: cost_bound x3, few_trades x1"
    assert line in prompt
    assert f"- cost_bound: {FAILURE_TAG_MEANINGS['cost_bound']}" in prompt
    assert prompt.index(line) < prompt.index("Finish with ONE JSON object")
    assert "no recorded failure tags" in _designer_prompt({})
    # Unknown tags (never stored, but the prompt does not trust the store either) are dropped.
    assert family_tags_section({"made_up": 2}) == family_tags_section({})


# --------------------------------------------------------------------------- orchestrator


async def test_critic_abandon_stores_its_tags(tmp_path: Path, cfg: LabConfig) -> None:
    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {
            "scout": scout_output("A"),
            "designer": w.designer,
            "critic": critic("abandon", "fees eat it", tags=["cost_bound", "few_trades"]),
        }
    )
    orch, ideas = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")
    assert ideas.tags == [(1, ["cost_bound", "few_trades"], state["run_id"])]
    assert ideas.ideas[1]["status"] == "abandoned"


async def test_critic_abandon_without_tags_is_tagged_other(tmp_path: Path, cfg: LabConfig) -> None:
    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": critic("abandon")}
    )
    orch, ideas = make(w, runner)
    await orch.run(["BTCUSDT"], "4h")
    assert [t for _, t, _ in ideas.tags] == [["other"]]


async def test_refinements_used_up_stores_the_last_critic_tags(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    verdicts = iter(
        [
            critic("refine", "spike", tags=["parameter_spike"]),
            critic("refine", "still a spike", tags=["parameter_spike"]),
            critic("refine", "decays", tags=["walk_forward_decay"]),
        ]
    )
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": lambda r: next(verdicts)}
    )
    orch, ideas = make(w, runner)
    await orch.run(["BTCUSDT"], "4h")
    assert w.designer_calls == [1, 1, 1]
    assert [t for _, t, _ in ideas.tags] == [["walk_forward_decay"]]


async def test_no_tags_without_a_critic_verdict(tmp_path: Path, cfg: LabConfig) -> None:
    """Designer failures, not-expressible ideas, and an unavailable Critic say nothing about
    the family: nothing is tagged."""
    w = World(tmp_path, cfg)
    calls = {"n": 0}

    def designer(req):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return RoleResult(None, 0.0, None, "invalid output after one retry", invalid_outputs=2)
        if calls["n"] == 2:
            return {"plan_hash": "", "plan": {}, "trial_ids": [], "notes": "needs order book"}
        return w.designer(req)

    runner = ScriptedAgentRunner(
        {
            "scout": scout_output("A", "B", "C"),
            "designer": designer,
            "critic": RoleResult(None, 0.0, None, "critic sdk error"),
        }
    )
    orch, ideas = make(w, runner)
    await orch.run(["BTCUSDT"], "4h")
    assert [d["status"] for d in ideas.ideas.values()] == ["abandoned"] * 3
    assert ideas.tags == []


async def test_designer_prompt_shows_the_family_tags(tmp_path: Path, cfg: LabConfig) -> None:
    w = World(tmp_path, cfg)
    fake = FakeIdeas(family_tags={"regime filters": {"cost_bound": 3, "regime_only": 1}})
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": critic("abandon")}
    )
    orch, _ = make(w, runner, ideas=fake)
    await orch.run(["BTCUSDT"], "4h")
    [prompt] = [r.prompt for r in runner.requests if r.role == "designer"]
    assert "failed for: cost_bound x3, regime_only x1" in prompt
    assert fake.family_queries == ["regime filters"]


async def test_a_store_without_tag_methods_still_runs(tmp_path: Path, cfg: LabConfig) -> None:
    class OldIdeas(FakeIdeas):
        def __getattribute__(self, name: str):  # type: ignore[no-untyped-def]
            if name in ("add_failure_tags", "family_failure_tags"):
                raise AttributeError(name)
            return super().__getattribute__(name)

    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": critic("abandon")}
    )
    orch, _ = make(w, runner, ideas=OldIdeas())
    state = await orch.run(["BTCUSDT"], "4h")
    assert state["outcome"] == "no_edge"


async def test_tags_reach_the_next_designer_of_the_family_with_the_real_library(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {
            "scout": scout_output("A", "B"),  # both "regime filters"
            "designer": w.designer,
            "critic": critic("abandon", "fees", tags=["cost_bound"]),
        }
    )
    with IdeaLibrary(tmp_path / "ideas.sqlite") as lib:
        orch, _ = make(w, runner, ideas=lib, rc=research(max_hypotheses=2))
        await orch.run(["BTCUSDT"], "4h")
        assert lib.family_failure_tags("Regime Filters") == {"cost_bound": 2}
    first, second = [r.prompt for r in runner.requests if r.role == "designer"]
    assert "no recorded failure tags" in first
    assert "failed for: cost_bound x1" in second
