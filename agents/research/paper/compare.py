"""Weekly forward-vs-backtest comparison for a paper-traded candidate.

* :func:`read_dryrun_trades` reads freqtrade's dry-run SQLite (opened read-only).
* :func:`replay_trades` / :func:`backtest_same_period` replay the same plan with the lab engine
  over the paper period, flat at ``start`` like the bot (signals computed on the history before
  ``end``, trades inside ``[start, end)``). They record nothing in the ledger: this is a
  consistency check of a frozen candidate, never a research input, and must not be used to
  re-tune anything.
* :func:`compare` builds the :class:`lab.research.contract.PaperComparison`; arithmetic stays
  in Python. :func:`write_comparison` stores it as JSON plus a short Markdown summary.
* :func:`observe_paper` turns the dry-run trades into what the frozen
  :class:`research.paper.protocol.PaperProtocol` judges: data coverage, entries, bars in
  market, the timing match of each paper fill to a lab replay fill bar
  (:func:`match_fill_bars`), per-fill implementation shortfall against the MATCHED lab fill
  bar's modelled fill, and per-bar paper returns.
* :func:`public_kline_loader` fetches closed candles from Binance's public market-data host
  (no API key) for weeks the monthly archive does not cover yet.

Results are historical, not forward-looking.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.engine import run_signals
from lab.engine.costs import slippage_series
from lab.plan import compile_plan, parse_plan
from lab.research.contract import PaperComparison, TradeStats
from lab.runner import CandleLoader
from lab.types import CostModel, Plan, Trade, interval_seconds
from research.paper.freqtrade import check_candidate_name
from research.paper.protocol import Fill, ObservedPaper, PaperProtocol, Verdict

# Verdict thresholds (one-line justification each; fixed here, not tuned per candidate).
MIN_FORWARD_TRADES = 5  # fewer closed forward trades than this make any comparison noise
MIN_FORWARD_DAYS = 28  # PLAN.md: at least 4 weeks of forward paper trading before judging
MIN_ENTRY_MATCH_RATE = 0.7  # identical logic should fire the same entries; <70% means drift
RETURN_DIFF_ABS = 0.05  # total returns may differ by up to 5 percentage points (fees, fills)
RETURN_DIFF_REL = 0.5  # ...or by half the backtest's own total return, whichever is larger
ENTRY_MATCH_BARS = 1  # a forward entry within 1 bar of the lab's fill bar counts as a match

DISCLAIMER = (
    "Results are historical, not forward-looking. A consistent comparison means the paper bot "
    "behaves like its backtest; it says nothing about future profitability."
)
REQUIRED_COLUMNS: tuple[str, ...] = ("pair", "is_open", "open_date", "open_rate")
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "close_date",
    "close_rate",
    "amount",
    "fee_open",
    "fee_close",
    "close_profit",
    "exit_reason",
)
KLINE_PAGE_LIMIT = 1000  # Binance /api/v3/klines maximum rows per request
# Rate limits (HTTP 429): Binance asks clients to back off for Retry-After seconds and bans
# IPs (HTTP 418) that keep requesting. A few polite retries, then give up with a clear error.
KLINE_MAX_RETRIES = 3  # retries per request after a 429 (never after a 418)
KLINE_MAX_WAIT_S = 60.0  # cap on the total Retry-After wait of one load; past it, give up
KLINE_DEFAULT_BACKOFF_S = 2.0  # wait when Retry-After is missing or unreadable (doubles)
# freqtrade exit reasons whose fill the lab models at the next bar's open (v3 name, legacy name).
# Stop-loss / ROI exits fill at a level in the lab, so their shortfall is not judged.
SIGNAL_EXIT_REASONS: frozenset[str] = frozenset({"exit_signal", "sell_signal"})


@dataclass(frozen=True)
class ForwardTrade:
    """One freqtrade dry-run trade. Times are tz-aware UTC; optional fields may be None."""

    pair: str
    is_open: bool
    open_time: pd.Timestamp
    open_rate: float
    close_time: pd.Timestamp | None = None
    close_rate: float | None = None
    amount: float | None = None
    fee_open: float | None = None
    fee_close: float | None = None
    close_profit: float | None = None
    exit_reason: str | None = None

    @property
    def closed(self) -> bool:
        return not self.is_open and self.close_time is not None

    def net_return(self) -> float | None:
        """Net return of a closed trade: ``close_profit`` if stored, else from rates and fees."""
        if not self.closed:
            return None
        if self.close_profit is not None:
            return float(self.close_profit)
        if self.close_rate is None or self.open_rate <= 0:
            return None
        fo, fc = self.fee_open or 0.0, self.fee_close or 0.0
        return (self.close_rate * (1.0 - fc)) / (self.open_rate * (1.0 + fo)) - 1.0


# --------------------------------------------------------------------------- reading


def _ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _float(value: Any) -> float | None:
    if value is None:
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def read_dryrun_trades(db_path: Path, *, pair: str | None = None) -> list[ForwardTrade]:
    """Every trade in freqtrade's ``trades`` table (optionally one ``pair``), oldest first.

    The database is opened read-only (``mode=ro``). Missing optional columns read as None;
    a missing file, table, or required column raises ``ValueError`` naming it.
    """
    path = Path(db_path)
    if not path.is_file():
        raise ValueError(f"dry-run database {path} not found (has the paper bot run yet?)")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        if not cols:
            raise ValueError(f"{path} has no `trades` table (not a freqtrade database?)")
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise ValueError(f"{path}: trades table is missing columns {missing}")
        select = [*REQUIRED_COLUMNS, *(c for c in OPTIONAL_COLUMNS if c in cols)]
        sql = f"SELECT {', '.join(select)} FROM trades"
        args: tuple[Any, ...] = ()
        if pair is not None:
            sql += " WHERE pair = ?"
            args = (pair,)
        rows = conn.execute(sql + " ORDER BY open_date", args).fetchall()
    finally:
        conn.close()
    out: list[ForwardTrade] = []
    for row in rows:
        d = dict(zip(select, tuple(row), strict=True))
        open_time = _ts(d["open_date"])
        if open_time is None:
            continue
        reason = d.get("exit_reason")
        out.append(
            ForwardTrade(
                pair=str(d["pair"]),
                is_open=bool(d["is_open"]),
                open_time=open_time,
                open_rate=float(d["open_rate"]),
                close_time=_ts(d.get("close_date")),
                close_rate=_float(d.get("close_rate")),
                amount=_float(d.get("amount")),
                fee_open=_float(d.get("fee_open")),
                fee_close=_float(d.get("fee_close")),
                close_profit=_float(d.get("close_profit")),
                exit_reason=None if reason is None else str(reason),
            )
        )
    return out


# --------------------------------------------------------------------------- lab replay


def _utc(ts: pd.Timestamp | datetime | str) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def replay_trades(
    plan: Mapping[str, Any] | Plan,
    history: pd.DataFrame,
    *,
    start: pd.Timestamp | datetime | str,
    end: pd.Timestamp | datetime | str,
    costs: CostModel,
    init_cash: float = 10_000.0,
) -> list[Trade]:
    """Lab-engine trades of ``plan`` on ``history`` entered in ``[start, end)``, flat at
    ``start`` (like a freshly started paper bot).

    Signals are compiled on the rows of ``history`` before ``end`` (earlier rows are warm-up;
    rows at or after ``end`` are dropped, so no look-ahead), the engine runs on the rows in
    ``[start, end)`` with ``costs`` and the plan's risk, and vol-scaled slippage is warmed up
    on the same history. Nothing is recorded."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    t0, t1 = _utc(start), _utc(end)
    if t1 <= t0:
        raise ValueError(f"end {t1} must be after start {t0}")
    history = history.loc[history.index < t1]
    mask = history.index >= t0
    if int(mask.sum()) < 2:
        return []
    sig = compile_plan(p, history)
    candles = history.loc[mask]
    result = run_signals(
        candles,
        sig.entries.loc[mask],
        sig.exits.loc[mask],
        costs=costs,
        risk=p.risk,
        init_cash=init_cash,
        cost_history=history,
    )
    return list(result.trades)


def backtest_same_period(
    plan: Mapping[str, Any] | Plan,
    *,
    start: pd.Timestamp | datetime | str,
    end: pd.Timestamp | datetime | str,
    cfg: LabConfig,
    loader: CandleLoader,
    costs: CostModel | None = None,
) -> list[Trade]:
    """Lab-engine trades of ``plan`` entered in ``[start, end)`` (:func:`replay_trades` on the
    history ``loader`` returns before ``end``), with ``costs`` (default: the lab's)."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    t0, t1 = _utc(start), _utc(end)
    if t1 <= t0:
        raise ValueError(f"end {t1} must be after start {t0}")
    history = loader(p.symbol, p.interval, t1)
    return replay_trades(
        p, history, start=t0, end=t1, costs=costs or cfg.costs, init_cash=cfg.init_cash
    )


# --------------------------------------------------------------------------- comparison


def _stats(returns: Sequence[float]) -> TradeStats:
    r = np.asarray([x for x in returns if x is not None and math.isfinite(x)], dtype=float)
    if r.size == 0:
        return TradeStats(n_trades=0, total_return=None, win_rate=None, mean_trade_return=None)
    return TradeStats(
        n_trades=int(r.size),
        total_return=float(np.prod(1.0 + r) - 1.0),
        win_rate=float(np.mean(r > 0)),
        mean_trade_return=float(np.mean(r)),
    )


def forward_stats(trades: Sequence[ForwardTrade]) -> TradeStats:
    """Stats over closed forward trades: compounded total, win rate, mean net trade return."""
    rets = [t.net_return() for t in trades if t.closed]
    return _stats([x for x in rets if x is not None])


def backtest_stats(trades: Sequence[Trade]) -> TradeStats:
    """Same stats over closed lab trades (a trade still open at the period end is excluded)."""
    return _stats([t.return_pct for t in trades if t.exit_time is not None])


def match_entries(
    forward: Sequence[ForwardTrade], backtest: Sequence[Trade], *, interval: str
) -> list[tuple[Trade, ForwardTrade]]:
    """One-to-one matches of each backtest entry with the nearest unused forward entry within
    :data:`ENTRY_MATCH_BARS` bars (freqtrade enters just after the signal bar closes, which is
    the lab's fill bar open)."""
    tol = pd.Timedelta(seconds=ENTRY_MATCH_BARS * interval_seconds(interval))
    used: set[int] = set()
    pairs: list[tuple[Trade, ForwardTrade]] = []
    for bt in sorted(backtest, key=lambda t: t.entry_time):
        best: tuple[pd.Timedelta, int] | None = None
        for i, fw in enumerate(forward):
            if i in used:
                continue
            gap = abs(fw.open_time - bt.entry_time)
            if gap <= tol and (best is None or gap < best[0]):
                best = (gap, i)
        if best is not None:
            used.add(best[1])
            pairs.append((bt, forward[best[1]]))
    return pairs


def compare(
    forward: Sequence[ForwardTrade],
    backtest: Sequence[Trade],
    *,
    interval: str,
    candles: pd.DataFrame,
    candidate: str = "",
    plan_hash: str = "",
    symbol: str = "",
    start: pd.Timestamp | datetime | str | None = None,
    end: pd.Timestamp | datetime | str | None = None,
    now: datetime | None = None,
) -> PaperComparison:
    """Forward (freqtrade dry-run) versus backtest (lab replay) over ``[start, end)``.

    ``start``/``end`` default to the span of ``candles``. Forward trades opened outside the
    period are ignored. Verdict: ``insufficient_data`` with fewer than
    :data:`MIN_FORWARD_TRADES` closed forward trades or a period shorter than
    :data:`MIN_FORWARD_DAYS`; ``diverging`` when the entry match rate is below
    :data:`MIN_ENTRY_MATCH_RATE` or total returns differ by more than
    ``max(RETURN_DIFF_ABS, RETURN_DIFF_REL x |backtest total|)``; else ``consistent``.
    """
    step = pd.Timedelta(seconds=interval_seconds(interval))
    if (start is None or end is None) and candles.empty:
        raise ValueError("candles are empty; pass start and end explicitly")
    t0 = _utc(start) if start is not None else candles.index[0]
    t1 = _utc(end) if end is not None else candles.index[-1] + step
    fwd = [t for t in forward if t0 <= t.open_time < t1]
    bt = [t for t in backtest if t0 <= t.entry_time < t1]

    f_stats, b_stats = forward_stats(fwd), backtest_stats(bt)
    pairs = match_entries(fwd, bt, interval=interval)
    match_rate = len(pairs) / len(bt) if bt else None
    slips: list[float] = []
    opens = candles["open"] if "open" in candles.columns else pd.Series(dtype=float)
    for b, f in pairs:
        if b.entry_time in opens.index:
            ref = float(opens.loc[b.entry_time])
            if ref > 0:
                slips.append((f.open_rate / ref - 1.0) * 1e4)
    slippage = float(np.mean(slips)) if slips else None

    notes: list[str] = [
        f"Thresholds: >= {MIN_FORWARD_TRADES} closed forward trades and >= {MIN_FORWARD_DAYS} "
        f"days; match rate >= {MIN_ENTRY_MATCH_RATE:.0%}; total-return gap <= max("
        f"{RETURN_DIFF_ABS:.0%}, {RETURN_DIFF_REL:.0%} of the backtest's).",
        "Forward stats use freqtrade's close_profit (fees included, fixed stake); backtest stats "
        "use the lab's net trade returns. Both compound per-trade returns.",
    ]
    days = (t1 - t0) / pd.Timedelta(days=1)
    n_fwd = f_stats["n_trades"]
    verdict: str
    if n_fwd < MIN_FORWARD_TRADES or days < MIN_FORWARD_DAYS:
        verdict = "insufficient_data"
        notes.append(
            f"{n_fwd} closed forward trades over {days:.1f} days: too little to judge yet."
        )
    else:
        reasons: list[str] = []
        if match_rate is not None and match_rate < MIN_ENTRY_MATCH_RATE:
            reasons.append(f"entry match rate {match_rate:.0%} < {MIN_ENTRY_MATCH_RATE:.0%}")
        if not bt:
            reasons.append(f"the backtest has no entries but the paper bot made {n_fwd} trades")
        f_tot, b_tot = f_stats["total_return"], b_stats["total_return"]
        if f_tot is not None:
            b_val = b_tot if b_tot is not None else 0.0
            limit = max(RETURN_DIFF_ABS, RETURN_DIFF_REL * abs(b_val))
            if abs(f_tot - b_val) > limit:
                reasons.append(
                    f"total return {f_tot:+.2%} forward vs {b_val:+.2%} backtest "
                    f"(gap above {limit:.2%})"
                )
        verdict = "diverging" if reasons else "consistent"
        notes.extend(reasons)
    generated = (now or datetime.now(UTC)).astimezone(UTC)
    return PaperComparison(
        candidate=candidate,
        plan_hash=plan_hash,
        symbol=symbol,
        interval=interval,
        period_start=int(t0.timestamp()),
        period_end=int(t1.timestamp()),
        generated_at=generated.isoformat(),
        forward=f_stats,
        backtest=b_stats,
        entry_match_rate=match_rate,
        mean_entry_slippage_bps=slippage,
        verdict=verdict,  # type: ignore[typeddict-item]
        notes=notes,
    )


# --------------------------------------------------------------------------- protocol observation


def _bar_of(ts: pd.Timestamp, step: int) -> pd.Timestamp:
    return pd.Timestamp(int(ts.timestamp()) // step * step, unit="s", tz="UTC")


def frozen_costs(protocol: PaperProtocol) -> CostModel:
    """The lab cost model frozen in ``protocol`` (never the current lab.toml)."""
    s = protocol.shortfall
    return CostModel(s.fee_rate, s.slippage_rate, s.slippage_vol_mult, s.slippage_cap)


def modelled_slippage(history: pd.DataFrame, costs: CostModel) -> pd.Series:
    """The lab's per-bar slippage fraction for a fill at each bar's open (vol-scaled, causal:
    bar j uses rows before j), computed on ``history`` so it is warmed up."""
    return pd.Series(slippage_series(history, costs), index=history.index, dtype=float)


def match_fill_bars(
    paper_bars: Sequence[int], lab_bars: Sequence[int], *, tolerance_seconds: int
) -> list[tuple[int, int]]:
    """Maximum one-to-one matching of paper fill bars to lab fill bars (UNIX seconds) with
    ``|paper - lab| <= tolerance_seconds``, as ``(paper_index, lab_index)`` pairs.

    Lab fills are taken in time order and each takes the EARLIEST unused paper fill inside its
    window: a paper fill too early for this lab fill is too early for every later one, so the
    greedy choice never blocks a later match (maximum cardinality for interval matching)."""
    if tolerance_seconds < 0:
        raise ValueError(f"tolerance_seconds must be >= 0, got {tolerance_seconds}")
    paper = sorted(range(len(paper_bars)), key=lambda i: paper_bars[i])
    used = [False] * len(paper)
    pairs: list[tuple[int, int]] = []
    lo = 0
    for j in sorted(range(len(lab_bars)), key=lambda k: lab_bars[k]):
        lab = lab_bars[j]
        while lo < len(paper) and paper_bars[paper[lo]] < lab - tolerance_seconds:
            lo += 1
        k = lo
        while k < len(paper) and paper_bars[paper[k]] <= lab + tolerance_seconds:
            if not used[k]:
                used[k] = True
                pairs.append((paper[k], j))
                break
            k += 1
    return sorted(pairs)


def _price_shortfall(side: str, paper: float, ref: float, slip: float) -> float:
    if side == "entry":
        return (paper / (ref * (1.0 + slip)) - 1.0) * 1e4
    return (1.0 - paper / (ref * (1.0 - slip))) * 1e4


def fill_shortfalls(
    trades: Sequence[ForwardTrade],
    candles: pd.DataFrame,
    slippage: pd.Series,
    *,
    interval: str,
    fee_rate: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[list[Fill], int]:
    """Adverse implementation shortfall (bps) of every entry and signal exit filled in
    ``[start, end)`` against the lab's modelled fill for the bar the paper fill landed in
    (``open_j * (1 + s_j)`` entry, ``open_j * (1 - s_j)`` exit, plus the fee difference
    ``(paper_fee - fee_rate) * 1e4``). Returns the fills and the number skipped (stop/ROI exits,
    or no candle for the bar). The protocol judges against the MATCHED lab fill bar instead
    (:func:`observe_paper`); this same-bar view is kept for the replay report."""
    step = interval_seconds(interval)
    opens = candles["open"]
    fills: list[Fill] = []
    skipped = 0

    def model(ts: pd.Timestamp) -> tuple[float, float] | None:
        bar = _bar_of(ts, step)
        if bar not in opens.index or bar not in slippage.index:
            return None
        ref = float(opens.loc[bar])
        return (ref, float(slippage.loc[bar])) if ref > 0 else None

    for t in trades:
        if start <= t.open_time < end:
            m = model(t.open_time)
            if m is None or t.open_rate <= 0:
                skipped += 1
            else:
                price = _price_shortfall("entry", t.open_rate, *m)
                fee = 0.0 if t.fee_open is None else (t.fee_open - fee_rate) * 1e4
                fills.append(Fill(int(t.open_time.timestamp()), "entry", price + fee))
        if t.closed and t.close_time is not None and start <= t.close_time < end:
            m = model(t.close_time)
            if (
                t.exit_reason not in SIGNAL_EXIT_REASONS
                or m is None
                or t.close_rate is None
                or t.close_rate <= 0
            ):
                skipped += 1
                continue
            price = _price_shortfall("exit", t.close_rate, *m)
            fee = 0.0 if t.fee_close is None else (t.fee_close - fee_rate) * 1e4
            fills.append(Fill(int(t.close_time.timestamp()), "exit", price + fee))
    fills.sort(key=lambda f: f.time)
    return fills, skipped


def paper_bar_returns(
    trades: Sequence[ForwardTrade], candles: pd.DataFrame, *, interval: str
) -> np.ndarray:
    """Per-bar returns of the paper position on ``candles`` rows, fully invested and net of
    freqtrade's fees (the Sharpe is scale-free, so the bot's fixed stake does not matter).

    Per trade: cost basis ``open_rate * (1 + fee_open)``; each held bar marks to its close; the
    exit bar ends at ``close_rate * (1 - fee_close)``; an open trade marks to the last close.
    Flat bars return 0. Only rows of ``candles`` are used (no bar after the last row)."""
    step = interval_seconds(interval)
    index = pd.DatetimeIndex(candles.index)
    closes = candles["close"].to_numpy(dtype=float)
    growth = np.ones(len(index))
    for t in trades:
        first = int(index.searchsorted(_bar_of(t.open_time, step), side="left"))
        last_bar = _bar_of(t.close_time, step) if t.closed and t.close_time is not None else None
        prev = t.open_rate * (1.0 + (t.fee_open or 0.0))
        for i in range(first, len(index)):
            if last_bar is not None and index[i] >= last_bar:
                if index[i] == last_bar and t.close_rate is not None and prev > 0:
                    growth[i] *= t.close_rate * (1.0 - (t.fee_close or 0.0)) / prev
                break
            if prev > 0:
                growth[i] *= closes[i] / prev
            prev = closes[i]
    return growth - 1.0


@dataclass(frozen=True)
class _PaperFill:
    time: pd.Timestamp  # paper fill time
    bar: int  # UNIX seconds of the bar it landed in
    rate: float
    fee: float | None
    kind: str  # exits: "signal" / "stop" / "target" / "other"; entries: "entry"


# freqtrade exit reasons by the lab exit they correspond to (a stop or ROI exit where the lab
# exited on a signal is a different exit: it never matches)
EXIT_KIND_BY_REASON: dict[str, str] = {
    **{r: "signal" for r in SIGNAL_EXIT_REASONS},
    "stop_loss": "stop",
    "stoploss_on_exchange": "stop",
    "trailing_stop_loss": "stop",
    "roi": "target",
}
LAB_EXIT_KIND: dict[str, str] = {"signal": "signal", "stop_loss": "stop", "take_profit": "target"}


def _match_by_kind(
    paper: Sequence[_PaperFill], lab: Sequence[tuple[int, str]], tolerance_seconds: int
) -> list[tuple[int, int]]:
    """:func:`match_fill_bars` within each exit kind; ``(paper_index, lab_index)`` pairs."""
    pairs: list[tuple[int, int]] = []
    for kind in sorted({f.kind for f in paper} | {k for _, k in lab}):
        pi = [i for i, f in enumerate(paper) if f.kind == kind]
        li = [j for j, (_, k) in enumerate(lab) if k == kind]
        if kind == "other" or not pi or not li:
            continue
        sub = match_fill_bars(
            [paper[i].bar for i in pi], [lab[j][0] for j in li], tolerance_seconds=tolerance_seconds
        )
        pairs.extend((pi[a], li[b]) for a, b in sub)
    return sorted(pairs)


def observe_paper(
    protocol: PaperProtocol,
    trades: Sequence[ForwardTrade],
    candles: pd.DataFrame,
    *,
    lab_trades: Sequence[Trade],
    slippage: pd.Series,
    asof: pd.Timestamp | datetime | str,
) -> ObservedPaper:
    """What the dry-run bot did in ``[protocol.start, min(asof, decision_end))``.

    ``candles`` are closed bars (rows past the period are ignored); ``slippage`` is
    :func:`modelled_slippage` on a history covering them; ``lab_trades`` is the lab replay of
    the plan on the same candles, flat at ``protocol.start`` (:func:`replay_trades`). ``asof``
    is when compare looked (it may be past the decision bar; it drives the data grace period).

    * Coverage: every expected bar time in the period versus the bar times with a candle; a
      paper entry or exit whose bar has no candle is unjudged (``fills_missing_candle``).
    * Entries count trades opened in the period; a bar is in market when a trade opened before
      its close and was still open at its close (the lab's position-at-close rule).
    * Timing: paper entries are matched to lab entries, and paper exits of every reason to lab
      exits of the same kind (signal / stop / take-profit), within
      ``protocol.matching.tolerance_bars`` (:func:`match_fill_bars`); each match records its lag
      (paper bar minus lab bar), counted per side: early (the bot acted before the bar closed)
      and late (the lab acted earlier than live could). An unmatched paper entry is *settled*
      once every bar a lab entry could match it on is observed.
    * Shortfall is judged on lag-0 matched entries and signal exits only, against the lab
      bar's modelled fill (stop / take-profit fills are at a level in the lab: not judged).
    """
    step = interval_seconds(protocol.interval)
    tol = protocol.matching.tolerance_bars * step
    t0 = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    until = int(_utc(asof).timestamp())
    asof_s = min(until, protocol.decision_end)
    t1 = pd.Timestamp(max(asof_s, protocol.start), unit="s", tz="UTC")
    s0, s1 = protocol.start, int(t1.timestamp())
    rows = candles.loc[(candles.index >= t0) & (candles.index < t1)]
    rows = rows[~rows.index.duplicated(keep="last")]
    present = set(int(x) for x in rows.index.as_unit("s").asi8)
    expected_bars = range(s0, s1, step)
    bars_present = sum(1 for b in expected_bars if b in present)

    in_period = [t for t in trades if t0 <= t.open_time < t1]
    held = 0
    for bar in rows.index:
        bar_close = bar + pd.Timedelta(seconds=step)
        if any(
            t.open_time < bar_close and (t.close_time is None or t.close_time >= bar_close)
            for t in in_period
        ):
            held += 1

    def bar_of(ts: pd.Timestamp) -> int:
        return int(ts.timestamp()) // step * step

    # paper fills; a fill on a missing candle is unjudged
    missing = 0
    missing_entries = 0
    entries: list[_PaperFill] = []
    exits: list[_PaperFill] = []
    for t in in_period:
        b = bar_of(t.open_time)
        if b not in present:
            missing += 1
            missing_entries += 1
        else:
            entries.append(_PaperFill(t.open_time, b, t.open_rate, t.fee_open, "entry"))
        if t.closed and t.close_time is not None and t0 <= t.close_time < t1:
            b = bar_of(t.close_time)
            kind = EXIT_KIND_BY_REASON.get(t.exit_reason or "", "other")
            if b not in present:
                missing += 1
            else:
                rate = t.close_rate if t.close_rate is not None else float("nan")
                exits.append(_PaperFill(t.close_time, b, rate, t.fee_close, kind))

    lab_in = [lt for lt in lab_trades if t0 <= lt.entry_time < t1]
    lab_entry_bars = [int(lt.entry_time.timestamp()) for lt in lab_in]
    lab_exits = [
        (int(lt.exit_time.timestamp()), LAB_EXIT_KIND.get(lt.exit_reason, "other"))
        for lt in lab_trades
        if lt.exit_time is not None and t0 <= lt.exit_time < t1
    ]
    entry_pairs = match_fill_bars([f.bar for f in entries], lab_entry_bars, tolerance_seconds=tol)
    exit_pairs = _match_by_kind(exits, lab_exits, tol)

    # A missing candle changes the lab replay from that bar on (indicators skip it), so only
    # irregular fills whose whole match window lies before the first missing bar are settled.
    first_gap = next((b for b in expected_bars if b not in present), None)

    def before_gap(bar: int) -> bool:
        return first_gap is None or bar + tol < first_gap

    irregular_before_gap = 0
    for paper, lab_bars, pairs in (
        (entries, lab_entry_bars, entry_pairs),
        (exits, [b for b, _ in lab_exits], exit_pairs),
    ):
        paired_p = {i for i, _ in pairs}
        paired_l = {j for _, j in pairs}
        irregular_before_gap += sum(
            1 for i, f in enumerate(paper) if i not in paired_p and before_gap(f.bar)
        )
        irregular_before_gap += sum(
            1 for j, b in enumerate(lab_bars) if j not in paired_l and before_gap(b)
        )
        irregular_before_gap += sum(
            1
            for i, j in pairs
            if paper[i].bar != lab_bars[j] and before_gap(max(paper[i].bar, lab_bars[j]))
        )

    opens = rows["open"]
    fee_rate = protocol.shortfall.fee_rate
    fills: list[Fill] = []
    early = {"entry": 0, "exit": 0}
    late = {"entry": 0, "exit": 0}
    for side, paper, lab_bars, pairs in (
        ("entry", entries, lab_entry_bars, entry_pairs),
        ("exit", exits, [b for b, _ in lab_exits], exit_pairs),
    ):
        for i, j in pairs:
            f, lab_bar_s = paper[i], lab_bars[j]
            lag = (f.bar - lab_bar_s) // step
            if lag < 0:
                early[side] += 1  # the bot acted before the bar closed (forming candle?)
            elif lag > 0:
                late[side] += 1  # the lab acted earlier than live could (look-ahead in the lab?)
            judged_kind = side == "entry" or f.kind == "signal"
            if lag != 0 or not judged_kind:
                continue
            lab_bar = pd.Timestamp(lab_bar_s, unit="s", tz="UTC")
            if lab_bar not in opens.index or lab_bar not in slippage.index:
                continue
            ref = float(opens.loc[lab_bar])
            if ref <= 0 or not f.rate > 0:
                continue
            slip = float(slippage.loc[lab_bar])
            price = _price_shortfall(side, f.rate, ref, slip)
            fee = 0.0 if f.fee is None else (f.fee - fee_rate) * 1e4
            fills.append(
                Fill(
                    int(f.time.timestamp()),
                    "entry" if side == "entry" else "exit",
                    price + fee,
                    0,
                    slip * 1e4,
                )
            )
    fills.sort(key=lambda x: x.time)

    # an unmatched paper entry is settled when every bar that could match it is observed
    matched_paper = {i for i, _ in entry_pairs}
    settled = 0
    for i, f in enumerate(entries):
        if i in matched_paper or f.bar + tol >= s1 or not before_gap(f.bar):
            continue
        window = range(max(f.bar - tol, s0), f.bar + tol + 1, step)
        if all(b in present for b in window):
            settled += 1

    rets = paper_bar_returns(in_period, rows, interval=protocol.interval)
    return ObservedPaper(
        asof=s1,
        observed_until=max(until, s1),
        bars_expected=len(expected_bars),
        bars_elapsed=bars_present,
        n_entries=len(in_period),
        bars_in_market=held,
        fills=tuple(fills),
        fills_skipped=sum(1 for f in exits if f.kind != "signal"),
        fills_missing_candle=missing,
        entries_missing_candle=missing_entries,
        lab_entries=len(lab_in),
        matched_entries=len(entry_pairs),
        unmatched_paper_settled=settled,
        paper_exits=len(exits) + (missing - missing_entries),
        lab_exits=len(lab_exits),
        matched_exits=len(exit_pairs),
        paper_signal_exits=sum(1 for f in exits if f.kind == "signal"),
        lab_signal_exits=sum(1 for _, k in lab_exits if k == "signal"),
        entry_early=early["entry"],
        entry_late=late["entry"],
        exit_early=early["exit"],
        exit_late=late["exit"],
        irregular_before_gap=irregular_before_gap,
        bar_returns=tuple(float(x) for x in rets),
    )


def render_verdict_markdown(verdict: Verdict) -> list[str]:
    """Markdown lines for the protocol verdict (comparison summary)."""
    lines = [
        f"## Protocol verdict: {verdict.verdict}",
        "",
        verdict.headline,
        "",
        f"Mode: {verdict.mode}; {verdict.elapsed_days:.1f} days elapsed; data "
        f"{verdict.bars_present} of {verdict.bars_expected} bars, {verdict.fills_unjudged} fills "
        f"unjudged ({'complete' if verdict.data_complete else 'incomplete'}); protocol sha256 "
        f"`{verdict.protocol_sha256[:12]}`.",
        "",
        *([f"Fail kind: {', '.join(verdict.fail_kinds)}.", ""] if verdict.fail_kinds else []),
        *(f"- Alert: {a}" for a in verdict.alerts),
        *([""] if verdict.alerts else []),
        "| Check | Status | Observed | Band | Note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in verdict.checks:
        obs = "-" if c.observed is None else f"{c.observed:.4g}"
        band = (
            f"[{'-' if c.lo is None else f'{c.lo:.4g}'}, {'-' if c.hi is None else f'{c.hi:.4g}'}]"
        )
        lines.append(f"| {c.name} | {c.status} | {obs} | {band} | {c.note} |")
    if verdict.weeks:
        lines += [
            "",
            "| Week | Fills | Median shortfall (bps) | Judged | Alert | Gross breakage |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for w in verdict.weeks:
            med = "-" if w.median_bps is None else f"{w.median_bps:+.1f}"
            yes = {True: "yes", False: "no"}
            lines.append(
                f"| {w.week} | {w.fills} | {med} | {yes[w.judged]} | {yes[w.alert]} | "
                f"{yes[w.gross]} |"
            )
    return [*lines, ""]


# --------------------------------------------------------------------------- writing


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _fmt_stats(s: TradeStats) -> list[str]:
    win = "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
    return [str(s["n_trades"]), _pct(s["total_return"]), win, _pct(s["mean_trade_return"])]


def render_comparison_markdown(cmp: PaperComparison, verdict: Verdict | None = None) -> str:
    start = datetime.fromtimestamp(cmp["period_start"], UTC).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(cmp["period_end"], UTC).strftime("%Y-%m-%d %H:%M")
    match = "-" if cmp["entry_match_rate"] is None else f"{cmp['entry_match_rate']:.0%}"
    slip = (
        "-"
        if cmp["mean_entry_slippage_bps"] is None
        else f"{cmp['mean_entry_slippage_bps']:+.1f} bps"
    )
    f, b = _fmt_stats(cmp["forward"]), _fmt_stats(cmp["backtest"])
    lines = [
        f"# Paper vs backtest: {cmp['candidate']} ({cmp['symbol']} {cmp['interval']})",
        "",
        f"Period {start} to {end} UTC. Plan `{cmp['plan_hash'][:12]}`. "
        f"Generated {cmp['generated_at']}.",
        "",
        f"**Verdict: {cmp['verdict']}**",
        "",
        *(render_verdict_markdown(verdict) if verdict is not None else []),
        "| | Trades | Total return | Win rate | Mean trade |",
        "| --- | --- | --- | --- | --- |",
        f"| Forward (dry-run) | {' | '.join(f)} |",
        f"| Backtest (lab) | {' | '.join(b)} |",
        "",
        f"Entry match rate: {match}. Mean entry slippage vs the lab's next-bar open: {slip}.",
        "",
        *(f"- {n}" for n in cmp["notes"]),
        "",
        f"> {DISCLAIMER}",
        "",
    ]
    return "\n".join(lines)


def _fsynced_tmp(path: Path, text: str) -> Path:
    """``text`` in a fresh ``mkstemp`` file next to ``path``, flushed and fsynced (a unique
    name, so two writers never share a temporary file); the caller renames or links it."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def _fsync_dir(folder: Path) -> None:
    """Best-effort fsync of a directory so a rename survives a power loss."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` atomically: a fresh fsynced temporary file in the same directory,
    then ``os.replace`` (readers see the old file or the new one, never a partial one)."""
    tmp = _fsynced_tmp(path, text)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _create_exclusive(path: Path, text: str) -> bool:
    """Create ``path`` atomically only if it does not exist; False (nothing written) if it
    does. Never overwrites."""
    tmp = _fsynced_tmp(path, text)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)
    return True


COMPARISON_NAME = re.compile(r"comparison-\d{4}-\d{2}-\d{2}\.json")


def comparison_path(cmp: PaperComparison, paper_dir: Path) -> Path:
    """``<paper_dir>/<candidate>/comparison-<YYYY-MM-DD>.json`` for ``cmp`` (not written)."""
    folder = Path(paper_dir) / check_candidate_name(cmp["candidate"])
    return folder / f"comparison-{str(cmp['generated_at'])[:10]}.json"


def _comparison_texts(cmp: PaperComparison, verdict: Verdict | None) -> tuple[str, str]:
    data: dict[str, Any] = dict(cmp)
    if verdict is not None:
        data["protocol_verdict"] = verdict.to_dict()
    body = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return body, render_comparison_markdown(cmp, verdict)


def write_missing_comparison(
    cmp: PaperComparison, paper_dir: Path, *, name: str, verdict: Verdict | None = None
) -> list[Path]:
    """Create ONLY the missing files of the comparison ``name`` (``comparison-<date>.json``
    and its ``.md``) in the candidate's folder, each atomically and exclusively: an existing
    file is never overwritten. Returns the paths written. Used when a final verdict's
    journal line names a comparison file that was never written (the journal is never
    touched here)."""
    if not COMPARISON_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a comparison file name (comparison-YYYY-MM-DD.json)")
    folder = Path(paper_dir) / check_candidate_name(cmp["candidate"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    body, md = _comparison_texts(cmp, verdict)
    written: list[Path] = []
    for target, text in ((path, body), (path.with_suffix(".md"), md)):
        if not target.exists() and _create_exclusive(target, text):
            written.append(target)
    return written


def write_comparison(
    cmp: PaperComparison, paper_dir: Path, *, verdict: Verdict | None = None
) -> Path:
    """Write :func:`comparison_path` (atomically) and a Markdown summary next to it; return the
    JSON path. With ``verdict`` the JSON gains a ``protocol_verdict`` object (the frozen
    protocol's pass / fail / continue / inconclusive). ``compare`` writes it only after the
    verdict line is in the journal."""
    path = comparison_path(cmp, paper_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body, md = _comparison_texts(cmp, verdict)
    _atomic_write(path, body)
    _atomic_write(path.with_suffix(".md"), md)
    return path


# --------------------------------------------------------------------------- public candles


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    """The response's ``Retry-After`` in seconds (Binance sends seconds), else ``default``."""
    raw = resp.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _get_klines(
    http: httpx.Client,
    host: str,
    params: Mapping[str, Any],
    *,
    sleep: Callable[[float], None],
    waited: list[float],
) -> httpx.Response:
    """One klines request with the bounded 429 back-off of :func:`public_kline_loader`."""
    url = f"https://{host}/api/v3/klines"
    for attempt in range(KLINE_MAX_RETRIES + 1):
        resp = http.get(url, params=dict(params))
        if resp.status_code == 418:
            raise httpx.HTTPStatusError(
                f"{host} returned HTTP 418 (this IP is temporarily banned for exceeding the "
                "rate limit); not retrying: wait before running compare again",
                request=resp.request,
                response=resp,
            )
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        wait = _retry_after_seconds(resp, KLINE_DEFAULT_BACKOFF_S * 2**attempt)
        if attempt == KLINE_MAX_RETRIES or waited[0] + wait > KLINE_MAX_WAIT_S:
            raise httpx.HTTPStatusError(
                f"{host} returned HTTP 429 (rate limited) on {attempt + 1} attempt(s); gave "
                f"up after waiting {waited[0]:.0f} s in total (Retry-After "
                f"{resp.headers.get('Retry-After', 'not sent')}; at most {KLINE_MAX_RETRIES} "
                f"retries and {KLINE_MAX_WAIT_S:.0f} s): try again later",
                request=resp.request,
                response=resp,
            )
        sleep(wait)
        waited[0] += wait
    raise AssertionError("unreachable")  # the loop always returns or raises


def public_kline_loader(
    *,
    since: pd.Timestamp | datetime | str,
    host: str = "data-api.binance.vision",
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CandleLoader:
    """``loader(symbol, interval, end)`` fetching CLOSED klines in ``[since, end)`` from
    ``https://<host>/api/v3/klines`` (public market data, no API key). Binance prices are
    strings and times milliseconds; the frame uses a UTC index, floats, and ``tradable =
    volume > 0``. A still-forming bar (close time in the future) is dropped. Gaps stay gaps.

    Rate limits: an HTTP 429 is retried at most :data:`KLINE_MAX_RETRIES` times per request,
    waiting ``Retry-After`` seconds (else a doubling :data:`KLINE_DEFAULT_BACKOFF_S`), and
    never past :data:`KLINE_MAX_WAIT_S` of total waiting per load; an HTTP 418 (IP ban) is
    never retried. Either then raises ``httpx.HTTPStatusError`` naming the status. Any other
    HTTP error or a network failure raises at once (``httpx.HTTPError``)."""
    t_since = _utc(since)

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        t_end = _utc(end)
        now_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
        end_ms = int(t_end.timestamp() * 1000)
        cursor = int(t_since.timestamp() * 1000)
        rows: list[list[Any]] = []
        own = client is None
        http = client or httpx.Client(timeout=20.0)
        waited = [0.0]  # total Retry-After wait of this load
        try:
            while cursor < end_ms:
                params = {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": KLINE_PAGE_LIMIT,
                }
                resp = _get_klines(http, host, params, sleep=sleep, waited=waited)
                page = resp.json()
                if not page:
                    break
                rows.extend(page)
                last_open = int(page[-1][0])
                if last_open < cursor or len(page) < KLINE_PAGE_LIMIT:
                    break
                cursor = last_open + 1
        finally:
            if own:
                http.close()
        kept = [r for r in rows if int(r[0]) < end_ms and int(r[6]) < now_ms]
        index = pd.DatetimeIndex(
            pd.to_datetime([int(r[0]) // 1000 for r in kept], unit="s", utc=True), name="time"
        )
        frame = pd.DataFrame(
            {
                col: np.asarray([float(r[i]) for r in kept], dtype=float)
                for i, col in ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume"))
            },
            index=index,
        )
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame["tradable"] = frame["volume"].to_numpy() > 0
        return frame

    return load


__all__ = [
    "COMPARISON_NAME",
    "DISCLAIMER",
    "EXIT_KIND_BY_REASON",
    "KLINE_MAX_RETRIES",
    "KLINE_MAX_WAIT_S",
    "LAB_EXIT_KIND",
    "MIN_ENTRY_MATCH_RATE",
    "MIN_FORWARD_DAYS",
    "MIN_FORWARD_TRADES",
    "RETURN_DIFF_ABS",
    "RETURN_DIFF_REL",
    "SIGNAL_EXIT_REASONS",
    "ForwardTrade",
    "backtest_same_period",
    "backtest_stats",
    "compare",
    "comparison_path",
    "fill_shortfalls",
    "forward_stats",
    "frozen_costs",
    "match_entries",
    "match_fill_bars",
    "modelled_slippage",
    "observe_paper",
    "paper_bar_returns",
    "public_kline_loader",
    "read_dryrun_trades",
    "render_comparison_markdown",
    "render_verdict_markdown",
    "replay_trades",
    "write_comparison",
    "write_missing_comparison",
]
