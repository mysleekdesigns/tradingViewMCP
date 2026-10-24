# Workflow

- `PLAN.md` is the source of truth for scope, phases, checklists, and each phase's **Done when:** line. Read the phase you are working on before writing code.
- Phases 0 → 3 run in order (first usable version = end of Phase 3). Phase 4 needs Phase 3. Phase 5 can start after Phase 2. Phases 6–9 follow in order; Phase 8 needs a green Phase 7.
- Do not start a phase until the previous phase's "Done when" check has been run and shown passing. State the command you ran and its output, not a claim.
- When a checklist item is finished and verified, flip `[ ]` to `[x]` in `PLAN.md` in the same change. Never tick an item that is unverified or needs a manual browser check you did not do; list those for the user instead.
- The checkboxes under Phase 8 **Success criteria** are acceptance criteria for strategies, not build tasks. Never tick them while building.
- Tooling: `npm` inside `web/` and `server/`; `uv` inside `lab/` and `agents/` (`uv run pytest`, `uv add <pkg>`). Never `pip install` into the system Python.
- Once git exists (Phase 0): work on a branch, use Conventional Commits with the phase as scope, e.g. `feat(phase-2): reconnect websocket with backoff`. Commit with `/commit-phase`; do not push unless asked.
- Before claiming a phase complete, run `/run-gate`. Delegate broad investigations to a subagent (`researcher`, Explore) rather than reading many files into the main conversation.
