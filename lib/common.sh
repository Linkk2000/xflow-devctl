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

devctl_validate_inline_issue_body() {
  local body="${1:-}"
  [[ -n "$body" ]] || return 0

  if [[ "$body" == *$'\n'* || "$body" == *$'\r'* || "$body" == *"\\n"* || "$body" == *"\\r"* ]]; then
    devctl_die "error: multi-line issue bodies must be passed with --body-file to avoid shell quoting and command-substitution corruption."
  fi
  if [[ "$body" == *'`'* || "$body" == *'$('* ]]; then
    devctl_die "error: shell-sensitive issue bodies must be passed with --body-file to avoid command-substitution corruption."
  fi
}

devctl_abs_path() {
  local path="$1" dir base
  if [[ "$path" = /* ]]; then
    dir="$(dirname "$path")"
    base="$(basename "$path")"
  else
    dir="$(dirname "$DEVCTL_REPO_ROOT/$path")"
    base="$(basename "$path")"
  fi
  (cd "$dir" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$base") || return 1
}

devctl_sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    devctl_die "sha256sum or shasum is required for local review checks"
  fi
}

devctl_issue_artifact_dir() {
  local issue="$1"
  if [[ "$issue" == "draft" ]]; then
    echo "$DEVCTL_REPO_ROOT/.xflow/issues/issue-draft"
  else
    echo "$DEVCTL_REPO_ROOT/.xflow/issues/issue-${issue}"
  fi
}

devctl_default_issue_file() {
  local issue="$1" file_name="$2"
  echo "$(devctl_issue_artifact_dir "$issue")/${file_name}"
}

devctl_review_field() {
  local file="$1" key="$2"
  grep -m1 -E "^${key}:" "$file" 2>/dev/null | sed -E "s/^${key}:[[:space:]]*//"
}

devctl_check_template_file() {
  local file="$1"
  shift
  [[ -f "$file" ]] || devctl_die "missing required file: $file"
  local needle
  for needle in "$@"; do
    grep -Fq -- "$needle" "$file" || devctl_die "missing required text '$needle' in $file"
  done
}

devctl_reject_publish_heading() {
  local file="$1"
  shift
  [[ -f "$file" ]] || devctl_die "missing required file: $file"
  local heading
  for heading in "$@"; do
    if grep -Eq "^[[:space:]]*${heading}[[:space:]]*$" "$file"; then
      devctl_die "internal draft heading is not allowed in remote body"
    fi
  done
}

devctl_require_local_review() {
  local issue="$1" action="$2" approved_file="$3"
  local review_file approved_action approved_path approved_hash actual_hash expected_abs approved_abs decision
  review_file="$(devctl_issue_artifact_dir "$issue")/approvals/local-review.md"
  [[ -f "$approved_file" ]] || devctl_die "approved file does not exist: $approved_file"
  [[ -f "$review_file" ]] || devctl_die "local review approval required: $review_file"

  approved_action="$(devctl_review_field "$review_file" "Approved Action")"
  approved_path="$(devctl_review_field "$review_file" "Approved File")"
  approved_hash="$(devctl_review_field "$review_file" "Approved SHA256")"
  decision="$(devctl_review_field "$review_file" "Approved")"

  [[ "$decision" == "yes" ]] || devctl_die "local review is not approved in $review_file"
  if [[ "$approved_action" != "$action" && "$approved_action" != "remote-write" ]]; then
    devctl_die "local review action mismatch: expected $action, got $approved_action"
  fi
  [[ -n "$approved_path" ]] || devctl_die "missing Approved File in $review_file"
  [[ -n "$approved_hash" ]] || devctl_die "missing Approved SHA256 in $review_file"

  expected_abs="$(devctl_abs_path "$approved_file")"
  approved_abs="$(devctl_abs_path "$approved_path")"
  [[ "$expected_abs" == "$approved_abs" ]] || devctl_die "local review file mismatch: expected $approved_file, got $approved_path"

  actual_hash="$(devctl_sha256_file "$approved_file")"
  [[ "$actual_hash" == "$approved_hash" ]] || devctl_die "local review hash mismatch for $approved_file"
}

devctl_project_local_dir() {
  local dir="$DEVCTL_REPO_ROOT/.xflow-local"
  mkdir -p "$dir"
  devctl_ensure_project_local_exclude
  echo "$dir"
}

devctl_ensure_project_local_exclude() {
  local git_dir exclude
  git_dir="$(git -C "$DEVCTL_REPO_ROOT" rev-parse --git-dir 2>/dev/null || true)"
  [[ -n "$git_dir" ]] || return 0
  case "$git_dir" in
    /*) exclude="$git_dir/info/exclude" ;;
    *) exclude="$DEVCTL_REPO_ROOT/$git_dir/info/exclude" ;;
  esac
  mkdir -p "$(dirname "$exclude")"
  touch "$exclude"
  grep -qxF ".xflow-local/" "$exclude" || printf '\n.xflow-local/\n' >>"$exclude"
}

# ── platform provider helper ──────────────────────────────────────────────────

devctl_parse_owner_repo() {
  local url
  url="$(git -C "$DEVCTL_REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  if [[ -z "$url" ]]; then
    DEVCTL_OWNER="${DEVCTL_OWNER:-}"
    DEVCTL_REPO="${DEVCTL_REPO:-}"
    return 0
  fi
  if [[ "$url" =~ [:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
    DEVCTL_OWNER="${BASH_REMATCH[1]}"
    DEVCTL_REPO="${BASH_REMATCH[2]%.git}"
  elif [[ "$url" =~ ^git@([^:]+):([^/]+)/([^/.]+)(\.git)?$ ]]; then
    DEVCTL_OWNER="${BASH_REMATCH[2]}"
    DEVCTL_REPO="${BASH_REMATCH[3]%.git}"
  else
    devctl_die "无法从 origin 解析 owner/repo: $url"
  fi
  export DEVCTL_OWNER DEVCTL_REPO
}

devctl_load_provider() {
  local platform="${XFLOW_PLATFORM:-}"
  if [[ -z "$platform" ]]; then
    local url
    url="$(git -C "$DEVCTL_REPO_ROOT" remote get-url origin 2>/dev/null || true)"
    if [[ "$url" =~ github\.com ]]; then
      platform="github"
    elif [[ "$url" =~ gitee\.com ]]; then
      platform="gitee"
    else
      platform="github"
    fi
  fi

  local provider_script="$DEVCTL_OPS_ROOT/lib/providers/${platform}.sh"
  if [[ -f "$provider_script" ]]; then
    # shellcheck disable=SC1090
    source "$provider_script"
    devctl_parse_owner_repo
    provider_init
  else
    devctl_die "不支持的平台提供者: $platform (未找到 $provider_script)"
  fi
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

if [[ "${DEVCTL_SKIP_PROVIDER_LOAD:-0}" != "1" ]]; then
  devctl_load_provider
fi
