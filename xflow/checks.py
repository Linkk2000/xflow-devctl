from __future__ import annotations

import json
import fnmatch
import re
import subprocess
from pathlib import Path

from .io import read_text_strict
from .paths import issue_dir


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

SCOPE_DEFAULTS = {
    "review-only": {
        "allow": [
            ".xflow/issues/issue-<id>/**",
            ".xflow/local/**",
            "reviews/issue-<id>/**",
            "review/issue-<id>/**",
        ],
        "allow_supporting": [
            ".xflow/scope-policy.json",
            "AGENTS.md",
            ".cursorrules",
            ".cursor/rules/**",
        ],
        "protected_hints": [
            "manuscript/**",
            "paper/**",
            "*.tex",
            "*.bib",
        ],
    }
}

POST_PR_STATES = {
    "S9_REMOTE_REVIEW_AND_CI",
    "G6_APPROVE_CLEANUP",
    "S10_CLOSE_AND_ARCHIVE",
}

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


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower().replace("_", "-")
    if normalized not in SCOPE_DEFAULTS:
        raise ValueError(f"unknown scope mode: {mode}")
    return normalized


def _scope_policy_mode_key(mode: str) -> str:
    return mode.replace("-", "_")


def _expand_issue_pattern(pattern: str, issue: str) -> str:
    return pattern.replace("<id>", issue).replace("<issue>", issue)


def _load_scope_policy(repo_root: Path, mode: str, issue: str) -> dict[str, list[str]]:
    merged = {
        "allow": list(SCOPE_DEFAULTS[mode]["allow"]),
        "allow_supporting": list(SCOPE_DEFAULTS[mode]["allow_supporting"]),
        "protected_hints": list(SCOPE_DEFAULTS[mode]["protected_hints"]),
    }
    policy_file = repo_root / ".xflow" / "scope-policy.json"
    if policy_file.is_file():
        data = json.loads(policy_file.read_text(encoding="utf-8"))
        mode_policy = data.get(_scope_policy_mode_key(mode), data.get(mode, {}))
        if not isinstance(mode_policy, dict):
            raise ValueError(f"invalid scope policy for mode: {mode}")
        for key in ("allow", "allow_supporting", "protected_hints"):
            values = mode_policy.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"invalid scope policy field: {key}")
            merged[key].extend(values)
    return {
        key: [_expand_issue_pattern(pattern, issue) for pattern in values]
        for key, values in merged.items()
    }


def _git_changed_files(repo_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError("git command is required for scope checks") from exc
    if result.returncode != 0:
        raise ValueError(f"cannot inspect git status for {repo_root}: {result.stderr.strip()}")

    files: list[str] = []
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        path = raw_line[3:] if len(raw_line) > 3 else raw_line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = Path(path).as_posix()
        if normalized:
            files.append(normalized)
    return files


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def check_scope(repo_root: Path, issue: str, mode: str) -> None:
    mode = _normalize_mode(mode)
    repo_root = repo_root.resolve()
    policy = _load_scope_policy(repo_root, mode, issue)
    allowed = policy["allow"] + policy["allow_supporting"]
    changed = _git_changed_files(repo_root)
    blocked = [path for path in changed if not _matches_any(path, allowed)]
    if not blocked:
        return

    first = blocked[0]
    if _matches_any(first, policy["protected_hints"]):
        raise ValueError(
            f"file outside {mode} allowlist and matched protected hint: {first}. "
            "Switch task mode or extend .xflow/scope-policy.json after human review."
        )
    raise ValueError(
        f"file outside {mode} allowlist: {first}. "
        "Extend .xflow/scope-policy.json after human review if this path is intentional."
    )


def _simple_field(text: str, field: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _git_config_value(repo_root: Path, key: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--local", "--get", key],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError("git command is required for current-task checks") from exc
    if result.returncode not in (0, 1):
        raise ValueError(f"cannot inspect git config for {repo_root}: {result.stderr.strip()}")
    return result.stdout.strip()


def check_current_task(repo_root: Path, issue: str | None = None) -> None:
    repo_root = repo_root.resolve()
    current = repo_root / ".xflow" / "current-task.md"
    if not current.is_file():
        raise ValueError(f"missing current task file: {current}")

    text = read_text_strict(current)
    task_issue = _simple_field(text, "Issue")
    if issue and task_issue and task_issue != issue:
        raise ValueError(f"current-task issue mismatch: expected {issue}, got {task_issue}")

    state = _simple_field(text, "State")
    if not state:
        raise ValueError("current-task missing State field")

    pr_number = _git_config_value(repo_root, "devctl.pr")
    if pr_number and state not in POST_PR_STATES:
        raise ValueError(
            "stale current-task: PR metadata exists but current-task still describes a pre-PR or forbidden remote-write state. "
            "Update .xflow/current-task.md from .xflow/issues/issue-<id>/state-update-suggestion.md."
        )


def write_pr_state_update_suggestion(repo_root: Path, issue: str, pr_number: str, pr_url: str = "") -> Path:
    path = issue_dir(repo_root, issue) / "state-update-suggestion.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# State Update Suggestion",
        "",
        f"Issue: {issue}",
        f"PR: {pr_number}",
        "State: S9_REMOTE_REVIEW_AND_CI",
    ]
    if pr_url:
        lines.append(f"PR URL: {pr_url}")
    lines.extend(
        [
            "",
            "## Suggested Local Update",
            "- Update `.xflow/current-task.md` to `State: S9_REMOTE_REVIEW_AND_CI`.",
            "- Remove stale forbidden actions for push, PR creation, or MR creation.",
            "- Record the PR number and URL in local task artifacts if useful.",
            "",
            "## Approval Note",
            "- No second local approval is required for PR number/URL metadata writeback covered by `Approved Action: git-mr`.",
            "- After the PR is merged, seal the task board; do not open another PR only to update local checklist records.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
