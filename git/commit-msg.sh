#!/usr/bin/env bash
# devctl git commit-msg [-a] [-c] [-m "message"]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

stage_all=0
do_commit=0
override=""

while getopts ":acm:" opt; do
  case "$opt" in
    a) stage_all=1 ;;
    c) do_commit=1 ;;
    m) override="$OPTARG" ;;
    *) devctl_die "用法: devctl git commit-msg [-a] [-c] [-m message]" ;;
  esac
done

devctl_need_cmd git

if [[ "$stage_all" -eq 1 ]]; then
  git -C "$DEVCTL_REPO_ROOT" add -A
fi

msg="$(devctl_summarize_commit_message "$override")"
devctl_info "建议提交信息:"
printf '\n  %s\n\n' "$msg"

if [[ "$do_commit" -eq 1 ]]; then
  git -C "$DEVCTL_REPO_ROOT" commit -m "$msg"
  devctl_info "已提交"
else
  devctl_info "确认提交: devctl git commit-msg -c"
  devctl_info "或: git commit -m $(printf '%q' "$msg")"
fi
