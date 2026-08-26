#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

resolve_test_python() {
  if [ -n "${TEST_PYTHON:-}" ]; then
    printf '%s\n' "$TEST_PYTHON"
    return 0
  fi
  if [ -n "${DEVCTL_PYTHON:-}" ]; then
    printf '%s\n' "$DEVCTL_PYTHON"
    return 0
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

TEST_PYTHON="$(resolve_test_python)" || {
  echo "[ERROR] review-gate requires TEST_PYTHON, DEVCTL_PYTHON, python3, or python." >&2
  exit 1
}
if ! command -v "$TEST_PYTHON" >/dev/null 2>&1 && [ ! -x "$TEST_PYTHON" ]; then
  printf '[ERROR] review-gate interpreter is not executable or not on PATH: %s\n' "$TEST_PYTHON" >&2
  exit 1
fi
DEVCTL_PYTHON="${DEVCTL_PYTHON:-$TEST_PYTHON}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

git -C "$tmpdir" init -q
git -C "$tmpdir" config user.email test@example.com
git -C "$tmpdir" config user.name "Test User"
git -C "$tmpdir" checkout -b main -q
touch "$tmpdir/README.md"
git -C "$tmpdir" add README.md
git -C "$tmpdir" commit -m init -q

run_devctl() {
  DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_SKIP_PROVIDER_LOAD=1 DEVCTL_PYTHON="$DEVCTL_PYTHON" \
    bash "$OPS_ROOT/devctl" "$@"
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
  DEVCTL_REPO_ROOT="$tmpdir" PYTHONPATH="$OPS_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$TEST_PYTHON" - "$issue" "$action" "$file" <<'PY_REVIEW'
import os
import sys
from pathlib import Path

from xflow import approval

repo_root = Path(os.environ["DEVCTL_REPO_ROOT"])
review = approval.prepare(
    repo_root,
    sys.argv[1],
    sys.argv[2],
    Path(sys.argv[3]),
    reviewer="user",
    force=True,
)
review.write_bytes(
    review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes").encode("utf-8")
)
PY_REVIEW
}

mkdir -p "$tmpdir/.xflow/issues/issue-draft" "$tmpdir/.xflow/issues/issue-1"

cat >"$tmpdir/.xflow/current-task.md" <<'EOF_TASK'
# XFlow Current Task

Issue: draft
State: G1_APPROVE_ISSUE_CREATE

## Allowed Actions
- Create the approved issue.

## Forbidden Actions
- Create unreviewed remote writes.
EOF_TASK

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
  DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_PYTHON="$DEVCTL_PYTHON" GITEE_ENV_FILE="$tmpdir/missing.env" \
  bash "$OPS_ROOT/devctl" check issue-draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" >/dev/null

cp "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" "$tmpdir/.xflow/issues/issue-draft/issue-draft.valid.md"
sed -i.bak '1i\
# Issue Draft
' "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
rm -f "$tmpdir/.xflow/issues/issue-draft/issue-draft.md.bak"
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
sed -i.bak '1i\
# MR Draft
' "$tmpdir/.xflow/issues/issue-1/mr-draft.md"
rm -f "$tmpdir/.xflow/issues/issue-1/mr-draft.md.bak"
expect_fail check mr-draft --issue 1
mv "$tmpdir/.xflow/issues/issue-1/mr-draft.valid.md" "$tmpdir/.xflow/issues/issue-1/mr-draft.md"

expect_fail check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create
write_review draft issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_pass check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create

printf '\nchanged\n' >>"$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
expect_fail check local-review --issue draft --file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md" --action issue-create
sed -i.bak '$d' "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
rm -f "$tmpdir/.xflow/issues/issue-draft/issue-draft.md.bak"

rm -f "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
expect_fail issue create "Review gate" --body-file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"
write_review draft issue-create "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

rm -f "$tmpdir/.xflow/issues/issue-draft/approvals/local-review.md"
expect_fail issue comment 1 --body-file "$tmpdir/.xflow/issues/issue-draft/issue-draft.md"

expect_fail issue close 1

git -C "$tmpdir" checkout -b feature/1-review-gate -q
expect_fail git mr --title "Review gate" --body-file "$tmpdir/.xflow/issues/issue-1/mr-draft.md" --base main --issue 1

echo "review gate ok"
