#!/usr/bin/env bash
# devctl issue list [--state open|closed|all] [--limit N]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

state="open"
limit=20

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state) state="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    *) devctl_die "未知选项: $1" ;;
  esac
done

devctl_need_cmd jq curl

case "$state" in
  open) filter_state="open" ;;
  closed) filter_state="closed" ;;
  all) filter_state="" ;;
  *) devctl_die "--state 必须是 open|closed|all" ;;
esac

devctl_load_gitee_env
path="$(devctl_gitee_repo_path /issues)"

args=(--data-urlencode "access_token=${GITEE_TOKEN}" --data-urlencode "per_page=${limit}" --data-urlencode "sort=updated")
[[ -n "$filter_state" ]] && args+=(--data-urlencode "state=${filter_state}")

resp="$(curl -sS -G "${GITEE_API_BASE}${path}" "${args[@]}")"

echo "$resp" | jq -r '.[] | select(.pull_request == null or .pull_request == {}) | "#\(.number)\t[\(.state)]\t\(.title)"'
