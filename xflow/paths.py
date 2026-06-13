from __future__ import annotations

from pathlib import Path


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / f"issue-{issue}"


def default_issue_file(repo_root: Path, issue: str, filename: str) -> Path:
    return issue_dir(repo_root, issue) / filename
