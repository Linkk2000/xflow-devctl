from __future__ import annotations

import re
from pathlib import Path


ISSUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalized_issue(issue: str) -> str:
    value = str(issue).strip()
    if value.startswith("#"):
        value = value[1:].strip()
    if not value or value in {".", ".."}:
        raise ValueError("issue identifier must not be empty")
    if not ISSUE_ID_RE.fullmatch(value):
        raise ValueError(
            "issue identifier must contain only letters, numbers, dots, underscores, or dashes"
        )
    return value


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"


def default_issue_file(repo_root: Path, issue: str, filename: str) -> Path:
    return issue_dir(repo_root, issue) / filename


def default_approval_file(repo_root: Path, issue: str) -> Path:
    return issue_dir(repo_root, issue) / "approvals" / "local-review.md"


def task_state_file(repo_root: Path, issue: str) -> Path:
    return issue_dir(repo_root, issue) / "task-state.md"


def active_task_pointer_file(repo_root: Path, worktree_fingerprint: str) -> Path:
    return repo_root.resolve() / ".xflow" / "local" / "worktrees" / worktree_fingerprint / "active-task.json"
