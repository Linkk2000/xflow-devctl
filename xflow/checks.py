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
MAIN_STATES = {
    "S0_REQUEST",
    "S1_LOCAL_ISSUE_DRAFT",
    "G1_APPROVE_ISSUE_CREATE",
    "S2_REMOTE_ISSUE_CREATED",
    "G2_APPROVE_DEVELOPMENT_START",
    "S3_TASK_BRANCH_STARTED",
    "S4_TDD_AND_IMPLEMENTATION",
    "S5_LOCAL_VERIFICATION",
    "G3_APPROVE_RESULT",
    "S6_PREPARE_COMMIT_AND_MR_DRAFT",
    "G4_APPROVE_REMOTE_WRITE",
    "S7_PUSH_BRANCH",
    "G5_APPROVE_MR_CREATE",
    "S8_CREATE_REMOTE_MR",
    "S9_REMOTE_REVIEW_AND_CI",
    "G6_APPROVE_CLEANUP",
    "S10_DONE",
}
PRE_PR_STATES = {
    "S6_PREPARE_COMMIT_AND_MR_DRAFT",
    "G4_APPROVE_REMOTE_WRITE",
    "S7_PUSH_BRANCH",
    "G5_APPROVE_MR_CREATE",
    "S8_CREATE_REMOTE_MR",
}
REQUIRED_CURRENT_TASK_SECTIONS = ("## Allowed Actions", "## Forbidden Actions")


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


def markdown_field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def git_config(repo_root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--local", "--get", key],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def normalized_issue(value: str) -> str:
    value = value.strip()
    return value[1:] if value.startswith("#") else value


def check_current_task(repo_root: Path, issue: str | None = None) -> None:
    path = repo_root / ".xflow" / "current-task.md"
    if not path.is_file():
        raise ValueError(f"missing current task state file: {path}")

    text = read_text(path)
    state = markdown_field(text, "State")
    if not state:
        raise ValueError("missing required field in .xflow/current-task.md: State")
    if state not in MAIN_STATES:
        raise ValueError(f"unknown current task State: {state}")

    task_issue = markdown_field(text, "Issue")
    if issue and normalized_issue(task_issue) != normalized_issue(issue):
        raise ValueError(f"current task Issue mismatch: expected {issue}, found {task_issue or '<missing>'}")

    for section in REQUIRED_CURRENT_TASK_SECTIONS:
        if section not in text:
            raise ValueError(f"missing required section in .xflow/current-task.md: {section}")

    pr_number = git_config(repo_root, "devctl.pr")
    if pr_number and state in PRE_PR_STATES:
        raise ValueError(
            "stale current task state: local git config already records "
            f"devctl.pr={pr_number}, but State is still {state}; update to S9_REMOTE_REVIEW_AND_CI or later"
        )


def write_pr_state_update_suggestion(repo_root: Path, issue: str, pr_number: str, pr_url: str | None = None) -> Path:
    path = repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}" / "state-update-suggestion.md"
    url_line = f"PR URL: {pr_url}\n" if pr_url else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# State Update Suggestion

Issue: {normalized_issue(issue)}
PR: {pr_number}
{url_line}Suggested State: S9_REMOTE_REVIEW_AND_CI

## Suggested Local Update
- Update `.xflow/current-task.md` to `State: S9_REMOTE_REVIEW_AND_CI`.
- Record the PR number and URL in the task evidence if needed.
- Do not create a follow-up PR only to commit this local state note after the PR has already been merged.
""",
        encoding="utf-8",
        newline="\n",
    )
    return path


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
