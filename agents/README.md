# agents/: AI strategy research (PLAN.md Phase 8)

An autonomous research loop built on the Claude Agent SDK. It looks for strategy ideas on the
web, tests them in the lab through the `strategy-lab` MCP server, and ends with either a
**candidate** that passes every frozen success criterion (then forward paper trading) or a
written **"no edge found"** report. Nothing here places, cancels, or modifies orders.

## Running a research run

From `agents/`:

```bash
uv run python research_loop.py --dry-run                     # offline smoke test, temp files only
uv run python research_loop.py --symbols BTCUSDT,ETHUSDT --interval 4h
uv run python research_loop.py --symbols BTCUSDT --interval 1h --family "regime filters"
uv run python research_loop.py --resume r-20261024-120000-abc123
```

Before the first live run, the owner must:

1. **Install the success criteria** in your own terminal (the file is read-only to Claude while
   `.claude/lab.lock` exists):
   `cp agents/config/success_criteria.proposed.toml lab/config/success_criteria.toml`.
   Edit thresholds and budgets there first if you want to. They are frozen into the ledger when a
   run starts and cannot change mid-run. No CLI flag can raise a budget.
2. **Authenticate the Agent SDK**: export `ANTHROPIC_API_KEY` in your shell (an API key with a
   spend cap is the most predictable for long runs) or be logged in to Claude Code. The SDK
   needs the `claude` CLI on `PATH`.
3. Optional: export `TYPESAFE_API_KEY` for the Jev gates. Without it, every gate falls back to
   local heuristics and the loop still completes.
4. Make sure the design-window candles are downloaded for the symbols and interval (Phase 7).

Exit code: 0 when the run finished (candidate, no_edge, or budget_exhausted), 1 on error (the
state file says `error`; continue with `--resume RUN_ID`), 2 when the success criteria are
missing.

## What happens in a run

| step | who | tools |
|---|---|---|
| start run: criteria + budget frozen in the ledger (with the lab-config fingerprint, `cfg=`), written to the journal first | Python | `lab.research.runs.start_research_run` |
| Scout: up to 2 × `max_hypotheses` hypotheses with source URLs | Claude (Sonnet) | WebSearch, WebFetch |
| Screen: hypothesis text and source excerpts, injected instructions dropped | Jev + heuristics | `research.screening` |
| Idea library + dedupe | Python + Jev | `research.ideas`, `research.gates.check_duplicate` |
| Designer: plan JSON, validate, backtest, sweep, walk-forward, adversarial, then cross-check (required before any holdout) | Claude (Opus) | `strategy-lab` MCP (incl. `run_crosscheck`), holdout disabled |
| Criteria: frozen success criteria evaluated | Python | `lab.research.criteria.evaluate_plan` |
| Sweep triage (advisory): the plan's Optuna trials, matched by the study names in its sweep evidence | Jev + Python | `research.gates.triage_sweep` |
| Critic: fresh session, sceptical brief | Claude (Opus) | `get_report`, `get_ledger`, `get_budget` |
| Decide: refine (logic change, ≤ 2) / abandon / promising | Python | |
| Promising (design-window criteria pass) and holdout budget remains: event-driven cross-check FIRST. Unavailable or disagreeing → stays `promising`, no holdout check spent (never dead, never re-tuned) | Python | `lab.research.pipeline.crosscheck_evidence` |
| Cross-check matched: `evaluate_plan` again; only if `pre_holdout_passed` (which includes `require_crosscheck_match`) is a holdout check spent. Otherwise stays `promising`, nothing spent | Python | `lab.research.criteria.evaluate_plan` |
| Holdout: fail → dead; refused because this logic was already checked → dead. Pass → `evaluate_plan` again; candidate only if `passed` | Python | `lab.research.holdout.run_holdout_check`, `evaluate_plan` |
| Report: Obsidian-friendly journal, no holdout metrics | Python | `research.journal` |

Outcomes: `candidate` (at least one plan passed every criterion, the holdout, and the
cross-check; it still needs 4–8 weeks of forward paper trading before it is "successful");
`no_edge` (every idea or the planned hypotheses tested, or every holdout
check spent, with no candidate); `budget_exhausted` (dollars, proposals, or backtests ran out);
`error`.

## Budgets

| budget | enforced by |
|---|---|
| hypotheses | orchestrator (`research.budget.BudgetTracker`) |
| model dollars | orchestrator. Every call gets `max_budget_usd = remaining − 20% of max_usd`; a call that cannot fit is refused before it starts. The SDK's limit is a client-side estimate, so the reserve absorbs overshoot. Unknown costs fail closed: a missing `total_cost_usd`, a call that raises after starting, or a retry with an unknown cost is charged its full cap. The cap is written to the resume sidecar as a reservation before each call and settled after it, so a crash mid-call is charged at the cap on `--resume`. Because the cap is all remaining money minus the reserve, one unknown-cost call ends the run's model spending. |
| proposals, backtests, holdout checks | the lab / MCP server (server-side), mirrored from the ledger with `run_usage` |

## Safety layers

- Each role runs with a restricted built-in tool set (`tools=`), `disallowed_tools`,
  `permission_mode="dontAsk"`, `strict_mcp_config` (the project's unpinned `.mcp.json` server is
  ignored), and SDK `PreToolUse` hooks: `deny_unsafe_tools` (edits, shell, holdout, start_run,
  order/withdraw names) plus a per-role allowlist.
- `setting_sources=["project"]` loads the project's `.claude/settings.json`, so the repository's
  hooks also apply: `protect-files.sh` and `guard-bash.sh` block edits to `lab/lab/engine/`,
  `registry/`, `stats/`, and `lab/config/success_criteria*` while `.claude/lab.lock` exists.
  `tests/test_lab_protection.py` runs those hook scripts to prove it.
- The designer's MCP server is pinned to the run (`STRATEGY_LAB_RUN_ID`) with the holdout disabled
  (`STRATEGY_LAB_DISABLE_HOLDOUT=1`). Only Python spends holdout checks.
- Scout output (including each idea's title and family) is untrusted: screened, sanitized,
  wrapped in `<untrusted_source>` markers in the Designer and Critic prompts, and
  journaled inside a warning callout. Hypotheses tied to an injection-suspected source are
  dropped entirely.
- Secrets are read from the environment by the SDK and Jev clients only; nothing here writes them
  to state, the journal, or logs.

## Paper trading (pre-registered protocol)

A candidate is paper traded with freqtrade `--dry-run` only (no exchange keys, no orders; the
Binance Spot Testnet retires endpoints and wipes accounts on reset, so it is not the venue).
Before the bot can trade, `prepare` freezes what it must show, so "consistent with the
backtest" is a number and "failed" is declared on schedule (PLAN.md Phase 13):

```bash
uv run python paper_trade.py prepare --plan-file plan.json --candidate NAME \
    [--claimed-sharpe 1.2] [--ledger PATH] [--candle-source api|archive] [--launch]
uv run python paper_trade.py compare --candidate NAME [--verbose]   # weekly
```

- `prepare` reads the lab ledger strictly read-only (`Ledger.open_readonly`: no migrations,
  no hash-chain backfill; schema >= 4; `--ledger` overrides the lab's `ledger.db`). It needs a
  design-window trial of the plan's `plan_hash` **in a research run** (a manual or web-UI
  backtest does not count); its trial id and run id go into the protocol. The hypothesis is
  the lab's **behaviour family** (`lab.research.family.behaviour_family`, reused read-only:
  the same logic on any interval, or design-window returns correlated >= the lab's family
  threshold, so a no-op-clause twin is the same hypothesis). A family whose locked-split check
  did not pass (or whose outcome cannot be tied to it; an uncomparable same-feature plan's
  failed check counts too: fail closed) is a dead hypothesis and is refused. Otherwise
  `locked_split_check` is frozen in `protocol.json` and the journal frontmatter as one of:
  - `passed`: THIS plan's own check passed, no other plan of its family (or uncomparable
    same-feature plan) was checked, and the ledger is the lab's own (`cfg.ledger_path`);
  - `family_checked_not_this_plan`: another family member was checked instead (the lab marks
    this variant's row FAIL: it can never be checked);
  - `passed_other_ledger`: the check passed, but `--ledger` pointed at another file;
  - `unchecked`: no check in the family.

  Every state but `passed` makes a paper pass "implementation consistent, but NOT a success
  candidate". The resolved ledger path, whether it is the lab's, and the run's hash-chain
  head (`Ledger.chain_head`) are frozen in the protocol's `source`.
- The claimed Sharpe is the **minimum walk-forward OOS Sharpe** of this `plan_hash`'s own
  results in the ledger (the lab's `wf_oos_sharpe_min` criterion takes the minimum over the
  whole behaviour family, which can be lower). Without walk-forward evidence `prepare`
  refuses unless `--claimed-sharpe` is given, and that value is labelled **owner-supplied**.
  The design-window Sharpe of a selected plan is never the claim.
- One hypothesis, one forward test: a new candidate name is refused when a protocol under
  `agents/data/paper/` **or a paper journal entry** (`*-paper-*.md` frontmatter) already has
  the same `plan_hash` or logic hash (same rules with other numbers or on another interval),
  a plan or logic in this plan's behaviour family (e.g. its no-op-clause twin), or the same
  journal name (`foo_bar` / `foo-bar`), so deleting a paper folder cannot free a plan.
- It writes `protocol.json` and then the journal entry
  `agents/journal/<date>-<symbol>-paper-NAME.md` (frontmatter: candidate, plan_hash,
  logic_hash, run_id, trial_id, date, protocol_sha256, mode, locked_split_check, outcome),
  both created exclusively,
  **before** any freqtrade file and before any launch. If writing the freqtrade files fails,
  re-run `prepare` with the same name; if the process died between the two registration
  writes, `prepare` of the same name writes the missing journal entry (only while the dry-run
  DB holds no trades). It refuses when the dry-run DB already holds trades. A registered
  protocol is kept byte for byte; a different plan, `--claimed-sharpe` or `--candle-source`
  needs a new candidate name.
- The protocol (`research/paper/protocol.py`; parameters with justifications in
  `config/paper_protocol.toml`):
  - **Data**: the candle source (public API host or the lab archive) and the warm-up length
    are frozen; `compare` refuses another `--source` or host.
  - **Entries** over the decision horizon: the union of the Poisson 95% interval, the
    2.5–97.5% range of rolling design blocks, and a predictive interval for one new block
    (`mean ± t · sd · sqrt(1 + 1/m)`, with m = the number of non-overlapping blocks in the
    design window, because rolling blocks overlap).
  - **Exposure** (share of bars in market at bar close): design blocks scored flat at their
    start, quantiles united with the same predictive interval, ± 0.02.
  - **Timing**: `compare` replays the plan with the lab engine on the same candles (frozen
    warm-up, flat at the start, frozen cost model). Paper entries are matched to lab entries,
    and paper exits of **every** reason to lab exits of the same kind (signal, stop-loss,
    take-profit/ROI), within ±1 bar; a stop or ROI exit where the lab exited on a signal is
    unmatched. The generated bot runs the lab's own `compile_plan` on closed candles from the
    protocol's frozen `data.warm_from` (rendered into the strategy as `WARM_FROM`; freqtrade
    itself holds ~1000 candles, so without it recursive EMAs started ~1000 bars back and honest
    bots disagreed near crossings), the same first bar as the lab replay, so by construction an
    honest bot fills on the lab's fill bars unless the two candle feeds differ. The strategy
    also carries the protocol's `START`: `enter_long` is set only on candles dated at or after
    it, the lab replay's first signal bar, so a bot launched before the start (`prepare
    --launch`) cannot enter at the start on a pre-start signal or hold a pre-start position
    into the test (exits are not gated). `compare` refuses a strategy file whose `WARM_FROM`
    or `START` is not the protocol's (missing, duplicated or `None` included). ONE joint count of **irregular fills**
    (unmatched paper entries + unmatched lab entries + unmatched paper exits + unmatched lab
    exits + matched fills off the lab's bar) may be at most `max(1, floor(0.05 × (all lab
    fills + all paper fills)))` (`irregular_share`; the one allowance covers a rare warm-up
    near-tie, i.e. one bar on which the exchange feed and the comparison's candles disagree; rounded down because rounding up would excuse a whole skipped trade at 21–40
    fills). It replaces the old 0.8 match-rate floor, whose 20% slack let skipped, extra and
    2+-bar-shifted trades pass: a fill shifted beyond the ±1-bar tolerance is now unmatched
    on both sides (2 irregular fills), so shifting further never helps. Reasons name each
    kind: **late** "the backtest acted earlier than live: possible look-ahead in the lab",
    **early** "the bot acted before the bar closed: possible use of the still-forming candle",
    unmatched paper fills (the bot traded where the lab did not) and unmatched lab fills (a
    skipped trade). With a missing candle the lab replay changes from that bar on, so only
    irregular fills settled before the first gap can fail it. Settled unmatched paper entries
    above `max(1, floor(0.05 × 4 × hi))` fail at once.
  - **Implementation shortfall** on lag-0 entries and signal exits only (their lab fill is the
    next bar's open; stop/take-profit fills are at a level, so their timing is matched but
    their price is not judged), against the modelled fill (`open × (1 ± modelled slippage)`,
    plus any fee difference). At the decision bar the median must be at most 1 × the modelled
    slippage (5 bps), and the median against the raw bar open at least −4 × that (below is
    implausibly favourable: possible look-ahead). Weekly medians above 5 bps are **alerts**
    only. Gross breakage (a median above 20 bps) fails at once, judged on each week with at
    least 3 fills **and** on every rolling run of 3 consecutive judged fills. The protocol
    states the false-fail probability of each rule (normal approximation
    `median ~ N(μ, (π/2)σ²/n)` with an assumed fill noise σ = 10 bps), and of the early gross
    rule as a union bound over the weekly looks and the expected `n − 3 + 1` fill windows. With
    the default config that bound is under 1% up to about 30 expected judged fills and grows
    past it for busier plans (about 1.8% at 60 fills).
  - **Honest false-fail: a single-path estimate** (can be off in either direction; excludes
    regime change). A seeded leave-overlap-out bootstrap (2000 draws) judges design blocks at
    random starts as the forward period of an honest bot, against bands rebuilt without the
    blocks that overlap each draw. It comes from ONE design path: in the final review's
    simulation on a stationary random walk the true honest false-fail ranged about 0–10%
    across design paths, and the estimate missed in both directions (1.8% where the truth was
    10%; 5.9% where it was 0.8%). It is printed with that range as a caveat, never as a bare
    rate, a floor or a bound.
  - **Stopping rule**: the MinTRL (`lab.stats.sharpe`, 95%, against a Sharpe of 0, per bar,
    365-day annualisation) at the claimed Sharpe, with 80% power. The decision comes at that
    length, clamped to 28–56 days. The rules print the smallest annual Sharpe 56 days can test.
    If the claim needs more than 56 days, the protocol says upfront that the test checks
    implementation only. A Sharpe pass means "SR > 0 at 95% (PSR); the claimed Sharpe itself
    is not confirmed".
- Verdicts (`compare`), against the frozen protocol:
  - **fail**: any breach. Entries above the band, exposure above the band's maximum, gross
    shortfall breakage, or settled unmatched paper entries beyond the most irregular fills the
    decision bar could allow fail at any time; the rest (entry band, exposure band, entry and exit
    irregular-fill count, median shortfall, PSR when tested) at the decision bar. Each
    reason says whether it is a **regime difference** (entries/exposure off while every fill
    matched the lab at lag 0) or an **implementation difference** (timing, shortfall).
  - **continue**: before the decision bar, or at it with **incomplete data** (a bar time in
    `[start, decision bar)` missing, or a paper fill on a missing candle) when no rule fails
    whatever the missing data hold (exposure is bounded by held/N and (held + missing)/N;
    entries, timing and shortfall do not depend on bars without fills).
  - **inconclusive**: at the decision bar, every check inside tolerance but the protocol cannot
    discriminate (fewer than 5 expected entries, an exposure band at least 0.8 wide, an entry
    band that includes 0, or fewer than 3 judged fills; the protocol says upfront when this
    will happen), or **data** still incomplete 7 days after the decision bar.
  - **pass**: at the decision bar with complete data and every check inside tolerance; the
    headline names exactly what was checked, the irregular count with its breakdown
    (unmatched per side, off-lag per side; "every fill matched at lag 0" only when it is 0),
    and, unless `locked_split_check` is `passed`, "implementation consistent, but NOT a success
    candidate" with the reason.
- `compare` refuses if `protocol.json` no longer matches the sha256 in the journal entry. It
  appends one dated verdict line to the journal entry under an exclusive lock (fsynced
  temporary file, atomic replace) and only then writes `comparison-<date>.json`/`.md` (with
  `protocol_verdict`: coverage, alerts, fail kinds, weekly medians). It never edits the
  registered section. The **first final verdict** (fail at any time; pass / fail /
  inconclusive at the decision bar) is written once into the frontmatter (`outcome`,
  `verdict_asof`, `verdict_protocol_sha256`); later `compare` runs print it and change nothing.
- Results are historical, not forward-looking. A candidate is "successful" only after every
  success criterion **and** a passing forward test.

## Files

- `research_loop.py`: CLI.
- `research/orchestrator.py`: the loop, role request/result types, output schemas, prompts.
- `research/sdk_runner.py`: SDK options per role, hooks, JSON-output parsing with one retry,
  and the scripted runner used by `--dry-run`.
- `research/budget.py`, `research/state.py`: budgets and atomic run-state files
  (`agents/data/runs/<run_id>.json` plus a `.resume.json` sidecar).
- `prompts/{scout,designer,critic}.md`: role briefs (system prompts).

Results are historical backtests and are not forward-looking. A candidate becomes "successful"
only after it passes every success criterion **and** 4–8 weeks of forward paper trading.
