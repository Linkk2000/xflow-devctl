#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

run_check() {
  DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_SKIP_PROVIDER_LOAD=1 bash "$OPS_ROOT/devctl" check "$@"
}

run_gate() {
  local action="$1" approved_file="$2" issue="${3:-}"
  DEVCTL_REPO_ROOT="$tmpdir" \
  DEVCTL_SKIP_PROVIDER_LOAD=1 \
  DEVCTL_ACADEMIC_ENFORCE=1 \
    bash -c 'source "$1/lib/common.sh"; devctl_academic_require_remote_approval "$2" "$3" "$4"' \
    _ "$OPS_ROOT" "$action" "$approved_file" "$issue"
}

expect_fail() {
  if run_check "$@" >/dev/null 2>&1; then
    echo "expected check to fail: $*" >&2
    exit 1
  fi
}

expect_pass() {
  if ! run_check "$@" >/dev/null 2>&1; then
    echo "expected check to pass: $*" >&2
    run_check "$@"
    exit 1
  fi
}

expect_gate_fail() {
  if run_gate "$@" >/dev/null 2>&1; then
    echo "expected gate to fail: $*" >&2
    exit 1
  fi
}

expect_gate_pass() {
  if ! run_gate "$@" >/dev/null 2>&1; then
    echo "expected gate to pass: $*" >&2
    run_gate "$@"
    exit 1
  fi
}

mkdir -p "$tmpdir/.xflow/issues/issue-1/approvals"

expect_fail academic-issue --issue 1

cat >"$tmpdir/.xflow/issues/issue-1/issue-draft.md" <<'EOF'
<!-- xflow: academic-issue-draft -->
<!-- task-type: tooling -->
<!-- workflow-product-line: academic -->
<!-- paper-base-branch: main -->
<!-- task-branch: feature/1-academic-gate -->

## Background
Academic tasks need local review gates.

## Goal
Validate academic task materials before remote writes.

## Scope
- Includes: local templates
- Excludes: remote provider APIs
- Affected paths: .xflow/issues/issue-1/

## Target Artifacts
- references/academic-workflow.md

## Acceptance Criteria
- [ ] Required templates are complete.

## Verification Plan
- devctl check academic-issue --issue 1

## Human Review Gate
- Reviewer: user
- Review focus: issue scope and usefulness
- Remote action allowed after approval: create issue
EOF

expect_pass academic-issue --issue 1

cp "$tmpdir/.xflow/issues/issue-1/issue-draft.md" "$tmpdir/.xflow/issues/issue-1/issue-draft.valid.md"
sed -i \
  -e '/workflow-product-line: academic/d' \
  -e '/paper-base-branch: main/d' \
  -e '/task-branch: feature\/1-academic-gate/c\Target Branch: academic' \
  "$tmpdir/.xflow/issues/issue-1/issue-draft.md"
expect_fail academic-issue --issue 1
mv "$tmpdir/.xflow/issues/issue-1/issue-draft.valid.md" "$tmpdir/.xflow/issues/issue-1/issue-draft.md"

expect_fail tdd-result --issue 1

cat >"$tmpdir/.xflow/issues/issue-1/tdd-result.md" <<'EOF'
# TDD Result

Issue: 1
Branch: feature/1-academic-gate
Verified At: 2026-06-13T00:00:00+08:00
Executor: Codex

## Verification Scope
- Includes: academic gate templates
- Excludes: remote provider APIs

## Commands
- bash tests/academic-checks.sh

## Results
- Passed: academic template checks
- Failed: none
- Skipped: none

## Risks
- Local approval authenticity still depends on reviewer discipline.

## Human Review Entry
- Basic target reached: yes
- Ready for local review: yes
EOF

expect_pass tdd-result --issue 1

expect_fail claude-package --issue 1

cat >"$tmpdir/.xflow/issues/issue-1/claude-task.md" <<'EOF'
# Claude Task Package

Issue: 1
Claude Skill: peer-review
Skill Source: AcademicForge
Invocation: /peer-review
Input Files:
- draft.md: sha256-placeholder
Output File: .xflow/issues/issue-1/claude-result.md

## Objective
Review the draft and return suggestions only.

## Constraints
- Do not overwrite final documents.

## Required Output Format
- Summary:
- Proposed changes:
- Risks:
- Questions:

## Human Review Requirement
Human review is required before use.
EOF

expect_pass claude-package --issue 1

cat >"$tmpdir/.xflow/issues/issue-1/mr-draft.md" <<'EOF'
<!-- xflow: academic-mr-draft -->
<!-- issue: 1 -->
<!-- workflow-product-line: academic -->
<!-- paper-base-branch: main -->
<!-- task-branch: feature/1-academic-gate -->

Closes #1

## Summary
- Add academic local gate checks.

## Evidence
- TDD Result: .xflow/issues/issue-1/tdd-result.md
- Local Review: .xflow/issues/issue-1/approvals/local-review.md

## Verification
- bash tests/academic-checks.sh

## Risks
- Local approval authenticity still depends on reviewer discipline.

## Review Request
- Please review scope, verification evidence, and local approval record.
EOF

expect_pass academic-mr --issue 1

cp "$tmpdir/.xflow/issues/issue-1/mr-draft.md" "$tmpdir/.xflow/issues/issue-1/mr-draft.valid.md"
sed -i \
  -e '/workflow-product-line: academic/d' \
  -e '/paper-base-branch: main/d' \
  -e '/task-branch: feature\/1-academic-gate/c\Target Branch: academic' \
  "$tmpdir/.xflow/issues/issue-1/mr-draft.md"
expect_fail academic-mr --issue 1
mv "$tmpdir/.xflow/issues/issue-1/mr-draft.valid.md" "$tmpdir/.xflow/issues/issue-1/mr-draft.md"

hash="$(sha256sum "$tmpdir/.xflow/issues/issue-1/tdd-result.md" | awk '{print $1}')"
cat >"$tmpdir/.xflow/issues/issue-1/approvals/local-review.md" <<EOF
# Local Review Approval

Issue: 1
Reviewer: user
Approved At: 2026-06-13T00:00:00+08:00
Approved Action: allow local review to proceed
Approved File: .xflow/issues/issue-1/tdd-result.md
Approved SHA256: $hash

## Decision
Approved: yes
EOF

expect_pass local-review --issue 1 --file "$tmpdir/.xflow/issues/issue-1/tdd-result.md"

printf '\nchanged\n' >>"$tmpdir/.xflow/issues/issue-1/tdd-result.md"
expect_fail local-review --issue 1 --file "$tmpdir/.xflow/issues/issue-1/tdd-result.md"

mkdir -p "$tmpdir/.xflow/issues/issue-draft/approvals"
cp "$tmpdir/.xflow/issues/issue-1/issue-draft.md" "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

expect_gate_fail issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" draft

draft_hash="$(sha256sum "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" | awk '{print $1}')"
cat >"$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md" <<EOF
# Local Review Approval

Issue: draft
Reviewer: user
Approved At: 2026-06-13T00:00:00+08:00
Approved Action: wrong-action
Approved File: .xflow/issues/issue-draft/issue-draft.md
Approved SHA256: $draft_hash

## Decision
Approved: yes
EOF

expect_gate_fail issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" draft

sed -i 's/Approved Action: wrong-action/Approved Action: issue-create/' "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
expect_gate_pass issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" draft

printf '\nchanged\n' >>"$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_gate_fail issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" draft

rm -f "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
if DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_SKIP_PROVIDER_LOAD=1 DEVCTL_ACADEMIC_ENFORCE=1 \
  bash "$OPS_ROOT/issue/create.sh" "Academic draft" --body-file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" >/dev/null 2>&1
then
  echo "expected issue create to fail without academic local approval" >&2
  exit 1
fi

echo "academic checks ok"
