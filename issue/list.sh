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
    *) devctl_die "unknown option: $1" ;;
  esac
done

devctl_need_cmd jq curl

case "$state" in
  open) filter_state="open" ;;
  closed) filter_state="closed" ;;
  all) filter_state="" ;;
  *) devctl_die "--state must be open|closed|all" ;;
esac

resp="$(provider_issue_list "$filter_state" "$limit")"

# 针对 github 的响应做空容错，如果是字符串（如出错），jq 会抛异常，所以先验证
if echo "$resp" | jq -e 'type == "array"' >/dev/null 2>&1; then
  echo "$resp" | jq -r '.[] | select(.pull_request == null or .pull_request == {}) | "#\(.number)\t[\(.state)]\t\(.title)"'
else
  # 如果返回错误对象，打印它的 message，或者直接报错
  message="$(echo "$resp" | jq -r '.message // empty')"
  if [[ -n "$message" ]]; then
    devctl_die "API returned an error: $message"
  else
    devctl_die "API returned an unexpected response: $resp"
  fi
fi
