#!/usr/bin/env bash
# devctl git start <slug> [--issue N] [--base BRANCH]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

slug=""
issue=""
base=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) issue="$2"; shift 2 ;;
    --base) base="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *)
      [[ -z "$slug" ]] || devctl_die "extra argument: $1"
      slug="$1"
      shift
      ;;
  esac
done

[[ -n "$slug" ]] || devctl_die "usage: devctl git start <slug> [--issue N] [--base BRANCH]"

devctl_need_cmd git
base="${base:-$(devctl_default_base_branch)}"
branch="$(devctl_branch_name_from_slug "$slug" "$issue")"

devctl_require_clean_worktree

current="$(devctl_current_branch)"
if [[ "$current" != "$base" ]]; then
  devctl_info "switching to ${base}"
  git -C "$DEVCTL_REPO_ROOT" checkout "$base"
fi

devctl_info "pulling origin/${base}"
git -C "$DEVCTL_REPO_ROOT" pull --ff-only origin "$base"

if git -C "$DEVCTL_REPO_ROOT" show-ref --verify --quiet "refs/heads/${branch}"; then
  devctl_die "branch already exists: ${branch}"
fi

devctl_info "creating branch ${branch}"
git -C "$DEVCTL_REPO_ROOT" checkout -b "$branch"

devctl_set_branch_meta slug "$slug"
[[ -n "$issue" ]] && devctl_set_branch_meta issue "$issue"
devctl_set_branch_meta base "$base"

devctl_info "ready; after development: devctl git commit-msg -ac && devctl git mr"
