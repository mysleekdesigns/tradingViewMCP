#!/usr/bin/env bash
# PreToolUse hook for any MCP tool whose name looks like it places, cancels, or changes orders,
# or withdraws funds. Always denies: real trading is a manual action by the owner (PLAN.md Non-goals).
set -u

input=$(cat)
tool=$(printf '%s' "$input" | jq -r '.tool_name // "unknown"' 2>/dev/null || echo unknown)

jq -n --arg t "$tool" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: ("\($t) is blocked by .claude/hooks/block-order-tools.sh. This project never places, cancels, or modifies orders and never withdraws funds. Read-only market-data tools remain available.")
  }
}'
exit 0
