from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

from .io import read_text


ISSUE_REQUIRED = (
    "<!-- xflow: issue-draft -->",
    "## Background",
    "## Problem",
    "## Goal",
    "## Scope",
    "## Acceptance Criteria",
    "## Verification Plan",
)
MR_REQUIRED = (
    "<!-- xflow: mr-draft -->",
    "## Summary",
    "## Test Plan",
    "## Risk",
    "## Review Request",
)
SUBMODULES = (".xflow/ops/devctl", ".xflow/ops/workflow")
BYPRODUCT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov"}
BYPRODUCT_FILES = {".coverage", "Thumbs.db", ".DS_Store"}
BYPRODUCT_SUFFIXES = {".pyc", ".pyo", ".tmp", ".log"}


def reject_publish_heading(path: Path, headings: tuple[str, ...]) -> None:
    text = read_text(path)
    for heading in headings:
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text):
            raise ValueError(f"internal draft heading is not allowed in remote body: {heading}")


def require_template(path: Path, needles: tuple[str, ...]) -> None:
    if not path.is_file():
        raise ValueError(f"missing required file: {path}")
    text = read_text(path)
    for needle in needles:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}' in {path}")


def check_issue_draft(path: Path) -> None:
    reject_publish_heading(path, ("# Issue Draft", "# Academic Issue Draft"))
    require_template(path, ISSUE_REQUIRED)


def check_mr_draft(path: Path) -> None:
    reject_publish_heading(path, ("# MR Draft", "# PR Draft", "# Merge Request Draft"))
    require_template(path, MR_REQUIRED)


def gitmodules_has_ignore(repo_root: Path, submodule: str) -> bool:
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return False
    current = False
    for raw in gitmodules.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[submodule "):
            current = False
        if line == f"path = {submodule}":
            current = True
        elif current and line == "ignore = untracked":
            return True
    return False


def find_byproducts(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_dir() and path.name in BYPRODUCT_DIRS:
            found.append(path)
        elif path.is_file() and (path.name in BYPRODUCT_FILES or path.suffix in BYPRODUCT_SUFFIXES):
            found.append(path)
    return found


def tracked_submodule_changes(path: Path) -> list[str]:
    if not (path / ".git").exists():
        return []
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot inspect git status for {path}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line and not line.startswith("??")]


def check_submodule_hygiene(repo_root: Path) -> None:
    repo_root = repo_root.resolve()
    for submodule in SUBMODULES:
        path = repo_root / submodule
        if not path.exists():
            continue
        if not gitmodules_has_ignore(repo_root, submodule):
            raise ValueError(f"missing ignore = untracked for {submodule} in .gitmodules")
        tracked = tracked_submodule_changes(path)
        if tracked:
            raise ValueError(f"tracked changes in {submodule}: {tracked[0]}")
        byproducts = find_byproducts(path)
        if byproducts:
            raise ValueError(f"byproduct in {submodule}: {byproducts[0].relative_to(path).as_posix()}")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
