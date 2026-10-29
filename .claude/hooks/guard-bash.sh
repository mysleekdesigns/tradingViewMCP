#!/usr/bin/env bash
# PreToolUse hook (Bash): deny order/withdraw endpoints, live freqtrade, secret literals, writes to
# .env, catastrophic deletes, system pip, and lab-lock bypasses; ask before history-rewriting git.
# Decisions are JSON on stdout with exit 0. Best-effort pattern matching, not a sandbox.
set -u

command -v jq >/dev/null 2>&1 || { echo "guard-bash.sh needs jq (brew install jq); command blocked." >&2; exit 2; }

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0
root="${CLAUDE_PROJECT_DIR:-$PWD}"

deny() {
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}
ask() {
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:$r}}'
  exit 0
}
has() { printf '%s' "$cmd" | grep -Eq -- "$1"; }

# --- deny: real order placement or withdrawals (testnet.binance.vision is the paper exchange and is allowed)
if ! has 'testnet\.binance\.vision'; then
  if has '/api/v3/(order|orderList|sor/order|cancelReplace)|/(fapi|dapi)/v[0-9]+/(order|batchOrders)|/sapi/v[0-9]+/(capital/withdraw|margin/order)'; then
    deny "Exchange order/withdraw endpoint. This app never trades real money (PLAN.md Non-goals, rules/trading-safety.md). Paper trading uses testnet.binance.vision or freqtrade --dry-run."
  fi
fi
if has 'api\.exchange\.coinbase\.com/orders|api\.coinbase\.com/api/v3/brokerage/orders'; then
  deny "Coinbase order endpoint. This app never places real orders."
fi

# --- deny: freqtrade live trading
if has '(^|[;&|[:space:]])freqtrade[[:space:]]+trade' && ! has '(^|[[:space:]])--dry-run([[:space:]=]|$)'; then
  deny "freqtrade trade without --dry-run is live trading. Use 'freqtrade trade --dry-run ...' for paper trading."
fi
if has '"dry_run"[[:space:]]*:[[:space:]]*false'; then
  deny "Setting dry_run false enables live trading. Paper only."
fi

# --- deny: secrets in commands or written to .env
if has '(BINANCE|COINBASE|TYPESAFE|ANTHROPIC|OPENAI)_[A-Z_]*(KEY|SECRET|TOKEN)=["'"'"']?[A-Za-z0-9_-]{12,}'; then
  deny "Command embeds an API key literal. Read keys from the environment (\$VAR) instead."
fi
if has '(>|>>|tee([[:space:]]+-a)?)[[:space:]]*["'"'"']?[^[:space:]]*\.env(\.[A-Za-z0-9_-]+)?["'"'"']?([[:space:]]|$)'; then
  deny "Writing to a .env file. Keys stay in the shell environment."
fi

# --- deny: catastrophic rm
if has '(^|[;&|[:space:]])rm[[:space:]]+(-[A-Za-z]*[[:space:]]+)*["'"'"']?(/|~|\$HOME|\.git|\.|\*|\.\.)["'"'"']?([[:space:]]|$|/\*)'; then
  if has '(^|[;&|[:space:]])rm[[:space:]]+-[A-Za-z]*[rR]'; then
    deny "Recursive delete of a root, home, repo root, or .git. Delete specific paths instead."
  fi
fi

# --- deny: system pip (use uv)
if has '(^|[;&|][[:space:]]*)(sudo[[:space:]]+)?pip3?[[:space:]]+install|python3?[[:space:]]+-m[[:space:]]+pip[[:space:]]+install'; then
  deny "Use uv (uv add <pkg> inside lab/ or agents/), not pip into the system Python."
fi

# --- deny: lab lock bypass
if has 'lab\.lock' && has '(^|[;&|[:space:]])(rm|mv|unlink|truncate)[[:space:]]|>[[:space:]]*[^[:space:]]*lab\.lock'; then
  deny "The lab lock is removed by the owner only, outside Claude Code."
fi
if [ -f "$root/.claude/lab.lock" ] && has 'lab/lab/(engine|registry|stats|research|search)/|lab/config/(success|overlay)_criteria'; then
  if has '(^|[;&|[:space:]])(rm|mv|cp|sed[[:space:]]+-i|perl[[:space:]]+-[a-z]*i|truncate|tee|patch|git[[:space:]]+(checkout|restore|apply))[[:space:]]|>'; then
    deny "The lab is locked (.claude/lab.lock): engine, registry, stats, research, search, and the success and overlay criteria are read-only. Propose the change to the owner instead."
  fi
fi

# --- ask: history rewriting or discarding work
if has 'git[[:space:]]+push[[:space:]].*(--force|-f([[:space:]]|$)|--force-with-lease)'; then
  ask "Force push rewrites remote history. Confirm this is intended."
fi
if has 'git[[:space:]]+(reset[[:space:]]+--hard|clean[[:space:]]+-[A-Za-z]*[fd]|checkout[[:space:]]+--[[:space:]]+\.|restore[[:space:]]+\.|stash[[:space:]]+drop|branch[[:space:]]+-D)'; then
  ask "This git command discards uncommitted work or branches. Confirm."
fi

exit 0
