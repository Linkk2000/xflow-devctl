#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

has_python_310() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" - <<'PY' >/dev/null 2>&1 && return 0
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    fi
  done
  return 1
}

run_preflight() {
  local cwd="$1"
  (cd "$cwd" && DEVCTL_REPO_ROOT="$ROOT" DEVCTL_PRODUCT_LINE=academic bash "$ROOT/devctl" preflight)
}

assert_preflight_success() {
  local cwd="$1"
  output="$(run_preflight "$cwd")"
  echo "$output" | grep -F "product_line: academic" >/dev/null
  echo "$output" | grep -F "version:" >/dev/null
  echo "$output" | grep -F "tool_root: $ROOT" >/dev/null
}

assert_preflight_fail_closed() {
  local cwd="$1"
  if output="$(run_preflight "$cwd" 2>&1)"; then
    echo "expected devctl preflight to fail without Python 3.10+" >&2
    echo "$output" >&2
    exit 1
  fi
  echo "$output" | grep -F "Python 3.10+ is required" >/dev/null
  echo "$output" | grep -F "No installation was performed" >/dev/null
  echo "$output" | grep -F "winget install Python.Python.3.12" >/dev/null
}

assert_academic_issue_create_gate_blocks() {
  local tmpdir body
  tmpdir="$(mktemp -d)"
  body="$tmpdir/.xflow/issue-draft/issue-draft.md"
  mkdir -p "$(dirname "$body")"
  echo "# Academic Issue Draft" >"$body"

  if output="$(
    DEVCTL_REPO_ROOT="$tmpdir" \
    DEVCTL_PRODUCT_LINE=academic \
    DEVCTL_SKIP_PROVIDER_LOAD=1 \
      bash "$ROOT/devctl" issue create "must be blocked" --body-file "$body" 2>&1
  )"; then
    echo "expected academic issue create to fail without local approval" >&2
    echo "$output" >&2
    rm -rf "$tmpdir"
    exit 1
  fi
  echo "$output" | grep -F "academic local approval required" >/dev/null
  rm -rf "$tmpdir"
}

assert_academic_issue_create_fail_closed() {
  local tmpdir body
  tmpdir="$(mktemp -d)"
  body="$tmpdir/.xflow/issue-draft/issue-draft.md"
  mkdir -p "$(dirname "$body")"
  echo "# Academic Issue Draft" >"$body"

  if output="$(
    DEVCTL_REPO_ROOT="$tmpdir" \
    DEVCTL_PRODUCT_LINE=academic \
    DEVCTL_SKIP_PROVIDER_LOAD=1 \
      bash "$ROOT/devctl" issue create "must be blocked" --body-file "$body" 2>&1
  )"; then
    echo "expected academic issue create to fail without Python 3.10+" >&2
    echo "$output" >&2
    rm -rf "$tmpdir"
    exit 1
  fi
  echo "$output" | grep -F "Python 3.10+ is required" >/dev/null
  rm -rf "$tmpdir"
}

outside_dir="$(mktemp -d)"
trap 'rm -rf "$outside_dir"' EXIT

if has_python_310; then
  assert_preflight_success "$ROOT"
  assert_preflight_success "$outside_dir"
  assert_academic_issue_create_gate_blocks
else
  assert_preflight_fail_closed "$ROOT"
  assert_preflight_fail_closed "$outside_dir"
  assert_academic_issue_create_fail_closed
fi

echo "python launcher ok"
