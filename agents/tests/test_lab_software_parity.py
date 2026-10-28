"""PLAN.md Phase 11 software record: lab and agents must resolve the same result-changing stack.

Research runs are started from ``agents/.venv`` (the Agent SDK loop calls the lab in-process)
but checked by the ``strategy-lab`` MCP server in ``lab/.venv``. ``lab.research.runs``
freezes :func:`software_values` (Optuna, vectorbt, numpy, pandas and Python versions) with the
run and refuses any later call whose installed versions differ *exactly*. If the two lockfiles
drift, a run started from one venv is refused in the other, so this test pins them together.

Offline: reads ``lab/uv.lock``, ``agents/uv.lock``, both ``pyproject.toml`` files, any
``.python-version`` files and (when both venvs exist) their ``pyvenv.cfg``. No network, no uv.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from lab.research.contract import REPO_ROOT
from lab.research.runs import software_values

PROJECTS = ("lab", "agents")
LOCKED_PACKAGES = ("optuna", "vectorbt", "numpy", "pandas")
NOT_A_PACKAGE = {"python", "tpe_sampler"}  # software_values keys checked another way
FIX = (
    "Upgrade both lockfiles together (`uv lock --upgrade-package <pkg>` in lab/ AND agents/, "
    "then `uv sync` in both) so research runs frozen in one venv are not refused in the other."
)


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _locked_versions(project: str) -> dict[str, list[str]]:
    lock = _toml(REPO_ROOT / project / "uv.lock")
    out: dict[str, list[str]] = {}
    for pkg in lock.get("package", []):
        out.setdefault(pkg["name"], []).append(str(pkg.get("version")))
    return out


def test_parity_list_covers_every_software_value() -> None:
    # A new result-changing package in software_values must be added here too.
    assert set(software_values()) - NOT_A_PACKAGE == set(LOCKED_PACKAGES)


def test_lab_and_agents_lock_the_same_result_changing_packages() -> None:
    locks = {p: _locked_versions(p) for p in PROJECTS}
    for name in LOCKED_PACKAGES:
        versions = {p: locks[p].get(name) for p in PROJECTS}
        for p, v in versions.items():
            assert v is not None, f"{name} is not in {p}/uv.lock. {FIX}"
            assert len(v) == 1, f"{p}/uv.lock locks several {name} versions {v}. {FIX}"
        assert versions["lab"] == versions["agents"], (
            f"{name}: lab/uv.lock {versions['lab'][0]} != agents/uv.lock "
            f"{versions['agents'][0]}. {FIX}"
        )


def test_lab_and_agents_pin_the_same_python() -> None:
    locks = {p: _toml(REPO_ROOT / p / "uv.lock")["requires-python"] for p in PROJECTS}
    assert locks["lab"] == locks["agents"], f"uv.lock requires-python differs: {locks}. {FIX}"
    assert locks["lab"].startswith("==3.12"), f"expected a single 3.12 minor, got {locks}"
    pyprojects = {
        p: _toml(REPO_ROOT / p / "pyproject.toml")["project"]["requires-python"] for p in PROJECTS
    }
    assert pyprojects["lab"] == pyprojects["agents"], (
        f"pyproject requires-python differs: {pyprojects}. Change both together."
    )
    pins = {
        p: (REPO_ROOT / p / ".python-version").read_text().strip()
        for p in PROJECTS
        if (REPO_ROOT / p / ".python-version").is_file()
    }
    assert pins, "neither lab/ nor agents/ has a .python-version file"
    assert len(set(pins.values())) == 1, f".python-version files disagree: {pins}"
    assert all(v.startswith("3.12") for v in pins.values()), f".python-version not 3.12: {pins}"


def _venv_python(project: str) -> str | None:
    cfg = REPO_ROOT / project / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    for line in cfg.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"version_info", "version"}:
            return value.strip()
    return None


def test_existing_lab_and_agents_venvs_run_the_same_python_patch_release() -> None:
    # software_values records platform.python_version() (X.Y.Z) and the check is exact, but the
    # lockfiles only pin the minor; compare the interpreters actually installed, when both exist.
    pythons = {p: _venv_python(p) for p in PROJECTS}
    installed = {p: v for p, v in pythons.items() if v is not None}
    assert "agents" in installed, "agents/.venv/pyvenv.cfg is missing; this test runs from it"
    if len(installed) == 2:
        assert installed["lab"] == installed["agents"], (
            f"lab/.venv runs Python {installed['lab']}, agents/.venv {installed['agents']}: "
            "recreate one venv on the same interpreter (`uv venv --python <X.Y.Z>` then "
            "`uv sync`) so frozen research runs are not refused."
        )
