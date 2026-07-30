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

        pointer = worktree_a / ".xflow" / "local" / "worktrees" / resolve_bindings(worktree_a).worktree / "active-task.json"
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert set(payload) == {"version", "repository", "worktree", "branch", "issue", "activatedAt"}
        assert payload["version"] == 1
        assert payload["issue"] == "101"

        git(worktree_a, "checkout", "-b", "feature/303-c", "-q")
        assert_value_error("active task branch mismatch", lambda: load_active_task(worktree_a))
        write_state(worktree_a, state("303", "feature/303-c"))
        run_devctl(worktree_a, "task", "activate", "--issue", "303")
        assert load_active_task(worktree_a).issue == "303"

        accepted = state("404", "feature/404-d", phase="accepted-design", approval="approvals/history/design.md")
        accepted_path = write_state(worktree_a, accepted)
        assert accepted_path.is_file()
        malformed = render_task_state(accepted).replace("Human Approval Ref: approvals/history/design.md", "Human Approval Ref: none")
        write(accepted_path, malformed)
        assert_value_error("Human Approval Ref", lambda: list_task_states(worktree_a))
        write(accepted_path, render_task_state(accepted))

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
        for old, new, expected in invalid_cases:
            write(invalid_path, template.replace(old, new))
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
        assert legacy.is_file()
        assert (worktree_a / ".xflow" / "issues" / "issue-LEGACY7" / "task-state.md").is_file()
        legacy.write_text("# XFlow Current Task\n\nIssue: ../invalid\n", encoding="utf-8", newline="\n")
        assert_value_error("current task Issue", lambda: migrate_legacy_current_task(worktree_a))
        assert not (worktree_a / ".xflow" / "issues" / "issue-invalid" / "task-state.md").exists()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")

        status = run_devctl(worktree_a, "task", "status")
        for field in ("repository", "worktree", "branch", "Issue", "Execution State", "Semantic Phase", "Classification", "Contract"):
            assert field in status.stdout
        listed = run_devctl(worktree_a, "task", "list")
        assert "#101" in listed.stdout
        migrated_output = run_devctl(worktree_a, "task", "migrate-current")
        assert "LEGACY7" in migrated_output.stdout

    print("task state ok")


if __name__ == "__main__":
    main()
