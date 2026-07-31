from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import local_artifacts as local_artifacts_module
from xflow.bindings import resolve_bindings
from xflow.checks import check_current_task
from xflow.cli import current_task_issue
from xflow.bindings import git_path
from xflow.collaboration import repository_lock
from xflow.paths import active_task_pointer_file, legacy_active_task_pointer_file
from xflow.task_state import (
    TaskState,
    activate_task,
    check_task_binding,
    list_task_states,
    load_active_task,
    migrate_legacy_current_task,
    parse_task_state,
    render_task_state,
)


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def assert_value_error(expected: str, action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def state(issue: str, branch: str, *, phase: str = "classified", approval: str = "none") -> TaskState:
    return TaskState(
        issue=issue,
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase=phase,
        classification="capability-change",
        contract="example.contract.capability-name@0.1.0",
        contract_file="docs/requirements/example/contract.yaml",
        contract_change_required=True,
        branch=branch,
        base="main",
        allowed_actions=("clarify-contract", "prepare-verification"),
        forbidden_actions=("edit-implementation", "push", "create-mr"),
        human_gate="capability design acceptance required",
        human_approval_ref=approval,
    )


def write_state(repo_root: Path, value: TaskState) -> Path:
    path = repo_root / ".xflow" / "issues" / f"issue-{value.issue}" / "task-state.md"
    write(path, render_task_state(value))
    return path


def run_devctl_result(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "DEVCTL_SKIP_PROVIDER_LOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
        "XFLOW_COLLABORATION_LOCK_TIMEOUT": "0.2",
    }
    return subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_devctl(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = run_devctl_result(repo_root, *args)
    assert result.returncode == 0, result.stderr
    return result


def authority_file(repo_root: Path, issue: str) -> Path:
    bindings = resolve_bindings(repo_root)
    return (
        git_path(repo_root, "--git-common-dir")
        / "xflow"
        / "local"
        / "worktrees"
        / bindings.worktree
        / "issues"
        / f"issue-{issue}"
        / "authority.json"
    )


def v1_pointer(payload: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "repository": payload["repository"],
        "worktree": payload["worktree"],
        "branch": payload["branch"],
        "issue": payload["issue"],
        "activatedAt": payload["activatedAt"],
    }


def test_git_hook_devctl_reentry(root: Path) -> None:
    repo = root / "hook-reentry"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "feature/77-hook", "-q")
    write(repo / "README.md", "# Hook reentry\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize hook fixture", "-q")
    write_state(repo, state("77", "feature/77-hook"))
    activate_task(repo, "77")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "77")

    hook_result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks/pre-commit"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hook = Path(hook_result.stdout.strip())
    if not hook.is_absolute():
        hook = repo / hook
    python_executable = Path(sys.executable).as_posix()
    write(hook, f'#!/bin/sh\n"{python_executable}" -m xflow task status >/dev/null\n')
    hook.chmod(0o755)
    write(repo / "README.md", "# Hook reentry\n\nchanged\n")
    run_devctl(repo, "git", "commit-msg", "-a", "-c", "验证钩子重入")


def test_official_git_start_respects_closure_lock(root: Path) -> None:
    repo = root / "git-start-lock"
    origin = root / "git-start-origin.git"
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# Git mutation lock\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize mutation fixture", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")

    with repository_lock(repo):
        result = run_devctl_result(
            repo,
            "git",
            "start",
            "parallel-mutation",
            "--issue",
            "77",
            "--base",
            "main",
        )
    assert result.returncode == 1, result.stdout
    assert "another devctl process holds the repository collaboration lock" in result.stderr
    assert resolve_bindings(repo).branch == "main"

    with repository_lock(repo):
        result = run_devctl_result(repo, "git", "done", "--force", "--issue", "77")
    assert result.returncode == 1, result.stdout
    assert "another devctl process holds the repository collaboration lock" in result.stderr
    assert resolve_bindings(repo).branch == "main"


def initialized_repo(root: Path, name: str, branch: str) -> Path:
    repo = root / name
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", branch, "-q")
    write(repo / "README.md", f"# {name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize fixture", "-q")
    return repo


def legacy_source(issue: str, *, action: str = "clarify-contract") -> str:
    return (
        f"# XFlow Current Task\n\nIssue: {issue}\nState: S2_REMOTE_ISSUE_CREATED\n\n"
        f"## Allowed Actions\n- {action}\n\n## Forbidden Actions\n- push\n"
    )


def test_modern_pointer_blocks_legacy_migration(root: Path) -> None:
    repo = initialized_repo(root, "modern-migration", "feature/501-modern")
    write_state(repo, state("501", "feature/501-modern"))
    activate_task(repo, "501")
    bindings = resolve_bindings(repo)
    pointer = active_task_pointer_file(repo, bindings.worktree)
    original_pointer = pointer.read_bytes()
    authority = authority_file(repo, "501")
    authority.unlink()
    current_task = repo / ".xflow" / "current-task.md"
    write(current_task, legacy_source("501"))
    migrated_state = TaskState(
        issue="501",
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase="none",
        classification="implementation-gap",
        contract="legacy.current-task@0.1.0",
        contract_file=".xflow/current-task.md",
        contract_change_required=False,
        branch="feature/501-modern",
        base="main",
        allowed_actions=("clarify-contract",),
        forbidden_actions=("push",),
        human_gate="legacy task state requires human gate confirmation",
        human_approval_ref="none",
    )
    write_state(repo, migrated_state)

    assert_value_error("modern task authority cannot downgrade to legacy", lambda: migrate_legacy_current_task(repo))
    assert pointer.read_bytes() == original_pointer
    assert not authority.exists()

    mismatching = {
        **json.loads(original_pointer.decode("utf-8")),
        "issue": "999",
        "taskMode": "legacy",
        "contractId": "legacy.current-task",
        "contractVersion": "0.1.0",
        "contractFile": ".xflow/current-task.md",
    }
    write(pointer, json.dumps(mismatching, ensure_ascii=True, indent=2) + "\n")
    assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(repo))

    write_state(repo, state("501", "feature/501-modern"))
    write(pointer, original_pointer.decode("utf-8"))
    assert load_active_task(repo).issue == "501"
    old_pointer = legacy_active_task_pointer_file(repo, bindings.worktree)
    write(old_pointer, pointer.read_text(encoding="utf-8"))
    assert load_active_task(repo).issue == "501"
    assert not old_pointer.exists()


def test_legacy_authority_is_live_provenance(root: Path) -> None:
    repo = initialized_repo(root, "legacy-provenance", "feature/502-legacy")
    current_task = repo / ".xflow" / "current-task.md"
    original_source = legacy_source("502")
    write(current_task, original_source)
    migrated = migrate_legacy_current_task(repo)
    task_path = repo / ".xflow" / "issues" / "issue-502" / "task-state.md"
    original_state = task_path.read_text(encoding="utf-8")
    assert load_active_task(repo) == migrated

    current_task.unlink()
    assert_value_error("missing current task state file", lambda: load_active_task(repo))
    assert_value_error("missing current task state file", lambda: check_current_task(repo, "502"))
    write(current_task, original_source)
    write(current_task, legacy_source("502", action="verify"))
    assert_value_error("legacy task authority source provenance mismatch", lambda: load_active_task(repo))
    assert_value_error("legacy task authority source provenance mismatch", lambda: check_current_task(repo, "502"))
    assert_value_error("legacy task authority source provenance mismatch", lambda: current_task_issue(repo))
    write(current_task, original_source)

    write(task_path, original_state.replace("Base: main", "Base: develop"))
    assert_value_error("canonical migrated task-state", lambda: load_active_task(repo))
    assert_value_error("canonical migrated task-state", lambda: check_current_task(repo, "502"))
    assert_value_error("canonical migrated task-state", lambda: current_task_issue(repo))
    write(task_path, original_state)

    original_reader = local_artifacts_module._read_stable_bytes

    def mutate_source(*args: object, **kwargs: object) -> bytes:
        content = original_reader(*args, **kwargs)
        target = Path(args[1])
        if target == current_task.resolve():
            write(current_task, legacy_source("502", action="race"))
        return content

    try:
        with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_source):
            assert_value_error("changed while reading", lambda: load_active_task(repo))
    finally:
        write(current_task, original_source)


def test_legacy_fallback_is_stable_and_authority_aware(root: Path) -> None:
    repo = initialized_repo(root, "legacy-fallback", "feature/503-fallback")
    current_task = repo / ".xflow" / "current-task.md"
    write(current_task, legacy_source("STALE"))
    write_state(repo, state("503", "feature/503-fallback"))
    activate_task(repo, "503")
    bindings = resolve_bindings(repo)
    active_task_pointer_file(repo, bindings.worktree).unlink()
    (repo / ".xflow" / "issues" / "issue-503" / "task-state.md").unlink()

    assert_value_error("missing active task pointer", lambda: check_current_task(repo))
    assert_value_error("missing active task pointer", lambda: current_task_issue(repo))

    authority_file(repo, "503").unlink()
    hardlink = current_task.with_name("current-task-hardlink.md")
    os.link(current_task, hardlink)
    assert_value_error("exactly one filesystem link", lambda: check_current_task(repo))
    assert_value_error("exactly one filesystem link", lambda: current_task_issue(repo))
    hardlink.unlink()

    real_source = current_task.with_name("current-task-real.md")
    current_task.replace(real_source)
    try:
        os.symlink(real_source.name, current_task)
    except OSError:
        real_source.replace(current_task)
        with patch.object(local_artifacts_module, "_is_reparse_point", return_value=True):
            assert_value_error("symlink, junction, or reparse point", lambda: check_current_task(repo))
            assert_value_error("symlink, junction, or reparse point", lambda: current_task_issue(repo))
    else:
        assert_value_error("symlink, junction, or reparse point", lambda: check_current_task(repo))
        assert_value_error("symlink, junction, or reparse point", lambda: current_task_issue(repo))
        current_task.unlink()
        real_source.replace(current_task)

    original_reader = local_artifacts_module._read_stable_bytes

    def mutate_fallback(*args: object, **kwargs: object) -> bytes:
        content = original_reader(*args, **kwargs)
        target = Path(args[1])
        if target == current_task.resolve():
            write(current_task, legacy_source("RACE"))
        return content

    with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_fallback):
        assert_value_error("changed while reading", lambda: check_current_task(repo))
    write(current_task, legacy_source("STALE"))
    with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_fallback):
        assert_value_error("changed while reading", lambda: current_task_issue(repo))

def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree_a = root / "worktree-a"
        worktree_b = root / "worktree-b"
        git(root, "init", "-q", str(worktree_a))
        git(worktree_a, "config", "user.email", "test@example.com")
        git(worktree_a, "config", "user.name", "Test User")
        git(worktree_a, "checkout", "-b", "main", "-q")
        write(worktree_a / "README.md", "# Demo\n")
        git(worktree_a, "add", "README.md")
        git(worktree_a, "commit", "-m", "init", "-q")
        git(worktree_a, "checkout", "-b", "feature/101-a", "-q")
        git(worktree_a, "worktree", "add", "-b", "feature/IK3RR6-b", str(worktree_b), "main")

        write_state(worktree_a, state("101", "feature/101-a"))
        write_state(worktree_a, state("102", "feature/101-a"))
        write_state(worktree_a, state("IK3RR6", "feature/IK3RR6-b"))
        write_state(worktree_a, state("202", "feature/202-dependency"))
        write_state(worktree_b, state("IK3RR6", "feature/IK3RR6-b"))
        write(worktree_a / ".xflow" / "issues" / "issue-101" / "subtask-001" / "task-state.md", render_task_state(state("101", "feature/101-a")))

        activate_task(worktree_a, "101")
        activate_task(worktree_b, "IK3RR6")
        assert load_active_task(worktree_a).issue == "101"
        assert load_active_task(worktree_b).issue == "IK3RR6"
        check_current_task(worktree_a, "101")
        assert_value_error("active task Issue mismatch", lambda: check_task_binding(worktree_b, "101"))
        assert [item.issue for item in list_task_states(worktree_a)] == ["101", "102", "202", "IK3RR6"]

        pointer = active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert set(payload) == {
            "version", "repository", "worktree", "branch", "issue", "taskMode",
            "contractId", "contractVersion", "contractFile", "activatedAt",
        }
        assert payload["version"] == 2
        assert payload["issue"] == "101"
        assert payload["taskMode"] == "modern-contract"
        assert payload["contractId"] == "example.contract.capability-name"
        assert payload["contractVersion"] == "0.1.0"
        assert payload["contractFile"] == "docs/requirements/example/contract.yaml"
        pointer_bytes = pointer.read_text(encoding="utf-8")
        for field, replacement in (
            (
                '  "branch": "feature/101-a",',
                '  "branch": "feature/stale",\n  "branch": "feature/101-a",',
            ),
            (
                '  "taskMode": "modern-contract",',
                '  "taskMode": "legacy",\n  "taskMode": "modern-contract",',
            ),
        ):
            write(pointer, pointer_bytes.replace(field, replacement))
            assert_value_error("duplicate JSON key", lambda: load_active_task(worktree_a))
        write(pointer, pointer_bytes)
        authority = authority_file(worktree_a, "101")
        authority_payload = json.loads(authority.read_text(encoding="utf-8"))
        assert authority_payload["taskMode"] == "modern-contract"
        assert authority_payload["contractId"] == "example.contract.capability-name"
        authority_bytes = authority.read_text(encoding="utf-8")
        authority_link = authority.with_name("authority-hardlink.json")
        os.link(authority, authority_link)
        assert_value_error("exactly one filesystem link", lambda: activate_task(worktree_a, "101"))
        authority_link.unlink()
        write(authority, authority_bytes.replace("\n}", ',\n  "unexpected": true\n}'))
        assert_value_error("unexpected JSON fields", lambda: activate_task(worktree_a, "101"))
        write(authority, authority_bytes)
        write(
            authority,
            authority_bytes.replace(
                '  "taskMode": "modern-contract",',
                '  "taskMode": "legacy",\n  "taskMode": "modern-contract",',
            ),
        )
        assert_value_error("duplicate JSON key: taskMode", lambda: activate_task(worktree_a, "101"))
        write(authority, authority_bytes)

        sentinel = TaskState(
            **{
                **state("SENTINEL", "feature/101-a").__dict__,
                "contract": "legacy.current-task@0.1.0",
                "contract_file": ".xflow/current-task.md",
            }
        )
        write_state(worktree_a, sentinel)
        assert_value_error(
            "legacy task mode requires validated current-task migration",
            lambda: activate_task(worktree_a, "SENTINEL"),
        )

        activate_task(worktree_a, "102")
        downgraded = state("101", "feature/101-a")
        downgraded = TaskState(
            **{
                **downgraded.__dict__,
                "contract": "legacy.current-task@0.1.0",
                "contract_file": ".xflow/current-task.md",
            }
        )
        write_state(worktree_a, downgraded)
        assert_value_error("modern task authority cannot downgrade to legacy", lambda: activate_task(worktree_a, "101"))
        write_state(worktree_a, state("101", "feature/101-a"))
        activate_task(worktree_a, "101")

        pointer.unlink()
        state_101 = worktree_a / ".xflow" / "issues" / "issue-101" / "task-state.md"
        write_state(worktree_a, downgraded)
        assert_value_error("modern task authority cannot downgrade to legacy", lambda: activate_task(worktree_a, "101"))
        write_state(worktree_a, state("101", "feature/101-a"))
        assert_value_error("missing active task pointer", lambda: check_current_task(worktree_a, "101"))
        activate_task(worktree_a, "101")

        pointer_link = pointer.with_name("active-task-hardlink.json")
        os.link(pointer, pointer_link)
        assert_value_error("exactly one filesystem link", lambda: load_active_task(worktree_a))
        pointer_link.unlink()

        state_link = state_101.with_name("task-state-hardlink.md")
        os.link(state_101, state_link)
        assert_value_error("exactly one filesystem link", lambda: parse_task_state(state_101))
        state_link.unlink()

        old_payload = v1_pointer(payload)
        write(pointer, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        assert load_active_task(worktree_a).issue == "101"
        migrated_payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert migrated_payload["version"] == 2
        assert migrated_payload["taskMode"] == "modern-contract"

        old_location = legacy_active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        pointer.unlink()
        legacy_pointer_text = json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n"
        write(
            old_location,
            legacy_pointer_text.replace(
                '  "branch": "feature/101-a",',
                '  "branch": "feature/stale",\n  "branch": "feature/101-a",',
            ),
        )
        assert_value_error("duplicate JSON key: branch", lambda: load_active_task(worktree_a))
        write(old_location, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        check_current_task(worktree_a, "101")
        assert pointer.is_file()
        assert not old_location.exists()
        assert json.loads(pointer.read_text(encoding="utf-8"))["version"] == 2

        write(old_location, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        assert_value_error("conflicting active task pointers", lambda: load_active_task(worktree_a))
        old_location.unlink()

        pointer.unlink()
        stale_payload = {**old_payload, "branch": "feature/stale"}
        write(old_location, json.dumps(stale_payload, ensure_ascii=True, indent=2) + "\n")
        write(
            worktree_a / ".xflow" / "current-task.md",
            "# XFlow Current Task\n\nIssue: 101\nState: S2_REMOTE_ISSUE_CREATED\n\n"
            "## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n",
        )
        assert_value_error("active task branch mismatch", lambda: check_current_task(worktree_a, "101"))
        old_location.unlink()
        assert_value_error("active task pointer", lambda: check_current_task(worktree_a, "101"))
        activate_task(worktree_a, "101")

        git(worktree_a, "checkout", "-b", "feature/303-c", "-q")
        assert_value_error("active task branch mismatch", lambda: load_active_task(worktree_a))
        write_state(worktree_a, state("303", "feature/303-c"))
        run_devctl(worktree_a, "task", "activate", "--issue", "303")
        assert load_active_task(worktree_a).issue == "303"

        legacy_compat = worktree_a / ".xflow" / "current-task.md"
        write(legacy_compat, "# XFlow Current Task\n\nIssue: 303\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        git(worktree_a, "checkout", "--detach", "-q")
        assert_value_error("cannot bind XFlow task to detached HEAD", lambda: check_current_task(worktree_a, "303"))
        git(worktree_a, "checkout", "feature/303-c", "-q")

        accepted = state("404", "feature/404-d", phase="accepted-design", approval="approvals/history/design.md")
        accepted_path = write_state(worktree_a, accepted)
        assert accepted_path.is_file()
        malformed = render_task_state(accepted).replace("Human Approval Ref: approvals/history/design.md", "Human Approval Ref: none")
        write(accepted_path, malformed)
        assert_value_error("Human Approval Ref", lambda: list_task_states(worktree_a))
        write(accepted_path, render_task_state(accepted))
        assert_value_error("missing matching human contract acceptance", lambda: list_task_states(worktree_a))
        accepted_path.unlink()

        invalid_path = worktree_a / ".xflow" / "issues" / "issue-505" / "task-state.md"
        invalid_cases = (
            ("Execution State: S2_REMOTE_ISSUE_CREATED", "Execution State: INVALID", "unknown task-state Execution State"),
            ("Semantic Phase: classified", "Semantic Phase: INVALID", "unknown task-state Semantic Phase"),
            ("Classification: capability-change", "Classification: INVALID", "unknown task-state Classification"),
            ("Contract Change Required: yes", "Contract Change Required: maybe", "Contract Change Required must be yes or no"),
            ("Human Gate: capability design acceptance required", "Human Gate: ", "missing required task-state field: Human Gate"),
            ("- clarify-contract\n- prepare-verification", "", "Allowed Actions must not be empty"),
            ("- edit-implementation\n- push\n- create-mr", "", "Forbidden Actions must not be empty"),
        )
        template = render_task_state(state("505", "feature/505-e"))
        for invalid_issue in ("ABC-1", "A_B", "A.B"):
            assert_value_error("letters and numbers", lambda invalid_issue=invalid_issue: render_task_state(state(invalid_issue, "feature/505-e")))
            invalid_issue_path = worktree_a / ".xflow" / "issues" / f"issue-{invalid_issue}" / "task-state.md"
            write(invalid_issue_path, template.replace("Issue: 505", f"Issue: {invalid_issue}"))
            assert_value_error("letters and numbers", lambda invalid_issue_path=invalid_issue_path: parse_task_state(invalid_issue_path))
            invalid_issue_path.unlink()
        for old, new, expected in invalid_cases:
            write(invalid_path, template.replace(old, new))
            assert_value_error(expected, lambda: parse_task_state(invalid_path))
        ambiguous_cases = (
            (template.replace("Issue: 505", "Issue: 505\nIssue: 606"), "duplicate task-state field: Issue"),
            (
                template.replace("Human Approval Ref: none", "Human Approval Ref: none\nHuman Approval Ref: approvals/history/design.md"),
                "duplicate task-state field: Human Approval Ref",
            ),
            (
                template.replace("Contract: example.contract.capability-name@0.1.0\n", "").replace(
                    "## Allowed Actions\n", "## Allowed Actions\nContract: supplied-in-action-section\n"
                ),
                "unexpected non-list line in ## Allowed Actions",
            ),
            (template.replace("## Allowed Actions", "## Allowed Actions\n## Allowed Actions", 1), "duplicate task-state heading: ## Allowed Actions"),
            (template.replace("# XFlow Task State", "# XFlow Task State\n# XFlow Task State", 1), "duplicate task-state heading: # XFlow Task State"),
            (template.replace("- clarify-contract", "not-a-list-item\n- clarify-contract"), "unexpected non-list line in ## Allowed Actions"),
        )
        for malformed, expected in ambiguous_cases:
            write(invalid_path, malformed)
            assert_value_error(expected, lambda: parse_task_state(invalid_path))
        wrong_path = worktree_a / ".xflow" / "issues" / "issue-OTHER" / "task-state.md"
        write(wrong_path, template)
        assert_value_error("matching Issue directory", lambda: parse_task_state(wrong_path))
        outside_path = worktree_a / "issue-505" / "task-state.md"
        write(outside_path, template)
        assert_value_error("matching Issue directory", lambda: parse_task_state(outside_path))
        invalid_path.unlink()
        wrong_path.unlink()
        outside_path.unlink()

        legacy = worktree_a / ".xflow" / "current-task.md"
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        legacy_link = legacy.with_name("current-task-hardlink.md")
        os.link(legacy, legacy_link)
        assert_value_error("exactly one filesystem link", lambda: migrate_legacy_current_task(worktree_a))
        legacy_link.unlink()
        assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(worktree_a))
        active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).unlink()
        migrated = migrate_legacy_current_task(worktree_a)
        assert migrated.issue == "LEGACY7"
        assert load_active_task(worktree_a).issue == "LEGACY7"
        legacy_payload = json.loads(active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).read_text(encoding="utf-8"))
        assert legacy_payload["taskMode"] == "legacy"
        assert legacy_payload["contractId"] == "legacy.current-task"
        assert legacy_payload["contractVersion"] == "0.1.0"
        assert legacy_payload["contractFile"] == ".xflow/current-task.md"
        legacy_authority = authority_file(worktree_a, "LEGACY7")
        legacy_authority_payload = json.loads(legacy_authority.read_text(encoding="utf-8"))
        assert legacy_authority_payload["legacySourceDigest"]
        assert legacy_authority_payload["legacySourceFile"] == ".xflow/current-task.md"

        legacy_pointer_path = active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        legacy_pointer_path.unlink()
        legacy_authority.unlink()
        recovered = migrate_legacy_current_task(worktree_a)
        assert recovered == migrated
        recovered_pointer = legacy_pointer_path.read_bytes()
        recovered_authority = legacy_authority.read_bytes()
        assert migrate_legacy_current_task(worktree_a) == migrated
        assert legacy_pointer_path.read_bytes() == recovered_pointer
        assert legacy_authority.read_bytes() == recovered_authority
        write(
            active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree),
            json.dumps(v1_pointer(legacy_payload), ensure_ascii=True, indent=2) + "\n",
        )
        assert_value_error("cannot be migrated safely", lambda: load_active_task(worktree_a))
        assert_value_error("legacy task mode requires validated current-task migration", lambda: activate_task(worktree_a, "LEGACY7"))
        migrate_legacy_current_task(worktree_a)
        assert legacy.is_file()
        migrated_path = worktree_a / ".xflow" / "issues" / "issue-LEGACY7" / "task-state.md"
        assert migrated_path.is_file()
        migrated_bytes = migrated_path.read_bytes()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S5_LOCAL_VERIFICATION\n\n## Allowed Actions\n- verify\n\n## Forbidden Actions\n- push\n")
        assert_value_error("legacy task authority source provenance mismatch", lambda: migrate_legacy_current_task(worktree_a))
        assert migrated_path.read_bytes() == migrated_bytes
        legacy.write_text("# XFlow Current Task\n\nIssue: ../invalid\n", encoding="utf-8", newline="\n")
        assert_value_error("current task Issue", lambda: migrate_legacy_current_task(worktree_a))
        assert not (worktree_a / ".xflow" / "issues" / "issue-invalid" / "task-state.md").exists()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")

        status = run_devctl(worktree_a, "task", "status")
        for field in ("repository", "worktree", "branch", "Issue", "Execution State", "Semantic Phase", "Classification", "Contract"):
            assert field in status.stdout
        listed = run_devctl(worktree_a, "task", "list")
        assert "#101" in listed.stdout
        write(legacy, "# XFlow Current Task\n\nIssue: CLI7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(worktree_a))
        active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).unlink()
        migrated_output = run_devctl(worktree_a, "task", "migrate-current")
        assert "CLI7" in migrated_output.stdout
        assert load_active_task(worktree_a).issue == "CLI7"

        test_official_git_start_respects_closure_lock(root)
        test_git_hook_devctl_reentry(root)
        test_modern_pointer_blocks_legacy_migration(root)
        test_legacy_authority_is_live_provenance(root)
        test_legacy_fallback_is_stable_and_authority_aware(root)

    print("task state ok")


if __name__ == "__main__":
    main()
