#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

issue=""
file=""
action=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) issue="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    --action) action="$2"; shift 2 ;;
    -*) devctl_die "unknown option: $1" ;;
    *) devctl_die "extra argument: $1" ;;
  esac
done

[[ -n "$issue" ]] || devctl_die "--issue is required"
[[ -n "$file" ]] || devctl_die "--file is required"
[[ -n "$action" ]] || devctl_die "--action is required"

devctl_require_local_review "$issue" "$action" "$file"
devctl_info "local-review check passed: $file"
