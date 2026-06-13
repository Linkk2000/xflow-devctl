from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .io import read_text_strict


ACADEMIC_ISSUE_REQUIRED = [
    "# Academic Issue Draft",
    "Task Type:",
    "Workflow Product Line:",
    "Paper Base Branch:",
    "Task Branch:",
    "Target Artifacts:",
    "## Background",
    "## Goal",
    "## Scope",
    "## Acceptance Criteria",
    "## Verification Plan",
    "## Human Review Gate",
]

TDD_RESULT_REQUIRED = [
    "# TDD Result",
    "Issue:",
    "Branch:",
    "Verified At:",
    "Executor:",
    "## Verification Scope",
    "## Commands",
    "## Results",
    "## Risks",
    "## Human Review Entry",
]

CLAUDE_PACKAGE_REQUIRED = [
    "# Claude Task Package",
    "Issue:",
    "AcademicForge Skill:",
    "Input Files:",
    "Output File:",
    "## Objective",
    "## Constraints",
    "## Required Output Format",
    "## Human Review Requirement",
]

ACADEMIC_MR_REQUIRED = [
    "# MR Draft",
    "Issue:",
    "Workflow Product Line:",
    "Paper Base Branch:",
    "Task Branch:",
    "## Summary",
    "## Evidence",
    "TDD Result:",
    "Local Review:",
    "## Remote Actions Requested",
]

OPS_SUBMODULES = (".xflow/ops/devctl", ".xflow/ops/workflow")

BYPRODUCT_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
}

BYPRODUCT_FILE_NAMES = {
    ".coverage",
    "Thumbs.db",
    ".DS_Store",
}

BYPRODUCT_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".tmp",
    ".log",
}


def require_template(path: Path, required: list[str]) -> None:
    if not path.is_file():
        raise ValueError(f"missing required file: {path}")
    text = read_text_strict(path)
    for needle in required:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}' in {path}")


def reject_obsolete_academic_target_branch(path: Path) -> None:
    text = read_text_strict(path)
    if re.search(r"(?m)^\s*Target Branch:\s*academic\s*$", text):
        raise ValueError(
            "obsolete academic branch field in "
            f"{path}: use Workflow Product Line, Paper Base Branch, and Task Branch"
        )


def check_academic_issue(path: Path) -> None:
    reject_obsolete_academic_target_branch(path)
    require_template(path, ACADEMIC_ISSUE_REQUIRED)


def check_tdd_result(path: Path) -> None:
    require_template(path, TDD_RESULT_REQUIRED)


def check_claude_package(path: Path) -> None:
    require_template(path, CLAUDE_PACKAGE_REQUIRED)


def check_academic_mr(path: Path) -> None:
    reject_obsolete_academic_target_branch(path)
    require_template(path, ACADEMIC_MR_REQUIRED)


def _run_git_status(path: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError("git command is required for submodule hygiene checks") from exc
    if result.returncode != 0:
        raise ValueError(f"cannot inspect git status for {path}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _is_tracked_status(line: str) -> bool:
    return not line.startswith("??")


def _find_byproducts(root: Path) -> list[Path]:
    byproducts: list[Path] = []
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_dir() and path.name in BYPRODUCT_DIR_NAMES:
            byproducts.append(path)
            continue
        if path.is_file() and (path.name in BYPRODUCT_FILE_NAMES or path.suffix in BYPRODUCT_SUFFIXES):
            byproducts.append(path)
    return byproducts


def _gitmodules_has_ignore_untracked(repo_root: Path, submodule_path: str) -> bool:
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return False

    current_section = False
    for raw_line in gitmodules.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[submodule "):
            current_section = False
            continue
        if line == f"path = {submodule_path}":
            current_section = True
            continue
        if current_section and line == "ignore = untracked":
            return True
    return False


def check_submodule_hygiene(repo_root: Path) -> None:
    repo_root = repo_root.resolve()
    for submodule in OPS_SUBMODULES:
        path = repo_root / submodule
        if not path.exists():
            continue

        if not _gitmodules_has_ignore_untracked(repo_root, submodule):
            raise ValueError(f"missing ignore = untracked for {submodule} in .gitmodules")

        status_lines = _run_git_status(path)
        tracked = [line for line in status_lines if _is_tracked_status(line)]
        if tracked:
            raise ValueError(f"tracked changes in {submodule}: {tracked[0]}")

        byproducts = _find_byproducts(path)
        if byproducts:
            relative = byproducts[0].relative_to(path)
            raise ValueError(f"byproduct in {submodule}: {relative}")
