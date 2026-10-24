---
name: commit-phase
description: Stage and commit the current work as a Conventional Commit scoped to the PLAN.md phase, after checks pass and PLAN.md checkboxes are updated. Manual only.
disable-model-invocation: true
argument-hint: [phase-number] [short summary]
allowed-tools: Bash(git add *) Bash(git commit *) Bash(git status *) Bash(git diff *) Bash(npx vitest *) Bash(npx tsc *) Bash(npm run *) Bash(uv run pytest*) Bash(uv run ruff*)
---

Commit the current tradingView work. Arguments: $ARGUMENTS (phase number, then an optional summary).

1. If this is not a git repository, stop and say Phase 0 (`git init` + `.gitignore`) comes first.
2. Run `git status --short` and `git diff --stat`. Stop if there is nothing to commit.
3. For every area touched (`web/`, `server/`, `lab/`, `agents/`), run its typecheck, lint, and tests. If anything fails, stop and report the output; never commit red.
4. Compare the diff with the phase checklist in `PLAN.md`. Every finished, verified item must be `[x]`; every ticked item must be in the diff or already committed. Fix `PLAN.md` first if not.
5. Stage only files that belong to this change. Never stage `.env*`, `*.db`, `node_modules/`, `dist/`, `cache/`, `lab/data/`, parquet files, or `.claude/settings.local.json`.
6. Commit with a Conventional Commit: type `feat|fix|test|docs|chore|refactor`, scope `phase-<n>`. The body lists the checklist items completed and the checks that passed, then any attribution trailer the session requires. Example:

```
feat(phase-2): live kline updates with reconnect and gap fill

- WebSocket subscription per symbol/interval
- Auto-reconnect with backoff + gap fill
- Connection status indicator

Checks: npx tsc --noEmit (clean), npx vitest run (18 passed)
Manual: survived a Wi-Fi toggle in the browser (owner verified)
```

7. Report the commit hash and subject. Do not push; pushing is a separate decision.
