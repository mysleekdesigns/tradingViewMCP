"""Golden snapshot of an edge run's ``get_report`` / ``get_budget`` / ``holdout_check`` output.

PLAN.md Phase 15: "An edge run's report is unchanged, which a test checks" and Done when "An
edge run's report is byte-identical to before." The fixture was captured at HEAD before any
Phase 15 code landed (commit 7680942), so any later change to an edge run's criteria rows,
wording, numbers, budget or holdout output shows up here as a diff.

Two edge runs through the real MCP server (offline, seeded synthetic candles, tmp ledger):

- ``lax``: the LAX criteria on ``HOLDOUT_PASS_CANDLES``: full pipeline, pre-holdout report and
  budget, a passing holdout check, then the report and budget after it.
- ``strict``: the proposed criteria file as shipped on the default ``trend_candles()``: full
  pipeline, report and budget, and the holdout refusal text (pre-holdout criteria fail).

Serialised with ``json.dumps(obj, sort_keys=True, indent=1)``. Stripped / pinned, and why:

- the run id: ``lab.research.runs.new_run_id`` uses the wall clock and ``secrets.token_hex``,
  so it is pinned to ``RUN_ID`` (it appears in ``run_id`` and in the CPCV ``configs_source``
  wording, both kept);
- the pytest ``tmp_path``: absolute and different every run, replaced by ``<TMP>`` (no output
  contains it at capture time; this only keeps a future path from making the test flaky).

Nothing else is stripped. Ledger ``created_at`` stamps (``ledger._now_iso``) and runner
``elapsed_ms`` never reach these outputs, so they need no handling. Every float is kept at
full ``repr`` precision; the numbers depend on the pinned numpy / pandas / vectorbt / optuna
versions in ``uv.lock``.

Regenerate only when the fixture is absent (the protect-files hook freezes existing fixtures):
``LAB_REGEN_GOLDEN=1 uv run pytest -q tests/test_report_golden.py``.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

import pytest

import lab.research.runs as runs
from tests.test_mcp_server import (
    HOLDOUT_PASS_CANDLES,
    LAX,
    TREND_PLAN,
    Lab,
    _full_pipeline,
    error_text,
    ok,
    trend_candles,
)

FIXTURE = Path(__file__).parent / "fixtures" / "edge_report_golden.json"
RUN_ID = "r-20261029-000000-abc123"
REGEN_ENV = "LAB_REGEN_GOLDEN"


def _lax_run(tmp_path: Path) -> dict[str, Any]:
    lab = Lab(tmp_path, candles=HOLDOUT_PASS_CANDLES, criteria=LAX)
    run_id = lab.start()
    assert run_id == RUN_ID
    h = _full_pipeline(lab, run_id, TREND_PLAN)
    report_pre = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert report_pre["pre_holdout_passed"] is True, report_pre["rows"]
    budget_pre = ok(lab.server, "get_budget", {"run_id": run_id})
    holdout = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    return {
        "report_pre_holdout": report_pre,
        "budget_pre_holdout": budget_pre,
        "holdout_check": holdout,
        "report_post_holdout": ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h}),
        "budget_post_holdout": ok(lab.server, "get_budget", {"run_id": run_id}),
    }


def _strict_run(tmp_path: Path) -> dict[str, Any]:
    lab = Lab(tmp_path, candles=trend_candles())  # the proposed criteria, unchanged
    run_id = lab.start()
    assert run_id == RUN_ID
    h = _full_pipeline(lab, run_id, TREND_PLAN)
    report = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert report["pre_holdout_passed"] is False, report["rows"]
    return {
        "report": report,
        "budget": ok(lab.server, "get_budget", {"run_id": run_id}),
        "holdout_refusal": error_text(
            lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN}
        ),
    }


def _snapshot(tmp_path: Path) -> str:
    out = {
        "lax": _lax_run(tmp_path / "lax"),
        "strict": _strict_run(tmp_path / "strict"),
    }
    text = json.dumps(out, sort_keys=True, indent=1) + "\n"
    return text.replace(str(tmp_path), "<TMP>")


def test_edge_report_matches_the_golden_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "new_run_id", lambda now=None: RUN_ID)
    (tmp_path / "lax").mkdir()
    (tmp_path / "strict").mkdir()
    actual = _snapshot(tmp_path)

    if not FIXTURE.exists():
        if os.environ.get(REGEN_ENV) == "1":
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(actual)
            pytest.fail(f"wrote {FIXTURE.name}; re-run without {REGEN_ENV} to compare against it")
        pytest.fail(f"{FIXTURE} is missing; it is captured once with {REGEN_ENV}=1")

    expected = FIXTURE.read_text()
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile="golden",
                tofile="actual",
                n=2,
            )
        )
        pytest.fail(f"an edge run's report changed (PLAN.md Phase 15):\n{diff[:6000]}")
