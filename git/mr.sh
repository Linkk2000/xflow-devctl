#!/usr/bin/env bash
# devctl git mr [--title T] [--body-file F] [--base BRANCH] [--issue N]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

title=""
body=""
body_file=""
base=""
issue=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title) title="$2"; shift 2 ;;
    --body) body="$2"; shift 2 ;;
    --body-file) body_file="$2"; shift 2 ;;
    --base) base="$2"; shift 2 ;;
    --issue) issue="$2"; shift 2 ;;
    *) devctl_die "未知选项: $1" ;;
  esac
done

devctl_need_cmd git curl jq

branch="$(devctl_current_branch)"
base="${base:-$(devctl_get_branch_meta base)}"
base="${base:-$(devctl_default_base_branch)}"
issue="${issue:-$(devctl_get_branch_meta issue)}"
slug="$(devctl_get_branch_meta slug)"

[[ "$branch" != "$base" ]] || devctl_die "当前在 ${base} 分支，请先 devctl git start"

# 去除分支前缀作为默认标题
raw_title="$branch"
raw_title="${raw_title#feature/}"
raw_title="${raw_title#fix/}"
raw_title="${raw_title#feat/}"
title="${title:-$raw_title}"
title="${title//-/ }"
[[ -n "$issue" ]] && title="[#${issue}] ${title}"

[[ -z "$body" ]] || devctl_die "remote MR/PR creation requires --body-file for local review"
[[ -n "$issue" ]] || devctl_die "devctl git mr requires --issue or branch issue metadata for local review"
body_file="${body_file:-$(devctl_default_issue_file "$issue" mr-draft.md)}"
[[ -f "$body_file" ]] || devctl_die "body file does not exist: $body_file"
body="$(cat "$body_file")"
devctl_require_local_review "$issue" "git-mr" "$body_file"

devctl_push_current_branch

devctl_info "创建 Pull Request: ${branch} → ${base}"
resp="$(provider_pr_create "$title" "$body" "$branch" "$base")"

pr_url="$(devctl_json_field "$resp" '.html_url // empty')"
pr_number="$(devctl_json_field "$resp" '.number // empty')"
devctl_set_branch_meta pr "$pr_number"

devctl_info "PR #${pr_number} 已创建"
[[ -n "$pr_url" ]] && devctl_info "$pr_url"
