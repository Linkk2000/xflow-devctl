#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVCTL_SKIP_PROVIDER_LOAD=1
source "$OPS_ROOT/lib/common.sh"
source "$OPS_ROOT/check/academic-common.sh"

academic_parse_issue_file_args "$@"
review_file="$(academic_default_file approvals/local-review.md)"
approved_file="${academic_file:-}"
[[ -n "$approved_file" ]] || devctl_die "--file is required for local-review checks"

academic_check_template "$review_file" \
  "# Local Review Approval" \
  "Issue:" \
  "Reviewer:" \
  "Approved At:" \
  "Approved Action:" \
  "Approved File:" \
  "Approved SHA256:" \
  "## Decision" \
  "Approved: yes"

expected="$(grep -E '^Approved SHA256:' "$review_file" | head -1 | sed 's/^Approved SHA256:[[:space:]]*//')"
actual="$(academic_sha256 "$approved_file")"
[[ "$expected" == "$actual" ]] || devctl_die "approval hash mismatch for $approved_file"

devctl_info "local-review check passed: $review_file"
