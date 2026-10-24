#!/usr/bin/env bash
# Print PLAN.md progress. Used by the SessionStart hook and the phase-status / next-phase skills.
#   plan-progress.sh            per-phase done/total checkbox counts
#   plan-progress.sh next [N]   the next N unchecked items (default 12), tagged with their phase
# Counts only boxes inside "## Phase" sections and ignores the Phase 8 "Success criteria" boxes,
# which are strategy acceptance criteria, not build tasks.
set -u

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
plan="$root/PLAN.md"
[ -f "$plan" ] || { echo "  (no PLAN.md found)"; exit 0; }

mode="${1:-summary}"
limit="${2:-12}"

awk -v mode="$mode" -v limit="$limit" '
  function flush() { if (mode == "summary" && name != "") printf "  %s: %d/%d\n", name, done, total }
  /^## Phase/ { flush(); name = $0; sub(/^## /, "", name); short = name; sub(/ —.*/, "", short); done = 0; total = 0; skip = 0; next }
  /^## /      { flush(); name = ""; next }
  /^### /     { skip = ($0 ~ /Success criteria/); next }
  name == "" || skip { next }
  /^- \[x\]/  { done++; total++ }
  /^- \[ \]/  { total++; if (mode == "next" && shown < limit) { shown++; printf "  [%s] %s\n", short, substr($0, 7) } }
  END { flush() }
' "$plan"
