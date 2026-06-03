#!/usr/bin/env bash
# devctl git mr [--title T] [--body B] [--base BRANCH] [--issue N]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

title=""
body=""
base=""
issue=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title) title="$2"; shift 2 ;;
    --body) body="$2"; shift 2 ;;
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

devctl_push_current_branch

title="${title:-${branch#feat/}}"
title="${title//-/ }"
[[ -n "$issue" ]] && title="[#${issue}] ${title}"

if [[ -z "$body" ]]; then
  body="## Summary\n"
  [[ -n "$issue" ]] && body+="- Closes #${issue}\n"
  body+="\n## Test plan\n- [ ] 本地验证"
  body="$(printf '%b' "$body")"
fi

payload="$(jq -n \
  --arg title "$title" \
  --arg head "$branch" \
  --arg base "$base" \
  --arg body "$body" \
  '{title: $title, head: $head, base: $base, body: $body}')"

devctl_info "创建 Pull Request: ${branch} → ${base}"
resp="$(devctl_gitee_api_json POST "$(devctl_gitee_repo_path /pulls)" "$payload")"

pr_url="$(devctl_json_field "$resp" '.html_url // empty')"
pr_number="$(devctl_json_field "$resp" '.number // empty')"
devctl_set_branch_meta pr "$pr_number"

devctl_info "PR #${pr_number} 已创建"
[[ -n "$pr_url" ]] && devctl_info "$pr_url"
