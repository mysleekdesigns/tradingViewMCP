# .claude layout

Project-scoped Claude Code configuration for tradingView, following Anthropic's guidance for rules, skills, subagents, and hooks (code.claude.com/docs: memory, skills, sub-agents, hooks, permissions). Adapted from `../hedgeFund/.claude/` as PLAN.md Phase 8 suggests.

| Piece | Where | Loads | Contents |
|---|---|---|---|
| Rules | `rules/*.md` | `workflow`, `trading-safety` at launch; the rest when a file matching `paths:` is read or edited | short, enforceable conventions per area |
| Skills | `skills/<name>/SKILL.md` | description always (except manual-only ones), body on invoke | workflows: `/phase-status`, `/next-phase`, `/run-gate`, `/commit-phase`, `/add-indicator`, `/research-strategy`; knowledge: `lightweight-charts-v5`, `binance-market-data`, `honest-backtesting` |
| Subagents | `agents/*.md` | on delegation | `phase-implementer`, `code-reviewer`, `quant-reviewer`, `gate-runner`, `test-debugger`, `researcher`, `strategy-scout`, `strategy-critic` |
| Hooks | `settings.json` + `hooks/*.sh` | deterministically on events | trading/secret/destructive-command guards, protected files, lab lock, holdout confirmation, format on edit, test gate on stop, session context |
| Scripts | `scripts/plan-progress.sh` | called by hooks and skills | PLAN.md checkbox progress (ignores Phase 8 success-criteria boxes) |

Rules of thumb used here:

- Advisory guidance goes in rules and skills. Anything that must hold every time is a hook or a `permissions.deny` rule.
- Rules are path-scoped so they cost no context until relevant.
- Skills with side effects or spend (`next-phase`, `commit-phase`, `research-strategy`) set `disable-model-invocation: true`. Pure API references set `user-invocable: false`.
- Reviewers, the gate runner, the scout, and the critic have no Edit/Write tools, so an agent grading work cannot change it. `strategy-critic` also cannot call `holdout_check`.
- Hooks use exec form (`command: bash`, `args: [...]`) with `${CLAUDE_PROJECT_DIR}`, need only `bash` 3.2 + `jq`, and are no-ops for areas not yet scaffolded. Guard hooks fail closed if `jq` is missing.
- Hook pattern matching is best-effort, not a sandbox. Real enforcement for the lab lock in Phase 8 headless runs should also use `permissions.deny` (e.g. `Edit(/lab/lab/engine/**)`) in the run's own settings.

Validate after editing: `claude plugin validate .claude/agents` and `claude plugin validate .claude/skills`. Run `/hooks` to see registered hooks.
