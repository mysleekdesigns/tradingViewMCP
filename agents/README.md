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
