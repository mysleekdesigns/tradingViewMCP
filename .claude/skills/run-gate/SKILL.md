---
name: run-gate
description: Run the acceptance check for a PLAN.md phase (typecheck, lint, tests, and the phase's "Done when" criteria) in an isolated read-only subagent and report PASS or FAIL with evidence. Use before claiming a phase complete, before committing, or when asked whether a phase is done.
argument-hint: [phase-number]
context: fork
agent: gate-runner
background: false
---

Run the acceptance check for tradingView phase: $ARGUMENTS

If no phase number was given, read `PLAN.md` and use the first `## Phase` section that still has unchecked `- [ ]` build items (ignore the Phase 8 "Success criteria" boxes).

Steps:
1. Read the phase's **Checklist** and **Done when:** line in `PLAN.md`.
2. For each area that exists (`web/`, `server/`, `lab/`, `agents/`), run its typecheck, lint, and test commands from that directory. Report a missing manifest instead of failing.
3. Run the phase-specific checks that can be done offline, using fixtures only. Never call live exchange, web, or LLM services.
4. List checks that need a browser or a human as **Manual check needed**; never mark them passed.
5. Cross-check every `[x]` item in that phase against the code and tests. List any ticked without evidence.

Return, in this order: `Gate: PASS` or `Gate: FAIL`; each command with its relevant output; manual checks needed; unevidenced checkboxes; the single most useful next action. Do not edit code, tests, or `PLAN.md`.
