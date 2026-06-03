"#!/usr/bin/env bash
# devctl git done [--base BRANCH] [--force]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

base=""
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) base="$2"; shift 2 ;;
    --force) force=1; shift ;;
    *) devctl_die "未知选项: $1" ;;
  esac
done

devctl_need_cmd git curl jq

branch="$(devctl_current_branch)"
base="${base:-$(devctl_get_branch_meta base)}"
base="${base:-$(devctl_default_base_branch)}"
pr_number="$(devctl_get_branch_meta pr)"

[[ "$branch" != "$base" ]] || devctl_die "当前已在 ${base}，无需 done"

if [[ "$force" -eq 0 && -n "$pr_number" ]]; then
  resp="$(provider_pr_get "$pr_number" 2>/dev/null || true)"
  if [[ -n "$resp" ]]; then
    state="$(devctl_json_field "$resp" '.state // empty')"
    merged="$(devctl_json_field "$resp" '.merged // false')"
    if [[ "$merged" != "true" && "$state" != "closed" ]]; then
      devctl_die "PR #${pr_number} 尚未合并。合并后再执行，或 devctl git done --force"
    fi
  fi
elif [[ "$force" -eq 0 ]]; then
  devctl_warn "未记录 PR 编号 (devctl.pr)，跳过合并状态检查"
fi

devctl_require_clean_worktree

devctl_info "切换到 ${base}"
git -C "$DEVCTL_REPO_ROOT" checkout "$base"

devctl_info "拉取 origin/${base}"
git -C "$DEVCTL_REPO_ROOT" pull --ff-only origin "$base"

if git -C "$DEVCTL_REPO_ROOT" show-ref --verify --quiet "refs/heads/${branch}"; then
  devctl_info "删除本地分支 ${branch}"
  git -C "$DEVCTL_REPO_ROOT" branch -d "$branch" || git -C "$DEVCTL_REPO_ROOT" branch -D "$branch"
fi

git -C "$DEVCTL_REPO_ROOT" config --local --unset-all "devctl.slug" 2>/dev/null || true
git -C "$DEVCTL_REPO_ROOT" config --local --unset-all "devctl.issue" 2>/dev/null || true
git -C "$DEVCTL_REPO_ROOT" config --local --unset-all "devctl.base"
<truncated 160 bytes>