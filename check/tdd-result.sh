#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVCTL_SKIP_PROVIDER_LOAD=1
source "$OPS_ROOT/lib/common.sh"
source "$OPS_ROOT/check/academic-common.sh"

academic_parse_issue_file_args "$@"
file="${academic_file:-$(academic_default_file tdd-result.md)}"

academic_check_template "$file" \
  "# TDD Result" \
  "Issue:" \
  "Branch:" \
  "Verified At:" \
  "Executor:" \
  "## Verification Scope" \
  "## Commands" \
  "## Results" \
  "## Risks" \
  "## Human Review Entry"

devctl_info "tdd-result check passed: $file"
