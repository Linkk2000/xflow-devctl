#!/usr/bin/env bash
# devctl issue create <title> [--body B] [--labels a,b]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

title=""
body=""
labels=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --body) body="$2"; shift 2 ;;
    --labels) labels="$2"; shift 2 ;;
    -*) devctl_die "未知选项: $1" ;;
    *)
      [[ -z "$title" ]] || devctl_die "多余的参数: $1"
      title="$1"
      shift
      ;;
  esac
done

[[ -n "$title" ]] || devctl_die "用法: devctl issue create <title> [--body B] [--labels a,b]"

devctl_need_cmd jq

resp="$(provider_issue_create "$title" "$body" "$labels")"
number="$(devctl_json_field "$resp" '.number // empty')"
url="$(devctl_json_field "$resp" '.html_url // empty')"

devctl_info "Issue #${number} 已创建"
[[ -n "$url" ]] && devctl_info "$url"
echo "$number"
