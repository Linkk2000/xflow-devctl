from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .io import read_text_strict


ACADEMIC_ISSUE_REQUIRED = [
    "<!-- xflow: academic-issue-draft -->",
    "<!-- task-type:",
    "<!-- workflow-product-line:",
    "<!-- paper-base-branch:",
    "<!-- task-branch:",
    "## Background",
    "## Goal",
    "## Scope",
    "## Target Artifacts",
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
    "Claude Skill:",
    "Skill Source:",
    "Invocation:",
    "Input Files:",
    "Output File:",
    "## Objective",
    "## Constraints",
    "## Required Output Format",
    "## Human Review Requirement",
]

ACADEMICFORGE_CATALOG = Path(__file__).resolve().parent / "catalogs" / "academicforge-skills.txt"

ACADEMIC_MR_REQUIRED = [
    "<!-- xflow: academic-mr-draft -->",
    "<!-- issue:",
    "<!-- workflow-product-line:",
    "<!-- paper-base-branch:",
    "<!-- task-branch:",
    "## Summary",
    "## Evidence",
    "TDD Result:",
    "Local Review:",
    "## Verification",
    "## Risks",
    "## Review Request",
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


def load_academicforge_skill_names(catalog: Path = ACADEMICFORGE_CATALOG) -> set[str]:
    if not catalog.is_file():
        raise ValueError(f"missing AcademicForge skill catalog: {catalog}")
    names: set[str] = set()
    for raw_line in catalog.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        names.add(line)
    if not names:
        raise ValueError(f"empty AcademicForge skill catalog: {catalog}")
    return names


def _field_value(text: str, field: str) -> str:
    match = re.search(rf"(?m)^{re.escape(field)}\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _invocation_skill(invocation: str) -> str:
    match = re.match(r"^/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\s|$)", invocation.strip())
    if not match:
        raise ValueError("Invocation must start with an explicit Claude skill command such as /peer-review")
    return match.group(1)


def reject_obsolete_claude_skill_field(path: Path) -> None:
    text = read_text_strict(path)
    if re.search(r"(?m)^AcademicForge Skill:", text):
        raise ValueError("obsolete Claude skill field in task package: use Claude Skill, Skill Source, and Invocation")


def validate_claude_skill_invocation(path: Path) -> None:
    text = read_text_strict(path)
    reject_obsolete_claude_skill_field(path)
    skill = _field_value(text, "Claude Skill:")
    source = _field_value(text, "Skill Source:")
    invocation = _field_value(text, "Invocation:")
    invocation_skill = _invocation_skill(invocation)
    if skill != invocation_skill:
        raise ValueError(f"Claude Skill '{skill}' does not match Invocation '/{invocation_skill}'")

    if "academicforge" in source.lower():
        allowed = load_academicforge_skill_names()
        if skill not in allowed:
            raise ValueError(f"unknown AcademicForge skill '{skill}': update the catalog or choose a verified skill")


def claude_invocation_from_package(path: Path) -> str:
    text = read_text_strict(path)
    invocation = _field_value(text, "Invocation:")
    _invocation_skill(invocation)
    return invocation


def claude_skill_source_from_package(path: Path) -> tuple[str, str]:
    text = read_text_strict(path)
    skill = _field_value(text, "Claude Skill:")
    source = _field_value(text, "Skill Source:")
    return skill, source


def reject_obsolete_academic_target_branch(path: Path) -> None:
    text = read_text_strict(path)
    if re.search(r"(?m)^\s*Target Branch:\s*academic\s*$", text):
        raise ValueError(
            "obsolete academic branch field in "
            f"{path}: use Workflow Product Line, Paper Base Branch, and Task Branch"
        )


def reject_internal_publish_heading(path: Path, headings: tuple[str, ...]) -> None:
    text = read_text_strict(path)
    for heading in headings:
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text):
            raise ValueError(f"internal draft heading is not allowed in remote body: {heading}")


def check_academic_issue(path: Path) -> None:
    reject_obsolete_academic_target_branch(path)
    reject_internal_publish_heading(path, ("# Academic Issue Draft", "# Issue Draft"))
    require_template(path, ACADEMIC_ISSUE_REQUIRED)


def check_tdd_result(path: Path) -> None:
    require_template(path, TDD_RESULT_REQUIRED)


def check_claude_package(path: Path) -> None:
    reject_obsolete_claude_skill_field(path)
    require_template(path, CLAUDE_PACKAGE_REQUIRED)
    validate_claude_skill_invocation(path)


def check_academic_mr(path: Path) -> None:
    reject_obsolete_academic_target_branch(path)
    reject_internal_publish_heading(path, ("# MR Draft", "# PR Draft", "# Merge Request Draft"))
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
