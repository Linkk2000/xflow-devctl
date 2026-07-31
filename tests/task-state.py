from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.bindings import resolve_bindings
from xflow.checks import check_current_task
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


def run_devctl(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "DEVCTL_SKIP_PROVIDER_LOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
        "XFLOW_COLLABORATION_LOCK_TIMEOUT": "0.2",
    }
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    return result


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
        assert [item.issue for item in list_task_states(worktree_a)] == ["101", "202", "IK3RR6"]

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

        old_payload = v1_pointer(payload)
        write(pointer, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        assert load_active_task(worktree_a).issue == "101"
        migrated_payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert migrated_payload["version"] == 2
        assert migrated_payload["taskMode"] == "modern-contract"

        old_location = legacy_active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        pointer.unlink()
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
        migrated = migrate_legacy_current_task(worktree_a)
        assert migrated.issue == "LEGACY7"
        assert load_active_task(worktree_a).issue == "LEGACY7"
        legacy_payload = json.loads(active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).read_text(encoding="utf-8"))
        assert legacy_payload["taskMode"] == "legacy"
        assert legacy_payload["contractId"] == "legacy.current-task"
        assert legacy_payload["contractVersion"] == "0.1.0"
        assert legacy_payload["contractFile"] == ".xflow/current-task.md"
        write(
            active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree),
            json.dumps(v1_pointer(legacy_payload), ensure_ascii=True, indent=2) + "\n",
        )
        assert_value_error("cannot be migrated safely", lambda: load_active_task(worktree_a))
        activate_task(worktree_a, "LEGACY7")
        assert legacy.is_file()
        migrated_path = worktree_a / ".xflow" / "issues" / "issue-LEGACY7" / "task-state.md"
        assert migrated_path.is_file()
        migrated_bytes = migrated_path.read_bytes()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S5_LOCAL_VERIFICATION\n\n## Allowed Actions\n- verify\n\n## Forbidden Actions\n- push\n")
        assert_value_error("task-state already exists", lambda: migrate_legacy_current_task(worktree_a))
        assert migrated_path.read_bytes() == migrated_bytes
        legacy.write_text("# XFlow Current Task\n\nIssue: ../invalid\n", encoding="utf-8", newline="\n")
        assert_value_error("current task Issue", lambda: migrate_legacy_current_task(worktree_a))
        assert not (worktree_a / ".xflow" / "issues" / "issue-invalid" / "task-state.md").exists()
        write(legacy, "# XFlow Current Task\n\nIssue: CLI7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")

        status = run_devctl(worktree_a, "task", "status")
        for field in ("repository", "worktree", "branch", "Issue", "Execution State", "Semantic Phase", "Classification", "Contract"):
            assert field in status.stdout
        listed = run_devctl(worktree_a, "task", "list")
        assert "#101" in listed.stdout
        migrated_output = run_devctl(worktree_a, "task", "migrate-current")
        assert "CLI7" in migrated_output.stdout
        assert load_active_task(worktree_a).issue == "CLI7"

        test_git_hook_devctl_reentry(root)

    print("task state ok")


if __name__ == "__main__":
    main()
