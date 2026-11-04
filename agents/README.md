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

## Paper risk controls and sizing (Phase 20)

For paper tests started after Phase 20 only. A protocol without these blocks behaves exactly as
before (version 7, same bytes, same sha256), so the HC tests and the shadow judge are untouched.
Sizing controls risk; it does not create an edge and never rescues a plan that fails the lab.

```bash
cp config/paper_risk.example.toml my-test.toml     # the numbers are examples: choose them first
uv run python paper_trade.py prepare --plan-file plan.json --candidate NAME \
    --risk-config my-test.toml [--launch]
uv run python paper_trade.py guard --candidate NAME --watch    # keep this running
uv run python paper_trade.py guard --candidate NAME            # one check
uv run python paper_trade.py compare --candidate NAME
uv run python paper_trade.py compare --portfolio [--candidates a,b]   # information only
```

- **Frozen before the start.** `--risk-config` freezes `risk_controls` and/or `sizing` into a
  version 8 protocol, so its sha256 covers them, and the journal entry states them. The template
  is `agents/config/paper_risk.example.toml`. A registered candidate refuses other values: a
  different size is a new test. `prepare` without the flag keeps a registered candidate's blocks,
  and `prepare --launch` is the restart path. A circuit breaker needs a `wallet`: the losses are
  measured from it, and it must equal `sizing.wallet` when both are set.
- **The whole config is frozen.** Every version 8 protocol also freezes `paper_config`: the stake,
  wallet, `max_open_trades`, exchange and public host `config.json` is rendered from, whichever
  block the test has. On real freqtrade, editing `fee`, `dry_run_wallet` or `stake_amount` moved
  masked entries from 1 to 3, 6 and 9 of 29, on a test that froze only protections. `compare`,
  `guard` (each bar) and a re-prepare compare the whole file with a fresh render and refuse any
  difference, at any depth. A flag that differs from a frozen value is refused too, whether or not
  `config.json` still exists, so deleting the file frees nothing. `db_url` is the one key that
  holds a path: a moved folder is named as moved. A re-prepare renders `plan.json`, the strategy,
  `config.json` and `meta.json` again and keeps the protocol; on a test with trades, any of those
  it changed gets a line under its own journal heading, before the verdicts. A
  halted folder still gets its stop. These checks read the file as it stands, so a bot started on
  an edited file that was then restored by hand is not caught, and a later change to
  `render_config` refuses running tests, like the strategy check.
- **Checks at `prepare`.** Any test with a block: averaging down is refused in what is about to be
  written and in any strategy already in the folder (`position_adjustment_enable` is refused in
  every config). With sizing: the plan needs a stop; a `--stake-amount` or `--wallet` that
  contradicts the block is refused.
  A full book at the maximum stake must fit in 80% of `max_total_exposure_pct`, so it stays
  fundable through a 20% loss: freqtrade clips a stake to the available balance, and `compare`
  would then report the clipped stake as an implementation problem. `min_stake_pct` must give at least 7.5 (Binance's 5 USDT
  minimum plus freqtrade's stop-loss reserve), and with a volatility target it may not exceed the
  base stake, or the floor itself would risk more than registered.
- **Volatility only shrinks a stake.** The scale is `min(1, vol_target / realized_vol)`. PLAN.md
  Part E2 wrote `vol_target / realized_vol` without the cap; low volatility would then push risk
  above the registered 0.5-1.5%, so the cap is the owner-visible rule.
- **Size steps need proof.** A step up names `prior_test`, which must be the very same plan
  (`plan_hash`, not just `logic_hash`: logic drops parameters, literals, stops and the interval,
  so EMA 5/60 at a 2.5% stop once borrowed the pass of EMA 8/30 at 10% and staked 4x at "2x"), with
  a final `pass` in `compare`, a frozen decision horizon of at least 90 days, a mode other than
  `implementation_only`, a locked-split check that passed (any other state is "NOT a success
  candidate"), no `halt.lock`, the same volatility target and window, the same protections if it
  had any, and its circuit breaker with no threshold looser. Each real size may at
  most double: wallet x risk x multiplier, wallet x maximum stake, wallet x minimum stake. A step
  must be a step: the smallest or largest stake this plan can really reach has to rise, so a
  number no stake reaches counts for nothing and the same plan at the same size stays one
  hypothesis. One proof unlocks one step. A default 28-56 day protocol can never unlock a step:
  the proof needs a protocol registered with `min_days >= 90` (`--protocol-config`). A chain A -> B
  -> C of one plan may register, each step on its own 90-day pass; a failed test can never be
  retried this way, and a re-tuned plan is refused even if it names a `prior_test`.
- **In the bot.** The strategy gets `custom_stake_amount` (`sized_stake`'s exact source, reading a
  closed-candle volatility column built from the same candles as the signals), a `protections`
  property, and `confirm_trade_entry`, which refuses entries while `halt.lock` sits beside the
  strategy file. The config gets `max_open_trades`, the frozen wallet, `tradable_balance_ratio`
  (the exposure cap) and `stake_amount: "unlimited"`: freqtrade checks a numeric stake against the
  balance before `custom_stake_amount` runs and silently drops the entry when the balance is below
  it (real run: 3 entries instead of 28).
- **Circuit breaker.** `guard` marks equity at each closed bar (wallet + closed P&L + open
  position at that bar's close) and finds the first bar at or beyond a frozen threshold: daily
  loss against the last bar before that UTC day, drawdown against the running peak. On a trip it
  writes `agents/data/paper/<NAME>/halt.lock`, stops the process, and adds a line under its own
  journal heading, before the verdicts. Exit 0 clear, 1 halted, 2 refused or cannot check. Candles
  that stop before the last closed bar exit 2 rather than read as clear, unless the bars present
  already trip, so the frozen `archive` candle source, which lags, cannot be guarded. An unreadable
  `halt.lock` is still a lock. A protocol with no circuit breaker is refused and nothing changes.
- **The breaker acts only while `guard --watch` runs.** `--launch` does not start it; `prepare`
  prints the command. The watcher retries network failures, rate limits, Binance's 418 and 403, a
  locked DB and late candles at the next bar, and stops with exit 2 on anything else, and on a 403
  at 12 checks in a row (`MAX_CONSECUTIVE_403`): that is a refusal, not a blip. A late newest
  bar gets 3 more tries 20 s apart first, so a slow candle does not cost a whole bar. `compare`
  applies the same thresholds
  afterwards whether or not `guard` ran, and reads `halt.lock` too: a trip is a final `fail` with
  its own reason, never an irregular-timing label, so it can never pass or unlock a step. That
  catches a test nobody guarded; it does not stop a bot that went on trading until then.
- **Stopping the bot.** `prepare --launch` writes `bot.pid` for a protocol with a circuit breaker.
  `guard` sends SIGTERM only when the pid's command line is `freqtrade trade --dry-run` on this
  folder's config. A bot started by hand has no `bot.pid`, and any pid that cannot be verified is
  never signalled: `guard` prints the manual step, and the strategy check still blocks entries.
  It keeps managing open positions until stopped. When `bot.pid`'s process no longer exists,
  `guard` says that pid is gone and that a bot started by hand would not show there, since a stale
  `bot.pid` says nothing about one; `ps` itself failing (a sandbox, say) is not read as gone.
- **`halt.lock`.** `prepare` and `launch` refuse while it exists. Only the owner deletes it,
  outside Claude Code; no code path removes it. A halted test is not resumed quietly: the bot
  stops trading while the lab replay continues, so `compare` will show the missing trades.
- **`compare` with protections.** It runs `freqtrade download-data`, then `freqtrade backtesting
  --enable-protections --export trades`, then replays the lab again with entry signals off on the
  bars a recorded lock blocks, so positions and later entries follow the bot's real path. A lock
  blocks the bars after its start up to its end: freqtrade records it on its exit's bar and runs
  entries before exits, so a trade stopped out on its own fill bar was not blocked. An entry
  outside a lock stays and counts as irregular if the bot did not make it. A backtest without the
  flag is refused. freqtrade missing, a failed run, an unexpected export (locks included), data
  that stop more than a bar before the end, a first replay entry with no freqtrade entry, or a
  freqtrade entry on a bar its own locks block is a bridge failure (below); there is no fallback to
  an unprotected comparison. Limits: the locks are the backtest's, so a skip inside one is not caught for that
  entry. The frozen entry and exposure bands come from an unprotected design replay, which biases a
  protected test towards false fails.
- **`max_blocked_share`.** Required with protections and frozen (the template's 0.333 is an
  example). The share is the unprotected replay's entries that the masked replay does not make on
  their bar, a shifted entry included, and every journal line of a protected test carries it as
  masked/total. Above the cap the best verdict at the decision bar is a final `inconclusive` naming
  it, never `pass`: the test measured the protections more than the plan. A real fail still fails.
- **A fail is never demoted, and there is no verdict forever.** Final verdicts stay write-once, a
  trip outranks everything, and once a trip is known the protected backtest is skipped.
  - A paper stake that differs stops nothing: Sharpe, entries and exposure are per unit or counted,
    and the masked replay is still valid. It is named as an implementation problem in the verdict
    and the journal. A rule that fails is a `fail`; with every other check fine at the decision bar
    the verdict is a final `inconclusive` naming it, never `pass`.
  - A bridge failure makes only the replay checks unknown (`irregular_fills`, `shortfall*`,
    `irregular_fills_cap`). Entries, `entries_cap`, exposure, `exposure_cap` and Sharpe come from
    the dry-run DB and the candles and are still judged, and any of them failing is a `fail`, before
    the grace too. With none failing, `compare` exits 2 until the decision bar plus the frozen
    `data_grace_days`, then records a final `inconclusive` naming the reason. What a bridge failure
    can still hide: a replay check that would have failed, past the grace, on a test where nothing
    else fails.
  - A strategy, `config.json` or `plan.json` that no longer matches the protocol exits 2, saying
    what differs: restore the file, or let the grace pass. A new candidate name is no way out, the
    same plan is refused under one. From the decision bar plus the grace, `compare` records a final
    `inconclusive` naming the void: the paper data cannot be trusted, so this is the one case where
    judging does not run, and there is no comparison file. A tripped breaker still gives `fail`. A
    moved folder is not a void: a re-prepare repairs it, and until then it exits 2.
- **`compare` with sizing.** The replay is sized with the same stake function at each trade's
  signal bar. `compare` exits 2 when the strategy is not a fresh render of the protocol, byte for
  byte, or when the config is not the frozen one. A matched paper stake is a problem when it is too
  far from the expected stake: below it, 1% or one lot (1.5 USDT), whichever is larger, and above it
  only 1%, because amounts round down (real gaps lay between -0.805 and 0 USDT; BTC lot rounding measured
  0.40% near 200 USDT and 0.61% near 80). The report and the verdict line gain a sizing
  line: risk per trade, exposure cap, largest single loss as % of wallet, and that sizing does not
  change whether the plan has an edge.
- **`compare --portfolio`.** N_eff on the coins' own daily returns over the same window, which is
  the figure to quote, then N_eff on the bots' returns, with Pearson correlations over common
  complete UTC days and the days each pair was in the market together. A bot is flat most days, so
  its series reads as independent: seven coins at correlation 0.8 give 5.03 at 25% in market
  against about 1.45 for the coins. By default it reads the largest cohort of tests with one
  `start` and names the others as excluded, so a late entrant cannot cut the window. Short windows
  bias N_eff low: even independent series read below their count. With no more common days than
  coins the matrix's rank cannot support the figure, so the report says "too few days to quote"
  instead of giving one. It writes nothing and changes no verdict. Confirmations across correlated
  coins are one piece of evidence, not N.
- **Checked against real freqtrade.** Version 2026.9 at `~/.venvs/freqtrade`, which the HC bots
  use (it only needs to be on PATH), ran `download-data` and `backtesting --enable-protections`
  on 2026-11-04 with sizing and protections: the export shape, the flags, the host override
  (through a scratch proxy setting in the sandbox) and `custom_stake_amount` in a backtest. A bot
  identical to its protected backtest then read 0 irregular fills against the masked replay, with
  the modelled entry slippage at 0, on the `rsi < 42` level plan at cooldowns 1, 3, 6 and 24, on a
  crossover plan and with `MaxDrawdown` firing. At the frozen 5 bps slippage the lab's stop sits 5
  bps higher than the backtest's, and on that level plan with a 2% stop one such stop cost 4
  irregular fills: 4 of 6 allowed at cooldowns 1 and 3, 9 of 6 at 6, 7 of 4 at 24. Not yet seen:
  protections and `custom_stake_amount` in a live dry-run, another freqtrade version. Run one
  short test and read the bot log before relying on them.

## Jev shadow judge (Phase 17)

Tests, forward only, whether Jev can tell good HC paper entries from bad ones. An LLM judge
cannot be backtested honestly (a model asked about past bars may remember them), so it only
ever sees bars that close after its pre-registration.

```bash
uv run python paper_trade.py shadow                  # runs forever: after each 4h close
uv run python paper_trade.py shadow --once           # process the closed bars once and exit
uv run python paper_trade.py shadow --model jev-latest   # the model asked (frozen on run 1)
uv run python paper_trade.py shadow-report [--asof 2026-12-26T00:00Z] [--write] \
    [--expect-head SHA256]
```

- **Key from the environment only.** Jev is asked with `TYPESAFE_API_KEY`. Without it the
  shadow prints a loud warning and keeps running: every signal is logged `unavailable`.
- **Watches, never steers.** It recomputes every registered HC candidate's entry signal with
  the lab's `compile_plan` on the same frozen candle source, host and warm-up window
  (`[warm_from, fill bar)`) as `compare`. It writes nothing in any candidate folder or
  candidate journal entry, never opens a dry-run DB, and nothing acts on a verdict. Its files
  are `agents/data/paper/jev-shadow/` (no `protocol.json`),
  `agents/journal/<date>-multi-jev-shadow.md` and `agents/journal/jev-shadow-heads.md`.
- **Blind snapshot.** Rounded ratios only, from bars closed at the signal bar. The snapshot
  reads 300 more closed bars before `warm_from` (`snapshot_since`, frozen per candidate) so
  its EMA200 can settle and a short gap in the warm-up cannot leave a bar without a snapshot.
  Known limitation: it is blind to symbol, dates and prices, but the BTC setup can be
  recognised from its BTC fields (its `ret_42_pct` equals `btc_ret_42_pct`, its
  `dist_ema50_pct` equals `btc_trend_pct`). The bars are forward, so this is not look-ahead.
- **Pre-registration first.** The first run writes the journal entry (start time, candidates
  with plan and protocol hashes, the blind snapshot fields and fingerprint, Jev's context and
  questions verbatim, the deadline rule, the analysis and its evaluation time), then the
  genesis row of the hash-chained `decisions.jsonl`, which freezes the start, grace,
  candidates, requested model, snapshot version and fingerprint, question hash, analysis, the
  entry's sha256 and the environment (`env`: Python, numpy and pandas versions and the sha256
  of `agents/uv.lock`), then the heads file. Later runs refuse (exit 2) if the entry, the
  questions, the snapshot formulas, a candidate's plan or protocol, the grace or the model
  changed, or a recorded head no longer matches. A candidate registered later is ignored.
- **Model.** `--model` (default `jev-latest`, the client default) is the model requested and
  is frozen by the genesis row. Every decision row records the model id Jev reports serving
  (`model`, null when Jev names none: the requested alias never stands in for it) and the
  `requested_model`; a judged bar with no served id prints a loud `WARNING:`. The analysis
  pins the served id of the first judged decision that has one, normalised to major.minor for
  `<name>-<major>.<minor>.<patch>` (`jev-1.13.0` and `jev-1.13.1` pin as `jev-1.13`), else the
  full id. Judged decisions with another pin (`model_mismatch`) or with no served id
  (`no_served_model`) are excluded from both arms and counted. A served id equal to the
  requested alias (when that is not a full `<name>-<major>.<minor>.<patch>` id, e.g.
  `jev-latest`) names no version: it counts as no served id and the report warns loudly.
- **Deadline rule.** A verdict is written before the fill bar's open + 15 minutes (the signal
  needs bar t's close, the instant the fill bar opens). A verdict arriving later is discarded
  and the bar logged `missed`; a bar nobody judged in time is logged `missed` once its
  deadline has passed (its signals are still counted). Jev down, a timeout or a bad answer is
  `unavailable`. Neither is ever a skip, and no row is edited or filled in later.
- **Tamper anchor (commit the heads file).** The hash chain is unkeyed: anyone with write
  access to `decisions.jsonl` can delete rows from the end, or edit a row and re-chain
  everything after it, and the result is again a valid chain. So the chain is tamper-evident
  only against heads recorded elsewhere. After every write the shadow appends
  `- <UTC time> seq <n> rows <n> head <sha256>` to `agents/journal/jev-shadow-heads.md`;
  **commit and push that file regularly** (with every push). `shadow-report` and every later
  shadow run check each recorded head against the log and refuse (exit 2, "the log was
  rewritten after head X was recorded") on any mismatch. `shadow-report` also refuses when the
  last recorded head is behind the log's last row (e.g. a header-only heads file with
  decisions). A crash can land between a log write and its head line, so every `shadow` run
  first checks every recorded head and, only if all verify and exactly one write (its
  decision rows ending in their bar row) is uncovered, appends a head line for the tip marked
  `recovered` and prints a warning. That append happens only after every other check (frozen
  hashes, grace, model, candidates, recorded heads) has passed, so a refused run writes
  nothing. The report lists each recovered seq with its lag (head line time minus the row's
  `written_at`). `shadow-report` also runs `git show HEAD:<heads file>` (read only, no shell,
  with a timeout) and exits 2 ("the heads file differs from the committed copy") unless the
  committed text is a prefix of the working file, and walks the file's committed history
  (`git log --format=%H -- <file>`, then `git show <commit>:<file>`), checking that every
  committed version extends the one before it. An interim look only warns when git or the
  committed copy is unavailable, when head lines are not committed yet, or when the history
  is not append-only. **The final report (at or after the evaluation time) fails closed on
  all of these:** it exits 2 unless the committed copy exists, anchors the log's last row
  (zero uncommitted head lines) and has an append-only history, so commit and push the heads
  file before the final read. `--expect-head` is an extra check against a head kept anywhere
  else.
- **Report.** `shadow-report` verifies the chain, every recorded head, every deadline, the
  pre-registration's sha256 and the snapshot fingerprint and question hash (any problem exits
  2), matches each judged decision to the paper fill opened in its fill bar, and prints take
  vs skip mean net trade returns, the unavailable and missed counts, the chain head and
  `trades_sha256` (the sha256 of the canonical matched-trade list, so a later final report can
  be checked to be the same report), any notes (e.g. an unreadable dry-run DB), a descriptive
  split by the snapshot's `btc_trend_pct` (> 0 vs <= 0; a trivial regime rule to set beside
  Jev's split, no test) and the genesis vs current environment (a reason line when numpy or
  pandas differ: the permutation stream and the fingerprint depend on them). A final report
  exits 2 ("final report refused: <candidate> dry-run DB unreadable; retry") while any
  candidate's dry-run DB is missing, locked or corrupt; a readable DB with no trades is fine. Its
  numbers come from the JSONL and the dry-run DBs alone (the journal entry and heads file are
  checked, not read for numbers). Before the frozen evaluation time (the latest candidate
  decision end + 14 days) a report is an interim look: descriptive only, no test, no p-value,
  no outcome. `--write` saves `jev-shadow/interim-<asof>.md` before it and
  `jev-shadow/report-<evaluate_at>.md` at or after it, once each; neither is ever overwritten.
- **Outcomes.** The frozen test is a two-sided stratified cluster permutation test: one Jev
  call judges a bar's setups together and the coins are correlated, so labels are permuted as
  whole fill-bar clusters among clusters of the same size, and only inside the same 7-day block
  counted from the shadow start (`strata: same_size_within_block`, `block_days: 7`), because
  regimes persist across bars and would otherwise pass for skill. It tests whether Jev
  discriminates within a week; the report gives the number of blocks and of informative strata
  (more than one cluster, both labels). The tested statistic (`statistic:
  centred_within_block_diff`) is the take-minus-skip difference centred on its exact null
  mean, `E0[take_sum] = sum_s (n_take_s / n_s) * R_s`: when weeks differ in take share or in
  mean return the uncentred pooled difference has a null centre away from 0, which would make
  the "two-sided" test one-sided and blind to a within-week effect of the other sign. Scaled
  by a constant (no p-value changes), `T` is the weighted mean of the within-week differences
  (weights `n_take_s * n_skip_s / n_s`; with one stratum it is the plain difference). `T` is
  the report's headline effect and sets the sign of "take better" / "take worse"; the pooled
  difference is shown as a description only (it can differ in sign). Below 30 closed judged trades or 8 per arm the
  result is "inconclusive"; with p >= alpha it is "no information". Both are correct outcomes,
  written up as carefully as a positive one. A positive result justifies only a second
  pre-registered forward test, never trading. Results are historical, not forward-looking.
- **Fail-closed edges (owner).** A crash between writing the pre-registration and the genesis
  row leaves a stray `*-multi-jev-shadow.md` that makes every later run refuse until the owner
  moves it aside (outside Claude Code). Never delete `decisions.jsonl` to restart: a second
  pre-registration in git history would be visible, and the heads file already names the old
  log. A missing heads file is recreated only while the log holds just its genesis row;
  afterwards restore it from git.
- **Recommended protection (owner edits hooks).** Add `agents/data/paper/jev-shadow/**` and
  `agents/journal/*-multi-jev-shadow.md` (optionally `agents/journal/jev-shadow-heads.md`) to
  the protect-files hook so Claude's edit tools cannot touch the log or the pre-registration.

## Files

- `research_loop.py`: CLI.
- `research/orchestrator.py`: the loop, role request/result types, output schemas, prompts.
- `research/sdk_runner.py`: SDK options per role, hooks, JSON-output parsing with one retry,
  and the scripted runner used by `--dry-run`.
- `research/budget.py`, `research/state.py`: budgets and atomic run-state files
  (`agents/data/runs/<run_id>.json` plus a `.resume.json` sidecar).
- `prompts/{scout,designer,critic}.md`: role briefs (system prompts).
- `research/paper/shadow.py` (+ `shadow_log.py`, `shadow_snapshot.py`, `shadow_report.py`): the
  Jev shadow judge, its decision log, blind snapshot and report.
- `research/paper/risk.py`, `guard.py`, `protected_backtest.py`, `portfolio.py`: paper risk rules
  and sizing, the circuit breaker, the protected backtest bridge, and correlation across tests.

Results are historical backtests and are not forward-looking. A candidate becomes "successful"
only after it passes every success criterion **and** 4–8 weeks of forward paper trading.
