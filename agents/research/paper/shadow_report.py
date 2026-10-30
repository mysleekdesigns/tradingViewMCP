"""Shadow-judge report (PLAN.md Phase 17): Jev-take versus Jev-skip paper trade returns.

The Jev shadow judge watches the HC paper bots and never steers them; its hash-chained
``decisions.jsonl`` records one verdict per signal before the fill bar opens. This module turns
that log plus the bots' dry-run databases into the pre-registered analysis frozen in the log's
genesis row (:func:`frozen_analysis`):

* :func:`load_trades` reads each genesis candidate's ``dryrun.sqlite`` (read-only, via
  :func:`research.paper.compare.read_dryrun_trades`, the same fills ``compare`` reads) and
  returns the candidates whose database could not be read as structured data
  (:class:`LoadedTrades`), never only as text.
* :func:`match_fills` pairs each judged decision with the candidate's paper trade that opened
  inside its fill bar ``[fill_open, fill_open + interval)``; each trade is used at most once.
* :func:`cluster_permutation_test` is the frozen two-sided test of the centred within-block
  difference ``T`` (:func:`within_block_effect`): ``mean(take) - mean(skip)`` centred on its
  exact null mean under the stratified permutation, so blocks with different take shares or
  mean returns cannot tilt the null away from 0. One Jev call per closed bar judges every
  signal of that bar together, so trades sharing a fill bar are one cluster. The test is
  stratified: whole label vectors are permuted only
  among clusters of the same size inside the same ``block_days`` block counted from the
  genesis start, so a regime that persists across bars (trades overlap, the BTC fields span 42
  bars) cannot by itself make the arms look different. It asks whether Jev discriminates
  between setups of the same week.
* :func:`build_report` applies the frozen evaluation time, model rule, strata, minimum sample
  and alpha (from the genesis row only, never from defaults) and :func:`render_report_markdown`
  writes a deterministic summary.

Evaluation time. The genesis analysis carries ``evaluate_at`` (the latest protocol decision end
plus ``evaluate_after_days``). Before it, a report is an "interim" look: descriptive numbers only,
no test, no p-value, no outcome. At or after it, the cutoff is ``evaluate_at`` itself (rows
written at or after it and trades opened at or after it are ignored; a trade not closed before it
counts as open and is excluded), so every ``asof >= evaluate_at`` gives the same report;
``trades_sha256`` (sha256 of the canonical matched-trade list) makes that checkable. A final
report refuses (:class:`FinalReportRefusedError`) while any genesis candidate's dry-run
database cannot be read, so a locked or missing database can never silently drop a candidate.

Model rule. Decision rows log the SERVED model id (``model``, ``None`` when Jev named none)
next to the ``requested_model``. The pin is :func:`model_pin` of the served id of the first
judged decision (by fill bar, then candidate) that has one: ``<name>-<major>.<minor>.<patch>``
pins as ``<name>-<major>.<minor>`` (patch releases share a pin), any other id pins as itself.
Judged decisions with another pin (``model_mismatch``) or with no served id
(``no_served_model``) are counted and kept out of both arms. A served id equal to the requested
alias (e.g. ``jev-latest``) names no version and counts as no served id, with a loud warning.

Unavailable and missed decisions are counted, never placed in an arm. "inconclusive" and
"no information" are correct outcomes.

Results are historical, not forward-looking. No edge claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lab.types import interval_seconds
from research.paper.compare import ForwardTrade, read_dryrun_trades
from research.paper.freqtrade import check_candidate_name, symbol_to_pair

METRIC = "mean_net_trade_return_take_minus_skip"
TEST = "cluster_permutation_two_sided"
CLUSTER = "fill_open"
MODEL_RULE = "first_served_model_major_minor"
STRATA = "same_size_within_block"
STATISTIC = "centred_within_block_diff"
DEFAULT_ANALYSIS: dict[str, Any] = {
    "metric": METRIC,
    "test": TEST,
    # the tested T is centred on its exact null mean: strata with different take shares or
    # mean returns must not push the null away from 0 (that makes |d| one-sided)
    "statistic": STATISTIC,
    "cluster": CLUSTER,  # one batched Jev call per closed bar judges that bar's signals together
    # permute only within a block: a regime persisting across bars must not fake a difference
    "strata": STRATA,
    "block_days": 7,  # one week of 4h bars (42), the span of the snapshot's longest return
    "n_permutations": 10000,  # enough for a p-value resolution of 1e-4
    "seed": 17,  # fixed so the p-value reproduces byte for byte
    "alpha": 0.05,  # conventional two-sided level; only a large effect can pass on 30-80 trades
    "min_judged_trades": 30,  # PLAN.md Phase 17: below 30 closed judged trades -> inconclusive
    "min_per_arm": 8,  # PLAN.md Phase 17: fewer than 8 in either arm -> inconclusive
    "model_rule": MODEL_RULE,  # served id, major.minor: a floating alias must not mix judges
    "evaluate_after_days": 14,  # lets trades opened near the decision end close before the read
}
EVALUATE_AT = "evaluate_at"
REPORT_DISCLAIMER = (
    "Results are historical, not forward-looking. A shadow verdict never steered a trade; "
    "no edge claim."
)
INTERIM_HEADLINE = "Interim look: not the pre-registered test; no outcome, no p-value"
COIN_MIX_NOTE = (
    "Descriptive only, not a test: a take/skip difference can reflect the coin mix (which "
    "candidates the judge took or skipped) rather than the judge."
)
BAR_STATUSES: tuple[str, ...] = ("judged", "no_signal", "unavailable", "missed")
DECISION_STATUSES: tuple[str, ...] = ("judged", "unavailable", "missed")
ARMS: tuple[str, ...] = ("take", "skip")
OUTCOMES: tuple[str, ...] = (
    "interim",
    "inconclusive",
    "no information",
    "take better",
    "take worse",
)
DRYRUN_DB = "dryrun.sqlite"
ENV_KEYS: tuple[str, ...] = ("python", "numpy", "pandas", "uv_lock_sha256")
# the permutation stream (numpy) and the snapshot formulas (pandas) depend on these
ENV_BEHAVIOUR_KEYS: tuple[str, ...] = ("numpy", "pandas")
AGENTS_DIR = Path(__file__).resolve().parents[2]
UV_LOCK = AGENTS_DIR / "uv.lock"
_SEMVER = re.compile(r"^(?P<name>.+)-(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
REGIME_FIELD = "btc_trend_pct"
# relative tolerance for "a permuted difference is as extreme as the observed one", so exact
# ties (identical arms) are not lost to floating-point rounding of the means
_TIE_RTOL = 1e-9


class FinalReportRefusedError(ValueError):
    """The final report must not be built yet (a genesis candidate's dry-run DB is unreadable)."""


@dataclass(frozen=True)
class MatchedTrade:
    """A judged decision paired with its paper fill. ``net_return`` and ``close_time`` are None
    while the trade is open as of the cutoff. ``btc_trend_pct`` is the decision snapshot's BTC
    trend (None when absent), used only for the descriptive regime comparison."""

    candidate: str
    signal_bar: int
    fill_open: int
    arm: str
    net_return: float | None
    is_open: bool
    open_time: int
    close_time: int | None
    btc_trend_pct: float | None = None


@dataclass(frozen=True)
class LoadedTrades:
    """:func:`load_trades` output: trades per genesis candidate, the human-readable notes, and
    the candidates whose dry-run database could not be read (missing, locked, corrupt)."""

    trades: dict[str, list[ForwardTrade]]
    notes: list[str] = field(default_factory=list)
    unreadable: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ArmStats:
    n: int
    mean: float | None


@dataclass(frozen=True)
class ShadowReport:
    """The shadow analysis. ``asof`` is the effective cutoff: the requested ``asof`` before
    ``evaluate_at`` (interim), else ``evaluate_at`` itself. ``chain_head`` / ``n_rows`` name
    the log prefix used (genesis plus every row written before the cutoff).

    ``effect`` is the headline: the centred within-block difference ``T`` that the test
    scores (:func:`within_block_effect`); ``diff`` is the pooled ``mean(take) - mean(skip)``,
    descriptive only (it mixes weeks with different take shares). ``alias_served`` counts the
    judged decisions whose served model is the requested alias (they are inside
    ``no_served_model``). ``recovered_heads`` lists ``(seq, lag seconds or None)`` of the head
    lines recovered after a crash for rows inside the prefix used."""

    asof: int
    evaluate_at: int
    start: int
    candidates: list[str]
    analysis: dict[str, Any]
    chain_head: str | None
    n_rows: int
    model: str | None
    model_mismatch: int
    no_served_model: int
    bars: dict[str, int]
    decisions: dict[str, int]
    judged_no_fill: int
    judged_open: int
    fills_without_decision: int
    take: ArmStats
    skip: ArmStats
    clusters: int
    mixed_clusters: int
    blocks: int
    informative_strata: int
    per_candidate: dict[str, dict[str, int]]
    diff: float | None
    p_value: float | None
    outcome: str
    reasons: list[str]
    trades: list[MatchedTrade]
    trades_sha256: str
    notes: list[str]
    regime_up: ArmStats
    regime_down: ArmStats
    regime_unknown: int
    genesis_env: dict[str, Any] | None
    env: dict[str, Any]
    effect: float | None = None
    alias_served: int = 0
    recovered_heads: list[tuple[int, float | None]] = field(default_factory=list)
    informative_trades: int = 0  # closed judged trades in informative strata


def evaluate_at_of(genesis: Mapping[str, Any]) -> int:
    """The frozen evaluation time of a genesis row (``ValueError`` when its analysis is not
    the validated pre-registration)."""
    return int(_analysis(genesis)[EVALUATE_AT])


# --------------------------------------------------------------------------- genesis


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return value


def _seconds(value: Any, name: str) -> float:
    """A finite UNIX time in seconds; the log writes ``written_at`` as a float."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of seconds, got {value!r}")
    return float(value)


def frozen_analysis(evaluate_at: int) -> dict[str, Any]:
    """The analysis the genesis row freezes: :data:`DEFAULT_ANALYSIS` plus ``evaluate_at``
    (UNIX seconds; the genesis writer sets it to the latest protocol ``decision_end`` plus
    ``evaluate_after_days * 86400``)."""
    return {**DEFAULT_ANALYSIS, EVALUATE_AT: _int(evaluate_at, EVALUATE_AT)}


def _genesis(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not rows or rows[0].get("kind") != "genesis":
        raise ValueError("the decisions log must start with a genesis row (kind 'genesis')")
    for i, row in enumerate(rows[1:], start=1):
        if row.get("kind") == "genesis":
            raise ValueError(f"row {i} is a second genesis row; only the first row may be one")
    return rows[0]


def _analysis(genesis: Mapping[str, Any]) -> dict[str, Any]:
    """The frozen analysis of the genesis row, validated. Never filled in from defaults."""
    raw = genesis.get("analysis")
    if not isinstance(raw, Mapping):
        raise ValueError("the genesis row has no 'analysis' object (the pre-registration)")
    missing = [k for k in (*DEFAULT_ANALYSIS, EVALUATE_AT) if k not in raw]
    if missing:
        raise ValueError(f"the genesis analysis is missing keys {missing}")
    for key, want in (
        ("metric", METRIC),
        ("test", TEST),
        ("cluster", CLUSTER),
        ("strata", STRATA),
        ("statistic", STATISTIC),
        ("model_rule", MODEL_RULE),
    ):
        if raw[key] != want:
            raise ValueError(f"genesis analysis {key} {raw[key]!r} is not {want!r}")
    for key in ("n_permutations", "min_judged_trades", "min_per_arm", "block_days"):
        if _int(raw[key], f"analysis.{key}") < 1:
            raise ValueError(f"analysis.{key} must be >= 1, got {raw[key]!r}")
    if _int(raw["evaluate_after_days"], "analysis.evaluate_after_days") < 0:
        raise ValueError(
            f"analysis.evaluate_after_days must be >= 0, got {raw['evaluate_after_days']!r}"
        )
    _int(raw["seed"], "analysis.seed")
    _int(raw[EVALUATE_AT], f"analysis.{EVALUATE_AT}")
    alpha = raw["alpha"]
    if isinstance(alpha, bool) or not isinstance(alpha, int | float) or not 0 < alpha < 1:
        raise ValueError(f"analysis.alpha must be a number in (0, 1), got {alpha!r}")
    return dict(raw)


def _candidates(genesis: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cands = genesis.get("candidates")
    if not isinstance(cands, Sequence) or isinstance(cands, str):
        raise ValueError("the genesis row has no 'candidates' list")
    out: list[Mapping[str, Any]] = []
    for c in cands:
        if not isinstance(c, Mapping) or "candidate" not in c or "symbol" not in c:
            raise ValueError(f"genesis candidate {c!r} needs 'candidate' and 'symbol'")
        out.append(c)
    return out


# --------------------------------------------------------------------------- trades


def load_trades(paper_dir: Path, genesis: Mapping[str, Any]) -> LoadedTrades:
    """Each genesis candidate's dry-run trades on its pair, read read-only from
    ``<paper_dir>/<candidate>/dryrun.sqlite``.

    A missing, locked or corrupt database gives ``[]`` for that candidate, a note naming it,
    and the candidate in ``unreadable``: an interim look shows the note, a final report refuses
    (:func:`build_report`). A readable database with no trades is not unreadable."""
    out: dict[str, list[ForwardTrade]] = {}
    notes: list[str] = []
    unreadable: set[str] = set()
    for c in _candidates(genesis):
        name = check_candidate_name(str(c["candidate"]))
        pair = symbol_to_pair(str(c["symbol"]))
        db = Path(paper_dir) / name / DRYRUN_DB
        try:
            out[name] = read_dryrun_trades(db, pair=pair)
        except (ValueError, OSError, sqlite3.Error) as exc:
            out[name] = []
            unreadable.add(name)
            notes.append(f"{name}: dry-run DB could not be read, no paper trades from {db} ({exc})")
    return LoadedTrades(trades=out, notes=notes, unreadable=frozenset(unreadable))


def _secs(ts: Any) -> int:
    return int(ts.timestamp())


def _regime_value(decision: Mapping[str, Any]) -> float | None:
    snap = decision.get("snapshot")
    value = snap.get(REGIME_FIELD) if isinstance(snap, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _closed_at(trade: ForwardTrade, asof: int | None) -> bool:
    if not trade.closed or trade.close_time is None:
        return False
    return asof is None or _secs(trade.close_time) < asof


def _assign(
    decisions: Sequence[Mapping[str, Any]],
    trades: Mapping[str, Sequence[ForwardTrade]],
    interval_s: int,
) -> dict[int, tuple[str, int]]:
    """One-to-one assignment of decision index -> (candidate, trade index): the earliest unused
    trade of the decision's candidate with ``open_time`` in ``[fill_open, fill_open +
    interval_s)``. Decisions are taken in fill order, so the assignment is deterministic."""
    order = sorted(
        range(len(decisions)),
        key=lambda i: (
            _int(decisions[i]["fill_open"], "fill_open"),
            str(decisions[i]["candidate"]),
        ),
    )
    used: set[tuple[str, int]] = set()
    out: dict[int, tuple[str, int]] = {}
    for i in order:
        d = decisions[i]
        cand = str(d["candidate"])
        lo = _int(d["fill_open"], "fill_open")
        hits = [
            (_secs(t.open_time), k)
            for k, t in enumerate(trades.get(cand, ()))
            if lo <= _secs(t.open_time) < lo + interval_s and (cand, k) not in used
        ]
        if hits:
            k = min(hits)[1]
            used.add((cand, k))
            out[i] = (cand, k)
    return out


def _arm(decision: Mapping[str, Any]) -> str:
    verdict = decision.get("verdict")
    choice = verdict.get("choice") if isinstance(verdict, Mapping) else None
    if choice not in ARMS:
        raise ValueError(
            f"judged decision for {decision.get('candidate')!r} at signal bar "
            f"{decision.get('signal_bar')!r} has no take/skip verdict (got {verdict!r})"
        )
    return str(choice)


def _matched(
    decisions: Sequence[Mapping[str, Any]],
    trades: Mapping[str, Sequence[ForwardTrade]],
    assignment: Mapping[int, tuple[str, int]],
    asof: int | None,
    include: set[int] | None = None,
) -> list[MatchedTrade]:
    """Matched trades of the judged decisions (only indices in ``include`` when given)."""
    out: list[MatchedTrade] = []
    for i, d in enumerate(decisions):
        if d.get("status") != "judged" or i not in assignment:
            continue
        if include is not None and i not in include:
            continue
        cand, k = assignment[i]
        trade = trades[cand][k]
        closed = _closed_at(trade, asof)
        out.append(
            MatchedTrade(
                candidate=cand,
                signal_bar=_int(d["signal_bar"], "signal_bar"),
                fill_open=_int(d["fill_open"], "fill_open"),
                arm=_arm(d),
                net_return=trade.net_return() if closed else None,
                is_open=not closed,
                open_time=_secs(trade.open_time),
                close_time=_secs(trade.close_time) if closed else None,
                btc_trend_pct=_regime_value(d),
            )
        )
    out.sort(key=lambda m: (m.fill_open, m.candidate))
    return out


def match_fills(
    decisions: Sequence[Mapping[str, Any]],
    trades: Mapping[str, Sequence[ForwardTrade]],
    interval_s: int,
) -> list[MatchedTrade]:
    """Judged decisions paired with their paper fills, in fill order.

    A judged decision matches the candidate's trade with ``open_time`` in ``[fill_open,
    fill_open + interval_s)``; each trade is matched at most once (by any decision, judged or
    not); the arm is the verdict's choice. Judged decisions with no such trade are left out.
    No model rule or cutoff is applied here (:func:`build_report` applies both).
    """
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    return _matched(decisions, trades, _assign(decisions, trades, interval_s), None)


def model_pin(served: Any) -> str | None:
    """The pin of a served model id: ``<name>-<major>.<minor>`` for
    ``<name>-<major>.<minor>.<patch>`` (``jev-1.13.0`` and ``jev-1.13.1`` both pin as
    ``jev-1.13``), else the id itself; ``None`` for a missing or blank id."""
    if not isinstance(served, str) or not served.strip():
        return None
    text = served.strip()
    m = _SEMVER.match(text)
    return f"{m['name']}-{m['major']}.{m['minor']}" if m else text


def trades_sha256(trades: Sequence[MatchedTrade]) -> str:
    """sha256 of the canonical JSON (sorted keys, no spaces) of the matched-trade list: per
    trade ``candidate, signal_bar, fill_open, open_time, close_time, net_return, arm``."""
    body = [
        {
            "candidate": m.candidate,
            "signal_bar": m.signal_bar,
            "fill_open": m.fill_open,
            "open_time": m.open_time,
            "close_time": m.close_time,
            "net_return": m.net_return,
            "arm": m.arm,
        }
        for m in trades
    ]
    text = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_env(lock_path: Path | None = None) -> dict[str, Any]:
    """The analysis environment: Python, numpy and pandas versions and the sha256 of
    ``agents/uv.lock`` (read only; ``None`` when absent). The genesis row records it."""
    lock = UV_LOCK if lock_path is None else Path(lock_path)
    try:
        lock_sha: str | None = hashlib.sha256(lock.read_bytes()).hexdigest()
    except OSError:
        lock_sha = None
    return {
        "python": platform.python_version(),
        "numpy": str(np.__version__),
        "pandas": str(pd.__version__),
        "uv_lock_sha256": lock_sha,
    }


# --------------------------------------------------------------------------- statistics


Trade4 = tuple[int, int, str, float]  # (block, cluster key, arm, net return)


def _clean_trades(trades: Sequence[Trade4]) -> list[Trade4]:
    items: list[Trade4] = []
    for block, key, arm, ret in trades:
        if arm not in ARMS:
            raise ValueError(f"trade arm must be one of {ARMS}, got {arm!r}")
        r = float(ret)
        if not math.isfinite(r):
            raise ValueError(f"returns must be finite, got {ret!r} in cluster {key!r}")
        items.append((_int(block, "block"), _int(key, "cluster key"), str(arm), r))
    items.sort()
    return items


def _strata(items: Sequence[Trade4]) -> dict[tuple[int, int], list[list[tuple[str, float]]]]:
    """Clusters grouped into strata keyed ``(block, cluster size)``, in key order; clusters
    inside a stratum ordered by cluster key, trades inside a cluster by (arm, return)."""
    clusters: dict[int, list[tuple[str, float]]] = {}
    block_of: dict[int, int] = {}
    for block, key, arm, r in items:
        if block_of.setdefault(key, block) != block:
            raise ValueError(f"cluster {key} appears in blocks {block_of[key]} and {block}")
        clusters.setdefault(key, []).append((arm, r))
    strata: dict[tuple[int, int], list[list[tuple[str, float]]]] = {}
    for key in sorted(clusters):
        strata.setdefault((block_of[key], len(clusters[key])), []).append(clusters[key])
    return dict(sorted(strata.items()))


def _informative(group: Sequence[Sequence[tuple[str, float]]]) -> bool:
    """A stratum carries weight (W_s = n_take_s * n_skip_s / n_s > 0) exactly when it holds
    both labels; even a single cluster then moves under the within-cluster shuffle."""
    return len({a for c in group for a, _ in c}) == len(ARMS)


def strata_summary(trades: Sequence[Trade4]) -> tuple[int, int]:
    """``(blocks, informative strata)`` of ``trades``: a stratum is informative when it holds
    both labels, i.e. when the stratified permutation can change the difference."""
    strata = _strata(_clean_trades(trades))
    blocks = len({b for b, _ in strata})
    return blocks, sum(1 for group in strata.values() if _informative(group))


def informative_counts(trades: Sequence[Trade4]) -> tuple[int, int]:
    """``(take, skip)`` trades inside informative strata: the only trades that carry weight in
    the centred statistic, so the minimum sample is counted on them."""
    take = skip = 0
    for group in _strata(_clean_trades(trades)).values():
        if _informative(group):
            arms = [a for c in group for a, _ in c]
            take += arms.count("take")
            skip += arms.count("skip")
    return take, skip


def _null_centre(
    strata: Mapping[tuple[int, int], Sequence[Sequence[tuple[str, float]]]],
) -> tuple[float, float, float]:
    """``(observed take_sum, E0[take_sum], W)`` of the stratified permutation.

    Under the null every trade of stratum ``s`` (``n_s`` trades, ``n_take_s`` takes, returns
    summing to ``R_s``) is a take with probability ``n_take_s / n_s`` (a uniformly random
    cluster's label vector lands on its cluster and is shuffled inside it), so exactly
    ``E0[take_sum] = sum_s (n_take_s / n_s) * R_s``. ``W = sum_s n_take_s * n_skip_s / n_s``
    is fixed by the label counts (0 when no stratum holds both labels)."""
    observed = centre = weight = 0.0
    for group in strata.values():
        labels = [a for c in group for a, _ in c]
        n, n_take = len(labels), labels.count("take")
        observed += sum(r for c in group for a, r in c if a == "take")
        centre += n_take / n * sum(r for c in group for _, r in c)
        weight += n_take * (n - n_take) / n
    return observed, centre, weight


def within_block_effect(trades: Sequence[Trade4]) -> float | None:
    """The tested statistic ``T`` of ``trades`` (``(block, cluster key, arm, net return)``):
    the take-minus-skip difference centred on its exact null mean.

    ``d = mean(take) - mean(skip)`` is affine in ``take_sum`` (the arm sizes and the total are
    fixed), so ``d(take_sum) - d(E0[take_sum]) = (1/n_take + 1/n_skip) * (take_sum -
    E0[take_sum])``. ``T`` divides that by the constant ``(1/n_take + 1/n_skip) * W``, which
    no permutation changes (so no p-value changes) and makes ``T`` the weighted mean of the
    within-stratum differences ``mean_s(take) - mean_s(skip)`` with weights ``n_take_s *
    n_skip_s / n_s``: with one stratum ``T = d``. ``None`` when no stratum holds both labels
    (nothing to compare inside a block) or an arm is empty."""
    items = _clean_trades(trades)
    if len({a for _, _, a, _ in items}) < len(ARMS):
        return None
    observed, centre, weight = _null_centre(_strata(items))
    return None if weight <= 0 else (observed - centre) / weight


def cluster_permutation_test(trades: Sequence[Trade4], *, n_permutations: int, seed: int) -> float:
    """Two-sided stratified cluster permutation p-value of the centred within-block difference
    ``T`` (:func:`within_block_effect`).

    ``trades`` holds ``(block, cluster key, arm, net return)``. The cluster key is the fill
    bar, since one Jev call judges a bar's signals together and their labels are not
    independent. The block is the frozen ``block_days`` week of the fill bar counted from the
    genesis start: market regimes persist across bars, so under the null a cluster's label
    vector moves only to a random cluster of the same size IN THE SAME BLOCK, and is randomly
    reordered inside it (``numpy.random.default_rng(seed)``; strata drawn in (block, size)
    order). The label multiset of every stratum is preserved, so both arms stay non-empty. The
    test asks whether the labels discriminate within a block, not whether one week beat
    another. Input order does not matter.

    Why centred: when strata differ in take share or in mean return, the uncentred pooled
    ``d`` has a null mean away from 0 (a judge that takes more in good weeks), so folding
    ``|d|`` at 0 makes the "two-sided" test one-sided and blind to a within-week effect of
    the other sign. ``T`` is centred on its exact conditional null mean, for the observed and
    every permuted assignment alike.
    ``p = (1 + #{|T*| >= |T_obs|}) / (1 + n_permutations)``, so ``0 < p <= 1``; ``p = 1`` when
    no stratum holds both labels. With one block and singleton clusters this is the ordinary
    two-sample permutation test."""
    if isinstance(n_permutations, bool) or not isinstance(n_permutations, int):
        raise ValueError(f"n_permutations must be an integer, got {n_permutations!r}")
    if n_permutations < 1:
        raise ValueError(f"n_permutations must be >= 1, got {n_permutations}")
    items = _clean_trades(trades)
    n_take = sum(1 for _, _, a, _ in items if a == "take")
    n_skip = len(items) - n_take
    if n_take == 0 or n_skip == 0:
        raise ValueError(f"both arms need returns (take {n_take}, skip {n_skip})")
    strata = _strata(items)
    observed_sum, centre, weight = _null_centre(strata)
    if weight <= 0:
        return 1.0  # no stratum holds both labels: no permutation can move a label
    rets = np.array([r for _, _, _, r in items], dtype=float)
    observed = abs((observed_sum - centre) / weight)
    threshold = observed - _TIE_RTOL * max(observed, float(np.abs(rets).max()), 1e-12)

    rng = np.random.default_rng(seed)
    take_sum = np.zeros(n_permutations, dtype=float)
    for group in strata.values():
        m = len(group)
        labels = np.array([[a == "take" for a, _ in c] for c in group], dtype=float)  # m x s
        values = np.array([[r for _, r in c] for c in group], dtype=float)  # m x s
        which = rng.permuted(np.tile(np.arange(m), (n_permutations, 1)), axis=1)  # N x m
        moved = rng.permuted(labels[which], axis=2)  # N x m x s, reordered inside the cluster
        take_sum += (moved * values[None, :, :]).sum(axis=(1, 2))
    centred = (take_sum - centre) / weight
    hits = int(np.count_nonzero(np.abs(centred) >= threshold))
    return (1 + hits) / (1 + n_permutations)


def _stats(values: Sequence[float]) -> ArmStats:
    return ArmStats(n=len(values), mean=float(np.mean(values)) if values else None)


# --------------------------------------------------------------------------- report


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _block(fill_open: int, start: int, block_s: int) -> int:
    return (fill_open - start) // block_s


def build_report(
    rows: Sequence[Mapping[str, Any]],
    trades: Mapping[str, Sequence[ForwardTrade]],
    *,
    asof: int,
    notes: Sequence[str] = (),
    unreadable: Collection[str] = (),
    env: Mapping[str, Any] | None = None,
    recovered_heads: Sequence[tuple[int, float | None]] = (),
) -> ShadowReport:
    """The pre-registered shadow analysis as of ``asof`` (UNIX seconds).

    ``rows`` is the chain-verified decisions log (genesis first); ``trades`` the dry-run trades
    per candidate, ``notes`` / ``unreadable`` the notes and unreadable candidates of
    :func:`load_trades`; ``env`` the current environment (default :func:`current_env`), shown
    beside the genesis one; ``recovered_heads`` the ``(seq, lag seconds)`` of the heads file's
    ``recovered`` lines (listed when the seq is inside the prefix used). Every parameter,
    including ``evaluate_at``, comes from the genesis row's ``analysis`` (``ValueError`` if
    any key is missing). The cutoff is ``min(asof,
    evaluate_at)``. Before ``evaluate_at`` the outcome is ``interim`` (no test, no p-value)
    and the notes are shown. From ``evaluate_at`` on, :class:`FinalReportRefusedError` is
    raised while any genesis candidate is ``unreadable`` (retry once the DB can be read);
    otherwise only closed matched trades of the pinned model with a finite net return enter an
    arm; ``inconclusive`` below the minimum sample (p-value None); else ``no information`` when
    ``p >= alpha``; else ``take better`` / ``take worse`` by the sign of the centred
    within-block difference ``T`` (``effect``; the pooled ``diff`` is descriptive only).

    A judged decision whose served model equals the requested alias (the genesis ``model`` or
    the row's ``requested_model``, when that is not a full ``<name>-<major>.<minor>.<patch>``
    id, e.g. ``jev-latest``) has no served model: an alias names no model version."""
    asof = _int(asof, "asof")
    genesis = _genesis(rows)
    analysis = _analysis(genesis)
    evaluate_at = int(analysis[EVALUATE_AT])
    interim = asof < evaluate_at
    cutoff = asof if interim else evaluate_at
    start = _int(genesis.get("start"), "genesis start")
    interval_s = interval_seconds(str(genesis.get("interval")))
    block_s = int(analysis["block_days"]) * 86400
    names = [check_candidate_name(str(c["candidate"])) for c in _candidates(genesis)]
    known = set(names)
    lost = sorted(set(unreadable) & known)
    if not interim and lost:
        raise FinalReportRefusedError(
            f"final report refused: {', '.join(lost)} dry-run DB unreadable; retry"
        )

    seen = [r for r in rows[1:] if _seconds(r.get("written_at"), "written_at") < cutoff]
    used_rows = [genesis, *seen]
    head = used_rows[-1].get("sha256")
    chain_head = head if isinstance(head, str) and head else None
    bars = dict.fromkeys(BAR_STATUSES, 0)
    counts = dict.fromkeys(DECISION_STATUSES, 0)
    decisions: list[Mapping[str, Any]] = []
    for r in seen:
        kind, status = r.get("kind"), r.get("status")
        if kind == "bar":
            if status not in bars:
                raise ValueError(f"bar row at {r.get('signal_bar')!r} has status {status!r}")
            bars[str(status)] += 1
        elif kind == "decision":
            if status not in counts:
                raise ValueError(f"decision row at {r.get('signal_bar')!r} has status {status!r}")
            if str(r.get("candidate")) not in known:
                raise ValueError(
                    f"decision for {r.get('candidate')!r} names no genesis candidate {names}"
                )
            counts[str(status)] += 1
            decisions.append(r)

    # model rule: the served id of the first judged decision (fill bar, then candidate) that
    # has one pins the judge, normalised to major.minor; no served id -> excluded and counted
    judged = sorted(
        (i for i, d in enumerate(decisions) if d.get("status") == "judged"),
        key=lambda i: (
            _int(decisions[i]["fill_open"], "fill_open"),
            str(decisions[i]["candidate"]),
        ),
    )
    aliases = {
        a.strip()
        for a in (genesis.get("model"),)
        if isinstance(a, str) and a.strip() and not _SEMVER.match(a.strip())
    }

    def served_pin(d: Mapping[str, Any]) -> str | None:
        served, asked = d.get("model"), d.get("requested_model")
        row_alias = (
            {asked.strip()}
            if isinstance(asked, str) and asked.strip() and not _SEMVER.match(asked.strip())
            else set()
        )
        if isinstance(served, str) and served.strip() in aliases | row_alias:
            return None
        return model_pin(served)

    alias_hits = [
        str(decisions[i].get("model")).strip()
        for i in judged
        if model_pin(decisions[i].get("model")) is not None and served_pin(decisions[i]) is None
    ]
    alias_served = len(alias_hits)
    pins = {i: served_pin(decisions[i]) for i in judged}
    model = next((pins[i] for i in judged if pins[i] is not None), None)
    pinned = {i for i in judged if pins[i] is not None and pins[i] == model}
    no_served_model = sum(1 for i in judged if pins[i] is None)
    model_mismatch = len(judged) - len(pinned) - no_served_model

    visible = {n: [t for t in trades.get(n, ()) if _secs(t.open_time) < cutoff] for n in names}
    # every decision (any status, any model) claims its fill, so no fill is counted twice
    assignment = _assign(decisions, visible, interval_s)
    matched = _matched(decisions, visible, assignment, cutoff, include=pinned)
    used = set(assignment.values())
    judged_no_fill = sum(1 for i in pinned if i not in assignment)
    judged_open = sum(1 for m in matched if m.is_open)
    fills_without_decision = sum(
        1
        for n in names
        for k, t in enumerate(visible[n])
        if _secs(t.open_time) >= start and (n, k) not in used
    )

    arms: dict[str, list[float]] = {a: [] for a in ARMS}
    counted: list[MatchedTrade] = []
    no_return = 0
    for m in matched:
        if m.is_open:
            continue
        if m.net_return is None or not math.isfinite(m.net_return):
            no_return += 1
            continue
        arms[m.arm].append(m.net_return)
        counted.append(m)
    take, skip = _stats(arms["take"]), _stats(arms["skip"])
    diff = None if take.mean is None or skip.mean is None else take.mean - skip.mean
    cluster_arms: dict[int, set[str]] = {}
    per_candidate = {n: dict.fromkeys(ARMS, 0) for n in names}
    for m in counted:
        cluster_arms.setdefault(m.fill_open, set()).add(m.arm)
        per_candidate[m.candidate][m.arm] += 1
    clusters = len(cluster_arms)
    mixed_clusters = sum(1 for a in cluster_arms.values() if len(a) == len(ARMS))
    test_input: list[Trade4] = [
        (_block(m.fill_open, start, block_s), m.fill_open, m.arm, float(m.net_return or 0.0))
        for m in counted
    ]
    blocks, informative = strata_summary(test_input) if test_input else (0, 0)
    info_take, info_skip = informative_counts(test_input) if test_input else (0, 0)
    effect = within_block_effect(test_input) if test_input else None
    up = [m.net_return or 0.0 for m in counted if (m.btc_trend_pct or 0.0) > 0]
    down = [
        m.net_return or 0.0 for m in counted if m.btc_trend_pct is not None and m.btc_trend_pct <= 0
    ]
    regime_unknown = sum(1 for m in counted if m.btc_trend_pct is None)

    current = dict(env) if env is not None else current_env()
    raw_env = genesis.get("env")
    genesis_env = dict(raw_env) if isinstance(raw_env, Mapping) else None

    reasons: list[str] = []
    if interim:
        reasons.append(
            f"interim look as of {_utc(cutoff)} UTC: the pre-registered test is read only at "
            f"{_utc(evaluate_at)} UTC (`{evaluate_at}`); no test is run and no outcome is stated"
        )
    if lost:
        reasons.append(
            f"dry-run DB unreadable for {', '.join(lost)}: its fills are missing from this "
            "interim look; the final report refuses until it can be read"
        )
    if model_mismatch:
        reasons.append(
            f"{model_mismatch} judged decisions by a model other than the pinned {model!r} "
            "(excluded from both arms)"
        )
    if alias_served:
        reasons.append(
            f"WARNING: {alias_served} judged decisions report the requested alias "
            f"{', '.join(repr(a) for a in sorted(set(alias_hits)))} as their served model; an "
            "alias names no model version, so they count as no served model id (excluded from "
            "both arms)"
        )
    if no_served_model:
        reasons.append(
            f"{no_served_model} judged decisions with no served model id (excluded from both arms)"
        )
    if genesis_env is not None:
        drift = [
            f"{k} {genesis_env.get(k)} -> {current.get(k)}"
            for k in ENV_BEHAVIOUR_KEYS
            if genesis_env.get(k) != current.get(k)
        ]
        if drift:
            reasons.append(
                "the environment differs from the genesis one ("
                + ", ".join(drift)
                + "): the permutation stream and the snapshot fingerprint depend on numpy and "
                "pandas; the fingerprint check refuses a behavioural change of the snapshot, "
                "but the p-value may differ from one computed under the genesis versions"
            )
    n_closed = take.n + skip.n
    min_total, min_arm = analysis["min_judged_trades"], analysis["min_per_arm"]
    if n_closed < min_total:
        reasons.append(f"{n_closed} closed judged trades < the minimum {min_total}")
    for arm, st in (("take", take), ("skip", skip)):
        if st.n < min_arm:
            reasons.append(f"{arm} arm has {st.n} closed trades < the minimum {min_arm}")
    n_info = info_take + info_skip
    if n_closed >= min_total and n_info < min_total:
        reasons.append(
            f"{n_info} closed judged trades in informative strata (blocks and cluster sizes "
            f"holding both labels) < the minimum {min_total}"
        )
    for arm, n_arm in (("take", info_take), ("skip", info_skip)):
        if n_arm < min_arm <= (take.n if arm == "take" else skip.n):
            reasons.append(
                f"{arm} arm has {n_arm} trades in informative strata < the minimum {min_arm}"
            )
    if no_return:
        reasons.append(f"{no_return} closed matched trades have no net return (excluded)")
    if take.n and skip.n and effect is None:
        reasons.append(
            "no block holds both labels in a same-size stratum: nothing to compare within a "
            "block, so the centred difference is undefined and p = 1"
        )
    used_seqs = len(used_rows)
    recovered = sorted((int(s), lag) for s, lag in recovered_heads if 0 <= int(s) < used_seqs)
    p_value: float | None = None
    if interim:
        outcome = "interim"
    elif (
        n_closed < min_total
        or take.n < min_arm
        or skip.n < min_arm
        or n_info < min_total
        or info_take < min_arm
        or info_skip < min_arm
    ):
        outcome = "inconclusive"
    else:
        p_value = cluster_permutation_test(
            test_input,
            n_permutations=analysis["n_permutations"],
            seed=analysis["seed"],
        )
        if p_value >= analysis["alpha"]:
            outcome = "no information"
            reasons.append(f"p = {p_value:.4f} >= alpha {analysis['alpha']}")
        else:
            outcome = "take better" if (effect or 0.0) > 0 else "take worse"
            reasons.append(f"p = {p_value:.4f} < alpha {analysis['alpha']}")
    return ShadowReport(
        asof=cutoff,
        evaluate_at=evaluate_at,
        start=start,
        candidates=names,
        analysis=analysis,
        chain_head=chain_head,
        n_rows=len(used_rows),
        model=model,
        model_mismatch=model_mismatch,
        no_served_model=no_served_model,
        bars=bars,
        decisions=counts,
        judged_no_fill=judged_no_fill,
        judged_open=judged_open,
        fills_without_decision=fills_without_decision,
        take=take,
        skip=skip,
        clusters=clusters,
        mixed_clusters=mixed_clusters,
        blocks=blocks,
        informative_strata=informative,
        per_candidate=per_candidate,
        diff=diff,
        p_value=p_value,
        outcome=outcome,
        reasons=reasons,
        trades=matched,
        trades_sha256=trades_sha256(matched),
        notes=[str(n) for n in notes],
        regime_up=_stats(up),
        regime_down=_stats(down),
        regime_unknown=regime_unknown,
        genesis_env=genesis_env,
        env=current,
        effect=effect,
        alias_served=alias_served,
        recovered_heads=recovered,
        informative_trades=info_take + info_skip,
    )


# --------------------------------------------------------------------------- markdown


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _pp(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.2f} pp"


def _interpretation(outcome: str) -> str:
    if outcome == "interim":
        return (
            "This is a look before the frozen evaluation time. The numbers below are "
            "descriptive only; the pre-registered test has not been run and no result is "
            "stated."
        )
    if outcome == "inconclusive":
        return (
            "The pre-registered minimum sample is not met, so no test was run. This is a "
            "correct outcome, not a failure: no edge is claimed."
        )
    if outcome == "no information":
        return (
            "The take and skip arms are not distinguishable at the frozen alpha. This is a "
            "correct outcome: no edge is claimed for the Jev judge."
        )
    return (
        f"The centred within-block difference passed the frozen test ({outcome}). This would "
        "justify only a second, separately pre-registered forward test, never trading on Jev "
        "verdicts, and it is not an edge claim."
    )


def _diff(a: ArmStats, b: ArmStats) -> float | None:
    return None if a.mean is None or b.mean is None else a.mean - b.mean


def _env_cell(env: Mapping[str, Any] | None, key: str) -> str:
    if env is None:
        return "not recorded"
    value = env.get(key)
    return "-" if value is None else f"`{value}`"


def render_report_markdown(report: ShadowReport) -> str:
    """Deterministic Markdown for ``report`` (same report, byte-identical text; no clock)."""
    a = report.analysis
    p = "-" if report.p_value is None else f"{report.p_value:.4f}"
    descriptive = report.outcome in ("interim", "inconclusive")
    diff_note = " (descriptive only; no test was run)" if descriptive else ""
    headline = (
        f"**{INTERIM_HEADLINE}**"
        if report.outcome == "interim"
        else f"**Outcome: {report.outcome}**"
    )
    lines = [
        "# Jev shadow judge: take vs skip",
        "",
        headline,
        "",
        _interpretation(report.outcome),
        "",
        f"Shadow start {_utc(report.start)} UTC (`{report.start}`); as of {_utc(report.asof)} "
        f"UTC (`{report.asof}`); frozen evaluation time {_utc(report.evaluate_at)} UTC "
        f"(`{report.evaluate_at}`). Candidates: {', '.join(report.candidates) or '-'}.",
        "",
        f"Decisions log: {report.n_rows} rows used (genesis plus rows written before the "
        f"cutoff); chain head `{report.chain_head or '-'}`.",
        "",
        f"Matched trades sha256 `{report.trades_sha256}` (canonical JSON of the matched-trade "
        "list below; every report at or after the evaluation time has the same one).",
        "",
        f"Pinned model (served id of the first judged decision, major.minor): "
        f"`{report.model or '-'}`; judged decisions excluded from both arms: by another model "
        f"{report.model_mismatch}, with no served model id {report.no_served_model}"
        + (
            f" (of which {report.alias_served} reported the requested alias as served: "
            "**WARNING, an alias is not a served model id**)"
            if report.alias_served
            else ""
        )
        + ".",
        "",
        "Recovered head lines (appended after a crash; lag = head line time minus the row's "
        "written_at): "
        + (
            ", ".join(
                f"seq {seq} ({'lag unknown' if lag is None else f'lag {lag:.0f} s'})"
                for seq, lag in report.recovered_heads
            )
            if report.recovered_heads
            else "none"
        )
        + ".",
        "",
        *(f"- {r}" for r in report.reasons),
        *([""] if report.reasons else []),
        *(["## Notes", "", *(f"- {n}" for n in report.notes), ""] if report.notes else []),
        "## Counts",
        "",
        "| Bars | " + " | ".join(BAR_STATUSES) + " |",
        "| --- |" + " --- |" * len(BAR_STATUSES),
        "| count | " + " | ".join(str(report.bars.get(s, 0)) for s in BAR_STATUSES) + " |",
        "",
        "| Decisions | " + " | ".join(DECISION_STATUSES) + " |",
        "| --- |" + " --- |" * len(DECISION_STATUSES),
        "| count | "
        + " | ".join(str(report.decisions.get(s, 0)) for s in DECISION_STATUSES)
        + " |",
        "",
        "Unavailable and missed decisions are counted here and never enter an arm.",
        "",
        f"- Judged decisions with no matching paper fill: {report.judged_no_fill}",
        f"- Judged decisions whose paper trade is still open: {report.judged_open}",
        f"- Paper entries since the start with no decision row: {report.fills_without_decision}",
        "",
        "## Arms (closed matched trades)",
        "",
        "| Arm | n | Mean net trade return |",
        "| --- | --- | --- |",
        f"| take | {report.take.n} | {_pct(report.take.mean)} |",
        f"| skip | {report.skip.n} | {_pct(report.skip.mean)} |",
        "",
        f"n = {report.take.n + report.skip.n} trades in {report.clusters} clusters (fill bars; "
        f"one Jev call per bar), {report.mixed_clusters} with both arms; {report.blocks} blocks "
        f"of {a.get('block_days')} days, {report.informative_strata} informative strata ("
        "same-size clusters in a block holding both labels), "
        f"{report.informative_trades} trades in them. The test permutes labels "
        "only inside a stratum: it asks whether Jev discriminates within a block.",
        "",
        f"Centred within-block difference take - skip (T, the tested statistic and the "
        f"headline effect): {_pp(report.effect)}{diff_note}. Two-sided stratified cluster "
        f"permutation p-value of |T|: {p}.",
        "",
        f"Pooled difference take - skip: {_pp(report.diff)} (descriptive only, not the tested "
        "statistic: it mixes blocks with different take shares and mean returns, so it can "
        "differ in size and even in sign from the within-block difference).",
        "",
        "## Per candidate (descriptive)",
        "",
        "| Candidate | take | skip |",
        "| --- | --- | --- |",
        *(
            f"| {name} | {report.per_candidate.get(name, {}).get('take', 0)} | "
            f"{report.per_candidate.get(name, {}).get('skip', 0)} |"
            for name in report.candidates
        ),
        "",
        COIN_MIX_NOTE,
        "",
        "## Trivial regime rule (descriptive)",
        "",
        f"Closed matched trades split by the snapshot's `{REGIME_FIELD}` (BTC close vs its "
        "50-bar EMA) instead of by Jev, so a trivial regime rule can be compared with Jev's "
        "take/skip split. Descriptive only, no test.",
        "",
        "| Split | n | Mean net trade return |",
        "| --- | --- | --- |",
        f"| {REGIME_FIELD} > 0 | {report.regime_up.n} | {_pct(report.regime_up.mean)} |",
        f"| {REGIME_FIELD} <= 0 | {report.regime_down.n} | {_pct(report.regime_down.mean)} |",
        "",
        f"Difference (> 0) - (<= 0): {_pp(_diff(report.regime_up, report.regime_down))}; trades "
        f"without the field: {report.regime_unknown}.",
        "",
        "## Environment",
        "",
        "| Package | Genesis | Now |",
        "| --- | --- | --- |",
        *(
            f"| {k} | {_env_cell(report.genesis_env, k)} | {_env_cell(report.env, k)} |"
            for k in ENV_KEYS
        ),
        "",
        "## Frozen analysis (genesis row)",
        "",
        *(f"- {k}: {a[k]}" for k in sorted(a)),
        "",
        "## Matched trades",
        "",
        "| Candidate | Signal bar (UTC) | Fill bar (UTC) | Arm | Status | Net return |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for m in report.trades:
        status = "open" if m.is_open else "closed"
        lines.append(
            f"| {m.candidate} | {_utc(m.signal_bar)} | {_utc(m.fill_open)} | {m.arm} | "
            f"{status} | {_pct(m.net_return)} |"
        )
    if not report.trades:
        lines.append("| - | - | - | - | - | - |")
    lines += ["", f"> {REPORT_DISCLAIMER}", ""]
    return "\n".join(lines)


__all__ = [
    "DEFAULT_ANALYSIS",
    "OUTCOMES",
    "REPORT_DISCLAIMER",
    "ArmStats",
    "FinalReportRefusedError",
    "LoadedTrades",
    "MatchedTrade",
    "ShadowReport",
    "build_report",
    "cluster_permutation_test",
    "current_env",
    "evaluate_at_of",
    "frozen_analysis",
    "load_trades",
    "match_fills",
    "model_pin",
    "render_report_markdown",
    "strata_summary",
    "trades_sha256",
    "within_block_effect",
]
