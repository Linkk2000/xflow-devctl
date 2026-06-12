#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVCTL_SKIP_PROVIDER_LOAD=1
source "$OPS_ROOT/lib/common.sh"
source "$OPS_ROOT/check/academic-common.sh"

academic_parse_issue_file_args "$@"
file="${academic_file:-$(academic_default_file issue-draft.md)}"

academic_check_template "$file" \
  "# Academic Issue Draft" \
  "Task Type:" \
  "Target Branch:" \
  "Target Artifacts:" \
  "## Background" \
  "## Goal" \
  "## Scope" \
  "## Acceptance Criteria" \
  "## Verification Plan" \
  "## Human Review Gate"

devctl_info "academic-issue check passed: $file"
