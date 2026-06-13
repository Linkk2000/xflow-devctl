#!/usr/bin/env bash
# Shared helpers for Academic XFlow local checks.

academic_issue_id=""
academic_file=""

academic_parse_issue_file_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --issue)
        academic_issue_id="${2:-}"
        [[ -n "$academic_issue_id" ]] || devctl_die "--issue requires a value"
        shift 2
        ;;
      --file)
        academic_file="${2:-}"
        [[ -n "$academic_file" ]] || devctl_die "--file requires a value"
        shift 2
        ;;
      *)
        devctl_die "unknown argument: $1"
        ;;
    esac
  done
}

academic_issue_dir() {
  [[ -n "$academic_issue_id" ]] || devctl_die "--issue is required"
  echo "$DEVCTL_REPO_ROOT/.xflow/issues/issue-$academic_issue_id"
}

academic_default_file() {
  local name="$1"
  echo "$(academic_issue_dir)/$name"
}

academic_require_file() {
  local file="$1"
  [[ -f "$file" ]] || devctl_die "missing required file: $file"
}

academic_require_text() {
  local file="$1" text="$2"
  grep -Fq "$text" "$file" || devctl_die "missing required text '$text' in $file"
}

academic_check_template() {
  local file="$1"
  shift
  academic_require_file "$file"
  local required
  for required in "$@"; do
    academic_require_text "$file" "$required"
  done
}

academic_sha256() {
  local file="$1"
  academic_require_file "$file"
  sha256sum "$file" | awk '{print $1}'
}
