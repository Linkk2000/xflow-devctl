from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

from xflow import approval as approval_gate
from xflow.migration import wrapper_files


def test_env() -> dict[str, str]:
    return {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
    }


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    write_text_lf(path, text)


def approval(repo_root: Path, issue: str, action: str, approved_file: Path) -> None:
    review = approval_gate.prepare(
        repo_root,
        issue,
        action,
        approved_file,
        reviewer="user",
        force=True,
    )
    write_text_lf(review, review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))


def run_devctl(repo_root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["DEVCTL_SKIP_PROVIDER_LOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(OPS_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected exit {expect}: {' '.join(args)}")
    return result


def run_posix_devctl(repo_root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DEVCTL_SKIP_PROVIDER_LOAD", None)
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["GITHUB_API_BASE"] = "http://127.0.0.1:9"
    env["GITHUB_TOKEN"] = "entrypoint-test-token"
    env["XFLOW_PLATFORM"] = "github"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [str(OPS_ROOT / "devctl"), *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected POSIX entrypoint exit {expect}: {' '.join(args)}")
    return result


def assert_dependency_check_routing(root: Path) -> None:
    repo = root / "dependency-routing"
    dependency_file = repo / ".xflow" / "issues" / "issue-IK152D" / "dependencies.yaml"
    write(
        dependency_file,
        """version: 0.1.0
issue: IK152D
dependencies:
  - issue: IK17AW
    repository: xflow-web
    type: child-feature
    requiredFor: [C-004]
    status: active
    blockingAssessment: partial
    decision: continue
    rationale: 不受影响的开发和测试继续进行。
""",
    )
    default_result = run_devctl(repo, "check", "dependencies", "--issue", "IK152D")
    assert "[WARN] dependency #IK17AW is active" in default_result.stdout
    assert str(dependency_file) in default_result.stdout

    explicit_result = run_devctl(
        repo,
        "check",
        "dependencies",
        "--issue",
        "IK152D",
        "--file",
        str(dependency_file),
    )
    assert "dependencies check passed" in explicit_result.stdout

    write(dependency_file, dependency_file.read_text(encoding="utf-8").replace("type: child-feature", "type: invalid"))
    invalid_result = run_devctl(repo, "check", "dependencies", "--issue", "IK152D", expect=1)
    assert "invalid dependency #IK17AW type" in invalid_result.stderr


def assert_commit_message_check_routing(root: Path) -> None:
    repo = root / "commit-message-routing"
    message_file = repo / ".xflow" / "local" / "commit-message.txt"
    message_file.parent.mkdir(parents=True, exist_ok=True)
    message_file.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "feat(canvas): 修复稳定端点定位[#IK152D]\n\n"
            "- 调整统一端点计算\n"
            "- 覆盖 C-004 并记录测试证据\n"
        ).encode("utf-8")
    )
    valid = run_devctl(
        repo,
        "check",
        "commit-msg",
        "--file",
        str(message_file),
        "--issue",
        "IK152D",
    )
    assert "associated Issues: #IK152D" in valid.stdout
    assert "commit-msg check passed" in valid.stdout

    write(message_file, "feat: invalid message\n")
    invalid = run_devctl(repo, "check", "commit-msg", "--file", str(message_file), expect=1)
    assert "commit subject" in invalid.stderr


def assert_no_legacy_run_command() -> None:
    entrypoint = (OPS_ROOT / "devctl").read_text(encoding="utf-8")
    assert entrypoint.startswith("#!/bin/sh\n")
    assert "set -eu" in entrypoint
    assert "select_python()" in entrypoint
    assert '"${DEVCTL_PYTHON:-}" python3 python' in entrypoint
    assert 'exec "$PYTHON" -m xflow "$@"' in entrypoint
    assert 'export DEVCTL_SKIP_PROVIDER_LOAD=1' in entrypoint
    assert "[[" not in entrypoint
    assert "BASH_SOURCE" not in entrypoint
    assert "source " not in entrypoint
    assert "set -euo pipefail" not in entrypoint


def assert_generated_wrapper_contract() -> None:
    wrappers = wrapper_files()
    entrypoint = wrappers["devctl"]
    assert entrypoint.startswith("#!/bin/sh\n")
    assert "set -eu" in entrypoint
    assert "select_python()" in entrypoint
    assert '"${DEVCTL_PYTHON:-}" python3 python' in entrypoint
    assert 'TOOL_ROOT="$ROOT/.xflow/ops/devctl"' in entrypoint
    assert 'export DEVCTL_TOOL_ROOT="$TOOL_ROOT"' in entrypoint
    assert 'DEVCTL_OPS_ROOT="$TOOL_ROOT"' in entrypoint
    assert 'exec "$PYTHON" -m xflow "$@"' in entrypoint
    assert 'export DEVCTL_SKIP_PROVIDER_LOAD=1' in entrypoint
    assert "[[" not in entrypoint
    assert "BASH_SOURCE" not in entrypoint
    assert "source " not in entrypoint
    assert "Python 3.9+" in wrappers["devctl.ps1"]
    assert "Python 3.10+" not in wrappers["devctl.ps1"]


def assert_posix_launcher_routes_core() -> None:
    env = test_env()
    env["DEVCTL_REPO_ROOT"] = str(OPS_ROOT)
    env["DEVCTL_SKIP_PROVIDER_LOAD"] = "1"
    result = subprocess.run(
        [str(OPS_ROOT / "devctl"), "check", "--help"],
        cwd=OPS_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert "dependencies" in result.stdout


def assert_check_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "--help"],
        [sys.executable, "-m", "xflow", "check", "--help"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=OPS_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr
        assert "dependencies" in result.stdout, (command, result.stdout)
        assert "commit-msg" in result.stdout, (command, result.stdout)
        if command[-2:] == ["check", "--help"]:
            assert "classification" in result.stdout, (command, result.stdout)

    for path in (OPS_ROOT / "README.md", OPS_ROOT / "help.txt"):
        text = path.read_text(encoding="utf-8")
        assert "devctl check dependencies --issue IK152D" in text
        assert "devctl check commit-msg --file .xflow/local/commit-message.txt --issue IK152D" in text
        assert "type(scope): 中文核心摘要[#Issue编号]" in text
        assert "active dependencies warn but do not block local development" in text
        assert "Gitee pull request merge is not supported" in text


def assert_unattended_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "unattended", "--help"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=OPS_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr
        for name in ("enable", "status", "disable"):
            assert name in result.stdout, (command, name, result.stdout)


def assert_task_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "task", "--help"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=OPS_ROOT, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 0, result.stderr
        for name in ("activate", "status", "list", "migrate-current"):
            assert name in result.stdout, (command, name, result.stdout)


def assert_contract_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "contract", "--help"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=OPS_ROOT, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 0, result.stderr
        for name in ("lint", "accept"):
            assert name in result.stdout, (command, name, result.stdout)


def assert_trace_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "trace", "--help"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=OPS_ROOT, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 0, result.stderr
        assert "check" in result.stdout, (command, result.stdout)


def assert_issue_workspace_migration_is_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "migrate", "--help"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=OPS_ROOT, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 0, result.stderr
        assert "issue-workspace" in result.stdout, (command, result.stdout)


def assert_cockpit_commands_are_discoverable() -> None:
    env = test_env()
    commands = (
        [sys.executable, "-m", "xflow", "--help"],
        [sys.executable, "-m", "xflow", "state", "--help"],
        [sys.executable, "-m", "xflow", "dev", "--help"],
        [sys.executable, "-m", "xflow", "dev", "docker", "--help"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=OPS_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr
    help_result = subprocess.run(
        [sys.executable, "-m", "xflow", "--help"],
        cwd=OPS_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for option in ("--profile", "--cockpit-root", "--repo"):
        assert option in help_result.stdout
    for command_name in ("state", "dev", "run", "pg", "playground"):
        assert command_name in help_result.stdout
    for path in (OPS_ROOT / "README.md", OPS_ROOT / "help.txt"):
        text = path.read_text(encoding="utf-8")
        assert "devctl [--profile PATH] [--cockpit-root PATH] [--repo NAME] state" in text
        assert "XFLOW_PROFILE" in text
        assert "python tests/cockpit-cli.py" in text


def assert_complete_command_contract_is_published() -> None:
    anchors = (
        "devctl task activate --issue IK3RR6",
        "devctl check classification --issue IK3RR6",
        "devctl contract lint --file docs/requirements/example/contract.yaml",
        "devctl contract accept --issue IK3RR6 --file docs/requirements/example/contract.yaml --objects <id,id,...>",
        "devctl contract diff --old <old.yaml> --new <new.yaml>",
        "devctl trace check --issue IK3RR6 --contract <contract.yaml> --matrix <traceability-matrix.yaml>",
        "devctl trace check --issue IK3RR6 --matrix <traceability-matrix.yaml>",
        "Omit --contract when immutable sealed contract-acceptance history is the authority.",
        "An explicit --contract is non-authoritative mechanical fail-closed evolution input.",
        "Non-acceptance routes still require --contract.",
        "devctl approval supersede-branch-start --issue <id> --approval-id <id>",
        "AI must never run branch-start supersede or supply its exact human confirmation.",
        ".xflow/issues/ is tracked by default",
        "lint does not approve semantic quality",
        "contract acceptance never supports unattended mode",
    )
    for path in (OPS_ROOT / "help.txt", OPS_ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        for anchor in anchors:
            assert anchor in text, (path, anchor)


def main() -> None:
    assert_no_legacy_run_command()
    assert_generated_wrapper_contract()
    assert_posix_launcher_routes_core()
    assert_check_commands_are_discoverable()
    assert_unattended_commands_are_discoverable()
    assert_task_commands_are_discoverable()
    assert_contract_commands_are_discoverable()
    assert_trace_commands_are_discoverable()
    assert_issue_workspace_migration_is_discoverable()
    assert_cockpit_commands_are_discoverable()
    assert_complete_command_contract_is_published()

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        assert_dependency_check_routing(root)
        assert_commit_message_check_routing(root)
        repo = root / "work"
        repo.mkdir()
        origin = root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        git(repo, "checkout", "-b", "main", "-q")
        write(repo / "README.md", "# Demo\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "init", "-q")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-u", "origin", "main", "-q")
        git(repo, "checkout", "-b", "feature/1-python-entrypoint", "-q")
        git(repo, "config", "--local", "devctl.issue", "1")
        git(repo, "config", "--local", "devctl.base", "main")

        issue_file = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
        write(
            issue_file,
            """<!-- xflow: issue-draft -->

## Background
Need Python entrypoint routing.

## Problem
Shell entrypoint may use old curl/jq scripts.

## Goal
Route remote-write commands through Python core.

## Scope
- Includes: issue and git-mr commands.

## Acceptance Criteria
- [ ] Bash entrypoint reaches Python skip-provider messages.

## Verification Plan
- python tests/entrypoint-routing.py
""",
        )
        approval(repo, "draft", "issue-create", issue_file)

        current_task = repo / ".xflow" / "current-task.md"
        write(
            current_task,
            """# XFlow Current Task

Issue: 1
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Verify entrypoint routing.

## Forbidden Actions
- Create PR before push approval.
""",
        )
        walkthrough = repo / ".xflow" / "issues" / "issue-1" / "walkthrough.md"
        write(walkthrough, "# Walkthrough\n\n- python tests/entrypoint-routing.py\n")

        mr_file = repo / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
        write(
            mr_file,
            """<!-- xflow: mr-draft -->

Closes #1

## Summary
- Route bash entrypoint to Python core.

## Test Plan
- python tests/entrypoint-routing.py

## Risk
- Low.

## Review Request
- Please review routing behavior.
""",
        )
        git(repo, "add", ".")
        git(repo, "commit", "-m", "test: add routing task artifacts", "-q")

        git(repo, "config", "--local", "devctl.issue", "draft")
        write(current_task, current_task.read_text(encoding="utf-8").replace("Issue: 1", "Issue: draft"))
        issue_result = run_posix_devctl(repo, "issue", "create", "Python routing", "--body-file", str(issue_file))
        assert "issue-create gate passed; provider skipped" in issue_result.stdout
        git(repo, "config", "--local", "devctl.issue", "1")
        write(current_task, current_task.read_text(encoding="utf-8").replace("Issue: draft", "Issue: 1"))

        approval(repo, "1", "git-push", walkthrough)
        push_result = run_devctl(repo, "git", "push", "--issue", "1", "--file", str(walkthrough))
        assert "pushed feature/1-python-entrypoint" in push_result.stdout

        approval(repo, "1", "git-mr", mr_file)
        mr_result = run_posix_devctl(repo, "git", "mr", "--body-file", str(mr_file), "--issue", "1", "--base", "main")
        assert "git-mr gate passed; provider skipped" in mr_result.stdout

        check_result = run_devctl(repo, "check", "mr-draft", "--issue", "1")
        assert "mr-draft check passed" in check_result.stdout

        pasted = repo / "route-image.png"
        pasted.write_bytes(b"\x89PNG\r\n\x1a\nroute-test")
        attachment_result = run_devctl(repo, "attachment", "add", "--issue", "draft", "--file", str(pasted), "--as", "image")
        assert "xflow-attachment://att-001" in attachment_result.stdout

    print("entrypoint routing ok")


if __name__ == "__main__":
    main()
