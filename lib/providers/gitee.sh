# shellcheck shell=bash
# Gitee Provider for devctl

GITEE_API_BASE="https://gitee.com/api/v5"

provider_init() {
  local f="${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
  if [[ -f "$f" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$f"
    set +a
  fi
  GITEE_TOKEN="${GITEE_TOKEN:-${GITEE_ACCESS_TOKEN:-${access_token:-${GITEE_PRIVATE_TOKEN:-}}}}"
  [[ -n "$GITEE_TOKEN" ]] || devctl_die "未找到 Gitee Token。请设置 GITEE_TOKEN 或写入 ${GITEE_ENV_FILE:-$HOME/gitee.env.local}"
}

devctl_gitee_repo_path() {
  local suffix="$1"
  [[ "$suffix" == /* ]] || suffix="/${suffix}"
  echo "/repos/${DEVCTL_OWNER}/${DEVCTL_REPO}${suffix}"
}

devctl_gitee_api() {
  local method="$1" path="$2"
  shift 2
  devctl_need_cmd curl
  local url="${GITEE_API_BASE}${path}"
  local tmp http_code
  tmp="$(mktemp)"
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

provider_issue_create() {
  local title="$1" body="$2" labels="$3"
  local payload
  payload="$(jq -n \
    --arg title "$title" \
    --arg body "$body" \
    --arg labels "$labels" \
    '{title: $title, body: $body} + (if $labels != "" then {labels: $labels} else {} end)')"
  devctl_gitee_api_json POST "$(devctl_gitee_repo_path /issues)" "$payload"
}

provider_issue_show() {
  local number="$1"
  devctl_gitee_api GET "$(devctl_gitee_repo_path "/issues/${number}")"
}

provider_issue_list() {
  local state="$1" limit="$2"
  local path
  path="$(devctl_gitee_repo_path /issues)"
  local args=(--data-urlencode "per_page=${limit}" --data-urlencode "sort=updated")
  [[ -n "$state" ]] && args+=(--data-urlencode "state=${state}")
  devctl_gitee_api GET "$path" "${args[@]}"
}

provider_issue_comment() {
  local number="$1" body="$2"
  local payload
  payload="$(jq -n --arg body "$body" '{body: $body}')"
  devctl_gitee_api_json POST "$(devctl_gitee_repo_path "/issues/${number}/comments")" "$payload"
}

provider_issue_close() {
  local number="$1"
  local payload
  payload="$(jq -n '{state: "closed"}')"
  devctl_gitee_api_json PATCH "$(devctl_gitee_repo_path "/issues/${number}")" "$payload"
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
  devctl_gitee_api_json POST "$(devctl_gitee_repo_path /pulls)" "$payload"
}

provider_pr_get() {
  local pr_number="$1"
  devctl_gitee_api GET "$(devctl_gitee_repo_path "/pulls/${pr_number}")"
}