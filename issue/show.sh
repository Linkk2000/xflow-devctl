#!/usr/bin/env bash
# devctl issue show <number>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

number="${1:-}"
[[ -n "$number" ]] || devctl_die "用法: devctl issue show <number>"

devctl_need_cmd jq

resp="$(provider_issue_show "$number")"

devctl_json_field "$resp" '"#(.number) [(.state)] (.title)

(.body // "")

(.html_url)"'
