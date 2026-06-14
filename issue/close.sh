#!/usr/bin/env bash
# devctl issue close <number>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

number=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -*) devctl_die "unknown option: $1" ;;
    *)
      [[ -z "$number" ]] || devctl_die "extra argument: $1"
      number="$1"
      shift
      ;;
  esac
done

[[ -n "$number" ]] || devctl_die "usage: devctl issue close <number>"

devctl_academic_require_remote_approval "issue-close" "${DEVCTL_ACADEMIC_APPROVED_FILE:-}" "$number"

devctl_need_cmd jq

devctl_info "closing Issue #${number}"
# shellcheck disable=SC2034
resp="$(provider_issue_close "$number")"

devctl_info "Issue #${number} closed"
