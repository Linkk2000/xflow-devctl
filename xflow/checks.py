from __future__ import annotations

from pathlib import Path

from .io import read_text_strict


ACADEMIC_ISSUE_REQUIRED = [
    "# Academic Issue Draft",
    "Task Type:",
    "Target Branch:",
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


def require_template(path: Path, required: list[str]) -> None:
    if not path.is_file():
        raise ValueError(f"missing required file: {path}")
    text = read_text_strict(path)
    for needle in required:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}' in {path}")


def check_academic_issue(path: Path) -> None:
    require_template(path, ACADEMIC_ISSUE_REQUIRED)


def check_tdd_result(path: Path) -> None:
    require_template(path, TDD_RESULT_REQUIRED)
