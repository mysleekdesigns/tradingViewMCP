---
name: phase-status
description: Show progress against PLAN.md - per-phase checkbox counts, the next unchecked items, the current phase's "Done when" line, and blockers. Use when the user asks where we are, what is next, what to work on, or which phase is current.
allowed-tools: Read Bash(git status *)
---

# Phase status

Phase progress (done/total build checkboxes):

!`bash "${CLAUDE_PROJECT_DIR}/.claude/scripts/plan-progress.sh"`

Next unchecked items, in plan order:

!`bash "${CLAUDE_PROJECT_DIR}/.claude/scripts/plan-progress.sh" next 12`

Working tree:

!`{ git -C "${CLAUDE_PROJECT_DIR}" status --short 2>/dev/null || echo "  (not a git repository yet - Phase 0 adds it)"; } | head -20`

## What to report

1. **Current phase**: the first phase with unchecked items. Phases 0–3 are sequential; Phase 5 can start after Phase 2; 6–9 follow in order; 8 needs a green 7.
2. **Done when** for that phase: quote its `**Done when:**` line from `PLAN.md`.
3. **Next three items** and the directory each lands in (PLAN.md Project Structure).
4. **Blockers**: uncommitted work, a previous phase not verified, a decision only the owner can make (e.g. data provider by region in Phase 0).

Do not edit `PLAN.md` from this skill. If an item looks done but is unchecked, say so and suggest `/run-gate` before ticking it.
