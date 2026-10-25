"""Render a validated plan as a freqtrade dry-run bot (paper trading only).

* :func:`render_strategy` writes a freqtrade ``IStrategy`` (interface v3) whose
  ``populate_indicators`` calls **our lab's** ``compile_plan`` on the closed candles, so the
  entry/exit rules are the same code as the backtest's. The signal is decided on bar t's close;
  freqtrade fills right after the close, i.e. at roughly bar t+1's open (the lab's fill rule).
  Signals are identical only for features whose value depends on a **bounded look-back** (EMA
  after warm-up, RSI, ATR, rolling windows). History-dependent features such as
  ``regime_signal`` (expanding exponentially weighted Markov transition counts) depend on every
  bar seen so far; freqtrade hands ``populate_indicators`` only the candles it holds (about
  ``startup_candle_count`` plus the bars since start), so those values, and the signals built on
  them, can differ from the lab's full-history backtest. The weekly comparison
  (:mod:`research.paper.compare`) then reports ``diverging``: a mismatch never shows up as a
  false pass. ``startup_candle_count`` is not raised for regime features here; to narrow the gap,
  set a larger value in the generated file by hand (freqtrade must be able to download that many
  candles) and re-check the comparison.
* :func:`render_config` builds a freqtrade config that is ALWAYS ``dry_run: true`` with empty
  exchange credentials. It raises ``ValueError`` if asked for anything else.
* :func:`prepare_candidate` writes ``PAPER_DIR/<candidate>/`` (plan, strategy, config, meta)
  and returns the ``freqtrade trade --dry-run`` command; :func:`launch` re-checks every safety
  property before starting it.

Freqtrade is GPL-3.0 and runs as a separate process in its own virtualenv (with ``lab``
installed editable there); nothing in this module imports it. The generated strategy file
imports freqtrade, but that file only runs inside freqtrade's own environment.
"""

from __future__ import annotations

import json
import keyword
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.plan.validator import checked_expressions, parse_plan, plan_hash, plan_to_dict
from lab.registry import REGISTRY
from lab.research.contract import PAPER_DIR
from lab.types import Plan

FREQTRADE_INSTALL_HINT = (
    "freqtrade not installed (pip install in its own venv: python -m venv ~/.venvs/freqtrade && "
    "~/.venvs/freqtrade/bin/pip install freqtrade && ~/.venvs/freqtrade/bin/pip install -e "
    "<repo>/lab, then put ~/.venvs/freqtrade/bin on PATH)"
)
# Longest first so "FDUSD" wins over "USD" and "USDT" over "USD".
KNOWN_QUOTES: tuple[str, ...] = (
    "FDUSD",
    "USDT",
    "USDC",
    "BUSD",
    "TUSD",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
    "USD",
)
MIN_STARTUP_CANDLES = 200  # freqtrade's usual minimum warm-up for recursive indicators
STARTUP_LOOKBACK_MULT = 3  # recursive EMA/RSI converge to the full-history value within ~3x
DISABLED_ROI = {"0": 100.0}  # freqtrade's documented way to disable ROI exits (+10000%)
NO_STOPLOSS = -0.99  # freqtrade requires a stoploss; -99% means "none" like the lab's null
CANDIDATE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Config keys that hold credentials anywhere in a freqtrade config. Only exchange.key and
# exchange.secret may appear, and only as empty strings.
CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "key",
        "secret",
        "password",
        "uid",
        "apikey",
        "apisecret",
        "secretkey",
        "privatekey",
        "walletaddress",
        "token",
        "jwtsecretkey",
        "wstoken",
        "username",
    }
)


def _norm_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# --------------------------------------------------------------------------- plan helpers


def _parse(plan: Mapping[str, Any] | Plan) -> Plan:
    return plan if isinstance(plan, Plan) else parse_plan(plan)


def plan_lookback_bars(plan: Mapping[str, Any] | Plan) -> int:
    """Longest warm-up of the plan: max feature warm-up + the rules' look-back (prev/crosses)."""
    p = _parse(plan)
    entry, exit_ = checked_expressions(p)
    warm = 0
    for ref in p.features:
        spec = REGISTRY[ref.feature]
        params = {q.name: ref.params.get(q.name, q.default) for q in spec.params}
        warm = max(warm, int(spec.warmup(params)))
    return warm + max(entry.lag, exit_.lag)


def startup_candle_count(plan: Mapping[str, Any] | Plan) -> int:
    """``max(200, 3 x longest look-back)`` candles of warm-up for freqtrade.

    Enough for bounded look-back features to match the lab. It does not make history-dependent
    features (``regime_signal``) match: those depend on all prior bars, so the forward
    comparison can report ``diverging`` for them (see the module docstring)."""
    return max(MIN_STARTUP_CANDLES, STARTUP_LOOKBACK_MULT * plan_lookback_bars(plan))


def symbol_to_pair(symbol: str) -> str:
    """``"BTCUSDT"`` -> ``"BTC/USDT"`` using :data:`KNOWN_QUOTES`; ``ValueError`` otherwise."""
    for quote in KNOWN_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}/{quote}"
    raise ValueError(
        f"cannot split symbol {symbol!r} into base/quote; known quote assets: "
        f"{', '.join(KNOWN_QUOTES)}"
    )


def class_name_for(candidate: str) -> str:
    """Strategy class name for a candidate: ``"ema-trend_v2"`` -> ``"TvPaperEmaTrendV2"``."""
    parts = re.split(r"[^A-Za-z0-9]+", candidate)
    return "TvPaper" + "".join(p[:1].upper() + p[1:] for p in parts if p)


# --------------------------------------------------------------------------- strategy


def render_strategy(plan: Mapping[str, Any] | Plan, *, class_name: str) -> str:
    """Python source of a freqtrade IStrategy (v3) running ``plan`` through ``lab.plan``.

    The plan is validated first (``PlanValidationError``, a ``ValueError``, lists every
    problem); ``class_name`` must be a valid, non-keyword Python identifier.
    """
    if not isinstance(class_name, str) or not class_name.isidentifier():
        raise ValueError(f"class_name {class_name!r} is not a valid Python identifier")
    if keyword.iskeyword(class_name):
        raise ValueError(f"class_name {class_name!r} is a Python keyword")
    p = _parse(plan)
    plan_json = json.dumps(plan_to_dict(p), sort_keys=True)
    tp = p.risk.take_profit_pct
    roi = {"0": round(tp / 100.0, 10)} if tp is not None else DISABLED_ROI
    sl = p.risk.stop_loss_pct
    stoploss = -round(sl / 100.0, 10) if sl is not None else NO_STOPLOSS
    startup = startup_candle_count(p)
    return f'''"""Generated by tradingView agents (research.paper.freqtrade). DO NOT EDIT.

PAPER TRADING ONLY: run with `freqtrade trade --dry-run`. Entry/exit signals come from the
tradingView lab (lab.plan.compile_plan) on closed candles: same rules and timing as the backtest
(signal at bar t's close, fill right after it, about bar t+1's open). Signals match the lab only
for features with a bounded look-back; history-dependent features (e.g. regime_signal's
exponentially weighted Markov counts) see only the candles freqtrade holds (about
startup_candle_count), so they can differ from the full-history backtest and the weekly
comparison may report "diverging" (never a false pass). Needs the `lab` package installed
(editable) in freqtrade's environment.

Plan hash: {plan_hash(p)}
Results are historical, not forward-looking.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
from lab.plan import compile_plan, parse_plan

PLAN_JSON = {plan_json!r}
PLAN = parse_plan(json.loads(PLAN_JSON))


def lab_candles(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """freqtrade frame (date/open/high/low/close/volume) -> lab candles + kept-row mask.

    Rows freqtrade filled in for missing candles (volume 0 and open == high == low == close)
    are dropped: the lab keeps missing bars absent, never forward-filled. Other zero-volume
    bars stay as untradable rows, as in the lab's archive loader.
    """
    o = dataframe["open"].to_numpy(dtype="float64")
    h = dataframe["high"].to_numpy(dtype="float64")
    lo = dataframe["low"].to_numpy(dtype="float64")
    c = dataframe["close"].to_numpy(dtype="float64")
    v = dataframe["volume"].to_numpy(dtype="float64")
    keep = ~((v <= 0) & (o == h) & (h == lo) & (lo == c))
    index = pd.DatetimeIndex(pd.to_datetime(dataframe["date"], utc=True))[keep]
    frame = pd.DataFrame(
        {{"open": o[keep], "high": h[keep], "low": lo[keep], "close": c[keep], "volume": v[keep]}},
        index=index.rename("time"),
    )
    frame["tradable"] = frame["volume"].to_numpy() > 0
    return frame, keep


class {class_name}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = {p.interval!r}
    can_short = False
    minimal_roi = {roi!r}
    stoploss = {stoploss!r}
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    process_only_new_candles = True
    startup_candle_count = {startup}
    order_types = {{
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }}

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n = len(dataframe)
        entries = np.zeros(n, dtype=bool)
        exits = np.zeros(n, dtype=bool)
        candles, keep = lab_candles(dataframe)
        if len(candles) > 0:
            signals = compile_plan(PLAN, candles)
            entries[keep] = signals.entries.to_numpy(dtype=bool)
            exits[keep] = signals.exits.to_numpy(dtype=bool)
        dataframe["lab_entry"] = entries
        dataframe["lab_exit"] = exits
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = dataframe["lab_entry"].astype("int64")
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = dataframe["lab_exit"].astype("int64")
        return dataframe
'''


# --------------------------------------------------------------------------- config


def _credential_paths(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if _norm_key(k) in CREDENTIAL_KEYS:
                found.append((here, v))
            found.extend(_credential_paths(v, here))
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            found.extend(_credential_paths(v, f"{path}[{i}]"))
    return found


def check_paper_config(config: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``config`` is a dry-run config without credentials:
    ``dry_run`` is ``True``, ``exchange.key``/``exchange.secret`` are empty strings, and no
    other credential-like key (password, token, uid, ...) appears anywhere."""
    if config.get("dry_run") is not True:
        raise ValueError(f"paper trading requires dry_run = true, got {config.get('dry_run')!r}")
    exchange = config.get("exchange")
    if not isinstance(exchange, Mapping):
        raise ValueError("config has no exchange object")
    for path, value in _credential_paths(config):
        if path in ("exchange.key", "exchange.secret") and value == "":
            continue
        raise ValueError(
            f"refusing credentials in a paper-trading config: {path!r} must not be set "
            "(exchange key/secret are always empty; never supply exchange keys)"
        )
    for key in ("key", "secret"):
        if exchange.get(key) != "":
            raise ValueError(f"exchange.{key} must be an empty string")


def render_config(
    *,
    plan: Mapping[str, Any] | Plan,
    exchange: str = "binance",
    stake_currency: str = "USDT",
    stake_amount: float = 100.0,
    dry_run_wallet: float = 1000.0,
    db_path: Path,
    public_api_host: str | None = None,
    dry_run: bool = True,
    exchange_options: Mapping[str, Any] | None = None,
    bot_name: str | None = None,
) -> dict[str, Any]:
    """A freqtrade config dict for a dry-run of ``plan`` on one pair.

    Always ``dry_run: true`` with ``exchange.key``/``secret`` empty strings. Raises
    ``ValueError`` if ``dry_run`` is False or any credential is supplied (also inside
    ``exchange_options``, which is merged into the ``exchange`` object). The api_server is
    left out (off); if the owner enables it, its credentials come from ``FREQTRADE__API_SERVER__*``
    environment variables at launch time, never from this file.

    ``public_api_host`` (e.g. ``"data-api.binance.vision"``) overrides binance's public REST URL,
    turns off freqtrade's websocket candles, and loads spot markets only. Workaround for
    ``api.binance.com`` returning HTTP 451 in the owner's region (the app itself reads
    data-api.binance.vision); verified 2026-10-25 with freqtrade 2026.9 / ccxt 4.5.90 (markets
    and 1h candles load, signals compute). Re-check the bot log after upgrading either.
    """
    if dry_run is not True:
        raise ValueError("render_config only builds dry-run configs (dry_run must be True)")
    p = _parse(plan)
    pair = symbol_to_pair(p.symbol)
    quote = pair.split("/", 1)[1]
    if stake_currency != quote:
        raise ValueError(
            f"stake_currency {stake_currency!r} must equal the pair's quote asset {quote!r}"
        )
    if not (stake_amount > 0 and dry_run_wallet >= stake_amount):
        raise ValueError(
            f"need 0 < stake_amount <= dry_run_wallet, got {stake_amount} / {dry_run_wallet}"
        )
    ex: dict[str, Any] = {
        "name": exchange,
        "key": "",
        "secret": "",
        "ccxt_config": {},
        "ccxt_async_config": {},
        "pair_whitelist": [pair],
        "pair_blacklist": [],
    }
    if public_api_host is not None:
        if exchange != "binance":
            raise ValueError(
                f"public_api_host only applies to exchange 'binance' (its /api/v3 layout), "
                f"got exchange {exchange!r}"
            )
        if not re.fullmatch(r"[A-Za-z0-9.-]+", public_api_host):
            raise ValueError(f"public_api_host {public_api_host!r} must be a bare host name")
        # Spot markets only: ccxt's binance also loads fapi/dapi futures markets, which the
        # public override does not cover and which return 451 where api.binance.com does.
        urls = {
            "urls": {"api": {"public": f"https://{public_api_host}/api/v3"}},
            "options": {"fetchMarkets": {"types": ["spot"]}},
        }
        ex["ccxt_config"] = json.loads(json.dumps(urls))
        ex["ccxt_async_config"] = json.loads(json.dumps(urls))
        ex["enable_ws"] = False
    if exchange_options:
        for path, _ in _credential_paths(exchange_options):
            raise ValueError(
                f"refusing credentials in exchange_options: {path!r} (never supply exchange keys)"
            )
        reserved = {"name", "key", "secret", "pair_whitelist"} & set(exchange_options)
        if public_api_host is not None:  # a shallow merge would silently drop the override
            reserved |= {"ccxt_config", "ccxt_async_config", "enable_ws"} & set(exchange_options)
        if reserved:
            raise ValueError(f"exchange_options may not set {sorted(reserved)}")
        ex.update(json.loads(json.dumps(dict(exchange_options))))
    config: dict[str, Any] = {
        "bot_name": bot_name or f"tv-paper-{p.name}"[:64],
        "dry_run": True,
        "dry_run_wallet": float(dry_run_wallet),
        "trading_mode": "spot",
        "max_open_trades": 1,
        "stake_currency": stake_currency,
        "stake_amount": float(stake_amount),
        "tradable_balance_ratio": 0.99,
        "timeframe": p.interval,
        "cancel_open_orders_on_exit": False,
        "unfilledtimeout": {"entry": 10, "exit": 10, "exit_timeout_count": 0, "unit": "minutes"},
        # Market orders need price_side "other" (cross the spread), like the lab's next-open fill.
        "entry_pricing": {
            "price_side": "other",
            "use_order_book": True,
            "order_book_top": 1,
            "price_last_balance": 0.0,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
        },
        "exit_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "exchange": ex,
        "pairlists": [{"method": "StaticPairList"}],
        "db_url": f"sqlite:///{Path(db_path).resolve()}",
        # No "api_server" or "telegram" block: both are off by default, and freqtrade >= 2026.9's
        # schema requires their listen address / token whenever the block is present.
        "initial_state": "running",
        "force_entry_enable": False,
        "internals": {"process_throttle_secs": 5},
    }
    check_paper_config(config)
    return config


# --------------------------------------------------------------------------- prepare / launch


@dataclass(frozen=True)
class PreparedPaper:
    dir: Path
    strategy_path: Path
    config_path: Path
    command: list[str]
    class_name: str = ""
    db_path: Path | None = None
    plan_hash: str = ""


@dataclass(frozen=True)
class LaunchResult:
    started: bool
    message: str
    process: Any | None = None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def check_candidate_name(candidate: str) -> str:
    if not isinstance(candidate, str) or not CANDIDATE_PATTERN.fullmatch(candidate):
        raise ValueError(
            f"candidate {candidate!r} must match {CANDIDATE_PATTERN.pattern} (letters, digits, "
            "'-' and '_'; it becomes a folder name)"
        )
    return candidate


def prepare_candidate(
    plan: Mapping[str, Any] | Plan,
    *,
    candidate: str,
    paper_dir: Path = PAPER_DIR,
    now: datetime | None = None,
    **config_kwargs: Any,
) -> PreparedPaper:
    """Write ``paper_dir/<candidate>/`` (``plan.json``, ``<Class>.py``, ``config.json``,
    ``meta.json``, ``user_data/``) and return the dry-run launch command.

    Re-preparing a candidate keeps its dry-run DB and first ``prepared_at``; a different plan
    under an existing candidate name raises ``ValueError`` (use a new name).
    """
    check_candidate_name(candidate)
    if "dry_run" in config_kwargs and config_kwargs["dry_run"] is not True:
        raise ValueError("prepare_candidate only prepares dry-run bots (dry_run must be True)")
    p = _parse(plan)
    phash = plan_hash(p)
    folder = Path(paper_dir) / candidate
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "user_data").mkdir(exist_ok=True)
    meta_path = folder / "meta.json"
    prepared_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    if meta_path.is_file():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if old.get("plan_hash") not in (None, phash):
            raise ValueError(
                f"candidate {candidate!r} already paper-trades plan {old['plan_hash'][:12]}; "
                "use a new candidate name for a different plan"
            )
        prepared_at = old.get("prepared_at", prepared_at)

    class_name = class_name_for(candidate)
    db_path = folder / "dryrun.sqlite"
    config = render_config(
        plan=p, db_path=db_path, bot_name=f"tv-paper-{candidate}", **config_kwargs
    )
    strategy_path = folder / f"{class_name}.py"
    config_path = folder / "config.json"
    _atomic_write(
        folder / "plan.json", json.dumps(plan_to_dict(p), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(strategy_path, render_strategy(p, class_name=class_name))
    _atomic_write(config_path, json.dumps(config, indent=2) + "\n")
    command = [
        "freqtrade",
        "trade",
        "--dry-run",
        "--config",
        str(config_path),
        "--strategy",
        class_name,
        "--strategy-path",
        str(folder),
        "--userdir",
        str(folder / "user_data"),
        "--db-url",
        config["db_url"],
    ]
    meta = {
        "candidate": candidate,
        "class_name": class_name,
        "plan_hash": phash,
        "symbol": p.symbol,
        "interval": p.interval,
        "prepared_at": prepared_at,
        "db_path": str(db_path),
        "command": command,
    }
    _atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")
    return PreparedPaper(
        dir=folder,
        strategy_path=strategy_path,
        config_path=config_path,
        command=command,
        class_name=class_name,
        db_path=db_path,
        plan_hash=phash,
    )


def _check_command(prepared: PreparedPaper) -> None:
    cmd = list(prepared.command)
    if cmd[:2] != ["freqtrade", "trade"]:
        raise ValueError(f"refusing to launch {cmd[:2]!r}: only `freqtrade trade` is allowed")
    if "--dry-run" not in cmd:
        raise ValueError("refusing to launch freqtrade without --dry-run (paper trading only)")
    configs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a in ("--config", "-c")]
    if configs != [str(prepared.config_path)]:
        raise ValueError(
            f"command must load exactly the prepared config {str(prepared.config_path)!r}, "
            f"got {configs!r}"
        )


def _child_env(env: Mapping[str, str] | None) -> dict[str, str]:
    base = dict(os.environ if env is None else env)
    # Exchange settings could otherwise be injected via FREQTRADE__EXCHANGE__KEY etc.
    clean = {k: v for k, v in base.items() if not k.upper().startswith("FREQTRADE__EXCHANGE__")}
    clean["FREQTRADE__DRY_RUN"] = "true"
    return clean


def launch(
    prepared: PreparedPaper,
    *,
    runner: Callable[..., Any] = subprocess.Popen,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> LaunchResult:
    """Start the prepared ``freqtrade trade --dry-run`` bot with ``runner``.

    Refuses (``ValueError``) unless the command is ``freqtrade trade ... --dry-run`` loading
    exactly the prepared config, and that config passes :func:`check_paper_config`. Returns
    ``started=False`` with an install hint when ``freqtrade`` is not on PATH (never raises for
    that). Exchange settings are stripped from the child's environment.
    """
    _check_command(prepared)
    config = json.loads(Path(prepared.config_path).read_text(encoding="utf-8"))
    check_paper_config(config)
    if which("freqtrade") is None:
        return LaunchResult(False, FREQTRADE_INSTALL_HINT)
    process = runner(list(prepared.command), cwd=str(prepared.dir), env=_child_env(env))
    return LaunchResult(True, "freqtrade dry-run started: " + " ".join(prepared.command), process)


__all__ = [
    "FREQTRADE_INSTALL_HINT",
    "LaunchResult",
    "PreparedPaper",
    "check_candidate_name",
    "check_paper_config",
    "class_name_for",
    "launch",
    "plan_lookback_bars",
    "prepare_candidate",
    "render_config",
    "render_strategy",
    "startup_candle_count",
    "symbol_to_pair",
]
