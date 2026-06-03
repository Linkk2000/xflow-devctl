# shellcheck shell=bash
# GitHub Provider for devctl

GITHUB_API_BASE="https://api.github.com"

provider_init() {
  local f="${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
  if [[ -f "$f" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$f"
    set +a
  fi
  GITHUB_TOKEN="${GITHUB_TOKEN:-${GITHUB_ACCESS_TOKEN:-${access_token:-${GITHUB_PRIVATE_TOKEN:-}}}}"
  [[ -n "$GITHUB_TOKEN" ]] || devctl_die "未找到 GitHub Token。请设置 GITHUB_TOKEN 或写入 ${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
}

devctl_github_repo_path() {
  local suffix="$1"
  [[ "$suffix" == /* ]] || suffix="/${suffix}"
  echo "/repos/${DEVCTL_OWNER}/${DEVCTL_REPO}${suffix}"
}

devctl_github_api() {
  local method="$1" path="$2"
  shift 2
  devctl_need_cmd curl
  local url="${GITHUB_API_BASE}${path}"
  local tmp http_code
  tmp="$(mktemp)"
  http_code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "User-Agent: xflow-devctl" \
    -G \
    "$url" "$@")" || devctl_die "GitHub API 请求失败: $method $path"
  if [[ "$http_code" -ge 400 ]]; then
    devctl_error "GitHub API ${http_code}: $(cat "$tmp")"
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

devctl_github_api_json() {
  local method="$1" path="$2" body="$3"
  devctl_need_cmd curl
  local url="${GITHUB_API_BASE}${path}"
  local tmp http_code
  tmp="$(mktemp)"
  http_code="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "User-Agent: xflow-devctl" \
    -H 'Content-Type: application/json' \
    -d "$body" \
    "$url")" || devctl_die "GitHub API 请求失败: $method $path"
  if [[ "$http_code" -ge 400 ]]; then
    devctl_error "GitHub API ${http_code}: $(cat "$tmp")"
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

provider_issue_create() {
  local title="$1" body="$2" labels="$3"
  local labels_json="[]"
  if [[ -n "$labels" ]]; then
    labels_json="$(echo "$labels" | jq -R 'split(",")')"
  fi
  local payload
  payload="$(jq -n \
    --arg title "$title" \
    --arg body "$body" \
    --argjson labels "$labels_json" \
    '{title: $title, body: $body, labels: $labels}')"
  devctl_github_api_json POST "$(devctl_github_repo_path /issues)" "$payload"
}

provider_issue_show() {
  local number="$1"
  devctl_github_api GET "$(devctl_github_repo_path "/issues/${number}")"
}

provider_issue_list() {
  local state="$1" limit="$2"
  local path
  path="$(devctl_github_repo_path /issues)"
  local args=(--data-urlencode "per_page=${limit}" --data-urlencode "sort=updated")
  [[ -n "$state" ]] && args+=(--data-urlencode "state=${state}")
  devctl_github_api GET "$path" "${args[@]}"
}

provider_issue_comment() {
  local number="$1" body="$2"
  local payload
  payload="$(jq -n --arg body "$body" '{body: $body}')"
  devctl_github_api_json POST "$(devctl_github_repo_path "/issues/${number}/comments")" "$payload"
}

provider_issue_close() {
  local number="$1"
  local payload
  payload="$(jq -n '{state: "closed"}')"
  devctl_github_api_json PATCH "$(devctl_github_repo_path "/issues/${number}")" "$payload"
}

provider_pr_create() {
  local title="$1" body="$2" head="$3" base="$4"
  local payload
  payload="$(jq -n \
    --arg title "$title" \
    --arg body "$body" \
    --arg head "$head" \
    --arg base "$base" \
    '{title: $title, body: $body, head: $head, base: $base}')"
  devctl_github_api_json POST "$(devctl_github_repo_path /pulls)" "$payload"
}

provider_pr_get() {
  local pr_number="$1"
  devctl_github_api GET "$(devctl_github_repo_path "/pulls/${pr_number}")"
}