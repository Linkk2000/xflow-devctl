#!/usr/bin/env bash
# devctl issue create <title> [--body B] [--body-file F] [--labels a,b]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

title=""
body=""
body_file=""
labels=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --body) body="$2"; shift 2 ;;
    --body-file) body_file="$2"; shift 2 ;;
    --labels) labels="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *)
      [[ -z "$title" ]] || devctl_die "extra argument: $1"
      title="$1"
      shift
      ;;
  esac
done

[[ -n "$title" ]] || devctl_die "usage: devctl issue create <title> [--body B] [--body-file F] [--labels a,b]"

if [[ -n "$body_file" ]]; then
  [[ -f "$body_file" ]] || devctl_die "file not found: $body_file"
  body="$(cat "$body_file")"
fi

if [[ -z "$body_file" && -n "$body" ]]; then
  devctl_validate_inline_issue_body "$body"
fi

approved_file="${body_file:-${DEVCTL_ACADEMIC_APPROVED_FILE:-}}"
devctl_academic_require_remote_approval "issue-create" "$approved_file" "draft"

devctl_need_cmd jq

resp="$(provider_issue_create "$title" "$body" "$labels")"
number="$(devctl_json_field "$resp" '.number // empty')"
url="$(devctl_json_field "$resp" '.html_url // empty')"

devctl_info "Issue #${number} created"
[[ -n "$url" ]] && devctl_info "$url"
echo "$number"
