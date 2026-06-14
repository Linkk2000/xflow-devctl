#!/usr/bin/env bash
# devctl git mr [--title T] [--body B] [--body-file F] [--base BRANCH] [--issue N]
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
    *) devctl_die "unknown option: $1" ;;
  esac
done

if [[ -n "$body" && -n "$body_file" ]]; then
  devctl_die "error: use only one of --body or --body-file"
fi
if [[ -n "$body" ]]; then
  devctl_validate_inline_issue_body "$body"
fi
if [[ -n "$body_file" ]]; then
  [[ -f "$body_file" ]] || devctl_die "missing MR body file: $body_file"
  body="$(cat "$body_file")"
fi

if [[ -n "${DEVCTL_PROVIDER_STUB:-}" ]]; then
  # shellcheck disable=SC1090
  source "$DEVCTL_PROVIDER_STUB"
fi

devctl_need_cmd git curl jq

branch="$(devctl_current_branch)"
base="${base:-$(devctl_get_branch_meta base)}"
base="${base:-$(devctl_default_base_branch)}"
issue="${issue:-$(devctl_get_branch_meta issue)}"
slug="$(devctl_get_branch_meta slug)"

[[ "$branch" != "$base" ]] || devctl_die "current branch is ${base}; run devctl git start first"

devctl_academic_require_remote_approval "git-mr" "${DEVCTL_ACADEMIC_APPROVED_FILE:-}" "$issue"

if [[ "${DEVCTL_SKIP_PUSH:-0}" != "1" ]]; then
  devctl_push_current_branch
fi

# 去除分支前缀作为默认标题
raw_title="$branch"
raw_title="${raw_title#feature/}"
raw_title="${raw_title#fix/}"
raw_title="${raw_title#feat/}"
title="${title:-$raw_title}"
title="${title//-/ }"
[[ -n "$issue" ]] && title="[#${issue}] ${title}"

if [[ -z "$body" ]]; then
  body="## Summary
"
  [[ -n "$issue" ]] && body+="- Closes #${issue}
"
  body+="
## Test plan
- [ ] 本地验证"
  body="$(printf '%b' "$body")"
fi

devctl_info "creating pull request: ${branch} -> ${base}"
resp="$(provider_pr_create "$title" "$body" "$branch" "$base")"

pr_url="$(devctl_json_field "$resp" '.html_url // empty')"
pr_number="$(devctl_json_field "$resp" '.number // empty')"
devctl_set_branch_meta pr "$pr_number"

devctl_info "PR #${pr_number} created"
[[ -n "$pr_url" ]] && devctl_info "$pr_url"
