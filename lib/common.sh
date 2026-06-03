# shellcheck shell=bash
# XFlow devctl — shared library (sourced, not executed)

[[ -n "${DEVCTL_COMMON_LOADED:-}" ]] && return 0
DEVCTL_COMMON_LOADED=1

set -euo pipefail

DEVCTL_REPO_ROOT="${DEVCTL_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
DEVCTL_OPS_ROOT="${DEVCTL_OPS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
GITEE_API_BASE="${GITEE_API_BASE:-https://gitee.com/api/v5}"

# ── output ────────────────────────────────────────────────────────────────────

devctl_info()  { printf '\033[36m[INFO]\033[0m %s\n' "$*"; }
devctl_warn()  { printf '\033[33m[WARN]\033[0m %s\n' "$*" >&2; }
devctl_error() { printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; }

devctl_die() {
  devctl_error "$@"
  exit 1
}

devctl_need_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || devctl_die "缺少命令: $c"
  done
}

# ── gitee credentials ───────────────────────────────────────────────────────

devctl_load_gitee_env() {
  local f="${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
  if [[ -f "$f" ]]; then
    # shellcheck disable=SC1090
    set -a
    # shellcheck source=/dev/null
    source "$f"
    set +a
  fi
  GITEE_TOKEN="${GITEE_TOKEN:-${GITEE_ACCESS_TOKEN:-${access_token:-${GITEE_PRIVATE_TOKEN:-}}}}"
  [[ -n "$GITEE_TOKEN" ]] || devctl_die "未找到 Gitee Token。请设置 GITEE_TOKEN 或写入 ${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
}

devctl_gitee_owner_repo() {
  local url
  url="$(git -C "$DEVCTL_REPO_ROOT" remote get-url origin 2>/dev/null)" || devctl_die "无法读取 origin 远程地址"
  if [[ "$url" =~ gitee\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
    GITEE_OWNER="${BASH_REMATCH[1]}"
    GITEE_REPO="${BASH_REMATCH[2]%.git}"
  elif [[ "$url" =~ ^git@([^:]+):([^/]+)/([^/.]+)(\.git)?$ ]]; then
    GITEE_OWNER="${BASH_REMATCH[2]}"
    GITEE_REPO="${BASH_REMATCH[3]%.git}"
  else
    devctl_die "无法从 origin 解析 Gitee owner/repo: $url"
  fi
  export GITEE_OWNER GITEE_REPO
}

# 参数为 /issues、/pulls 等仓库内路径后缀
devctl_gitee_repo_path() {
  devctl_gitee_owner_repo
  local suffix="$1"
  [[ "$suffix" == /* ]] || suffix="/${suffix}"
  echo "/repos/${GITEE_OWNER}/${GITEE_REPO}${suffix}"
}

devctl_gitee_api() {
  local method="$1" path="$2"
  shift 2
  devctl_load_gitee_env
  devctl_need_cmd curl
  local url="${GITEE_API_BASE}${path}"
  local tmp
  tmp="$(mktemp)"
  local http_code
  http_code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
    -H 'Content-Type: application/json' \
    -G --data-urlencode "access_token=${GITEE_TOKEN}" \
    "$url" "$@")" || devctl_die "Gitee API 请求失败: $method $path"
  if [[ "$http_code" -ge 400 ]]; then
    devctl_error "Gitee API ${http_code}: $(cat "$tmp")"
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

devctl_gitee_api_json() {
  local method="$1" path="$2" body="$3"
  devctl_load_gitee_env
  devctl_need_cmd curl
  local url="${GITEE_API_BASE}${path}?access_token=${GITEE_TOKEN}"
  local tmp http_code
  tmp="$(mktemp)"
  http_code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
    -H 'Content-Type: application/json' \
    -d "$body" \
    "$url")" || devctl_die "Gitee API 请求失败: $method $path"
  if [[ "$http_code" -ge 400 ]]; then
    devctl_error "Gitee API ${http_code}: $(cat "$tmp")"
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

# ── git helpers ───────────────────────────────────────────────────────────────

devctl_default_base_branch() {
  local base="${DEVCTL_BASE_BRANCH:-}"
  if [[ -n "$base" ]]; then
    echo "$base"
    return
  fi
  if git -C "$DEVCTL_REPO_ROOT" show-ref --verify --quiet refs/heads/master; then
    echo master
  elif git -C "$DEVCTL_REPO_ROOT" show-ref --verify --quiet refs/heads/main; then
    echo main
  else
    git -C "$DEVCTL_REPO_ROOT" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || echo master
  fi
}

devctl_require_clean_worktree() {
  if ! git -C "$DEVCTL_REPO_ROOT" diff --quiet 2>/dev/null; then
    devctl_die "工作区有未暂存修改，请先 commit 或 stash"
  fi
  if ! git -C "$DEVCTL_REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    devctl_die "暂存区有未提交修改，请先 commit 或 reset"
  fi
  if [[ -n "$(git -C "$DEVCTL_REPO_ROOT" ls-files --others --exclude-standard)" ]]; then
    devctl_die "存在未跟踪文件，请先处理（add / .gitignore / 删除）"
  fi
}

devctl_current_branch() {
  git -C "$DEVCTL_REPO_ROOT" branch --show-current
}

devctl_branch_slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g; s/-+/-/g'
}

devctl_branch_name_from_slug() {
  local slug issue prefix="${DEVCTL_BRANCH_PREFIX:-feat}"
  slug="$(devctl_branch_slugify "$1")"
  [[ -n "$slug" ]] || devctl_die "slug 无效"
  issue="${2:-}"
  if [[ -n "$issue" ]]; then
    echo "${prefix}/${issue}-${slug}"
  else
    echo "${prefix}/${slug}"
  fi
}

devctl_set_branch_meta() {
  local key="$1" val="$2"
  git -C "$DEVCTL_REPO_ROOT" config --local "devctl.${key}" "$val"
}

devctl_get_branch_meta() {
  local key="$1"
  git -C "$DEVCTL_REPO_ROOT" config --local --get "devctl.${key}" 2>/dev/null || true
}

devctl_push_current_branch() {
  local branch upstream
  branch="$(devctl_current_branch)"
  upstream="$(git -C "$DEVCTL_REPO_ROOT" rev-parse --abbrev-ref "${branch}@{upstream}" 2>/dev/null || true)"
  if [[ -z "$upstream" ]]; then
    devctl_info "推送并设置 upstream: origin/${branch}"
    git -C "$DEVCTL_REPO_ROOT" push -u origin "$branch"
  else
    git -C "$DEVCTL_REPO_ROOT" push origin "$branch"
  fi
}

# ── commit message heuristic ──────────────────────────────────────────────────

devctl_guess_commit_type() {
  local f
  while IFS= read -r f; do
    case "$f" in
      *.md|docs/*|AGENTS.md|README*) echo docs; return ;;
      *_test.*|*test/*|tests/*) echo test; return ;;
      _ops/*|devctl|scripts/*) echo chore; return ;;
      *.vue|*.tsx|*.jsx) echo feat; return ;;
      *.java) echo feat; return ;;
    esac
  done < <(git -C "$DEVCTL_REPO_ROOT" diff --cached --name-only 2>/dev/null; git -C "$DEVCTL_REPO_ROOT" diff --name-only 2>/dev/null)
  echo chore
}

devctl_guess_commit_scope() {
  local paths scope
  paths="$( { git -C "$DEVCTL_REPO_ROOT" diff --cached --name-only; git -C "$DEVCTL_REPO_ROOT" diff --name-only; } 2>/dev/null | sort -u | head -5)"
  if echo "$paths" | grep -q 'warmflow-designer'; then echo warmflow-designer; return; fi
  if echo "$paths" | grep -q 'xflow-server\|xflow-app'; then echo server; return; fi
  if echo "$paths" | grep -q 'apps/xflow'; then echo xflow; return; fi
  if echo "$paths" | grep -q '_ops/'; then echo devctl; return; fi
  scope="$(echo "$paths" | head -1 | cut -d/ -f1-2)"
  [[ -n "$scope" ]] && echo "$scope" || echo dev
}

devctl_summarize_commit_message() {
  local override="${1:-}" type scope summary files
  if [[ -n "$override" ]]; then
    echo "$override"
    return
  fi
  devctl_need_cmd git
  type="$(devctl_guess_commit_type)"
  scope="$(devctl_guess_commit_scope)"
  files="$( { git -C "$DEVCTL_REPO_ROOT" diff --cached --name-only; git -C "$DEVCTL_REPO_ROOT" diff --name-only; } 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$files" -eq 0 ]]; then
    devctl_die "没有可总结的变更（工作区与暂存区均为空）"
  fi
  summary="$( { git -C "$DEVCTL_REPO_ROOT" diff --cached --name-only; git -C "$DEVCTL_REPO_ROOT" diff --name-only; } 2>/dev/null \
    | sort -u | head -3 | xargs -I{} basename {} | paste -sd', ' -)"
  if [[ "$files" -gt 3 ]]; then
    summary="${summary} 等 ${files} 个文件"
  fi
  echo "${type}(${scope}): 更新 ${summary}"
}

# ── jq optional ───────────────────────────────────────────────────────────────

devctl_json_field() {
  local json="$1" jq_expr="$2"
  if command -v jq >/dev/null 2>&1; then
    echo "$json" | jq -r "$jq_expr"
  else
    devctl_die "需要 jq 解析 Gitee API 响应（请安装 jq）"
  fi
}
