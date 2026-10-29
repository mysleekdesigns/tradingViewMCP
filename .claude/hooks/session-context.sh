#!/usr/bin/env bash
# SessionStart hook: print git state, PLAN.md phase progress, which areas exist, and the lab lock.
# Plain stdout from a SessionStart hook is added to Claude's context. Keep it fast and offline.
set -u

root="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$root" || exit 0

echo "tradingView session context (from .claude/hooks/session-context.sh)"

if git rev-parse --git-dir >/dev/null 2>&1; then
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "n/a")
  dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  echo "Git: branch $branch, $dirty uncommitted paths."
else
  echo "Git: not a repository yet (PLAN.md Phase 0 runs git init)."
fi

areas=""
for d in web server lab agents desktop; do
  [ -d "$d" ] && areas="$areas $d/"
done
echo "Areas present:${areas:- none yet (Phase 0 not started)}"

if [ -f .claude/lab.lock ]; then
  echo "Lab lock: ON. lab/lab/engine, registry, stats, research, search and the success criteria are read-only (owner removes .claude/lab.lock to unlock)."
fi

echo "PLAN.md progress (done/total build checkboxes):"
bash "$root/.claude/scripts/plan-progress.sh"
echo "Use /phase-status for next items, /next-phase to work a phase, /run-gate before claiming it done."
exit 0
