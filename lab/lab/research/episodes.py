"""The per-episode table and the turnover block of ``get_report`` (PLAN.md Phase 20, Parts C, G).

``[episodes]`` in lab.toml names crash episodes, ``[start, end)`` UTC, fixed before use
(:class:`lab.types.Episode`). This module is pure: arrays and evidence dicts in, JSON-safe dicts
out, no ledger, no loader.

* :func:`usable_episodes` drops every episode that ends after ``holdout_start``, so a holdout
  bar is never read. ``end == holdout_start`` stays: ``[start, end)`` reads no holdout bar. It
  clips nothing else.
* :func:`episode_table` is one row per episode: strategy vs buy-and-hold return and max drawdown,
  exposure and trades, on the bars of the base trial. An episode with no scored bars gets a row
  with ``bars: 0``, nulls and a note, never a silent drop. Only holdout-dropped episodes leave
  the rows, and :func:`episodes_evidence` lists those by name under ``dropped_for_holdout``.
* :func:`hmm_label_diagnostics` summarises, for a plan with an HMM feature, how often the bear
  state of the fit that scored each bar was the higher-volatility state, and how often that
  answer changed. States are ordered by mean return, so the bear state need not be the volatile
  one: if it is often the calm one, a null overlay result may be about the labels.
* :func:`report_blocks` turns recorded backtest evidence into ``get_report``'s ``episodes`` and
  ``turnover`` blocks. It only reads the evidence: nothing is re-run at report time.

Max drawdown is a positive depth measured inside the episode: equity is 1.0 before the episode's
first bar, so bars before the episode cannot deepen it (:func:`lab.stats.drawdown.drawdown_series`,
the overlay block's convention; ``metrics.max_drawdown`` is the same quantity with a negative
sign). Exposure is the share of the episode's bars holding the coin at the close. ``trades``
counts entries filled inside the episode, so a trade carried in from before it is not counted.

The table is information only: not a criterion, not a trial. It adds no ledger row, no criterion
row and no pass/fail, and building it re-runs nothing that is ledgered.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from lab.plan import OVERLAY_KIND, PlanValidationError, checked_expressions, parse_plan
from lab.report import num, seconds
from lab.stats.drawdown import drawdown_series
from lab.types import Episode, bars_per_year

__all__ = [
    "EPISODES_INFORMATION_NOTE",
    "EPISODES_PORTFOLIO_NOTE",
    "EPISODES_PREDATE_NOTE",
    "EPISODES_TABLE_VERSION",
    "HMM_DIAGNOSTICS_NOTE",
    "NO_BARS_NOTE",
    "episode_table",
    "episodes_evidence",
    "hmm_label_diagnostics",
    "invested_flags",
    "nothing_to_read",
    "persist_settings",
    "portfolio_blocks",
    "report_blocks",
    "turnover_block",
    "usable_episodes",
]

EPISODES_TABLE_VERSION = 1
EPISODES_INFORMATION_NOTE = "information only: not a criterion, not a trial"
NO_BARS_NOTE = "no scored bars: this episode is outside the scored window"
EPISODES_PREDATE_NOTE = (
    "this run's backtest evidence predates the per-episode table; a new run_backtest records it"
)
EPISODES_PORTFOLIO_NOTE = "portfolio plans have no per-episode table or turnover block"
HMM_DIAGNOSTICS_NOTE = (
    "information only: not a criterion, not a trial. bars have a decision; "
    "share_bear_is_high_vol is the share of them whose bear state (lowest mean return in that "
    "fit) was the higher-volatility state, and flips counts changes of that answer between "
    "decided bars. A low share or many flips means the labels are unstable, so a null result "
    "may be about the labels, not about regimes"
)
_TABLE_BASIS = (
    "returns are net of costs on the base trial's bars; max drawdowns are positive depths from "
    "equity 1.0 before the episode's first bar; exposure is the share of the episode's bars "
    "invested at the close; trades counts entries filled inside the episode"
)
_TURNOVER_NOTE = (
    "per 365-day year of the base trial; traded_value_per_year is the sum of |change in "
    "position| as a multiple of equity, and each unit traded pays the per-side costs"
)


# --------------------------------------------------------------------------- the table


def usable_episodes(
    episodes: Iterable[Episode], holdout_start: pd.Timestamp
) -> tuple[Episode, ...]:
    """``episodes`` without any that end after ``holdout_start`` (module docstring)."""
    return tuple(ep for ep in episodes if ep.end <= holdout_start)


def invested_flags(
    index: pd.DatetimeIndex, spans: Iterable[tuple[pd.Timestamp, pd.Timestamp | None]]
) -> np.ndarray:
    """Per-bar flags: True where a trade holds the coin at the bar's close.

    ``spans`` are ``(entry_time, exit_time)``; ``exit_time`` None is a trade still open at the
    window end. A fill or a stop on bar x leaves the bar flat at its close, so a trade covers
    ``[entry, exit)``, which is the engine's ``position != 0``."""
    flags = np.zeros(len(index), dtype=bool)
    for entry, exit_ in spans:
        lo = int(index.searchsorted(entry))
        hi = len(index) if exit_ is None else int(index.searchsorted(exit_))
        flags[lo:hi] = True
    return flags


def _total_return(returns: np.ndarray) -> float | None:
    return num(float(np.prod(1.0 + returns)) - 1.0)


def _max_dd(returns: np.ndarray) -> float | None:
    if not np.all(np.isfinite(returns)):
        return None
    return num(float(drawdown_series(returns).max(initial=0.0)))


def _no_bars_row(ep: Episode) -> dict[str, Any]:
    return {
        "name": ep.name,
        "start": seconds(ep.start),
        "end": seconds(ep.end),
        "bars": 0,
        "strategy_return": None,
        "buy_hold_return": None,
        "strategy_max_dd": None,
        "buy_hold_max_dd": None,
        "exposure": None,
        "trades": None,
        "note": NO_BARS_NOTE,
    }


def episode_table(
    episodes: Sequence[Episode],
    index: pd.DatetimeIndex,
    strategy_returns: np.ndarray,
    buy_hold_returns: np.ndarray,
    invested: np.ndarray,
    entry_times: Iterable[pd.Timestamp],
) -> list[dict[str, Any]]:
    """One row per episode, in the given order (module docstring). ``strategy_returns``,
    ``buy_hold_returns`` and ``invested`` are per-bar arrays on ``index``, a sorted tz-aware
    index; ``entry_times`` are the trades' entry fills. Pass :func:`usable_episodes`' result:
    this function reads whatever bars ``index`` holds."""
    if index.tz is None or not index.is_monotonic_increasing:
        raise ValueError("episode_table needs a sorted tz-aware UTC index")
    n = len(index)
    strategy = np.asarray(strategy_returns, dtype=float)
    buy_hold = np.asarray(buy_hold_returns, dtype=float)
    held = np.asarray(invested, dtype=float) > 0.0
    if not strategy.size == buy_hold.size == held.size == n:
        raise ValueError(
            f"episode_table needs series aligned with the {n}-bar index, got "
            f"{strategy.size} strategy, {buy_hold.size} buy-and-hold, {held.size} invested"
        )
    entries = pd.DatetimeIndex(list(entry_times), tz="UTC")
    rows: list[dict[str, Any]] = []
    for ep in episodes:
        lo = int(index.searchsorted(ep.start))
        hi = int(index.searchsorted(ep.end))
        if hi <= lo:
            rows.append(_no_bars_row(ep))
            continue
        rows.append(
            {
                "name": ep.name,
                "start": seconds(ep.start),
                "end": seconds(ep.end),
                "bars": hi - lo,
                "strategy_return": _total_return(strategy[lo:hi]),
                "buy_hold_return": _total_return(buy_hold[lo:hi]),
                "strategy_max_dd": _max_dd(strategy[lo:hi]),
                "buy_hold_max_dd": _max_dd(buy_hold[lo:hi]),
                "exposure": num(float(np.mean(held[lo:hi]))),
                "trades": int(np.count_nonzero((entries >= ep.start) & (entries < ep.end))),
            }
        )
    return rows


def episodes_evidence(
    episodes: Sequence[Episode],
    holdout_start: pd.Timestamp,
    index: pd.DatetimeIndex,
    strategy_returns: np.ndarray,
    buy_hold_returns: np.ndarray,
    invested: np.ndarray,
    entry_times: Iterable[pd.Timestamp],
) -> dict[str, Any]:
    """The backtest evidence's ``"episodes_table"``. ``ValueError`` if ``index`` reaches
    ``holdout_start``: the caller loaded a bar it must never read."""
    if len(index) and index[-1] >= holdout_start:
        raise ValueError(f"the episode table's bars reach the locked holdout ({holdout_start})")
    usable = usable_episodes(episodes, holdout_start)
    kept = {ep.name for ep in usable}
    return {
        "version": EPISODES_TABLE_VERSION,
        "rows": episode_table(
            usable, index, strategy_returns, buy_hold_returns, invested, entry_times
        ),
        "dropped_for_holdout": [ep.name for ep in episodes if ep.name not in kept],
        "basis": _TABLE_BASIS,
    }


def hmm_label_diagnostics(bear_is_high_vol: np.ndarray) -> dict[str, Any]:
    """The evidence's ``"hmm_diagnostics"`` from a per-bar flag over the base trial's bars: 1.0
    when the bear state of the fit that scored the bar is the higher-volatility state, 0.0 when
    not, NaN when there is no decision. ``bars`` counts decided bars, and
    ``n_flat_warmup_bars`` (present only when > 0) the bars before the first decision; a later
    NaN is skipped, so a gap is not read as a flip."""
    flags = np.asarray(bear_is_high_vol, dtype=float)
    if flags.ndim != 1:
        raise ValueError(f"bear_is_high_vol must be 1-D, got shape {flags.shape}")
    decided = np.isfinite(flags)
    high = flags[decided] > 0.5
    out: dict[str, Any] = {
        "bars": int(high.size),
        "share_bear_is_high_vol": num(float(np.mean(high))) if high.size else None,
        "flips": int(np.count_nonzero(high[1:] != high[:-1])),
    }
    warmup = int(np.argmax(decided)) if high.size else int(flags.size)
    if warmup:
        out["n_flat_warmup_bars"] = warmup
    out["note"] = HMM_DIAGNOSTICS_NOTE
    return out


# --------------------------------------------------------------------------- report blocks


def _nested(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def persist_settings(raw_plan: object) -> list[int]:
    """The ``k`` of every ``persist`` call in a plan's rules: entry then exit, or only
    ``flat_when`` for an overlay (its entry is the negated rule, so it would count each call
    twice). Empty for a plan that no longer validates: it cannot run ``persist`` either."""
    if not isinstance(raw_plan, Mapping | str):
        return []
    try:
        plan = parse_plan(raw_plan)
        entry, exit_ = checked_expressions(plan)
    except PlanValidationError:
        return []
    exprs = (exit_,) if plan.kind == OVERLAY_KIND else (entry, exit_)
    return [int(k) for expr in exprs for k in expr.persist_ks]


def _trades_per_year(metrics: object, interval: object) -> float | None:
    n_trades, bars = _nested(metrics, "n_trades"), _nested(metrics, "bars")
    if not (isinstance(n_trades, int) and isinstance(bars, int) and bars > 0):
        return None
    if not isinstance(interval, str):
        return None
    try:
        return num(n_trades * bars_per_year(interval) / bars)  # type: ignore[arg-type]
    except (KeyError, ValueError):
        return None


def turnover_block(bt: Mapping[str, Any]) -> dict[str, Any]:
    """``get_report``'s ``turnover`` block, from the base trial's net metrics in ``bt``."""
    net = _nested(bt, "metrics", "net")
    return {
        "trades_per_year": _trades_per_year(net, bt.get("interval")),
        "traded_value_per_year": num(_nested(net, "turnover")),
        "persist_settings": persist_settings(bt.get("plan")),
        "note": _TURNOVER_NOTE,
    }


def report_blocks(bt: Mapping[str, Any] | None) -> dict[str, Any]:
    """``get_report``'s information-only keys for an edge or overlay plan's latest backtest
    evidence: ``episodes``, ``turnover``, ``episodes_note`` beside a null ``episodes`` when the
    evidence predates the table, and ``hmm_diagnostics`` when the evidence has it (plans with
    an HMM feature). Empty without evidence. Never a row, never pass/fail."""
    if bt is None:
        return {}
    table = bt.get("episodes_table")
    episodes: dict[str, Any]
    if isinstance(table, Mapping):
        episodes = {"episodes": {**table, "note": EPISODES_INFORMATION_NOTE}}
    else:
        episodes = {"episodes": None, "episodes_note": EPISODES_PREDATE_NOTE}
    blocks = {**episodes, "turnover": turnover_block(bt)}
    if "hmm_diagnostics" in bt:
        blocks["hmm_diagnostics"] = bt["hmm_diagnostics"]
    return blocks


def portfolio_blocks() -> dict[str, Any]:
    """The one line a portfolio plan's report carries instead of :func:`report_blocks`."""
    return {"episodes": None, "episodes_note": EPISODES_PORTFOLIO_NOTE}


def nothing_to_read(blocks: Mapping[str, Any]) -> bool:
    """True when :func:`report_blocks`' result has no episode row, no table error, no
    ``persist`` setting and no ``hmm_diagnostics``. An edge report leaves such blocks out, so
    its shape stays pinned by ``tests/test_report_golden.py``; a null ``episodes`` (older
    evidence) is still shown."""
    if not blocks:
        return True
    if "hmm_diagnostics" in blocks:
        return False
    episodes = blocks.get("episodes")
    if not isinstance(episodes, Mapping) or episodes.get("rows") or "error" in episodes:
        return False
    return not _nested(blocks, "turnover", "persist_settings")
