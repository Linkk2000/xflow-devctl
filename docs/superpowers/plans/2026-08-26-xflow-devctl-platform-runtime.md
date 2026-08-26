# XFlow devctl Platform Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python 3.9+ cross-platform command runtime that preserves existing Git/Issue gates and can execute a repository-defined cockpit profile without requiring GNU Bash or PowerShell on POSIX desktop environments.

**Architecture:** Keep `xflow.cli` as the public router, move platform-neutral concerns into focused modules (`runtime`, `cockpit`, `commands`, `services`), and make both POSIX and PowerShell wrappers enter the same Python core. Repository-specific state and service definitions arrive through a validated YAML profile; the core never imports consumer business code.

**Tech Stack:** Python 3.9+, standard library (`argparse`, `dataclasses`, `pathlib`, `subprocess`, `signal`, `urllib`, `webbrowser`), PyYAML, POSIX `/bin/sh`, existing GitHub/Gitee providers.

**Spec:** `docs/superpowers/specs/2026-08-25-platform-runtime-design.md`

## Global Constraints

- Python runtime minimum is exactly 3.9.
- Every new Python module begins with `from __future__ import annotations`; type expressions must execute on Python 3.9.
- POSIX execution must not require GNU Bash or PowerShell.
- Windows `devctl.ps1` remains supported, but PowerShell tests run only when that capability exists.
- Existing remote-write approvals, task authority, provider detection, exit codes, and secret handling remain unchanged.
- Runtime and profile files must contain no consumer-specific delivery workflow or business fields.
- All subprocess commands are argument arrays; profile values never pass through `shell=True`.
- Formal files contain no machine-local absolute path, credential, or host identity.

---

### Task 1: Python 3.9-safe I/O and canonical paths

**Files:**
- Modify: `xflow/io.py`
- Modify: `xflow/env.py`
- Modify: `xflow/approval.py`
- Modify: `xflow/attachment.py`
- Modify: `xflow/checks.py`
- Modify: `xflow/cli.py`
- Modify: `xflow/migration.py`
- Create: `tests/support.py`
- Create: `tests/portable-runtime.py`
- Modify: Python test files currently using `Path.write_text(..., newline="\n")`

**Interfaces:**
- Produces: `canonical_path(path: Path) -> Path`
- Produces: `write_text_lf(path: Path, text: str, *, mode: str = "w") -> None`
- Produces: `tests.support.write_text_lf(path: Path, text: str) -> None`

- [ ] **Step 1: Write failing Python 3.9 portability tests**

```python
def test_canonical_path_collapses_equivalent_absolute_spellings(tmp_path: Path) -> None:
    alias = Path(str(tmp_path).replace("/private/var/", "/var/"))
    assert canonical_path(alias) == canonical_path(tmp_path)


def test_write_text_lf_uses_utf8_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "record.txt"
    write_text_lf(target, "第一行\n第二行\n")
    assert target.read_bytes() == "第一行\n第二行\n".encode("utf-8")
```

- [ ] **Step 2: Run the tests with the system Python 3.9 and confirm RED**

Run:

```bash
PYTHON39="${PYTHON39:-/usr/bin/python3}"
"$PYTHON39" tests/portable-runtime.py
```

Expected: FAIL because `canonical_path` and `write_text_lf` do not exist.

- [ ] **Step 3: Implement the shared primitives**

```python
def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def write_text_lf(path: Path, text: str, *, mode: str = "w") -> None:
    if mode not in {"w", "x"}:
        raise ValueError(f"unsupported text write mode: {mode}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(text)
```

Use `canonical_path` in `home_from_env`, `repo_root_from_env`, `RuntimeContext.from_env`, and returned environment-file paths. Replace production `Path.write_text(..., newline="\n")` calls with `write_text_lf`. Add the equivalent helper to `tests/support.py` and replace direct test calls so the suite itself runs on Python 3.9.

- [ ] **Step 4: Run focused and existing core tests**

```bash
"$PYTHON39" tests/portable-runtime.py
"$PYTHON39" tests/python-core.py
python tests/python-core.py
```

Expected: all three commands exit 0; the former environment-file path assertion passes after canonicalization.

- [ ] **Step 5: Commit the portability foundation**

```bash
git add xflow tests
git commit -m "fix(runtime): 支持 Python 3.9 文件与路径语义" -m "- 统一 UTF-8 LF 写入入口\n- 规范化等价工作区路径\n- 让核心测试可在 Python 3.9 执行"
```

### Task 2: Capability-scoped entrypoint tests

**Files:**
- Modify: `tests/entrypoint-routing.py`
- Create: `tests/powershell-entrypoint.py`
- Modify: `README.md`
- Modify: `help.txt`

**Interfaces:**
- Produces: `powershell_executable() -> Optional[str]`
- Produces: a POSIX routing suite that never invokes PowerShell
- Produces: an optional PowerShell-only suite with exit code 77 when unavailable

- [ ] **Step 1: Write a failing capability-isolation test**

```python
def test_posix_suite_does_not_require_powershell() -> None:
    source = (OPS_ROOT / "tests" / "entrypoint-routing.py").read_text(encoding="utf-8")
    assert '"powershell"' not in source
    assert '"pwsh"' not in source
```

- [ ] **Step 2: Run the current entrypoint suite and record RED**

```bash
python tests/entrypoint-routing.py
```

Expected: FAIL with `FileNotFoundError` for `powershell` in an environment where it is absent.

- [ ] **Step 3: Separate the suites without weakening PowerShell coverage**

Move all PowerShell calls and assertions into `tests/powershell-entrypoint.py` and resolve the executable explicitly:

```python
def powershell_executable() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def main() -> int:
    executable = powershell_executable()
    if executable is None:
        print("SKIP: PowerShell capability unavailable")
        return 77
    run_all_powershell_assertions(executable)
    return 0
```

Keep Python-core and POSIX-wrapper assertions in `entrypoint-routing.py`. Document the two independent commands and state that exit 77 is a capability skip, not a pass.

- [ ] **Step 4: Verify both capability paths**

```bash
python tests/entrypoint-routing.py
python tests/powershell-entrypoint.py; test "$?" -eq 0 -o "$?" -eq 77
```

Expected: POSIX suite exits 0; PowerShell suite exits 0 when available or 77 when absent.

- [ ] **Step 5: Commit test isolation**

```bash
git add tests/entrypoint-routing.py tests/powershell-entrypoint.py README.md help.txt
git commit -m "test(entrypoint): 按运行能力隔离入口验证" -m "- POSIX 回归不再依赖 PowerShell\n- 保留可独立执行的 PowerShell 入口测试"
```

### Task 3: POSIX launcher and generated wrappers

**Files:**
- Modify: `devctl`
- Modify: `xflow/migration.py`
- Create: `tests/posix-launcher.py`
- Modify: `tests/entrypoint-routing.py`
- Modify: `README.md`
- Modify: `help.txt`

**Interfaces:**
- Consumes: Python 3.9-safe core from Task 1
- Produces: `DEVCTL_PYTHON` interpreter override
- Produces: deterministic interpreter order `DEVCTL_PYTHON`, `python3`, `python`
- Produces: generated `devctl` wrapper with the same selection logic

- [ ] **Step 1: Write failing launcher selection tests**

Create fake interpreters that record invocation and return controlled version checks. Assert explicit override wins, `python3` precedes `python`, candidates below 3.9 are rejected, and all original arguments arrive at `python -m xflow` unchanged.

```python
result = run_launcher(
    env={"DEVCTL_PYTHON": str(fake_python), "PATH": str(fake_bin)},
    args=("issue", "show", "IK3RR6"),
)
assert result.returncode == 0
assert invocation.read_text() == "-m\nxflow\nissue\nshow\nIK3RR6\n"
```

- [ ] **Step 2: Run launcher tests and confirm RED**

```bash
python tests/posix-launcher.py
```

Expected: FAIL because the current entrypoint requires Bash and hardcodes `python`.

- [ ] **Step 3: Replace the launcher with POSIX `/bin/sh` logic**

The launcher must implement this behavior without arrays, `[[ ... ]]`, `source`, or `BASH_SOURCE`:

```sh
#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
select_python() {
  for candidate in "${DEVCTL_PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ] || continue
    "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 || continue
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

PYTHON=$(select_python) || {
  printf '%s\n' '[ERROR] devctl requires Python 3.9+; set DEVCTL_PYTHON.' >&2
  exit 1
}
export DEVCTL_TOOL_ROOT="$ROOT" DEVCTL_OPS_ROOT="$ROOT" PYTHONDONTWRITEBYTECODE=1
export DEVCTL_REPO_ROOT="${DEVCTL_REPO_ROOT:-$(pwd)}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m xflow "$@"
```

Update `wrapper_files()` so generated POSIX wrappers use identical discovery and set `DEVCTL_TOOL_ROOT` to `.xflow/ops/devctl`. Keep `devctl.ps1` and its Python-core routing intact, changing its stated floor to 3.9.

- [ ] **Step 4: Verify launcher and wrapper migration**

```bash
python tests/posix-launcher.py
python tests/entrypoint-routing.py
python tests/python-core.py
```

Expected: all exit 0.

- [ ] **Step 5: Commit the launcher**

```bash
git add devctl xflow/migration.py tests README.md help.txt
git commit -m "feat(entrypoint): 增加 POSIX Python 启动路径" -m "- 按固定顺序选择 Python 3.9+\n- 统一生成包装器与仓库入口行为\n- 保留 PowerShell 独立入口"
```

### Task 4: Validated cockpit profile

**Files:**
- Create: `xflow/cockpit.py`
- Create: `tests/cockpit-profile.py`
- Create: `tests/fixtures/cockpit-profile.yaml`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `CockpitContext(cockpit_root: Path, workspace_root: Path, repo_root: Path, python_executable: Path, run_dir: Path, env: Mapping[str, str])`
- Produces: `CommandSpec(argv: tuple[str, ...], cwd: str, env: Mapping[str, str])`
- Produces: `ComposeDependency(id: str, cwd: str, service: str, up: CommandSpec, ready: CommandSpec, timeout_seconds: int)`
- Produces: `ServiceSpec(id: str, command: CommandSpec, dependencies: tuple[str, ...], health_urls: tuple[str, ...], log_file: str)`
- Produces: `CheckSpec(id: str, command: CommandSpec, expect_regex: Optional[str])`
- Produces: `DockerSpec(cli_check: CommandSpec, compose_check: CommandSpec, engine_probe: CommandSpec, image_probe: Optional[CommandSpec], startup_timeout_seconds: int)`
- Produces: `ScenarioSpec(services: tuple[str, ...], open_url: Optional[str])`
- Produces: `PlaygroundSpec(id: str, aliases: tuple[str, ...], command: CommandSpec, build: Optional[CommandSpec], url: str)`
- Produces: `CockpitProfile(version: int, repositories: tuple[str, ...], default_playground: Optional[str], state_command: CommandSpec, checks: tuple[CheckSpec, ...], docker: DockerSpec, dependencies: Mapping[str, ComposeDependency], services: Mapping[str, ServiceSpec], scenarios: Mapping[str, ScenarioSpec], playgrounds: Mapping[str, PlaygroundSpec])`
- Produces: `load_cockpit_profile(path: Path) -> CockpitProfile`
- Produces: `expand_profile_path(value: str, cockpit_root: Path, workspace_root: Path, repo_root: Path) -> Path`

- [ ] **Step 1: Write profile parsing and rejection tests**

Use a fixture with `version: 1`, `state.command`, `preflight.checks`, Docker probes, one Compose dependency, four services, a `run` scenario, and three playground targets. Assert rejection of unknown top-level keys, `shell` strings, empty argv, `..` path escape, duplicate service IDs, unknown dependency/scenario IDs, nonpositive timeouts, and non-HTTP health URLs.

```python
profile = load_cockpit_profile(FIXTURES / "cockpit-profile.yaml")
assert profile.version == 1
assert profile.scenarios["run"].services == ("server", "web")
assert profile.playgrounds["flowable"].aliases == ("f", "bpmn")
```

- [ ] **Step 2: Run parser tests and confirm RED**

```bash
python tests/cockpit-profile.py
```

Expected: FAIL because `xflow.cockpit` does not exist.

- [ ] **Step 3: Implement strict dataclasses and loader**

Define frozen dataclasses and explicit field readers; do not pass raw YAML mappings beyond `load_cockpit_profile`. Path templates may contain only `{cockpit}`, `{workspace}`, and `{repo}`. Command argv templates additionally allow the built-in `{python}` token, resolved to `sys.executable`, and environment values declared in the profile's `allowedEnvironment` list.

```python
ALLOWED_PATH_TOKENS = {"cockpit", "workspace", "repo"}

def expand_profile_path(value: str, cockpit_root: Path, workspace_root: Path, repo_root: Path) -> Path:
    rendered = value.format(cockpit=str(cockpit_root), workspace=str(workspace_root), repo=str(repo_root))
    path = canonical_path(Path(rendered))
    if not any(path == root or root in path.parents for root in (cockpit_root, workspace_root, repo_root)):
        raise ValueError(f"profile path escapes declared roots: {value}")
    return path
```

- [ ] **Step 4: Run profile and core regressions**

```bash
python tests/cockpit-profile.py
python tests/python-core.py
```

Expected: both exit 0.

- [ ] **Step 5: Commit profile support**

```bash
git add xflow/cockpit.py tests/cockpit-profile.py tests/fixtures/cockpit-profile.yaml requirements.txt
git commit -m "feat(cockpit): 增加严格的工作区配置合同" -m "- 校验状态、检查、服务与场景定义\n- 禁止 Shell 字符串和路径越界\n- 为后续命令编排提供稳定数据类型"
```

### Task 5: Command execution, preflight, and Docker capabilities

**Files:**
- Create: `xflow/commands.py`
- Create: `xflow/capabilities.py`
- Create: `tests/cockpit-commands.py`

**Interfaces:**
- Consumes: `CommandSpec`, `CockpitProfile` from Task 4
- Produces: `CommandOutcome(argv: tuple[str, ...], returncode: int, stdout: str, stderr: str)`
- Produces: `execute_command(spec: CommandSpec, context: CockpitContext, *, capture: bool = False) -> CommandOutcome`
- Produces: `execute_state(profile: CockpitProfile, context: CockpitContext, args: Sequence[str]) -> int`
- Produces: `run_preflight(profile: CockpitProfile, context: CockpitContext, warn_only: bool) -> int`
- Produces: `run_docker(profile: CockpitProfile, context: CockpitContext, action: str) -> int`
- Produces: `start_engine_provider(env: Mapping[str, str]) -> bool`

- [ ] **Step 1: Write failing command and capability tests**

Test argument preservation with spaces, cwd expansion, allowed environment forwarding, missing executable diagnostics, preflight aggregation, `--warn-only`, Docker probe ordering, provider-start injection, engine readiness polling, and failure before an optional image probe.

```python
outcome = execute_command(spec, context, capture=True)
assert outcome.argv == (str(helper), "value with spaces")
assert outcome.returncode == 0
assert outcome.stdout == "value with spaces\n"
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
python tests/cockpit-commands.py
```

Expected: FAIL because command and capability modules do not exist.

- [ ] **Step 3: Implement shell-free command execution**

Use `subprocess.run(list(argv), shell=False, cwd=str(context.cwd), env=child_env)`. Redact environment values whose names contain `TOKEN`, `PASSWORD`, `SECRET`, or `COOKIE` from diagnostics. Preflight runs all checks, matches `expect_regex` against combined stdout/stderr when configured, and aggregates failures. Docker `status` runs CLI, Compose, and Engine probes. Docker `setup` verifies CLI/Compose, starts the registered engine provider only when the Engine probe fails, polls until `startup_timeout_seconds`, and then performs the optional image probe. Provider selection is isolated in `capabilities.py` and is tested through injected command discovery rather than an installed desktop application.

```python
completed = subprocess.run(
    list(argv), cwd=str(cwd), env=child_env, text=True, encoding="utf-8",
    stdout=subprocess.PIPE if capture else None,
    stderr=subprocess.PIPE if capture else None,
    shell=False,
)
```

- [ ] **Step 4: Verify focused tests**

```bash
python tests/cockpit-commands.py
python tests/cockpit-profile.py
```

Expected: both exit 0.

- [ ] **Step 5: Commit command capabilities**

```bash
git add xflow/commands.py xflow/capabilities.py tests/cockpit-commands.py
git commit -m "feat(cockpit): 增加无 Shell 命令与能力检查" -m "- 聚合开发环境预检结果\n- 以参数数组执行 Docker 操作\n- 保留退出码并过滤敏感诊断"
```

### Task 6: Service supervisor and playground execution

**Files:**
- Create: `xflow/services.py`
- Create: `tests/cockpit-services.py`

**Interfaces:**
- Consumes: profile and command interfaces from Tasks 4-5
- Produces: `ServiceHandle(id: str, process: subprocess.Popen[str], log_path: Path)`
- Produces: `ServiceSupervisor.start(service_ids: Sequence[str]) -> tuple[ServiceHandle, ...]`
- Produces: `ServiceSupervisor.ensure_dependencies(service_ids: Sequence[str]) -> None`
- Produces: `ServiceSupervisor.wait_healthy(handles: Sequence[ServiceHandle]) -> None`
- Produces: `ServiceSupervisor.stop_all() -> None`
- Produces: `run_scenario(profile: CockpitProfile, context: CockpitContext, scenario_id: str) -> int`
- Produces: `run_playground(profile: CockpitProfile, context: CockpitContext, target: str, open_browser: bool) -> int`

- [ ] **Step 1: Write lifecycle tests with short-lived fixture processes**

Cover Compose dependency startup/readiness, dependency de-duplication, service startup order, log paths, health fallback URLs, early child failure, signal cleanup, stale pid metadata rejection, alias resolution, and optional browser opening through an injected callable.

```python
supervisor = ServiceSupervisor(profile, context, opener=recorded_urls.append)
supervisor.ensure_dependencies(("server", "web"))
handles = supervisor.start(("server", "web"))
supervisor.wait_healthy(handles)
assert [handle.id for handle in handles] == ["server", "web"]
supervisor.stop_all()
assert all(handle.process.poll() is not None for handle in handles)
```

- [ ] **Step 2: Run lifecycle tests and confirm RED**

```bash
python tests/cockpit-services.py
```

Expected: FAIL because `ServiceSupervisor` does not exist.

- [ ] **Step 3: Implement deterministic supervision**

For each unique Compose dependency, run its `up` command and poll its `ready` command until `timeout_seconds`; on failure, stop before starting application services and identify the dependency ID. Start each child with `start_new_session=True`, write logs under the profile's run directory, store pid metadata atomically with `write_text_lf`, poll health URLs with `urllib.request.urlopen` until the configured deadline, and terminate process groups in reverse startup order. Browser opening uses `webbrowser.open(url, new=2)` only after health succeeds.

- [ ] **Step 4: Verify lifecycle and command tests**

```bash
python tests/cockpit-services.py
python tests/cockpit-commands.py
```

Expected: both exit 0 and leave no child processes or temporary pid files.

- [ ] **Step 5: Commit service orchestration**

```bash
git add xflow/services.py tests/cockpit-services.py
git commit -m "feat(cockpit): 增加服务与 playground 编排" -m "- 管理多进程启动、健康检查与逆序清理\n- 统一日志和 pid 元数据\n- 仅在服务就绪后打开浏览器"
```

### Task 7: Public CLI routing and compatibility contract

**Files:**
- Modify: `xflow/cli.py`
- Modify: `xflow/env.py`
- Modify: `xflow/__main__.py`
- Modify: `README.md`
- Modify: `help.txt`
- Create: `tests/cockpit-cli.py`
- Modify: `tests/entrypoint-routing.py`

**Interfaces:**
- Consumes: all interfaces from Tasks 3-6
- Produces: global `--profile PATH`, `--cockpit-root PATH`, and `--repo NAME`
- Produces: `state`, `dev preflight`, `dev docker setup|status`, `dev all`, `run`, `dev playground`, `pg`, and `playground`
- Preserves: existing `git` and `issue` parsers and approval paths

- [ ] **Step 1: Write failing end-to-end CLI tests**

Use a temporary multi-repository workspace and the fixture profile. Assert `--repo` resolves only declared sibling repositories; `state` runs the configured Python state provider; aliases choose the same playground; unsupported commands fail before side effects; existing `git status` and `issue list` still route through current Python functions.

```python
result = run_devctl(cockpit, "--profile", str(profile), "--repo", "xflow-web", "git", "status")
assert result.returncode == 0
assert "xflow-web" in result.stdout
```

- [ ] **Step 2: Run CLI tests and confirm RED**

```bash
python tests/cockpit-cli.py
```

Expected: argparse rejects `--profile`, `--cockpit-root`, or cockpit commands.

- [ ] **Step 3: Add routing without changing existing handlers**

Parse the three global options before subcommands, create a `CockpitContext`, then dispatch new commands to the modules from Tasks 4-6. Existing Git/Issue branches remain unchanged except that their `RuntimeContext.repo_root` comes from validated `--repo` resolution.

```python
if args.command == "state":
    return execute_state(profile, cockpit_context, args.state_args)
if args.command == "dev" and args.dev_command == "preflight":
    return run_preflight(profile, cockpit_context, args.warn_only)
```

Update help and README with the exact supported first-phase commands, Python discovery order, profile discovery, unsupported-command semantics, and independent platform test commands.

- [ ] **Step 4: Run focused and full declared regressions**

```bash
python tests/cockpit-cli.py
python tests/entrypoint-routing.py
python tests/python-core.py
python tests/cockpit-profile.py
python tests/cockpit-commands.py
python tests/cockpit-services.py
```

Expected: every command exits 0.

- [ ] **Step 5: Commit the public contract**

```bash
git add xflow README.md help.txt tests
git commit -m "feat(devctl): 接入可配置的跨平台 cockpit 命令" -m "- 保持 Git 与 Issue 门禁不变\n- 增加状态、预检、Docker 和服务场景入口\n- 固定首阶段命令与错误合同"
```

### Task 8: Dual-version regression and release-ready evidence

**Files:**
- Create: `tests/run-platform-runtime.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: completed public CLI from Task 7
- Produces: one deterministic regression entry accepting `PYTHON39` and `PYTHON_CURRENT`
- Produces: exact runtime commit for the dependent `xflow-spec` plan

- [ ] **Step 1: Write the regression driver**

```sh
#!/bin/sh
set -eu
PYTHON39=${PYTHON39:-/usr/bin/python3}
PYTHON_CURRENT=${PYTHON_CURRENT:-python3}
for python in "$PYTHON39" "$PYTHON_CURRENT"; do
  "$python" tests/portable-runtime.py
  "$python" tests/python-core.py
  "$python" tests/entrypoint-routing.py
  "$python" tests/cockpit-profile.py
  "$python" tests/cockpit-commands.py
  "$python" tests/cockpit-services.py
  "$python" tests/cockpit-cli.py
done
python tests/powershell-entrypoint.py || [ "$?" -eq 77 ]
git diff --check
```

- [ ] **Step 2: Run the complete driver**

```bash
sh tests/run-platform-runtime.sh
```

Expected: both Python lanes pass; PowerShell passes or reports capability skip 77; `git diff --check` passes.

- [ ] **Step 3: Scan delivery boundaries**

```bash
git diff origin/main...HEAD -- . ':!docs/superpowers/specs' ':!docs/superpowers/plans'
rg -n '/Users/|001-local|TOKEN=|PASSWORD=|SECRET=|COOKIE=' . --glob '!tests/fixtures/**'
rg -n 'devops-provisioner|maven|artifact|pipeline' xflow devctl README.md help.txt
```

Expected: no machine-local path or secret; no consumer-specific coupling in runtime code. Generic security field names in redaction tests are acceptable only inside test fixtures.

- [ ] **Step 4: Commit the verification entry**

```bash
git add tests/run-platform-runtime.sh README.md
git commit -m "test(runtime): 固化双版本跨平台回归入口" -m "- 覆盖 Python 3.9 与当前稳定版本\n- 将 PowerShell 作为独立可选能力验证\n- 为规范仓固定运行时提供可复现证据"
```

- [ ] **Step 5: Record the immutable candidate commit**

```bash
git status --short
git log --oneline origin/main..HEAD
git rev-parse HEAD
```

Expected: worktree clean; the printed HEAD is the only commit the dependent `xflow-spec` plan may pin after human review.
