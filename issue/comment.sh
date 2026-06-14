#!/usr/bin/env bash
# devctl issue comment <number> [--body B] [--body-file F]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

number=""
body=""
body_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --body) body="$2"; shift 2 ;;
    --body-file) body_file="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *)
      [[ -z "$number" ]] || devctl_die "extra argument: $1"
      number="$1"
      shift
      ;;
  esac
done

[[ -n "$number" ]] || devctl_die "usage: devctl issue comment <number> [--body B] [--body-file F]"

if [[ -n "$body_file" ]]; then
  [[ -f "$body_file" ]] || devctl_die "file not found: $body_file"
  body="$(cat "$body_file")"
fi

[[ -n "$body" ]] || devctl_die "comment body cannot be empty"

if [[ -z "$body_file" && -n "$body" ]]; then
  devctl_validate_inline_issue_body "$body"
fi

approved_file="${body_file:-${DEVCTL_ACADEMIC_APPROVED_FILE:-}}"
devctl_academic_require_remote_approval "issue-comment" "$approved_file" "$number"

devctl_need_cmd jq

devctl_info "posting comment on Issue #${number}"
# shellcheck disable=SC2034
resp="$(provider_issue_comment "$number" "$body")"

devctl_info "comment posted"
