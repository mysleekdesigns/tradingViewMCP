"""Risk controls and sizing wired into the paper flow (PLAN.md Phase 20 Parts E, E2, F).

Offline, in ``tmp_path``. Freqtrade is never imported or started. What this file pins:

* a protocol without the new blocks is the old protocol, byte for byte, including the real HC
  files when they are on disk (read only);
* ``prepare --risk-config`` freezes the blocks and every refusal around them;
* the generated stake, volatility column, protections and halt check;
* ``compare`` applies the same sizing to both sides and refuses a mismatch;
* ``compare`` judges the frozen circuit breaker itself, whether or not ``guard`` ran;
* ``compare --portfolio`` quotes the coins' N_eff on one cohort and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import paper_trade
import pytest

from lab.config import load_config
from lab.research.contract import JOURNAL_DIR, PAPER_DIR
from lab.research.logic import logic_hash
from lab.types import Trade, bars_per_year
from research.paper import risk, shadow
from research.paper.compare import (
    STAKE_TOLERANCE_LOT,
    STAKE_TOLERANCE_REL,
    ForwardTrade,
    StakeMismatchError,
    check_paper_stakes,
    daily_coin_returns,
    daily_in_market,
    daily_paper_returns,
    frozen_costs,
    replay_trades,
    size_both_sides,
    sizing_line,
    stakes_by_fill_bar,
)
from research.paper.freqtrade import (
    check_paper_config,
    class_name_for,
    prepare_candidate,
    render_config,
    render_strategy,
    strategy_frozen_risk,
)
from research.paper.protected_backtest import ProtectedBacktestError
from research.paper.protocol import (
    PROTOCOL_VERSION,
    REWRITE_HEADING,
    RISK_PROTOCOL_VERSION,
    VERDICTS_HEADING,
    append_halt,
    append_rewrite,
    append_verdict,
    build_protocol,
    check_not_registered_elsewhere,
    evaluate,
    load_protocol_config,
    load_registered_protocol,
    protocol_from_dict,
    protocol_sha256,
    protocol_to_dict,
    protocol_to_json,
    recorded_final_comparison,
    recorded_final_verdict,
    register_protocol,
    render_journal_entry,
    size_steps_from,
)
from research.paper.risk import (
    CircuitBreaker,
    PaperConfig,
    Protections,
    RiskControls,
    Sizing,
    check_no_averaging_down,
    load_risk_config,
    realized_vol_annual,
    sized_stake,
)
from tests.test_paper_protocol import (
    CANDLES,
    COSTS,
    DESIGN_START,
    LINK,
    SPLIT,
    _Clock,
    build,
    observed,
    seed_ledger,
    synthetic_design,
)
from tests.test_paper_protocol import PLAN as PLAN_NO_STOP
from tests.test_shadow import design as shadow_design
from tests.test_shadow import make_world, plan_for, synthetic

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "paper_risk.example.toml"
PLAN: dict[str, Any] = {**PLAN_NO_STOP, "risk": {"position_size_pct": 100, "stop_loss_pct": 5}}
H4 = pd.Timedelta(hours=4)
PROTECTIONS = Protections(2, 24, 3, 12, 42, 5, 24, 0.06, 1 / 3)
BREAKER = CircuitBreaker(max_daily_loss_pct=3.0, max_drawdown_pct=10.0, wallet=1000.0)
SIZING = Sizing(
    risk_per_trade_pct=1.0,
    justification="test",
    wallet=1000.0,
    max_stake_pct=20.0,
    min_stake_pct=2.0,
    max_total_exposure_pct=100.0,
    max_open_trades=4,
    vol_target_annual=0.5,
    vol_window_bars=42,
)
PLAIN_SIZING = replace(SIZING, vol_target_annual=None, vol_window_bars=None)
CONTROLS = RiskControls(protections=PROTECTIONS, circuit_breaker=BREAKER)
# sha256 of the outputs of the code as committed before Phase 20 (agents/research/paper at
# cad8bee), on the inputs of no_block_outputs below.
GOLDEN = {
    "strategy": "ef0ad9f3b46909373c6ed47716ddbd6bdb45ae6f3d43c6e63bc50de3c78af2e2",
    "config": "baa8adf8f2f9586535b9209c04a1e90779c508be883a0c4e8abd6068d1b63263",
    "protocol_sha": "c36a52a73e518f625553944e387e70833796da6a89fa12fbe42e1918a6f7af07",
    "protocol_json": "3b4dee437a274cdc8078aa91ec6e2d84e30093011d1c543d446de5c7ea8b9a1a",
    "journal": "9f81bcd84f2efd4ea4db6bbdac3c2c0643fed3c996c82214e6482a01e467c1bd",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def frozen_config_for(**blocks: Any) -> PaperConfig | None:
    """What ``prepare`` freezes beside these blocks, at its default stake and wallet."""
    controls, sizing = blocks.get("risk_controls"), blocks.get("sizing")
    if controls is None and sizing is None:
        return None
    wallet = risk.frozen_wallet(controls, sizing) or 1000.0
    if sizing is None:
        return PaperConfig("binance", None, 100.0, wallet, 1)
    return PaperConfig("binance", None, risk.UNLIMITED_STAKE, wallet, sizing.max_open_trades)


def sized_protocol(**kwargs: Any) -> Any:
    kwargs.setdefault("paper_config", frozen_config_for(**kwargs))
    return build_protocol(
        candidate=kwargs.pop("candidate", "sized"),
        plan=kwargs.pop("plan", PLAN),
        design=synthetic_design(mean=0.001),
        costs=COSTS,
        config=load_protocol_config(),
        created_at=datetime(2026, 11, 2, 10, 30, 7, tzinfo=UTC),
        link=LINK,
        claimed_sharpe_annual=0.5,
        claim_source="owner_supplied",
        claim_note="test claim",
        **kwargs,
    )


# --------------------------------------------------------------------------- HC byte compatibility


def test_no_block_outputs_match_the_code_before_phase_20() -> None:
    """Golden hashes: without a block, nothing a registration writes moved by one byte."""
    strategy = render_strategy(
        PLAN_NO_STOP, class_name="TvPaperGolden", warm_from=1_700_000_000, start=1_700_720_000
    )
    config = render_config(
        plan=PLAN_NO_STOP,
        db_path=Path("/golden/dryrun.sqlite"),
        bot_name="tv-paper-golden",
        public_api_host="data-api.binance.vision",
    )
    protocol = build(synthetic_design(mean=0.001), claimed=0.5)
    assert {
        "strategy": _sha(strategy),
        "config": _sha(json.dumps(config, indent=2) + "\n"),
        "protocol_sha": protocol_sha256(protocol),
        "protocol_json": _sha(protocol_to_json(protocol)),
        "journal": _sha(render_journal_entry(protocol)),
    } == GOLDEN


def test_no_block_protocol_stays_version_7_without_the_new_keys() -> None:
    protocol = build(synthetic_design(mean=0.001), claimed=0.5)
    data = protocol_to_dict(protocol)
    assert protocol.version == PROTOCOL_VERSION == 7
    assert "risk_controls" not in data and "sizing" not in data
    assert protocol_from_dict(data) == protocol


def test_the_real_hc_files_are_untouched_when_present() -> None:
    """Read only. Each file on disk still loads, hashes as registered and renders identically:
    this is what stops a code change from raising ProtocolChangedError on a live test."""
    root = Path(PAPER_DIR)
    folders = sorted(root.glob("*/protocol.json")) if root.is_dir() else []
    for folder in (path.parent for path in folders):
        raw = (folder / "protocol.json").read_text(encoding="utf-8")
        protocol, _ = load_registered_protocol(root, folder.name, JOURNAL_DIR)
        assert protocol.version == PROTOCOL_VERSION
        assert protocol_to_json(protocol) == raw
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        strategy = folder / f"{class_name_for(folder.name)}.py"
        assert strategy.read_text(encoding="utf-8") == render_strategy(
            plan,
            class_name=class_name_for(folder.name),
            warm_from=protocol.data.warm_from,
            start=protocol.start,
        )
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        host = config["exchange"]["ccxt_config"]["urls"]["api"]["public"].split("/")[2]
        assert config == render_config(
            plan=plan,
            db_path=Path(config["db_url"].removeprefix("sqlite:///")),
            bot_name=config["bot_name"],
            public_api_host=host,
            stake_amount=config["stake_amount"],
            dry_run_wallet=config["dry_run_wallet"],
        )


def test_version_rules_of_the_blocks() -> None:
    plain = protocol_to_dict(build(synthetic_design(mean=0.001), claimed=0.5))
    blocked = protocol_to_dict(sized_protocol(sizing=SIZING, risk_controls=CONTROLS))
    assert blocked["version"] == RISK_PROTOCOL_VERSION == 8
    with pytest.raises(ValueError, match="version 7 cannot carry sizing"):
        protocol_from_dict({**plain, "sizing": blocked["sizing"]})
    with pytest.raises(ValueError, match="version 7 cannot carry risk_controls"):
        protocol_from_dict({**plain, "risk_controls": blocked["risk_controls"]})
    with pytest.raises(ValueError, match="version 7 cannot carry"):
        protocol_from_dict({**plain, "sizing": None})
    with pytest.raises(ValueError, match="version 8 needs"):
        protocol_from_dict({**plain, "version": 8})
    with pytest.raises(ValueError, match="sizing"):
        protocol_from_dict({**blocked, "sizing": None})
    with pytest.raises(ValueError, match="unsupported protocol version"):
        protocol_from_dict({**plain, "version": 9})


def test_blocks_round_trip_and_the_sha_covers_them() -> None:
    protocol = sized_protocol(sizing=SIZING, risk_controls=CONTROLS)
    again = protocol_from_dict(json.loads(protocol_to_json(protocol)))
    assert again == protocol and protocol_sha256(again) == protocol_sha256(protocol)
    other = sized_protocol(sizing=replace(SIZING, risk_per_trade_pct=0.5), risk_controls=CONTROLS)
    assert protocol_sha256(other) != protocol_sha256(protocol)


def test_the_rules_state_what_the_frozen_blocks_can_and_cannot_do() -> None:
    text = " ".join(sized_protocol(sizing=SIZING, risk_controls=CONTROLS).rules)
    assert "come from an unprotected design replay" in text and "false fails" in text
    assert "acts only while the watcher runs" in text and "on the frozen 1000 wallet" in text
    assert "a trip there is a final fail" in text
    assert "the scale stops at 1" in text


def test_one_test_has_one_wallet() -> None:
    other = replace(BREAKER, wallet=2000.0)
    controls = RiskControls(protections=None, circuit_breaker=other)
    with pytest.raises(ValueError, match="one frozen wallet"):
        sized_protocol(sizing=SIZING, risk_controls=controls)


def test_a_sizing_block_is_refused_on_a_plan_without_a_stop() -> None:
    with pytest.raises(ValueError, match="sized stakes need a stop"):
        sized_protocol(plan=PLAN_NO_STOP, sizing=SIZING)
    with pytest.raises(ValueError, match="sized stakes need a stop"):
        render_strategy(PLAN_NO_STOP, class_name="X", sizing=SIZING)


# --------------------------------------------------------------------------- CLI registration


@pytest.fixture
def rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    base = load_config()
    splits = replace(base.splits, design_start=DESIGN_START, holdout_start=SPLIT)
    cfg = replace(base, data_dir=tmp_path / "lab", splits=splits)
    monkeypatch.setattr(paper_trade, "load_config", lambda: cfg)
    monkeypatch.setattr(paper_trade, "default_loader", lambda c: lambda *a: CANDLES)
    monkeypatch.setattr(paper_trade, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 1, 9, 15, tzinfo=UTC))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))
    seed_ledger(cfg.ledger_path, PLAN, wf_sharpes=(0.4,))
    paper, journal = tmp_path / "paper", tmp_path / "journal"
    return {
        "plan": plan_file,
        "paper": paper,
        "journal": journal,
        "base": ["--paper-dir", str(paper), "--journal-dir", str(journal)],
        "tmp": tmp_path,
    }


def prepare(rc: dict[str, Any], *extra: str, candidate: str = "cand", plan: Any = None) -> int:
    argv = [*rc["base"], "prepare", "--plan-file", str(plan or rc["plan"])]
    argv += ["--candidate", candidate, "--candle-source", "archive", *extra]
    return paper_trade.main(argv)


def frozen_files(rc: dict[str, Any], candidate: str = "cand") -> dict[str, str]:
    folder = rc["paper"] / candidate
    return {
        "protocol": (folder / "protocol.json").read_text(),
        "journal": next(rc["journal"].glob(f"*-paper-{candidate}.md")).read_text(),
    }


def test_prepare_without_the_flag_is_a_plain_protocol(rc: dict[str, Any]) -> None:
    assert prepare(rc) == 0
    data = json.loads((rc["paper"] / "cand" / "protocol.json").read_text())
    assert data["version"] == 7 and "sizing" not in data and "risk_controls" not in data
    source = (rc["paper"] / "cand" / f"{class_name_for('cand')}.py").read_text()
    assert "FROZEN_RISK" not in source and "custom_stake_amount" not in source
    config = json.loads((rc["paper"] / "cand" / "config.json").read_text())
    assert config["stake_amount"] == 100.0 and config["max_open_trades"] == 1


def test_prepare_freezes_the_blocks_into_every_file(rc: dict[str, Any]) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    folder = rc["paper"] / "cand"
    expected = load_risk_config(EXAMPLE)
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    assert protocol.version == 8
    assert protocol.sizing == expected.sizing and protocol.risk_controls == expected.risk_controls
    journal = jpath.read_text()
    assert "risk per trade 1% of the 1000 wallet" in journal
    assert "does not change whether the plan has an edge" in journal
    assert "CooldownPeriod 2 candles" in journal and "3% loss" in journal
    config = json.loads((folder / "config.json").read_text())
    assert config["dry_run_wallet"] == 1000.0
    assert config["stake_amount"] == "unlimited"  # a number drops entries below the balance
    assert config["max_open_trades"] == 4 and config["tradable_balance_ratio"] == 1.0
    assert config["dry_run"] is True
    source = (folder / f"{class_name_for('cand')}.py").read_text()
    assert strategy_frozen_risk(source)["sizing"]["risk_per_trade_pct"] == 1.0


def test_a_registered_candidate_refuses_other_blocks(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    before = frozen_files(rc)
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0  # the same blocks: a plain restart
    assert prepare(rc) == 0  # no flag: the frozen blocks stand
    other = rc["tmp"] / "other.toml"
    other.write_text(
        EXAMPLE.read_text().replace("risk_per_trade_pct = 1.0", "risk_per_trade_pct = 1.5")
    )
    capsys.readouterr()
    assert prepare(rc, "--risk-config", str(other)) == 2
    assert "--risk-config would change it" in capsys.readouterr().err
    assert frozen_files(rc) == before


def test_a_no_block_candidate_refuses_a_risk_config(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The live HC path: a registered v7 test can never gain a block."""
    assert prepare(rc) == 0
    before = frozen_files(rc)
    capsys.readouterr()
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 2
    assert "never overwritten" in capsys.readouterr().err
    assert frozen_files(rc) == before


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--wallet", "5000"), "contradicts the sizing block's wallet"),
        (("--stake-amount", "100"), "contradicts the sizing block"),
    ],
)
def test_stake_flags_may_not_contradict_the_block(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], flags: tuple[str, str], message: str
) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE), *flags) == 2
    assert message in capsys.readouterr().err
    assert not (rc["paper"] / "cand" / "protocol.json").exists()
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    capsys.readouterr()
    assert prepare(rc, *flags) == 2  # on a restart too
    assert message in capsys.readouterr().err


def test_flags_equal_to_the_block_are_fine(rc: dict[str, Any]) -> None:
    extra = ("--wallet", "1000", "--stake-amount", "200")
    assert prepare(rc, "--risk-config", str(EXAMPLE), *extra) == 0


def test_a_plan_without_a_stop_is_refused_with_sizing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    no_stop = rc["tmp"] / "no-stop.json"
    no_stop.write_text(json.dumps(PLAN_NO_STOP))
    assert prepare(rc, "--risk-config", str(EXAMPLE), plan=no_stop) == 2
    assert "sized stakes need a stop" in capsys.readouterr().err
    assert not (rc["paper"] / "cand" / "protocol.json").exists()


DCA = (
    "class S:\n"
    "    position_adjustment_enable = True\n"
    "    def adjust_trade_position(self, trade, **kwargs):\n"
    "        return trade.stake_amount\n"
)


def test_averaging_down_in_the_folder_is_refused(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = rc["paper"] / "cand"
    folder.mkdir(parents=True)
    strategy = folder / f"{class_name_for('cand')}.py"
    strategy.write_text(DCA)
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 2
    assert "no averaging down rule" in capsys.readouterr().err
    assert not (folder / "protocol.json").exists()
    assert strategy.read_text() == DCA  # the refusal rewrites nothing


def test_averaging_down_is_screened_on_every_prepare_with_sizing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    strategy = rc["paper"] / "cand" / f"{class_name_for('cand')}.py"
    strategy.write_text(DCA)
    capsys.readouterr()
    assert prepare(rc) == 2
    assert "no averaging down rule" in capsys.readouterr().err


def _breaker_only(rc: dict[str, Any]) -> str:
    return breaker_only_toml(rc)


def _protections_only(rc: dict[str, Any]) -> str:
    return protections_only_toml(rc)


@pytest.mark.parametrize("toml", [_breaker_only, _protections_only])
def test_averaging_down_is_screened_for_a_test_without_sizing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], toml: Any
) -> None:
    folder = rc["paper"] / "cand"
    folder.mkdir(parents=True)
    strategy = folder / f"{class_name_for('cand')}.py"
    strategy.write_text(DCA)
    assert prepare(rc, "--risk-config", toml(rc)) == 2
    assert "no averaging down rule" in capsys.readouterr().err
    assert not (folder / "protocol.json").exists()


@pytest.mark.parametrize("toml", [_breaker_only, _protections_only])
def test_averaging_down_is_screened_on_a_re_prepare_without_sizing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], toml: Any
) -> None:
    assert prepare(rc, "--risk-config", toml(rc)) == 0
    strategy = rc["paper"] / "cand" / f"{class_name_for('cand')}.py"
    strategy.write_text(DCA)
    capsys.readouterr()
    assert prepare(rc) == 2
    assert "no averaging down rule" in capsys.readouterr().err


def test_a_no_block_prepare_never_screens_an_existing_strategy(rc: dict[str, Any]) -> None:
    """HC restarts: `prepare --launch` must not start refusing a frozen test's folder."""
    assert prepare(rc) == 0
    strategy = rc["paper"] / "cand" / f"{class_name_for('cand')}.py"
    strategy.write_text(DCA)
    assert prepare(rc) == 0


def test_position_adjustment_is_refused_in_every_config_and_never_rendered() -> None:
    config = render_config(plan=PLAN, db_path=Path("/x/dryrun.sqlite"))
    assert "position_adjustment_enable" not in config
    check_paper_config({**config, "position_adjustment_enable": False})
    with pytest.raises(ValueError, match="no averaging down rule"):
        check_paper_config({**config, "position_adjustment_enable": True})


def _register_prior(
    rc: dict[str, Any],
    *,
    days: float,
    outcome: str = "pass",
    name: str = "prior",
    plan: Any = PLAN,
    prior_test: str | None = None,
    risk_pct: float = 0.5,
    **fields: Any,
) -> None:
    """A registered test with a recorded verdict; by default a real proof of a smaller size."""
    block = replace(SIZING, risk_per_trade_pct=risk_pct, prior_test=prior_test)
    protocol = sized_protocol(candidate=name, sizing=block, plan=plan)
    proof = {"mode": "sharpe_and_implementation", "locked_split_check": "passed"}
    protocol = replace(protocol, decision_days=days, **{**proof, **fields})
    _, jpath = register_protocol(protocol, paper_dir=rc["paper"], journal_dir=rc["journal"])
    text = jpath.read_text().replace('outcome: "pending"', f'outcome: "{outcome}"', 1)
    jpath.write_text(text)


def _stepped(rc: dict[str, Any], *, prior: str = "prior", multiplier: float = 1.0) -> str:
    path = rc["tmp"] / "step.toml"
    text = EXAMPLE.read_text().replace(
        "size_multiplier = 1.0", f'size_multiplier = {multiplier}\nprior_test = "{prior}"'
    )
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize(
    ("days", "outcome", "code", "message"),
    [
        (89.0, "pass", 2, "decision horizon of at least 90"),
        (90.0, "pass", 0, ""),
        (90.0, "fail", 2, "must have passed"),
        (120.0, "inconclusive", 2, "must have passed"),
    ],
)
def test_a_size_step_needs_a_90_day_pass(
    rc: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    days: float,
    outcome: str,
    code: int,
    message: str,
) -> None:
    _register_prior(rc, days=days, outcome=outcome)
    assert prepare(rc, "--risk-config", _stepped(rc)) == code
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("controls", "message"),
    [
        (RiskControls(None, BREAKER), "circuit breaker this step drops"),
        (RiskControls(PROTECTIONS, None), "protections this step does not keep"),
    ],
)
def test_a_size_step_may_not_drop_what_the_prior_passed_under(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], controls: RiskControls, message: str
) -> None:
    _register_prior(rc, days=90.0, risk_controls=controls)
    full = Path(_stepped(rc)).read_text()
    bare = rc["tmp"] / "bare.toml"
    bare.write_text(full[full.index("[sizing]") :])
    assert prepare(rc, "--risk-config", str(bare)) == 2
    assert message in capsys.readouterr().err


def test_a_size_step_that_keeps_what_the_prior_passed_under_registers(rc: dict[str, Any]) -> None:
    _register_prior(rc, days=90.0, risk_controls=load_risk_config(EXAMPLE).risk_controls)
    assert prepare(rc, "--risk-config", _stepped(rc)) == 0


def test_a_size_step_refuses_more_than_one_step_and_an_unknown_prior(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _register_prior(rc, days=90.0)
    assert prepare(rc, "--risk-config", _stepped(rc, multiplier=2.0)) == 2  # 0.5 -> 2.0 is 4x
    assert "at most one 2x step" in capsys.readouterr().err


def test_a_size_step_refuses_an_unknown_prior(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", _stepped(rc, prior="missing")) == 2
    assert "not found" in capsys.readouterr().err


def test_only_the_named_prior_may_share_the_hypothesis(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A size step names its prior test; any other registration of the plan is still refused."""
    _register_prior(rc, days=90.0)
    _register_prior(rc, days=90.0, name="other")
    assert prepare(rc, "--risk-config", _stepped(rc)) == 2
    assert "already registered as paper candidate 'other'" in capsys.readouterr().err


def test_a_pass_of_another_plan_unlocks_nothing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    other = {**PLAN, "long_entry": "fast > slow", "long_exit": "fast < slow"}
    _register_prior(rc, days=90.0, plan=other)
    assert prepare(rc, "--risk-config", _stepped(rc, multiplier=2.0)) == 2
    assert "has other logic" in capsys.readouterr().err
    assert not (rc["paper"] / "cand" / "protocol.json").exists()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"mode": "implementation_only"}, "implementation_only"),
        ({"locked_split_check": "unchecked"}, "NOT a success candidate"),
    ],
)
def test_a_pass_that_is_no_proof_unlocks_nothing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], fields: dict[str, Any], message: str
) -> None:
    _register_prior(rc, days=90.0, **fields)
    assert prepare(rc, "--risk-config", _stepped(rc)) == 2
    assert message in capsys.readouterr().err


def test_a_halted_prior_unlocks_nothing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _register_prior(rc, days=90.0)
    (rc["paper"] / "prior" / "halt.lock").write_text("{}")
    assert prepare(rc, "--risk-config", _stepped(rc)) == 2
    assert "holds halt.lock" in capsys.readouterr().err


def test_one_proof_unlocks_one_step(rc: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    _register_prior(rc, days=90.0)
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="first") == 0
    assert size_steps_from(rc["paper"], rc["journal"], "prior") == ("first",)
    capsys.readouterr()
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="second") == 2
    assert "already unlocked first" in capsys.readouterr().err


def test_deleting_a_step_folder_does_not_free_its_proof(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _register_prior(rc, days=90.0)
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="first") == 0
    (rc["paper"] / "first" / "protocol.json").unlink()
    assert size_steps_from(rc["paper"], rc["journal"], "prior") == ("first",)


def test_a_chain_of_the_same_logic_registers_each_step_on_its_own_proof(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A -> B -> C: the duplicate check used to refuse C for sharing logic with A."""
    _register_prior(rc, days=90.0, name="stepa")
    _register_prior(rc, days=90.0, name="stepb", prior_test="stepa", risk_pct=0.75)
    assert prepare(rc, "--risk-config", _stepped(rc, prior="stepb"), candidate="stepc") == 0
    assert (rc["paper"] / "stepc" / "protocol.json").is_file()


def test_a_chain_does_not_excuse_an_unrelated_registration(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _register_prior(rc, days=90.0, name="stepa")
    _register_prior(rc, days=90.0, name="stepb", prior_test="stepa", risk_pct=0.75)
    _register_prior(rc, days=90.0, name="other")
    assert prepare(rc, "--risk-config", _stepped(rc, prior="stepb"), candidate="stepc") == 2
    assert "already registered as paper candidate 'other'" in capsys.readouterr().err


def plan_with(**risk_fields: Any) -> dict[str, Any]:
    return {**PLAN, "risk": {"position_size_pct": 100, **risk_fields}}


def ledgered(rc: dict[str, Any], plan: dict[str, Any], name: str) -> Path:
    path = rc["tmp"] / f"{name}.json"
    path.write_text(json.dumps(plan))
    seed_ledger(paper_trade.load_config().ledger_path, plan, run_id="r-test-second")
    return path


def _retuned() -> dict[str, Any]:
    features = {"fast": {"ema": {"period": 5}}, "slow": {"ema": {"period": 60}}}
    return {**plan_with(stop_loss_pct=2.5), "features": features}


def test_a_re_tuned_plan_cannot_step_on_another_plans_pass(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """EMA 5/60 at a 2.5% stop borrowed the pass of EMA 8/30 at 10%: stakes of 50 became 200."""
    earned, retuned = plan_with(stop_loss_pct=10), _retuned()
    assert logic_hash(earned) == logic_hash(retuned)
    _register_prior(rc, days=90.0, plan=earned)
    path = ledgered(rc, retuned, "retuned")
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="retuned", plan=path) == 2
    assert "is another plan" in capsys.readouterr().err
    assert not (rc["paper"] / "retuned" / "protocol.json").exists()


def test_a_name_in_prior_test_excuses_only_its_own_plan(rc: dict[str, Any]) -> None:
    """Even a step proved some other way shares nothing with a re-tune in its chain."""
    _register_prior(rc, days=90.0, plan=plan_with(stop_loss_pct=10))
    with pytest.raises(ValueError, match="already registered as paper candidate 'prior'"):
        check_not_registered_elsewhere(
            rc["paper"], "new", _retuned(), journal_dir=rc["journal"], size_step_of="prior"
        )


def test_the_identical_plan_at_the_identical_size_is_no_step(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The 2x rule had no lower bound, so a pass bought a free second test of one hypothesis."""
    _register_prior(rc, days=90.0, risk_pct=1.0)
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="again") == 2
    assert "a step must be a step" in capsys.readouterr().err
    assert not (rc["paper"] / "again" / "protocol.json").exists()


def test_a_failed_prior_is_refused_for_what_it_is_not_as_a_duplicate(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _register_prior(rc, days=90.0, outcome="fail")
    assert prepare(rc, "--risk-config", _stepped(rc), candidate="retry") == 2
    err = capsys.readouterr().err
    assert "must have passed" in err and "already registered" not in err


def test_the_default_duration_can_never_unlock_a_step(rc: dict[str, Any]) -> None:
    """Intended: the shipped 28-56 day protocol gives no 90-day proof."""
    assert load_protocol_config().max_days < risk.MIN_PRIOR_DAYS


# --------------------------------------------------------------------------- generated strategy


@pytest.fixture
def stub_freqtrade(monkeypatch: pytest.MonkeyPatch) -> None:
    class IStrategy:
        def __init__(self, config: dict[str, Any] | None = None) -> None:
            self.config = config or {}

    pkg = types.ModuleType("freqtrade")
    strategy = types.ModuleType("freqtrade.strategy")
    strategy.IStrategy = IStrategy  # type: ignore[attr-defined]
    pkg.strategy = strategy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "freqtrade", pkg)
    monkeypatch.setitem(sys.modules, "freqtrade.strategy", strategy)


def load_strategy(source: str, path: Path | None = None) -> Any:
    ns: dict[str, Any] = {"__name__": "rendered"}
    if path is not None:
        ns["__file__"] = str(path)
    exec(compile(source, "<rendered>", "exec"), ns)
    return ns["TvPaperRisk"]()


def freqtrade_frame(candles: pd.DataFrame) -> pd.DataFrame:
    frame = candles.reset_index().rename(columns={"time": "date"})
    return frame[["date", "open", "high", "low", "close", "volume"]]


class _Provider:
    def __init__(self, frame: pd.DataFrame | None = None, mode: str = "dry_run") -> None:
        self.frame = frame
        self.runmode = types.SimpleNamespace(value=mode)

    def get_analyzed_dataframe(self, pair: str, timeframe: str) -> tuple[pd.DataFrame, Any]:
        if self.frame is None:
            raise KeyError(pair)
        return self.frame, None


def stake_of(strategy: Any, *, max_stake: float = 1e9) -> float:
    return strategy.custom_stake_amount(
        pair="BTC/USDT",
        current_time=None,
        current_rate=100.0,
        proposed_stake=250.0,
        min_stake=1.0,
        max_stake=max_stake,
        leverage=1.0,
        entry_tag=None,
        side="long",
    )


def test_rendered_strategy_passes_its_own_rule_and_shows_the_blocks() -> None:
    source = render_strategy(
        PLAN, class_name="TvPaperRisk", risk_controls=CONTROLS, sizing=SIZING, warm_from=1, start=2
    )
    check_no_averaging_down(source, {})
    assert strategy_frozen_risk(source) == {
        "risk_controls": risk.risk_controls_to_dict(CONTROLS),
        "sizing": risk.sizing_to_dict(SIZING),
    }


def test_protections_property_is_freqtrades_list(stub_freqtrade: None) -> None:
    strategy = load_strategy(
        render_strategy(PLAN, class_name="TvPaperRisk", risk_controls=CONTROLS)
    )
    assert strategy.protections == [
        {"method": "CooldownPeriod", "stop_duration_candles": 2},
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 24,
            "trade_limit": 3,
            "stop_duration_candles": 12,
        },
        {
            "method": "MaxDrawdown",
            "calculation_mode": "equity",
            "lookback_period_candles": 42,
            "trade_limit": 5,
            "stop_duration_candles": 24,
            "max_allowed_drawdown": 0.06,
        },
    ]


def test_a_breaker_only_block_adds_no_stake_code(stub_freqtrade: None) -> None:
    only = RiskControls(protections=None, circuit_breaker=BREAKER)
    source = render_strategy(PLAN_NO_STOP, class_name="TvPaperRisk", risk_controls=only)
    assert "custom_stake_amount" not in source and "def protections" not in source
    assert "confirm_trade_entry" in source
    load_strategy(source)


def test_strategy_stake_is_sized_stake_on_the_last_closed_bar(stub_freqtrade: None) -> None:
    candles = CANDLES.iloc[:300]
    source = render_strategy(PLAN, class_name="TvPaperRisk", sizing=SIZING)
    strategy = load_strategy(source)
    analysed = strategy.populate_indicators(freqtrade_frame(candles), {})
    strategy.dp = _Provider(analysed)
    vol = realized_vol_annual(candles["close"], 42, bars_per_year("4h")).iloc[-1]
    assert math.isfinite(vol)
    expected = sized_stake(
        stop_distance=0.05,
        wallet=1000.0,
        risk_per_trade_pct=1.0,
        max_stake_pct=25.0,
        min_stake_pct=2.0,
        vol_target_annual=0.5,
        realized_vol_annual=float(vol),
    )
    assert stake_of(strategy) == pytest.approx(expected, rel=0, abs=0)
    assert stake_of(strategy, max_stake=10.0) == 10.0  # never above what freqtrade allows


def test_strategy_without_a_vol_target_never_reads_the_frame(stub_freqtrade: None) -> None:
    strategy = load_strategy(render_strategy(PLAN, class_name="TvPaperRisk", sizing=PLAIN_SIZING))
    strategy.dp = _Provider(None)  # raises if read
    assert stake_of(strategy) == pytest.approx(200.0)  # 1000 x 1% / 5%


def test_strategy_without_volatility_uses_the_minimum_stake(stub_freqtrade: None) -> None:
    strategy = load_strategy(render_strategy(PLAN, class_name="TvPaperRisk", sizing=SIZING))
    strategy.dp = _Provider(None)
    assert stake_of(strategy) == pytest.approx(20.0)  # 2% of the wallet


def test_volatility_column_matches_risk_including_nan_across_a_gap(stub_freqtrade: None) -> None:
    """Parity with ``risk.realized_vol_annual`` on the kept bars, on a frame with a missing bar
    and a filler row freqtrade adds for one: NaN while either gap is in the window."""
    candles = CANDLES.iloc[:300]
    missing = candles.drop(candles.index[150])
    frame = freqtrade_frame(missing)
    filler = frame.iloc[[220]].copy()
    filler[["open", "high", "low", "close"]] = float(frame.iloc[219]["close"])
    filler["volume"] = 0.0
    frame = pd.concat([frame.iloc[:220], filler, frame.iloc[221:]], ignore_index=True)
    kept = missing.drop(missing.index[220])

    source = render_strategy(PLAN, class_name="TvPaperRisk", sizing=SIZING)
    analysed = load_strategy(source).populate_indicators(frame, {})
    expected = realized_vol_annual(kept["close"], 42, bars_per_year("4h")).to_numpy()
    column = analysed["lab_vol"].to_numpy()
    filler_row = 220
    assert np.isnan(column[filler_row])
    np.testing.assert_array_equal(np.delete(column, filler_row), expected)
    assert np.isnan(expected[150:192]).all()  # a window holding the missing bar
    assert np.isfinite(expected[200])


def test_warm_from_also_starts_the_volatility_column(stub_freqtrade: None) -> None:
    candles = CANDLES.iloc[:300]
    warm = int(candles.index[100].timestamp())
    source = render_strategy(PLAN, class_name="TvPaperRisk", sizing=SIZING, warm_from=warm)
    analysed = load_strategy(source).populate_indicators(freqtrade_frame(candles), {})
    expected = realized_vol_annual(candles["close"].iloc[100:], 42, bars_per_year("4h"))
    np.testing.assert_array_equal(analysed["lab_vol"].to_numpy()[100:], expected.to_numpy())
    assert np.isnan(analysed["lab_vol"].to_numpy()[:142]).all()


def test_halt_check_blocks_entries_and_fails_closed(stub_freqtrade: None, tmp_path: Path) -> None:
    source = render_strategy(PLAN, class_name="TvPaperRisk", risk_controls=CONTROLS)
    strategy = load_strategy(source, tmp_path / "TvPaperRisk.py")
    entry = {
        "pair": "BTC/USDT",
        "order_type": "market",
        "amount": 1.0,
        "rate": 100.0,
        "time_in_force": "GTC",
        "current_time": None,
        "entry_tag": None,
        "side": "long",
    }
    strategy.dp = _Provider()
    assert strategy.confirm_trade_entry(**entry) is True
    (tmp_path / "halt.lock").write_text("{}")
    assert strategy.confirm_trade_entry(**entry) is False
    strategy.dp = _Provider(mode="backtest")  # compare's replay of the plan, not of the halt
    assert strategy.confirm_trade_entry(**entry) is True
    del strategy.dp  # the run mode cannot be read: fail closed
    assert strategy.confirm_trade_entry(**entry) is False
    no_file = load_strategy(source)  # __file__ undefined: the check itself raises
    assert no_file.confirm_trade_entry(**entry) is False


# --------------------------------------------------------------------------- sizing compare side


def ft(open_time: pd.Timestamp, stake: float | None, **kw: Any) -> ForwardTrade:
    base: dict[str, Any] = {
        "pair": "BTC/USDT",
        "is_open": False,
        "open_time": open_time + pd.Timedelta(seconds=5),
        "open_rate": 100.0,
        "close_time": open_time + 5 * H4,
        "close_rate": 110.0,
        "close_profit": 0.1,
        "stake_amount": stake,
    }
    return ForwardTrade(**{**base, **kw})


def lab(entry: pd.Timestamp, ret: float, exit_time: pd.Timestamp | None = None) -> Trade:
    return Trade(
        direction="long",
        entry_time=entry,
        entry_price=100.0,
        exit_time=exit_time if exit_time is not None else entry + 5 * H4,
        exit_price=100.0 * (1.0 + ret),
        size=1.0,
        pnl=ret * 100.0,
        return_pct=ret,
        fees=0.0,
        exit_reason="signal",
        bars_held=5,
    )


def test_a_fill_bar_stake_is_sized_on_the_bar_before_it() -> None:
    candles = CANDLES.iloc[:200]
    stakes = stakes_by_fill_bar(candles, SIZING, stop_distance=0.05, interval="4h")
    vol = realized_vol_annual(candles["close"], 42, bars_per_year("4h"))
    assert math.isnan(stakes.iloc[0])
    assert stakes.iloc[120] == sized_stake(
        stop_distance=0.05,
        wallet=1000.0,
        risk_per_trade_pct=1.0,
        max_stake_pct=25.0,
        min_stake_pct=2.0,
        vol_target_annual=0.5,
        realized_vol_annual=float(vol.iloc[119]),
    )


def test_stake_perturbation_after_the_signal_bar_changes_nothing() -> None:
    """No look-ahead: prices after bar t leave every stake up to fill bar t+1 bit-identical."""
    candles = CANDLES.iloc[:200].copy()
    before = stakes_by_fill_bar(candles, SIZING, stop_distance=0.05, interval="4h")
    t = 150
    changed = candles.copy()
    changed.iloc[t + 1 :, changed.columns.get_loc("close")] *= 3.0
    after = stakes_by_fill_bar(changed, SIZING, stop_distance=0.05, interval="4h")
    np.testing.assert_array_equal(before.iloc[: t + 2].to_numpy(), after.iloc[: t + 2].to_numpy())
    assert not before.iloc[t + 2 :].equals(after.iloc[t + 2 :])


def test_a_volatility_spike_shrinks_the_next_stake_not_the_current_one() -> None:
    candles = CANDLES.iloc[:200].copy()
    t = 150
    before = stakes_by_fill_bar(candles, SIZING, stop_distance=0.05, interval="4h")
    spiked = candles.copy()
    spiked.iloc[t, spiked.columns.get_loc("close")] *= 1.5
    after = stakes_by_fill_bar(spiked, SIZING, stop_distance=0.05, interval="4h")
    assert after.iloc[t] == before.iloc[t]
    assert after.iloc[t + 1] < before.iloc[t + 1]


def test_a_stake_above_the_frozen_one_gets_no_lot_allowance() -> None:
    """Amounts round down: every real gap lay between -0.805 and 0. One lot up would accept +7.5%
    at a 20 stake, and a stake that high was not lot rounding."""
    start = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    stakes = pd.Series([20.0], index=[start])

    def judge(stake: float) -> None:
        paper = [ft(start, stake)]
        check_paper_stakes(paper, [lab(start, 0.01)], stakes, interval="4h", tolerance_bars=1)

    judge(20.0 - STAKE_TOLERANCE_LOT)
    judge(20.0 * (1 + STAKE_TOLERANCE_REL))
    with pytest.raises(StakeMismatchError):
        judge(20.0 * (1 + STAKE_TOLERANCE_REL) + 0.01)
    with pytest.raises(StakeMismatchError):
        judge(21.5)
    with pytest.raises(StakeMismatchError):
        judge(20.0 - STAKE_TOLERANCE_LOT - 0.01)


def test_paper_stake_mismatch_is_refused_within_a_stated_tolerance() -> None:
    bar = CANDLES.index[120]
    stakes = pd.Series([200.0], index=[bar])
    check = {"interval": "4h", "tolerance_bars": 1}
    check_paper_stakes([ft(bar, 200.0)], [lab(bar, 0.1)], stakes, **check)
    near = 200.0 * (1.0 - STAKE_TOLERANCE_REL * 0.9)  # exchange lot rounding
    check_paper_stakes([ft(bar, near)], [lab(bar, 0.1)], stakes, **check)
    with pytest.raises(StakeMismatchError, match="not trading the registered size"):
        check_paper_stakes([ft(bar, 200.0 * 1.02)], [lab(bar, 0.1)], stakes, **check)
    with pytest.raises(StakeMismatchError, match="no stake_amount"):
        check_paper_stakes([ft(bar, None)], [lab(bar, 0.1)], stakes, **check)
    # an entry with no lab counterpart is judged by the timing rules, not here
    far = bar + 50 * H4
    check_paper_stakes([ft(far, 999.0)], [lab(bar, 0.1)], stakes, **check)


def test_a_small_stake_may_round_by_one_lot() -> None:
    """1e-5 BTC at 150k is 1.5 USDT, 7.5% of a 20 stake: a percentage refuses an honest bot."""
    bar = CANDLES.index[120]
    stakes = pd.Series([20.0], index=[bar])
    check = {"interval": "4h", "tolerance_bars": 1}
    check_paper_stakes([ft(bar, 20.0 - STAKE_TOLERANCE_LOT)], [lab(bar, 0.1)], stakes, **check)
    with pytest.raises(StakeMismatchError, match="not trading the registered size"):
        check_paper_stakes(
            [ft(bar, 20.0 - 1.1 * STAKE_TOLERANCE_LOT)], [lab(bar, 0.1)], stakes, **check
        )


def test_an_expected_stake_of_zero_is_a_mismatch_not_a_crash() -> None:
    """It used to reach an uncaught ZeroDivisionError past compare's exit 2."""
    bar = CANDLES.index[120]
    stakes = pd.Series([0.0], index=[bar])
    with pytest.raises(StakeMismatchError, match="no stake"):
        check_paper_stakes(
            [ft(bar, 200.0)], [lab(bar, 0.1)], stakes, interval="4h", tolerance_bars=1
        )


def test_stakes_agree_with_the_strategy_across_a_flat_zero_volume_candle(
    stub_freqtrade: None,
) -> None:
    """A real flat zero-volume candle: lab_candles drops it as synthetic, the public loader keeps
    it. Both sides must then read the same volatility, or every compare is a StakeMismatchError."""
    candles = CANDLES.iloc[:300].copy()
    row = 200
    close = float(candles["close"].iloc[row - 1])
    for column in ("open", "high", "low", "close"):
        candles.iloc[row, candles.columns.get_loc(column)] = close
    candles.iloc[row, candles.columns.get_loc("volume")] = 0.0
    source = render_strategy(PLAN, class_name="TvPaperRisk", sizing=SIZING)
    column = load_strategy(source).populate_indicators(freqtrade_frame(candles), {})["lab_vol"]
    strategy_stakes = [
        sized_stake(
            stop_distance=0.05,
            wallet=1000.0,
            risk_per_trade_pct=1.0,
            max_stake_pct=20.0,
            min_stake_pct=2.0,
            vol_target_annual=0.5,
            realized_vol_annual=float(vol),
        )
        for vol in column.to_numpy()
    ]
    compare_stakes = stakes_by_fill_bar(candles, SIZING, stop_distance=0.05, interval="4h")
    np.testing.assert_array_equal(compare_stakes.iloc[1:].to_numpy(), strategy_stakes[:-1])
    assert compare_stakes.iloc[row + 1] == pytest.approx(20.0)  # the dropped row has no volatility


def test_both_sides_are_sized_by_hand() -> None:
    candles = CANDLES.iloc[:200]
    start, end = candles.index[100], candles.index[140]
    a, b = candles.index[110], candles.index[120]  # 5 and 5 bars of 40, then flat
    trades = [
        ft(a, 200.0, close_profit=-0.05, close_time=a + 5 * H4, close_rate=95.0),
        ft(b, 200.0, close_profit=0.10, close_time=b + 5 * H4),
    ]
    replay, paper = size_both_sides(
        trades,
        [lab(a, -0.04), lab(b, 0.09)],
        candles,
        PLAIN_SIZING,
        stop_distance=0.05,
        interval="4h",
        tolerance_bars=1,
        start=start,
        end=end,
    )
    assert paper.trades == 2 and replay.trades == 2
    assert paper.total_return == pytest.approx((200 * -0.05 + 200 * 0.10) / 1000)
    assert paper.largest_loss_pct == pytest.approx(1.0)  # 10 of 1000
    assert replay.total_return == pytest.approx((200 * -0.04 + 200 * 0.09) / 1000)
    assert replay.largest_loss_pct == pytest.approx(0.8)
    assert paper.exposure == pytest.approx(0.2 * 10 / 40)
    line = sizing_line(PLAIN_SIZING, replay, paper)
    assert "risk per trade 1% of the 1000 wallet" in line and "exposure cap 100%" in line
    assert "largest single loss 1.00% of the wallet" in line
    assert line.endswith("Sizing does not change whether the plan has an edge.")


def test_the_report_and_the_journal_carry_the_sizing_line(tmp_path: Path) -> None:
    """The note stays before the comparison name, which the final-verdict lookup needs last."""
    p = build(synthetic_design(mean=0.001, sd=0.01), claimed=0.5)
    _, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    verdict = evaluate(p, observed(p))
    line = sizing_line(PLAIN_SIZING, *_sides())
    text = append_verdict(
        jpath, verdict, asof=p.decision_end, comparison="comparison-2026-12-25.json", note=line
    )
    assert line in text and text.endswith("Comparison: `comparison-2026-12-25.json`.")
    assert recorded_final_comparison(jpath) == "comparison-2026-12-25.json"


def _sides() -> tuple[Any, Any]:
    from research.paper.compare import SizedSide

    side = SizedSide(trades=1, total_return=0.01, largest_loss_pct=1.0, exposure=0.2)
    return side, side


# --------------------------------------------------------------------------- compare, sized


def make_db(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = (
        "pair",
        "is_open",
        "open_date",
        "close_date",
        "open_rate",
        "close_rate",
        "amount",
        "fee_open",
        "fee_close",
        "close_profit",
        "exit_reason",
        "stake_amount",
    )
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE trades (id INTEGER PRIMARY KEY, {', '.join(columns)})")
    for row in rows:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO trades ({names}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    conn.close()


def _date(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")


def honest_rows(protocol: Any, *, stake_factor: float = 1.0) -> list[dict[str, Any]]:
    """The dry-run DB of a bot that traded exactly like the lab replay, at the sized stake."""
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    end = pd.Timestamp(_Clock.at)
    warm = pd.Timestamp(protocol.data.warm_from, unit="s", tz="UTC")
    history = CANDLES.loc[(CANDLES.index >= warm) & (CANDLES.index < end)]
    trades = replay_trades(PLAN, history, start=start, end=end, costs=frozen_costs(protocol))
    assert len(trades) >= 2
    stakes = stakes_by_fill_bar(history, protocol.sizing, stop_distance=0.05, interval="4h")
    reason = {"signal": "exit_signal", "stop_loss": "stop_loss"}
    rows = []
    for t in trades:
        stake = float(stakes.loc[t.entry_time]) * stake_factor
        closed = t.exit_time is not None
        rows.append(
            {
                "pair": "BTC/USDT",
                "is_open": 0 if closed else 1,
                "open_date": _date(t.entry_time + pd.Timedelta(seconds=5)),
                "close_date": _date(t.exit_time + pd.Timedelta(seconds=5)) if closed else None,
                "open_rate": t.entry_price,
                "close_rate": t.exit_price if closed else None,
                "amount": stake / t.entry_price,
                "fee_open": 0.001,
                "fee_close": 0.001,
                "close_profit": t.return_pct if closed else None,
                "exit_reason": reason.get(t.exit_reason, "exit_signal") if closed else None,
                "stake_amount": stake,
            }
        )
    return rows


def sizing_only_toml(rc: dict[str, Any]) -> str:
    path = rc["tmp"] / "sizing-only.toml"
    text = EXAMPLE.read_text()
    path.write_text(text[text.index("[sizing]") :])
    return str(path)


def compare_argv(rc: dict[str, Any]) -> list[str]:
    return [*rc["base"], "compare", "--candidate", "cand"]


def test_compare_sizes_both_sides_and_writes_the_line(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol))
    assert paper_trade.main(compare_argv(rc)) == 0
    report = next((rc["paper"] / "cand").glob("comparison-*.md")).read_text()
    assert "Sizing: risk per trade 1%" in report
    assert "Sizing does not change whether the plan has an edge" in report
    assert "Sizing: risk per trade 1%" in jpath.read_text()


def test_a_paper_stake_that_is_not_the_frozen_size_is_named_and_stops_nothing(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol, stake_factor=1.3))
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "protocol verdict: continue" in capsys.readouterr().out
    assert recorded_final_verdict(jpath) is None
    line = next(ln for ln in jpath.read_text().splitlines() if ln.startswith("- 20"))
    assert "implementation problem: paper stake:" in line
    assert "not trading the registered size" in line and "Sizing: not computed:" in line
    assert list((rc["paper"] / "cand").glob("comparison-*.json"))


@pytest.mark.parametrize("factor", [1.0, 1.3, 0.7])
def test_the_same_losing_bot_fails_whatever_it_staked(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    factor: float,
) -> None:
    """A final inconclusive used to outrank every fail: staking x1.3 could buy one."""
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (decision + pd.Timedelta(days=1)).to_pydatetime())
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol, stake_factor=factor)[:1])
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "fail"  # type: ignore[index]
    assert ("implementation problem: paper stake:" in jpath.read_text()) is (factor != 1.0)


@pytest.mark.parametrize(
    ("target", "old", "new", "message"),
    [
        ("strategy", "'risk_per_trade_pct': 1.0", "'risk_per_trade_pct': 1.5", "not the strategy"),
        ("strategy", "    base = wallet * ", "    base = 2 * wallet * ", "not the strategy"),
        ("strategy", "    stoploss = -0.05", "    stoploss = -0.5", "not the strategy"),
        ("strategy", "logger = logging", "# edited\nlogger = logging", "not the strategy"),
        ("config", '"max_open_trades": 4', '"max_open_trades": 1', "differs from the frozen"),
        (
            "config",
            '"stake_amount": "unlimited"',
            '"stake_amount": 100.0',
            "differs from the frozen",
        ),
        (
            "config",
            '"dry_run_wallet": 1000.0',
            '"dry_run_wallet": 9000.0',
            "differs from the frozen",
        ),
    ],
)
def test_compare_refuses_files_that_differ_from_the_frozen_sizing(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
    old: str,
    new: str,
    message: str,
) -> None:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol))
    folder = rc["paper"] / "cand"
    path = folder / ("config.json" if target == "config" else f"{class_name_for('cand')}.py")
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    before = jpath.read_text()
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    assert message in capsys.readouterr().err
    assert jpath.read_text() == before


# --------------------------------------------------------------------------- files that differ


def edit_strategy(folder: Path) -> str:
    path = folder / f"{class_name_for('cand')}.py"
    path.write_text(path.read_text().replace("logger = logging", "# edited\nlogger = logging"))
    return "not the strategy this protocol renders (first difference at line"


def edit_warm_from(folder: Path) -> str:
    path = folder / f"{class_name_for('cand')}.py"
    text = path.read_text()
    assert "WARM_FROM = " in text
    path.write_text(text.replace("WARM_FROM = ", "WARM_FROM = 1 + "))
    return "WARM_FROM"


def edit_config(folder: Path) -> str:
    path = folder / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "fee": 0.5}))
    return "differs from the frozen config on ['fee']"


def edit_plan(folder: Path) -> str:
    path = folder / "plan.json"
    plan = json.loads(path.read_text())
    plan["risk"]["stop_loss_pct"] = 4.0
    path.write_text(json.dumps(plan))
    return "is not the registered plan"


def break_plan(folder: Path) -> str:
    (folder / "plan.json").write_text("{")
    return "plan.json"


def delete_config(folder: Path) -> str:
    (folder / "config.json").unlink()
    return "config.json"


EDITS = [edit_strategy, edit_warm_from, edit_config, edit_plan, break_plan, delete_config]


def _sized_test(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, days_past_decision: float
) -> tuple[Path, Path]:
    """A registered sized test, an honest bot, and the clock this far past the decision bar."""
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    at = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (at + pd.Timedelta(days=days_past_decision)).to_pydatetime())
    folder = rc["paper"] / "cand"
    make_db(folder / "dryrun.sqlite", honest_rows(protocol))
    return jpath, folder


@pytest.mark.parametrize("change", EDITS)
def test_an_edited_file_inside_the_grace_exits_2_and_says_what_to_do(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Any,
) -> None:
    jpath, folder = _sized_test(rc, monkeypatch, days_past_decision=6.0)
    message = change(folder)
    before = jpath.read_text()
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    err = capsys.readouterr().err
    assert message in err and "use a new candidate name" not in err
    assert "from " in err and "final inconclusive" in err and "void" in err
    assert jpath.read_text() == before


@pytest.mark.parametrize("change", [edit_strategy, edit_config])
def test_the_advice_is_the_way_out_and_a_new_name_is_not(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Any,
) -> None:
    """The old message sent the owner to a new candidate name, which the duplicate check refuses."""
    jpath, folder = _sized_test(rc, monkeypatch, days_past_decision=6.0)
    change(folder)
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    err = capsys.readouterr().err
    assert "restore the file: delete it and re-run `prepare --candidate cand`" in err
    assert "the same plan under a new candidate name is refused" in err
    argv = ["--candidate", "cand2", "--risk-config", sizing_only_toml(rc)]
    assert prepare(rc, *argv[2:], candidate="cand2") == 2  # what the old advice led to
    capsys.readouterr()
    (folder / "config.json").unlink()
    assert prepare(rc) == 0
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath) is not None


@pytest.mark.parametrize("change", EDITS)
def test_an_edited_file_past_the_grace_is_a_final_void_and_nothing_is_judged(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: Any,
) -> None:
    jpath, folder = _sized_test(rc, monkeypatch, days_past_decision=protocol_grace())
    change(folder)

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a void test is not judged")

    monkeypatch.setattr(paper_trade, "_judge", never)
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "protocol verdict: inconclusive" in capsys.readouterr().out
    recorded = recorded_final_verdict(jpath)
    assert recorded is not None and recorded["outcome"] == "inconclusive"
    line = [ln for ln in jpath.read_text().splitlines() if ln.startswith("- 20")][-1]
    assert "inconclusive: void:" in line and "restore" not in line and "re-run" not in line
    assert not list(folder.glob("comparison-*"))
    before = jpath.read_text()
    assert paper_trade.main(compare_argv(rc)) == 0  # write-once, and no file to recreate
    assert jpath.read_text() == before


def protocol_grace() -> float:
    return float(load_protocol_config().data_grace_days)


def test_a_void_is_a_fail_when_the_breaker_tripped(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    _past_the_grace(protocol, monkeypatch)
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", _one_big_position(protocol))
    edit_config(rc["paper"] / "cand")
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "fail"  # type: ignore[index]
    assert "fail: circuit breaker tripped:" in jpath.read_text()
    assert "also void:" in jpath.read_text()


def test_a_void_is_not_recorded_until_the_breaker_can_be_read(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No candles, no way to say the breaker did not trip: a final inconclusive could hide one."""
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    _past_the_grace(protocol, monkeypatch)
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", _one_big_position(protocol))
    edit_config(rc["paper"] / "cand")

    def down(*a: Any, **k: Any) -> Any:
        raise OSError("no candles")

    monkeypatch.setattr(paper_trade, "_closed_candles", down)
    assert paper_trade.main(compare_argv(rc)) == 2
    assert recorded_final_verdict(jpath) is None


def test_a_moved_folder_past_the_grace_is_no_void(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jpath, _ = _sized_test(rc, monkeypatch, days_past_decision=protocol_grace())
    moved = rc["tmp"] / "moved"
    rc["paper"].rename(moved)
    rc["paper"] = moved
    rc["base"] = ["--paper-dir", str(moved), "--journal-dir", str(rc["journal"])]
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    err = capsys.readouterr().err
    assert "the folder was moved" in err and "final inconclusive" not in err
    assert recorded_final_verdict(jpath) is None
    assert prepare(rc) == 0
    assert paper_trade.main(compare_argv(rc)) == 0
    recorded = recorded_final_verdict(jpath)
    assert recorded is not None and "inconclusive: void:" not in jpath.read_text()


def _point_meta_at_another_db(rc: dict[str, Any]) -> None:
    path = rc["paper"] / "cand" / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["db_path"] = str(rc["tmp"] / "clean.sqlite")
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_a_meta_file_naming_another_db_is_refused_inside_the_grace(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jpath, _ = _sized_test(rc, monkeypatch, days_past_decision=0)
    _point_meta_at_another_db(rc)
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    assert "names another dry-run DB" in capsys.readouterr().err
    assert recorded_final_verdict(jpath) is None


def test_a_meta_file_naming_another_db_is_a_void_past_the_grace(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    jpath, _ = _sized_test(rc, monkeypatch, days_past_decision=protocol_grace())
    _point_meta_at_another_db(rc)
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "inconclusive: void:" in jpath.read_text(encoding="utf-8")


def test_a_moved_folder_that_was_also_edited_is_a_void(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jpath, _ = _sized_test(rc, monkeypatch, days_past_decision=protocol_grace())
    moved = rc["tmp"] / "moved"
    rc["paper"].rename(moved)
    rc["paper"] = moved
    rc["base"] = ["--paper-dir", str(moved), "--journal-dir", str(rc["journal"])]
    edit_strategy(moved / "cand")
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "inconclusive: void:" in jpath.read_text()


def test_a_v7_test_never_voids(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    _past_the_grace(protocol, monkeypatch)
    edit_warm_from(rc["paper"] / "cand")
    before = jpath.read_text()
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    err = capsys.readouterr().err
    assert "final inconclusive" not in err and "re-run `prepare --candidate cand`" in err
    assert jpath.read_text() == before


def test_the_rules_of_a_v8_protocol_state_the_verdict_order() -> None:
    both = "\n".join(sized_protocol(risk_controls=CONTROLS, sizing=SIZING).rules)
    assert "Verdict order: a fail is never demoted" in both
    assert "A paper stake other than the frozen one does not stop judging" in both
    assert "A protected backtest that fails makes only the replay checks unknown" in both
    assert "a final inconclusive naming the void" in both
    breaker = "\n".join(sized_protocol(risk_controls=RiskControls(None, BREAKER)).rules)
    assert "Verdict order: a fail is never demoted" in breaker
    assert "paper stake" not in breaker and "protected backtest" not in breaker
    sizing_only = "\n".join(sized_protocol(sizing=SIZING).rules)
    assert "A paper stake other than" in sizing_only
    assert "protected backtest that fails" not in sizing_only


def test_the_rules_of_a_v7_protocol_do_not_change() -> None:
    plain = sized_protocol(paper_config=None)
    assert plain.version == PROTOCOL_VERSION
    assert not any("Verdict order" in rule for rule in plain.rules)


# --------------------------------------------------------------------------- re-prepare


def _traded(rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path]:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    folder = rc["paper"] / "cand"
    make_db(folder / "dryrun.sqlite", honest_rows(protocol))
    return protocol, jpath, folder


def test_a_re_prepare_that_rewrites_a_file_of_a_traded_test_leaves_a_journal_line(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol, jpath, folder = _traded(rc, monkeypatch)
    sha = protocol_sha256(protocol)
    edit_strategy(folder)
    assert prepare(rc) == 0
    text = jpath.read_text()
    assert REWRITE_HEADING in text and text.index(REWRITE_HEADING) < text.index(VERDICTS_HEADING)
    line = next(ln for ln in text.splitlines() if "re-prepare rewrote" in ln)
    assert f"{class_name_for('cand')}.py" in line and "config.json" not in line
    assert "trades in the dry-run DB" in line
    again, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])  # sha still matches
    assert protocol_sha256(again) == sha


def test_a_re_prepare_line_leaves_the_verdict_parsing_alone(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    protocol, jpath, folder = _traded(rc, monkeypatch)
    _past_the_grace(protocol, monkeypatch)
    edit_strategy(folder)
    assert prepare(rc) == 0
    assert paper_trade.main(compare_argv(rc)) == 0
    recorded, name = recorded_final_verdict(jpath), recorded_final_comparison(jpath)
    assert recorded is not None and name is not None
    (folder / "user_data").mkdir(exist_ok=True)
    edit_config(folder)
    (folder / "config.json").unlink()
    assert prepare(rc) == 0  # a re-prepare after the verdict: its line still goes before it
    assert recorded_final_comparison(jpath) == name
    assert recorded_final_verdict(jpath) == recorded
    assert jpath.read_text().rstrip().endswith(f"Comparison: `{name}`.")


def test_a_deleted_file_counts_as_a_rewrite(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, jpath, folder = _traded(rc, monkeypatch)
    (folder / "config.json").unlink()
    assert prepare(rc) == 0
    assert "re-prepare rewrote config.json," in jpath.read_text()


def test_a_re_prepare_that_changes_nothing_adds_no_line(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, jpath, _ = _traded(rc, monkeypatch)
    assert prepare(rc) == 0
    assert REWRITE_HEADING not in jpath.read_text()


def test_a_re_prepare_before_any_trade_adds_no_line(rc: dict[str, Any]) -> None:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    _, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    edit_strategy(rc["paper"] / "cand")
    assert prepare(rc) == 0
    assert REWRITE_HEADING not in jpath.read_text()


def test_a_v7_re_prepare_of_a_traded_test_adds_no_line(rc: dict[str, Any]) -> None:
    assert prepare(rc) == 0
    _, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    folder = rc["paper"] / "cand"
    make_db(folder / "dryrun.sqlite", [{"pair": "BTC/USDT", "is_open": 1}])
    edit_strategy(folder)
    before = jpath.read_text()
    assert prepare(rc) == 0
    assert jpath.read_text() == before


def test_a_re_prepare_that_stops_half_way_still_leaves_its_line(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, jpath, folder = _traded(rc, monkeypatch)
    edit_strategy(folder)

    def half_way(*a: Any, **k: Any) -> Any:
        (folder / f"{class_name_for('cand')}.py").write_text("half")
        raise OSError("disk full")

    monkeypatch.setattr(paper_trade, "prepare_candidate", half_way)
    assert prepare(rc) == 2
    assert "re-prepare rewrote" in jpath.read_text()


def test_each_kept_line_stays_under_its_own_heading(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, jpath, _ = _traded(rc, monkeypatch)
    assert append_halt(jpath, "- halt one") and append_rewrite(jpath, "- rewrite one")
    assert append_halt(jpath, "- halt two") and append_rewrite(jpath, "- rewrite two")
    assert append_rewrite(jpath, "- rewrite two") is False
    text = jpath.read_text()
    halts = text[: text.index(REWRITE_HEADING)]
    rewrites = text[text.index(REWRITE_HEADING) : text.index(VERDICTS_HEADING)]
    assert "- halt one\n- halt two\n" in halts
    assert "- rewrite one\n- rewrite two\n" in rewrites


# --------------------------------------------------------------------------- the frozen wallet


def breaker_only_toml(rc: dict[str, Any], wallet: float = 1000.0) -> str:
    path = rc["tmp"] / "breaker.toml"
    path.write_text(
        "[risk_controls.circuit_breaker]\n"
        f"max_daily_loss_pct = 3.0\nmax_drawdown_pct = 10.0\nwallet = {wallet}\n"
    )
    return str(path)


def test_the_example_config_loads_and_freezes_the_breaker_wallet() -> None:
    config = load_risk_config(EXAMPLE)
    assert config.risk_controls is not None and config.risk_controls.circuit_breaker is not None
    assert config.risk_controls.circuit_breaker.wallet == 1000.0
    assert config.sizing is not None and config.sizing.wallet == 1000.0


def test_a_breaker_toml_without_a_wallet_is_refused(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = rc["tmp"] / "no-wallet.toml"
    path.write_text(
        "[risk_controls.circuit_breaker]\nmax_daily_loss_pct = 3\nmax_drawdown_pct = 9\n"
    )
    assert prepare(rc, "--risk-config", str(path)) == 2
    assert "missing keys: wallet" in capsys.readouterr().err


def test_a_new_registration_takes_its_wallet_from_the_breaker(rc: dict[str, Any]) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc, 5000.0)) == 0
    config = json.loads((rc["paper"] / "cand" / "config.json").read_text())
    assert config["dry_run_wallet"] == 5000.0


def test_a_new_registration_refuses_a_wallet_against_the_breaker(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc), "--wallet", "5000") == 2
    assert "contradicts the circuit breaker's frozen wallet" in capsys.readouterr().err
    assert not (rc["paper"] / "cand" / "protocol.json").exists()


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--wallet", "1000000"), "contradicts the circuit breaker's frozen wallet"),
        (("--stake-amount", "500"), "--stake-amount would change"),
        (("--public-api-host", "other.example"), "--public-api-host would change"),
        (("--exchange", "kraken"), "--exchange would change"),
    ],
)
def test_a_registered_breaker_test_refuses_a_differing_flag(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str], flags: tuple[str, str], message: str
) -> None:
    """`prepare --wallet 1000000` used to exit 0 with the sha unchanged: a 20% loss read clear."""
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    config = (rc["paper"] / "cand" / "config.json").read_bytes()
    before = frozen_files(rc)
    capsys.readouterr()
    assert prepare(rc, *flags) == 2
    assert message in capsys.readouterr().err
    assert (rc["paper"] / "cand" / "config.json").read_bytes() == config
    assert frozen_files(rc) == before


def test_a_plain_re_prepare_never_changes_a_v8_config(rc: dict[str, Any]) -> None:
    """A flag left out must not reset what the bot trades."""
    extra = ("--stake-amount", "75", "--public-api-host", "data-api.binance.vision")
    assert prepare(rc, "--risk-config", breaker_only_toml(rc), *extra) == 0
    path = rc["paper"] / "cand" / "config.json"
    config = path.read_bytes()
    assert json.loads(config)["stake_amount"] == 75.0
    assert prepare(rc) == 0
    assert path.read_bytes() == config


def test_a_v8_config_edited_away_from_the_frozen_wallet_refuses_re_prepare(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    path = rc["paper"] / "cand" / "config.json"
    path.write_text(path.read_text().replace('"dry_run_wallet": 1000.0', '"dry_run_wallet": 9.0'))
    capsys.readouterr()
    assert prepare(rc) == 2
    assert "differs from the frozen config on ['dry_run_wallet']" in capsys.readouterr().err


def protections_only_toml(rc: dict[str, Any]) -> str:
    path = rc["tmp"] / "protections.toml"
    text = EXAMPLE.read_text()
    begin, end = text.index("[risk_controls.protections]"), text.index("[risk_controls.circuit")
    path.write_text(text[begin:end])
    return str(path)


def _edit(path: Path, change: dict[str, Any]) -> None:
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}, indent=2))


def test_the_config_is_frozen_for_every_block_and_under_the_sha(rc: dict[str, Any]) -> None:
    extra = ("--stake-amount", "75", "--wallet", "500")
    assert prepare(rc, "--risk-config", protections_only_toml(rc), *extra) == 0
    protocol, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    assert protocol.paper_config == PaperConfig("binance", None, 75.0, 500.0, 1)
    assert "Config (frozen): stake 75.0, wallet 500" in " ".join(protocol.rules)
    other = replace(protocol, paper_config=replace(protocol.paper_config, stake_amount=76.0))
    assert protocol_sha256(other) != protocol_sha256(protocol)
    assert protocol_from_dict(protocol_to_dict(protocol)) == protocol


def test_version_rules_of_the_frozen_config() -> None:
    plain = protocol_to_dict(build(synthetic_design(mean=0.001), claimed=0.5))
    blocked = protocol_to_dict(sized_protocol(sizing=SIZING, risk_controls=CONTROLS))
    with pytest.raises(ValueError, match="version 7 cannot carry paper_config"):
        protocol_from_dict({**plain, "paper_config": blocked["paper_config"]})
    without = {k: v for k, v in blocked.items() if k != "paper_config"}
    with pytest.raises(ValueError, match="version 8 needs paper_config"):
        protocol_from_dict(without)
    with pytest.raises(ValueError, match="must freeze its paper_config"):
        sized_protocol(sizing=SIZING, paper_config=None)
    with pytest.raises(ValueError, match="comes with risk_controls or sizing"):
        sized_protocol(paper_config=PaperConfig("binance", None, 100.0, 1000.0, 1))


@pytest.mark.parametrize(
    "edit",
    [
        {"fee": 0.01},  # real freqtrade: removed entries went from 1 to 3 of 29
        {"dry_run_wallet": 200.0},  # 6 of 29
        {"stake_amount": 500.0},  # 9 of 29
        {"max_open_trades": 3},
        {"exchange": {"name": "binance"}},
        {"a_new_key": True},
    ],
)
@pytest.mark.parametrize("toml", [protections_only_toml, breaker_only_toml])
def test_compare_refuses_any_config_edit_whichever_block_the_test_has(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    toml: Any,
    edit: dict[str, Any],
) -> None:
    assert prepare(rc, "--risk-config", toml(rc)) == 0
    _, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", [])
    _edit(rc["paper"] / "cand" / "config.json", edit)
    before = jpath.read_text()
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    assert "differs from the frozen config" in capsys.readouterr().err
    assert jpath.read_text() == before


def test_a_nested_edit_is_named_by_its_path(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", protections_only_toml(rc)) == 0
    path = rc["paper"] / "cand" / "config.json"
    config = json.loads(path.read_text())
    config["exchange"]["pair_whitelist"] = ["ETH/USDT"]
    path.write_text(json.dumps(config))
    capsys.readouterr()
    assert prepare(rc) == 2
    assert "exchange.pair_whitelist" in capsys.readouterr().err


def test_deleting_the_config_frees_nothing(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", protections_only_toml(rc)) == 0
    path = rc["paper"] / "cand" / "config.json"
    original = path.read_bytes()
    path.unlink()
    capsys.readouterr()
    assert prepare(rc, "--stake-amount", "500") == 2
    assert "--stake-amount would change the frozen config" in capsys.readouterr().err
    assert not path.exists()
    assert prepare(rc, "--wallet", "9000") == 2
    assert prepare(rc) == 0
    assert path.read_bytes() == original


def test_a_moved_folder_is_named_as_moved_and_re_prepare_repairs_only_the_path(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", protections_only_toml(rc)) == 0
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", [])
    moved = rc["tmp"] / "moved"
    (rc["paper"]).rename(moved)
    rc["paper"] = moved
    rc["base"] = ["--paper-dir", str(moved), "--journal-dir", str(rc["journal"])]
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 2
    err = capsys.readouterr().err
    assert "the folder was moved" in err and "differs from the frozen" not in err
    assert "it renders plan.json, the strategy, config.json and meta.json again" in err
    old = json.loads((moved / "cand" / "config.json").read_text())
    assert prepare(rc) == 0
    new = json.loads((moved / "cand" / "config.json").read_text())
    assert new["db_url"] == f"sqlite:///{(moved / 'cand' / 'dryrun.sqlite').resolve()}"
    assert {k: v for k, v in new.items() if k != "db_url"} == {
        k: v for k, v in old.items() if k != "db_url"
    }


def test_a_v7_compare_never_reads_the_frozen_config(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert prepare(rc) == 0
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", [])
    _edit(rc["paper"] / "cand" / "config.json", {"fee": 0.01})
    assert paper_trade.main(compare_argv(rc)) == 0


def test_a_v7_re_prepare_keeps_taking_its_flags(rc: dict[str, Any]) -> None:
    """The HC restart path stays exactly as it was."""
    assert prepare(rc) == 0
    assert prepare(rc, "--wallet", "5000", "--stake-amount", "50") == 0
    config = json.loads((rc["paper"] / "cand" / "config.json").read_text())
    assert config["dry_run_wallet"] == 5000.0 and config["stake_amount"] == 50.0


def test_prepare_prints_the_watch_command_for_a_breaker_protocol(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    out = capsys.readouterr().out
    assert "acts only while this runs" in out and "--launch does not start it" in out
    assert f"paper_trade.py --paper-dir {rc['paper']} guard --candidate cand --watch" in out


def test_prepare_prints_no_watch_command_without_a_breaker(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    assert "--watch" not in capsys.readouterr().out


# --------------------------------------------------------------------------- compare, breaker


def _one_big_position(protocol: Any, *, lots: float = 25.0) -> list[dict[str, Any]]:
    """An open position far above the wallet: any move of the coin trips a threshold."""
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    bar = CANDLES.loc[CANDLES.index >= start].iloc[0]
    amount = lots * 1000.0 / float(bar["open"])
    row = {"pair": "BTC/USDT", "is_open": 1, "open_date": _date(start + pd.Timedelta(seconds=5))}
    return [{**row, "open_rate": float(bar["open"]), "amount": amount, "fee_open": 0.001}]


def test_compare_fails_a_trip_nobody_guarded(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", _one_big_position(protocol))
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "protocol verdict: fail" in capsys.readouterr().out
    recorded = recorded_final_verdict(jpath)
    assert recorded is not None and recorded["outcome"] == "fail"
    line = next(ln for ln in jpath.read_text().splitlines() if ln.startswith("- 20"))
    assert "fail: circuit breaker tripped:" in line
    assert "timing" not in line and "irregular" not in line
    assert not (rc["paper"] / "cand" / "halt.lock").exists()  # compare judges, guard halts


def test_compare_of_a_breaker_test_without_a_trip_says_nothing_about_it(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", breaker_only_toml(rc)) == 0
    _, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", [])
    assert paper_trade.main(compare_argv(rc)) == 0
    assert "circuit breaker tripped" not in jpath.read_text()


def _past_the_grace(protocol: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    at = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    at += pd.Timedelta(days=protocol.data.grace_days)
    monkeypatch.setattr(_Clock, "at", at.to_pydatetime())


def test_a_stake_mismatch_at_the_decision_bar_is_a_final_inconclusive(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sized test used to exit 2 at the decision bar and 60 days later, with no verdict."""
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (decision + pd.Timedelta(days=1)).to_pydatetime())
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol, stake_factor=1.3))
    capsys.readouterr()
    assert paper_trade.main(compare_argv(rc)) == 0
    recorded = recorded_final_verdict(jpath)
    assert recorded is not None and recorded["outcome"] == "inconclusive"
    line = [ln for ln in jpath.read_text().splitlines() if ln.startswith("- 20")][-1]
    assert "implementation problem: paper stake:" in line
    assert "not trading the registered size" in line and "Sizing: not computed:" in line
    before = jpath.read_text()
    assert paper_trade.main(compare_argv(rc)) == 0
    assert jpath.read_text() == before


def test_a_correct_stake_gives_the_same_bot_a_better_verdict(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control for the test above: only the stake differs, so a wrong one costs the pass."""
    assert prepare(rc, "--risk-config", sizing_only_toml(rc)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (decision + pd.Timedelta(days=1)).to_pydatetime())
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol))
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "pass"  # type: ignore[index]


def _bridge_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*a: Any, **k: Any) -> Any:
        raise ProtectedBacktestError("freqtrade not installed")

    monkeypatch.setattr(paper_trade, "run_protected_backtest", down)


def test_a_bridge_failure_and_a_good_stake_mark_the_sizing_line_unmasked(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    _past_the_grace(protocol, monkeypatch)
    _bridge_down(monkeypatch)
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol))
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "inconclusive"  # type: ignore[index]
    assert "Its replay figures are unmasked." in jpath.read_text()


def test_a_bridge_failure_and_a_bad_stake_are_both_named_and_a_fail_still_fails(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    _past_the_grace(protocol, monkeypatch)
    _bridge_down(monkeypatch)
    rows = honest_rows(protocol, stake_factor=1.3)[:1]
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", rows)
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "fail"  # type: ignore[index]
    line = [ln for ln in jpath.read_text().splitlines() if ln.startswith("- 20")][-1]
    assert "cannot be judged: protected backtest:" in line
    assert "implementation problem: paper stake:" in line


def test_a_trip_with_a_clipped_stake_is_still_a_fail(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After a trip nobody stopped, freqtrade clips stakes to the shrunken balance. That must not
    turn a final fail into an endless cannot-compare."""
    assert prepare(rc, "--risk-config", str(EXAMPLE)) == 0
    protocol, jpath = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(paper_trade, "has_protections", lambda p: False)
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", honest_rows(protocol, stake_factor=0.5))
    monkeypatch.setattr(
        paper_trade.guard_module, "trip_reason", lambda *a, **k: "circuit breaker tripped: test"
    )
    assert paper_trade.main(compare_argv(rc)) == 0
    assert recorded_final_verdict(jpath)["outcome"] == "fail"  # type: ignore[index]
    assert "not computed after the breaker tripped" in jpath.read_text()


def test_a_no_block_compare_runs_neither_bridge(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not run for a protocol without blocks")

    monkeypatch.setattr(paper_trade, "run_protected_backtest", never)
    monkeypatch.setattr(paper_trade, "size_both_sides", never)
    assert prepare(rc) == 0
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    make_db(rc["paper"] / "cand" / "dryrun.sqlite", [])
    assert paper_trade.main(compare_argv(rc)) == 0
    report = next((rc["paper"] / "cand").glob("comparison-*.md")).read_text()
    assert "Sizing:" not in report and "Protections:" not in report


# --------------------------------------------------------------------------- portfolio


def test_daily_returns_compound_bars_and_drop_incomplete_days() -> None:
    index = pd.date_range("2026-01-01 12:00", periods=4 + 6 + 2, freq="4h", tz="UTC", name="time")
    closes = np.full(len(index), 100.0)
    closes[4:] = 110.0  # the first full day's first bar closes up 10%
    closes[10:] = 121.0
    candles = pd.DataFrame({"close": closes, "open": closes}, index=index)
    trade = ForwardTrade(
        pair="BTC/USDT", is_open=True, open_time=index[3], open_rate=100.0, fee_open=0.0
    )
    daily = daily_paper_returns([trade], candles, interval="4h")
    assert list(daily.index) == [pd.Timestamp("2026-01-02", tz="UTC")]
    assert daily.iloc[0] == pytest.approx(0.10)


def test_daily_coin_returns_ignore_the_bot_and_drop_incomplete_days() -> None:
    index = pd.date_range("2026-01-01 12:00", periods=4 + 6 + 2, freq="4h", tz="UTC", name="time")
    opens = np.full(len(index), 100.0)
    closes = np.full(len(index), 100.0)
    closes[8] = 90.0  # the last bar of the only complete day
    candles = pd.DataFrame({"open": opens, "close": closes}, index=index)
    daily = daily_coin_returns(candles, interval="4h")
    assert list(daily.index) == [pd.Timestamp("2026-01-02", tz="UTC")]
    assert daily.iloc[0] == pytest.approx(-0.10)


def test_daily_in_market_uses_the_position_at_close_rule() -> None:
    index = pd.date_range("2026-01-01", periods=18, freq="4h", tz="UTC", name="time")
    candles = pd.DataFrame({"open": 100.0, "close": 100.0}, index=index)
    closed = ForwardTrade(
        pair="BTC/USDT",
        is_open=False,
        open_time=index[7],  # day 2, second bar
        open_rate=100.0,
        close_time=index[9] + pd.Timedelta(minutes=5),
        close_rate=100.0,
    )
    daily = daily_in_market([closed], candles, interval="4h")
    assert daily.tolist() == [False, True, False]


PORTFOLIO_END = "2026-11-12"


@pytest.fixture
def portfolio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Three registered tests with dry-run DBs: x and y held the same coin path, z another."""
    paper, journal = tmp_path / "paper", tmp_path / "journal"
    created = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
    config = replace(load_protocol_config(), min_days=10.0, max_days=10.0)
    frames: dict[str, pd.DataFrame] = {}
    specs = (("px", "BTCUSDT", 21), ("py", "ETHUSDT", 21), ("pz", "SOLUSDT", 22))
    for name, symbol, seed in specs:
        plan = plan_for(symbol, "close > slow")
        protocol = build_protocol(
            candidate=name,
            plan=plan,
            design=shadow_design(),
            costs=COSTS,
            config=config,
            created_at=created,
            link=LINK,
            claimed_sharpe_annual=1.0,
            claim_source="owner_supplied",
            claim_note="test claim",
        )
        register_protocol(protocol, paper_dir=paper, journal_dir=journal)
        prepared = prepare_candidate(
            plan,
            candidate=name,
            paper_dir=paper,
            warm_from=protocol.data.warm_from,
            start=protocol.start,
        )
        frames[symbol] = synthetic(protocol.start, 70, seed)
        pair = f"{symbol[:-4]}/USDT"
        make_db(
            prepared.db_path,
            [
                {
                    "pair": pair,
                    "is_open": 1,
                    "open_date": _date(frames[symbol].index[0]),
                    "open_rate": float(frames[symbol]["open"].iloc[0]),
                    "fee_open": 0.0,
                }
            ],
        )
    (paper / "jev-shadow").mkdir()
    (paper / "jev-shadow" / "decisions.jsonl").write_text("{}\n")
    monkeypatch.setattr(
        paper_trade,
        "public_kline_loader",
        lambda since, host: lambda symbol, interval, end: frames[symbol],
    )
    monkeypatch.setattr(paper_trade, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2026, 11, 13, 0, 0, tzinfo=UTC))
    return {"base": ["--paper-dir", str(paper), "--journal-dir", str(journal)], "root": tmp_path}


def tree(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_portfolio_prints_n_eff_and_writes_nothing(
    portfolio: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(portfolio["root"])
    argv = [*portfolio["base"], "compare", "--portfolio", "--end", PORTFOLIO_END]
    assert paper_trade.main(argv) == 0
    out = capsys.readouterr().out
    assert "N_eff to quote" in out and "of 3 coins" in out and "of 3 series" in out
    assert "Information only" in out and "bias N_eff low" in out
    assert "jev-shadow" not in out
    assert tree(portfolio["root"]) == before


def test_portfolio_candidates_narrow_it_and_equal_paths_count_as_one_bet(
    portfolio: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*portfolio["base"], "compare", "--portfolio", "--candidates", "px,py"]
    assert paper_trade.main([*argv, "--end", PORTFOLIO_END]) == 0
    out = capsys.readouterr().out
    assert "N_eff to quote (the coins' own daily returns): 1.00 of 2 coins" in out
    assert "(do not quote): 1.00 of 2 series" in out


def test_portfolio_default_is_the_largest_cohort_and_names_the_rest(
    portfolio: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """One late entrant used to cut every test's window to its own few days."""
    paper = portfolio["root"] / "paper"
    plan = plan_for("SUIUSDT", "close > slow")
    late = build_protocol(
        candidate="plate",
        plan=plan,
        design=shadow_design(),
        costs=COSTS,
        config=replace(load_protocol_config(), min_days=10.0, max_days=10.0),
        created_at=datetime(2026, 11, 9, 10, 30, tzinfo=UTC),
        link=LINK,
        claimed_sharpe_annual=1.0,
        claim_source="owner_supplied",
        claim_note="test claim",
    )
    register_protocol(late, paper_dir=paper, journal_dir=portfolio["root"] / "journal")
    prepare_candidate(
        plan, candidate="plate", paper_dir=paper, warm_from=late.data.warm_from, start=late.start
    )
    argv = [*portfolio["base"], "compare", "--portfolio", "--end", PORTFOLIO_END]
    assert paper_trade.main(argv) == 0
    out = capsys.readouterr().out
    assert "of 3 coins" in out
    assert "Excluded plate: started 2026-11-09" in out


def test_portfolio_reports_days_both_in_market(
    portfolio: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*portfolio["base"], "compare", "--portfolio", "--end", PORTFOLIO_END]
    assert paper_trade.main(argv) == 0
    assert "px / py: " in capsys.readouterr().out


def test_portfolio_exit_2_on_an_unknown_test(
    portfolio: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*portfolio["base"], "compare", "--portfolio", "--candidates", "px,nope"]
    assert paper_trade.main(argv) == 2
    assert "cannot compare" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["compare"], ["compare", "--candidates", "a"], ["compare", "--portfolio", "--candidate", "a"]],
)
def test_candidate_stays_required_except_with_portfolio(args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        paper_trade.main(args)
    assert caught.value.code == 2


# --------------------------------------------------------------------------- shadow judge


def test_a_later_v8_candidate_cannot_join_the_shadow_judge(tmp_path: Path) -> None:
    """The frozen candidate set is the genesis row's. A protocol registered afterwards, version
    8 included, is reported as ignored, and the judge keeps its candidates and hashes."""
    world = make_world(tmp_path)
    world.run()
    plan = {**plan_for("SOLUSDT", "close > slow"), "risk": PLAN["risk"]}
    created = datetime.fromtimestamp(world.a.start - 3600, UTC)
    late = build_protocol(
        candidate="hc-sol-v8",
        plan=plan,
        design=shadow_design(),
        costs=COSTS,
        config=replace(load_protocol_config(), min_days=2.0, max_days=2.0),
        created_at=created,
        link=LINK,
        claimed_sharpe_annual=1.0,
        claim_source="owner_supplied",
        claim_note="test claim",
        sizing=SIZING,
        risk_controls=CONTROLS,
        paper_config=frozen_config_for(sizing=SIZING, risk_controls=CONTROLS),
    )
    register_protocol(late, paper_dir=world.paper, journal_dir=world.journal)
    prepare_candidate(
        plan,
        candidate="hc-sol-v8",
        paper_dir=world.paper,
        warm_from=late.data.warm_from,
        start=late.start,
        sizing=SIZING,
        risk_controls=CONTROLS,
    )
    found, _ = shadow.registered_candidates(world.paper, world.journal)
    assert found["hc-sol-v8"].protocol.version == 8  # it loads: only the genesis keeps it out
    world.clock.now = world.first_fill + 30
    summary = world.run()
    assert any("hc-sol-v8" in w and "after the shadow genesis" in w for w in summary.warnings)
    assert "hc-sol-v8" not in summary.candidates
    assert "hc-sol-v8" not in json.dumps(world.rows())
