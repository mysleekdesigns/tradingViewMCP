#!/usr/bin/env bash
# PreToolUse hook (mcp__strategy-lab__holdout_check): ask the owner before spending a locked-holdout
# check. The MCP server enforces the budget itself; this adds a human look at each use.
set -u

input=$(cat)
plan=$(printf '%s' "$input" | jq -c '.tool_input // {}' 2>/dev/null | cut -c1-300)

jq -n --arg p "$plan" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: ("Locked-holdout check requested (budget-limited; a failed holdout kills the hypothesis, no re-tuning). Confirm the strategy-critic verdict was \"promising\". Input: " + $p)
  }
}'
exit 0
