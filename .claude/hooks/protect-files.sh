#!/usr/bin/env bash
# PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit): block edits to files Claude must not touch.
# Exit 2 blocks the call and shows stderr to Claude. Ask decisions go out as JSON with exit 0.
# Fails closed: if jq is missing the edit is blocked with an explanation.
set -u

command -v jq >/dev/null 2>&1 || { echo "protect-files.sh needs jq (brew install jq); edit blocked." >&2; exit 2; }

input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
[ -z "$file" ] && exit 0

root="${CLAUDE_PROJECT_DIR:-$PWD}"
case "$file" in
  "$root"/*) rel="${file#"$root"/}" ;;
  *)         rel="$file" ;;
esac

block() {
  echo "Blocked by .claude/hooks/protect-files.sh: $1" >&2
  exit 2
}
ask() {
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:$r}}'
  exit 0
}

case "$rel" in
  .env|.env.*|*/.env|*/.env.*)
    block "'$rel' holds secrets. Keys come from environment variables only (rules/trading-safety.md)." ;;
  .git/*|*/.git/*)
    block "never edit .git internals; use git commands." ;;
  package-lock.json|*/package-lock.json|pnpm-lock.yaml|*/pnpm-lock.yaml|yarn.lock|*/yarn.lock|uv.lock|*/uv.lock)
    block "'$rel' is generated. Use npm install / uv add / uv lock instead." ;;
  *.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3)
    block "'$rel' is runtime data (candle cache, ledger, alerts). Change it through the app or a migration, never by hand." ;;
  *.parquet|lab/data/*|cache/*)
    block "'$rel' is downloaded or generated market data. Regenerate it through the downloader/cache." ;;
  */tests/fixtures/*|*/test/fixtures/*|*/__fixtures__/*)
    if [ -f "$root/$rel" ]; then
      block "'$rel' is a frozen test fixture. Add a new fixture instead of overwriting; ask the owner if it must change."
    fi ;;
  agents/data/paper/jev-shadow/*|agents/journal/*-multi-jev-shadow.md|agents/journal/jev-shadow-heads.md)
    block "'$rel' is the Jev shadow judge's frozen record (PLAN.md Phase 17). Only paper_trade.py shadow writes it." ;;
  .claude/lab.lock)
    block "the lab lock is controlled by the owner only." ;;
  .claude/settings.json|.claude/hooks/*)
    ask "Editing Claude Code hooks/settings changes the project's safety guardrails. Confirm this edit is intended." ;;
esac

# Lab lock (PLAN.md Phase 8 guardrail): once .claude/lab.lock exists, agents cannot make tests easier to pass.
if [ -f "$root/.claude/lab.lock" ]; then
  case "$rel" in
    lab/lab/engine/*|lab/lab/registry/*|lab/lab/stats/*|lab/lab/research/*|lab/lab/search/*|lab/config/success_criteria*|lab/config/overlay_criteria*)
      block "'$rel' is read-only while the lab is locked (.claude/lab.lock). Engine, registry, stats, research (criteria, judging), search (Optuna), and the success and overlay criteria change only by the owner. Propose the change in your report instead." ;;
  esac
fi

exit 0
