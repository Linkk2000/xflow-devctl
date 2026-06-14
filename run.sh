#!/usr/bin/env bash
# devctl run [--backend PATH]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

backend=""
frontend_port="${XFLOW_FRONTEND_PORT:-5173}"
backend_port="${XFLOW_BACKEND_PORT:-8080}"
browser_url="http://localhost:${frontend_port}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) backend="$2"; shift 2 ;;
    *) devctl_die "unknown option: $1" ;;
  esac
done

devctl_need_cmd pnpm mvn

# 前端仓库根 = devctl 所在目录
frontend_root="$DEVCTL_REPO_ROOT"
backend_root="${backend:-${XFLOW_BACKEND_ROOT:-$(cd "$frontend_root/../xflow-server" 2>/dev/null && pwd || true)}}"

[[ -d "$backend_root" ]] || devctl_die "backend directory not found: set XFLOW_BACKEND_ROOT or --backend PATH"
[[ -f "$backend_root/pom.xml" ]] || devctl_die "invalid backend directory: $backend_root"

pids_dir="$(devctl_project_local_dir)/run"
mkdir -p "$pids_dir"
backend_pid_file="$pids_dir/backend.pid"
frontend_pid_file="$pids_dir/frontend.pid"

cleanup() {
  devctl_info "stopping processes"
  [[ -f "$backend_pid_file" ]] && kill "$(cat "$backend_pid_file")" 2>/dev/null || true
  [[ -f "$frontend_pid_file" ]] && kill "$(cat "$frontend_pid_file")" 2>/dev/null || true
  rm -f "$backend_pid_file" "$frontend_pid_file"
}
trap cleanup EXIT INT TERM

if [[ -f "$backend_pid_file" ]] && kill -0 "$(cat "$backend_pid_file")" 2>/dev/null; then
  devctl_warn "backend already appears to be running (pid $(cat "$backend_pid_file"))"
else
  devctl_info "starting backend: $backend_root"
  (
    cd "$backend_root"
    if ! docker compose ps --status running 2>/dev/null | grep -q postgres; then
      devctl_info "starting PostgreSQL (docker compose up -d)"
      docker compose up -d
    fi
    mvn spring-boot:run -pl xflow-app
  ) >"$pids_dir/backend.log" 2>&1 &
  echo $! >"$backend_pid_file"
fi

devctl_info "waiting for backend http://localhost:${backend_port}"
for _ in $(seq 1 90); do
  if curl -sf "http://localhost:${backend_port}/api/plugins/manifests" >/dev/null 2>&1 \
    || curl -sf "http://localhost:${backend_port}/actuator/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

devctl_info "starting frontend: $frontend_root"
(
  cd "$frontend_root"
  pnpm dev --host 127.0.0.1 --port "$frontend_port"
) >"$pids_dir/frontend.log" 2>&1 &
echo $! >"$frontend_pid_file"

devctl_info "waiting for frontend ${browser_url}"
for _ in $(seq 1 60); do
  if curl -sf "$browser_url" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$browser_url" >/dev/null 2>&1 || true
elif command -v sensible-browser >/dev/null 2>&1; then
  sensible-browser "$browser_url" >/dev/null 2>&1 || true
fi

devctl_info "started; logs: $pids_dir/backend.log / frontend.log"
devctl_info "press Ctrl+C to stop"

wait "$(cat "$frontend_pid_file")" 2>/dev/null || wait
