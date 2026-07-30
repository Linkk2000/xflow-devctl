from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitBindings:
    repository: str
    worktree: str
    branch: str


def git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def git_path(repo_root: Path, argument: str) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute", argument],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"cannot resolve Git {argument}: {detail or 'unknown Git error'}")
    return Path(value).resolve()


def _canonical_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def fingerprint(label: str, path: Path) -> str:
    value = f"{label}\0{_canonical_path(path)}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def resolve_bindings(repo_root: Path) -> GitBindings:
    common_dir = git_path(repo_root, "--git-common-dir")
    worktree = git_path(repo_root, "--show-toplevel")
    branch = git_output(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise ValueError("cannot bind XFlow task to detached HEAD")
    return GitBindings(
        repository=fingerprint("repository", common_dir),
        worktree=fingerprint("worktree", worktree),
        branch=branch,
    )
