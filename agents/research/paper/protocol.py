"""Pre-registered paper-trade protocol (PLAN.md Phase 13; research report section 4).

Before the first freqtrade ``--dry-run`` trade, ``paper_trade.py prepare`` freezes what the paper
bot must show, so "consistent with the backtest" is a number and "failed" is declared on
schedule. ``compare`` judges the bot against that frozen copy only.

Every expectation comes from the lab backtest of the plan on the **design window only**
(:func:`design_stats`: the lab's own split helpers; bars at or after the lab's locked split are
never loaded); nothing is recorded in the ledger. The plan must already be in the ledger as a
research-run design-window trial (:func:`ledger_link`, read through ``Ledger.open_readonly``).
The lab's behaviour family (:func:`lab.research.family.behaviour_family`: the same logic, or
design-window returns correlated >= the lab's family threshold, e.g. a no-op-clause twin) decides
what counts as the same hypothesis: a family whose locked-split check did not pass is refused,
and so is a family member of a plan already registered for paper trading. Whether the family's
check passed or was never run is frozen (``locked_split_check``); without a passed check a paper
pass is never a success candidate. The claimed Sharpe
is the plan's walk-forward out-of-sample Sharpe from the ledger, or an owner-supplied value
labelled as such; never the design-window Sharpe of a selected plan. Formulas (all Sharpes per
bar unless named ``annual``; annualisation uses 365 days x bars/day):

* **Clock.** ``start`` = first bar boundary at or after registration. Decision horizon
  ``D = clamp(T_dec, min_days, max_days)`` days (``T_dec`` = ``max_days`` when the Sharpe cannot
  be tested), rounded up to whole bars.
* **Data.** The candle source (public API host or the lab archive) and the warm-up length are
  frozen. A decision-bar verdict needs every bar time in ``[start, decision_end)`` and a candle
  for every paper fill. With data missing at the decision bar a rule still fails when it fails
  whatever the missing bars hold; otherwise ``continue``, and ``data_grace_days`` after the
  decision bar a final ``inconclusive`` ("data"). The first final verdict is written once.
* **Entries.** Design rate ``r = n_design_trades / design_days``; expected count
  ``lambda = r * D``. Band = the union of the Poisson ``[F^-1(a/2; lambda), F^-1(1 - a/2;
  lambda)]``, the empirical ``a/2`` / ``1 - a/2`` quantiles of rolling ``D``-day design blocks
  (started every ``block_step_days``), and a predictive interval ``mean +/- t_{a/2, m-1} * sd *
  sqrt(1 + 1/m)``. Rolling blocks overlap, so they hold only ``m = floor(design_days / D)``
  independent blocks: mean and sd come from every block (the marginal distribution), the
  t-quantile and ``1/m`` from ``m``. That widening is what accounts for the overlap.
* **Exposure.** Share of bars with a position at bar close; blocks are scored as if flat at
  their start (the bot starts flat). Band = empirical quantiles united with the same predictive
  interval, widened by ``slack``, clipped to [0, 1].
* **Timing.** The lab engine replays the plan on the same candles (frozen warm-up, flat at
  ``start``, no look-ahead). Paper entries are matched one-to-one to lab entries, and paper
  exits of EVERY reason to lab exits of the same kind (signal / stop-loss / take-profit), within
  ``+/- match_tolerance_bars``. The generated bot runs the lab's own ``compile_plan`` on closed
  candles from the same frozen first bar as the replay (``data.warm_from``, rendered into the
  strategy as ``WARM_FROM``), so by construction an honest bot fills on the lab's fill bars
  unless the two candle feeds differ. ONE joint count of irregular
  fills (unmatched paper entries + unmatched lab entries + unmatched paper exits + unmatched lab
  exits + matched fills off the lab's bar) must stay within ``max(1, floor(irregular_share *
  (all lab fills + all paper fills)))``. A fill shifted beyond the tolerance is unmatched on
  both sides, so it costs more than a 1-bar shift (the rule is monotone in the shift). LATE
  paper fills mean the backtest acted earlier than live could (possible look-ahead in the lab);
  EARLY ones that the bot acted before the bar closed (the still-forming candle). Settled
  unmatched paper entries above the most the decision bar could allow fail at once.
* **Implementation shortfall** on lag-0 entries and signal exits only (their lab fill is the
  next bar's open; stop / take-profit fills are at a level, so their price is not judged):
  entry ``(paper / (open_j (1 + s_j)) - 1) * 1e4``, signal exit
  ``(1 - paper / (open_j (1 - s_j))) * 1e4``, plus ``(paper_fee - lab_fee) * 1e4``. Positive =
  worse than modelled. The median of all judged fills at the decision bar must lie in
  ``[-gross, tol]`` (``tol = tolerance_mult * slippage * 1e4``, ``gross =
  gross_breakage_multiple * tol``): above ``tol`` is drift, below ``-gross`` is implausibly
  favourable (possible look-ahead). Weekly medians are alerts; a week, or any ``min_fills``
  consecutive judged fills, with a median above ``gross`` fails at once. False-fail arithmetic
  (normal approximation of the median): an honest bot has mean ``mu = -slippage * 1e4`` and
  assumed noise ``sigma = fill_noise_bps``; ``median ~ N(mu, (pi/2) sigma^2 / n)``.
* **Honest false-fail: a single-path estimate.** Leave-overlap-out seeded bootstrap: ``draws``
  design blocks at random starts are judged as pseudo forward periods (entries, flat-start
  exposure, simulated honest fills, PSR when tested) against bands rebuilt without the design
  blocks that overlap each one. It comes from ONE design path, so it can be off in either
  direction (on a stationary random walk the true honest false-fail ranged about
  :data:`SIMULATED_FALSE_FAIL_RANGE` across design paths: e.g. an estimate of 1.8% where the
  truth was 10%, 5.9% where it was 0.8%) and excludes regime change. It is a caveat, never a
  floor or a rate.
* **Stopping rule.** Claimed Sharpe ``SR`` (walk-forward OOS or owner-supplied, annual /
  sqrt(bars_per_year)), design skew ``g3`` and raw kurtosis ``g4``. MinTRL from
  ``lab.stats.sharpe`` (Bailey & Lopez de Prado 2012 eq. 13) at ``confidence`` against
  ``SR* = benchmark_annual / sqrt(bars_per_year)``, and at ``Phi(z_c + z_p)`` for ``power``. If
  that length exceeds ``max_days`` the test checks implementation only. A Sharpe pass means
  "SR > SR* at the PSR confidence"; the claimed Sharpe itself is never confirmed.

Verdicts (:func:`evaluate`): ``fail`` on any breach (labelled a regime difference when only
entries/exposure are off while every matched fill was on time, else an implementation
difference), ``continue`` before the decision bar or while data may still arrive,
``inconclusive`` at the decision bar when every check is inside tolerance but the protocol
cannot discriminate (or the data never completed), ``pass`` otherwise. A pass with
``locked_split_check = "unchecked"`` says "implementation consistent, but NOT a success
candidate". Results are historical, not forward-looking. Paper trading only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from lab.config import LabConfig
from lab.engine import run_signals
from lab.engine.splits import assert_no_holdout, design_view, slice_window
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan, plan_hash
from lab.research.family import (
    Loader,
    behaviour_family,
    evidence_in_family,
    family_holdout_conflict,
    plan_logic,
)
from lab.research.logic import logic_hash
from lab.runner import CandleLoader, plan_design_window
from lab.stats.metrics import sharpe_per_bar
from lab.stats.sharpe import (
    min_track_record_length_from_moments,
    probabilistic_sharpe,
)
from lab.types import CostModel, Plan, bars_per_year, interval_seconds
from research.journal import guard_holdout, parse_frontmatter
from research.paper.freqtrade import check_candidate_name, startup_candle_count

AGENTS_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL_CONFIG = AGENTS_DIR / "config" / "paper_protocol.toml"
DEFAULT_CANDLE_HOST = "data-api.binance.vision"
PROTOCOL_VERSION = 7  # v7: START entry gate in the rules; v6: data.warm_from; v5: irregular
PROTOCOL_FILE = "protocol.json"
SECONDS_PER_DAY = 86_400
WEEK_SECONDS = 7 * SECONDS_PER_DAY
# Ledger schema with everything read here: costs_json (v2), research_runs/evidence (v3),
# holdout_checks.logic_hash (v4). v5 only adds trial_chain, which is not read.
LEDGER_MIN_SCHEMA = 4
VERDICTS_HEADING = "## Verdicts (appended by `paper_trade.py compare`)"
PROTOCOL_HEADING = "## Registered protocol (frozen before the first dry-run trade)"

Mode = Literal["sharpe_and_implementation", "implementation_only"]
VerdictName = Literal["pass", "fail", "continue", "inconclusive"]
ClaimSource = Literal["walk_forward_oos", "owner_supplied"]
CandleSource = Literal["api", "archive"]
CheckStatus = Literal["ok", "fail", "pending"]
CheckKind = Literal["regime", "implementation", "performance"]
FINAL_VERDICTS: frozenset[str] = frozenset({"pass", "fail", "inconclusive"})
LockedSplitCheck = Literal[
    "passed", "unchecked", "family_checked_not_this_plan", "passed_other_ledger"
]
LOCKED_SPLIT_STATES: tuple[str, ...] = (
    "passed",
    "unchecked",
    "family_checked_not_this_plan",
    "passed_other_ledger",
)
# The reviewer's simulation (Phase 13 final quant review, p13r3/band.py): the protocol built on
# ten seeded stationary 4h random-walk design paths and judged on 120 honest forward paths each
# had a true honest false-fail between about 0% and 10%, while the single-path bootstrap estimate
# was off in either direction (1.8% vs 10%; 5.9% vs 0.8%). Printed beside every estimate.
SIMULATED_FALSE_FAIL_RANGE: tuple[float, float] = (0.0, 0.10)
LATE_LABEL = "the backtest acted earlier than live: possible look-ahead in the lab"
EARLY_LABEL = "the bot acted before the bar closed: possible use of the still-forming candle"
UNMATCHED_PAPER_LABEL = (
    "the bot traded where the lab did not (or more than the match tolerance off its bar): it is "
    "not running the plan as backtested"
)
UNMATCHED_LAB_LABEL = (
    "the lab traded where the bot did not (a skipped or far-shifted trade): the backtest "
    "includes trades live did not make"
)


class ProtocolChangedError(ValueError):
    """protocol.json no longer matches the sha256 registered in the journal entry."""


class FinalVerdictRecordedError(RuntimeError):
    """The journal entry already holds a final verdict; it is never changed."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ProtocolConfig:
    min_days: float
    max_days: float
    data_grace_days: float
    confidence: float
    power: float
    benchmark_annual: float
    trades_band_confidence: float
    trades_min_expected: int
    exposure_band_confidence: float
    exposure_slack: float
    max_band_width: float
    shortfall_tolerance_mult: float
    shortfall_min_fills: int
    fill_noise_bps: float
    gross_breakage_multiple: float
    match_tolerance_bars: int
    irregular_share: float
    block_step_days: float
    min_blocks: int
    min_independent_blocks: int
    bootstrap_draws: int
    bootstrap_seed: int
    config_sha256: str


def _num(raw: Mapping[str, Any], key: str, where: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"[{where}] {key} must be a finite number, got {value!r}")
    return float(value)


def _int(raw: Mapping[str, Any], key: str, where: str, low: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ValueError(f"[{where}] {key} must be an integer >= {low}, got {value!r}")
    return value


def _open_unit(value: float, name: str) -> float:
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must lie strictly between 0 and 1, got {value}")
    return value


def load_protocol_config(path: Path | str = DEFAULT_PROTOCOL_CONFIG) -> ProtocolConfig:
    """Parse and validate ``paper_protocol.toml``; ``config_sha256`` is the file's sha256."""
    data = Path(path).read_bytes()
    raw = tomllib.loads(data.decode("utf-8"))

    def sec(name: str) -> Mapping[str, Any]:
        value = raw.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: missing [{name}] section")
        return value

    dur, shp, trd = sec("duration"), sec("sharpe"), sec("trades")
    exp, sft, des = sec("exposure"), sec("shortfall"), sec("design")
    mat, boot = sec("matching"), sec("bootstrap")
    min_days, max_days = _num(dur, "min_days", "duration"), _num(dur, "max_days", "duration")
    if not 0 < min_days <= max_days:
        raise ValueError(f"[duration] need 0 < min_days <= max_days, got {min_days}/{max_days}")
    grace = _num(dur, "data_grace_days", "duration")
    if grace < 0:
        raise ValueError(f"[duration] data_grace_days must be >= 0, got {grace}")
    power = _num(shp, "power", "sharpe")
    if not 0.5 <= power < 1.0:
        raise ValueError(f"[sharpe] power must lie in [0.5, 1), got {power}")
    slack = _num(exp, "slack", "exposure")
    if not 0.0 <= slack < 1.0:
        raise ValueError(f"[exposure] slack must lie in [0, 1), got {slack}")
    width = _num(exp, "max_band_width", "exposure")
    if not 0.0 < width <= 1.0:
        raise ValueError(f"[exposure] max_band_width must lie in (0, 1], got {width}")
    mult = _num(sft, "tolerance_mult", "shortfall")
    if mult < 0:
        raise ValueError(f"[shortfall] tolerance_mult must be >= 0, got {mult}")
    noise = _num(sft, "fill_noise_bps", "shortfall")
    if noise <= 0:
        raise ValueError(f"[shortfall] fill_noise_bps must be > 0, got {noise}")
    gross = _num(sft, "gross_breakage_multiple", "shortfall")
    if gross <= 1.0:
        raise ValueError(f"[shortfall] gross_breakage_multiple must be > 1, got {gross}")
    irregular = _num(mat, "irregular_share", "matching")
    if not 0.0 <= irregular < 0.5:
        raise ValueError(f"[matching] irregular_share must lie in [0, 0.5), got {irregular}")
    step = _num(des, "block_step_days", "design")
    if step <= 0:
        raise ValueError(f"[design] block_step_days must be > 0, got {step}")
    return ProtocolConfig(
        min_days=min_days,
        max_days=max_days,
        data_grace_days=grace,
        confidence=_open_unit(_num(shp, "confidence", "sharpe"), "[sharpe] confidence"),
        power=power,
        benchmark_annual=_num(shp, "benchmark_annual", "sharpe"),
        trades_band_confidence=_open_unit(
            _num(trd, "band_confidence", "trades"), "[trades] band_confidence"
        ),
        trades_min_expected=_int(trd, "min_expected", "trades", 0),
        exposure_band_confidence=_open_unit(
            _num(exp, "band_confidence", "exposure"), "[exposure] band_confidence"
        ),
        exposure_slack=slack,
        max_band_width=width,
        shortfall_tolerance_mult=mult,
        shortfall_min_fills=_int(sft, "min_fills", "shortfall", 1),
        fill_noise_bps=noise,
        gross_breakage_multiple=gross,
        match_tolerance_bars=_int(mat, "match_tolerance_bars", "matching", 0),
        irregular_share=irregular,
        block_step_days=step,
        min_blocks=_int(des, "min_blocks", "design", 2),
        min_independent_blocks=_int(des, "min_independent_blocks", "design", 2),
        bootstrap_draws=_int(boot, "draws", "bootstrap", 100),
        bootstrap_seed=_int(boot, "seed", "bootstrap", 0),
        config_sha256=hashlib.sha256(data).hexdigest(),
    )


# --------------------------------------------------------------------------- design window


@dataclass(frozen=True)
class DesignStats:
    """The plan's lab backtest on the design window: per-bar net returns and position at each
    bar's close (aligned on ``index``), the entry fill times of its trades, and the share of
    closed trades that left on an exit signal (the exits whose shortfall is judged)."""

    interval: str
    index: pd.DatetimeIndex
    returns: np.ndarray
    position: np.ndarray
    entry_times: pd.DatetimeIndex
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    signal_exit_share: float = 1.0

    def __post_init__(self) -> None:
        n = len(self.index)
        if self.returns.shape != (n,) or self.position.shape != (n,):
            raise ValueError("returns and position must align with index")
        if n < 2:
            raise ValueError("the design window needs at least 2 bars")
        if not 0.0 <= self.signal_exit_share <= 1.0:
            raise ValueError(f"signal_exit_share must lie in [0, 1], got {self.signal_exit_share}")


def design_stats(
    plan: Mapping[str, Any] | Plan, cfg: LabConfig, loader: CandleLoader
) -> DesignStats:
    """Run ``plan`` on its design window exactly as ``lab.runner`` scores it (signals on the
    history through the window end, engine on the window slice, lab costs, vol-scaled slippage
    warmed up on the same history). Candles are loaded with ``end = holdout_start`` and
    ``assert_no_holdout`` guards the frame, so no bar at or after the locked split is read.
    Nothing is recorded in the ledger."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    raw = loader(p.symbol, p.interval, cfg.splits.holdout_start)
    history = design_view(raw, cfg.splits)
    assert_no_holdout(history, cfg.splits)
    window, _ = plan_design_window(p, history, cfg)
    candles = slice_window(history, window)
    history = history.loc[history.index < window.end]
    sig = compile_plan(p, history)
    result = run_signals(
        candles,
        sig.entries.loc[candles.index],
        sig.exits.loc[candles.index],
        costs=cfg.costs,
        risk=p.risk,
        init_cash=cfg.init_cash,
        cost_history=history,
    )
    closed = [t for t in result.trades if t.exit_time is not None]
    share = sum(t.exit_reason == "signal" for t in closed) / len(closed) if closed else 1.0
    return DesignStats(
        interval=p.interval,
        index=pd.DatetimeIndex(candles.index),
        returns=result.returns.to_numpy(dtype=float),
        position=result.position.to_numpy(dtype=float),
        entry_times=pd.DatetimeIndex([t.entry_time for t in result.trades]),
        window_start=pd.Timestamp(window.start),
        window_end=pd.Timestamp(window.end),
        signal_exit_share=float(share),
    )


@dataclass(frozen=True)
class _Blocks:
    """Per-block bar ranges ``[lo, hi)``, entry counts and flat-start exposures."""

    lo: np.ndarray
    hi: np.ndarray
    counts: np.ndarray
    exposures: np.ndarray


def _block_stats(design: DesignStats, starts: np.ndarray, block_seconds: int) -> _Blocks:
    """Blocks ``[t, t + block)`` for each start ``t`` (UNIX seconds). A held bar counts only
    when the position holding it was entered at or after ``t`` (scored flat at the start)."""
    idx = design.index.as_unit("s").asi8
    entries = np.sort(design.entry_times.as_unit("s").asi8)
    held = design.position != 0
    # entry time of the position held at each bar (the latest entry at or before the bar);
    # no pyramiding, so that is the trade holding it
    owner = np.searchsorted(entries, idx, side="right") - 1
    owner_entry = np.where(owner >= 0, entries[np.clip(owner, 0, None)], np.iinfo(np.int64).min)
    starts = np.asarray(starts, dtype=np.int64)
    lo = np.searchsorted(idx, starts, side="left")
    hi = np.searchsorted(idx, starts + block_seconds, side="left")
    e_lo = np.searchsorted(entries, starts, side="left")
    e_hi = np.searchsorted(entries, starts + block_seconds, side="left")
    exposures = np.zeros(len(starts))
    for k, (a, b, t) in enumerate(zip(lo, hi, starts, strict=True)):
        if b > a:
            exposures[k] = float((held[a:b] & (owner_entry[a:b] >= t)).mean())
    return _Blocks(lo, hi, (e_hi - e_lo).astype(float), exposures)


def design_blocks(
    design: DesignStats, *, block_seconds: int, step_seconds: int
) -> tuple[np.ndarray, np.ndarray]:
    """Entry counts and exposures of rolling ``[t, t + block)`` blocks inside the design window,
    ``t = window_start + k * step``; only blocks that end inside the window (and hold bars).

    Each block is scored as if flat at its start, like the paper bot: a held bar counts only
    when the position holding it was entered at or after ``t`` (the position open at the
    block's first bar is ignored until it exits)."""
    if block_seconds <= 0 or step_seconds <= 0:
        raise ValueError("block_seconds and step_seconds must be > 0")
    t0 = int(design.window_start.timestamp())
    t_end = int(design.window_end.timestamp())
    starts = np.arange(t0, t_end - block_seconds + 1, step_seconds, dtype=np.int64)
    b = _block_stats(design, starts, block_seconds)
    keep = b.hi > b.lo
    return b.counts[keep], b.exposures[keep]


def predictive_band(values: np.ndarray, independent: int, confidence: float) -> tuple[float, float]:
    """Two-sided ``confidence`` prediction interval for ONE new block: ``mean +/- t_{a/2,
    m-1} * sd * sqrt(1 + 1/m)`` with the mean and sd (ddof=1) of every (overlapping) block and
    ``m = independent`` non-overlapping blocks for the t-quantile and the ``1/m`` term."""
    if independent < 2:
        raise ValueError(f"need at least 2 independent blocks, got {independent}")
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    t = float(sp_stats.t.ppf(1.0 - (1.0 - confidence) / 2.0, independent - 1))
    half = t * sd * math.sqrt(1.0 + 1.0 / independent)
    return mean - half, mean + half


def entry_band(
    counts: np.ndarray, independent: int, poisson: tuple[int, int], confidence: float
) -> tuple[int, int, int, int, int, int]:
    """``(lo, hi, empirical_lo, empirical_hi, predictive_lo, predictive_hi)``: the union of the
    Poisson band, the empirical ``a/2`` / ``1 - a/2`` block quantiles (``lower`` / ``higher``)
    and the predictive interval (floor / ceil, at least 0)."""
    a = 1.0 - confidence
    e_lo = int(np.quantile(counts, a / 2.0, method="lower"))
    e_hi = int(np.quantile(counts, 1.0 - a / 2.0, method="higher"))
    c_lo, c_hi = predictive_band(counts, independent, confidence)
    pr_lo, pr_hi = max(0, math.floor(c_lo)), max(0, math.ceil(c_hi))
    return (
        min(poisson[0], e_lo, pr_lo),
        max(poisson[1], e_hi, pr_hi),
        e_lo,
        e_hi,
        pr_lo,
        pr_hi,
    )


def exposure_band(
    exposures: np.ndarray, independent: int, confidence: float, slack: float
) -> tuple[float, float, float, float, float, float]:
    """``(lo, hi, empirical_lo, empirical_hi, predictive_lo, predictive_hi)``: the empirical
    quantiles united with the predictive interval, widened by ``slack``, clipped to [0, 1]."""
    a = 1.0 - confidence
    x_lo = float(np.quantile(exposures, a / 2.0, method="lower"))
    x_hi = float(np.quantile(exposures, 1.0 - a / 2.0, method="higher"))
    q_lo, q_hi = predictive_band(exposures, independent, confidence)
    lo = max(0.0, min(x_lo, q_lo) - slack)
    hi = min(1.0, max(x_hi, q_hi) + slack)
    return lo, hi, x_lo, x_hi, q_lo, q_hi


def poisson_band(mean: float, confidence: float) -> tuple[int, int]:
    """Two-sided ``confidence`` interval of a Poisson count with ``mean``:
    ``[F^-1(a/2), F^-1(1 - a/2)]`` (smallest k with CDF >= q), ``a = 1 - confidence``."""
    if not math.isfinite(mean) or mean < 0:
        raise ValueError(f"mean must be finite and >= 0, got {mean}")
    if mean == 0:
        return 0, 0
    a = 1.0 - confidence
    lo = float(sp_stats.poisson.ppf(a / 2.0, mean))
    hi = float(sp_stats.poisson.ppf(1.0 - a / 2.0, mean))
    return int(lo), int(hi)


def sharpe_moments(returns: np.ndarray) -> tuple[float, float, float]:
    """Per-bar ``(SR, skew, raw kurtosis)`` with the lab's conventions (SR with ddof=1; biased
    sample skew and kurtosis, as ``lab.stats.sharpe``)."""
    arr = np.asarray(returns, dtype=float)
    sr = sharpe_per_bar(arr)
    if not math.isfinite(sr):
        return float("nan"), float("nan"), float("nan")
    return sr, float(sp_stats.skew(arr)), float(sp_stats.kurtosis(arr, fisher=False))


def min_testable_sharpe(
    skew: float, kurtosis: float, benchmark: float, confidence: float, max_bars: float
) -> float | None:
    """Smallest per-bar Sharpe whose track record ``min_track_record_length_from_moments(SR,
    skew, kurtosis, benchmark, confidence)`` fits in ``max_bars`` (geometric scan, then
    bisection); None when no Sharpe up to ``benchmark + 5`` fits or the moments are undefined."""
    if not all(math.isfinite(v) for v in (skew, kurtosis, benchmark, max_bars)):
        return None

    def fits(d: float) -> bool:
        n = min_track_record_length_from_moments(
            benchmark + d, skew, kurtosis, benchmark, confidence
        )
        return math.isfinite(n) and n <= max_bars

    prev = 0.0
    for d in np.geomspace(1e-6, 5.0, 400):
        if fits(float(d)):
            lo, hi = prev, float(d)
            for _ in range(100):
                mid = (lo + hi) / 2.0
                lo, hi = (lo, mid) if fits(mid) else (mid, hi)
            return benchmark + hi
        prev = float(d)
    return None


def median_bounds(values: Sequence[float], unknown: int) -> tuple[float, float]:
    """The smallest and largest median ``values`` can have once ``unknown`` more values of any
    size are added (``(nan, nan)`` with no values at all)."""
    vals = sorted(float(v) for v in values)
    if not vals and unknown == 0:
        return math.nan, math.nan
    lo = [-math.inf] * unknown + vals
    hi = vals + [math.inf] * unknown
    return float(np.median(lo)), float(np.median(hi))


def gross_fill_windows(
    fills: Sequence[Fill], *, size: int, gross_bps: float
) -> list[tuple[int, float]]:
    """``(start index, median)`` of every rolling window of ``size`` consecutive ``fills``
    (in the given order) whose median shortfall exceeds ``gross_bps``. Fills already observed
    never change, so a breached window stays breached."""
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    vals = [f.shortfall_bps for f in fills]
    out: list[tuple[int, float]] = []
    for i in range(len(vals) - size + 1):
        med = float(np.median(vals[i : i + size]))
        if med > gross_bps:
            out.append((i, med))
    return out


# --------------------------------------------------------------------------- ledger link


@dataclass(frozen=True)
class LedgerLink:
    """Where the candidate sits in the lab ledger: its newest research-run design-window trial,
    every walk-forward result recorded for its ``plan_hash`` (annual OOS Sharpes), whether its
    behaviour family's locked-split check passed or was never run, and the family itself (the
    lab's :class:`lab.research.family.FamilyInfo`, as hashes) for the duplicate check."""

    run_id: str
    trial_id: int
    window_start: int  # UNIX seconds of that trial's design window
    window_end: int
    wf_oos_sharpes: tuple[float | None, ...]
    wf_evidence_ids: tuple[int, ...]
    wf_run_ids: tuple[str, ...]
    locked_split_check: LockedSplitCheck = "unchecked"
    family_logic_hashes: tuple[str, ...] = ()
    family_plan_hashes: tuple[str, ...] = ()
    family_uncertain: tuple[str, ...] = ()  # same-feature logics not comparable (fail closed)
    ledger_path: str = ""  # the ledger read, resolved
    ledger_is_default: bool = False  # it is the lab's own ledger (cfg.ledger_path)
    chain_head: str | None = None  # stored hash-chain head of run_id (None: not chained)


def _family_checks(ledger: Ledger, family: Any) -> list[dict[str, Any]]:
    """Every spent locked-split check (``Ledger.holdout_checked``) of a member of ``family``
    or of an uncomparable same-feature plan (fail closed), by the lab's own membership rule
    (:func:`lab.research.family.evidence_in_family`: plan hash, stored or recomputed logic),
    each with ``passed``: True / False / None (outcome not recorded)."""
    out: list[dict[str, Any]] = []
    for check in ledger.holdout_checked():
        plan_json = check.get("plan_json")
        try:
            plan_obj = json.loads(plan_json) if isinstance(plan_json, str) else None
        except json.JSONDecodeError:
            plan_obj = None
        payload = {"logic_hash": check.get("logic_hash"), "plan": plan_obj}
        logics = {x for x in (check.get("logic_hash"), plan_logic(plan_json)) if x}
        member = evidence_in_family(family, str(check["plan_hash"]), payload)
        if not member and not logics & set(family.uncertain):
            continue
        outcomes = [
            row["payload"].get("passed")
            for row in ledger.evidence(
                check["run_id"], plan_hash=check["plan_hash"], kind="holdout"
            )
            if isinstance(row["payload"], Mapping)
        ]
        passed = None if not outcomes else all(o is True for o in outcomes)
        out.append({**check, "member": member, "passed": passed})
    return out


def ledger_link(
    ledger: Ledger,
    plan: Mapping[str, Any] | Plan,
    *,
    cfg: LabConfig,
    loader: Loader | None = None,
) -> LedgerLink:
    """Lookup of ``plan`` in ``ledger`` (read calls only): the newest ok design-window trial of
    its ``plan_hash`` in a RESEARCH run (a manual or web-UI backtest does not count), and the
    walk-forward evidence of that hash in every research run.

    The hypothesis is the plan's behaviour family, computed by the lab itself
    (:func:`lab.research.family.behaviour_family` with ``cfg``; ``loader`` rebuilds bar times
    for comparisons across intervals or windows): its logic on any interval, plus every logic
    whose submitted design-window returns correlate >= ``cfg.research.family_return_corr`` (a
    no-op clause or a stop that never triggers trades the same). ``ValueError`` when there is
    no research trial, or when the family is a dead hypothesis:
    :func:`lab.research.family.family_holdout_conflict` finds a spent locked-split check of a
    family member (or of an uncomparable same-feature plan: fail closed) and not every recorded
    family outcome passed, or the outcome cannot be tied to the family. Otherwise the frozen
    ``locked_split_check`` is ``"passed"`` only when the one check in the family is THIS
    plan's own and passed, and ``ledger`` is the lab's own ledger (``cfg.ledger_path``; any
    other file gives ``"passed_other_ledger"``); ``"family_checked_not_this_plan"`` when another
    family member (or an uncomparable same-feature plan) was checked, so this variant can never
    be; ``"unchecked"`` when none was. Every state but ``"passed"`` makes a paper pass "NOT a
    success candidate". Only pass/fail is read, never a metric."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    h = plan_hash(p)
    family = behaviour_family(ledger, symbol=p.symbol, plan_hash=h, cfg=cfg, plan=p, loader=loader)
    status: LockedSplitCheck = "unchecked"
    conflict = family_holdout_conflict(ledger, family)
    checks = _family_checks(ledger, family)
    if conflict is not None or checks:
        if not checks or any(c["passed"] is not True for c in checks):
            what = (conflict or "a plan of this behaviour family was checked").replace(
                "holdout-checked", "locked-split-checked"
            )
            raise ValueError(
                f"{what}: this plan is that hypothesis, and its one locked-split check did not "
                "pass (or its outcome is not recorded, or belongs to a plan whose returns could "
                "not be compared: fail closed); a dead hypothesis is never paper-registered or "
                "re-tuned"
            )
        # passed only for THIS plan's own check with no other family (or uncertain) check:
        # the lab marks any other variant's row FAIL ("can never be checked")
        if all(c["plan_hash"] == h for c in checks):
            status = "passed"
        else:
            status = "family_checked_not_this_plan"
    resolved = Path(ledger.path).resolve() if ledger.path is not None else None
    is_default = resolved is not None and resolved == Path(cfg.ledger_path).resolve()
    if status == "passed" and not is_default:
        status = "passed_other_ledger"  # a --ledger file is not the lab's record
    base = ledger.latest_research_base_trial(h)
    if base is None or base.id is None or base.interval != p.interval:
        raise ValueError(
            f"plan {h[:12]} ({p.symbol} {p.interval}) has no design-window trial in a research "
            "run of the ledger: backtest it through the strategy-lab research tools "
            "(start_run, run_backtest) first; a manual or web-UI backtest does not count"
        )
    sharpes: list[float | None] = []
    ids: list[int] = []
    runs: list[str] = []
    for row in ledger.evidence(None, plan_hash=h, kind="walk_forward"):
        payload = row["payload"]
        oos = payload.get("oos_metrics") if isinstance(payload, Mapping) else None
        value = oos.get("sharpe") if isinstance(oos, Mapping) else None
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        sharpes.append(float(value) if ok and math.isfinite(float(value)) else None)
        ids.append(int(row["id"]))
        runs.append(str(row["run_id"]))
    return LedgerLink(
        run_id=base.run_id,
        trial_id=int(base.id),
        window_start=int(base.window_start.timestamp()),
        window_end=int(base.window_end.timestamp()),
        wf_oos_sharpes=tuple(sharpes),
        wf_evidence_ids=tuple(ids),
        wf_run_ids=tuple(runs),
        locked_split_check=status,
        family_logic_hashes=tuple(sorted(family.logic_hashes)),
        family_plan_hashes=tuple(sorted(family.plan_hashes)),
        family_uncertain=tuple(sorted(family.uncertain)),
        ledger_path=str(resolved) if resolved is not None else "",
        ledger_is_default=is_default,
        chain_head=_chain_head(ledger, base.run_id),
    )


def _chain_head(ledger: Ledger, run_id: str) -> str | None:
    """``Ledger.chain_head(run_id)`` (stored, unverified), or None on a ledger without the
    hash chain (schema < 5)."""
    try:
        return ledger.chain_head(run_id)
    except (sqlite3.Error, ValueError):
        return None


def read_ledger_link(
    path: Path | str,
    plan: Mapping[str, Any] | Plan,
    *,
    cfg: LabConfig,
    loader: Loader | None = None,
) -> LedgerLink:
    """:func:`ledger_link` on the ledger at ``path`` opened with ``Ledger.open_readonly``
    (SQLite ``mode=ro``: no migrations, no hash-chain backfill, never creates the file) at
    schema >= :data:`LEDGER_MIN_SCHEMA`. ``ValueError`` for a missing file or an older
    schema."""
    with Ledger.open_readonly(path, min_schema_version=LEDGER_MIN_SCHEMA) as ledger:
        return ledger_link(ledger, plan, cfg=cfg, loader=loader)


def resolve_claim(
    link: LedgerLink, claimed_sharpe_annual: float | None
) -> tuple[float, ClaimSource, str]:
    """The claimed annual Sharpe, its source and a note. An owner value wins and is labelled
    ``owner_supplied``; otherwise the MINIMUM walk-forward OOS Sharpe over this ``plan_hash``'s
    own results in the ledger (the lab's ``wf_oos_sharpe_min`` criterion takes the minimum over
    the plan's whole behaviour family, which can be lower). ``ValueError`` without either: the
    design-window Sharpe of a selected plan is never the claim."""
    wf = ", ".join("undefined" if v is None else f"{v:.3f}" for v in link.wf_oos_sharpes)
    if claimed_sharpe_annual is not None:
        if not math.isfinite(claimed_sharpe_annual):
            raise ValueError(f"claimed Sharpe must be finite, got {claimed_sharpe_annual}")
        note = "owner-supplied (--claimed-sharpe)"
        if link.wf_oos_sharpes:
            note += f"; ledger walk-forward OOS Sharpe(s) of this plan: {wf}"
        return float(claimed_sharpe_annual), "owner_supplied", note
    if not link.wf_oos_sharpes:
        raise ValueError(
            "no walk_forward evidence for this plan in the ledger: run walk_forward in the lab, "
            "or pass --claimed-sharpe (recorded as owner-supplied); the design-window Sharpe of "
            "a selected plan is never used as the claim"
        )
    if any(v is None for v in link.wf_oos_sharpes):
        raise ValueError(
            f"a walk_forward OOS Sharpe of this plan is undefined ({wf}); pass --claimed-sharpe "
            "(recorded as owner-supplied)"
        )
    values = [v for v in link.wf_oos_sharpes if v is not None]
    note = (
        f"minimum stitched walk-forward OOS Sharpe over this plan's own {len(values)} result(s) "
        f"in the ledger (evidence ids {', '.join(str(i) for i in link.wf_evidence_ids)}); the "
        "lab's wf_oos_sharpe_min criterion uses the whole behaviour family and can be lower"
    )
    return min(values), "walk_forward_oos", note


# --------------------------------------------------------------------------- protocol


@dataclass(frozen=True)
class SourceInfo:
    kind: str  # "design_window_backtest"
    window_start: int  # UNIX seconds
    window_end: int
    bars: int
    days: float
    run_id: str  # ledger research run of the candidate's design-window trial
    trial_id: int
    trial_window_start: int
    trial_window_end: int
    wf_evidence_ids: tuple[int, ...]
    ledger_path: str  # resolved path of the ledger read at registration
    ledger_is_default: bool  # it was the lab's own ledger (cfg.ledger_path)
    chain_head: str | None  # stored hash-chain head of run_id at registration (unverified)


@dataclass(frozen=True)
class DataRule:
    candle_source: CandleSource  # frozen: compare refuses another source
    candle_host: str  # public kline host for "api"
    warmup_bars: int  # history before start fed to the lab replay (freqtrade startup count)
    # UTC epoch seconds of the first bar BOTH the lab replay and the generated strategy compute
    # indicators from (start - warmup_bars bars); rendered into the strategy as WARM_FROM
    warm_from: int
    grace_days: float  # after the decision bar, missing data makes the verdict inconclusive


@dataclass(frozen=True)
class TradeBand:
    design_trades: int
    design_trades_per_day: float
    expected: float  # lambda over the decision horizon
    lo: int
    hi: int
    poisson_lo: int
    poisson_hi: int
    empirical_lo: int
    empirical_hi: int
    predictive_lo: int
    predictive_hi: int
    independent_blocks: int
    confidence: float
    min_expected: int
    weak: bool  # expected < min_expected: the count check says little


@dataclass(frozen=True)
class ExposureBand:
    expected: float
    lo: float
    hi: float
    empirical_lo: float
    empirical_hi: float
    predictive_lo: float
    predictive_hi: float
    slack: float
    confidence: float
    max_band_width: float


@dataclass(frozen=True)
class MatchRule:
    tolerance_bars: int
    irregular_share: float  # irregular fills allowed: max(1, floor(share x all fills))


def irregular_allowance(rule: MatchRule, fills: int) -> int:
    """Irregular fills allowed among ``fills`` (every lab fill plus every paper fill, entries
    and exits): ``max(1, floor(irregular_share * fills))``. The generated bot runs the lab's
    own ``compile_plan`` on the same closed candles from the same ``WARM_FROM`` as the lab
    replay, so an honest bot has none by construction; the one allowance covers a bar on which
    the exchange feed and the comparison's candle source disagree. Rounded DOWN: an unmatched
    trade is 2-4 irregular fills (its entry and exit, on one or both sides), and rounding up
    would excuse one at 21-40 fills."""
    return max(1, math.floor(rule.irregular_share * fills + 1e-9))


@dataclass(frozen=True)
class ShortfallRule:
    fee_rate: float  # lab cost model, frozen
    slippage_rate: float
    slippage_vol_mult: float
    slippage_cap: float
    modelled_fill_cost_bps: float  # per side, fee + base slippage
    round_trip_cost_bps: float
    tolerance_mult: float
    tolerance_bps: float  # adverse overall median shortfall allowed at the decision bar
    min_fills: int
    fill_noise_bps: float  # assumed sigma of one honest fill's shortfall
    gross_breakage_multiple: float
    gross_bps: float  # a week's / window's median above this fails at once
    honest_mean_bps: float  # mu = -slippage_rate * 1e4
    expected_fills: float  # judged fills expected over the horizon
    overall_n: int  # fill count the false-fail probability is stated at
    overall_sd_bps: float
    false_fail_overall: float  # P(median > tol)
    false_fail_favourable: float  # P(median vs the bar open < -gross)
    weekly_sd_bps: float  # at min_fills fills
    false_fail_gross_week: float
    weeks: int
    false_fail_gross_any_week: float
    windows_expected: int  # rolling windows of min_fills consecutive judged fills
    false_fail_gross_any_window: float  # union bound: min(1, windows x P)
    false_fail_gross_early: float  # union bound over weekly looks and fill windows


@dataclass(frozen=True)
class SharpeRule:
    claim_source: ClaimSource
    claim_note: str
    claimed_annual: float | None
    claimed_per_bar: float | None
    skew: float | None
    kurtosis: float | None  # raw (3 = normal)
    benchmark_annual: float
    benchmark_per_bar: float
    confidence: float
    power: float
    min_trl_bars: float | None  # None = infinite / undefined
    min_trl_days: float | None
    decision_trl_bars: float | None
    decision_trl_days: float | None
    min_testable_annual: float | None  # smallest claim the max duration can test
    testable: bool


@dataclass(frozen=True)
class BootstrapEstimate:
    """Honest false-fail, a SINGLE-PATH estimate: design blocks at seeded random starts judged
    as pseudo forward periods. One design path, so it can be off in either direction (see
    :data:`SIMULATED_FALSE_FAIL_RANGE`) and it excludes regime change; never a floor."""

    draws: int
    seed: int
    fail_rate: float  # any rule
    entries_rate: float
    exposure_rate: float
    shortfall_rate: float
    sharpe_rate: float


@dataclass(frozen=True)
class PaperProtocol:
    version: int
    candidate: str
    plan_hash: str
    logic_hash: str
    symbol: str
    interval: str
    created_at: str  # ISO-8601 UTC
    start: int  # UNIX seconds, first bar of the test
    decision_end: int  # UNIX seconds, end of the decision horizon
    decision_bars: int
    decision_days: float
    min_days: float
    max_days: float
    mode: Mode
    locked_split_check: LockedSplitCheck  # the behaviour family's lab check: passed / never run
    source: SourceInfo
    data: DataRule
    trades: TradeBand
    exposure: ExposureBand
    matching: MatchRule
    shortfall: ShortfallRule
    sharpe: SharpeRule
    bootstrap: BootstrapEstimate
    design_blocks: int
    inconclusive_reasons: tuple[str, ...]  # known upfront: the best verdict is inconclusive
    rules: tuple[str, ...]
    config_sha256: str


def _finite(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _ceil_to(ts: int, step: int) -> int:
    return -(-ts // step) * step


def _median_sd(sigma: float, n: int) -> float:
    """Normal approximation of the sample median's sd: ``sqrt(pi/2) * sigma / sqrt(n)``."""
    return math.sqrt(math.pi / 2.0) * sigma / math.sqrt(n)


def bootstrap_false_fail(
    design: DesignStats,
    *,
    decision_bars: int,
    block_step_seconds: int,
    independent: int,
    trades: TradeBand,
    exposure: ExposureBand,
    shortfall: ShortfallRule,
    sharpe: SharpeRule,
    draws: int,
    seed: int,
) -> BootstrapEstimate:
    """Leave-overlap-out bootstrap of the honest false-fail rate. ``draws`` design blocks of
    ``decision_bars`` bars at seeded uniform random starts are judged as the forward period of
    an honest bot, each against bands REBUILT without the rolling design blocks that overlap
    it (so the drawn block never helped set its own band; ``independent - 1`` independent
    blocks, the frozen Poisson part unchanged): the entry count and flat-start exposure,
    ``round(entries x (1 + signal_exit_share))`` honest fills drawn from ``N(mu,
    fill_noise_bps)`` against the shortfall rules (overall median in ``[-gross, tol]``, no
    ``min_fills``-fill window above ``gross``), and the block's returns against the PSR rule
    when the Sharpe is tested. Timing is exact for an honest bot. Still one design path, so it
    cannot see regimes the design window never had."""
    step = int((design.index[1] - design.index[0]).total_seconds())
    block = decision_bars * step
    idx = design.index.as_unit("s").asi8
    t0 = int(design.window_start.timestamp())
    t_end = int(design.window_end.timestamp())
    valid = idx[idx + block <= t_end]
    if valid.size == 0:
        raise ValueError("the design window is shorter than one decision horizon")
    grid = np.arange(t0, t_end - block + 1, block_step_seconds, dtype=np.int64)
    g = _block_stats(design, grid, block)
    keep = g.hi > g.lo
    grid, g_counts, g_exp = grid[keep], g.counts[keep], g.exposures[keep]
    rng = np.random.default_rng(seed)
    starts = rng.choice(valid, size=draws, replace=True)
    b = _block_stats(design, starts, block)
    poisson = (trades.poisson_lo, trades.poisson_hi)
    m = max(2, independent - 1)
    ent = np.zeros(draws, dtype=bool)
    expo = np.zeros(draws, dtype=bool)
    sf = np.zeros(draws, dtype=bool)
    shp = np.zeros(draws, dtype=bool)
    s = shortfall
    for k in range(draws):
        t = int(starts[k])
        out = (grid + block <= t) | (grid >= t + block)
        if int(out.sum()) >= 2:
            lo_n, hi_n = entry_band(g_counts[out], m, poisson, trades.confidence)[:2]
            lo_x, hi_x = exposure_band(g_exp[out], m, exposure.confidence, exposure.slack)[:2]
        else:
            lo_n, hi_n, lo_x, hi_x = trades.lo, trades.hi, exposure.lo, exposure.hi
        ent[k] = not lo_n <= b.counts[k] <= hi_n
        expo[k] = not lo_x <= b.exposures[k] <= hi_x
        n = round(float(b.counts[k]) * (1.0 + design.signal_exit_share))
        if n >= s.min_fills:
            vals = rng.normal(s.honest_mean_bps, s.fill_noise_bps, n)
            med = float(np.median(vals))
            windows = np.lib.stride_tricks.sliding_window_view(vals, s.min_fills)
            sf[k] = (
                med > s.tolerance_bps
                or med - s.honest_mean_bps < -s.gross_bps  # against the bar open
                or bool((np.median(windows, axis=1) > s.gross_bps).any())
            )
        if sharpe.testable:
            rets = design.returns[b.lo[k] : b.hi[k]]
            psr = probabilistic_sharpe(rets, sharpe.benchmark_per_bar) if rets.size >= 2 else 0.0
            shp[k] = not (math.isfinite(psr) and psr >= sharpe.confidence)
    any_fail = ent | expo | sf | shp
    return BootstrapEstimate(
        draws=draws,
        seed=seed,
        fail_rate=float(any_fail.mean()),
        entries_rate=float(ent.mean()),
        exposure_rate=float(expo.mean()),
        shortfall_rate=float(sf.mean()),
        sharpe_rate=float(shp.mean()),
    )


def build_protocol(
    *,
    candidate: str,
    plan: Mapping[str, Any] | Plan,
    design: DesignStats,
    costs: CostModel,
    config: ProtocolConfig,
    created_at: datetime,
    link: LedgerLink,
    claimed_sharpe_annual: float,
    claim_source: ClaimSource,
    claim_note: str,
    candle_source: CandleSource = "api",
    candle_host: str = DEFAULT_CANDLE_HOST,
) -> PaperProtocol:
    """Freeze the forward-test expectations of ``plan`` from its design-window ``design`` stats
    and the ledger ``link`` (see the module docstring for every formula). Pure: no I/O."""
    check_candidate_name(candidate)
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    if design.interval != p.interval:
        raise ValueError(f"design stats are {design.interval}, the plan is {p.interval}")
    if not math.isfinite(claimed_sharpe_annual):
        raise ValueError(f"claimed_sharpe_annual must be finite, got {claimed_sharpe_annual}")
    if link.locked_split_check not in LOCKED_SPLIT_STATES:
        raise ValueError(
            f"locked_split_check {link.locked_split_check!r} must be one of {LOCKED_SPLIT_STATES}"
        )
    if claim_source not in ("walk_forward_oos", "owner_supplied"):
        raise ValueError(f"claim_source {claim_source!r} must be walk_forward_oos/owner_supplied")
    if candle_source not in ("api", "archive"):
        raise ValueError(f"candle_source {candle_source!r} must be 'api' or 'archive'")
    step = interval_seconds(p.interval)
    bars_per_day = SECONDS_PER_DAY / step
    root_bpy = math.sqrt(bars_per_year(p.interval))

    # --- claimed Sharpe and the stopping rule (lab.stats, per bar)
    _, g3, g4 = sharpe_moments(design.returns)
    sr = claimed_sharpe_annual / root_bpy
    bench = config.benchmark_annual / root_bpy
    z = float(sp_stats.norm.ppf(config.confidence) + sp_stats.norm.ppf(config.power))
    conf_dec = float(sp_stats.norm.cdf(z))
    if all(math.isfinite(v) for v in (sr, g3, g4)):
        min_trl = min_track_record_length_from_moments(sr, g3, g4, bench, config.confidence)
        dec_trl = min_track_record_length_from_moments(sr, g3, g4, bench, conf_dec)
    else:
        min_trl = dec_trl = float("nan")
    min_testable = min_testable_sharpe(g3, g4, bench, conf_dec, config.max_days * bars_per_day)
    dec_trl_days = dec_trl / bars_per_day
    testable = math.isfinite(dec_trl) and dec_trl_days <= config.max_days
    target_days = max(dec_trl_days if testable else config.max_days, config.min_days)
    decision_bars = math.ceil(target_days * bars_per_day - 1e-9)
    decision_days = decision_bars / bars_per_day
    mode: Mode = "sharpe_and_implementation" if testable else "implementation_only"

    # --- clock
    created = created_at.astimezone(UTC)
    start = _ceil_to(math.ceil(created.timestamp()), step)
    decision_end = start + decision_bars * step

    # --- entries and exposure bands from the design window (blocks scored flat-start)
    w0, w1 = int(design.window_start.timestamp()), int(design.window_end.timestamp())
    design_days = (w1 - w0) / SECONDS_PER_DAY
    counts, exposures = design_blocks(
        design,
        block_seconds=decision_bars * step,
        step_seconds=round(config.block_step_days * SECONDS_PER_DAY),
    )
    independent = math.floor(design_days / decision_days + 1e-9)
    if counts.size < config.min_blocks or independent < config.min_independent_blocks:
        raise ValueError(
            f"the design window ({design_days:.0f} days) holds {counts.size} rolling and "
            f"{independent} non-overlapping blocks of {decision_days:.1f} days; the protocol "
            f"needs at least {config.min_blocks} ([design] min_blocks) and "
            f"{config.min_independent_blocks} ([design] min_independent_blocks)"
        )
    n_design = len(design.entry_times)
    rate = n_design / design_days
    expected = rate * decision_days
    p_lo, p_hi = poisson_band(expected, config.trades_band_confidence)
    t_lo, t_hi, e_lo, e_hi, pr_lo, pr_hi = entry_band(
        counts, independent, (p_lo, p_hi), config.trades_band_confidence
    )
    trades = TradeBand(
        design_trades=n_design,
        design_trades_per_day=rate,
        expected=expected,
        lo=t_lo,
        hi=t_hi,
        poisson_lo=p_lo,
        poisson_hi=p_hi,
        empirical_lo=e_lo,
        empirical_hi=e_hi,
        predictive_lo=pr_lo,
        predictive_hi=pr_hi,
        independent_blocks=independent,
        confidence=config.trades_band_confidence,
        min_expected=config.trades_min_expected,
        weak=expected < config.trades_min_expected,
    )
    x_band_lo, x_band_hi, x_lo, x_hi, q_lo, q_hi = exposure_band(
        exposures, independent, config.exposure_band_confidence, config.exposure_slack
    )
    exposure = ExposureBand(
        expected=float(np.mean(design.position != 0)),
        lo=x_band_lo,
        hi=x_band_hi,
        empirical_lo=x_lo,
        empirical_hi=x_hi,
        predictive_lo=q_lo,
        predictive_hi=q_hi,
        slack=config.exposure_slack,
        confidence=config.exposure_band_confidence,
        max_band_width=config.max_band_width,
    )

    # --- implementation shortfall against the lab's modelled fill, with its false-fail odds
    per_side = (costs.fee_rate + costs.slippage_rate) * 1e4
    tol = config.shortfall_tolerance_mult * costs.slippage_rate * 1e4
    mu = -costs.slippage_rate * 1e4
    sigma = config.fill_noise_bps
    expected_fills = expected * (1.0 + design.signal_exit_share)
    n_overall = max(1, math.floor(expected_fills))
    sd_overall = _median_sd(sigma, n_overall)
    gross = config.gross_breakage_multiple * tol
    sd_week = _median_sd(sigma, config.shortfall_min_fills)
    weeks = math.ceil(decision_days / 7.0 - 1e-9)
    p_overall = float(sp_stats.norm.sf((tol - mu) / sd_overall))
    # favourable side is judged against the raw bar open (honest mean 0, whatever s_j is)
    p_favourable = float(sp_stats.norm.cdf(-gross / sd_overall))
    p_week = float(sp_stats.norm.sf((gross - mu) / sd_week))
    # the same per-look odds apply to each window of min_fills consecutive judged fills; the
    # windows overlap (correlated), so the union bound is conservative
    n_windows = max(0, n_overall - config.shortfall_min_fills + 1)
    shortfall = ShortfallRule(
        fee_rate=costs.fee_rate,
        slippage_rate=costs.slippage_rate,
        slippage_vol_mult=costs.slippage_vol_mult,
        slippage_cap=costs.slippage_cap,
        modelled_fill_cost_bps=per_side,
        round_trip_cost_bps=2.0 * per_side,
        tolerance_mult=config.shortfall_tolerance_mult,
        tolerance_bps=tol,
        min_fills=config.shortfall_min_fills,
        fill_noise_bps=sigma,
        gross_breakage_multiple=config.gross_breakage_multiple,
        gross_bps=gross,
        honest_mean_bps=mu,
        expected_fills=expected_fills,
        overall_n=n_overall,
        overall_sd_bps=sd_overall,
        false_fail_overall=p_overall,
        false_fail_favourable=p_favourable,
        weekly_sd_bps=sd_week,
        false_fail_gross_week=p_week,
        weeks=weeks,
        false_fail_gross_any_week=1.0 - (1.0 - p_week) ** weeks,
        windows_expected=n_windows,
        false_fail_gross_any_window=min(1.0, n_windows * p_week),
        false_fail_gross_early=min(1.0, (weeks + n_windows) * p_week),
    )
    sharpe = SharpeRule(
        claim_source=claim_source,
        claim_note=claim_note,
        claimed_annual=_finite(sr * root_bpy),
        claimed_per_bar=_finite(sr),
        skew=_finite(g3),
        kurtosis=_finite(g4),
        benchmark_annual=config.benchmark_annual,
        benchmark_per_bar=bench,
        confidence=config.confidence,
        power=config.power,
        min_trl_bars=_finite(min_trl),
        min_trl_days=_finite(min_trl / bars_per_day),
        decision_trl_bars=_finite(dec_trl),
        decision_trl_days=_finite(dec_trl_days),
        min_testable_annual=None if min_testable is None else min_testable * root_bpy,
        testable=testable,
    )
    boot = bootstrap_false_fail(
        design,
        decision_bars=decision_bars,
        block_step_seconds=round(config.block_step_days * SECONDS_PER_DAY),
        independent=independent,
        trades=trades,
        exposure=exposure,
        shortfall=shortfall,
        sharpe=sharpe,
        draws=config.bootstrap_draws,
        seed=config.bootstrap_seed,
    )
    source_info = SourceInfo(
        kind="design_window_backtest",
        window_start=w0,
        window_end=w1,
        bars=len(design.index),
        days=design_days,
        run_id=link.run_id,
        trial_id=link.trial_id,
        trial_window_start=link.window_start,
        trial_window_end=link.window_end,
        wf_evidence_ids=tuple(link.wf_evidence_ids),
        ledger_path=link.ledger_path,
        ledger_is_default=link.ledger_is_default,
        chain_head=link.chain_head,
    )
    inconclusive: list[str] = []
    if trades.weak:
        inconclusive.append(
            f"only {trades.expected:.1f} entries are expected (< {trades.min_expected}): the "
            "entry count cannot discriminate"
        )
    if exposure.hi - exposure.lo >= config.max_band_width:
        inconclusive.append(
            f"the exposure band [{exposure.lo:.3f}, {exposure.hi:.3f}] is "
            f"{exposure.hi - exposure.lo:.2f} wide (>= {config.max_band_width:g}): exposure "
            "cannot discriminate"
        )
    if trades.lo == 0:
        inconclusive.append(
            f"the entry band [0, {trades.hi}] includes 0: a bot that never trades would be "
            "inside it"
        )
    proto = PaperProtocol(
        version=PROTOCOL_VERSION,
        candidate=candidate,
        plan_hash=plan_hash(p),
        logic_hash=logic_hash(p),
        symbol=p.symbol,
        interval=p.interval,
        created_at=created.isoformat(timespec="seconds"),
        start=start,
        decision_end=decision_end,
        decision_bars=decision_bars,
        decision_days=decision_days,
        min_days=config.min_days,
        max_days=config.max_days,
        mode=mode,
        locked_split_check=link.locked_split_check,
        source=source_info,
        data=DataRule(
            candle_source=candle_source,
            candle_host=candle_host,
            warmup_bars=startup_candle_count(p),
            warm_from=start - startup_candle_count(p) * step,
            grace_days=config.data_grace_days,
        ),
        trades=trades,
        exposure=exposure,
        matching=MatchRule(config.match_tolerance_bars, config.irregular_share),
        shortfall=shortfall,
        sharpe=sharpe,
        bootstrap=boot,
        design_blocks=int(counts.size),
        inconclusive_reasons=tuple(inconclusive),
        rules=(),
        config_sha256=config.config_sha256,
    )
    return _with_rules(proto)


LOCKED_SPLIT_WHY: dict[str, str] = {
    "unchecked": "the lab's locked-split check was never run for this behaviour family",
    "family_checked_not_this_plan": (
        "the lab's locked-split check was spent on another plan of this behaviour family (or "
        "on a same-feature plan it could not compare), so this variant can never be checked"
    ),
    "passed_other_ledger": (
        "the locked-split check passed only in a ledger that is not the lab's own ledger, so "
        "it is not the lab's record"
    ),
}


def locked_split_text(p: PaperProtocol) -> str:
    """The frozen locked-split state in words, for the rules and the journal header."""
    if p.locked_split_check == "passed":
        return (
            "PASSED for this plan itself, in the lab's own ledger: a pass here completes the "
            "candidate path (backtest, locked-split check, then this forward test); it still "
            "is not a forward-looking result."
        )
    why = LOCKED_SPLIT_WHY[p.locked_split_check]
    return (
        f"{p.locked_split_check.replace('_', ' ').upper()}: {why}. A pass here means "
        "'implementation consistent, but NOT a success candidate'; a forward test cannot "
        "replace the lab's check."
    )


def false_fail_text(bt: BootstrapEstimate) -> str:
    """The bootstrap figure with its caveats, never as a bare rate."""
    lo, hi = SIMULATED_FALSE_FAIL_RANGE
    return (
        f"single-path estimate {bt.fail_rate:.1%} (can be off in either direction; excludes "
        f"regime change; simulated range on a stationary random walk ~{lo:.0%}-{hi:.0%})"
    )


def _fmt(x: float | None, spec: str = ".2f") -> str:
    return "undefined" if x is None else format(x, spec)


def _claim_label(sh: SharpeRule) -> str:
    return "walk-forward OOS" if sh.claim_source == "walk_forward_oos" else "owner-supplied"


def _with_rules(p: PaperProtocol) -> PaperProtocol:
    t, e, s, sh, m, d, bt = (
        p.trades,
        p.exposure,
        p.shortfall,
        p.sharpe,
        p.matching,
        p.data,
        p.bootstrap,
    )
    where = f"public API {d.candle_host}" if d.candle_source == "api" else "the lab archive"
    rules = [
        f"Clock: the test starts at {_utc(p.start)}; the decision bar closes at "
        f"{_utc(p.decision_end)} ({p.decision_days:.1f} days, {p.decision_bars} bars). Before "
        "that, compare says continue unless a fail rule trips.",
        f"Data: candles come from {where} (frozen; compare refuses another source) with "
        f"{d.warmup_bars} warm-up bars before the start: the lab replay and the generated bot "
        f"both compute every indicator from {_utc(d.warm_from)} (WARM_FROM {d.warm_from}, "
        "rendered into the strategy; compare refuses a strategy file with another one), so "
        "recursive features start from the same bar, and the bot enters only on signal bars "
        f"from the start (START {p.start}, also rendered; a bot launched earlier holds nothing "
        "into the test). A verdict at the "
        "decision bar needs every bar time in [start, decision bar) and a candle for every "
        "paper fill. With data missing, a rule still fails when it fails whatever the missing "
        "bars hold (exposure bounded by held/N and (held + missing)/N; entries, timing and "
        "shortfall do not depend on bars without fills); otherwise compare says continue "
        f"('incomplete data: N of M bars, K fills unjudged'), and {d.grace_days:g} days after "
        "the decision bar the verdict is a final inconclusive ('data'). The first final "
        "verdict (fail at any time; pass, fail or inconclusive at the decision bar) is written "
        "once into the journal frontmatter; later compare runs never change it.",
        f"Entries: dry-run entries over the decision horizon must lie in [{t.lo}, {t.hi}] "
        f"(expected {t.expected:.1f} from {t.design_trades} design trades; union of Poisson "
        f"[{t.poisson_lo}, {t.poisson_hi}], design blocks [{t.empirical_lo}, {t.empirical_hi}] "
        f"and the predictive interval [{t.predictive_lo}, {t.predictive_hi}] from "
        f"{t.independent_blocks} non-overlapping blocks); more than {t.hi} at any time fails "
        "at once.",
        f"Exposure: the share of bars holding a position at bar close must lie in "
        f"[{e.lo:.3f}, {e.hi:.3f}] at the decision bar (design blocks scored flat at their "
        f"start; quantiles [{e.empirical_lo:.3f}, {e.empirical_hi:.3f}] united with the "
        f"predictive interval [{e.predictive_lo:.3f}, {e.predictive_hi:.3f}], +/- {e.slack:g}); "
        f"bars in market above {e.hi:.3f} x {p.decision_bars} at any time fail at once.",
        f"Timing: the lab engine replays the plan on the same candles (frozen warm-up, flat at "
        f"the start, signals at bar close, fills at the next open). Paper entries are matched "
        f"one-to-one to lab entries, and paper exits of every reason to lab exits of the same "
        f"kind (signal, stop-loss, take-profit/ROI), within +/-{m.tolerance_bars} bar(s); a "
        "stop or ROI exit where the lab exited on a signal is unmatched. The bot runs the lab's "
        "own compile_plan on closed candles from the same WARM_FROM as the replay, so by "
        "construction an honest bot fills on the lab's fill bars unless the two candle feeds "
        "differ. At the decision bar ONE count of irregular fills (unmatched paper entries + "
        "unmatched lab entries + unmatched paper exits + unmatched lab exits + matched fills "
        "off the lab's fill bar) must be at most max(1, floor("
        f"{m.irregular_share:g} x (all lab fills + all paper fills))); the one allowance covers "
        "a bar on which the exchange feed and the comparison's candles disagree. A skipped, "
        "extra or far-shifted trade is 2-4 irregular fills, "
        f"a 1-bar shift 1 per fill. A late paper fill means {LATE_LABEL}; an early one means "
        f"{EARLY_LABEL}; an unmatched paper fill means {UNMATCHED_PAPER_LABEL}; an unmatched "
        f"lab fill means {UNMATCHED_LAB_LABEL}. Settled unmatched paper entries above max(1, "
        f"floor({m.irregular_share:g} x 4 x {t.hi})), the most the decision bar could allow, "
        "fail at once.",
        f"Implementation shortfall: judged on lag-0 entries and signal exits only (their lab "
        f"fill is the next bar's open x (1 +/- modelled slippage), fee "
        f"{s.fee_rate * 1e4:.1f} bps); stop-loss and take-profit fills are at a level in the "
        "lab, so their timing is matched but their price is not judged. At the decision bar the "
        f"median of all judged fills must be at most {s.tolerance_bps:.1f} bps against the "
        f"modelled fill (drift), and the median against the raw bar open must be at least "
        f"-{s.gross_bps:.1f} bps (below that is implausibly favourable: possible look-ahead, "
        "e.g. fills at the signal bar's open). Weekly medians "
        f"(weeks with >= {s.min_fills} fills) above {s.tolerance_bps:.1f} bps are alerts only; "
        f"a week whose median exceeds {s.gross_breakage_multiple:g} x {s.tolerance_bps:.1f} = "
        f"{s.gross_bps:.1f} bps is gross breakage and fails at once, and so is any run of "
        f"{s.min_fills} consecutive judged fills (rolling, any time) whose median exceeds "
        f"{s.gross_bps:.1f} bps.",
        f"False-fail arithmetic (normal approximation of the median): an honest bot fills at "
        f"the open, so mu = -{s.slippage_rate * 1e4:.1f} bps, with an assumed fill noise sigma = "
        f"{s.fill_noise_bps:g} bps. Expected judged fills {s.expected_fills:.1f}, taken as n = "
        f"{s.overall_n}: sd = sqrt(pi/2) x {s.fill_noise_bps:g} / sqrt({s.overall_n}) = "
        f"{s.overall_sd_bps:.2f} bps; P(median > tol) = 1 - Phi(({s.tolerance_bps:.1f} - "
        f"({s.honest_mean_bps:.1f})) / {s.overall_sd_bps:.2f}) = {s.false_fail_overall:.2%}; "
        f"the favourable side is judged against the raw bar open, where an honest fill has mean "
        f"0 whatever the vol-scaled slippage: P(median vs open < -gross) = Phi(-"
        f"{s.gross_bps:.1f} / {s.overall_sd_bps:.2f}) = {s.false_fail_favourable:.3%}. "
        f"Gross breakage per week at {s.min_fills} fills: sd = {s.weekly_sd_bps:.2f} bps, "
        f"P = 1 - Phi(({s.gross_bps:.1f} - ({s.honest_mean_bps:.1f})) / {s.weekly_sd_bps:.2f}) "
        f"= {s.false_fail_gross_week:.3%}; over {s.weeks} weekly looks 1 - (1 - P)^{s.weeks} = "
        f"{s.false_fail_gross_any_week:.3%}. Rolling windows of {s.min_fills} consecutive fills: "
        f"n - {s.min_fills} + 1 = {s.overall_n} - {s.min_fills} + 1 = {s.windows_expected} "
        f"windows at the same P, union bound min(1, {s.windows_expected} x P) = "
        f"{s.false_fail_gross_any_window:.3%}; every early gross look together (weeks + windows), "
        f"min(1, ({s.weeks} + {s.windows_expected}) x P) = {s.false_fail_gross_early:.3%}.",
        f"Honest false-fail, {false_fail_text(bt)} (leave-overlap-out bootstrap: {bt.draws} "
        f"design blocks at random starts, seed {bt.seed}, each judged as the forward period of "
        "an honest bot against bands rebuilt without the design blocks that overlap it; by "
        f"rule: entries {bt.entries_rate:.1%}, exposure {bt.exposure_rate:.1%}, shortfall "
        f"{bt.shortfall_rate:.1%}, Sharpe {bt.sharpe_rate:.1%}). It is neither a floor nor a "
        "bound, and a fail on entries or exposure alone is reported as a possible regime "
        "difference.",
    ]
    min_test = _fmt(sh.min_testable_annual)
    reach = (
        f"The smallest annualised Sharpe the {p.max_days:g}-day maximum can test (at "
        f"{sh.confidence:.0%} confidence, {sh.power:.0%} power, design moments) is {min_test}."
    )
    if sh.testable:
        rules.append(
            f"Sharpe: claimed {_fmt(sh.claimed_annual)} annualised ({_claim_label(sh)}: "
            f"{sh.claim_note}); MinTRL at {sh.confidence:.0%} confidence vs "
            f"{sh.benchmark_annual:g} is {_fmt(sh.min_trl_days, '.1f')} days, "
            f"{_fmt(sh.decision_trl_days, '.1f')} days with {sh.power:.0%} power. {reach} At "
            f"the decision bar the paper per-bar returns must give PSR(SR* = "
            f"{sh.benchmark_annual:g}) >= {sh.confidence:.2f}; otherwise fail. A pass shows SR > "
            f"{sh.benchmark_annual:g} at {sh.confidence:.0%} (PSR); the claimed Sharpe itself "
            "is not confirmed."
        )
    else:
        rules.append(
            f"Sharpe NOT tested: the claimed Sharpe {_fmt(sh.claimed_annual)} annualised "
            f"({_claim_label(sh)}: {sh.claim_note}) needs a track record of "
            f"{_fmt(sh.min_trl_days, '.1f')} days (MinTRL at {sh.confidence:.0%}; "
            f"{_fmt(sh.decision_trl_days, '.1f')} days with {sh.power:.0%} power), beyond the "
            f"{p.max_days:g}-day maximum. {reach} This forward test can only check "
            "implementation (entries, exposure, timing, shortfall); a pass means "
            "'implementation consistent with the backtest', never 'Sharpe confirmed'."
        )
    if p.inconclusive_reasons:
        rules.append(
            "Inconclusive by construction: "
            + "; ".join(p.inconclusive_reasons)
            + ". At the decision bar this protocol can say fail or inconclusive, never pass."
        )
    rules.append(f"Locked-split check: {locked_split_text(p)}")
    rules.append(
        "Verdicts: fail if any rule fails (a fail on entries/exposure while every fill matched "
        "the lab at lag 0 is labelled a regime difference, timing/shortfall fails an "
        "implementation difference); continue before the decision bar or while data may "
        "still arrive; "
        "inconclusive at the decision bar when every check is inside tolerance but the protocol "
        f"cannot discriminate (the reasons above, or fewer than {s.min_fills} judged fills) or "
        "the data never completed; pass otherwise. The protocol is frozen: a changed protocol "
        "needs a new candidate name, and a plan (or its logic) already registered under another "
        "name is refused."
    )
    return PaperProtocol(**{**_shallow(p), "rules": tuple(rules)})


def _shallow(p: PaperProtocol) -> dict[str, Any]:
    return {k: getattr(p, k) for k in p.__dataclass_fields__}


# --------------------------------------------------------------------------- serialisation


def protocol_to_dict(p: PaperProtocol) -> dict[str, Any]:
    d = asdict(p)
    d["rules"] = list(p.rules)
    d["inconclusive_reasons"] = list(p.inconclusive_reasons)
    d["source"]["wf_evidence_ids"] = list(p.source.wf_evidence_ids)
    return d


def protocol_from_dict(d: Mapping[str, Any]) -> PaperProtocol:
    """Inverse of :func:`protocol_to_dict`; ``ValueError`` on a missing or unknown field."""
    try:
        data = dict(d)
        if data.get("version") != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {data.get('version')!r}")
        src = dict(data["source"])
        src["wf_evidence_ids"] = tuple(src["wf_evidence_ids"])
        data["source"] = SourceInfo(**src)
        data["data"] = DataRule(**data["data"])
        data["trades"] = TradeBand(**data["trades"])
        data["exposure"] = ExposureBand(**data["exposure"])
        data["matching"] = MatchRule(**data["matching"])
        data["shortfall"] = ShortfallRule(**data["shortfall"])
        data["sharpe"] = SharpeRule(**data["sharpe"])
        data["bootstrap"] = BootstrapEstimate(**data["bootstrap"])
        data["inconclusive_reasons"] = tuple(data["inconclusive_reasons"])
        data["rules"] = tuple(data["rules"])
        return PaperProtocol(**data)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed protocol: {exc}") from None


def _canonical(p: PaperProtocol) -> str:
    return json.dumps(protocol_to_dict(p), sort_keys=True, separators=(",", ":"), allow_nan=False)


def protocol_to_json(p: PaperProtocol) -> str:
    return json.dumps(protocol_to_dict(p), indent=2, sort_keys=True, allow_nan=False) + "\n"


def protocol_sha256(p: PaperProtocol) -> str:
    """sha256 of the canonical JSON (sorted keys, no whitespace): layout-independent."""
    return hashlib.sha256(_canonical(p).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Fill:
    time: int  # UNIX seconds of the paper fill
    side: Literal["entry", "exit"]
    shortfall_bps: float  # adverse = positive, against the matched lab fill bar's model
    lag_bars: int = 0  # paper fill bar minus matched lab fill bar
    slip_bps: float = 0.0  # the lab's modelled slippage s_j on that bar (vol-scaled), bps

    @property
    def vs_open_bps(self) -> float:
        """Adverse bps against the raw bar open (``shortfall + s_j``): ~0 for an honest market
        order at the open, whatever the modelled slippage."""
        return self.shortfall_bps + self.slip_bps


@dataclass(frozen=True)
class ObservedPaper:
    """What the dry-run bot did in ``[protocol.start, asof)`` (never past the decision bar),
    with the data coverage and the lab-replay timing match."""

    asof: int  # UNIX seconds: end of the observed period (a bar boundary, <= decision_end)
    observed_until: int  # when compare looked (unclamped; drives the data grace period)
    bars_expected: int  # bar times in [start, asof)
    bars_elapsed: int  # of those, bar times with a candle
    n_entries: int  # paper entries
    bars_in_market: int  # rows holding a position at bar close
    fills: tuple[Fill, ...]  # shortfall-judged: lag-0 matched entries and signal exits
    fills_skipped: int  # paper exits whose price is not judged (stop / ROI / other reasons)
    fills_missing_candle: int  # paper fills whose bar has no candle (unjudged)
    entries_missing_candle: int  # of those, entries
    lab_entries: int  # lab replay entries in the period
    matched_entries: int
    unmatched_paper_settled: int  # unmatched paper entries that can no longer match
    paper_exits: int  # every reason
    lab_exits: int  # every reason
    matched_exits: int  # same kind (signal / stop / target) within the tolerance
    paper_signal_exits: int
    lab_signal_exits: int
    entry_early: int  # matched entries before the lab's fill bar (bot used a forming candle?)
    entry_late: int  # matched entries after it (the lab acted earlier than live could?)
    exit_early: int
    exit_late: int
    # irregular fills whose match window lies wholly before the first missing bar (a gap
    # changes the lab replay from there on); equals ``irregular`` when no bar is missing
    irregular_before_gap: int
    bar_returns: tuple[float, ...]  # per-bar paper returns, fully invested, net of fees

    @property
    def matched_early(self) -> int:
        return self.entry_early + self.exit_early

    @property
    def matched_late(self) -> int:
        return self.entry_late + self.exit_late

    @property
    def matched_lag0(self) -> int:
        """Matched fills (entries + exits) on exactly the lab's fill bar."""
        return self.matched_entries + self.matched_exits - self.matched_early - self.matched_late

    @property
    def unmatched_paper_entries(self) -> int:
        """Paper entries with a candle that match no lab entry within the tolerance."""
        return self.n_entries - self.entries_missing_candle - self.matched_entries

    @property
    def unmatched_lab_entries(self) -> int:
        return self.lab_entries - self.matched_entries

    @property
    def exits_missing_candle(self) -> int:
        return self.fills_missing_candle - self.entries_missing_candle

    @property
    def unmatched_paper_exits(self) -> int:
        """Paper exits with a candle that match no lab exit of the same kind (any reason)."""
        return self.paper_exits - self.exits_missing_candle - self.matched_exits

    @property
    def unmatched_lab_exits(self) -> int:
        return self.lab_exits - self.matched_exits

    @property
    def total_fills(self) -> int:
        """Every lab fill plus every paper fill (entries and exits) in the period."""
        return self.lab_entries + self.lab_exits + self.n_entries + self.paper_exits

    @property
    def irregular(self) -> int:
        """Unmatched paper and lab entries and exits, plus matched fills off the lab's bar."""
        return (
            self.unmatched_paper_entries
            + self.unmatched_lab_entries
            + self.unmatched_paper_exits
            + self.unmatched_lab_exits
            + self.matched_early
            + self.matched_late
        )

    def irregular_detail(self) -> str:
        return (
            f"unmatched entries {self.unmatched_paper_entries} paper / "
            f"{self.unmatched_lab_entries} lab, unmatched exits {self.unmatched_paper_exits} "
            f"paper / {self.unmatched_lab_exits} lab, off the lab's fill bar "
            f"{self.entry_early + self.entry_late} entries / {self.exit_early + self.exit_late} "
            f"exits ({self.matched_late} late, {self.matched_early} early)"
        )

    @property
    def bars_missing(self) -> int:
        return self.bars_expected - self.bars_elapsed

    @property
    def complete(self) -> bool:
        return self.bars_missing == 0 and self.fills_missing_candle == 0


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    observed: float | None
    lo: float | None
    hi: float | None
    note: str
    kind: CheckKind = "implementation"


@dataclass(frozen=True)
class WeekShortfall:
    week: int  # 0-based week since the start
    start: int
    fills: int
    median_bps: float | None
    judged: bool
    alert: bool  # judged and median above the tolerance (reported, never a verdict)
    gross: bool  # judged and median above the gross-breakage level (fails)


@dataclass(frozen=True)
class Verdict:
    verdict: VerdictName
    mode: Mode
    headline: str
    reasons: tuple[str, ...]
    fail_kinds: tuple[str, ...]  # "regime", "implementation", "performance"
    alerts: tuple[str, ...]
    checks: tuple[Check, ...]
    weeks: tuple[WeekShortfall, ...]
    elapsed_days: float
    at_decision: bool
    data_complete: bool
    bars_expected: int
    bars_present: int
    fills_unjudged: int
    protocol_sha256: str

    @property
    def final(self) -> bool:
        return self.verdict in FINAL_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        d["fail_kinds"] = list(self.fail_kinds)
        d["alerts"] = list(self.alerts)
        return d


def weekly_shortfall(
    protocol: PaperProtocol, fills: Sequence[Fill], *, asof: int, at_decision: bool
) -> list[WeekShortfall]:
    """Median adverse shortfall per 7-day week from ``protocol.start``. A week is judged once it
    has ended (or at the decision bar, for the last partial week) and holds at least
    ``min_fills`` fills; a judged week above the tolerance is an alert, above the gross level
    it is gross breakage."""
    end = min(asof, protocol.decision_end)
    if end <= protocol.start:
        return []
    s = protocol.shortfall
    n_weeks = -(-(end - protocol.start) // WEEK_SECONDS)
    out: list[WeekShortfall] = []
    for k in range(n_weeks):
        w0 = protocol.start + k * WEEK_SECONDS
        w1 = min(w0 + WEEK_SECONDS, protocol.decision_end)
        vals = [f.shortfall_bps for f in fills if w0 <= f.time < w1]
        complete = w1 <= asof or at_decision
        med = float(np.median(vals)) if vals else None
        judged = complete and len(vals) >= s.min_fills
        out.append(
            WeekShortfall(
                week=k,
                start=w0,
                fills=len(vals),
                median_bps=med,
                judged=judged,
                alert=judged and med is not None and med > s.tolerance_bps,
                gross=judged and med is not None and med > s.gross_bps,
            )
        )
    return out


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _irregular_labels(o: ObservedPaper) -> list[str]:
    """What each kind of irregular fill suggests (named on every timing fail)."""
    out: list[str] = []
    if o.matched_late:
        out.append(f"late: {LATE_LABEL}")
    if o.matched_early:
        out.append(f"early: {EARLY_LABEL}")
    if o.unmatched_paper_entries > 0 or o.unmatched_paper_exits > 0:
        out.append(f"unmatched paper fills: {UNMATCHED_PAPER_LABEL}")
    if o.unmatched_lab_entries > 0 or o.unmatched_lab_exits > 0:
        out.append(f"unmatched lab fills: {UNMATCHED_LAB_LABEL}")
    return out


def _decision_checks(protocol: PaperProtocol, o: ObservedPaper) -> list[Check]:
    """The decision-bar checks. With complete data they are exact; with missing data each is
    judged in the case most favourable to the bot over the missing bars and the unjudged fills,
    so a ``fail`` here holds whatever the missing data hold, and anything that could still go
    either way is ``pending``."""
    t, e, s, sh, m = (
        protocol.trades,
        protocol.exposure,
        protocol.shortfall,
        protocol.sharpe,
        protocol.matching,
    )
    exact = o.complete
    k_all = o.fills_missing_candle
    out: list[Check] = []

    # entries: counted from the dry-run DB, independent of candles
    # (above the band is the any-time ``entries_cap`` rule)
    n = o.n_entries
    ok = n >= t.lo
    note = f"{n} entries in [{t.lo}, {t.hi}]" if ok else f"{n} entries < {t.lo}"
    out.append(Check("entries", "ok" if ok else "fail", n, t.lo, t.hi, note, "regime"))

    # exposure: bounded by held/N and (held + missing)/N
    big_n = o.bars_expected
    lo_b = o.bars_in_market / big_n if big_n else 0.0
    hi_b = (o.bars_in_market + o.bars_missing) / big_n if big_n else 1.0
    if hi_b < e.lo or lo_b > e.hi:
        span = f"{lo_b:.3f}" if exact else f"[{lo_b:.3f}, {hi_b:.3f}] over the missing bars"
        note = f"exposure {span} outside [{e.lo:.3f}, {e.hi:.3f}]"
        out.append(Check("exposure", "fail", lo_b, e.lo, e.hi, note, "regime"))
    else:
        status: CheckStatus = "ok" if exact else "pending"
        note = f"exposure {lo_b:.3f} vs [{e.lo:.3f}, {e.hi:.3f}]"
        out.append(Check("exposure", status, lo_b, e.lo, e.hi, note, "regime"))

    # timing: ONE joint count of irregular fills (unmatched paper and lab entries and exits of
    # every reason, plus matched fills off the lab's bar) against max(1, floor(share x all
    # fills)). A shift beyond the match tolerance is unmatched on both sides, so it costs more
    # than a 1-bar shift (monotone). Best case over the unjudged paper fills: each matches an
    # unmatched lab fill of its side at lag 0.
    irr, fills_all = o.irregular, o.total_fills
    allowed = irregular_allowance(m, fills_all)
    if exact:
        best_irr, best_allowed = irr, allowed
    else:
        # a missing candle changes the lab replay from that bar on: only the irregular fills
        # settled before the first gap count, against the largest cap complete data could give
        best_irr = o.irregular_before_gap
        best_allowed = irregular_allowance(m, max(fills_all, 4 * protocol.trades.hi))
    detail = f"{irr} irregular of {fills_all} fills ({o.irregular_detail()})"
    if not exact:
        detail += f"; {best_irr} settled before the first missing bar"
    if best_irr > best_allowed:
        note = (
            f"timing: {detail} > {allowed} allowed (max(1, floor({m.irregular_share:g} x "
            f"{fills_all}))); " + "; ".join(_irregular_labels(o))
        )
        out.append(Check("irregular_fills", "fail", irr, None, allowed, note))
    else:
        status = "ok" if exact else "pending"
        note = f"{detail}, {allowed} allowed"
        out.append(Check("irregular_fills", status, irr, None, allowed, note))

    # shortfall: the median of judged fills must lie in [-gross, tol]
    judged = [f for f in o.fills if f.time < protocol.decision_end]
    vals = [f.shortfall_bps for f in judged]
    med_lo, _ = median_bounds(vals, k_all)
    _, open_hi = median_bounds([f.vs_open_bps for f in judged], k_all)
    if len(vals) + k_all < s.min_fills:
        note = f"{len(vals)} judged fills < {s.min_fills}: implementation not verifiable"
        out.append(Check("shortfall", "pending", None, -s.gross_bps, s.tolerance_bps, note))
    elif med_lo > s.tolerance_bps:
        note = (
            f"shortfall: median {med_lo:+.1f} bps over {len(vals)} judged fills > "
            f"{s.tolerance_bps:.1f} bps (drift beyond the modelled slippage)"
        )
        out.append(Check("shortfall", "fail", med_lo, -s.gross_bps, s.tolerance_bps, note))
    elif open_hi < -s.gross_bps:
        note = (
            f"shortfall: median {open_hi:+.1f} bps against the bar open over {len(vals)} judged "
            f"fills < -{s.gross_bps:.1f} bps: implausibly favourable fills (possible look-ahead, "
            "e.g. filling at the signal bar's open)"
        )
        out.append(Check("shortfall", "fail", open_hi, -s.gross_bps, s.tolerance_bps, note))
    else:
        med = float(np.median(vals)) if vals else None
        status = "ok" if exact and len(vals) >= s.min_fills else "pending"
        note = f"median shortfall {_fmt(med, '+.1f')} bps over {len(vals)} judged fills"
        if exact and len(vals) < s.min_fills:
            note = f"{len(vals)} judged fills < {s.min_fills}: implementation not verifiable"
        out.append(Check("shortfall", status, med, -s.gross_bps, s.tolerance_bps, note))

    # Sharpe: only when the protocol said upfront that the horizon can test it; it depends on
    # every bar's return, so with missing bars it stays pending
    if sh.testable:
        if exact:
            rets = np.asarray(o.bar_returns, dtype=float)
            psr = probabilistic_sharpe(rets, sh.benchmark_per_bar) if rets.size >= 2 else math.nan
            ok = math.isfinite(psr) and psr >= sh.confidence
            note = (
                f"PSR(SR* = {sh.benchmark_annual:g}) {_fmt(_finite(psr), '.3f')} vs "
                f">= {sh.confidence:.2f} over {rets.size} bars"
            )
            status = "ok" if ok else "fail"
            out.append(
                Check("sharpe", status, _finite(psr), sh.confidence, None, note, "performance")
            )
        else:
            note = "PSR needs every bar's return"
            out.append(Check("sharpe", "pending", None, sh.confidence, None, note, "performance"))
    return out


def _label(checks: Sequence[Check], o: ObservedPaper) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fail reasons prefixed with what kind of difference they show, and the kinds."""
    fails = [c for c in checks if c.status == "fail"]
    impl = any(c.kind == "implementation" for c in fails)
    on_time = o.irregular == 0
    reasons: list[str] = []
    kinds: set[str] = set()
    for c in fails:
        if c.kind == "regime":
            if not impl and on_time:
                label = (
                    "regime difference (entries/exposure outside the design range while every "
                    "fill matched the lab at lag 0)"
                )
                kinds.add("regime")
            else:
                label = "regime or implementation difference (irregular fills or shortfall too)"
                kinds.update(("regime", "implementation"))
        elif c.kind == "performance":
            label = "performance"
            kinds.add("performance")
        else:
            label = "implementation difference"
            kinds.add("implementation")
        reasons.append(f"[{label}] {c.note}")
    return tuple(reasons), tuple(sorted(kinds))


def _pass_headline(protocol: PaperProtocol, o: ObservedPaper) -> str:
    """The pass headline: both off-lag counts ("lag 0" only when every matched fill was on the
    lab's fill bar), the Sharpe wording, and NOT a success candidate unless the behaviour
    family's locked-split check passed."""
    m = protocol.matching
    allowed = irregular_allowance(m, o.total_fills)
    if o.irregular == 0:
        timing = f"every fill matched at lag 0 (0 irregular of {o.total_fills} fills)"
    else:
        timing = (
            f"{o.irregular} irregular of {o.total_fills} fills, within the {allowed} allowed "
            f"({o.irregular_detail()})"
        )
    checked = (
        "entry count and exposure inside the design bands, entries and exits of every reason "
        f"against the lab replay: {timing}, and shortfall of entries and signal exits inside "
        "tolerance (stop/take-profit exit prices are not judged)"
    )
    if protocol.mode == "implementation_only":
        sharpe = (
            "the claimed Sharpe was NOT tested: its minimum track record exceeds the maximum "
            "test length"
        )
    else:
        sh = protocol.sharpe
        sharpe = (
            f"SR > {sh.benchmark_annual:g} at {sh.confidence:.0%} (PSR); the claimed Sharpe "
            "itself is not confirmed"
        )
    if protocol.locked_split_check == "passed":
        lead = (
            "pass (implementation consistent with the backtest on what this protocol checks; "
            "the lab's locked-split check of this plan passed"
        )
    else:
        lead = (
            "pass (implementation consistent, but NOT a success candidate ("
            f"{LOCKED_SPLIT_WHY[protocol.locked_split_check]})"
        )
    return f"{lead}: {checked}; {sharpe})"


def evaluate(protocol: PaperProtocol, observed: ObservedPaper) -> Verdict:
    """Judge ``observed`` against the frozen ``protocol``.

    * Fail at any time: entries above the band, bars in market above the band's maximum,
      gross shortfall breakage in a judged week or in any run of ``min_fills`` consecutive
      judged fills, settled unmatched paper entries above the most irregular fills the
      decision bar could allow.
    * At the decision bar: the decision checks (entry band, exposure band, the joint
      irregular-fill count, overall median shortfall in ``[-gross, tol]``, PSR when tested).
      With complete data they decide; with missing data a check fails only if it fails
      whatever the missing data hold, else ``continue``, and ``grace_days`` after the decision
      bar a final ``inconclusive`` ("data").
    * With every check inside tolerance: ``inconclusive`` when the protocol cannot discriminate
      (weak / vacuous bands, too few judged fills), else ``pass``.
    """
    at_decision = observed.asof >= protocol.decision_end
    asof = min(observed.asof, protocol.decision_end)
    elapsed = max(0, asof - protocol.start) / SECONDS_PER_DAY
    complete = observed.complete
    grace_end = protocol.decision_end + round(protocol.data.grace_days * SECONDS_PER_DAY)
    t, e, s, m = protocol.trades, protocol.exposure, protocol.shortfall, protocol.matching
    checks: list[Check] = []

    # --- fail rules that apply at any time (they only depend on what has been observed)
    n = observed.n_entries
    if n > t.hi:
        checks.append(
            Check("entries_cap", "fail", n, t.lo, t.hi, f"{n} entries > {t.hi}", "regime")
        )
    held = observed.bars_in_market
    if held > e.hi * protocol.decision_bars:
        note = f"{held} bars in market > {e.hi:.3f} x {protocol.decision_bars} decision bars"
        checks.append(Check("exposure_cap", "fail", held, e.lo, e.hi, note, "regime"))
    weeks = weekly_shortfall(protocol, observed.fills, asof=observed.asof, at_decision=at_decision)
    alerts = tuple(
        f"week {w.week} median shortfall {w.median_bps:+.1f} bps > {s.tolerance_bps:.1f} bps "
        f"over {w.fills} fills (alert only)"
        for w in weeks
        if w.alert and not w.gross and w.median_bps is not None
    )
    for w in weeks:
        if w.gross and w.median_bps is not None:
            checks.append(
                Check(
                    f"shortfall_week_{w.week}",
                    "fail",
                    w.median_bps,
                    None,
                    s.gross_bps,
                    f"week {w.week} median shortfall {w.median_bps:+.1f} bps > "
                    f"{s.gross_bps:.1f} bps over {w.fills} fills (gross breakage beyond the "
                    "modelled slippage)",
                )
            )
    judged = sorted(
        (f for f in observed.fills if f.time < protocol.decision_end), key=lambda f: f.time
    )
    runs = gross_fill_windows(judged, size=s.min_fills, gross_bps=s.gross_bps)
    if runs:
        i, run_med = runs[0]
        first, last = judged[i], judged[i + s.min_fills - 1]
        checks.append(
            Check(
                f"shortfall_fills_{i}",
                "fail",
                run_med,
                None,
                s.gross_bps,
                f"fills {i + 1}-{i + s.min_fills} ({_utc(first.time)} to {_utc(last.time)}) "
                f"median shortfall {run_med:+.1f} bps > {s.gross_bps:.1f} bps over "
                f"{s.min_fills} consecutive fills (gross breakage beyond the modelled slippage; "
                f"{len(runs)} such window(s))",
            )
        )
    # settled unmatched paper entries can never become regular. With at most t.hi paper
    # entries (more fails entries_cap) there are at most 4 x t.hi fills from a lab that trades
    # as often; each extra lab trade adds 2 fills (<= 1 more allowed, share < 0.5) but at least
    # 2 irregular ones, so this cap is the most the decision bar could ever allow.
    settled = observed.unmatched_paper_settled
    cap = irregular_allowance(m, 4 * t.hi)
    if settled > cap:
        note = (
            f"timing: {settled} paper entries match no lab fill and never can; at most {cap} "
            f"irregular fills can be allowed at the decision bar (max(1, floor("
            f"{m.irregular_share:g} x 4 x {t.hi}))); unmatched paper fills: "
            f"{UNMATCHED_PAPER_LABEL}"
        )
        checks.append(Check("irregular_fills_cap", "fail", settled, None, cap, note))

    # --- the decision bar
    if at_decision:
        checks.extend(_decision_checks(protocol, observed))
    else:
        note = (
            f"{n} entries, {observed.matched_entries} matched of {observed.lab_entries} lab; "
            f"{len(judged)} judged fills so far"
        )
        checks.append(Check("progress", "pending", None, None, None, note))

    data_note = (
        f"incomplete data: {observed.bars_elapsed} of {observed.bars_expected} bars, "
        f"{observed.fills_missing_candle} fills unjudged"
    )
    reasons, kinds = _label(checks, observed)
    verdict: VerdictName
    if reasons:
        verdict = "fail"
        headline = "fail: " + "; ".join(reasons)
        if not complete:
            headline += f" ({data_note}; this fails whatever the missing data hold)"
        if protocol.mode == "implementation_only":
            headline += " [implementation-only protocol: the Sharpe was not under test]"
    elif not at_decision:
        verdict = "continue"
        reasons = () if complete else (data_note,)
        headline = (
            f"continue: {elapsed:.1f} of {protocol.decision_days:.1f} days elapsed; no fail rule "
            "tripped" + ("" if complete else f" ({data_note})")
        )
    elif not complete and observed.observed_until >= grace_end:
        verdict = "inconclusive"
        reasons = (
            f"data: {data_note} {protocol.data.grace_days:g} days after the decision bar; no "
            "rule fails whatever the missing data hold",
        )
        headline = "inconclusive: " + reasons[0]
    elif not complete:
        verdict, reasons = "continue", (data_note,)
        headline = (
            f"continue: {data_note}; the decision bar has passed but no rule fails whatever the "
            f"missing data hold; re-run compare with complete candles (a final inconclusive "
            f"after {_utc(grace_end)})"
        )
    else:
        vague = list(protocol.inconclusive_reasons)
        vague += [c.note for c in checks if c.name == "shortfall" and c.status == "pending"]
        if vague:
            verdict, reasons = "inconclusive", tuple(vague)
            headline = (
                "inconclusive: every check is inside tolerance but the protocol cannot "
                "discriminate: " + "; ".join(vague)
            )
        else:
            verdict, reasons = "pass", ()
            headline = _pass_headline(protocol, observed)
    return Verdict(
        verdict=verdict,
        mode=protocol.mode,
        headline=headline,
        reasons=reasons,
        fail_kinds=kinds if verdict == "fail" else (),
        alerts=alerts,
        checks=tuple(checks),
        weeks=tuple(weeks),
        elapsed_days=elapsed,
        at_decision=at_decision,
        data_complete=complete,
        bars_expected=observed.bars_expected,
        bars_present=observed.bars_elapsed,
        fills_unjudged=observed.fills_missing_candle,
        protocol_sha256=protocol_sha256(protocol),
    )


# --------------------------------------------------------------------------- registration


def _fsync_write(path: Path, data: bytes) -> Path:
    """Write ``data`` to a fresh ``mkstemp`` file next to ``path`` and fsync it; returns the
    temporary path (the caller links or renames it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def _create_exclusive(path: Path, data: bytes) -> None:
    """Atomically create ``path``; ``FileExistsError`` if it exists (never overwrites)."""
    tmp = _fsync_write(path, data)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_atomic(path: Path, data: bytes) -> None:
    tmp = _fsync_write(path, data)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _journal_lock(journal: Path) -> Iterator[None]:
    """Exclusive ``flock`` on ``.<journal name>.lock`` beside the entry, held across a
    read-check-write so two ``compare`` runs cannot both record a final verdict."""
    lock = journal.with_name(f".{journal.name}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", text.lower()).strip("-") or "x"


def protocol_path(paper_dir: Path, candidate: str) -> Path:
    return Path(paper_dir) / check_candidate_name(candidate) / PROTOCOL_FILE


def registered_hashes(
    paper_dir: Path, journal_dir: Path | None = None
) -> list[tuple[str, str, str, str]]:
    """``(candidate, plan_hash, logic_hash, where)`` of every protocol registered under
    ``paper_dir`` and every paper journal entry (``*-paper-*.md``) under ``journal_dir``, so
    deleting ``paper/<old>`` cannot free a plan for re-registration. A protocol without
    ``logic_hash`` is hashed from its folder's ``plan.json``. ``ValueError`` naming the file
    when one cannot be read: the duplicate check fails closed."""
    out: list[tuple[str, str, str, str]] = []
    root = Path(paper_dir)
    for path in sorted(root.glob(f"*/{PROTOCOL_FILE}")) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cand, ph = str(data["candidate"]), str(data["plan_hash"])
            lh = data.get("logic_hash")
            if not isinstance(lh, str):
                plan = json.loads((path.parent / "plan.json").read_text(encoding="utf-8"))
                lh = logic_hash(plan)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                f"cannot read the registered protocol {path} for the duplicate check: {exc}"
            ) from None
        out.append((cand, ph, lh, str(path)))
    jroot = Path(journal_dir) if journal_dir is not None else None
    if jroot is not None and jroot.is_dir():
        for path in sorted(jroot.glob("*-paper-*.md")):
            try:
                front = parse_frontmatter(path.read_text(encoding="utf-8"))
                cand, ph, lh = front["candidate"], front["plan_hash"], front["logic_hash"]
            except (OSError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"cannot read the paper journal entry {path} for the duplicate check: {exc}"
                ) from None
            out.append((str(cand), str(ph), str(lh), str(path)))
    return out


def check_not_registered_elsewhere(
    paper_dir: Path,
    candidate: str,
    plan: Mapping[str, Any] | Plan,
    *,
    journal_dir: Path | None = None,
    family: LedgerLink | None = None,
) -> None:
    """``ValueError`` naming the existing candidate when a protocol under ``paper_dir`` or a
    paper journal entry under ``journal_dir`` with another candidate name has the same
    ``plan_hash`` or the same logic hash (one hypothesis gets one pre-registered forward test;
    re-registering it under a new name would let a failed test be retried), or when another
    candidate name has the same journal slug (``foo_bar`` / ``foo-bar``). With ``family`` (the
    :func:`ledger_link` of ``plan``, i.e. the lab's behaviour family) a registered plan in that
    family (a no-op-clause twin, the same rule re-tuned, returns correlated >= the lab's family
    threshold, or an uncomparable same-feature plan: fail closed) is refused too."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    ph, lh = plan_hash(p), logic_hash(p)
    fam_logic = set(family.family_logic_hashes) if family is not None else set()
    fam_uncertain = set(family.family_uncertain) if family is not None else set()
    fam_plans = set(family.family_plan_hashes) if family is not None else set()
    for cand, other_ph, other_lh, where in registered_hashes(paper_dir, journal_dir):
        if cand == candidate:
            continue
        if _slug(cand) == _slug(candidate):
            raise ValueError(
                f"candidate {candidate!r} has the same journal name as registered candidate "
                f"{cand!r} ({where}); choose a distinct name"
            )
        if other_ph == ph:
            raise ValueError(
                f"plan {ph[:12]} is already registered as paper candidate {cand!r} ({where}); a "
                "hypothesis gets one pre-registered forward test"
            )
        if other_lh == lh:
            raise ValueError(
                f"the logic of this plan ({lh[:12]}) is already registered as paper candidate "
                f"{cand!r} (same rules, other numbers or interval; {where}); a hypothesis gets "
                "one pre-registered forward test"
            )
        if other_lh in fam_logic or other_ph in fam_plans:
            raise ValueError(
                f"paper candidate {cand!r} ({where}) is in this plan's behaviour family (the lab "
                "counts it as the same hypothesis: the same logic, or design-window returns "
                "correlated >= its family threshold, e.g. a no-op clause); a hypothesis gets one "
                "pre-registered forward test"
            )
        if other_lh in fam_uncertain:
            raise ValueError(
                f"paper candidate {cand!r} ({where}) uses the same features as this plan and the "
                "lab could not compare their returns; an unknown comparison counts as the same "
                "hypothesis (fail closed)"
            )


def journal_path(protocol: PaperProtocol, journal_dir: Path) -> Path:
    """``<journal_dir>/<date>-<symbol>-paper-<candidate>.md`` (date of registration, UTC)."""
    date = protocol.created_at[:10]
    return (
        Path(journal_dir) / f"{date}-{_slug(protocol.symbol)}-paper-{_slug(protocol.candidate)}.md"
    )


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_journal_entry(protocol: PaperProtocol) -> str:
    """The candidate's journal entry: Obsidian frontmatter, the protocol in readable form, the
    frozen JSON and its sha256, and an empty verdicts section that ``compare`` appends to."""
    p, t, e, s, sh, m, bt = (
        protocol,
        protocol.trades,
        protocol.exposure,
        protocol.shortfall,
        protocol.sharpe,
        protocol.matching,
        protocol.bootstrap,
    )
    sha = protocol_sha256(p)
    src = p.source
    lines = [
        "---",
        f"candidate: {_yaml(p.candidate)}",
        f"plan_hash: {_yaml(p.plan_hash)}",
        f"logic_hash: {_yaml(p.logic_hash)}",
        f"run_id: {_yaml(src.run_id)}",
        f"trial_id: {src.trial_id}",
        f"date: {_yaml(p.created_at[:10])}",
        f"symbol: {_yaml(p.symbol)}",
        f"interval: {_yaml(p.interval)}",
        f"protocol_sha256: {_yaml(sha)}",
        f"mode: {_yaml(p.mode)}",
        f"locked_split_check: {_yaml(p.locked_split_check)}",
        'outcome: "pending"',
        "tags: [paper-trade]",
        "---",
        "",
        f"# Paper-trade protocol: {p.candidate} ({p.symbol} {p.interval})",
        "",
        f"Registered {p.created_at} before the first dry-run trade. Venue: freqtrade "
        "`--dry-run` (paper only; no exchange keys, no orders). The protocol below is frozen: "
        "`paper_trade.py compare` refuses to judge if `protocol.json` no longer matches the "
        "sha256 above, only appends to the verdicts section, and writes the first final "
        "verdict into `outcome` once.",
        "",
        PROTOCOL_HEADING,
        "",
        f"- Plan `{p.plan_hash}` (logic `{p.logic_hash[:12]}`); protocol sha256 `{sha}`; "
        f"config sha256 `{p.config_sha256}`.",
        f"- Ledger: research-run design-window trial {src.trial_id} of run {src.run_id} "
        f"({_utc(src.trial_window_start)} to {_utc(src.trial_window_end)}); walk-forward "
        f"evidence ids {list(src.wf_evidence_ids) or '-'}.",
        f"- Candles: {p.data.candle_source} ({p.data.candle_host}), {p.data.warmup_bars} warm-up "
        f"bars; data grace {p.data.grace_days:g} days.",
        f"- Mode: **{p.mode}**. Verdicts: pass / fail / continue / inconclusive.",
        f"- Expectations from the lab backtest of the plan on the design window "
        f"{_utc(src.window_start)} to {_utc(src.window_end)} ({src.bars} bars, "
        f"{src.days:.0f} days); {p.design_blocks} rolling design blocks ({t.independent_blocks} "
        "non-overlapping), each scored flat at its start.",
        f"- Honest false-fail: {false_fail_text(bt)}; leave-overlap-out bootstrap of design "
        "blocks, neither a floor nor a bound.",
        f"- Locked-split check: {locked_split_text(p)}",
        f"- Ledger read at registration: `{src.ledger_path or '-'}` "
        f"({'the lab ledger' if src.ledger_is_default else 'NOT the lab ledger'}); run "
        f"{src.run_id} chain head `{src.chain_head or 'not chained'}`.",
        "",
        "| Check | Expected | Tolerance |",
        "| --- | --- | --- |",
        f"| Entries over {p.decision_days:.1f} days | {t.expected:.1f} | [{t.lo}, {t.hi}] "
        f"(Poisson [{t.poisson_lo}, {t.poisson_hi}], design blocks [{t.empirical_lo}, "
        f"{t.empirical_hi}], predictive [{t.predictive_lo}, {t.predictive_hi}], "
        f"{t.confidence:.0%}) |",
        f"| Exposure (bars in market) | {e.expected:.3f} | [{e.lo:.3f}, {e.hi:.3f}] "
        f"(design blocks [{e.empirical_lo:.3f}, {e.empirical_hi:.3f}], predictive "
        f"[{e.predictive_lo:.3f}, {e.predictive_hi:.3f}], +/- {e.slack:g}) |",
        f"| Timing vs the lab replay | every entry and exit (any reason) on the lab's fill bar "
        f"| +/-{m.tolerance_bars} bar(s) to match; irregular fills (unmatched paper or lab "
        "entries and exits, plus matched fills off the lab's bar) at most max(1, floor("
        f"{m.irregular_share:g} x all fills)) (late = possible look-ahead in the lab, early = "
        "possible use of the still-forming candle) |",
        f"| Implementation shortfall (lag-0 entries and signal exits) | "
        f"{s.honest_mean_bps:+.1f} bps for an honest open fill vs the modelled fill (fee "
        f"{s.fee_rate * 1e4:.1f} + slippage {s.slippage_rate * 1e4:.1f} bps per side) | overall "
        f"median in [-{s.gross_bps:.1f}, {s.tolerance_bps:.1f}] bps (P false fail "
        f"{s.false_fail_overall:.2%}); weekly / {s.min_fills}-fill median > {s.gross_bps:.1f} "
        f"bps fails (union bound {s.false_fail_gross_early:.2%}) |",
        f"| Claimed Sharpe (annualised, 365 d) | {_fmt(sh.claimed_annual)} ({_claim_label(sh)}) "
        f"| MinTRL {_fmt(sh.min_trl_days, '.1f')} d at {sh.confidence:.0%}; "
        f"{_fmt(sh.decision_trl_days, '.1f')} d at {sh.power:.0%} power; smallest testable "
        f"{_fmt(sh.min_testable_annual)} |",
        "",
        "### Decision rules",
        "",
        *(f"{i}. {r}" for i, r in enumerate(p.rules, 1)),
        "",
        "### Frozen protocol JSON",
        "",
        "```json",
        protocol_to_json(p).rstrip("\n"),
        "```",
        "",
        "> Results are historical, not forward-looking. Paper trading only.",
        "",
        VERDICTS_HEADING,
        "",
    ]
    return "\n".join(lines)


def register_protocol(
    protocol: PaperProtocol, *, paper_dir: Path, journal_dir: Path
) -> tuple[Path, Path]:
    """Exclusively create ``protocol.json`` and then the journal entry (never overwrite
    either). ``FileExistsError`` before any write if either exists; a failed journal write
    removes the new protocol. If the process dies between the two writes, ``prepare`` of the
    same name completes the registration (:func:`complete_registration`)."""
    ppath = protocol_path(paper_dir, protocol.candidate)
    jpath = journal_path(protocol, journal_dir)
    for path in (ppath, jpath):
        if path.exists():
            raise FileExistsError(
                f"{path} already exists: a registered protocol is never overwritten; use a new "
                "candidate name for a changed protocol"
            )
    text = render_journal_entry(protocol)
    guard_holdout(text)
    _create_exclusive(ppath, protocol_to_json(protocol).encode("utf-8"))
    try:
        _create_exclusive(jpath, text.encode("utf-8"))
    except BaseException:
        ppath.unlink(missing_ok=True)
        raise
    return ppath, jpath


def complete_registration(paper_dir: Path, candidate: str, journal_dir: Path) -> Path | None:
    """If ``protocol.json`` exists but its journal entry does not (the registration was
    interrupted between the two writes), write the entry from the frozen protocol and return
    its path; None when nothing is missing. The caller must have checked that the dry-run bot
    has not traded yet."""
    ppath = protocol_path(paper_dir, candidate)
    if not ppath.is_file():
        return None
    try:
        protocol = protocol_from_dict(json.loads(ppath.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolChangedError(f"{ppath} is not a valid protocol: {exc}") from None
    jpath = journal_path(protocol, journal_dir)
    if jpath.exists():
        return None
    text = render_journal_entry(protocol)
    guard_holdout(text)
    _create_exclusive(jpath, text.encode("utf-8"))
    return jpath


def find_journal_entry(protocol: PaperProtocol, journal_dir: Path) -> Path:
    path = journal_path(protocol, journal_dir)
    if not path.is_file():
        raise ProtocolChangedError(
            f"journal entry {path} not found: the protocol was not registered (or was renamed)"
        )
    return path


def load_registered_protocol(
    paper_dir: Path, candidate: str, journal_dir: Path
) -> tuple[PaperProtocol, Path]:
    """Load ``protocol.json`` and check its sha256 against the journal entry's frontmatter.
    ``FileNotFoundError`` without a protocol; :class:`ProtocolChangedError` on any mismatch."""
    ppath = protocol_path(paper_dir, candidate)
    if not ppath.is_file():
        raise FileNotFoundError(f"{ppath} not found; run `prepare` first")
    try:
        protocol = protocol_from_dict(json.loads(ppath.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolChangedError(f"{ppath} is not a valid protocol: {exc}") from None
    if protocol.candidate != candidate:
        raise ProtocolChangedError(
            f"{ppath} registers candidate {protocol.candidate!r}, not {candidate!r}"
        )
    jpath = find_journal_entry(protocol, journal_dir)
    front = parse_frontmatter(jpath.read_text(encoding="utf-8"))
    sha = protocol_sha256(protocol)
    if front.get("protocol_sha256") != sha:
        raise ProtocolChangedError(
            f"protocol changed after registration: {ppath} hashes to {sha[:12]}, the journal "
            f"entry {jpath.name} registered {str(front.get('protocol_sha256'))[:12]}"
        )
    return protocol, jpath


def _final_from_text(text: str) -> dict[str, Any] | None:
    front = parse_frontmatter(text)
    outcome = front.get("outcome")
    if outcome not in FINAL_VERDICTS:
        return None
    return {
        "outcome": outcome,
        "verdict_asof": front.get("verdict_asof"),
        "verdict_protocol_sha256": front.get("verdict_protocol_sha256"),
    }


def recorded_final_verdict(journal: Path) -> dict[str, Any] | None:
    """``{outcome, verdict_asof, verdict_protocol_sha256}`` when the journal entry's
    frontmatter already holds a final verdict; None while it is pending."""
    return _final_from_text(journal.read_text(encoding="utf-8"))


_COMPARISON_TAIL = re.compile(r" Comparison: `(comparison-\d{4}-\d{2}-\d{2}\.json)`\.$")


def recorded_final_comparison(journal: Path) -> str | None:
    """The comparison file name (``comparison-YYYY-MM-DD.json``) that the recorded FINAL
    verdict's journal line names, or None (no final verdict, or its line names no file). The
    final line is the last line of the verdicts section: nothing is appended after it."""
    text = journal.read_text(encoding="utf-8")
    recorded = _final_from_text(text)
    if recorded is None or VERDICTS_HEADING not in text:
        return None
    section = text.split(VERDICTS_HEADING, 1)[1]
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    if not lines or f"**{recorded['outcome']}**" not in lines[-1]:
        return None
    m = _COMPARISON_TAIL.search(lines[-1])
    return m.group(1) if m else None


def append_verdict(journal: Path, verdict: Verdict, *, asof: int, comparison: str = "") -> str:
    """Append one dated verdict line under the verdicts heading (the registered protocol
    section is never edited). The first final verdict (pass / fail / inconclusive) also sets
    the frontmatter ``outcome`` with ``verdict_asof`` and ``verdict_protocol_sha256``; it is
    write-once: :class:`FinalVerdictRecordedError` once a final verdict is recorded. The read,
    check and write happen under an exclusive lock, and the new text goes to a fresh fsynced
    temporary file that replaces the entry atomically. Returns the line."""
    line = (
        f"- {datetime.now(UTC).strftime('%Y-%m-%d')} (data to {_utc(asof)}): "
        f"**{verdict.verdict}**, {verdict.elapsed_days:.1f} days. {verdict.headline}"
    )
    if verdict.alerts:
        line += " Alerts: " + "; ".join(verdict.alerts) + "."
    if comparison:
        line += f" Comparison: `{comparison}`."
    guard_holdout(line)
    with _journal_lock(journal):
        text = journal.read_text(encoding="utf-8")
        recorded = _final_from_text(text)
        if recorded is not None:
            raise FinalVerdictRecordedError(
                f"{journal.name} already records the final verdict {recorded['outcome']!r} "
                f"(data to {recorded['verdict_asof']}); it is never changed"
            )
        if VERDICTS_HEADING not in text:
            raise ProtocolChangedError(f"{journal} has no verdicts section; was it edited by hand?")
        new = text.rstrip("\n") + "\n" + line + "\n"
        if verdict.final:
            head, sep, body = new.partition("\n---\n")
            stamp = (
                f"outcome: {_yaml(verdict.verdict)}\n"
                f"verdict_asof: {_yaml(datetime.fromtimestamp(asof, UTC).isoformat())}\n"
                f"verdict_protocol_sha256: {_yaml(verdict.protocol_sha256)}"
            )
            head = re.sub(r"^outcome: .*$", lambda _: stamp, head, count=1, flags=re.M)
            new = head + sep + body
        _replace_atomic(journal, new.encode("utf-8"))
    return line


__all__ = [
    "DEFAULT_CANDLE_HOST",
    "DEFAULT_PROTOCOL_CONFIG",
    "EARLY_LABEL",
    "FINAL_VERDICTS",
    "LATE_LABEL",
    "LEDGER_MIN_SCHEMA",
    "LOCKED_SPLIT_STATES",
    "SIMULATED_FALSE_FAIL_RANGE",
    "UNMATCHED_LAB_LABEL",
    "UNMATCHED_PAPER_LABEL",
    "BootstrapEstimate",
    "Check",
    "DataRule",
    "DesignStats",
    "Fill",
    "FinalVerdictRecordedError",
    "LedgerLink",
    "MatchRule",
    "ObservedPaper",
    "PaperProtocol",
    "ProtocolChangedError",
    "ProtocolConfig",
    "Verdict",
    "append_verdict",
    "bootstrap_false_fail",
    "build_protocol",
    "check_not_registered_elsewhere",
    "complete_registration",
    "design_blocks",
    "design_stats",
    "entry_band",
    "evaluate",
    "exposure_band",
    "false_fail_text",
    "gross_fill_windows",
    "irregular_allowance",
    "journal_path",
    "ledger_link",
    "load_protocol_config",
    "load_registered_protocol",
    "locked_split_text",
    "median_bounds",
    "min_testable_sharpe",
    "poisson_band",
    "predictive_band",
    "protocol_from_dict",
    "protocol_path",
    "protocol_sha256",
    "protocol_to_dict",
    "protocol_to_json",
    "read_ledger_link",
    "recorded_final_comparison",
    "recorded_final_verdict",
    "register_protocol",
    "registered_hashes",
    "render_journal_entry",
    "resolve_claim",
    "sharpe_moments",
    "weekly_shortfall",
]
