"""freqtrade dry-run rendering: valid strategy source, signal parity with the lab, forced
dry-run configs without credentials, and a launcher that refuses anything but a prepared
``freqtrade trade --dry-run``. Freqtrade is never imported or started."""

from __future__ import annotations

import ast
import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import load_config
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan, plan_hash
from lab.types import TrialRecord
from research.paper.freqtrade import (
    FREQTRADE_INSTALL_HINT,
    PreparedPaper,
    check_paper_config,
    class_name_for,
    launch,
    prepare_candidate,
    render_config,
    render_strategy,
    startup_candle_count,
    symbol_to_pair,
)

PLAN: dict[str, Any] = {
    "name": "ema_rsi_paper",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "features": {
        "fast": {"ema": {"period": 12}},
        "slow": {"ema": {"period": 40}},
        "rsi": {"rsi": {"period": 14}},
    },
    "long_entry": "crosses_above(fast, slow) and rsi < 70",
    "long_exit": "crosses_below(fast, slow) or rsi > prev(rsi, 2) + 15",
    "risk": {"position_size_pct": 100, "stop_loss_pct": 5, "take_profit_pct": 8},
}
FAKE_SECRET = "fake-exchange-secret-for-tests-0000"


def freqtrade_frame(n: int = 600, *, seed: int = 3) -> pd.DataFrame:
    """A freqtrade-style OHLCV frame: ``date`` column (UTC), RangeIndex."""
    rng = np.random.default_rng(seed)
    close = 30_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, n)))
    open_ = np.r_[30_000.0, close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.003, n)) * close
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(1.0, 50.0, n),
        }
    )


def lab_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.set_index(pd.DatetimeIndex(df["date"], name="time"))[
        ["open", "high", "low", "close", "volume"]
    ].astype(float)
    out["tradable"] = out["volume"] > 0
    return out


@pytest.fixture
def fake_freqtrade(monkeypatch: pytest.MonkeyPatch) -> type:
    """A minimal ``freqtrade.strategy.IStrategy`` stub in sys.modules (no freqtrade install)."""

    class IStrategy:
        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

    pkg = types.ModuleType("freqtrade")
    strategy = types.ModuleType("freqtrade.strategy")
    strategy.IStrategy = IStrategy  # type: ignore[attr-defined]
    pkg.strategy = strategy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "freqtrade", pkg)
    monkeypatch.setitem(sys.modules, "freqtrade.strategy", strategy)
    return IStrategy


def load_rendered(source: str, name: str = "rendered_strategy") -> types.ModuleType:
    module = types.ModuleType(name)
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


# --------------------------------------------------------------------------- strategy source


def test_rendered_strategy_is_valid_python_with_v3_interface() -> None:
    src = render_strategy(PLAN, class_name="TvPaperTest")
    tree = ast.parse(src)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == ["TvPaperTest"]
    assert "INTERFACE_VERSION = 3" in src
    assert "timeframe = '1h'" in src
    assert "can_short = False" in src
    assert "process_only_new_candles = True" in src
    assert "minimal_roi = {'0': 0.08}" in src
    assert "stoploss = -0.05" in src
    assert "from lab.plan import compile_plan, parse_plan" in src
    methods = {n.name for n in classes[0].body if isinstance(n, ast.FunctionDef)}
    assert {"populate_indicators", "populate_entry_trend", "populate_exit_trend"} <= methods


def test_strategy_without_risk_disables_roi_and_stoploss() -> None:
    plan = {**PLAN, "risk": {"position_size_pct": 100}}
    src = render_strategy(plan, class_name="NoRisk")
    assert "minimal_roi = {'0': 100.0}" in src
    assert "stoploss = -0.99" in src


def test_startup_candles_cover_three_lookbacks_min_200() -> None:
    # slowest feature EMA(40): warm-up 39 + rule look-back 2 -> 41; 3 x 41 = 123 -> floor 200
    assert startup_candle_count(PLAN) == 200
    long_plan = {**PLAN, "features": {**PLAN["features"], "slow": {"ema": {"period": 150}}}}
    assert startup_candle_count(long_plan) == 3 * (149 + 2)


@pytest.mark.parametrize("bad", ["1Bad", "has space", "class", "", "a-b"])
def test_class_name_must_be_identifier(bad: str) -> None:
    with pytest.raises(ValueError, match="class_name"):
        render_strategy(PLAN, class_name=bad)


def test_invalid_plan_is_rejected_before_rendering() -> None:
    bad = {**PLAN, "long_entry": "rsi <"}
    with pytest.raises(ValueError, match="long_entry"):
        render_strategy(bad, class_name="X")


def test_plan_text_cannot_inject_code(fake_freqtrade: type) -> None:
    sneaky = {**PLAN, "description": '"""\nimport os\nos.system("echo pwned")\n"""'}
    src = render_strategy(sneaky, class_name="Sneaky")
    tree = ast.parse(src)
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
    assert all("os" not in [a.name for a in n.names] for n in imports)
    module = load_rendered(src)
    assert module.PLAN.description == sneaky["description"]


def test_signal_parity_with_lab(fake_freqtrade: type) -> None:
    """The rendered populate_* methods set exactly the lab's compiled signals."""
    module = load_rendered(render_strategy(PLAN, class_name="Parity"))
    strat = module.Parity({})
    df = freqtrade_frame()
    out = strat.populate_indicators(df.copy(), {"pair": "BTC/USDT"})
    out = strat.populate_entry_trend(out, {"pair": "BTC/USDT"})
    out = strat.populate_exit_trend(out, {"pair": "BTC/USDT"})

    expected = compile_plan(parse_plan(PLAN), lab_frame(df))
    assert expected.entries.sum() > 0 and expected.exits.sum() > 0  # the test is not vacuous
    np.testing.assert_array_equal(out["enter_long"].to_numpy(), expected.entries.to_numpy())
    np.testing.assert_array_equal(out["exit_long"].to_numpy(), expected.exits.to_numpy())
    assert set(np.unique(out["enter_long"])) <= {0, 1}


def test_freqtrade_filled_candles_are_dropped_not_forward_filled(fake_freqtrade: type) -> None:
    module = load_rendered(render_strategy(PLAN, class_name="Gaps"))
    df = freqtrade_frame()
    gap = [300, 301, 302]
    filled = df.copy()
    for i in gap:  # freqtrade's fill-up: previous close, zero volume
        c = filled.loc[i - 1, "close"] if i == gap[0] else filled.loc[gap[0] - 1, "close"]
        filled.loc[i, ["open", "high", "low", "close", "volume"]] = [c, c, c, c, 0.0]
    out = module.Gaps({}).populate_indicators(filled.copy(), {})

    without_gap = df.drop(index=gap).reset_index(drop=True)
    expected = compile_plan(parse_plan(PLAN), lab_frame(without_gap))
    kept = np.ones(len(df), dtype=bool)
    kept[gap] = False
    assert not out["lab_entry"].to_numpy()[gap].any()
    np.testing.assert_array_equal(out["lab_entry"].to_numpy()[kept], expected.entries.to_numpy())
    np.testing.assert_array_equal(out["lab_exit"].to_numpy()[kept], expected.exits.to_numpy())


# --------------------------------------------------------------------------- config


def test_config_is_forced_dry_run_without_credentials(tmp_path: Path) -> None:
    cfg = render_config(plan=PLAN, db_path=tmp_path / "db.sqlite")
    assert cfg["dry_run"] is True
    assert cfg["exchange"]["key"] == "" and cfg["exchange"]["secret"] == ""
    assert cfg["exchange"]["pair_whitelist"] == ["BTC/USDT"]
    assert cfg["pairlists"] == [{"method": "StaticPairList"}]
    # Both off by default; freqtrade's schema rejects either block without its address/token.
    assert "api_server" not in cfg and "telegram" not in cfg
    assert cfg["db_url"] == f"sqlite:///{(tmp_path / 'db.sqlite').resolve()}"
    assert cfg["timeframe"] == "1h"
    assert cfg["entry_pricing"]["price_side"] == "other"
    assert "ccxt_config" in cfg["exchange"] and cfg["exchange"]["ccxt_config"] == {}
    check_paper_config(cfg)


def test_config_refuses_live_trading(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dry_run"):
        render_config(plan=PLAN, db_path=tmp_path / "db.sqlite", dry_run=False)


@pytest.mark.parametrize(
    "options",
    [
        {"key": FAKE_SECRET},
        {"secret": FAKE_SECRET},
        {"password": FAKE_SECRET},
        {"ccxt_config": {"apiKey": FAKE_SECRET}},
        {"ccxt_config": {"options": {"privateKey": FAKE_SECRET}}},
        {"uid": "123"},
    ],
)
def test_config_refuses_any_supplied_credential(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="credentials"):
        render_config(plan=PLAN, db_path=tmp_path / "db.sqlite", exchange_options=options)


def test_check_paper_config_rejects_tampered_configs(tmp_path: Path) -> None:
    good = render_config(plan=PLAN, db_path=tmp_path / "db.sqlite")
    for mutate in (
        lambda c: c.update(dry_run=False),
        lambda c: c.update(dry_run="true"),
        lambda c: c["exchange"].update(key=FAKE_SECRET),
        lambda c: c.update(api_server={"enabled": True, "password": FAKE_SECRET}),
        lambda c: c.update(telegram={"enabled": True, "token": FAKE_SECRET}),
    ):
        cfg = json.loads(json.dumps(good))
        mutate(cfg)
        with pytest.raises(ValueError):
            check_paper_config(cfg)


def test_public_api_host_override(tmp_path: Path) -> None:
    cfg = render_config(
        plan=PLAN, db_path=tmp_path / "db.sqlite", public_api_host="data-api.binance.vision"
    )
    urls = {
        "urls": {"api": {"public": "https://data-api.binance.vision/api/v3"}},
        "options": {"fetchMarkets": {"types": ["spot"]}},
    }
    assert cfg["exchange"]["ccxt_config"] == urls
    assert cfg["exchange"]["ccxt_async_config"] == urls
    assert cfg["exchange"]["enable_ws"] is False
    with pytest.raises(ValueError, match="bare host"):
        render_config(plan=PLAN, db_path=tmp_path / "db", public_api_host="evil.com/x?y=")


def test_public_api_host_is_binance_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only applies to exchange 'binance'"):
        render_config(
            plan=PLAN,
            db_path=tmp_path / "db",
            exchange="okx",
            public_api_host="data-api.binance.vision",
        )


def test_exchange_options_cannot_replace_public_api_host_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ccxt_config"):
        render_config(
            plan=PLAN,
            db_path=tmp_path / "db",
            public_api_host="data-api.binance.vision",
            exchange_options={"ccxt_config": {"timeout": 30000}},
        )


@pytest.mark.parametrize(
    ("symbol", "pair"),
    [
        ("BTCUSDT", "BTC/USDT"),
        ("ETHBTC", "ETH/BTC"),
        ("SOLFDUSD", "SOL/FDUSD"),
        ("BNBEUR", "BNB/EUR"),
    ],
)
def test_symbol_to_pair(symbol: str, pair: str) -> None:
    assert symbol_to_pair(symbol) == pair


def test_unknown_quote_and_stake_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="base/quote"):
        symbol_to_pair("ABCXYZ")
    with pytest.raises(ValueError, match="stake_currency"):
        render_config(plan=PLAN, db_path=tmp_path / "db", stake_currency="BTC")


# --------------------------------------------------------------------------- prepare / launch


def test_prepare_writes_files_and_dry_run_command(tmp_path: Path) -> None:
    prepared = prepare_candidate(PLAN, candidate="ema-rsi_1", paper_dir=tmp_path)
    folder = tmp_path / "ema-rsi_1"
    assert prepared.dir == folder
    assert prepared.class_name == class_name_for("ema-rsi_1") == "TvPaperEmaRsi1"
    assert prepared.strategy_path == folder / "TvPaperEmaRsi1.py"
    ast.parse(prepared.strategy_path.read_text())
    config = json.loads(prepared.config_path.read_text())
    check_paper_config(config)
    assert json.loads((folder / "plan.json").read_text())["name"] == PLAN["name"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["plan_hash"] == prepared.plan_hash and meta["prepared_at"]
    assert (folder / "user_data").is_dir()
    cmd = prepared.command
    assert cmd[:3] == ["freqtrade", "trade", "--dry-run"]
    assert cmd[cmd.index("--config") + 1] == str(prepared.config_path)
    assert cmd[cmd.index("--strategy") + 1] == "TvPaperEmaRsi1"
    assert cmd[cmd.index("--strategy-path") + 1] == str(folder)
    assert cmd[cmd.index("--db-url") + 1] == config["db_url"]


def test_prepare_never_writes_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", FAKE_SECRET)
    monkeypatch.setenv("BINANCE_API_SECRET", FAKE_SECRET)
    prepared = prepare_candidate(PLAN, candidate="nosecrets", paper_dir=tmp_path)
    for path in prepared.dir.rglob("*"):
        if path.is_file():
            assert FAKE_SECRET not in path.read_text()


def test_prepare_refuses_dry_run_false_and_bad_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dry_run"):
        prepare_candidate(PLAN, candidate="x", paper_dir=tmp_path, dry_run=False)
    for bad in ("../escape", "a/b", "", "-lead"):
        with pytest.raises(ValueError, match="candidate"):
            prepare_candidate(PLAN, candidate=bad, paper_dir=tmp_path)


def test_prepare_refuses_a_different_plan_under_the_same_name(tmp_path: Path) -> None:
    first = prepare_candidate(PLAN, candidate="same", paper_dir=tmp_path)
    again = prepare_candidate(PLAN, candidate="same", paper_dir=tmp_path)
    assert again.plan_hash == first.plan_hash
    other = {**PLAN, "risk": {"position_size_pct": 100, "stop_loss_pct": 7}}
    with pytest.raises(ValueError, match="new candidate name"):
        prepare_candidate(other, candidate="same", paper_dir=tmp_path)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> str:
        self.calls.append((command, kwargs))
        return "fake-process"


def test_launch_passes_the_dry_run_command_to_the_runner(tmp_path: Path) -> None:
    prepared = prepare_candidate(PLAN, candidate="go", paper_dir=tmp_path)
    runner = FakeRunner()
    env = {"PATH": "/bin", "FREQTRADE__EXCHANGE__KEY": FAKE_SECRET, "HOME": "/tmp"}
    result = launch(prepared, runner=runner, which=lambda _: "/usr/bin/freqtrade", env=env)
    assert result.started and result.process == "fake-process"
    ((command, kwargs),) = runner.calls
    assert command == prepared.command and "--dry-run" in command
    assert kwargs["cwd"] == str(prepared.dir)
    assert "FREQTRADE__EXCHANGE__KEY" not in kwargs["env"]
    assert kwargs["env"]["FREQTRADE__DRY_RUN"] == "true"
    assert FAKE_SECRET not in json.dumps(kwargs["env"])


def test_launch_refuses_a_command_without_dry_run(tmp_path: Path) -> None:
    prepared = prepare_candidate(PLAN, candidate="live", paper_dir=tmp_path)
    runner = FakeRunner()
    for command in (
        [a for a in prepared.command if a != "--dry-run"],
        ["freqtrade", "download-data", "--dry-run"],
        [*prepared.command, "--config", str(tmp_path / "other.json")],
    ):
        tampered = PreparedPaper(
            dir=prepared.dir,
            strategy_path=prepared.strategy_path,
            config_path=prepared.config_path,
            command=command,
        )
        with pytest.raises(ValueError, match=r"refusing|exactly the prepared config"):
            launch(tampered, runner=runner, which=lambda _: "/usr/bin/freqtrade")
    assert runner.calls == []


def test_launch_refuses_a_tampered_config(tmp_path: Path) -> None:
    prepared = prepare_candidate(PLAN, candidate="tamper", paper_dir=tmp_path)
    config = json.loads(prepared.config_path.read_text())
    config["dry_run"] = False
    prepared.config_path.write_text(json.dumps(config))
    runner = FakeRunner()
    with pytest.raises(ValueError, match="dry_run"):
        launch(prepared, runner=runner, which=lambda _: "/usr/bin/freqtrade")
    assert runner.calls == []


def test_launch_without_freqtrade_reports_not_installed(tmp_path: Path) -> None:
    prepared = prepare_candidate(PLAN, candidate="absent", paper_dir=tmp_path)
    runner = FakeRunner()
    result = launch(prepared, runner=runner, which=lambda _: None)
    assert result.started is False
    assert result.message == FREQTRADE_INSTALL_HINT
    assert "freqtrade not installed" in result.message
    assert runner.calls == []


# --------------------------------------------------------------------------- CLI


def test_cli_prepare_prints_the_dry_run_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import paper_trade

    # prepare runs the design-window backtest for the protocol: keep it offline with seeded
    # synthetic 1h candles (the seed plan's interval) and a 9-month design window.
    base = load_config()
    splits = replace(
        base.splits,
        design_start=pd.Timestamp("2025-01-01", tz="UTC"),
        holdout_start=pd.Timestamp("2025-10-01", tz="UTC"),
    )
    lab_cfg = replace(base, data_dir=tmp_path / "lab", splits=splits)
    n = 24 * 304  # 2024-12-01 .. 2025-10-01
    rng = np.random.default_rng(11)
    close = 30_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, n)))
    open_ = np.r_[30_000.0, close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.003, n)) * close
    candles = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(1.0, 50.0, n),
            "tradable": np.ones(n, dtype=bool),
        },
        index=pd.date_range("2024-12-01", periods=n, freq="1h", tz="UTC", name="time"),
    )
    monkeypatch.setattr(paper_trade, "load_config", lambda: lab_cfg)
    monkeypatch.setattr(paper_trade, "default_loader", lambda cfg: lambda *a: candles)

    seed_file = Path(__file__).resolve().parents[1] / "seeds" / "freqtrade_strategy001.json"
    # prepare needs the plan in the lab ledger (read only): a tmp ledger holding the seed plan's
    # design-window trial and one walk-forward result (its OOS Sharpe becomes the claim)
    seed_plan = json.loads(seed_file.read_text())["plan"]
    parsed = parse_plan(seed_plan)
    ledger_path = tmp_path / "ledger.db"
    with Ledger(ledger_path) as led:
        led.start_research_run("r-seed", label="seed", config_json="{}", config_sha256="0" * 64)
        trial_id = led.record(
            TrialRecord(
                run_id="r-seed",
                source="api",
                status="ok",
                plan_hash=plan_hash(parsed),
                plan_json=json.dumps(seed_plan),
                params_json="{}",
                symbol=parsed.symbol,
                interval=parsed.interval,
                window_kind="design",
                window_start=pd.Timestamp("2025-01-01", tz="UTC"),
                window_end=pd.Timestamp("2025-10-01", tz="UTC"),
                metrics_json="{}",
                sharpe_per_bar=0.0,
                n_trades=3,
                returns=np.zeros(8),
            )
        )
        led.record_evidence(
            "r-seed",
            plan_hash(parsed),
            "walk_forward",
            json.dumps({"oos_metrics": {"sharpe": 0.3}}),
        )
    argv = [
        "--paper-dir",
        str(tmp_path),
        "--journal-dir",
        str(tmp_path / "journal"),
        "prepare",
        "--plan-file",
        str(seed_file),
        "--ledger",
        str(ledger_path),
    ]
    assert paper_trade.main([*argv, "--candidate", "s001"]) == 0
    out = capsys.readouterr().out
    assert "freqtrade trade --dry-run --config" in out
    assert (tmp_path / "s001" / "config.json").is_file()
    protocol = json.loads((tmp_path / "s001" / "protocol.json").read_text())
    assert protocol["source"]["trial_id"] == trial_id and protocol["source"]["run_id"] == "r-seed"
    assert protocol["sharpe"]["claim_source"] == "walk_forward_oos"
    assert not (tmp_path / "lab" / "ledger.db").exists()  # the lab default was never touched

    runner = FakeRunner()

    def fake_launch(prepared: PreparedPaper) -> Any:
        return launch(prepared, runner=runner, which=lambda _: "/usr/bin/freqtrade")

    monkeypatch.setattr(paper_trade, "launch", fake_launch)
    assert paper_trade.main([*argv, "--candidate", "s001", "--launch"]) == 0
    assert runner.calls and "--dry-run" in runner.calls[0][0]


def test_cli_prepare_rejects_an_invalid_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import paper_trade

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**PLAN, "long_entry": "nonsense("}))
    argv = ["--paper-dir", str(tmp_path), "prepare", "--plan-file", str(bad), "--candidate", "b"]
    assert paper_trade.main(argv) == 2
    assert "long_entry" in capsys.readouterr().err
