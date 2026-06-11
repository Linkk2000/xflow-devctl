#!/usr/bin/env bash
# devctl issue comment <number> [--body B] [--body-file F]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

number=""
body=""
body_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --body) body="$2"; shift 2 ;;
    --body-file) body_file="$2"; shift 2 ;;
    -*) devctl_die "未知选项: $1" ;;
    *)
      [[ -z "$number" ]] || devctl_die "多余的参数: $1"
      number="$1"
      shift
      ;;
  esac
done

[[ -n "$number" ]] || devctl_die "用法: devctl issue comment <number> [--body B] [--body-file F]"

if [[ -n "$body_file" ]]; then
  [[ -f "$body_file" ]] || devctl_die "文件不存在: $body_file"
  body="$(cat "$body_file")"
fi

[[ -n "$body" ]] || devctl_die "评论内容不能为空"

# 检测 Windows 终端环境下直接用字符串参数传递多行文本的截断风险
if [[ -z "$body_file" && -n "$body" ]]; then
  if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    if [[ "$body" == *$'\n'* || "$body" == *$'\r'* ]]; then
      devctl_die "错误: 在 Windows 环境下直接通过命令行传递多行文本会导致数据截断。请先将内容写入文件，并使用 --body-file 参数。"
    fi
  fi
fi

devctl_need_cmd jq

devctl_info "在 Issue #${number} 发表评论..."
# shellcheck disable=SC2034
resp="$(provider_issue_comment "$number" "$body")"

devctl_info "评论已发表。"
