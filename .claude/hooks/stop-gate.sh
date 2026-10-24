#!/usr/bin/env bash
# Stop hook: if source files in web/, server/, lab/, or agents/ changed during this turn, run that
# area's typecheck/lint/tests before the turn ends. Blocks the stop (decision: block) with the
# failure output so Claude fixes it. Honors stop_hook_active to avoid loops.
set -u
set -f   # find patterns below are passed unquoted; never glob them

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ] && exit 0
sid=$(printf '%s' "$input" | jq -r '.session_id // "default"')

root="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$root" || exit 0
marker="${TMPDIR:-/tmp}/claude-tradingview/turn-$sid"
[ -f "$marker" ] || exit 0

changed_in() {
  [ -d "$1" ] || return 1
  [ -n "$(find "$1" -newer "$marker" -type f \( $2 \) \
      -not -path '*/node_modules/*' -not -path '*/.venv/*' -not -path '*/dist/*' 2>/dev/null | head -n 1)" ]
}

failures=""
add_fail() { failures="${failures}$1:\n$(printf '%s' "$2" | tail -n 40)\n\n"; }

for area in web server; do
  changed_in "$area" "-name *.ts -o -name *.tsx" || continue
  [ -f "$area/package.json" ] && [ -d "$area/node_modules" ] || continue
  bin="$area/node_modules/.bin"
  if [ -f "$area/tsconfig.json" ] && [ -x "$bin/tsc" ]; then
    # Vite templates use project references (noEmit is set inside them); tsc -b checks them all.
    if grep -q '"references"' "$area/tsconfig.json"; then tsc_args="-b"; else tsc_args="--noEmit"; fi
    out=$(cd "$area" && ./node_modules/.bin/tsc $tsc_args 2>&1) || add_fail "$area: tsc $tsc_args" "$out"
  fi
  if [ -x "$bin/vitest" ]; then
    out=$(cd "$area" && ./node_modules/.bin/vitest run --passWithNoTests 2>&1) || add_fail "$area: vitest run" "$out"
  fi
done

for area in lab agents; do
  changed_in "$area" "-name *.py -o -name pyproject.toml" || continue
  [ -f "$area/pyproject.toml" ] && command -v uv >/dev/null 2>&1 || continue
  if grep -q 'ruff' "$area/pyproject.toml"; then
    out=$(cd "$area" && uv run --quiet ruff check . 2>&1) || add_fail "$area: ruff check" "$out"
  fi
  if [ -d "$area/tests" ]; then
    out=$(cd "$area" && uv run --quiet pytest -q -x -p no:cacheprovider 2>&1); rc=$?
    # rc 5 = no tests collected; fine on a fresh scaffold.
    [ $rc -ne 0 ] && [ $rc -ne 5 ] && add_fail "$area: pytest -q -x (exit $rc)" "$out"
  fi
done

if [ -n "$failures" ]; then
  reason=$(printf "Source files changed this turn and checks are red. Fix the cause before finishing (do not skip tests, loosen tolerances, or add ignores):\n\n%b" "$failures")
  jq -n --arg r "$reason" '{decision: "block", reason: $r}'
fi
exit 0
