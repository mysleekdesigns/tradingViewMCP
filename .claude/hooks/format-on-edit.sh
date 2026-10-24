#!/usr/bin/env bash
# PostToolUse hook (Edit|Write|MultiEdit): format and lint-fix the edited file with the tools its
# package already has installed. No-op until a package exists (Phase 0+). Remaining lint errors are
# returned to Claude as additionalContext so it fixes them before moving on.
set -u

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$file" ] && [ -f "$file" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-$PWD}"
case "$file" in
  "$root"/*) ;;
  *) exit 0 ;;
esac
case "$file" in
  */node_modules/*|*/.venv/*|*/dist/*) exit 0 ;;
esac

# Nearest ancestor directory (inside the project) containing the given manifest.
pkg_root() {
  d=$(dirname "$file")
  while [ "${#d}" -ge "${#root}" ]; do
    [ -f "$d/$1" ] && { printf '%s' "$d"; return 0; }
    [ "$d" = "$root" ] && break
    d=$(dirname "$d")
  done
  return 1
}

remaining=""
case "$file" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs|*.css|*.json|*.html)
    pkg=$(pkg_root package.json) || exit 0
    bin="$pkg/node_modules/.bin"
    if [ -x "$bin/eslint" ]; then
      case "$file" in
        *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs)
          remaining=$(cd "$pkg" && "$bin/eslint" --fix --no-warn-ignored "$file" 2>&1 | tail -n 30) ;;
      esac
    fi
    [ -x "$bin/prettier" ] && (cd "$pkg" && "$bin/prettier" --write --log-level warn "$file" >/dev/null 2>&1)
    ;;
  *.py)
    pkg=$(pkg_root pyproject.toml) || exit 0
    grep -q 'ruff' "$pkg/pyproject.toml" || exit 0
    command -v uv >/dev/null 2>&1 || exit 0
    remaining=$(cd "$pkg" && uv run --quiet ruff check --fix --quiet "$file" 2>&1)
    (cd "$pkg" && uv run --quiet ruff format --quiet "$file" >/dev/null 2>&1)
    ;;
  *) exit 0 ;;
esac

if [ -n "$remaining" ]; then
  jq -n --arg f "$file" --arg o "$remaining" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: ("Auto-fix ran on " + $f + ". These lint issues remain and must be fixed by hand (do not disable the rule):\n" + $o)
    }
  }'
fi
exit 0
