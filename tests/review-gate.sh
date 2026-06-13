#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

run_devctl() {
  DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_SKIP_PROVIDER_LOAD=1 bash "$OPS_ROOT/devctl" "$@"
}

expect_fail() {
  if run_devctl "$@" >/dev/null 2>&1; then
    echo "expected command to fail: $*" >&2
    exit 1
  fi
}

expect_pass() {
  if ! run_devctl "$@" >/dev/null 2>&1; then
    echo "expected command to pass: $*" >&2
    run_devctl "$@"
    exit 1
  fi
}

write_review() {
  local issue="$1" action="$2" file="$3"
  local review_dir="$tmpdir/.xflow/issues/issue-${issue}/approvals"
  local hash
  mkdir -p "$review_dir"
  hash="$(sha256sum "$file" | awk '{print $1}')"
  cat >"$review_dir/local-review.md" <<EOF_REVIEW
# Local Review Approval

Issue: $issue
Reviewer: user
Approved At: 2026-06-13T00:00:00+08:00
Approved Action: $action
Approved File: $file
Approved SHA256: $hash

## Decision
Approved: yes
EOF_REVIEW
}

mkdir -p "$tmpdir/.xflow/issues/issue-draft" "$tmpdir/.xflow/issues/issue-1"

cat >"$tmpdir/.xflow/issues/issue-draft/issue-draft.md" <<'EOF_ISSUE'
<!-- xflow: issue-draft -->

## Background
Need a reviewable remote issue.

## Problem
Remote writes need local review.

## Goal
Create the issue only after approval.

## Scope
- Includes: review gate
- Excludes: academic fields

## Acceptance Criteria
- [ ] Local review gate blocks unapproved writes.

## Verification Plan
- bash tests/review-gate.sh
EOF_ISSUE

expect_pass check issue-draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
env -u DEVCTL_SKIP_PROVIDER_LOAD -u GITHUB_TOKEN -u GITHUB_ACCESS_TOKEN -u GITEE_TOKEN -u GITEE_ACCESS_TOKEN \
  DEVCTL_REPO_ROOT="$tmpdir" GITEE_ENV_FILE="$tmpdir/missing.env" \
  bash "$OPS_ROOT/devctl" check issue-draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" >/dev/null

cp "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" "$tmpdir/.xflow/issues/issue-draft/issue-draft.valid.md"
sed -i '1i# Issue Draft' "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_fail check issue-draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
mv "$tmpdir/.xflow/issues/issue-draft/issue-draft.valid.md" "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

cat >"$tmpdir/.xflow/issues/issue-1/mr-draft.md" <<'EOF_MR'
<!-- xflow: mr-draft -->

Closes #1

## Summary
- Add a generic local review gate.

## Test Plan
- bash tests/review-gate.sh

## Risk
- Low: shell-only validation.

## Review Request
- Please review the diff and local approval record.
EOF_MR

expect_pass check mr-draft --issue 1

cp "$tmpdir/.xflow/issues/issue-1/mr-draft.md" "$tmpdir/.xflow/issues/issue-1/mr-draft.valid.md"
sed -i '1i# MR Draft' "$tmpdir/.xflow/issues/issue-1/mr-draft.md"
expect_fail check mr-draft --issue 1
mv "$tmpdir/.xflow/issues/issue-1/mr-draft.valid.md" "$tmpdir/.xflow/issues/issue-1/mr-draft.md"

expect_fail check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create
write_review draft issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_pass check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create

printf '\nchanged\n' >>"$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_fail check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create
sed -i '$d' "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

rm -f "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
expect_fail issue create "Review gate" --body-file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
write_review draft issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

rm -f "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
expect_fail issue comment 1 --body-file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

expect_fail issue close 1

git -C "$tmpdir" init -q
git -C "$tmpdir" config user.email test@example.com
git -C "$tmpdir" config user.name "Test User"
git -C "$tmpdir" checkout -b main -q
touch "$tmpdir/README.md"
git -C "$tmpdir" add README.md
git -C "$tmpdir" commit -m init -q
git -C "$tmpdir" checkout -b feature/1-review-gate -q
expect_fail git mr --title "Review gate" --body-file "$tmpdir/.xflow/issues/issue-1/mr-draft.md" --base main --issue 1

echo "review gate ok"
