"""Jev gates for the research loop: dedupe, sweep triage, done-check.

(The source screen lives in :mod:`research.screening`.)

Every gate fails open: if Jev is missing or unavailable it logs one line and returns a
``decided_by="fallback"`` (or ``"unknown"``) verdict. No gate raises on Jev problems or blocks
the loop. Arithmetic -- neighbour medians, MADs, trade counts -- is done here in Python; Jev only
gives qualitative labels from the precomputed facts.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from research.jev import JevClient, JevResponse
from research.screening import sanitize_untrusted

logger = logging.getLogger(__name__)

# Dedupe: Jev's probability for the chosen existing idea must reach this to call it a duplicate.
DUPLICATE_THRESHOLD = 0.8
MAX_CHOICE_OPTIONS = 255  # Jev choice question limit, including the "none" option
IDEA_TEXT_CHARS = 600

# Sweep triage. MIN_TRADES matches lab.search.optuna_search.MIN_TRADES (config/lab.toml
# optuna.min_trades): fewer trades than this is too little evidence to trust a Sharpe.
MIN_TRADES = 30
NEIGHBOURS_K = 8  # nearest trials in normalised parameter space used as the local surface
SPIKE_MADS = 2.0  # isolated spike: Sharpe > neighbour median + SPIKE_MADS * MAD
# MAD floor (Sharpe units) so a perfectly flat neighbourhood doesn't turn a tiny excess into a
# "spike"; 0.05 is well below any Sharpe difference the critic would act on.
MAD_FLOOR = 0.05
MIN_TRIALS_FOR_SURFACE = 3
SUSPICIOUS_THRESHOLD = 0.5
MAX_TRIALS_PER_CALL = 100

DONE_THRESHOLD = 0.5
_STUDY_KEY = "_study"  # reserved params key in ledger rows (study design, not a parameter)


def _fallback(gate: str, reason: str) -> None:
    logger.warning("jev gate %s falling back: %s", gate, reason)


def _jev_ready(gate: str, jev: JevClient | None) -> bool:
    if jev is None or not jev.enabled:
        _fallback(gate, "Jev not configured")
        return False
    return True


# ------------------------------------------------------------------------- dedupe


@dataclass(frozen=True)
class DuplicateVerdict:
    duplicate_of: int | None
    probability: float | None
    decided_by: Literal["jev", "fallback"]


def check_duplicate(
    jev: JevClient | None, hypothesis: str, existing: Sequence[tuple[int, str]]
) -> DuplicateVerdict:
    """Ask Jev whether ``hypothesis`` is the same idea as one in ``existing`` ``(id, text)``.

    ``probability`` is Jev's probability for the best-matching existing idea; the verdict is a
    duplicate only when that reaches :data:`DUPLICATE_THRESHOLD`. Only the first 254 distinct
    ids are offered (Jev's 255-option limit includes "none"); pass the most relevant first.
    Fallback: ``duplicate_of=None`` -- the idea library's local exact/near-dup check still runs.
    """
    fallback = DuplicateVerdict(duplicate_of=None, probability=None, decided_by="fallback")
    if not existing:
        logger.info("jev gate dedupe: no existing ideas, nothing to compare")
        return fallback
    if not _jev_ready("dedupe", jev):
        return fallback
    assert jev is not None

    options: dict[str, str] = {}
    ids: dict[str, int] = {}
    for idea_id, text in existing:
        key = str(idea_id)
        if key in options:
            continue
        if len(options) >= MAX_CHOICE_OPTIONS - 1:
            logger.info("jev gate dedupe: %d ideas offered, rest skipped", len(options))
            break
        options[key] = sanitize_untrusted(text, max_chars=IDEA_TEXT_CHARS)
        ids[key] = idea_id
    options["none"] = "The new hypothesis is not the same idea as any listed existing idea."

    question = {
        "is_duplicate_of": {
            "type": "choice",
            "instructions": (
                "Is the new hypothesis essentially the same trading idea (same signal logic and "
                "economic rationale, even if worded differently or with other parameter values) "
                "as one of the existing ideas? Choose that idea's id, or 'none'."
            ),
            "criteria": options,
        }
    }
    state = {"new_hypothesis": sanitize_untrusted(hypothesis, max_chars=IDEA_TEXT_CHARS)}
    resp = jev.ask(state, question)
    if resp is None:
        _fallback("dedupe", "Jev unavailable")
        return fallback
    got = resp.choice("is_duplicate_of")
    if got is None:
        _fallback("dedupe", "no choice answer")
        return fallback
    choice, confidence, probs = got

    id_probs = {k: v for k, v in probs.items() if k in ids}
    best: str | None
    p_best: float | None
    if id_probs:
        best = max(id_probs, key=lambda k: id_probs[k])
        p_best = id_probs[best]
    elif choice in ids:
        best, p_best = choice, confidence
    else:
        best, p_best = None, None
    if best is not None and p_best is not None and p_best >= DUPLICATE_THRESHOLD:
        return DuplicateVerdict(duplicate_of=ids[best], probability=p_best, decided_by="jev")
    return DuplicateVerdict(duplicate_of=None, probability=p_best, decided_by="jev")


# ------------------------------------------------------------------------- sweep triage


@dataclass(frozen=True)
class TriageRow:
    index: int
    flag: Literal["ok", "suspicious", "unknown"]
    reasons: list[str]


@dataclass(frozen=True)
class _TrialFacts:
    index: int
    sharpe: float | None
    n_trades: int | None
    neighbour_median: float | None
    neighbour_mad: float | None
    isolated_spike: bool
    low_trade_count: bool
    problem: str | None  # why facts couldn't be computed (missing Sharpe, failed trial)

    def python_reasons(self) -> list[str]:
        out: list[str] = []
        if self.problem:
            out.append(self.problem)
        if self.isolated_spike:
            assert self.sharpe is not None and self.neighbour_median is not None
            out.append(
                f"isolated spike: Sharpe {self.sharpe:.3g} vs neighbour median "
                f"{self.neighbour_median:.3g} (MAD {self.neighbour_mad:.3g})"
            )
        if self.low_trade_count:
            out.append(f"low trade count: {self.n_trades} < {MIN_TRADES}")
        return out

    def as_state(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return None if x is None else round(x, 4)

        excess = (
            None
            if self.sharpe is None or self.neighbour_median is None
            else self.sharpe - self.neighbour_median
        )
        return {
            "i": self.index,
            "sharpe": r(self.sharpe),
            "neighbour_median_sharpe": r(self.neighbour_median),
            "neighbour_mad_sharpe": r(self.neighbour_mad),
            "sharpe_minus_neighbour_median": r(excess),
            "isolated_spike": self.isolated_spike,
            "n_trades": self.n_trades,
            "low_trade_count": self.low_trade_count,
            "problem": self.problem,
        }


def _finite(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _trial_sharpe(trial: Mapping[str, Any]) -> float | None:
    for key in ("sharpe", "sharpe_per_bar"):
        if key in trial:
            return _finite(trial[key])
    return None


def _trial_params(trial: Mapping[str, Any]) -> dict[str, Any]:
    params = trial.get("params")
    if not isinstance(params, Mapping):
        return {}
    return {str(k): v for k, v in params.items() if k != _STUDY_KEY}


def _distance(
    a: Mapping[str, Any], b: Mapping[str, Any], ranges: Mapping[str, tuple[float, float]]
) -> float:
    """Euclidean distance on min-max normalised numeric params; +1 per differing other param."""
    total = 0.0
    for key in set(a) | set(b):
        if key in ranges:
            fa, fb = _finite(a.get(key)), _finite(b.get(key))
            if fa is None or fb is None:
                total += 1.0
                continue
            lo, hi = ranges[key]
            span = hi - lo
            d = 0.0 if span <= 0 else (fa - fb) / span
            total += d * d
        elif a.get(key) != b.get(key):
            total += 1.0
    return math.sqrt(total)


def _compute_facts(trials: Sequence[Mapping[str, Any]]) -> list[_TrialFacts]:
    params = [_trial_params(t) for t in trials]
    sharpes = [_trial_sharpe(t) for t in trials]
    statuses = [t.get("status") for t in trials]

    numeric_values: dict[str, list[float]] = {}
    non_numeric: set[str] = set()
    for p in params:
        for k, v in p.items():
            f = _finite(v)
            if f is None:
                non_numeric.add(k)
            else:
                numeric_values.setdefault(k, []).append(f)
    ranges = {k: (min(vs), max(vs)) for k, vs in numeric_values.items() if k not in non_numeric}
    usable = [i for i, s in enumerate(sharpes) if s is not None and statuses[i] in (None, "ok")]

    facts: list[_TrialFacts] = []
    for i, trial in enumerate(trials):
        n_float = _finite(trial.get("n_trades"))
        n_trades = int(n_float) if n_float is not None else None
        ok_status = statuses[i] in (None, "ok")
        low = ok_status and n_trades is not None and n_trades < MIN_TRADES
        sharpe = sharpes[i]
        problem: str | None = None
        if not ok_status:
            problem = f"trial status {statuses[i]!r}"
        elif sharpe is None:
            problem = "missing or non-finite Sharpe"
        if n_trades is None and problem is None:
            problem = "missing trade count"

        median: float | None = None
        mad: float | None = None
        spike = False
        if sharpe is not None and i in usable:
            others = [j for j in usable if j != i]
            if len(others) >= MIN_TRIALS_FOR_SURFACE - 1:
                others.sort(key=lambda j: (_distance(params[i], params[j], ranges), j))
                neigh = [sharpes[j] for j in others[:NEIGHBOURS_K]]
                vals = [v for v in neigh if v is not None]
                median = statistics.median(vals)
                mad = statistics.median(abs(v - median) for v in vals)
                spike = sharpe > median + SPIKE_MADS * max(mad, MAD_FLOOR)
        facts.append(
            _TrialFacts(
                index=i,
                sharpe=sharpe,
                n_trades=n_trades,
                neighbour_median=median,
                neighbour_mad=mad,
                isolated_spike=spike,
                low_trade_count=low,
                problem=problem,
            )
        )
    return facts


def _python_row(f: _TrialFacts) -> TriageRow:
    if f.isolated_spike or f.low_trade_count:
        return TriageRow(index=f.index, flag="suspicious", reasons=f.python_reasons())
    return TriageRow(index=f.index, flag="unknown", reasons=f.python_reasons())


def _jev_triage(jev: JevClient, chunk: Sequence[_TrialFacts]) -> JevResponse | None:
    state = {
        "note": (
            "Facts about parameter-sweep trials, all computed by our code. Neighbours are the "
            "nearest trials in parameter space. Judge each trial from these facts only."
        ),
        "min_trades": MIN_TRADES,
        "trials": [f.as_state() for f in chunk],
    }
    questions = {
        f"t{f.index}_suspicious": {
            "type": "noul",
            "instructions": (
                f"Trial {f.index}: from its facts, is this result suspicious -- likely luck or "
                "overfitting (a lone spike above its neighbours, too few trades, or otherwise "
                "implausible) rather than part of a stable parameter region?"
            ),
        }
        for f in chunk
    }
    return jev.ask(state, questions)


def triage_sweep(jev: JevClient | None, trials: Sequence[Mapping[str, Any]]) -> list[TriageRow]:
    """Flag suspicious sweep trials. One row per input trial, same order (``index``).

    Each trial is a mapping with ``params`` (mapping), ``sharpe`` (or ``sharpe_per_bar``), and
    ``n_trades``; an optional ``status`` other than ``"ok"`` marks a failed trial. Python
    computes the neighbour surface and hard rules; an isolated spike or ``n_trades < 30`` is
    always "suspicious". Jev, when available, labels the rest ok/suspicious from those facts.
    Fallback: Python rules only, otherwise "unknown".
    """
    if not trials:
        return []
    facts = _compute_facts(trials)
    if not _jev_ready("sweep-triage", jev):
        return [_python_row(f) for f in facts]
    assert jev is not None

    rows: list[TriageRow] = []
    for start in range(0, len(facts), MAX_TRIALS_PER_CALL):
        chunk = facts[start : start + MAX_TRIALS_PER_CALL]
        resp = _jev_triage(jev, chunk)
        if resp is None:
            _fallback(
                "sweep-triage", f"Jev unavailable for trials {chunk[0].index}-{chunk[-1].index}"
            )
            rows.extend(_python_row(f) for f in chunk)
            continue
        for f in chunk:
            p = resp.noul(f"t{f.index}_suspicious")
            if p is None:
                rows.append(_python_row(f))
                continue
            reasons = f.python_reasons()
            hard = f.isolated_spike or f.low_trade_count
            if p >= SUSPICIOUS_THRESHOLD:
                reasons.append(f"jev: suspicious (p={p:.2f})")
            if hard or p >= SUSPICIOUS_THRESHOLD:
                rows.append(TriageRow(index=f.index, flag="suspicious", reasons=reasons))
            elif f.problem:
                rows.append(TriageRow(index=f.index, flag="unknown", reasons=reasons))
            else:
                reasons.append(f"jev: looks ok (p_suspicious={p:.2f})")
                rows.append(TriageRow(index=f.index, flag="ok", reasons=reasons))
    return rows


# ------------------------------------------------------------------------- done-check


@dataclass(frozen=True)
class DoneVerdict:
    done: bool | None
    probability: float | None
    decided_by: Literal["jev", "fallback"]


def done_check(jev: JevClient | None, summary: Mapping[str, Any]) -> DoneVerdict:
    """Ask Jev whether the run has honestly exhausted its plan and should stop.

    ``summary`` holds facts computed by the orchestrator (budgets used/remaining, hypotheses
    tried, candidates, last critic verdicts). Fallback: ``done=None`` -- the orchestrator decides
    by its budgets. This gate never overrides a budget; it is advice only.
    """
    fallback = DoneVerdict(done=None, probability=None, decided_by="fallback")
    if not _jev_ready("done-check", jev):
        return fallback
    assert jev is not None
    question = {
        "should_stop": {
            "type": "noul",
            "instructions": (
                "From this research-run summary, has the run honestly exhausted its plan -- the "
                "planned hypothesis families were tried or budgets are nearly used, and further "
                "iterations would mostly re-tune the same ideas -- so it should stop and write "
                "its report (including 'no edge found')?"
            ),
        }
    }
    resp = jev.ask(dict(summary), question)
    if resp is None:
        _fallback("done-check", "Jev unavailable")
        return fallback
    p = resp.noul("should_stop")
    if p is None:
        _fallback("done-check", "no answer")
        return fallback
    return DoneVerdict(done=p >= DONE_THRESHOLD, probability=p, decided_by="jev")
