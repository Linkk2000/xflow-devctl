#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVCTL_SKIP_PROVIDER_LOAD=1
source "$OPS_ROOT/lib/common.sh"
source "$OPS_ROOT/check/academic-common.sh"

academic_parse_issue_file_args "$@"
file="${academic_file:-$(academic_default_file claude-task.md)}"

academic_check_template "$file" \
  "# Claude Task Package" \
  "Issue:" \
  "AcademicForge Skill:" \
  "Input Files:" \
  "Output File:" \
  "## Objective" \
  "## Constraints" \
  "## Required Output Format" \
  "## Human Review Requirement"

devctl_info "claude-package check passed: $file"
