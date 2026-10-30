"""Blind snapshot of an entry setup and the batched Jev take/skip call (PLAN.md Phase 17).

The Jev shadow judge watches the HC exploratory paper bots and never steers them. For each
closed 4h bar it shows Jev every candidate whose entry signal fired, as a *blind* snapshot:
rounded ratios computed here in pandas/numpy from closed candles at or before the signal bar
(no symbol, no timestamps, no price or volume levels), so the model cannot recognise the coin
or the date and cannot look ahead. All arithmetic stays in Python; Jev only answers typed
questions about the precomputed figures.

* :func:`blind_snapshot` builds the snapshot for one candidate at signal bar ``at``.
* :func:`label_setups` / :func:`build_state` / :func:`build_questions` turn the bar's snapshots
  into ONE batched Jev request. Labels ``s1..sk`` are assigned by snapshot hash, so a label does
  not consistently stand for one coin.
* :func:`judge` makes exactly one :meth:`JevClient.ask` call per bar and returns verdicts keyed
  by candidate, or ``None`` (fail open: the caller logs every setup of that bar as
  ``unavailable``, never as a skip).

The question texts and context are module constants and :data:`QUESTIONS_SHA256` pins them, so
the pre-registration can freeze exactly what Jev was asked. :func:`snapshot_fingerprint` pins
the *behaviour* of the formulas (EMA spans, return bars, rounding, Wilder smoothing, history
length, label order, verdict validity) on fixed synthetic inputs, so a genesis that freezes it
refuses any formula change mid-test. The API key is read only by
:class:`research.jev.JevClient` from ``TYPESAFE_API_KEY``; nothing here sees, logs or returns it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from functools import cache
from typing import Any

import numpy as np
import pandas as pd

from research.jev import JevClient, JevResponse

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION: int = 1
# EMA200 plus one previous close: the longest look-back the snapshot reads.
MIN_HISTORY_BARS: int = 201

SNAPSHOT_FIELDS: tuple[str, ...] = (
    "ret_1_pct",
    "ret_6_pct",
    "ret_42_pct",
    "atr14_pct",
    "dist_ema20_pct",
    "dist_ema50_pct",
    "dist_ema200_pct",
    "rsi14",
    "vol_ratio_20",
    "btc_trend_pct",
    "btc_ret_42_pct",
)

_RETURN_BARS = (1, 6, 42)
_EMA_SPANS = (20, 50, 200)
_ATR_PERIOD = 14
_RSI_PERIOD = 14
_VOL_WINDOW = 20
_BTC_EMA_SPAN = 50
_BTC_RETURN_BARS = 42
_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

CONTEXT: str = (
    "Long-only, rule-generated entry setups on crypto 4h bars. Each figure is a rounded ratio "
    "at the signal bar's close: ret_N_pct = percent return over the last N bars; atr14_pct = "
    "14-bar average true range as a percent of close; dist_emaN_pct = percent distance of "
    "close above its N-bar EMA; rsi14 = 14-bar RSI (0-100); vol_ratio_20 = this bar's volume "
    "over the mean of the previous 20 bars; btc_trend_pct = BTC close vs its 50-bar EMA in "
    "percent; btc_ret_42_pct = BTC's 42-bar return in percent. A null figure is unavailable. "
    "No symbol, dates or prices are given. Judge each setup independently."
)

TAKE_INSTRUCTIONS: str = (
    "For setup {label} in setups: should this long entry be taken or skipped, judged only from "
    "its figures?"
)
TAKE_CRITERIA: dict[str, str] = {
    "take": "The figures favour taking this long entry: it is more likely to work than to fail.",
    "skip": "The figures favour skipping this long entry: it is more likely to fail than to work.",
}
FAKEOUT_INSTRUCTIONS: str = (
    "For setup {label} in setups: is this long setup a fake-out that fails soon after entry "
    "(price reverses against the long within a few bars)?"
)
CONF_INSTRUCTIONS: str = (
    "For setup {label} in setups: how confident are you in your take/skip call for it, from 0 "
    "(no confidence) to 1 (fully confident)?"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------- snapshot


def _history(frame: pd.DataFrame, at: pd.Timestamp, name: str) -> pd.DataFrame:
    """Rows of ``frame`` with index <= ``at``; validates layout, ``at`` and history length."""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{name} must be a pandas DataFrame")
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError(f"{name} index must be a tz-aware UTC DatetimeIndex of bar open times")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError(f"{name} index must be strictly increasing (sorted, no duplicate bars)")
    missing = [c for c in _REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{name} is missing columns {missing}")
    if at not in index:
        raise ValueError(f"signal bar at={at.isoformat()} is not in the {name} index")
    hist = frame.loc[index <= at, list(_REQUIRED_COLUMNS)].astype(np.float64)
    if len(hist) < MIN_HISTORY_BARS:
        raise ValueError(
            f"{name} has {len(hist)} bars up to at={at.isoformat()}; "
            f"need at least {MIN_HISTORY_BARS}"
        )
    return hist


def _wilder(x: np.ndarray, period: int) -> float:
    """Last value of Wilder's smoother (TradingView ``ta.rma``): SMA seed, then alpha 1/period.

    Leading NaNs are skipped; the seed is the mean of the first ``period`` finite values.
    """
    finite = np.flatnonzero(~np.isnan(x))
    if finite.size == 0:
        return math.nan
    start = int(finite[0])
    seed_at = start + period - 1
    if seed_at >= x.shape[0]:
        return math.nan
    tail = np.array(x[seed_at:], dtype=np.float64, copy=True)
    tail[0] = x[start : seed_at + 1].mean()
    smoothed = pd.Series(tail).ewm(alpha=1.0 / period, adjust=False).mean()
    return float(smoothed.iloc[-1])


def _pct_return(close: np.ndarray, bars: int) -> float:
    with np.errstate(divide="ignore", invalid="ignore"):
        return float((close[-1] / close[-1 - bars] - 1.0) * 100.0)


def _ema_dist_pct(close: np.ndarray, span: int) -> float:
    ema = float(pd.Series(close).ewm(span=span, adjust=False).mean().iloc[-1])
    with np.errstate(divide="ignore", invalid="ignore"):
        return float((close[-1] / ema - 1.0) * 100.0)


def _atr_pct(hist: pd.DataFrame, period: int) -> float:
    high = hist["high"].to_numpy()
    low = hist["low"].to_numpy()
    close = hist["close"].to_numpy()
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = _wilder(tr, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(atr / close[-1] * 100.0)


def _rsi(close: np.ndarray, period: int) -> float:
    change = np.diff(close, prepend=np.nan)
    gain = _wilder(np.maximum(change, 0.0), period)
    loss = _wilder(np.maximum(-change, 0.0), period)
    if math.isnan(gain) or math.isnan(loss):
        return math.nan
    if loss == 0.0:
        return 100.0
    if gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _vol_ratio(volume: np.ndarray, window: int) -> float:
    mean_prev = float(volume[-1 - window : -1].mean())
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.float64(volume[-1]) / np.float64(mean_prev))


def _rounded(value: float, digits: int) -> float | None:
    if not math.isfinite(value):
        return None
    return round(value, digits) + 0.0  # + 0.0 turns -0.0 into 0.0


def _rounded_int(value: float) -> int | None:
    if not math.isfinite(value):
        return None
    return round(float(value))


def blind_snapshot(
    candles: pd.DataFrame, btc: pd.DataFrame, at: pd.Timestamp
) -> dict[str, float | int | None]:
    """Blind snapshot of the candidate's setup at signal bar ``at`` (its open time).

    Every figure is computed from bar ``at``'s close and earlier closed rows only (rows after
    ``at`` in either frame are ignored). Output keys are exactly :data:`SNAPSHOT_FIELDS`;
    values are rounded ratios (``None`` when not finite), never a symbol, time or level.
    """
    if not isinstance(at, pd.Timestamp) or at.tzinfo is None:
        raise ValueError("at must be a tz-aware pandas Timestamp (the signal bar's open time)")
    hist = _history(candles, at, "candles")
    btc_hist = _history(btc, at, "btc")
    close = hist["close"].to_numpy()
    btc_close = btc_hist["close"].to_numpy()

    out: dict[str, float | int | None] = {}
    for bars in _RETURN_BARS:
        out[f"ret_{bars}_pct"] = _rounded(_pct_return(close, bars), 1)
    out["atr14_pct"] = _rounded(_atr_pct(hist, _ATR_PERIOD), 2)
    for span in _EMA_SPANS:
        out[f"dist_ema{span}_pct"] = _rounded(_ema_dist_pct(close, span), 1)
    out["rsi14"] = _rounded_int(_rsi(close, _RSI_PERIOD))
    out["vol_ratio_20"] = _rounded(_vol_ratio(hist["volume"].to_numpy(), _VOL_WINDOW), 1)
    out["btc_trend_pct"] = _rounded(_ema_dist_pct(btc_close, _BTC_EMA_SPAN), 1)
    out["btc_ret_42_pct"] = _rounded(_pct_return(btc_close, _BTC_RETURN_BARS), 1)
    assert tuple(out) == SNAPSHOT_FIELDS
    return out


def snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    """sha256 hex of the snapshot's canonical JSON (sorted keys, compact, no NaN)."""
    return _sha256(dict(snapshot))


# ------------------------------------------------------------------------- Jev request


def label_setups(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Map candidate name -> ``"s1".."sk"``, ordered by (snapshot sha256, candidate name).

    The order does not depend on dict insertion order, and since it follows the snapshot hash a
    label does not consistently stand for one coin.
    """
    keyed = sorted((snapshot_sha256(snap), str(name)) for name, snap in snapshots.items())
    return {name: f"s{i}" for i, (_sha, name) in enumerate(keyed, start=1)}


def _label_number(label: str) -> tuple[int, str]:
    digits = label[1:]
    return (int(digits), label) if digits.isdigit() else (0, label)


def build_state(labelled: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Jev ``state``: the fixed :data:`CONTEXT` plus ``{label: snapshot}`` in label order."""
    setups = {label: dict(labelled[label]) for label in sorted(labelled, key=_label_number)}
    return {"context": CONTEXT, "setups": setups}


def build_questions(labels: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Three questions per label: ``{L}_take`` (choice), ``{L}_fakeout`` (noul), ``{L}_conf``."""
    questions: dict[str, dict[str, Any]] = {}
    for label in labels:
        questions[f"{label}_take"] = {
            "type": "choice",
            "instructions": TAKE_INSTRUCTIONS.format(label=label),
            "criteria": dict(TAKE_CRITERIA),
        }
        questions[f"{label}_fakeout"] = {
            "type": "noul",
            "instructions": FAKEOUT_INSTRUCTIONS.format(label=label),
        }
        questions[f"{label}_conf"] = {
            "type": "score",
            "instructions": CONF_INSTRUCTIONS.format(label=label),
        }
    return questions


QUESTIONS_SHA256: str = _sha256({"context": CONTEXT, "questions": build_questions(["L"])})


# ------------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class JevVerdict:
    """Jev's answer for one setup."""

    choice: str  # "take" | "skip"
    choice_confidence: float | None
    fakeout: float  # noul P(fake-out)
    score: float  # confidence score

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def parse_verdicts(resp: JevResponse | None, labels: Iterable[str]) -> dict[str, JevVerdict] | None:
    """Verdicts keyed by label, or ``None`` if ``resp`` is ``None`` or ANY label is invalid."""
    if resp is None:
        return None
    out: dict[str, JevVerdict] = {}
    for label in labels:
        got = resp.choice(f"{label}_take")
        if got is None or got[0] not in TAKE_CRITERIA:
            logger.warning("jev shadow: setup %s has no valid take/skip choice", label)
            return None
        fakeout = resp.noul(f"{label}_fakeout")
        if fakeout is None or not (math.isfinite(fakeout) and 0.0 <= fakeout <= 1.0):
            logger.warning("jev shadow: setup %s has no valid fake-out probability", label)
            return None
        conf_ans = resp.answers.get(f"{label}_conf")
        score = (
            _finite_number(conf_ans.get("score"))
            if isinstance(conf_ans, dict) and conf_ans.get("type") == "score"
            else None
        )
        if score is None:
            logger.warning("jev shadow: setup %s has no finite confidence score", label)
            return None
        choice, confidence, _probs = got
        out[label] = JevVerdict(
            choice=choice, choice_confidence=confidence, fakeout=fakeout, score=score
        )
    return out


def judge(
    client: JevClient,
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, JevVerdict] | None, str | None, int]:
    """One batched Jev call for one closed bar's setups.

    Returns ``(verdicts keyed by candidate | None, served model id | None, latency_ms)``.
    ``None`` verdicts mean Jev was unavailable or any answer was invalid (fail open: log every
    setup of the bar as ``unavailable``). The model id is the one Jev reports SERVING
    (``resp.model``), never the requested alias: it is ``None`` when Jev gave no response or
    named no model (blank), so a floating alias can never stand in for the served judge. An
    empty ``snapshots`` returns ``({}, None, 0)`` without calling Jev.
    """
    if not snapshots:
        return {}, None, 0
    labels = label_setups(snapshots)
    by_label = {labels[name]: snapshots[name] for name in labels}
    ordered = sorted(by_label, key=_label_number)
    state = build_state(by_label)
    questions = build_questions(ordered)
    started = clock()
    resp = client.ask(state, questions)
    latency_ms = max(0, round((clock() - started) * 1000.0))
    if resp is None:
        logger.warning("jev shadow: Jev unavailable for %d setup(s)", len(ordered))
        return None, None, latency_ms
    served = resp.model.strip() if isinstance(resp.model, str) else ""
    model = served or None  # never the requested alias
    parsed = parse_verdicts(resp, ordered)
    if parsed is None:
        return None, model, latency_ms
    return {name: parsed[label] for name, label in labels.items()}, model, latency_ms


# ------------------------------------------------------------------------- fingerprint

# Fixed inputs of :func:`snapshot_fingerprint`. The two frames are SYNTHETIC random walks drawn
# here from ``np.random.default_rng(SNAPSHOT_FINGERPRINT_INPUTS_SEED)`` (candles first, then
# BTC, from one generator); they are not market data. Changing any of these values changes the
# fingerprint, which is the point: only the code under test may vary them.
SNAPSHOT_FINGERPRINT_INPUTS_SEED: int = 20261029
_FP_START = "2024-01-01T00:00:00+00:00"  # first bar open time (UTC)
_FP_BARS = 320  # rows per frame
# Signal bar positions 250..299: one rounded snapshot can hide a small formula change (an EMA
# span of 51 instead of 50 may round to the same 0.1%), fifty cannot. Every one has 20+ later
# rows that the snapshot must ignore.
_FP_AT_POSITIONS = tuple(range(250, 300))
_FP_CANDLES_BASE = 100.0
_FP_BTC_BASE = 30000.0
_FP_LABEL_SNAPSHOTS: dict[str, dict[str, float | int | None]] = {
    "a": {"x": 1.0, "y": None},
    "b": {"x": -2.5, "y": 3},
    "c": {"x": 1.0, "y": None},  # same snapshot as "a": the name breaks the tie
}
_FP_PARSE_LABELS = ("s1", "s2")
_FP_VALID_ANSWERS: dict[str, dict[str, Any]] = {
    "s1_take": {
        "type": "choice",
        "choice": "skip",
        "confidence": 0.8,
        "probabilities": {"take": 0.2, "skip": 0.8},
    },
    "s1_fakeout": {"type": "noul", "noul": 0.6},
    "s1_conf": {"type": "score", "score": 0.4},
    "s2_take": {"type": "choice", "choice": "take"},
    "s2_fakeout": {"type": "noul", "noul": 0.1},
    "s2_conf": {"type": "score", "score": 0.9},
}
# The same answers with s2's confidence score missing: parse_verdicts must return None.
_FP_INVALID_ANSWERS: dict[str, dict[str, Any]] = {
    **_FP_VALID_ANSWERS,
    "s2_conf": {"type": "score"},
}


def _fingerprint_frame(rng: np.random.Generator, base: float) -> pd.DataFrame:
    """Synthetic 4h OHLCV random walk (not market data) from ``rng``, in a fixed draw order."""
    n = _FP_BARS
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.concatenate(([base], close[:-1]))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.01, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.01, n))
    volume = rng.uniform(100.0, 1000.0, n)
    index = pd.date_range(pd.Timestamp(_FP_START), periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    )


def _fingerprint_payload() -> dict[str, Any]:
    rng = np.random.default_rng(SNAPSHOT_FINGERPRINT_INPUTS_SEED)
    candles = _fingerprint_frame(rng, _FP_CANDLES_BASE)
    btc = _fingerprint_frame(rng, _FP_BTC_BASE)
    snapshots = [blind_snapshot(candles, btc, candles.index[i]) for i in _FP_AT_POSITIONS]

    def parsed(answers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        resp = JevResponse(model="fingerprint", answers=answers, usage={})
        out = parse_verdicts(resp, _FP_PARSE_LABELS)
        return None if out is None else {label: v.as_dict() for label, v in out.items()}

    was_disabled = logger.disabled
    logger.disabled = True  # the invalid case warns by design; keep it out of the logs
    try:
        parse = {"valid": parsed(_FP_VALID_ANSWERS), "invalid": parsed(_FP_INVALID_ANSWERS)}
    finally:
        logger.disabled = was_disabled
    return {
        "version": SNAPSHOT_VERSION,
        "fields": list(SNAPSHOT_FIELDS),
        "min_history": MIN_HISTORY_BARS,
        "snapshot": snapshots,
        "labels": label_setups(_FP_LABEL_SNAPSHOTS),
        "parse": parse,
    }


@cache
def snapshot_fingerprint() -> str:
    """sha256 hex of the snapshot code's behaviour on fixed synthetic inputs.

    Canonical JSON (sorted keys, compact, no NaN) of ``{"version", "fields", "min_history",
    "snapshot", "labels", "parse"}``: the list of :func:`blind_snapshot` results on two
    synthetic 4h frames drawn from :data:`SNAPSHOT_FINGERPRINT_INPUTS_SEED` at signal bars
    250..299 (later rows present, so look-ahead would show), the :func:`label_setups` order of
    three fixed snapshots, and :func:`parse_verdicts` on one valid and one invalid (missing
    score) fixed response. Any formula change moves it; the genesis
    freezes it so a mid-test change is refused. Cached: call ``cache_clear()`` after patching.
    """
    return _sha256(_fingerprint_payload())
