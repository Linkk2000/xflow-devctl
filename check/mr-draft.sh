#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

issue=""
file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) issue="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *) devctl_die "extra argument: $1" ;;
  esac
done

if [[ -z "$file" ]]; then
  [[ -n "$issue" ]] || devctl_die "--issue is required when --file is not provided"
  file="$(devctl_default_issue_file "$issue" mr-draft.md)"
fi

devctl_reject_publish_heading "$file" \
  "# MR Draft" \
  "# PR Draft" \
  "# Merge Request Draft"

devctl_check_template_file "$file" \
  "<!-- xflow: mr-draft -->" \
  "## Summary" \
  "## Test Plan" \
  "## Risk" \
  "## Review Request"

devctl_info "mr-draft check passed: $file"
