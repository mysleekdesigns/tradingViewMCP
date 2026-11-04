"""Render a validated plan as a freqtrade dry-run bot (paper trading only).

* :func:`render_strategy` writes a freqtrade ``IStrategy`` (interface v3) whose
  ``populate_indicators`` calls **our lab's** ``compile_plan`` on the closed candles, so the
  entry/exit rules are the same code as the backtest's. The signal is decided on bar t's close;
  freqtrade fills right after the close, i.e. at roughly bar t+1's open (the lab's fill rule).
* **Same history as the lab replay.** On its first fetch freqtrade's dry-run pulls the
  exchange's ``ohlcv_candle_limit`` candles (1000 on Binance) and keeps about
  ``candle_limit + startup_candle_count`` of them, so recursive indicators (EMA) would start
  ~1000 bars back while ``paper_trade.py compare`` replays the lab from the protocol's frozen
  warm-up (``start - warmup_bars`` bars). Honest bots then disagreed near crossings. The
  rendered strategy therefore carries a frozen ``WARM_FROM`` (UTC epoch seconds, the
  protocol's ``data.warm_from``): ``populate_indicators`` runs ``compile_plan`` only on rows
  with ``date >= WARM_FROM`` and maps the signals back (earlier rows get no signal), so the bot
  and the lab replay compute every feature from the same first bar. It also carries the
  protocol's frozen ``START`` (UTC epoch seconds): ``enter_long`` is set only on candles with
  ``date >= START``, the same first signal bar as the lab replay (``replay_trades`` keeps rows
  ``>= start``), so a bot launched before the test starts can neither enter at ``start`` on a
  pre-start signal nor hold a pre-start position into it. Exits are not gated (with entries
  gated there is no position before ``START``). ``compare`` refuses when the strategy file's
  ``WARM_FROM`` or ``START`` is not the protocol's. Remaining limits: candle data that
  differ between the exchange feed and the comparison's source, and a dataframe that no longer
  reaches back to ``WARM_FROM`` (freqtrade keeps a bounded number of candles: with ``warmup_bars
  + decision_bars`` above about 1000, the oldest rows drop off late in the test; the strategy
  logs a warning then, and any resulting mismatch shows up as a fail, never a false pass).
* :func:`render_config` builds a freqtrade config that is ALWAYS ``dry_run: true`` with empty
  exchange credentials. It raises ``ValueError`` if asked for anything else.
* :func:`prepare_candidate` writes ``PAPER_DIR/<candidate>/`` (plan, strategy, config, meta)
  and returns the ``freqtrade trade --dry-run`` command; :func:`launch` re-checks every safety
  property before starting it.
* **Risk controls and sizing (PLAN.md Phase 20), future tests only.** With a frozen ``sizing``
  or ``risk_controls`` block, :func:`render_strategy` adds ``FROZEN_RISK``, a ``protections``
  property, ``custom_stake_amount`` (``sized_stake``'s exact source, the same volatility column
  the lab replay reads) and a ``confirm_trade_entry`` that blocks entries while ``halt.lock``
  sits beside the strategy file; :func:`render_config` adds the exposure settings. Without a
  block every output is unchanged, byte for byte, because the block code never runs.

Freqtrade is GPL-3.0 and runs as a separate process in its own virtualenv (with ``lab``
installed editable there); nothing in this module imports it. The generated strategy file
imports freqtrade, but that file only runs inside freqtrade's own environment.
"""

from __future__ import annotations

import ast
import inspect
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
from lab.types import Plan, bars_per_year
from research.paper import risk as risk_rules
from research.paper.risk import (
    HALT_LOCK,
    UNLIMITED_STAKE,
    PaperConfig,
    RiskControls,
    Sizing,
    check_not_halted,
    check_sizing_against_plan,
    realized_vol_annual,
    risk_controls_to_dict,
    sized_stake,
    sizing_to_dict,
)

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
DRYRUN_DB = "dryrun.sqlite"
DB_URL_KEY = "db_url"  # the one config key that holds a path; bot_name holds only the candidate
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


def sizing_max_stake(sizing: Sizing) -> float:
    """The largest stake the block allows."""
    return sizing.wallet * sizing.max_stake_pct / 100.0


def frozen_risk(risk_controls: RiskControls | None, sizing: Sizing | None) -> dict[str, Any]:
    """The protocol's blocks as the plain dict rendered into a strategy as ``FROZEN_RISK``."""
    return {
        "risk_controls": None if risk_controls is None else risk_controls_to_dict(risk_controls),
        "sizing": None if sizing is None else sizing_to_dict(sizing),
    }


def _replace_once(source: str, anchor: str, text: str) -> str:
    if source.count(anchor) != 1:  # the template changed: never ship a half-patched strategy
        raise ValueError(f"strategy template has no single anchor {anchor!r}")
    return source.replace(anchor, text)


_HALT_ENTRY_CHECK = '''
    def confirm_trade_entry(
        self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, **kwargs
    ) -> bool:
        """False while halt.lock sits beside this file, so a bot restarted by hand cannot enter.
        A backtest replays the plan, not the halt, so it ignores the lock. Errors fail closed."""
        try:
            if not (Path(__file__).resolve().parent / HALT_LOCK).exists():
                return True
            return self.dp.runmode.value == "backtest"
        except Exception:
            logger.exception("halt.lock check failed: blocking this entry")
            return False
'''

_SIZED_STAKE_METHOD = '''
    def custom_stake_amount(
        self,
        pair,
        current_time,
        current_rate,
        proposed_stake,
        min_stake,
        max_stake,
        leverage,
        entry_tag,
        side,
        **kwargs,
    ) -> float:
        """The frozen sized stake: bar t's close decides it, like the entry signal. Volatility is
        the closed-candle column, last row of the analysed frame. No value means the minimum.
        max_stake is the available balance: a clip below the frozen stake shows up at compare."""
        realized = None
        if SIZING["vol_target_annual"] is not None:
            try:
                frame, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
                realized = float(frame["lab_vol"].iloc[-1])
            except Exception:
                logger.exception("no realised volatility for %s: minimum stake", pair)
        stake = sized_stake(
            stop_distance=STOP_DISTANCE,
            wallet=SIZING["wallet"],
            risk_per_trade_pct=SIZING["risk_per_trade_pct"],
            max_stake_pct=SIZING["max_stake_pct"],
            min_stake_pct=SIZING["min_stake_pct"],
            size_multiplier=SIZING["size_multiplier"],
            vol_target_annual=SIZING["vol_target_annual"],
            realized_vol_annual=realized,
        )
        return min(stake, max_stake)
'''


def _protections_property(risk_controls: RiskControls) -> str:
    g = risk_controls.protections
    if g is None:
        return ""
    entries = [
        {"method": "CooldownPeriod", "stop_duration_candles": g.cooldown_candles},
        {
            "method": "StoplossGuard",
            "lookback_period_candles": g.stoploss_guard_lookback_candles,
            "trade_limit": g.stoploss_guard_trade_limit,
            "stop_duration_candles": g.stoploss_guard_stop_candles,
        },
        {
            "method": "MaxDrawdown",
            "calculation_mode": "equity",
            "lookback_period_candles": g.max_drawdown_lookback_candles,
            "trade_limit": g.max_drawdown_trade_limit,
            "stop_duration_candles": g.max_drawdown_stop_candles,
            "max_allowed_drawdown": g.max_drawdown_allowed,
        },
    ]
    return (
        f"\n    @property\n    def protections(self) -> list[dict]:\n        return {entries!r}\n"
    )


def _with_risk(
    source: str, plan: Plan, risk_controls: RiskControls | None, sizing: Sizing | None
) -> str:
    """``source`` (the plain strategy) plus the frozen blocks. Text is only added, so what the
    plain strategy computes (signals, ``WARM_FROM``, ``START``) stays the same code."""
    imports = ["from pathlib import Path\n"]
    constants = [
        "# Frozen in the paper protocol (risk_controls and sizing, under its sha256); a different\n"
        "# size is a new test, and compare refuses a strategy file with other values.\n"
        f"FROZEN_RISK = {frozen_risk(risk_controls, sizing)!r}\n"
        f"HALT_LOCK = {HALT_LOCK!r}\n"
    ]
    methods = ""
    if risk_controls is not None:
        methods += _protections_property(risk_controls)
        if risk_controls.circuit_breaker is not None:
            methods += _HALT_ENTRY_CHECK
    if sizing is not None:
        check_sizing_against_plan(sizing, plan.risk.stop_loss_pct)
        stop_pct = plan.risk.stop_loss_pct
        assert stop_pct is not None  # checked above
        imports.insert(0, "import math\n")
        constants.append(
            'SIZING = FROZEN_RISK["sizing"]\n'
            f"STOP_DISTANCE = {stop_pct / 100.0!r}\n"
            f"\n\n{inspect.getsource(sized_stake)}"
        )
        methods += _SIZED_STAKE_METHOD
        if sizing.vol_target_annual is not None:
            constants.append(
                f"\n\nBARS_PER_YEAR = {bars_per_year(plan.interval)!r}\n"
                f"VOL_WINDOW_BARS = {sizing.vol_window_bars!r}\n"
                f"_SECONDS_PER_YEAR = {risk_rules._SECONDS_PER_YEAR!r}\n"
                f"_BAR_STEP_TOLERANCE = {risk_rules._BAR_STEP_TOLERANCE!r}\n"
                f"\n\n{inspect.getsource(realized_vol_annual)}"
            )
            exits = "        exits = np.zeros(n, dtype=bool)\n"
            source = _replace_once(source, exits, exits + "        vol = np.full(n, np.nan)\n")
            filled = "            exits[rows] = signals.exits.to_numpy(dtype=bool)\n"
            column = (
                "            vol[rows] = realized_vol_annual(\n"
                '                candles["close"], VOL_WINDOW_BARS, BARS_PER_YEAR\n'
                '            ).to_numpy(dtype="float64")\n'
            )
            source = _replace_once(source, filled, filled + column)
            saved = '        dataframe["lab_entry"] = entries\n'
            source = _replace_once(source, saved, '        dataframe["lab_vol"] = vol\n' + saved)
    stdlib = "import json\nimport logging\n"
    logger_line = "logger = logging.getLogger(__name__)\n"
    source = _replace_once(source, stdlib, stdlib + "".join(imports))
    source = _replace_once(source, logger_line, logger_line + "".join(constants))
    return source + methods


def render_strategy(
    plan: Mapping[str, Any] | Plan,
    *,
    class_name: str,
    warm_from: int | None = None,
    start: int | None = None,
    risk_controls: RiskControls | None = None,
    sizing: Sizing | None = None,
) -> str:
    """Python source of a freqtrade IStrategy (v3) running ``plan`` through ``lab.plan``.

    The plan is validated first (``PlanValidationError``, a ``ValueError``, lists every
    problem); ``class_name`` must be a valid, non-keyword Python identifier. ``warm_from``
    (UTC epoch seconds; the paper protocol's frozen ``data.warm_from``) is rendered as
    ``WARM_FROM``: indicators are computed only from that bar on, exactly like the lab replay
    in ``compare`` (None: on every candle freqtrade holds, for previews without a protocol).
    ``start`` (the protocol's start, UTC epoch seconds) is rendered as ``START``: entry signals
    count only on candles with ``date >= START``, the lab replay's first signal bar (None: no
    gate, for previews). ``risk_controls`` and ``sizing`` are the protocol's frozen blocks (see
    the module docstring); without them the source is the plain strategy.
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
    for name, value in (("warm_from", warm_from), ("start", start)):
        ok = value is None or (isinstance(value, int) and not isinstance(value, bool))
        if not ok or (value is not None and value < 0):
            raise ValueError(f"{name} must be a non-negative int (epoch seconds), got {value!r}")
    if warm_from is not None and start is not None and warm_from > start:
        raise ValueError(f"warm_from {warm_from} must not be after start {start}")
    source = f'''"""Generated by tradingView agents (research.paper.freqtrade). DO NOT EDIT.

PAPER TRADING ONLY: run with `freqtrade trade --dry-run`. Entry/exit signals come from the
tradingView lab (lab.plan.compile_plan) on closed candles: same rules and timing as the backtest
(signal at bar t's close, fill right after it, about bar t+1's open). Indicators are computed
only on candles from WARM_FROM on (the paper protocol's frozen warm-up start), the same history
the lab replays in `paper_trade.py compare`, so recursive features start from the same bar.
Needs the `lab` package installed (editable) in freqtrade's environment.

Plan hash: {plan_hash(p)}
Results are historical, not forward-looking.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
from lab.plan import compile_plan, parse_plan

PLAN_JSON = {plan_json!r}
PLAN = parse_plan(json.loads(PLAN_JSON))
# Frozen in the paper protocol (data.warm_from, UTC epoch seconds): indicators start here, like
# the lab replay in `paper_trade.py compare`. None: every candle freqtrade holds.
WARM_FROM = {warm_from!r}
# Frozen in the paper protocol (start, UTC epoch seconds): entries only on candles dated at or
# after it, the lab replay's first signal bar. None: no gate.
START = {start!r}
logger = logging.getLogger(__name__)


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
        rows = np.flatnonzero(keep)  # dataframe positions of the lab candles
        if WARM_FROM is not None and len(candles) > 0:
            first = pd.Timestamp(WARM_FROM, unit="s", tz="UTC")
            if candles.index[0] > first:
                logger.warning(
                    "candles start at %s, after WARM_FROM %s: indicators cannot match the lab "
                    "replay exactly",
                    candles.index[0],
                    first,
                )
            warm = np.asarray(candles.index >= first)
            candles, rows = candles.loc[warm], rows[warm]
        if len(candles) > 0:
            signals = compile_plan(PLAN, candles)
            entries[rows] = signals.entries.to_numpy(dtype=bool)
            exits[rows] = signals.exits.to_numpy(dtype=bool)
        dataframe["lab_entry"] = entries
        dataframe["lab_exit"] = exits
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        enter = dataframe["lab_entry"].to_numpy(dtype=bool)
        if START is not None:
            dates = pd.DatetimeIndex(pd.to_datetime(dataframe["date"], utc=True))
            enter = enter & np.asarray(dates >= pd.Timestamp(START, unit="s", tz="UTC"))
        dataframe["enter_long"] = enter.astype("int64")
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = dataframe["lab_exit"].astype("int64")
        return dataframe
'''
    if risk_controls is None and sizing is None:
        return source
    return _with_risk(source, p, risk_controls, sizing)


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
    ``dry_run`` is ``True``, ``exchange.key``/``exchange.secret`` are empty strings, no other
    credential-like key (password, token, uid, ...) appears anywhere, and
    ``position_adjustment_enable`` is off (no averaging down)."""
    if config.get("dry_run") is not True:
        raise ValueError(f"paper trading requires dry_run = true, got {config.get('dry_run')!r}")
    if config.get("position_adjustment_enable"):
        raise ValueError("no averaging down rule: config sets position_adjustment_enable")
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
    sizing: Sizing | None = None,
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

    ``sizing`` (the protocol's frozen block) sets the wallet to ``sizing.wallet``,
    ``max_open_trades``, ``tradable_balance_ratio`` and ``stake_amount`` to ``"unlimited"``:
    ``custom_stake_amount`` decides every stake, and a numeric value would drop an entry whenever
    the balance fell below it. ``stake_amount`` and ``dry_run_wallet`` are then ignored, so callers
    refuse a contradicting value first.
    """
    if sizing is not None:
        stake_amount, dry_run_wallet = sizing_max_stake(sizing), sizing.wallet
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
    if sizing is not None:
        config["stake_amount"] = UNLIMITED_STAKE
        config["max_open_trades"] = sizing.max_open_trades
        config["tradable_balance_ratio"] = sizing.max_total_exposure_pct / 100.0
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
    warm_from: int | None = None,
    start: int | None = None,
    risk_controls: RiskControls | None = None,
    sizing: Sizing | None = None,
    **config_kwargs: Any,
) -> PreparedPaper:
    """Write ``paper_dir/<candidate>/`` (``plan.json``, ``<Class>.py``, ``config.json``,
    ``meta.json``, ``user_data/``) and return the dry-run launch command. ``warm_from`` is the
    registered protocol's ``data.warm_from`` and ``start`` is its start, both rendered into the
    strategy (see :func:`render_strategy`); ``paper_trade.py prepare`` always passes them, and
    the protocol's ``risk_controls`` and ``sizing`` when it has them.

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
    db_path = folder / DRYRUN_DB
    config = render_config(
        plan=p, db_path=db_path, bot_name=f"tv-paper-{candidate}", sizing=sizing, **config_kwargs
    )
    strategy_path = folder / f"{class_name}.py"
    config_path = folder / "config.json"
    _atomic_write(
        folder / "plan.json", json.dumps(plan_to_dict(p), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(
        strategy_path,
        render_strategy(
            p,
            class_name=class_name,
            warm_from=warm_from,
            start=start,
            risk_controls=risk_controls,
            sizing=sizing,
        ),
    )
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
        "warm_from": warm_from,
        "start": start,
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


def frozen_settings(paper: PaperConfig) -> dict[str, Any]:
    """:func:`render_config` keywords for a frozen config. A sized test's stake and wallet come
    from its block, so ``unlimited`` is not passed on as a number."""
    settings: dict[str, Any] = {
        "exchange": paper.exchange,
        "public_api_host": paper.public_api_host,
        "dry_run_wallet": paper.dry_run_wallet,
    }
    if paper.stake_amount != UNLIMITED_STAKE:
        settings["stake_amount"] = paper.stake_amount
    return settings


def fresh_config(
    plan: Mapping[str, Any] | Plan,
    *,
    candidate: str,
    folder: Path,
    paper: PaperConfig,
    sizing: Sizing | None,
) -> dict[str, Any]:
    """What ``config.json`` of ``folder`` must be: a render from the frozen values, the same call
    :func:`prepare_candidate` makes, through JSON so it compares with a parsed file."""
    config = render_config(
        plan=plan,
        db_path=Path(folder) / DRYRUN_DB,
        bot_name=f"tv-paper-{candidate}",
        sizing=sizing,
        **frozen_settings(paper),
    )
    return json.loads(json.dumps(config))


def config_differences(found: Any, fresh: Any, path: str = "") -> list[str]:
    """Dotted keys on which two parsed configs differ, whole file: a key added, dropped or
    changed at any depth counts."""
    if not (isinstance(found, Mapping) and isinstance(fresh, Mapping)):
        return [] if found == fresh else [path or "<root>"]
    out: list[str] = []
    for key in sorted(set(found) | set(fresh), key=str):
        where = f"{path}.{key}" if path else str(key)
        if key not in found or key not in fresh:
            out.append(where)
        else:
            out.extend(config_differences(found[key], fresh[key], where))
    return out


def strategy_frozen_value(source: str, name: str) -> int | None:
    """The frozen ``<name> = <int>`` (``WARM_FROM`` or ``START``) rendered into a strategy
    file's source; ``ValueError`` when the line is missing or appears more than once (a
    hand-edited or older file)."""
    found = re.findall(rf"^{re.escape(name)} = (None|\d+)$", source, flags=re.M)
    if len(found) != 1:
        raise ValueError(f"the strategy file has no single `{name} = ...` line")
    return None if found[0] == "None" else int(found[0])


def strategy_frozen_risk(source: str) -> dict[str, Any]:
    """The ``FROZEN_RISK`` dict rendered into a strategy file's source; ``ValueError`` when the
    line is missing, repeated or not a literal (a hand-edited or older file)."""
    found = re.findall(r"^FROZEN_RISK = (.+)$", source, flags=re.M)
    if len(found) != 1:
        raise ValueError("the strategy file has no single `FROZEN_RISK = ...` line")
    try:
        value = ast.literal_eval(found[0])
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"the strategy file's FROZEN_RISK is not a literal: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("the strategy file's FROZEN_RISK is not a dict")
    return value


def strategy_warm_from(source: str) -> int | None:
    """:func:`strategy_frozen_value` for ``WARM_FROM``."""
    return strategy_frozen_value(source, "WARM_FROM")


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
    pid_file: Path | None = None,
) -> LaunchResult:
    """Start the prepared ``freqtrade trade --dry-run`` bot with ``runner``.

    Refuses (``ValueError``) unless the command is ``freqtrade trade ... --dry-run`` loading
    exactly the prepared config, that config passes :func:`check_paper_config`, and the folder
    holds no ``halt.lock``. Returns ``started=False`` with an install hint when ``freqtrade`` is
    not on PATH (never raises for that). Exchange settings are stripped from the child's
    environment. ``pid_file``, given only for a protocol with a circuit breaker, records the
    process id for ``paper_trade.py guard``.
    """
    _check_command(prepared)
    check_not_halted(prepared.dir)
    config = json.loads(Path(prepared.config_path).read_text(encoding="utf-8"))
    check_paper_config(config)
    if which("freqtrade") is None:
        return LaunchResult(False, FREQTRADE_INSTALL_HINT)
    process = runner(list(prepared.command), cwd=str(prepared.dir), env=_child_env(env))
    if pid_file is not None:
        _atomic_write(Path(pid_file), f"{process.pid}\n")
    return LaunchResult(True, "freqtrade dry-run started: " + " ".join(prepared.command), process)


__all__ = [
    "DB_URL_KEY",
    "DRYRUN_DB",
    "FREQTRADE_INSTALL_HINT",
    "UNLIMITED_STAKE",
    "LaunchResult",
    "PreparedPaper",
    "check_candidate_name",
    "check_paper_config",
    "class_name_for",
    "config_differences",
    "fresh_config",
    "frozen_risk",
    "frozen_settings",
    "launch",
    "plan_lookback_bars",
    "prepare_candidate",
    "render_config",
    "render_strategy",
    "sizing_max_stake",
    "startup_candle_count",
    "strategy_frozen_risk",
    "strategy_frozen_value",
    "strategy_warm_from",
    "symbol_to_pair",
]
