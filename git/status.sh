#!/usr/bin/env bash
# devctl git status
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

devctl_need_cmd git

branch="$(devctl_current_branch)"
base="$(devctl_get_branch_meta base)"
base="${base:-$(devctl_default_base_branch)}"
issue="$(devctl_get_branch_meta issue)"
slug="$(devctl_get_branch_meta slug)"
pr="$(devctl_get_branch_meta pr)"

printf 'branch:  %s\n' "$branch"
printf 'base:    %s\n' "$base"
[[ -n "$slug" ]] && printf 'slug:    %s\n' "$slug"
[[ -n "$issue" ]] && printf 'issue:   #%s\n' "$issue"
[[ -n "$pr" ]] && printf 'pr:      #%s\n' "$pr"

upstream="$(git -C "$DEVCTL_REPO_ROOT" rev-parse --abbrev-ref "${branch}@{upstream}" 2>/dev/null || echo '(none)')"
printf 'upstream: %s\n' "$upstream"

if [[ "$upstream" != "(none)" ]]; then
  ahead="$(git -C "$DEVCTL_REPO_ROOT" rev-list --count "${upstream}..HEAD" 2>/dev/null || echo 0)"
  behind="$(git -C "$DEVCTL_REPO_ROOT" rev-list --count "HEAD..${upstream}" 2>/dev/null || echo 0)"
  printf 'ahead:   %s  behind: %s\n' "$ahead" "$behind"
fi

if git -C "$DEVCTL_REPO_ROOT" diff --quiet && git -C "$DEVCTL_REPO_ROOT" diff --cached --quiet; then
  printf 'worktree: clean\n'
else
  printf 'worktree: dirty\n'
fi
