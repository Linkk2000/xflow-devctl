#!/usr/bin/env bash
# devctl app start-frontend [--port PORT] [--foreground]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$SCRIPT_DIR/../lib/common.sh"

frontend_port="${XFLOW_FRONTEND_PORT:-5173}"
foreground=0

usage() {
  cat <<'EOF'
Usage:
  devctl app start-frontend [--port PORT] [--foreground]

Starts the frontend dev server from the current project repository.
Runtime files are written to .xflow-local/ and ignored via .git/info/exclude.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      frontend_port="$2"
      shift 2
      ;;
    --foreground)
      foreground=1
      shift
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

devctl_need_cmd curl node setsid

[[ -f "$DEVCTL_REPO_ROOT/package.json" ]] || devctl_die "not a frontend repo: missing package.json in $DEVCTL_REPO_ROOT"
[[ -x "$DEVCTL_REPO_ROOT/node_modules/.bin/vite" ]] || devctl_die "missing Vite binary: run pnpm install in $DEVCTL_REPO_ROOT"

local_dir="$(devctl_project_local_dir)"
run_dir="$local_dir/run"
mkdir -p "$run_dir"
pid_file="$run_dir/frontend.pid"
log_file="$run_dir/frontend.log"
config_file="$local_dir/vite.xflow.wsl.config.mjs"
url="http://localhost:${frontend_port}"

cat >"$config_file" <<'EOF'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const projectRoot = process.env.DEVCTL_REPO_ROOT || process.cwd()
const require = createRequire(resolve(projectRoot, 'package.json'))
const viteConfigRequire = createRequire(resolve(projectRoot, 'internal/vite-config/package.json'))

const { defineConfig, loadEnv } = await import(require.resolve('vite'))
const vue = (await import(require.resolve('@vitejs/plugin-vue'))).default
const vueJsx = (await import(require.resolve('@vitejs/plugin-vue-jsx'))).default
const tailwindcss = (await import(viteConfigRequire.resolve('@tailwindcss/vite'))).default

const TAILWIND_REFERENCE_LINE = '@reference "@vben/tailwind-config/theme";\n'

function tailwindReferencePlugin() {
  return {
    enforce: 'pre',
    name: 'devctl:tailwind-reference',
    transform(code, id) {
      if (!id.includes('.vue') || !id.includes('type=style')) {
        return null
      }
      if (code.includes('@reference') || !code.includes('@apply')) {
        return null
      }
      return {
        code: TAILWIND_REFERENCE_LINE + code,
        map: null,
      }
    },
  }
}

export default defineConfig(({ mode }) => {
  const appRoot = resolve(projectRoot, process.env.XFLOW_FRONTEND_APP_ROOT || 'apps/xflow')
  const env = loadEnv(mode, appRoot)
  const port = Number(process.env.XFLOW_FRONTEND_PORT || env.VITE_PORT) || 5173

  return {
    root: appRoot,
    plugins: [
      vue({
        script: {
          defineModel: true,
        },
      }),
      vueJsx(),
      tailwindReferencePlugin(),
      tailwindcss(),
    ],
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(process.env.XFLOW_APP_VERSION || '0.1.0'),
    },
    resolve: {
      alias: {
        '#': resolve(appRoot, 'src'),
        '@warm-flow/designer-vueflow': resolve(projectRoot, 'packages/warmflow-designer/src/index.ts'),
      },
      dedupe: ['vue', '@vue-flow/core'],
    },
    optimizeDeps: {
      include: [
        '@vue-flow/core',
        '@vue-flow/background',
        '@vue-flow/controls',
        '@vue-flow/minimap',
      ],
    },
    server: {
      host: '0.0.0.0',
      port,
      proxy: {
        '/api': {
          changeOrigin: true,
          target: process.env.XFLOW_BACKEND_URL || 'http://localhost:8080',
          ws: true,
        },
      },
    },
  }
})
EOF

if [[ -f "$pid_file" ]]; then
  old_pid="$(cat "$pid_file")"
  if kill -0 "$old_pid" 2>/dev/null; then
    devctl_info "frontend already running: pid $old_pid, $url"
    exit 0
  fi
  rm -f "$pid_file"
fi

if curl -sf "$url" >/dev/null 2>&1; then
  devctl_die "port ${frontend_port} already responds with HTTP; confirm and stop the old service first"
fi

cmd=(
  "$DEVCTL_REPO_ROOT/node_modules/.bin/vite"
  --config "$config_file"
  --mode development
  --host 0.0.0.0
  --port "$frontend_port"
)

devctl_info "starting frontend: ${url}"
devctl_info "log: $log_file"

if [[ "$foreground" -eq 1 ]]; then
  cd "$DEVCTL_REPO_ROOT"
  export DEVCTL_REPO_ROOT XFLOW_FRONTEND_PORT="$frontend_port"
  exec "${cmd[@]}"
fi

(
  cd "$DEVCTL_REPO_ROOT"
  export DEVCTL_REPO_ROOT XFLOW_FRONTEND_PORT="$frontend_port"
  exec setsid "${cmd[@]}"
) >"$log_file" 2>&1 < /dev/null &
pid=$!
echo "$pid" >"$pid_file"

ready=0
for _ in $(seq 1 90); do
  if curl -sf "$url" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    devctl_error "frontend process exited; recent log:"
    tail -n 120 "$log_file" 2>/dev/null || true
    rm -f "$pid_file"
    exit 1
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  devctl_error "frontend startup timed out; recent log:"
  tail -n 120 "$log_file" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  exit 1
fi

devctl_info "frontend started: ${url} (pid ${pid})"
