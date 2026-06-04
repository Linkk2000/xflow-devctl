#!/usr/bin/env bash
# devctl app status [--port PORT]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

frontend_port="${XFLOW_FRONTEND_PORT:-5173}"

usage() {
  cat <<'EOF'
Usage:
  devctl app status [--port PORT]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      frontend_port="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      devctl_die "unknown option: $1"
      ;;
  esac
done

run_dir="$(devctl_project_local_dir)/run"
pid_file="$run_dir/frontend.pid"
log_file="$run_dir/frontend.log"
url="http://localhost:${frontend_port}"

if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  devctl_info "frontend process: running pid $(cat "$pid_file")"
else
  devctl_warn "frontend process: not running"
fi

if curl -sf "$url" >/dev/null 2>&1; then
  devctl_info "frontend HTTP: ok ${url}"
else
  devctl_warn "frontend HTTP: unavailable ${url}"
fi

if [[ -f "$log_file" ]]; then
  devctl_info "log: $log_file"
fi
