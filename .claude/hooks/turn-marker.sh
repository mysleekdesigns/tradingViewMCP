#!/usr/bin/env bash
# UserPromptSubmit hook: touch a per-session marker so stop-gate.sh can tell which source files
# changed during this turn. Prints nothing (stdout here would be added to the prompt context).
set -u

input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // "default"' 2>/dev/null || echo default)
dir="${TMPDIR:-/tmp}/claude-tradingview"
mkdir -p "$dir" 2>/dev/null && touch "$dir/turn-$sid"
exit 0
