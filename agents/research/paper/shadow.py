"""Jev shadow judge process (PLAN.md Phase 17): watches the HC paper bots, never steers them.

After each closed bar the shadow process recomputes every registered HC candidate's entry
signal from closed candles with the lab's :func:`lab.plan.compile_plan` (read-only import; the
same frozen candle source, host and warm-up window ``[warm_from, fill_open)`` ``compare`` uses),
shows Jev a blind snapshot of every signalled setup in ONE batched call per bar, and appends the
verdicts to the hash-chained ``agents/data/paper/jev-shadow/decisions.jsonl``
(:mod:`research.paper.shadow_log`) before the fill bar's open + grace. It writes nothing inside
any candidate folder or candidate journal entry and never opens a dry-run database.

* **Pre-registration first.** The first run writes the journal entry
  ``<journal>/<date>-multi-jev-shadow.md`` exclusively (start time, candidates with their
  plan/protocol hashes, the blind snapshot, the Jev context and questions verbatim, the
  deadline rule, the frozen analysis with its evaluation time), then the log's genesis row,
  which freezes the start, interval, grace, candidates, requested model, snapshot version and
  fingerprint, question hash, analysis and the journal's sha256, then the heads file. Later runs
  (and ``shadow-report``) refuse when the journal no longer hashes to that sha256, the question
  texts, snapshot version or snapshot fingerprint changed, a recorded head no longer matches
  the log, a genesis candidate is no longer registered with the same plan and protocol, or the
  grace / model differs. The entry is never edited; reports go to separate files.
* **Tamper anchor.** The chain is unkeyed: anyone who can write the log can re-chain it from
  any row on. After every append the process adds ``- <UTC> seq <n> rows <n> head <sha256>``
  to the git-tracked ``<journal>/jev-shadow-heads.md``; once that file is committed and pushed,
  a rewritten log no longer matches a recorded head and every later run refuses. The last
  recorded head must cover the log's last row: ``shadow-report`` refuses a tip that no head
  covers (e.g. a header-only heads file with decisions). A crash can land between the log
  append and its head line, so a ``shadow`` run first checks every recorded head and, only when
  all of them verify and exactly one append (decision rows ending in its bar row) is uncovered,
  adds a head line for the tip marked ``recovered`` and warns (only after every other check
  passed, so a refused run writes nothing); the report lists recovered lines with their lag.
  ``shadow-report`` also compares the heads file with ``git show HEAD:<path>``: the committed
  text must be a prefix of the working file. A FINAL report (at or after the evaluation time)
  also needs the committed copy to anchor the log's tip and every committed version (``git
  log``) to extend the previous one; an interim look only warns about those.
* **Served model.** Decision rows log the model id Jev reports serving (``model``, ``None``
  when it names none) and the ``requested_model``; a judged bar with no served id prints a
  loud warning (those decisions are excluded from the arms by the report's model rule).
* **Environment.** The genesis row records the Python, numpy and pandas versions and the
  sha256 of ``agents/uv.lock``; the report shows them beside the current ones.
* **Bars.** Fill bar ``f`` has signal bar ``t = f - interval``. A genesis candidate is checked on
  ``t`` iff ``protocol_start <= t < decision_end``. Every ``f`` from the genesis ``start`` to the
  last bar boundary with at least one checked candidate gets exactly one bar row. The signal
  uses the closed rows in ``[warm_from, f)``; the snapshot uses ``[snapshot_since, f)`` with
  ``snapshot_since = warm_from - 300 bars``, so its EMA200 has history to settle and a gap in
  the warm-up cannot leave a bar without a snapshot.
* **Deadline.** A bar is judged only while ``now < f + grace_s``; the clock is re-read after the
  Jev call and a late verdict is discarded and logged ``missed``. A bar first seen at or after
  its deadline is logged ``missed`` (its signals are still computed, without asking Jev, so the
  report can count missed signals). Jev unavailable or an invalid answer is logged
  ``unavailable``. Neither is ever a skip.
* **Retries.** A candle, network or snapshot problem on a bar still before its deadline writes
  nothing for that bar; the loop retries shortly and, after the deadline, logs it ``missed``.

Paper trading only; nothing here places orders. The Jev key is read by
:class:`research.jev.JevClient` from ``TYPESAFE_API_KEY`` and never seen here. Results are
historical, not forward-looking.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from lab.config import load_config
from lab.plan import compile_plan, parse_plan, plan_hash
from lab.runner import CandleLoader, default_loader
from lab.types import Plan, interval_seconds
from research.jev import JevClient
from research.journal import guard_holdout
from research.paper.compare import public_kline_loader
from research.paper.protocol import (
    PROTOCOL_FILE,
    PaperProtocol,
    _create_exclusive,
    load_registered_protocol,
    protocol_sha256,
)
from research.paper.shadow_log import (
    DEFAULT_GRACE_S,
    ChainError,
    DecisionLog,
    check_deadlines,
    snapshot_sha256,
    verify_chain,
)
from research.paper.shadow_report import (
    DEFAULT_ANALYSIS,
    EVALUATE_AT,
    FinalReportRefusedError,
    build_report,
    current_env,
    evaluate_at_of,
    frozen_analysis,
    load_trades,
    render_report_markdown,
)
from research.paper.shadow_snapshot import (
    CONF_INSTRUCTIONS,
    CONTEXT,
    FAKEOUT_INSTRUCTIONS,
    QUESTIONS_SHA256,
    SNAPSHOT_FIELDS,
    SNAPSHOT_VERSION,
    TAKE_CRITERIA,
    TAKE_INSTRUCTIONS,
    blind_snapshot,
    judge,
    snapshot_fingerprint,
)

logger = logging.getLogger(__name__)

SHADOW_DIRNAME = "jev-shadow"
DECISIONS_FILE = "decisions.jsonl"
PROCESS_LOCK = "shadow.lock"
PREREG_SUFFIX = "-multi-jev-shadow.md"
HEADS_FILE = "jev-shadow-heads.md"  # git-tracked tamper anchor in the journal folder
DEFAULT_MODEL = "jev-latest"  # the JevClient default; the served id is logged per decision
# extra closed bars before compare's warm_from fed to the snapshot only: EMA200 (the longest
# snapshot look-back, MIN_HISTORY_BARS 201) gets ~1.5x its span to settle, and a short gap in
# the warm-up can no longer leave the first bars below the snapshot's minimum history
SNAPSHOT_EXTRA_BARS = 300
BTC_SYMBOL = "BTCUSDT"
SHADOW_INTERVAL = "4h"  # the Jev context describes 4h bars; every HC candidate is 4h
DEFAULT_SETTLE_S = 20.0  # wait after a bar boundary so the exchange has published the close
DEFAULT_RETRY_S = 60.0  # retry a bar that could not be judged yet, well inside the grace
GIT_TIMEOUT_S = 10.0  # `git show` of one small file; a hung git must not hang the report

NowFn = Callable[[], float]
LoaderFor = Callable[[Mapping[str, Any]], CandleLoader]


class ShadowRefusedError(RuntimeError):
    """The shadow judge must not run (changed pre-registration, candidates, grace, ...)."""


# --------------------------------------------------------------------------- paths


def shadow_dir(paper_dir: Path) -> Path:
    """``<paper_dir>/jev-shadow`` (no protocol.json, so candidate scans skip it)."""
    return Path(paper_dir) / SHADOW_DIRNAME


def decisions_path(paper_dir: Path) -> Path:
    return shadow_dir(paper_dir) / DECISIONS_FILE


def prereg_path(journal_dir: Path, date: str) -> Path:
    """``<journal_dir>/<date>-multi-jev-shadow.md`` (never matches ``*-paper-*.md``)."""
    return Path(journal_dir) / f"{date}{PREREG_SUFFIX}"


def heads_path(journal_dir: Path) -> Path:
    """``<journal_dir>/jev-shadow-heads.md``: the append-only, git-tracked chain heads."""
    return Path(journal_dir) / HEADS_FILE


@contextlib.contextmanager
def _process_lock(directory: Path) -> Iterator[None]:
    """Non-blocking exclusive lock: a second shadow process on the same log refuses."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / PROCESS_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ShadowRefusedError(
                f"another shadow process holds {directory / PROCESS_LOCK}; run only one"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- candidates


@dataclass(frozen=True)
class Candidate:
    """A registered HC candidate as the shadow judge sees it (read only)."""

    name: str
    protocol: PaperProtocol
    plan: Plan
    protocol_sha256: str

    def spec(self) -> dict[str, Any]:
        """The candidate's frozen genesis entry."""
        p = self.protocol
        warm_from = int(p.data.warm_from)
        return {
            "candidate": self.name,
            "symbol": p.symbol,
            "plan_hash": p.plan_hash,
            "protocol_sha256": self.protocol_sha256,
            "protocol_start": int(p.start),
            "decision_end": int(p.decision_end),
            "warm_from": warm_from,
            "snapshot_since": warm_from - SNAPSHOT_EXTRA_BARS * interval_seconds(p.interval),
            "candle_source": p.data.candle_source,
            "candle_host": p.data.candle_host,
        }


def registered_candidates(
    paper_dir: Path, journal_dir: Path
) -> tuple[dict[str, Candidate], list[str]]:
    """Every folder under ``paper_dir`` with a ``protocol.json`` that verifies against its
    journal entry (:func:`load_registered_protocol`) and whose ``plan.json`` hashes to the
    protocol's plan. Others are skipped with a warning. Nothing is written."""
    found: dict[str, Candidate] = {}
    warnings: list[str] = []
    root = Path(paper_dir)
    if not root.is_dir():
        return found, warnings
    for folder in sorted(p for p in root.iterdir() if (p / PROTOCOL_FILE).is_file()):
        name = folder.name
        try:
            protocol, _jpath = load_registered_protocol(root, name, Path(journal_dir))
            plan = parse_plan(json.loads((folder / "plan.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            warnings.append(f"skipping {name}: not a verified registration ({exc})")
            continue
        if plan_hash(plan) != protocol.plan_hash:
            warnings.append(
                f"skipping {name}: plan.json ({plan_hash(plan)[:12]}) is not the registered "
                f"plan ({protocol.plan_hash[:12]})"
            )
            continue
        found[name] = Candidate(name, protocol, plan, protocol_sha256(protocol))
    return found, warnings


# --------------------------------------------------------------------------- pre-registration


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_preregistration(
    *,
    date: str,
    start: int,
    interval: str,
    grace_s: int,
    candidates: Sequence[Mapping[str, Any]],
    model: str,
    analysis: Mapping[str, Any],
    fingerprint: str,
    env: Mapping[str, Any] | None = None,
) -> str:
    """The pre-registration journal entry (deterministic for the same inputs)."""
    a = analysis
    env_text = (
        ", ".join(f"{k} `{env.get(k)}`" for k in ("python", "numpy", "pandas", "uv_lock_sha256"))
        if env is not None
        else "not recorded"
    )
    evaluate_at = int(a[EVALUATE_AT])
    lines = [
        "---",
        'run_id: "jev-shadow"',
        f'date: "{date}"',
        'outcome: "pending"',
        "idea_ids: []",
        "tags: [jev-shadow, paper-trade]",
        "---",
        "",
        "# Jev shadow judge: pre-registration (PLAN.md Phase 17)",
        "",
        "Written before the first decision and never edited afterwards. Reports are written to",
        "separate files: `agents/data/paper/jev-shadow/interim-*.md` before the evaluation time,",
        "`agents/data/paper/jev-shadow/report-*.md` at or after it.",
        "",
        "## Why forward only",
        "",
        "An LLM judge cannot be backtested honestly: a model asked about past bars may remember",
        "them, and a filter tuned on the same history only fits that history. This test asks",
        "Jev only about bars that close after this entry was written.",
        "",
        "## Watches, never steers",
        "",
        "The shadow process recomputes each registered HC candidate's entry signal with the",
        "lab's `compile_plan` on the same candles, warm-up window and data source as `compare`.",
        "It writes no strategy, config, protocol or journal file of the paper bots, never opens",
        "their dry-run databases, and cannot change their trades or verdicts. No Jev verdict is",
        "ever acted on.",
        "",
        "## Start and candidates",
        "",
        f"- Start (first judged fill bar open): {_utc(start)} (`{start}`)",
        f"- Interval: {interval}",
        f"- Requested Jev model: `{model}`. The model id Jev reports serving is recorded on every",
        "  decision row next to the requested one; the analysis pins the served model of the",
        "  first judged decision (see the model rule below).",
        f"- Environment at genesis: {env_text}. The permutation stream and the snapshot",
        "  formulas depend on numpy and pandas; a report run under other versions says so.",
        "",
        "| Candidate | Symbol | plan_hash | protocol_sha256 | Protocol window (signal bars) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in candidates:
        lines.append(
            f"| {c['candidate']} | {c['symbol']} | `{c['plan_hash']}` | "
            f"`{c['protocol_sha256']}` | {_utc(int(c['protocol_start']))} to "
            f"{_utc(int(c['decision_end']))} (end excluded) |"
        )
    lines += [
        "",
        "A candidate is checked on signal bar t only when its protocol start <= t < its",
        "decision end. A candidate registered after this entry is not part of this test.",
        "The entry signal uses exactly `compare`'s closed candles from the protocol's",
        f"`warm_from`; the snapshot alone reads {SNAPSHOT_EXTRA_BARS} more closed bars before it",
        "(`snapshot_since`, frozen per candidate in the genesis row).",
        "",
        "## Blind snapshot",
        "",
        f"Snapshot version {SNAPSHOT_VERSION}, fingerprint `{fingerprint}` (the formulas'",
        "behaviour on fixed synthetic inputs; a changed formula is refused). Rounded ratios",
        "only, from bars closed at the signal bar (no symbol, no dates, no prices or volume",
        "levels):",
        "",
        *(f"- `{name}`" for name in SNAPSHOT_FIELDS),
        "",
        "Every signalled setup of one bar goes to Jev in ONE batched call; setups are labelled",
        "s1..sk by snapshot hash, so a label does not stand for one coin.",
        "",
        "Known limitation: the snapshot is blind to symbol, dates and prices, but the BTC",
        "candidate's setup can be recognised from its BTC fields (its `ret_42_pct` equals",
        "`btc_ret_42_pct` and its `dist_ema50_pct` equals `btc_trend_pct`). The fields are kept",
        "as they are. The bars are forward (closed after this entry), so this is not look-ahead;",
        "it only means the judge may tell the BTC setup from the others.",
        "",
        "## Jev context and questions (verbatim)",
        "",
        "Context:",
        "",
        "```text",
        CONTEXT,
        "```",
        "",
        "Per setup `{label}`:",
        "",
        "- `{label}_take` (choice):",
        "",
        "```text",
        TAKE_INSTRUCTIONS,
        "```",
        "",
        *(f"  - criterion `{k}`: {v}" for k, v in TAKE_CRITERIA.items()),
        "",
        "- `{label}_fakeout` (noul, probability of a fake-out):",
        "",
        "```text",
        FAKEOUT_INSTRUCTIONS,
        "```",
        "",
        "- `{label}_conf` (score, confidence 0 to 1):",
        "",
        "```text",
        CONF_INSTRUCTIONS,
        "```",
        "",
        f"QUESTIONS_SHA256 (context + the three questions for label `L`): `{QUESTIONS_SHA256}`",
        "",
        "## Deadline rule",
        "",
        "The signal needs bar t's close, which is the instant the fill bar t+1 opens; the",
        "snapshot holds only bars closed at t. Every verdict row is written before the fill",
        f"bar's open + {grace_s} s grace. A bar that could not be judged by then is logged",
        "`missed` (never judged later, never filled in). Jev down, a timeout or an invalid",
        "answer is logged `unavailable`. Neither `missed` nor `unavailable` is ever counted as",
        "a skip. The log is append-only and hash-chained; no row is edited.",
        "",
        "## Tamper anchor",
        "",
        "The hash chain is unkeyed, so on its own it does not stop a full re-chain. After every",
        f"append the shadow process adds the new chain head to `agents/journal/{HEADS_FILE}`",
        "(append-only, committed with each push). `shadow-report` and every later shadow run",
        "check every recorded head against the log and refuse if the log was rewritten. The",
        "final report (at or after the evaluation time) is refused unless the committed heads",
        "file anchors the log's last row and every committed version extends the one before",
        "it, so the anchor the final read relies on is the one in the shared git history.",
        "",
        "## Pre-registered analysis",
        "",
        f"- Evaluation time: {_utc(evaluate_at)} (`{evaluate_at}`), the latest candidate",
        f"  decision end plus {a['evaluate_after_days']} days so trades opened near the end can",
        "  close. Any report before it is an interim look: descriptive numbers only, no test,",
        "  no p-value and no outcome. At or after it the cutoff is the evaluation time itself",
        "  (later rows and trades are ignored; a trade still open then is excluded), so every",
        "  later report is the same report.",
        f"- Metric (`{a['metric']}`): mean net trade return of Jev-take trades minus Jev-skip",
        "  trades. The net return is the freqtrade dry-run trade's net return, the number",
        "  `compare` reads. A judged decision is matched to the candidate's paper trade whose",
        "  open time is in [fill_open, fill_open + interval); each trade is used once. Only",
        "  closed matched trades enter an arm.",
        f"- Test (`{a['test']}`, cluster `{a['cluster']}`, strata `{a['strata']}`, block",
        f"  {a['block_days']} days, statistic `{a['statistic']}`): two-sided stratified",
        "  cluster permutation test of the centred within-block difference T defined below.",
        "  One Jev call judges every setup of a bar together and the coins are",
        "  correlated, so the trades of one fill bar are one cluster. Market regimes also",
        "  persist across bars (trades overlap, the snapshot's BTC fields span 42 bars), so",
        "  a plain cluster test would let one good week against one bad week look like skill.",
        "  The test is therefore stratified: the block of a fill bar is (fill_open - start) //",
        f"  ({a['block_days']} x 86400 s), and label vectors are permuted as whole fill-bar",
        "  clusters only among clusters of the same size in the same block (and shuffled",
        "  inside the cluster). It asks whether Jev discriminates between setups of the same",
        "  week, not whether one week beat another. The report gives the number of blocks and",
        "  of informative strata (same-size clusters of a block holding both labels).",
        "- Minimum sample counts only trades that carry weight in T: at least",
        f"  {a['min_judged_trades']} closed judged trades inside informative strata and at least "
        f"{a['min_per_arm']} per arm",
        '  there (as well as overall), else the outcome is "inconclusive", never "no information".',
        "- Statistic T (centred within-block difference). Let S be the sum of take returns and",
        "  d(S) = S / n_take - (total - S) / n_skip the pooled difference (n_take, n_skip and the",
        "  total are fixed under the permutation). Under the stratified permutation every trade",
        "  of stratum s (n_s trades, n_take_s takes, returns summing to R_s) is a take with",
        "  probability n_take_s / n_s, so exactly E0[S] = sum over s of (n_take_s / n_s) * R_s.",
        "  T = (d(S) - d(E0[S])) / c with the constant c = (1/n_take + 1/n_skip) * W, W = sum",
        "  over s of n_take_s * n_skip_s / n_s. Dividing by c changes no p-value; it makes T the",
        "  weighted mean of the within-stratum differences mean_s(take) - mean_s(skip) (weights",
        "  n_take_s * n_skip_s / n_s), so with a single stratum T is the plain difference. Why",
        "  centred: when weeks differ in take share or in mean return the uncentred d has a null",
        "  mean away from 0 (a judge that takes more in good weeks), and folding |d| at 0 would",
        "  make the two-sided test one-sided and blind to a within-week effect of the other",
        "  sign. T is computed the same way for the observed and every permuted assignment;",
        "  p = (1 + #{|T*| >= |T_obs|}) / (1 + n_permutations), and p = 1 when no stratum holds",
        "  both labels. T_obs is the headline effect; the pooled difference is shown as a",
        "  description only.",
        f"  n_permutations {a['n_permutations']}, seed {a['seed']}, alpha {a['alpha']}.",
        f"- Model rule (`{a['model_rule']}`): every decision row logs the served model id (or",
        "  none, when Jev names none) and the requested one. The pin is the served id of the",
        "  first judged decision (by fill bar, then candidate) that has one, normalised to",
        "  major.minor when it has the form `<name>-<major>.<minor>.<patch>` (`jev-1.13.0` and",
        "  `jev-1.13.1` both pin as `jev-1.13`), else the full id. Judged decisions with a",
        "  different pin, or with no served id, are excluded from both arms and counted",
        "  separately in the report; the requested alias never stands in for a served id. A",
        "  served id equal to the requested alias (when that alias is not a full",
        "  `<name>-<major>.<minor>.<patch>` id, e.g. `jev-latest`) names no version and counts",
        "  as no served id; the report warns loudly.",
        "- Descriptive only (no test): the mean return of matched trades whose snapshot",
        "  `btc_trend_pct` > 0 versus <= 0, so a trivial regime rule can be set beside Jev's",
        "  split. A final report is refused while any candidate's dry-run DB cannot be read,",
        "  and it prints the sha256 of its matched-trade list so a later final report can be",
        "  checked to be the same report.",
        f"- Minimum sample: at least {a['min_judged_trades']} closed judged trades and at least",
        f'  {a["min_per_arm"]} per arm, else the outcome is "inconclusive" and no test is run.',
        '- Outcomes: "inconclusive", "no information" (p >= alpha), "take better" or "take',
        '  worse" (p < alpha, by the sign of T_obs). Every one of them is a correct',
        "  outcome and is written up as carefully as the others.",
        "- A positive result justifies only a second, separately pre-registered forward test,",
        "  never trading on Jev verdicts. No edge claim.",
        "",
        "> Results are historical, not forward-looking.",
        "",
    ]
    return "\n".join(lines)


@dataclass
class ShadowState:
    genesis: dict[str, Any]
    rows: list[dict[str, Any]]
    candidates: dict[str, Candidate]  # the genesis candidates, current files (verified equal)
    warnings: list[str]
    created: bool


def _floor(ts: float, step: int) -> int:
    return math.floor(ts / step) * step


def _common_interval(cands: Mapping[str, Candidate]) -> str:
    intervals = {c.protocol.interval for c in cands.values()}
    if intervals != {SHADOW_INTERVAL}:
        raise ShadowRefusedError(
            f"every candidate must be {SHADOW_INTERVAL} (the Jev context says 4h bars); "
            f"found {sorted(intervals)}"
        )
    return SHADOW_INTERVAL


# --------------------------------------------------------------------------- heads file

HEADS_HEADER = (
    "# Jev shadow judge: recorded chain heads\n"
    "\n"
    "Append-only; never edit or reorder a line. The shadow judge adds one line after every\n"
    "write to `agents/data/paper/jev-shadow/decisions.jsonl`. Commit this file with each push:\n"
    "the committed heads are what make a rewritten (re-chained) log detectable, because\n"
    "`shadow-report` and every later shadow run check each recorded head against the log.\n"
    "\n"
)
_HEAD_LINE = re.compile(r"^- (\S+) seq (\d+) rows (\d+) head ([0-9a-f]{64})( recovered)?$")
RECOVERED = "recovered"


@dataclass(frozen=True)
class HeadEntry:
    """One recorded head line; ``recovered`` marks a head added for an uncovered tip after a
    crash between a log append and its head line."""

    seq: int
    sha256: str
    recovered: bool
    at: float | None = None  # the line's UTC time in UNIX seconds (None when unparseable)


_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime(_ISO_FORMAT)


def _parse_utc_iso(text: str) -> float | None:
    try:
        return datetime.strptime(text, _ISO_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def head_line(row: Mapping[str, Any], now: float, *, recovered: bool = False) -> str:
    """``- <UTC time> seq <n> rows <n + 1> head <sha256>`` for the last appended ``row``
    (`` recovered`` appended for a head added after a crash, see :func:`recover_tip`)."""
    seq = int(row["seq"])
    mark = f" {RECOVERED}" if recovered else ""
    return f"- {_utc_iso(now)} seq {seq} rows {seq + 1} head {row['sha256']}{mark}\n"


def parse_head_entries(text: str, source: str = "the heads file") -> list[HeadEntry]:
    """Every head line of a heads-file text, in order (see :func:`read_head_entries`)."""
    out: list[HeadEntry] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.startswith("- "):
            continue
        m = _HEAD_LINE.match(line)
        if m is None or int(m.group(3)) != int(m.group(2)) + 1:
            raise ValueError(f"{source}: line {line_no} is not a valid head line: {line!r}")
        out.append(
            HeadEntry(
                int(m.group(2)), m.group(4), m.group(5) is not None, _parse_utc_iso(m.group(1))
            )
        )
    return out


def read_head_entries(path: Path) -> list[HeadEntry]:
    """Every recorded head line of the heads file, in file order.

    Lines that do not start with ``"- "`` are prose (the header). A ``"- "`` line that is not a
    well-formed head line, or whose ``rows`` is not ``seq + 1``, raises ``ValueError``."""
    return parse_head_entries(Path(path).read_text(encoding="utf-8"), str(path))


def recovered_lags(
    entries: Sequence[HeadEntry], rows: Sequence[Mapping[str, Any]]
) -> list[tuple[int, float | None]]:
    """``(seq, lag seconds)`` of every ``recovered`` head line: the line's time minus the
    row's ``written_at`` (None when either is unusable). A long lag means the tip went
    unanchored for that long."""
    out: list[tuple[int, float | None]] = []
    for e in entries:
        if not e.recovered:
            continue
        written = rows[e.seq].get("written_at") if 0 <= e.seq < len(rows) else None
        usable = isinstance(written, int | float) and not isinstance(written, bool)
        lag = e.at - float(written) if e.at is not None and usable else None
        out.append((e.seq, lag))
    return out


def read_heads(path: Path) -> list[tuple[int, str]]:
    """Every recorded ``(seq, sha256)`` of the heads file, in file order
    (:func:`read_head_entries` without the recovered flag)."""
    return [(e.seq, e.sha256) for e in read_head_entries(path)]


def head_problems(rows: Sequence[Mapping[str, Any]], heads: Sequence[tuple[int, str]]) -> list[str]:
    """One problem per recorded head that the log no longer has ([] when every one matches)."""
    problems: list[str] = []
    for seq, sha in heads:
        if seq >= len(rows):
            problems.append(
                f"the log has {len(rows)} rows but head {sha} was recorded at seq {seq}: the log "
                f"was rewritten after head {sha} was recorded (rows deleted)"
            )
        elif rows[seq].get("sha256") != sha:
            problems.append(
                f"row seq {seq} hashes to {rows[seq].get('sha256')}, not the recorded {sha}: the "
                f"log was rewritten after head {sha} was recorded"
            )
    return problems


def tip_problem(rows: Sequence[Mapping[str, Any]], heads: Sequence[tuple[int, str]]) -> str | None:
    """A problem when no recorded head covers the log's last row (None when one does or the
    log is empty). A header-only heads file with rows in the log is uncovered too."""
    if not rows:
        return None
    last = max((seq for seq, _ in heads), default=-1)
    tip = len(rows) - 1
    if last >= tip:
        return None
    covered = f"seq {last}" if last >= 0 else "no row (no head line at all)"
    return (
        f"the heads file is behind the log: its last head covers {covered} but the log's last "
        f"row is seq {tip} ({tip - last} rows not anchored); run `shadow` once to recover a "
        "crash between a log write and its head line, or restore the heads file from git"
    )


def _append_head(
    path: Path, row: Mapping[str, Any], now: float, *, recovered: bool = False
) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(head_line(row, now, recovered=recovered))
        handle.flush()
        os.fsync(handle.fileno())


def _verify_heads(
    journal_dir: Path, rows: Sequence[Mapping[str, Any]], *, require_tip: bool = True
) -> list[str]:
    hpath = heads_path(journal_dir)
    if not hpath.is_file():
        return [
            f"the heads file {hpath} is missing (restore it from git; it is the log's tamper "
            "anchor)"
        ]
    try:
        heads = read_heads(hpath)
    except (OSError, ValueError) as exc:
        return [f"the heads file does not parse: {exc}"]
    problems = head_problems(rows, heads)
    if require_tip and not problems:
        tip = tip_problem(rows, heads)
        if tip is not None:
            problems.append(tip)
    return problems


def _single_append(uncovered: Sequence[Mapping[str, Any]]) -> bool:
    """True when ``uncovered`` is exactly what ONE shadow write appends: the genesis row alone,
    or decision rows followed by their bar row."""
    if not uncovered:
        return False
    kinds = [r.get("kind") for r in uncovered]
    if kinds == ["genesis"]:
        return True
    return kinds[-1] == "bar" and all(k == "decision" for k in kinds[:-1])


def recover_tip(journal_dir: Path, rows: Sequence[Mapping[str, Any]], now: float) -> str | None:
    """Re-anchor the tip after a crash between a log append and its head line.

    Call only after every recorded head verified (:func:`head_problems` is empty). Returns
    None when the tip is covered; appends a ``recovered`` head line for the tip and returns a
    warning when exactly one append is uncovered; raises :class:`ShadowRefusedError` when more
    is uncovered (a crash leaves one append at most, so more means lost head lines)."""
    hpath = heads_path(journal_dir)
    heads = read_heads(hpath)
    problem = tip_problem(rows, heads)
    if problem is None:
        return None
    last = max((seq for seq, _ in heads), default=-1)
    uncovered = rows[last + 1 :]
    if not _single_append(uncovered):
        raise ShadowRefusedError(
            f"{problem}. More than one write is unanchored, which a crash cannot cause; "
            "restore the heads file from git"
        )
    tip = rows[-1]
    _append_head(hpath, tip, now, recovered=True)
    return (
        f"WARNING: the heads file did not cover the log's last row (seq {tip['seq']}; a crash "
        f"between a log write and its head line?): every recorded head verified, so a head "
        f"line for seq {tip['seq']} marked '{RECOVERED}' was appended to {HEADS_FILE}. Commit it."
    )


@dataclass(frozen=True)
class GitHeadsCheck:
    """The heads file against git (read only).

    ``problem``: the committed copy (``git show HEAD:``) is not a prefix of the working file.
    ``warning``: git or the committed copy is unavailable (the check was skipped).
    ``uncommitted``: head lines of the working file not in the committed copy.
    ``committed_last_seq``: the last seq the committed copy anchors (-1 for none).
    ``history_problem``: a committed version (``git log -- <file>``) does not extend the
    version committed before it, i.e. the committed history is not append-only.
    ``versions``: the number of committed versions checked."""

    problem: str | None
    warning: str | None
    uncommitted: int | None
    committed_last_seq: int | None = None
    history_problem: str | None = None
    versions: int | None = None


_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _count_head_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("- "))


def _git(args: Sequence[str], cwd: Path, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    """One read-only git command (no shell; ``check=False``; raises on a missing or hung git)."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, timeout=timeout_s, check=False
    )


def _git_error(proc: subprocess.CompletedProcess[bytes]) -> str:
    why = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    return why[-1] if why else f"git exit {proc.returncode}"


def _history_check(hpath: Path, timeout_s: float) -> tuple[str | None, str | None, int | None]:
    """``(history_problem, warning, versions)``: every committed version of the heads file,
    oldest first (``git log --format=%H -- ./<name>``, then ``git show <h>:./<name>``), must be
    a prefix of the next one."""
    name, cwd = hpath.name, hpath.parent
    try:
        log = _git(["log", "--format=%H", "--", f"./{name}"], cwd, timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git log is unavailable ({exc}); the committed history was not checked", None
    if log.returncode != 0:
        return None, f"git log failed ({_git_error(log)}); the history was not checked", None
    commits = list(reversed(log.stdout.decode("ascii", "replace").split()))
    previous: tuple[str, bytes] | None = None
    for commit in commits:
        if not _COMMIT.match(commit):
            return None, f"git log returned {commit!r}, not a commit id; history not checked", None
        try:
            shown = _git(["show", "--no-textconv", f"{commit}:./{name}"], cwd, timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"git show is unavailable ({exc}); history not checked", None
        if shown.returncode != 0:
            return (
                f"the committed history of {name} is not append-only: commit {commit[:12]} has "
                f"no readable copy ({_git_error(shown)}; deleted in that commit?)",
                None,
                len(commits),
            )
        if previous is not None and not shown.stdout.startswith(previous[1]):
            return (
                f"the committed history of {name} is not append-only: the version in commit "
                f"{commit[:12]} does not extend the one in {previous[0][:12]} (a committed head "
                "line was edited, removed or reordered and later restored)",
                None,
                len(commits),
            )
        previous = (commit, shown.stdout)
    return None, None, len(commits)


def check_heads_committed(hpath: Path, *, timeout_s: float = GIT_TIMEOUT_S) -> GitHeadsCheck:
    """Compare the heads file with its committed copy and its committed history (read only:
    ``git show`` / ``git log``, cwd = the heads file's folder, ``./<name>`` = its
    repository-relative path; no shell; every command has ``timeout_s``)."""
    hpath = Path(hpath)
    try:
        proc = _git(["show", "--no-textconv", f"HEAD:./{hpath.name}"], hpath.parent, timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitHeadsCheck(
            None, f"git is unavailable ({exc}); the committed heads check was skipped", None
        )
    if proc.returncode != 0:
        return GitHeadsCheck(
            None,
            f"the heads file is not committed yet or this is not a git checkout "
            f"({_git_error(proc)}); the committed heads check was skipped. Commit and push the "
            "heads file",
            None,
        )
    committed = proc.stdout
    working = hpath.read_bytes()
    if not working.startswith(committed):
        return GitHeadsCheck(
            f"the heads file differs from the committed copy (git HEAD): {hpath} must extend "
            "the committed text, but a committed line was edited, removed or reordered",
            None,
            None,
        )
    committed_text = committed.decode("utf-8", "replace")
    try:
        anchored = parse_head_entries(committed_text, f"git HEAD:{hpath.name}")
    except ValueError as exc:
        return GitHeadsCheck(f"the committed heads file does not parse: {exc}", None, None)
    pending = _count_head_lines(working.decode("utf-8", "replace")) - _count_head_lines(
        committed_text
    )
    history, warning, versions = _history_check(hpath, timeout_s)
    return GitHeadsCheck(
        None,
        warning,
        pending,
        committed_last_seq=max((e.seq for e in anchored), default=-1),
        history_problem=history,
        versions=versions,
    )


def final_git_problems(git: GitHeadsCheck | None, tip_seq: int) -> list[str]:
    """Why a FINAL report must not be built from this heads file (fail closed): the committed
    copy must exist, be a prefix of the working file, anchor the log's tip (zero uncommitted
    head lines) and have an append-only committed history. [] when all hold."""
    if git is None:
        return ["final report refused: the heads file is missing"]
    if git.problem is not None:
        return []  # already a problem for every report
    out: list[str] = []
    if git.warning is not None or git.uncommitted is None:
        out.append(
            f"final report refused: the committed heads check could not run ({git.warning}); "
            "commit and push the heads file, then retry"
        )
        return out
    if git.uncommitted != 0:
        out.append(
            f"final report refused: {git.uncommitted} head lines are not committed yet; commit "
            "and push the heads file, then retry"
        )
    last = -1 if git.committed_last_seq is None else git.committed_last_seq
    if last < tip_seq:
        out.append(
            f"final report refused: the committed heads file anchors seq {last} but the log's "
            f"last row is seq {tip_seq}; commit and push the heads file, then retry"
        )
    if git.history_problem is not None:
        out.append(f"final report refused: {git.history_problem}")
    return out


# --------------------------------------------------------------------------- frozen checks


def frozen_problems(genesis: Mapping[str, Any], journal_dir: Path) -> list[str]:
    """What no longer matches the genesis: snapshot version / fingerprint, the question texts
    and the pre-registration journal entry (missing or edited). [] when all match."""
    problems: list[str] = []
    if genesis.get("snapshot_version") != SNAPSHOT_VERSION:
        problems.append(
            f"the snapshot version changed: genesis froze {genesis.get('snapshot_version')!r}, "
            f"the code is {SNAPSHOT_VERSION}"
        )
    fingerprint = snapshot_fingerprint()
    if genesis.get("snapshot_fingerprint") != fingerprint:
        problems.append(
            "the snapshot fingerprint changed (a snapshot formula changed since the "
            f"pre-registration): genesis {str(genesis.get('snapshot_fingerprint'))[:12]}, "
            f"now {fingerprint[:12]}"
        )
    if genesis.get("questions_sha256") != QUESTIONS_SHA256:
        problems.append(
            "the Jev context or question texts changed since the pre-registration "
            f"(genesis {str(genesis.get('questions_sha256'))[:12]}, now {QUESTIONS_SHA256[:12]})"
        )
    jpath = Path(journal_dir) / str(genesis.get("prereg_journal"))
    if not jpath.is_file():
        problems.append(f"the pre-registration {jpath} is missing")
    elif _sha256_bytes(jpath.read_bytes()) != genesis.get("prereg_sha256"):
        problems.append(
            f"the pre-registration changed: {jpath.name} no longer hashes to the genesis "
            f"prereg_sha256 {str(genesis.get('prereg_sha256'))[:12]}"
        )
    return problems


def ensure_genesis(
    paper_dir: Path,
    journal_dir: Path,
    *,
    client: JevClient,
    now_fn: NowFn,
    grace_s: int,
) -> ShadowState:
    """Write the pre-registration + genesis row + heads file on the first run; verify them on
    later runs.

    Raises :class:`ShadowRefusedError` for anything that must stop the judge, and
    :class:`research.paper.shadow_log.ChainError` for a broken log. Every refusal (frozen
    hashes, grace, model, candidates, recorded heads) is checked BEFORE anything is written,
    so a refused run writes nothing: a recovered head line (or a heads file recreated after a
    crash right after the genesis row) is appended only once every check has passed."""
    if isinstance(grace_s, bool) or not isinstance(grace_s, int) or grace_s < 0:
        raise ShadowRefusedError(f"grace_s must be an integer >= 0, got {grace_s!r}")
    if not isinstance(client.model, str) or not client.model.strip():
        raise ShadowRefusedError(f"the Jev model must be a non-empty id, got {client.model!r}")
    log = DecisionLog(decisions_path(paper_dir))
    rows = log.rows()
    found, warnings = registered_candidates(paper_dir, journal_dir)
    if not rows:
        return _create_genesis(log, journal_dir, found, warnings, client, now_fn, grace_s)
    genesis = rows[0]
    problems = frozen_problems(genesis, journal_dir)
    if problems:
        raise ShadowRefusedError("; ".join(problems))
    if genesis.get("grace_s") != grace_s:
        raise ShadowRefusedError(
            f"the grace is frozen at {genesis.get('grace_s')} s by the genesis row; got {grace_s}"
        )
    if genesis.get("model") != client.model:
        raise ShadowRefusedError(
            f"the Jev model is frozen as {genesis.get('model')!r}; the client asks {client.model!r}"
        )
    cands: dict[str, Candidate] = {}
    for spec in genesis.get("candidates", []):
        name = str(spec["candidate"])
        cur = found.get(name)
        if cur is None:
            raise ShadowRefusedError(
                f"genesis candidate {name} is no longer a verified registration "
                f"({'; '.join(w for w in warnings if name in w) or 'folder missing'})"
            )
        if cur.spec() != dict(spec):
            raise ShadowRefusedError(
                f"genesis candidate {name} changed since the pre-registration (plan "
                f"{cur.protocol.plan_hash[:12]} vs {str(spec.get('plan_hash'))[:12]}, protocol "
                f"{cur.protocol_sha256[:12]} vs {str(spec.get('protocol_sha256'))[:12]})"
            )
        cands[name] = cur
    # every refusal above wrote nothing; only now may the heads file be touched
    hpath = heads_path(journal_dir)
    if not hpath.exists() and len(rows) == 1:
        # a crash between the genesis row and the heads file: nothing to protect yet
        hpath.write_text(HEADS_HEADER + head_line(genesis, float(now_fn())), encoding="utf-8")
    problems = _verify_heads(journal_dir, rows, require_tip=False)
    if problems:
        raise ShadowRefusedError("; ".join(problems))
    try:
        recovered = recover_tip(journal_dir, rows, float(now_fn()))
    except (OSError, ValueError) as exc:
        raise ShadowRefusedError(f"the heads file does not parse: {exc}") from exc
    if recovered is not None:
        logger.warning("shadow: %s", recovered)
        warnings.append(recovered)
    for name in sorted(set(found) - set(cands)):
        warnings.append(f"ignoring {name}: registered after the shadow genesis")
    return ShadowState(dict(genesis), rows, cands, warnings, created=False)


def _create_genesis(
    log: DecisionLog,
    journal_dir: Path,
    found: dict[str, Candidate],
    warnings: list[str],
    client: JevClient,
    now_fn: NowFn,
    grace_s: int,
) -> ShadowState:
    if not found:
        raise ShadowRefusedError(
            "no verified registered candidate to watch: "
            + ("; ".join(warnings) or "the paper folder has none")
        )
    interval = _common_interval(found)
    jdir = Path(journal_dir)
    stray = sorted(jdir.glob(f"*{PREREG_SUFFIX}")) if jdir.is_dir() else []
    if heads_path(jdir).exists():
        stray.append(heads_path(jdir))
    if stray:
        raise ShadowRefusedError(
            f"a shadow pre-registration or heads file already exists ({stray[0]}) but the "
            "decisions log is empty or missing; a pre-registration is never re-made. Restore "
            "the log, or (owner decision, outside Claude Code) move the old entry aside"
        )
    step = interval_seconds(interval)
    now = float(now_fn())
    start = _floor(now, step) + step  # strictly after now: its signal bar has not closed yet
    date = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    specs = [found[n].spec() for n in sorted(found)]
    evaluate_at = (
        max(int(s["decision_end"]) for s in specs)
        + int(DEFAULT_ANALYSIS["evaluate_after_days"]) * 86400
    )
    analysis = frozen_analysis(evaluate_at)
    fingerprint = snapshot_fingerprint()
    env = current_env()
    text = render_preregistration(
        date=date,
        start=start,
        interval=interval,
        grace_s=grace_s,
        candidates=specs,
        model=client.model,
        analysis=analysis,
        fingerprint=fingerprint,
        env=env,
    )
    guard_holdout(text)
    jpath = prereg_path(jdir, date)
    data = text.encode("utf-8")
    try:
        _create_exclusive(jpath, data)
    except FileExistsError:
        raise ShadowRefusedError(f"{jpath} already exists; it is never overwritten") from None
    genesis = {
        "kind": "genesis",
        "written_at": float(now_fn()),
        "start": start,
        "interval": interval,
        "grace_s": grace_s,
        "prereg_journal": jpath.name,
        "prereg_sha256": _sha256_bytes(data),
        "candidates": specs,
        "model": client.model,
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_fingerprint": fingerprint,
        "questions_sha256": QUESTIONS_SHA256,
        "analysis": analysis,
        "btc_symbol": BTC_SYMBOL,
        "btc_since": min(int(s["snapshot_since"]) for s in specs),
        "env": env,
    }
    stored = log.append(genesis)
    _create_exclusive(
        heads_path(jdir), (HEADS_HEADER + head_line(stored, float(now_fn()))).encode("utf-8")
    )
    return ShadowState(stored, [stored], dict(found), warnings, created=True)


# --------------------------------------------------------------------------- candles


def default_loader_for(spec: Mapping[str, Any]) -> CandleLoader:
    """The frozen source as ``compare`` loads it: the public klines host for ``api`` (from the
    load spec's ``since``, at or before ``compare``'s ``warm_from``: the same klines, plus the
    snapshot's extra history), the lab archive for ``archive``."""
    if spec["candle_source"] == "archive":
        return default_loader(load_config())
    since = pd.Timestamp(int(spec["since"]), unit="s", tz="UTC")
    return public_kline_loader(since=since, host=str(spec["candle_host"]))


def _load_spec(spec: Mapping[str, Any], since: int) -> dict[str, Any]:
    """What a loader needs: symbol, frozen source and host, and the first bar to load."""
    return {
        "symbol": str(spec["symbol"]),
        "candle_source": str(spec["candle_source"]),
        "candle_host": str(spec["candle_host"]),
        "since": int(since),
    }


_LoadKey = tuple[str, str, str, int]


class _Frames:
    """Each (symbol, source, host, since) loaded at most once per run, errors kept. The loaded
    frame is only ever cut by :func:`_window`, so rows at or after the fill bar (a forming bar
    or a leaky source) never reach a signal or a snapshot."""

    def __init__(self, loader_for: LoaderFor, interval: str, end: int) -> None:
        self._loader_for = loader_for
        self._interval = interval
        self._end = pd.Timestamp(end, unit="s", tz="UTC")
        self._cache: dict[_LoadKey, pd.DataFrame | Exception] = {}

    def get(self, spec: Mapping[str, Any]) -> pd.DataFrame | Exception:
        key = (
            str(spec["symbol"]),
            str(spec["candle_source"]),
            str(spec["candle_host"]),
            int(spec["since"]),
        )
        if key not in self._cache:
            try:
                loader = self._loader_for(_load_spec(spec, key[3]))
                self._cache[key] = loader(key[0], self._interval, self._end)
            except Exception as exc:  # fail open: a reason in the log, never a crash
                logger.warning("shadow: cannot load %s candles: %s", key[0], exc)
                self._cache[key] = exc
        return self._cache[key]


def _window(frame: pd.DataFrame, since: int, fill_open: int) -> pd.DataFrame:
    lo = pd.Timestamp(since, unit="s", tz="UTC")
    hi = pd.Timestamp(fill_open, unit="s", tz="UTC")
    return frame.loc[(frame.index >= lo) & (frame.index < hi)]


# --------------------------------------------------------------------------- bars


@dataclass
class BarOutcome:
    """What one run wrote for one fill bar."""

    fill_open: int
    signal_bar: int
    status: str
    checked: list[str]
    signals: list[str]
    verdicts: dict[str, dict[str, Any] | None]
    reason: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    genesis: dict[str, Any]
    created: bool
    candidates: list[str]
    bars: list[BarOutcome] = field(default_factory=list)
    pending: list[tuple[int, str]] = field(default_factory=list)  # (fill_open, why)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Evaluated:
    signals: list[str]
    unknown: dict[str, str]  # candidate -> why its signal could not be computed
    histories: dict[str, pd.DataFrame]


def _evaluate(t: int, fill_open: int, checked: Sequence[Candidate], frames: _Frames) -> _Evaluated:
    """Entry signal at signal bar ``t`` of each checked candidate, from closed candles in
    ``[warm_from, fill_open)`` (exactly the window ``compare`` uses); the snapshot history kept
    for a candidate is ``[snapshot_since, fill_open)``."""
    t_ts = pd.Timestamp(t, unit="s", tz="UTC")
    out = _Evaluated([], {}, {})
    for cand in checked:
        spec = cand.spec()
        frame = frames.get({**spec, "since": int(spec["snapshot_since"])})
        if isinstance(frame, Exception):
            out.unknown[cand.name] = f"candles not loaded: {frame}"
            continue
        hist = _window(frame, int(spec["warm_from"]), fill_open)
        if hist.empty or hist.index[-1] != t_ts:
            out.unknown[cand.name] = "no closed candle for the signal bar"
            continue
        try:
            entries = compile_plan(cand.plan, hist).entries
            signal = bool(entries.loc[t_ts])
        except ValueError as exc:
            out.unknown[cand.name] = f"signal not computable: {exc}"
            continue
        out.histories[cand.name] = _window(frame, int(spec["snapshot_since"]), fill_open)
        if signal:
            out.signals.append(cand.name)
    return out


def _decision(
    cand: str,
    t: int,
    f: int,
    written_at: float,
    status: str,
    *,
    snapshot: Mapping[str, Any] | None,
    verdict: Mapping[str, Any] | None,
    model: str | None,
    requested_model: str | None,
    latency_ms: int | None,
    reason: str | None,
) -> dict[str, Any]:
    """One decision row; ``model`` is the SERVED id (None when Jev named none),
    ``requested_model`` the id the client asked for."""
    return {
        "kind": "decision",
        "signal_bar": t,
        "fill_open": f,
        "candidate": cand,
        "written_at": written_at,
        "status": status,
        "snapshot": dict(snapshot) if snapshot is not None else None,
        "snapshot_sha256": snapshot_sha256(snapshot) if snapshot is not None else None,
        "verdict": dict(verdict) if verdict is not None else None,
        "model": model,
        "requested_model": requested_model,
        "latency_ms": latency_ms,
        "reason": reason,
    }


def _bar_row(
    t: int,
    f: int,
    written_at: float,
    status: str,
    checked: Sequence[str],
    signals: Sequence[str],
    *,
    unknown: Sequence[str] = (),
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "bar",
        "signal_bar": t,
        "fill_open": f,
        "written_at": written_at,
        "status": status,
        "checked": sorted(checked),
        "signals": sorted(signals),
        "unknown": sorted(unknown),
        "reason": reason,
    }


class _BarPendingError(Exception):
    """The bar cannot be judged yet (still before its deadline): write nothing."""


def _missed_rows(
    t: int,
    f: int,
    checked: Sequence[Candidate],
    frames: _Frames,
    now: float,
    requested_model: str | None,
) -> tuple[list[dict[str, Any]], BarOutcome]:
    ev = _evaluate(t, f, checked, frames)
    reason = "not judged before the deadline (fill bar open + grace)"
    rows = [
        _decision(
            name,
            t,
            f,
            now,
            "missed",
            snapshot=None,
            verdict=None,
            model=None,
            requested_model=requested_model,
            latency_ms=None,
            reason=reason,
        )
        for name in sorted(ev.signals)
    ]
    detail = "; ".join(f"{k}: {v}" for k, v in sorted(ev.unknown.items()))
    bar_reason = f"{reason}; {detail}" if detail else reason
    names = [c.name for c in checked]
    rows.append(
        _bar_row(
            t, f, now, "missed", names, ev.signals, unknown=list(ev.unknown), reason=bar_reason
        )
    )
    outcome = BarOutcome(f, t, "missed", sorted(names), sorted(ev.signals), {}, bar_reason)
    return rows, outcome


def _judged_rows(
    t: int,
    f: int,
    checked: Sequence[Candidate],
    frames: _Frames,
    btc_spec: Mapping[str, Any],
    *,
    client: JevClient,
    now_fn: NowFn,
    grace_s: int,
) -> tuple[list[dict[str, Any]], BarOutcome]:
    ev = _evaluate(t, f, checked, frames)
    if ev.unknown:
        raise _BarPendingError("; ".join(f"{k}: {v}" for k, v in sorted(ev.unknown.items())))
    t_ts = pd.Timestamp(t, unit="s", tz="UTC")
    snapshots: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, Any] | None = {}
    model: str | None = None
    latency: int | None = None
    if ev.signals:
        btc = frames.get(btc_spec)
        if isinstance(btc, Exception):
            raise _BarPendingError(f"BTC candles not loaded: {btc}")
        btc_hist = _window(btc, int(btc_spec["since"]), f)
        if btc_hist.empty or btc_hist.index[-1] != t_ts:
            raise _BarPendingError("no closed BTC candle for the signal bar")
        for name in ev.signals:
            try:
                snapshots[name] = dict(blind_snapshot(ev.histories[name], btc_hist, t_ts))
            except ValueError as exc:
                raise _BarPendingError(f"{name}: snapshot not computable: {exc}") from None
        # the SERVED model id (None when Jev names none); never the requested alias
        verdicts, model, latency = judge(client, snapshots)
    now = float(now_fn())  # re-read AFTER the Jev call: a late verdict is discarded
    names = [c.name for c in checked]
    rows: list[dict[str, Any]] = []
    shown: dict[str, dict[str, Any] | None] = {}
    if now >= f + grace_s:
        status = "missed"
        reason: str | None = "verdict after deadline" if ev.signals else "written after deadline"
        for name in sorted(ev.signals):
            rows.append(
                _decision(
                    name,
                    t,
                    f,
                    now,
                    "missed",
                    snapshot=snapshots[name],
                    verdict=None,
                    model=model,
                    requested_model=client.model,
                    latency_ms=latency,
                    reason=reason,
                )
            )
    elif not ev.signals:
        status, reason = "no_signal", None
    elif verdicts is None:
        status, reason = "unavailable", "jev unavailable"
        for name in sorted(ev.signals):
            rows.append(
                _decision(
                    name,
                    t,
                    f,
                    now,
                    "unavailable",
                    snapshot=snapshots[name],
                    verdict=None,
                    model=model,
                    requested_model=client.model,
                    latency_ms=latency,
                    reason=reason,
                )
            )
    else:
        status, reason = "judged", None
        for name in sorted(ev.signals):
            verdict = verdicts[name].as_dict()
            shown[name] = verdict
            rows.append(
                _decision(
                    name,
                    t,
                    f,
                    now,
                    "judged",
                    snapshot=snapshots[name],
                    verdict=verdict,
                    model=model,
                    requested_model=client.model,
                    latency_ms=latency,
                    reason=None,
                )
            )
    rows.append(_bar_row(t, f, now, status, names, ev.signals, reason=reason))
    outcome = BarOutcome(f, t, status, sorted(names), sorted(ev.signals), shown, reason)
    if status == "judged" and model is None:
        outcome.warnings.append(
            f"WARNING: Jev served no model id for the judged bar {_utc(f)} (requested "
            f"{client.model!r}); its {len(ev.signals)} decision(s) are logged with model null "
            "and the report excludes them from both arms (no_served_model)"
        )
    return rows, outcome


def _btc_spec(genesis: Mapping[str, Any]) -> dict[str, Any]:
    specs = list(genesis["candidates"])
    own = next((s for s in specs if s["symbol"] == BTC_SYMBOL), specs[0])
    return {
        "symbol": genesis.get("btc_symbol", BTC_SYMBOL),
        "candle_source": own["candle_source"],
        "candle_host": own["candle_host"],
        "since": int(genesis["btc_since"]),
    }


def run_once(
    paper_dir: Path,
    journal_dir: Path,
    *,
    client: JevClient,
    loader_for: LoaderFor = default_loader_for,
    now_fn: NowFn = time.time,
    grace_s: int = DEFAULT_GRACE_S,
    max_bars: int | None = None,
) -> RunSummary:
    """Pre-register on the first run, then log every closed fill bar not yet logged, oldest
    first: judged while before its deadline, ``missed`` after it (see the module docstring).
    Raises :class:`ShadowRefusedError` / ``ChainError`` when the judge must not run."""
    with _process_lock(shadow_dir(paper_dir)):
        state = ensure_genesis(
            paper_dir, journal_dir, client=client, now_fn=now_fn, grace_s=grace_s
        )
        genesis = state.genesis
        interval = str(genesis["interval"])
        step = interval_seconds(interval)
        grace = int(genesis["grace_s"])
        specs = {str(s["candidate"]): s for s in genesis["candidates"]}
        summary = RunSummary(
            genesis=genesis,
            created=state.created,
            candidates=sorted(state.candidates),
            warnings=list(state.warnings),
        )
        done = {int(r["fill_open"]) for r in state.rows if r.get("kind") == "bar"}
        last = _floor(float(now_fn()), step)
        todo: list[tuple[int, list[Candidate]]] = []
        for f in range(int(genesis["start"]), last + 1, step):
            t = f - step
            checked = [
                state.candidates[n]
                for n in sorted(specs)
                if int(specs[n]["protocol_start"]) <= t < int(specs[n]["decision_end"])
            ]
            if checked and f not in done:
                todo.append((f, checked))
        if max_bars is not None:
            todo = todo[: max(0, max_bars)]
        if not todo:
            return summary
        frames = _Frames(loader_for, interval, last)
        btc_spec = _btc_spec(genesis)
        log = DecisionLog(decisions_path(paper_dir))
        for f, checked in todo:
            t = f - step
            now = float(now_fn())
            try:
                if now < f + grace:
                    rows, outcome = _judged_rows(
                        t,
                        f,
                        checked,
                        frames,
                        btc_spec,
                        client=client,
                        now_fn=now_fn,
                        grace_s=grace,
                    )
                else:
                    rows, outcome = _missed_rows(
                        t, f, checked, frames, now, str(genesis.get("model"))
                    )
            except _BarPendingError as why:
                summary.pending.append((f, str(why)))
                logger.info("shadow: bar %s not judged yet: %s", _utc(f), why)
                continue
            stored = log.append_many(rows)
            _append_head(heads_path(journal_dir), stored[-1], float(now_fn()))
            for warning in outcome.warnings:
                logger.warning("shadow: %s", warning)
                summary.warnings.append(warning)
            summary.bars.append(outcome)
        return summary


def run_loop(
    paper_dir: Path,
    journal_dir: Path,
    *,
    client: JevClient,
    loader_for: LoaderFor = default_loader_for,
    now_fn: NowFn = time.time,
    grace_s: int = DEFAULT_GRACE_S,
    settle_s: float = DEFAULT_SETTLE_S,
    retry_s: float = DEFAULT_RETRY_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_summary: Callable[[RunSummary], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    max_iterations: int | None = None,
) -> int:
    """``run_once`` after every bar boundary + ``settle_s``, forever (``max_iterations`` for
    tests). A bar still pending is retried every ``retry_s``. A per-run error is logged and
    the loop continues (fail open); a refusal or a broken chain stops it. Ctrl-C returns 0."""
    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            pending = False
            step = interval_seconds(SHADOW_INTERVAL)
            try:
                summary = run_once(
                    paper_dir,
                    journal_dir,
                    client=client,
                    loader_for=loader_for,
                    now_fn=now_fn,
                    grace_s=grace_s,
                )
                step = interval_seconds(str(summary.genesis["interval"]))
                pending = bool(summary.pending)
                if on_summary is not None:
                    on_summary(summary)
            except (ShadowRefusedError, ChainError):
                raise
            except Exception as exc:  # fail open: log it, never crash silently
                logger.exception("shadow: run failed; retrying")
                if on_error is not None:
                    on_error(exc)
                pending = True
            now = float(now_fn())
            wake = _floor(now, step) + step + settle_s
            if pending:
                wake = min(wake, now + retry_s)
            if max_iterations is not None and iteration >= max_iterations:
                break
            sleep_fn(max(0.0, wake - now))
    except KeyboardInterrupt:
        return 0
    return 0


# --------------------------------------------------------------------------- CLI glue


def make_client(model: str = DEFAULT_MODEL) -> JevClient:
    """The Jev client asking for ``model``; the key comes from ``TYPESAFE_API_KEY`` (env only)."""
    return JevClient(model=model)


def wall_clock() -> float:
    return time.time()


def format_bar(outcome: BarOutcome) -> str:
    """One line per bar: fill bar time, status, signals and verdicts."""
    parts = [f"{_utc(outcome.fill_open)} fill bar: {outcome.status}"]
    parts.append("signals: " + (", ".join(outcome.signals) or "-"))
    if outcome.verdicts:
        shown = []
        for name, v in sorted(outcome.verdicts.items()):
            if v is None:
                continue
            conf = v.get("choice_confidence")
            conf_s = "-" if conf is None else f"{conf:.2f}"
            shown.append(
                f"{name} {v['choice']} (conf {conf_s}, score {v['score']:.2f}, "
                f"fake-out {v['fakeout']:.2f})"
            )
        parts.append("verdicts: " + "; ".join(shown))
    if outcome.reason:
        parts.append(f"({outcome.reason})")
    return "; ".join(parts)


def _print_summary(summary: RunSummary, out: Callable[[str], None]) -> None:
    for w in summary.warnings:
        out(w if w.startswith("WARNING:") else f"warning: {w}")
    for bar in summary.bars:
        out(format_bar(bar))
    for f, why in summary.pending:
        out(f"{_utc(f)} fill bar: not judged yet ({why}); retrying before its deadline")


def cli_shadow(
    paper_dir: Path,
    journal_dir: Path,
    *,
    once: bool,
    grace_s: int,
    settle_s: float,
    model: str = DEFAULT_MODEL,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = print,
) -> int:
    """``paper_trade.py shadow``: exit 0, or 2 on a refusal / broken log. ``model`` is the Jev
    model requested (frozen by the genesis row on the first run)."""
    if not isinstance(model, str) or not model.strip():
        err(f"refusing to run the shadow judge: --model must be a non-empty id, got {model!r}")
        return 2
    client = make_client(model)
    if not client.enabled:
        err(
            "WARNING: TYPESAFE_API_KEY is not set: every signal will be logged 'unavailable' "
            "(fail open; never a skip). Set it in the environment to ask Jev."
        )
    loader_for = default_loader_for
    now_fn = wall_clock
    try:
        if once:
            summary = run_once(
                paper_dir,
                journal_dir,
                client=client,
                loader_for=loader_for,
                now_fn=now_fn,
                grace_s=grace_s,
            )
            _print_start(summary, out)
            _print_summary(summary, out)
            return 0
        first = [True]

        def show(summary: RunSummary) -> None:
            if first[0]:
                _print_start(summary, out)
                first[0] = False
            _print_summary(summary, out)

        return run_loop(
            paper_dir,
            journal_dir,
            client=client,
            loader_for=loader_for,
            now_fn=now_fn,
            grace_s=grace_s,
            settle_s=settle_s,
            on_summary=show,
            on_error=lambda exc: err(f"shadow run failed (will retry): {exc}"),
        )
    except (ShadowRefusedError, ChainError, ValueError, OSError) as exc:
        err(f"refusing to run the shadow judge: {exc}")
        return 2


def _print_start(summary: RunSummary, out: Callable[[str], None]) -> None:
    g = summary.genesis
    if summary.created:
        out(f"pre-registered: {g['prereg_journal']} (sha256 {g['prereg_sha256'][:12]})")
    out(f"shadow start {_utc(int(g['start']))} ({g['interval']}, grace {g['grace_s']} s)")
    out(
        f"Jev model requested {g['model']!r}; evaluation time "
        f"{_utc(int(g['analysis'][EVALUATE_AT]))} (reports before it are interim looks)"
    )
    if summary.created:
        out(
            f"heads file: {HEADS_FILE} in the journal folder (append-only; commit it with "
            "every push: it is the log's tamper anchor)"
        )
    for spec in g["candidates"]:
        out(
            f"  {spec['candidate']} {spec['symbol']} plan {spec['plan_hash'][:12]} protocol "
            f"{spec['protocol_sha256'][:12]} window {_utc(int(spec['protocol_start']))} to "
            f"{_utc(int(spec['decision_end']))}"
        )


def report_name(asof: int, evaluate_at: int) -> str:
    """``interim-<asof>.md`` before ``evaluate_at``, else ``report-<evaluate_at>.md`` (every
    report at or after the evaluation time is the same report, so it has one name)."""
    if asof < evaluate_at:
        return f"interim-{datetime.fromtimestamp(asof, UTC).strftime('%Y%m%dT%H%MZ')}.md"
    return f"report-{datetime.fromtimestamp(evaluate_at, UTC).strftime('%Y%m%dT%H%MZ')}.md"


def _parse_asof(text: str) -> int:
    ts = pd.Timestamp(text)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.timestamp())


def cli_shadow_report(
    paper_dir: Path,
    journal_dir: Path,
    *,
    asof: str | None,
    write: bool,
    expect_head: str | None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = print,
) -> int:
    """``paper_trade.py shadow-report``: the numbers come from the JSONL and the dry-run DBs
    alone; the pre-registration and the heads file in ``journal_dir`` are checked (fail
    closed: missing or changed exits 2), as are the frozen snapshot and question hashes.

    The heads file's last head must cover the log's last row, and its committed copy
    (``git show HEAD:``) must be a prefix of the working file (exit 2 otherwise). A FINAL
    report (``asof >= evaluate_at``) fails closed on the git anchor too: it exits 2 unless the
    committed copy exists, anchors the log's tip (zero uncommitted head lines) and every
    committed version extends the one before it (:func:`final_git_problems`); an interim look
    only warns about those. A final report also exits 2 while a genesis candidate's dry-run DB
    cannot be read. The report lists the recovered head lines with their lag."""
    path = decisions_path(paper_dir)
    try:
        rows = verify_chain(path, expect_head=expect_head)
    except ChainError as exc:
        err(f"the decisions log does not verify: {exc}")
        return 2
    if not rows:
        err(f"{path} is empty or missing: the shadow judge has not started")
        return 2
    genesis = rows[0]
    try:
        now = int(wall_clock())
        when = _parse_asof(asof) if asof else now
        if when > now:
            # a future --asof would read the pre-registered test early on today's data
            raise ValueError(f"--asof {_utc(when)} is in the future (now {_utc(now)})")
        final = when >= evaluate_at_of(genesis)
    except ValueError as exc:
        err(f"cannot build the shadow report: {exc}")
        return 2
    problems = frozen_problems(genesis, journal_dir)
    problems += _verify_heads(journal_dir, rows)
    hpath = heads_path(journal_dir)
    git = check_heads_committed(hpath) if hpath.is_file() else None
    if git is not None and git.problem is not None:
        problems.append(git.problem)
    if final and hpath.is_file():
        problems += final_git_problems(git, len(rows) - 1)
    try:
        problems += check_deadlines(rows, int(genesis["grace_s"]))
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"the genesis grace_s is not usable: {exc}")
    if problems:
        for p in problems:
            err(f"problem: {p}")
        return 2
    try:
        entries = read_head_entries(hpath)
        loaded = load_trades(paper_dir, genesis)
        report = build_report(
            rows,
            loaded.trades,
            asof=when,
            notes=loaded.notes,
            unreadable=loaded.unreadable,
            recovered_heads=recovered_lags(entries, rows),
        )
    except FinalReportRefusedError as exc:
        err(str(exc))
        return 2
    except ValueError as exc:
        err(f"cannot build the shadow report: {exc}")
        return 2
    text = render_report_markdown(report)
    out(text)
    head = rows[-1]["sha256"]
    lags = recovered_lags(entries, rows)
    listed = ", ".join(
        f"seq {seq} {'lag unknown' if lag is None else f'lag {lag:.0f} s'}" for seq, lag in lags
    )
    out(
        f"chain head sha256: {head} ({len(rows)} rows); every recorded head in {HEADS_FILE} "
        f"matches ({len(entries)} head lines, {len(lags)} recovered after a crash"
        f"{': ' + listed if listed else ''}). Commit the heads file; --expect-head adds a "
        "check against a head kept elsewhere"
    )
    if git is not None and git.warning is not None:
        err(f"warning: {git.warning}")
    if git is not None and git.history_problem is not None:
        err(f"warning: {git.history_problem} (a final report refuses)")
    if git is not None and git.uncommitted is not None:
        out(f"heads file: {git.uncommitted} head lines not yet committed (git HEAD)")
    if write:
        target = shadow_dir(paper_dir) / report_name(report.asof, report.evaluate_at)
        try:
            _create_exclusive(target, text.encode("utf-8"))
        except FileExistsError:
            err(f"{target} already exists; reports are never overwritten")
            return 2
        out(f"written: {target}")
    return 0


__all__ = [
    "BTC_SYMBOL",
    "DEFAULT_MODEL",
    "HEADS_FILE",
    "SNAPSHOT_EXTRA_BARS",
    "BarOutcome",
    "Candidate",
    "GitHeadsCheck",
    "HeadEntry",
    "RunSummary",
    "ShadowRefusedError",
    "ShadowState",
    "check_heads_committed",
    "cli_shadow",
    "cli_shadow_report",
    "decisions_path",
    "default_loader_for",
    "ensure_genesis",
    "final_git_problems",
    "format_bar",
    "frozen_problems",
    "head_line",
    "head_problems",
    "heads_path",
    "parse_head_entries",
    "prereg_path",
    "read_head_entries",
    "read_heads",
    "recover_tip",
    "recovered_lags",
    "registered_candidates",
    "render_preregistration",
    "report_name",
    "run_loop",
    "run_once",
    "shadow_dir",
    "tip_problem",
]
