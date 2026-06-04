#!/usr/bin/env bash
# devctl app stop-frontend
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

usage() {
  cat <<'EOF'
Usage:
  devctl app stop-frontend
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
[[ $# -eq 0 ]] || devctl_die "unknown option: $1"

run_dir="$(devctl_project_local_dir)/run"
pid_file="$run_dir/frontend.pid"

if [[ ! -f "$pid_file" ]]; then
  devctl_info "no recorded frontend process"
  exit 0
fi

pid="$(cat "$pid_file")"
if kill -0 "$pid" 2>/dev/null; then
  devctl_info "stopping frontend: pid $pid"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
else
  devctl_warn "recorded frontend process is gone: pid $pid"
fi

rm -f "$pid_file"
