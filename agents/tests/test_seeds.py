"""Seed importer: every seed validates, malformed seeds are named, calibration records evidence
under a fresh research run, writes the report, captures per-seed errors, never touches the
holdout. Offline: seeded synthetic candles, tmp ledger and journal."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import research.seeds as seeds_mod
from lab.config import LabConfig, load_config
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash, validate_plan
from lab.plan.schema import MAX_FEATURE_PARAMS, MAX_FEATURES
from lab.registry import REGISTRY
from lab.research.contract import (
    RESEARCH_RUN_ID_PATTERN,
    ResearchConfig,
    RunBudget,
    SuccessCriteria,
)
from lab.runner import DataUnavailableError
from research.seeds import EXPECTATION, Seed, load_seeds, run_seed_calibration

SOURCE_PREFIXES = (
    "https://raw.githubusercontent.com/freqtrade/freqtrade/",
    "https://github.com/freqtrade/freqtrade-strategies/",
)


def make_candles(n: int, *, seed: int, start: str, interval_s: int = 3600) -> pd.DataFrame:
    """Seeded geometric random walk OHLCV on a regular UTC grid, all bars tradable."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(
        pd.Timestamp(start, tz="UTC"), periods=n, freq=pd.Timedelta(seconds=interval_s), name="time"
    )
    close = 20_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.006, n)))
    open_ = np.empty(n)
    open_[0] = 20_000.0
    open_[1:] = close[:-1] * np.exp(rng.normal(0.0, 0.0006, n - 1))
    spread = np.abs(rng.normal(0.0, 0.004, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(10.0, 100.0, n),
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


def research_config() -> ResearchConfig:
    return ResearchConfig(
        criteria=SuccessCriteria(
            wf_oos_sharpe_min=1.0,
            dsr_min=0.95,
            pbo_max=0.2,
            beat_buy_hold=True,
            beat_vol_target=True,
            random_entry_percentile_min=95.0,
            cost_stress_factor=2.0,
            require_gradual_delay_decay=True,
            max_drawdown_max=0.25,
            min_trades=100,
            min_profitable_regimes=2,
            regime_window_days=90,
            regime_threshold=0.10,
            neighbourhood_pct=0.2,
            require_holdout_pass=True,
            require_crosscheck_match=True,
        ),
        budget=RunBudget(
            max_hypotheses=5,
            max_proposals=15,
            max_backtests=3000,
            max_holdout_checks=1,
            max_usd=20.0,
        ),
        sha256="0" * 64,
    )


# 2018-12 .. ~2019-10 hourly: starts before design_start (2019-01-01) so warm-up exists.
CANDLES = make_candles(24 * 300, seed=11, start="2018-12-01")


def fake_loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
    if symbol != "BTCUSDT":
        raise DataUnavailableError(f"no archive data for {symbol} {interval}")
    return CANDLES.loc[CANDLES.index < end]


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path,
        splits=replace(base.splits, wf_train_months=4, wf_test_months=2, wf_step_months=2),
        benchmarks=replace(base.benchmarks, random_entry_paths=10),
        stats=replace(
            base.stats,
            bootstrap_samples=50,
            monte_carlo_paths=20,
            permutation_paths=5,
            neighbourhood_max_trials=4,
        ),
    )


@pytest.fixture
def ledger(tmp_path: Path):  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


# --------------------------------------------------------------------------- loading


def test_all_seeds_load_and_validate() -> None:
    seeds = load_seeds()
    assert len(seeds) >= 5
    names = {s.plan["name"] for s in seeds}
    assert len(names) == len(seeds), "seed plan names must be unique"
    for s in seeds:
        plan, errors = validate_plan(s.plan)
        assert plan is not None, (s.name, errors)
        assert s.source_url.startswith(SOURCE_PREFIXES), s.source_url
        assert len(s.translation_notes) > 40
        assert len(plan.features) <= MAX_FEATURES
        n_params = sum(len(REGISTRY[f.feature].params) for f in plan.features)
        assert n_params <= MAX_FEATURE_PARAMS
        assert plan.symbol == "BTCUSDT"
        assert plan.interval in ("1h", "4h")
        assert plan.risk.stop_loss_pct is not None  # every freqtrade strategy has a stoploss


def test_expected_seeds_present() -> None:
    urls = " ".join(s.source_url for s in load_seeds())
    for name in ("sample_strategy.py", "Strategy001", "Strategy002", "Strategy003", "Bandtastic"):
        assert name in urls


def test_bandtastic_uses_two_band_aliases_and_keeps_stoploss() -> None:
    band = next(s for s in load_seeds() if "Bandtastic" in s.source_url)
    plan = parse_plan(band.plan)
    mults = sorted(float(f.params["mult"]) for f in plan.features if f.feature == "bollinger")
    assert mults == [1.0, 2.0]
    assert plan.risk.stop_loss_pct == 34.5  # inside the (0, 50] bound: no clamping


def _write(tmp_path: Path, name: str, payload: Any) -> Path:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _good_seed() -> dict[str, Any]:
    return json.loads((seeds_mod.SEED_DIR / "freqtrade_strategy001.json").read_text())


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ("{not json", "cannot read JSON"),
        (["a list"], "top level must be an object"),
        ({"name": "x", "source_url": "u", "plan": {}}, "missing keys ['translation_notes']"),
        ("BAD_PLAN", "invalid plan"),
        ("EXTRA_KEY", "unknown keys ['extra']"),
    ],
)
def test_malformed_seed_names_the_file(tmp_path: Path, payload: Any, fragment: str) -> None:
    _write(tmp_path, "a_good.json", _good_seed())
    if payload == "BAD_PLAN":
        bad = _good_seed()
        bad["plan"]["features"]["ema20"] = {"tema": {"period": 9}}
        payload = bad
    elif payload == "EXTRA_KEY":
        payload = {**_good_seed(), "extra": 1}
    _write(tmp_path, "z_broken.json", payload)
    with pytest.raises(ValueError, match=r"z_broken\.json") as info:
        load_seeds(tmp_path)
    assert fragment in str(info.value)


def test_empty_seed_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no seed files"):
        load_seeds(tmp_path)


# --------------------------------------------------------------------------- calibration


def _fake_evaluate(calls: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
    def evaluate(ledger: Ledger, run_id: str, plan_hash_: str, cfg: LabConfig) -> dict[str, Any]:
        calls.append((run_id, plan_hash_))
        kinds = {e["kind"] for e in ledger.evidence(run_id, plan_hash=plan_hash_)}
        assert kinds == {"backtest", "adversarial"}  # evidence recorded before judging
        return {
            "run_id": run_id,
            "plan_hash": plan_hash_,
            "pre_holdout_passed": False,
            "passed": False,
            "rows": [
                {
                    "criterion": "dsr_min",
                    "status": "fail",
                    "value": 0.12,
                    "threshold": 0.95,
                    "note": "",
                },
                {
                    "criterion": "pbo_max",
                    "status": "pass",
                    "value": 0.1,
                    "threshold": 0.2,
                    "note": "",
                },
                {
                    "criterion": "holdout",
                    "status": "missing",
                    "value": None,
                    "threshold": True,
                    "note": "not run",
                },
            ],
            "missing": ["holdout"],
            "ledger_n": 3,
            "budget": {},
        }

    return evaluate


@pytest.fixture
def holdout_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import lab.research.holdout as holdout

    calls: list[str] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("holdout")
        raise AssertionError("calibration must never call the holdout")

    monkeypatch.setattr(holdout, "run_holdout_check", spy)
    monkeypatch.setattr(Ledger, "record_holdout_check", spy)
    return calls


def _seeds_for_calibration() -> list[Seed]:
    by_url = {s.source_url.rsplit("/", 1)[-1]: s for s in load_seeds()}
    good = [by_url["Strategy001.py"], by_url["Bandtastic.py"]]
    missing = Seed(
        name="no data seed",
        source_url=good[0].source_url,
        translation_notes="ETH copy of Strategy001 whose data is absent in this test.",
        plan={**good[0].plan, "name": "seed_eth_missing", "symbol": "ETHUSDT"},
    )
    return [good[0], missing, good[1]]


def test_calibration_records_evidence_and_writes_report(
    cfg: LabConfig,
    ledger: Ledger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holdout_spy: list[str],
) -> None:
    import lab.research.criteria as criteria

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(criteria, "evaluate_plan", _fake_evaluate(calls))
    seeds = _seeds_for_calibration()
    journal = tmp_path / "journal"
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    load_ends: list[pd.Timestamp] = []

    def spy_loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        load_ends.append(end)
        return fake_loader(symbol, interval, end)

    result = run_seed_calibration(
        cfg=cfg,
        ledger=ledger,
        research=research_config(),
        loader=spy_loader,
        seeds=seeds,
        journal_dir=journal,
        now=now,
    )

    assert re.fullmatch(RESEARCH_RUN_ID_PATTERN, result.run_id)
    run = ledger.research_run(result.run_id)
    assert run is not None and run["label"] == "seed-calibration"
    assert len(result.outcomes) == 3
    ok1, bad, ok2 = result.outcomes

    for outcome in (ok1, ok2):
        assert outcome.error is None
        assert outcome.backtest_trial_id is not None
        assert outcome.plan_hash == plan_hash(parse_plan(outcome.seed.plan))
        kinds = sorted(
            e["kind"] for e in ledger.evidence(result.run_id, plan_hash=outcome.plan_hash)
        )
        assert kinds == ["adversarial", "backtest"]
        # Same evidence path as the MCP server: stripped, with the frozen-definition regimes.
        (bt,) = ledger.evidence(result.run_id, plan_hash=outcome.plan_hash, kind="backtest")
        regimes = bt["payload"]["regimes"]
        assert "error" not in regimes, regimes
        assert set(regimes["regimes"]) == {"bear", "sideways", "bull"}
        assert regimes["basis"].startswith("trailing 90-day simple return")
        assert not {"trades", "equity", "benchmark_equity"} & set(bt["payload"])
        (adv,) = ledger.evidence(result.run_id, plan_hash=outcome.plan_hash, kind="adversarial")
        assert not {"trades", "equity", "benchmark_equity"} & set(adv["payload"])
        assert outcome.criteria is not None and outcome.criteria["pre_holdout_passed"] is False
        trial = ledger.get(outcome.backtest_trial_id)
        assert trial.run_id == result.run_id
    assert calls == [(result.run_id, ok1.plan_hash), (result.run_id, ok2.plan_hash)]

    # the missing-data seed is captured, not raised, and leaves no evidence
    assert bad.error is not None and "DataUnavailableError" in bad.error
    assert bad.criteria is None and bad.backtest_trial_id is None
    assert ledger.evidence(result.run_id, plan_hash=bad.plan_hash) == []

    # report: frontmatter, expectation up front, table, disclaimer, no holdout
    assert result.report_path == journal / "2026-10-24-seed-calibration.md"
    text = result.report_path.read_text()
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    assert f"run_id: {result.run_id}" in front
    assert "date: 2026-10-24" in front
    assert "outcome: calibration" in front
    assert "tags: [" in front
    assert text.index(EXPECTATION) < text.index("## Results")
    assert f"[[{result.run_id}]]" in text
    assert "| Seed | Overall | dsr_min | pbo_max | holdout |" in text
    assert "| freqtrade-strategies Strategy001 | failed | fail | pass | missing |" in text
    assert "| no data seed | error | missing | missing | missing |" in text
    assert "not forward-looking" in text
    assert "DataUnavailableError" in text

    assert holdout_spy == []
    assert ledger.holdout_checks_used(result.run_id) == 0
    assert load_ends and all(end == cfg.splits.holdout_start for end in load_ends)


def test_second_calibration_same_day_does_not_overwrite(
    cfg: LabConfig,
    ledger: Ledger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holdout_spy: list[str],
) -> None:
    import lab.research.criteria as criteria

    monkeypatch.setattr(criteria, "evaluate_plan", _fake_evaluate([]))
    seeds = _seeds_for_calibration()[1:2]  # the no-data seed: fast
    now = datetime(2026, 10, 24, tzinfo=UTC)
    kw: dict[str, Any] = {
        "cfg": cfg,
        "ledger": ledger,
        "research": research_config(),
        "loader": fake_loader,
        "seeds": seeds,
        "journal_dir": tmp_path,
        "now": now,
    }
    first = run_seed_calibration(**kw)
    second = run_seed_calibration(**kw)
    assert first.run_id != second.run_id
    assert first.report_path != second.report_path
    assert first.report_path.exists() and second.report_path.exists()
    assert second.run_id in second.report_path.name


def test_criteria_failure_is_captured(
    cfg: LabConfig,
    ledger: Ledger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holdout_spy: list[str],
) -> None:
    import lab.research.criteria as criteria

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("criteria exploded")

    monkeypatch.setattr(criteria, "evaluate_plan", boom)
    seed = _seeds_for_calibration()[0]
    result = run_seed_calibration(
        cfg=cfg,
        ledger=ledger,
        research=research_config(),
        loader=fake_loader,
        seeds=[seed],
        journal_dir=tmp_path,
    )
    (outcome,) = result.outcomes
    assert outcome.criteria is None
    assert outcome.error is not None and "criteria exploded" in outcome.error
    assert outcome.backtest_trial_id is not None  # the backtest itself still ran and was kept
    assert holdout_spy == []


def test_cli_refuses_to_run_without_installed_criteria(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import seed_calibration

    from lab.research.contract import ResearchConfigError

    def missing() -> ResearchConfig:
        raise ResearchConfigError("criteria file not found")

    def never(**kwargs: Any) -> Any:
        raise AssertionError("must not start a run")

    monkeypatch.setattr(seed_calibration, "load_research_config", missing)
    monkeypatch.setattr(seed_calibration, "run_seed_calibration", never)
    assert seed_calibration.main([]) == 2
    err = capsys.readouterr().err
    assert "criteria file not found" in err and "owner" in err


def test_calibration_starts_its_run_with_the_lab_config(
    cfg: LabConfig, ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs without a lab-config fingerprint are refused by every evaluator, so the seed run
    must be started with ``cfg=``."""
    import research.seeds as seeds_mod

    seen: list[dict[str, Any]] = []

    class StopError(Exception):
        pass

    def spy(ledger_: Ledger, research: ResearchConfig, **kwargs: Any) -> str:
        seen.append(kwargs)
        raise StopError

    monkeypatch.setattr(seeds_mod, "start_research_run", spy)
    with pytest.raises(StopError):
        run_seed_calibration(
            cfg=cfg,
            ledger=ledger,
            research=research_config(),
            seeds=_seeds_for_calibration()[:1],
            journal_dir=tmp_path / "journal",
        )
    assert seen and seen[0]["cfg"] is cfg and seen[0]["label"] == "seed-calibration"
