#!/usr/bin/env bash
# devctl issue close <number>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

number=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -*) devctl_die "未知选项: $1" ;;
    *)
      [[ -z "$number" ]] || devctl_die "多余的参数: $1"
      number="$1"
      shift
      ;;
  esac
done

[[ -n "$number" ]] || devctl_die "用法: devctl issue close <number>"

approved_file="${DEVCTL_APPROVED_FILE:-$(devctl_default_issue_file "$number" walkthrough.md)}"
devctl_require_local_review "$number" "issue-close" "$approved_file"

devctl_need_cmd jq

devctl_info "正在关闭 Issue #${number}..."
# shellcheck disable=SC2034
resp="$(provider_issue_close "$number")"

devctl_info "Issue #${number} 已关闭。"
