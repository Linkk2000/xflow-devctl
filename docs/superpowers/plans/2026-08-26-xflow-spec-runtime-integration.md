# XFlow Spec Minimal Runtime Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the verified `xflow-devctl` Python runtime into `xflow-spec` with a thin dispatcher while restoring the existing Linux Shell path and limiting long-term platform code in the specification repository.

**Architecture:** A POSIX `/bin/sh` dispatcher selects the unchanged GNU Bash Linux backend or a pinned `.xflow/ops/devctl` submodule. A small profile and Python state adapter describe XFlow-specific topology; all generic process, Docker, Git, Issue, and browser behavior stays in `xflow-devctl`.

**Tech Stack:** POSIX `/bin/sh`, existing GNU Bash 4+ Linux scripts, Python 3.9+, YAML cockpit profile, Git submodule, existing XFlow work-item parser.

**Spec:** `docs/superpowers/specs/2026-08-25-platform-runtime-design.md` in the sibling `xflow-devctl` worktree that owns this plan

## Global Constraints

- Begin only after the `xflow-devctl` plan is complete, human-reviewed, and its exact clean HEAD is known.
- Begin only from latest, clean `xflow-spec/master`; if the canonical worktree is dirty or behind `origin/master`, stop without moving or committing those changes.
- `xflow-spec` commits directly to `master`; do not create a feature branch or MR.
- Linux commands, defaults, paths, service management, terminal behavior, and GNU Bash 4+ dependency remain unchanged.
- POSIX desktop execution requires Python 3.9+ but no additional GNU Bash or PowerShell.
- Product repositories receive no platform implementation changes in this plan.
- Existing `_ops/*.sh` files do not gain new platform conditionals.
- Formal files use neutral terms such as runtime capability, desktop environment, interpreter selection, and platform route.
- No machine-local path, credential, host identity, consumer business workflow, or unrelated planning file enters a commit.

---

### Task 1: Establish a clean master and capture the Linux contract

**Files:**
- Create: `_ops/tests/linux-runtime-contract.sh`
- Modify: `devctl`
- Read only: `_ops/git/*.sh`
- Read only: `_ops/issue/*.sh`
- Read only: `_ops/state/*.sh`
- Read only: `_ops/dev/*.sh`

**Interfaces:**
- Consumes: current `origin/master`
- Produces: a Linux contract test that can be run before and after dispatcher integration
- Produces: baseline evidence for help, argument forwarding, repository selection, and backend selection

- [ ] **Step 1: Verify the mandatory clean-master precondition**

```bash
git fetch origin master
test "$(git branch --show-current)" = master
test -z "$(git status --porcelain)"
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)"
```

Expected: every command exits 0. If any check fails, stop and ask the user to preserve or finish the existing work; do not stash, reset, clean, or move it.

- [ ] **Step 2: Record the pre-change Linux suite**

Run the existing Linux tests exactly as documented by the current repository, including both supported GNU Bash versions when their executables are configured:

```bash
XFLOW_TEST_LEGACY_BASH="${XFLOW_TEST_LEGACY_BASH:?configure supported legacy Bash}"
XFLOW_TEST_TARGET_BASH="${XFLOW_TEST_TARGET_BASH:?configure supported current Bash}"
XFLOW_TEST_LEGACY_BASH="$XFLOW_TEST_LEGACY_BASH" XFLOW_TEST_TARGET_BASH="$XFLOW_TEST_TARGET_BASH" bash _ops/tests/cross-platform-shell.sh
```

Expected: current documented count passes. Save the command and count in the commit body, not in a machine-local file.

- [ ] **Step 3: Write the failing contract test around an injectable backend marker**

The test creates a temporary fake Linux backend and expects the future dispatcher to honor `XFLOW_DEVCTL_LINUX_ENTRY` only when `XFLOW_RUNTIME_ROUTE=linux-test` is set. The override is test-only and must reject relative paths.

```sh
#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/xflow-linux-contract.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
cat >"$TMP/backend" <<'SH'
#!/bin/sh
printf 'backend=linux\n'
printf 'arg=%s\n' "$@"
SH
chmod +x "$TMP/backend"
output=$(XFLOW_RUNTIME_ROUTE=linux-test XFLOW_DEVCTL_LINUX_ENTRY="$TMP/backend" "$ROOT/devctl" --repo xflow-web git status)
printf '%s\n' "$output" | grep -Fx 'backend=linux'
printf '%s\n' "$output" | grep -Fx 'arg=--repo'
printf '%s\n' "$output" | grep -Fx 'arg=xflow-web'
printf '%s\n' "$output" | grep -Fx 'arg=git'
printf '%s\n' "$output" | grep -Fx 'arg=status'
```

- [ ] **Step 4: Run the new test and confirm RED**

```bash
bash _ops/tests/linux-runtime-contract.sh
```

Expected: FAIL because the current entrypoint has no isolated backend override.

- [ ] **Step 5: Add the test-only backend seam**

Before the current GNU Bash version guard, add only this fail-closed branch; normal invocations remain byte-for-byte on the existing path:

```bash
if [[ "${XFLOW_RUNTIME_ROUTE:-}" == linux-test ]]; then
  case "${XFLOW_DEVCTL_LINUX_ENTRY:-}" in
    /*) exec "${XFLOW_DEVCTL_LINUX_ENTRY}" "$@" ;;
    *) printf '%s\n' '[ERROR] XFLOW_DEVCTL_LINUX_ENTRY must be absolute in linux-test route.' >&2; exit 2 ;;
  esac
fi
```

- [ ] **Step 6: Verify the seam and commit the executable contract**

```bash
bash _ops/tests/linux-runtime-contract.sh
git diff --check
git add devctl _ops/tests/linux-runtime-contract.sh
git commit -m "test(devctl): 固化 Linux 后端参数合同" -m "- 验证仓库选择和子命令参数原样透传\n- 仅增加受限的测试后端注入点"
```

Expected: the contract test exits 0 and normal `./devctl help` remains unchanged.

### Task 2: Restore and isolate the unchanged Linux backend

**Files:**
- Create: `_ops/devctl-linux.sh`
- Modify: `devctl`
- Modify: `_ops/dev/all.sh`
- Modify: `_ops/dev/docker/setup.sh`
- Modify: `_ops/dev/docker/status.sh`
- Modify: `_ops/dev/preflight-run.sh`
- Modify: `_ops/dev/preflight.sh`
- Modify: `_ops/dev/sdk-m2.sh`
- Delete: `_ops/lib/platform.sh`
- Delete: `_ops/tests/cross-platform-shell.sh`
- Delete: `ops/plans/cross-platform-devctl-implementation.md`
- Delete: `ops/plans/cross-platform-devctl.md`
- Test: `_ops/tests/linux-runtime-contract.sh`

**Interfaces:**
- Produces: `_ops/devctl-linux.sh "$@"`, the sole Linux backend
- Produces: test-only absolute override `XFLOW_DEVCTL_LINUX_ENTRY` guarded by `XFLOW_RUNTIME_ROUTE=linux-test`
- Preserves: all pre-platform Linux Shell implementations from commit `97088c85405842d81cf06f6c10821e4c4866d7ca`

- [ ] **Step 1: Compare every platform-era file to the known Linux baseline**

```bash
git diff --stat 97088c85405842d81cf06f6c10821e4c4866d7ca..HEAD -- devctl _ops ops/plans
git diff 97088c85405842d81cf06f6c10821e4c4866d7ca..HEAD -- \
  _ops/dev/all.sh _ops/dev/docker/setup.sh _ops/dev/docker/status.sh \
  _ops/dev/preflight-run.sh _ops/dev/preflight.sh _ops/dev/sdk-m2.sh
```

Expected: review shows only the previously introduced runtime/platform modifications in the listed paths. If unrelated changes appear, preserve them explicitly and stop before editing that file.

- [ ] **Step 2: Restore Linux scripts and move only the router**

Using `apply_patch`, restore the six existing `_ops` scripts to their baseline content from commit `97088c8`; delete `_ops/lib/platform.sh` and the superseded compatibility test/plans. Move the command router portion of the baseline `devctl` into `_ops/devctl-linux.sh` unchanged except its cockpit calculation:

```bash
#!/usr/bin/env bash
set -euo pipefail
COCKPIT="${DEVCTL_COCKPIT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OPS="$COCKPIT/_ops"
export DEVCTL_COCKPIT_ROOT="$COCKPIT" DEVCTL_OPS_ROOT="$OPS"
```

After this prefix, copy the argument parsing, repository/focus resolution, help renderer, `run_script`, and complete `main` case dispatch verbatim from `97088c8:devctl`. Verify exactness with `git show 97088c8:devctl` and the Linux tests; do not paraphrase or simplify that body.

The new top-level `devctl` is temporarily a POSIX wrapper that routes only the test override and Linux backend; Task 3 adds the Python branch:

```sh
#!/bin/sh
set -eu
COCKPIT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LINUX_ENTRY="$COCKPIT/_ops/devctl-linux.sh"
if [ "${XFLOW_RUNTIME_ROUTE:-}" = linux-test ]; then
  case "${XFLOW_DEVCTL_LINUX_ENTRY:-}" in /*) LINUX_ENTRY=$XFLOW_DEVCTL_LINUX_ENTRY ;; *) exit 2 ;; esac
fi
export DEVCTL_COCKPIT_ROOT="$COCKPIT"
exec "$LINUX_ENTRY" "$@"
```

- [ ] **Step 3: Run the Linux contract test**

```bash
bash _ops/tests/linux-runtime-contract.sh
./devctl help >/tmp/xflow-help.out
grep -F 'XFlow devctl' /tmp/xflow-help.out
```

Expected: contract passes and the actual Linux backend prints current help.

- [ ] **Step 4: Run Linux regression before committing**

Run the repository's baseline Shell regression commands against `_ops/devctl-linux.sh`. Verify `git diff --check` and inspect the restored files against `97088c8`:

```bash
git diff --check
git diff --exit-code 97088c85405842d81cf06f6c10821e4c4866d7ca -- \
  _ops/dev/all.sh _ops/dev/docker/setup.sh _ops/dev/docker/status.sh \
  _ops/dev/preflight-run.sh _ops/dev/preflight.sh _ops/dev/sdk-m2.sh
```

Expected: no diff for restored Linux scripts; the new entry and test are the only runtime changes.

- [ ] **Step 5: Commit the Linux preservation boundary**

```bash
git add devctl _ops ops/plans
git commit -m "refactor(devctl): 隔离并保留 Linux 运行路径" -m "- 恢复既有 GNU Bash 脚本行为\n- 将平台分发与 Linux 命令路由分离\n- 删除已被 Python 运行时取代的兼容层"
```

### Task 3: Pin the verified Python runtime and dispatch by capability

**Files:**
- Modify: `.gitmodules`
- Add submodule: `.xflow/ops/devctl`
- Modify: `devctl`
- Modify: `_ops/tests/linux-runtime-contract.sh`
- Create: `_ops/tests/python-runtime-routing.sh`

**Interfaces:**
- Consumes: exact clean HEAD produced by the `xflow-devctl` plan
- Produces: `XFLOW_RUNTIME_ROUTE=auto|linux|python` override, with `auto` as default
- Produces: `XFLOW_DEVCTL_ROOT` explicit runtime override
- Produces: Linux route that never requires initialized submodule
- Produces: Python route invoking the pinned submodule `devctl`

- [ ] **Step 1: Write failing Python route tests**

Use a fake runtime directory containing a POSIX `devctl` recorder. Test explicit Python route, automatic non-Linux route through an injected capability probe, missing runtime diagnostics, exact argument forwarding, and Linux operation when `.xflow/ops/devctl` is absent.

```sh
output=$(XFLOW_RUNTIME_ROUTE=python XFLOW_DEVCTL_ROOT="$TMP/runtime" "$ROOT/devctl" state --json)
printf '%s\n' "$output" | grep -Fx 'arg=state'
printf '%s\n' "$output" | grep -Fx 'arg=--json'
```

- [ ] **Step 2: Run Python routing test and confirm RED**

```bash
bash _ops/tests/python-runtime-routing.sh
```

Expected: FAIL because the dispatcher has no Python branch.

- [ ] **Step 3: Add and pin the runtime submodule**

Obtain the reviewed runtime commit from the completed sibling worktree and pin exactly that commit:

```bash
runtime_root=../.worktrees/xflow-devctl-platform-runtime
runtime_commit=$(git -C "$runtime_root" rev-parse HEAD)
test -z "$(git -C "$runtime_root" status --porcelain)"
git submodule add -b main git@github.com:Linkk2000/xflow-devctl.git .xflow/ops/devctl
git -C .xflow/ops/devctl fetch origin "$runtime_commit"
git -C .xflow/ops/devctl checkout --detach "$runtime_commit"
```

Set `ignore = untracked` for the submodule, matching the repository's existing hygiene convention.

- [ ] **Step 4: Implement capability dispatch**

The POSIX dispatcher selects Linux when `uname -s` reports `Linux`; otherwise it selects Python. Explicit `XFLOW_RUNTIME_ROUTE` is accepted only for `linux`, `python`, and test routes. Python execution exports cockpit/workspace/profile paths and delegates without parsing command arguments:

```sh
RUNTIME_ROOT=${XFLOW_DEVCTL_ROOT:-"$COCKPIT/.xflow/ops/devctl"}
PROFILE="$COCKPIT/.xflow/cockpit.yaml"
export DEVCTL_COCKPIT_ROOT="$COCKPIT"
export XFLOW_WORKSPACE_ROOT="${XFLOW_WORKSPACE_ROOT:-$(CDPATH= cd -- "$COCKPIT/.." && pwd)}"
case "$route" in
  linux) exec "$COCKPIT/_ops/devctl-linux.sh" "$@" ;;
  python)
    [ -x "$RUNTIME_ROOT/devctl" ] || {
      printf '%s\n' '[ERROR] Python runtime is not initialized; run: git submodule update --init .xflow/ops/devctl' >&2
      exit 1
    }
    exec "$RUNTIME_ROOT/devctl" --profile "$PROFILE" --cockpit-root "$COCKPIT" "$@"
    ;;
esac
```

- [ ] **Step 5: Verify both routes and commit**

```bash
bash _ops/tests/linux-runtime-contract.sh
bash _ops/tests/python-runtime-routing.sh
XFLOW_RUNTIME_ROUTE=linux ./devctl help >/dev/null
XFLOW_RUNTIME_ROUTE=python ./devctl --help >/dev/null
git diff --check
git add .gitmodules .xflow/ops/devctl devctl _ops/tests
git commit -m "feat(devctl): 绑定并分发 Python 运行时" -m "- Linux 保持原有 GNU Bash 后端\n- 其他受支持环境进入固定版本 Python 核心\n- 缺失运行时在副作用前给出初始化命令"
```

### Task 4: Add the minimal XFlow cockpit profile and state adapter

**Files:**
- Create: `.xflow/cockpit.yaml`
- Create: `_ops/portable/state.py`
- Test: `_ops/tests/python-runtime-routing.sh`
- Modify: `_ops/help.txt`
- Modify: `ops/dev-workflow.md`

**Interfaces:**
- Consumes: profile schema and CLI from the pinned runtime
- Consumes: `_ops/lib/work_items.py`
- Produces: `_ops/portable/state.py show [--json]`
- Produces: `_ops/portable/state.py sync`
- Produces: profile definitions for preflight, Docker, four services, `run`, and three playgrounds

- [ ] **Step 1: Extend routing tests with real state fixtures**

Copy a minimal state/work-item fixture into a temporary cockpit. Assert `state --json` returns `active`, `next`, `blocked`, `done`, and aggregate counts; assert `state sync` preserves `active`, `repos`, `program_repos`, and `foci` while rebuilding only derived lists.

```python
payload = json.loads(run_state(cockpit, "show", "--json"))
assert payload["work_items"] == {
    "pending": 2, "passing": 1, "ready": 1, "blocked": 1,
    "per_slice": payload["work_items"]["per_slice"],
}
```

- [ ] **Step 2: Run the real-state test and confirm RED**

```bash
bash _ops/tests/python-runtime-routing.sh
```

Expected: FAIL because profile and state adapter do not exist.

- [ ] **Step 3: Extract existing state behavior into one Python adapter**

Move the embedded Python algorithms from `_ops/state/show.sh` and `_ops/state/sync.sh` into named functions without changing field semantics:

```python
def load_state(cockpit: Path) -> dict[str, object]:
    state = yaml.safe_load((cockpit / "state.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(state, dict):
        raise ValueError("state.yaml must contain a mapping")
    return state


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cockpit = Path(os.environ["DEVCTL_COCKPIT_ROOT"]).resolve()
    if args.action == "show":
        summary = summarize_state(cockpit, load_state(cockpit))
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else render_state_text(summary, color=sys.stdout.isatty()))
        return 0
    synchronize_state(cockpit, date.today())
    return 0
```

Implement `summarize_state`, `render_state_text`, and `synchronize_state` by extracting the complete algorithms currently embedded in `_ops/state/show.sh` and `_ops/state/sync.sh`: the same work-item loader calls, design-gate decisions, derived fields, sort order, headings, and count reconciliation are required. `main` obtains the cockpit only from `DEVCTL_COCKPIT_ROOT`, imports `_ops/lib/work_items.py` by its canonical cockpit-relative path, validates generated YAML with `yaml.safe_load`, and writes through a temporary sibling followed by `os.replace`. Text rendering preserves the current headings and counts.

- [ ] **Step 4: Add the strict profile**

The profile declares only repository topology and executable argument arrays. It uses these service IDs and existing commands:

```yaml
version: 1
state:
  command:
    argv: ["{python}", "{cockpit}/_ops/portable/state.py"]
    cwd: "{cockpit}"
preflight:
  checks:
    - {id: java, argv: [java, -version], expectRegex: 'version "21[.]'}
    - {id: maven, argv: [mvn, -version]}
    - {id: pnpm, argv: [pnpm, -v]}
    - {id: docker-compose, argv: [docker, compose, version]}
    - {id: docker-engine, argv: [docker, info]}
docker:
  cliCheck: {argv: [docker, --version]}
  composeCheck: {argv: [docker, compose, version]}
  engineProbe: {argv: [docker, info]}
  imageProbe: {argv: [docker, image, inspect, "postgres:16-alpine"]}
  startupTimeoutSeconds: 60
dependencies:
  - id: postgres
    cwd: "{workspace}/xflow-server"
    service: postgres
    up: {argv: [docker, compose, up, -d, postgres]}
    ready: {argv: [docker, compose, exec, -T, postgres, pg_isready, -U, xflow, -d, xflow]}
    timeoutSeconds: 120
services:
  - id: server
    cwd: "{workspace}/xflow-server"
    argv: [mvn, -pl, xflow-app, -am, spring-boot:run]
    dependsOn: [postgres]
    healthUrls: ["http://127.0.0.1:8080/actuator/health"]
    logFile: server.log
  - id: web
    cwd: "{workspace}/xflow-web"
    argv: [pnpm, dev, --host, "127.0.0.1", --port, "5173"]
    healthUrls: ["http://127.0.0.1:5173/"]
    logFile: web.log
scenarios:
  run: {services: [server, web], openUrl: "http://127.0.0.1:5173/"}
```

Add the existing demo services and all three playground definitions with their current ports, package filters, aliases, build prerequisite, and URLs. Do not put credentials or local installation paths in the profile.

- [ ] **Step 5: Verify state/profile behavior and commit**

```bash
XFLOW_RUNTIME_ROUTE=python ./devctl state --json | python3 -m json.tool >/dev/null
XFLOW_RUNTIME_ROUTE=python ./devctl dev preflight --warn-only
bash _ops/tests/python-runtime-routing.sh
git diff --check
git add .xflow/cockpit.yaml _ops/portable/state.py _ops/tests _ops/help.txt ops/dev-workflow.md
git commit -m "feat(devctl): 声明 XFlow 工作区与状态适配" -m "- 将既有状态算法提取为单个 Python 适配器\n- 以严格 profile 描述服务、检查和 playground\n- 不在规范仓复制通用进程逻辑"
```

### Task 5: Verify first-phase commands through product shims

**Files:**
- Modify only if a test exposes a template defect: `_ops/repo/new.sh`
- Modify only if a test exposes a template defect: `ops/dev-workflow.md`
- Test: `_ops/tests/python-runtime-routing.sh`

**Interfaces:**
- Consumes: product-repository `devctl` shims without adding platform code to those repositories
- Produces: evidence for Web, Server, and SDK read-only routing
- Produces: fail-fast behavior for commands outside the first-phase Python set

- [ ] **Step 1: Add shim integration fixtures**

Create temporary sibling `xflow-web`, `xflow-server`, and `xflow-sdk` Git repositories. Generate the existing shim template into each fixture, point `XFLOW_COCKPIT` at the temporary cockpit, and invoke `git status` through each shim. Also invoke one unsupported command and assert it produces no file or Git change.

- [ ] **Step 2: Run shim tests and confirm whether the current template passes**

```bash
bash _ops/tests/python-runtime-routing.sh
```

Expected: either PASS with no product changes, or a focused RED showing the shared shim template requires syntax supported by the system-provided shell.

- [ ] **Step 3: If RED, change only the generator template**

The generated shim remains a small forwarder and performs no platform detection:

```sh
#!/bin/sh
set -eu
SELF=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COCKPIT=${XFLOW_COCKPIT:-$(CDPATH= cd -- "$SELF/.." && pwd)/xflow-spec}
[ -x "$COCKPIT/devctl" ] || { printf '%s\n' '[ERROR] xflow-spec devctl not found' >&2; exit 1; }
exec "$COCKPIT/devctl" --repo "$(basename -- "$SELF")" "$@"
```

Do not edit existing product repositories in this plan. The template affects only newly generated shims; propagation to tracked shims requires a separate governed task if fixture evidence proves it necessary.

- [ ] **Step 4: Run shim and first-phase command tests**

```bash
bash _ops/tests/python-runtime-routing.sh
XFLOW_RUNTIME_ROUTE=python ./devctl --repo xflow-web git status
XFLOW_RUNTIME_ROUTE=python ./devctl issue list --limit 1
XFLOW_RUNTIME_ROUTE=python ./devctl pg --help
```

Expected: read-only commands and help exit 0; no remote writes occur.

- [ ] **Step 5: Commit only if the shared template changed**

```bash
if ! git diff --quiet -- _ops/repo/new.sh ops/dev-workflow.md _ops/tests; then
  git add _ops/repo/new.sh ops/dev-workflow.md _ops/tests
  git commit -m "fix(devctl): 统一产品仓薄转发入口" -m "- 仅保留仓库定位与参数转发\n- 平台路由继续由规范仓统一处理"
fi
```

### Task 6: Full Linux and Python route acceptance

**Files:**
- Modify: `AGENTS.md`
- Modify: `ops/dev-workflow.md`
- Modify: `_ops/help.txt`

**Interfaces:**
- Consumes: completed Linux and Python routes
- Produces: formal support statement, regression evidence, and clean `xflow-spec/master`

- [ ] **Step 1: Update only stable workflow documentation**

Document the dispatcher, Python 3.9 floor, submodule initialization command, runtime/profile overrides, first-phase command list, unsupported-command error, and Linux preservation contract. Remove text requiring `XFLOW_BASH` or a separately installed GNU Bash on the Python route. Keep Linux's existing GNU Bash statement unchanged.

- [ ] **Step 2: Run Linux regression**

```bash
XFLOW_RUNTIME_ROUTE=linux ./devctl help >/dev/null
bash _ops/tests/linux-runtime-contract.sh
```

Then run the repository's documented GNU Bash 4.4 and 5.x suites. Expected: the same passing counts captured in Task 1 and no new Linux dependency or terminal behavior.

- [ ] **Step 3: Run Python route regression**

```bash
PYTHON39="${PYTHON39:-/usr/bin/python3}"
DEVCTL_PYTHON="$PYTHON39" XFLOW_RUNTIME_ROUTE=python ./devctl state --json | "$PYTHON39" -m json.tool >/dev/null
DEVCTL_PYTHON="$PYTHON39" XFLOW_RUNTIME_ROUTE=python ./devctl dev preflight --warn-only
bash _ops/tests/python-runtime-routing.sh
```

Expected: all commands exit 0 without invoking a newer GNU Bash or PowerShell.

- [ ] **Step 4: Perform repository-boundary review**

```bash
git diff --check
git status --short
git diff HEAD~5..HEAD --stat
rg -n '/Users/|001-local|TOKEN=|PASSWORD=|SECRET=|COOKIE=' devctl .xflow _ops ops AGENTS.md
rg -n 'devops-provisioner|maven repository|artifact publish|pipeline trigger' devctl .xflow _ops ops AGENTS.md
git submodule status .xflow/ops/devctl
```

Expected: no local path, credential, consumer coupling, unrelated planning file, or dirty submodule; the submodule points at the reviewed runtime commit.

- [ ] **Step 5: Commit documentation and present push evidence**

```bash
git add AGENTS.md ops/dev-workflow.md _ops/help.txt
git commit -m "docs(devctl): 固化跨平台运行时工作流" -m "- 说明解释器和固定运行时发现顺序\n- 保留 Linux 原命令与依赖边界\n- 列出首阶段命令和回归入口"
git status --short
git log --oneline origin/master..HEAD
```

Expected: clean worktree with only reviewed commits ahead of `origin/master`. Do not push until the user explicitly approves the final diff and evidence.
