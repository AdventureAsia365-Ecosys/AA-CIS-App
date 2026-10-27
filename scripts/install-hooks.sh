#!/usr/bin/env bash
# AA-993 H1: point git at the tracked hooks in .githooks/ (pre-push runs scripts/pre-push.sh).
# The setting lives in the shared repo config, so every worktree of this clone uses its own .githooks/
# (a worktree on a branch without .githooks/ simply runs no hook).
# Undo: git config --unset core.hooksPath
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
chmod +x .githooks/* scripts/pre-push.sh
old=$(git config --get core.hooksPath || true)
if [[ -n $old && $old != .githooks ]]; then
  echo "note: core.hooksPath was '$old'; it is replaced by .githooks"
fi
# Hooks in .git/hooks stop running once core.hooksPath is set: name them, so they can be moved.
hooks_dir=$(git rev-parse --path-format=absolute --git-common-dir)/hooks
if [[ -d $hooks_dir ]]; then
  active=$(find "$hooks_dir" -maxdepth 1 -type f ! -name '*.sample' -printf '%f ' 2>/dev/null || true)
  if [[ -n $active ]]; then
    echo "warning: these hooks in $hooks_dir no longer run: $active"
    echo "         copy them into .githooks/ if you still need them (for example git-secrets)."
  fi
fi
git config core.hooksPath .githooks
echo "installed: core.hooksPath=.githooks (pre-push -> scripts/pre-push.sh)"
echo "check it:  scripts/pre-push.sh --dry-run    bypass once: git push --no-verify"
