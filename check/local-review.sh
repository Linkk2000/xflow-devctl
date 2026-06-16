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

for field in Reviewer "Approved At" "Approved Action" "Approved File" "Approved SHA256" Approved; do
  grep -Fq "${field}:" "$review_file" || devctl_die "invalid approval: missing $field"
  devctl_academic_reject_placeholder_field "$field" "$review_file"
done

approved_file_text="$(devctl_academic_field "Approved File" "$review_file")"
declared_file="$(devctl_academic_abs_path "$approved_file_text")"
actual_file="$(devctl_academic_abs_path "$approved_file")"
[[ "$declared_file" == "$actual_file" ]] || devctl_die "approval file mismatch: expected $approved_file, got $approved_file_text"

expected="$(devctl_academic_field "Approved SHA256" "$review_file" | tr '[:upper:]' '[:lower:]')"
actual="$(academic_sha256 "$approved_file")"
[[ "$expected" == "$actual" ]] || devctl_die "approval hash mismatch for $approved_file: expected $expected, actual $actual"

devctl_info "local-review check passed: $review_file"
