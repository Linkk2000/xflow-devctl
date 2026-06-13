#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

issue="draft"
file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) issue="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *) devctl_die "extra argument: $1" ;;
  esac
done

file="${file:-$(devctl_default_issue_file "$issue" issue-draft.md)}"

devctl_reject_publish_heading "$file" \
  "# Issue Draft" \
  "# Academic Issue Draft"

devctl_check_template_file "$file" \
  "<!-- xflow: issue-draft -->" \
  "## Background" \
  "## Problem" \
  "## Goal" \
  "## Scope" \
  "## Acceptance Criteria" \
  "## Verification Plan"

devctl_info "issue-draft check passed: $file"
