from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval
from xflow.task_state import TaskState, activate_task, render_task_state


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


def task_state(issue: str, branch: str) -> TaskState:
    return TaskState(
        issue=issue,
        execution_state="S6_PREPARE_COMMIT_AND_MR_DRAFT",
        semantic_phase="classified",
        classification="capability-change",
        contract="example.contract.approval-binding@0.1.0",
        contract_file="docs/requirements/example/contract.yaml",
        contract_change_required=True,
        branch=branch,
        base="main",
        allowed_actions=("prepare-verification",),
        forbidden_actions=("edit-implementation",),
        human_gate="local human approval required",
        human_approval_ref="none",
    )


def activate(repo_root: Path, issue: str) -> None:
    branch = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    write(
        repo_root / ".xflow" / "issues" / f"issue-{issue}" / "task-state.md",
        render_task_state(task_state(issue, branch)),
    )
    activate_task(repo_root, issue)


def approve(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8", newline="\n")


def test_consumed_record(repo_root: Path, approved_file: Path) -> None:
    review = approval.prepare(repo_root, "202", "git-push", approved_file, reviewer="trusted local reviewer", force=True)
    approve(review)
    record = approval.record_consumed_approval(  # type: ignore[attr-defined]
        repo_root, "202", "git-push", approved_file, "local-review", "ignored", "success"
    )
    text = record.read_text(encoding="utf-8")
    assert record.parent == repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    assert "version: 0.1.0\nreusable: false\nsource: local-review\n" in text
    assert "issue: \"202\"\naction: git-push\n" in text
    assert "reviewerSummary: \"trusted local reviewer\"" in text
    assert "Approved: yes" not in text
    assert "GITHUB_TOKEN" not in text
    assert_value_error(
        "approval already consumed",
        lambda: approval.require_remote(repo_root, "git-push", approved_file, "202"),
    )

    unattended = approval.record_consumed_approval(  # type: ignore[attr-defined]
        repo_root, "202", "git-mr", approved_file, "unattended", "GITHUB_TOKEN=secret", "success"
    )
    unattended_text = unattended.read_text(encoding="utf-8")
    assert "reviewerSummary: task-scoped-unattended" in unattended_text
    assert "GITHUB_TOKEN" not in unattended_text
    assert "secret" not in unattended_text

    history = record.parent
    before = tuple(history.glob("*.yaml"))
    assert_value_error(
        "confirmed success",
        lambda: approval.record_consumed_approval(  # type: ignore[attr-defined]
            repo_root, "202", "git-push", approved_file, "local-review", "ignored", "failure"
        ),
    )
    assert tuple(history.glob("*.yaml")) == before

    class FrozenDatetime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            return datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)

    with patch.object(approval, "datetime", FrozenDatetime):
        approval.record_consumed_approval(  # type: ignore[attr-defined]
            repo_root, "202", "issue-comment", approved_file, "unattended", "ignored", "success"
        )
        assert_value_error(
            "history collision",
            lambda: approval.record_consumed_approval(  # type: ignore[attr-defined]
                repo_root, "202", "issue-comment", approved_file, "unattended", "ignored", "success"
            ),
        )

    unsafe_review = approval.prepare(
        repo_root, "202", "issue-create", approved_file, reviewer="GITHUB_TOKEN=secret", force=True
    )
    approve(unsafe_review)
    assert_value_error(
        "credential-like text",
        lambda: approval.record_consumed_approval(  # type: ignore[attr-defined]
            repo_root, "202", "issue-create", approved_file, "local-review", "ignored", "success"
        ),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree_a = root / "worktree-a"
        worktree_b = root / "worktree-b"
        repository_c = root / "repository-c"
        git(root, "init", "-q", str(worktree_a))
        git(worktree_a, "config", "user.email", "test@example.com")
        git(worktree_a, "config", "user.name", "Test User")
        git(worktree_a, "checkout", "-b", "main", "-q")
        write(worktree_a / "README.md", "# Demo\n")
        git(worktree_a, "add", "README.md")
        git(worktree_a, "commit", "-m", "init", "-q")
        git(worktree_a, "checkout", "-b", "feature/202-a", "-q")
        git(worktree_a, "worktree", "add", "-b", "feature/202-b", str(worktree_b), "main")

        a_file = worktree_a / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        b_file = worktree_b / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        write(a_file, "# Walkthrough\n\nApproved artifact.\n")
        write(b_file, a_file.read_text(encoding="utf-8"))
        activate(worktree_a, "202")
        activate(worktree_b, "202")
        review_a = approval.prepare(worktree_a, "202", "git-push", a_file, reviewer="human reviewer")
        approve(review_a)
        review_b = worktree_b / ".xflow" / "issues" / "issue-202" / "approvals" / "local-review.md"
        review_b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_b)

        assert_value_error(
            "approval worktree mismatch",
            lambda: approval.require_remote(worktree_b, "git-push", b_file, "202"),
        )

        git(worktree_a, "checkout", "-b", "feature/202-switched", "-q")
        assert_value_error(
            "approval branch mismatch",
            lambda: approval.require_remote(worktree_a, "git-push", a_file, "202"),
        )
        git(worktree_a, "checkout", "feature/202-a", "-q")

        activate(worktree_a, "303")
        review_303 = worktree_a / ".xflow" / "issues" / "issue-303" / "approvals" / "local-review.md"
        review_303.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_303)
        assert_value_error(
            "approval Issue mismatch",
            lambda: approval.require_remote(worktree_a, "git-push", a_file, "303"),
        )

        git(root, "init", "-q", str(repository_c))
        git(repository_c, "config", "user.email", "test@example.com")
        git(repository_c, "config", "user.name", "Test User")
        git(repository_c, "checkout", "-b", "feature/202-a", "-q")
        write(repository_c / "README.md", "# Demo\n")
        git(repository_c, "add", "README.md")
        git(repository_c, "commit", "-m", "init", "-q")
        c_file = repository_c / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        write(c_file, a_file.read_text(encoding="utf-8"))
        activate(repository_c, "202")
        review_c = repository_c / ".xflow" / "issues" / "issue-202" / "approvals" / "local-review.md"
        review_c.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_c)
        assert_value_error(
            "approval repository mismatch",
            lambda: approval.require_remote(repository_c, "git-push", c_file, "202"),
        )

        activate(worktree_a, "202")
        assert_value_error(
            "action mismatch: expected git-mr, got git-push",
            lambda: approval.require_remote(worktree_a, "git-mr", a_file, "202"),
        )
        assert_value_error(
            "contract-acceptance is not eligible for unattended mode",
            lambda: approval.require_remote_or_unattended(
                worktree_a, "contract-acceptance", a_file, "202", request_unattended=True
            ),
        )
        test_consumed_record(worktree_a, a_file)

    print("approval binding ok")


if __name__ == "__main__":
    main()
