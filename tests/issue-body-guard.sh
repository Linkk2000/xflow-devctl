#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export DEVCTL_SKIP_PROVIDER_LOAD=1
# shellcheck source=../lib/common.sh
source "$OPS_ROOT/lib/common.sh"

expect_pass() {
  local body="$1"
  if ! ( devctl_validate_inline_issue_body "$body" ) >/dev/null 2>&1; then
    echo "expected inline body to pass: $body" >&2
    exit 1
  fi
}

expect_fail() {
  local body="$1"
  if ( devctl_validate_inline_issue_body "$body" ) >/dev/null 2>&1; then
    echo "expected inline body to fail: $body" >&2
    exit 1
  fi
}

expect_pass "simple status update"
expect_fail $'line1\nline2'
expect_fail 'line1\nline2'
expect_fail 'uses `code`'
expect_fail 'uses $(cmd)'

echo "issue body guard ok"
