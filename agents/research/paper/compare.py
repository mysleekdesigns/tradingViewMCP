"""Weekly forward-vs-backtest comparison for a paper-traded candidate.

* :func:`read_dryrun_trades` reads freqtrade's dry-run SQLite (opened read-only).
* :func:`backtest_same_period` replays the same plan with the lab engine over the paper period
  (signals computed on the history through ``end``, trades inside ``[start, end)``). It records
  nothing in the ledger: this is a consistency check of a frozen candidate, never a research
  input, and must not be used to re-tune anything.
* :func:`compare` builds the :class:`lab.research.contract.PaperComparison`; arithmetic stays
  in Python. :func:`write_comparison` stores it as JSON plus a short Markdown summary.
* :func:`public_kline_loader` fetches closed candles from Binance's public market-data host
  (no API key) for weeks the monthly archive does not cover yet.

Results are historical, not forward-looking.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.engine import run_signals
from lab.plan import compile_plan, parse_plan
from lab.research.contract import PaperComparison, TradeStats
from lab.runner import CandleLoader
from lab.types import Plan, Trade, interval_seconds
from research.paper.freqtrade import check_candidate_name

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


def backtest_same_period(
    plan: Mapping[str, Any] | Plan,
    *,
    start: pd.Timestamp | datetime | str,
    end: pd.Timestamp | datetime | str,
    cfg: LabConfig,
    loader: CandleLoader,
) -> list[Trade]:
    """Lab-engine trades of ``plan`` entered in ``[start, end)``.

    Signals come from the full history ``loader`` returns before ``end`` (earlier bars are
    warm-up), the engine runs on the bars in ``[start, end)`` with the lab's costs and the
    plan's risk, and vol-scaled slippage is warmed up on the same history. Nothing is recorded.
    """
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    t0, t1 = _utc(start), _utc(end)
    if t1 <= t0:
        raise ValueError(f"end {t1} must be after start {t0}")
    history = loader(p.symbol, p.interval, t1)
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
        costs=cfg.costs,
        risk=p.risk,
        init_cash=cfg.init_cash,
        cost_history=history,
    )
    return list(result.trades)


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


# --------------------------------------------------------------------------- writing


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _fmt_stats(s: TradeStats) -> list[str]:
    win = "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
    return [str(s["n_trades"]), _pct(s["total_return"]), win, _pct(s["mean_trade_return"])]


def render_comparison_markdown(cmp: PaperComparison) -> str:
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


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_comparison(cmp: PaperComparison, paper_dir: Path) -> Path:
    """Write ``<paper_dir>/<candidate>/comparison-<YYYY-MM-DD>.json`` (atomically) and a
    Markdown summary next to it; return the JSON path."""
    folder = Path(paper_dir) / check_candidate_name(cmp["candidate"])
    folder.mkdir(parents=True, exist_ok=True)
    date = str(cmp["generated_at"])[:10]
    path = folder / f"comparison-{date}.json"
    _atomic_write(path, json.dumps(dict(cmp), indent=2, sort_keys=True, allow_nan=False) + "\n")
    _atomic_write(path.with_suffix(".md"), render_comparison_markdown(cmp))
    return path


# --------------------------------------------------------------------------- public candles


def public_kline_loader(
    *,
    since: pd.Timestamp | datetime | str,
    host: str = "data-api.binance.vision",
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> CandleLoader:
    """``loader(symbol, interval, end)`` fetching CLOSED klines in ``[since, end)`` from
    ``https://<host>/api/v3/klines`` (public market data, no API key). Binance prices are
    strings and times milliseconds; the frame uses a UTC index, floats, and ``tradable =
    volume > 0``. A still-forming bar (close time in the future) is dropped. Gaps stay gaps."""
    t_since = _utc(since)

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        t_end = _utc(end)
        now_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
        end_ms = int(t_end.timestamp() * 1000)
        cursor = int(t_since.timestamp() * 1000)
        rows: list[list[Any]] = []
        own = client is None
        http = client or httpx.Client(timeout=20.0)
        try:
            while cursor < end_ms:
                resp = http.get(
                    f"https://{host}/api/v3/klines",
                    params={
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms - 1,
                        "limit": KLINE_PAGE_LIMIT,
                    },
                )
                resp.raise_for_status()
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
    "DISCLAIMER",
    "MIN_ENTRY_MATCH_RATE",
    "MIN_FORWARD_DAYS",
    "MIN_FORWARD_TRADES",
    "RETURN_DIFF_ABS",
    "RETURN_DIFF_REL",
    "ForwardTrade",
    "backtest_same_period",
    "backtest_stats",
    "compare",
    "forward_stats",
    "match_entries",
    "public_kline_loader",
    "read_dryrun_trades",
    "render_comparison_markdown",
    "write_comparison",
]
