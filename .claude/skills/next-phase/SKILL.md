---
name: next-phase
description: Work the next unfinished PLAN.md phase end to end - split the checklist into packages with disjoint file ownership, run phase-implementer subagents in parallel, integrate, review, gate, tick PLAN.md, and commit. Manual only.
disable-model-invocation: true
argument-hint: [phase-number]
allowed-tools: Agent Read Edit Write Grep Glob Bash(npm *) Bash(npx *) Bash(uv *) Bash(git add *) Bash(git commit *) Bash(git status *) Bash(git diff *) Bash(git log *)
---

# Next phase

Work phase `$ARGUMENTS` of `PLAN.md`. If no number was given, use the first phase with unchecked build items. Run subagents in parallel wherever packages are independent: send every Agent call for a stage in one message.

Progress right now:

!`bash "${CLAUDE_PROJECT_DIR}/.claude/scripts/plan-progress.sh"`

Working tree: !`if git -C "${CLAUDE_PROJECT_DIR}" rev-parse --git-dir >/dev/null 2>&1; then echo "$(git -C "${CLAUDE_PROJECT_DIR}" status --short | wc -l | tr -d ' ') dirty paths"; else echo "not a git repository yet"; fi`.

Copy this checklist into your first message and keep it updated:

```
Next phase progress:
- [ ] 1. Preconditions: tree clean, previous phase verified, phase scope read
- [ ] 2. Work packages defined (disjoint file ownership, interfaces fixed)
- [ ] 3. Implementers launched in parallel and all returned
- [ ] 4. Integration: typecheck + lint + tests green in every touched area
- [ ] 5. Review (code-reviewer and/or quant-reviewer) and gate-runner in parallel; findings fixed
- [ ] 6. PLAN.md boxes ticked for verified items only; manual checks listed
- [ ] 7. Committed (no push)
```

## 1. Preconditions

- Phase 0 is special: it installs tools and needs owner decisions (data provider by region: Binance / Binance.US / Coinbase). Ask with AskUserQuestion before scaffolding if the provider is not yet decided. Do Phase 0 directly in the main thread; it is too small and sequential to parallelise.
- Otherwise the working tree must be clean; if not, stop and say what is uncommitted.
- Unless the target is Phase 0, launch `gate-runner` for the previous phase. If it fails, fix that first or stop and report.
- Phase 8 starts only after the owner confirms in this conversation that Phase 7's lab is trustworthy. Ask if they have not.
- Read in full: the phase's **How**, **Checklist**, **Done when**, the Tech Stack table, Project Structure, and any research section the phase cites.

## 2. Work packages

Partition the unchecked items so no two packages create or edit the same file. Typical shapes:

- Phase 1: (a) `types.ts` + `data/provider.ts` + `data/binance.ts` + mapper tests, (b) `chart/Chart.tsx` + legend + theme. Fix the `Candle`/provider signatures in both prompts so (b) can code against (a).
- Phase 4: one package per indicator group (overlays, oscillators) plus one for the registry + menu + settings dialog.
- Phase 5: (a) server scaffold + SQLite migrations, then in parallel (b) candle cache endpoint, (c) WS fan-out, (d) persistence endpoints, (e) `ServerProvider` in `web/`.
- Phase 7: (a) archive downloader, (b) registry + look-ahead tests, (c) plan schema + validator, (d) engine + accounting tests, (e) splits + ledger, (f) stats. Fix shared dataclasses/schemas first.

Write the shared interfaces (exact signatures and types) into every prompt that depends on them. If an API or formula is uncertain, launch `researcher` in the same message and pass its answer on.

Browser-only or owner-only checks are never assigned to an implementer and never ticked; collect them for the user.

## 3. Launch implementers

`subagent_type: phase-implementer`, one call per package, all in one message. Each prompt contains: phase number, checklist items quoted verbatim, the exact files it owns, interfaces it exposes/consumes, and its own test command. Wait for every result; do not redo their work meanwhile.

## 4. Integration

Run each touched area's typecheck, lint, and tests from its directory. If anything is red, launch `test-debugger` (several in parallel for unrelated failures, each told which files it may touch). Never fix by skipping tests or loosening tolerances.

## 5. Review and gate, in parallel

In one message launch:
- `code-reviewer` if `web/` or `server/` changed; `quant-reviewer` if `lab/`, `agents/`, or `web/src/indicators/` changed. Pass the phase number and changed files.
- `gate-runner` for this phase.

Fix every Critical and Warning finding, re-run step 4, and re-run `gate-runner` until `Gate: PASS`. After three failed rounds, stop and report instead of forcing it.

## 6. Tick PLAN.md

Flip `[ ]` → `[x]` only for items with code and a passing test, or verified by the gate. Leave manual-check items unticked. Do not touch other phases.

## 7. Commit

Stage only this change's files (never `.env*`, `*.db`, `node_modules/`, `dist/`, data or cache files). Conventional Commit, scope `phase-<n>`, body listing completed items and the checks that passed, plus any attribution trailer the session requires. Do not push.

## Final report

Items completed; items left and why; **manual checks for the owner** (e.g. "open http://localhost:5173 and confirm BTCUSDT 1h renders with crosshair legend"); gate output; reviewer findings fixed; commit hash; the next phase and its first three items.
