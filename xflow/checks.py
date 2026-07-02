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
SUBTASK_REQUIRED_SECTIONS = (
    "## Source",
    "## Purpose",
    "## Implementation Plan",
    "## Evidence",
    "## AI Review Checkpoints",
    "## Human Review Checkpoints",
    "## Conclusion",
)
SUBTASK_CONCLUSIONS = {"success", "blocked", "superseded-by-human"}
SUBTASK_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SUBTASK_NAME_RE = re.compile(r"subtask-(\d{3})")
REMOTE_EVIDENCE_RE = re.compile(r"(?i)(https?://|oss://|cos://|aliyuncs\.com|myqcloud\.com|qcloudcos|cos\.)")
ISSUE_WORKSPACE_REMOTE_RE = re.compile(
    r"(?i)(oss://|cos://|aliyuncs\.com|myqcloud\.com|qcloudcos|/xflow/issues/issue-[^\s)\"']+/attachments/)"
)
PUBLISHED_URL_RE = re.compile(r'"publishedUrl"\s*:\s*"[^"]+"')
ISSUE_WORKSPACE_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".log"}


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


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"


def resolve_repo_path(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def require_inside(path: Path, parent: Path, message: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise ValueError(message) from exc


def section_text(text: str, heading: str) -> str:
    pattern = rf"(?ms)^\s*{re.escape(heading)}\s*$\n(.*?)(?=^\s*##\s+|\Z)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def has_section(text: str, heading: str) -> bool:
    return re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text) is not None


def first_section_entry(raw: str) -> str:
    for line in raw.splitlines():
        item = line.strip()
        if not item:
            continue
        item = re.sub(r"^[-*]\s+", "", item).strip()
        if not item:
            continue
        link = SUBTASK_LINK_RE.search(item)
        if link:
            item = link.group(1).strip()
        if item.startswith("`") and "`" in item[1:]:
            item = item.strip("`")
        return item
    return ""


def section_entries(raw: str) -> list[str]:
    entries = []
    for line in raw.splitlines():
        item = line.strip()
        if not item:
            continue
        item = re.sub(r"^[-*]\s+", "", item).strip()
        item = re.sub(r"^\[[ xX]\]\s+", "", item).strip()
        if not item:
            continue
        link = SUBTASK_LINK_RE.search(item)
        if link:
            item = link.group(1).strip()
        if item.startswith("`") and "`" in item[1:]:
            item = item.strip("`")
        entries.append(item)
    return entries


def check_subtask(repo_root: Path, issue: str, subtask_path: Path | None = None) -> Path:
    repo_root = repo_root.resolve()
    current_issue_dir = issue_dir(repo_root, issue).resolve()
    path = resolve_repo_path(repo_root, subtask_path or current_issue_dir / "subtask-001")
    if path.parent.resolve() != current_issue_dir:
        raise ValueError(f"subtask path must be directly under {current_issue_dir}")
    name_match = SUBTASK_NAME_RE.fullmatch(path.name)
    if not name_match or int(name_match.group(1)) == 0:
        raise ValueError("subtask directory must be named subtask-001, subtask-002, ...")

    readme = path / "README.md"
    if not readme.is_file():
        raise ValueError(f"missing subtask README: {readme}")
    text = read_text(readme)
    sections = {}
    for section in SUBTASK_REQUIRED_SECTIONS:
        if not has_section(text, section):
            raise ValueError(f"missing required subtask README section: {section}")
        sections[section] = section_text(text, section)
        if not sections[section]:
            raise ValueError(f"empty required subtask README section: {section}")

    source = first_section_entry(sections["## Source"])
    if not source:
        raise ValueError("subtask README Source must reference a file in the same issue directory")
    if re.search(r"(?i)^[a-z][a-z0-9+.-]*://", source):
        raise ValueError("subtask Source must be a local issue file, not a URL")
    source_path = resolve_repo_path(current_issue_dir, Path(source))
    require_inside(source_path, current_issue_dir, "subtask Source must stay inside the same issue directory")
    source_relative = source_path.relative_to(current_issue_dir)
    if source_relative.parts and SUBTASK_NAME_RE.fullmatch(source_relative.parts[0]):
        raise ValueError("subtask Source must reference an issue-level file, not another subtask file")
    if not source_path.is_file():
        raise ValueError(f"subtask Source file does not exist: {source}")

    evidence = sections["## Evidence"]
    if REMOTE_EVIDENCE_RE.search(evidence):
        raise ValueError("subtask evidence must stay in the repository; do not use COS/OSS/http(s) links")
    evidence_entries = section_entries(evidence)
    if not evidence_entries:
        raise ValueError("subtask Evidence must reference at least one repository-local file")
    for link in evidence_entries:
        link = link.strip()
        if not link or link.startswith("#"):
            continue
        if re.search(r"(?i)^[a-z][a-z0-9+.-]*://", link):
            raise ValueError("subtask evidence links must be repository-local paths")
        evidence_path = resolve_repo_path(path, Path(link))
        require_inside(evidence_path, path, "subtask evidence links must stay inside the subtask directory")
        evidence_relative = evidence_path.relative_to(path)
        if not evidence_relative.parts or evidence_relative.parts[0] != "evidence":
            raise ValueError("subtask evidence links must stay under the subtask evidence directory")
        if not evidence_path.exists():
            raise ValueError(f"subtask evidence file does not exist: {link}")

    conclusion = sections["## Conclusion"]
    match = re.search(r"\b(success|blocked|superseded-by-human)\b\s*[:\uFF1A-]\s*(\S.+)", conclusion, re.IGNORECASE)
    if not match or match.group(1).lower() not in SUBTASK_CONCLUSIONS:
        raise ValueError("subtask Conclusion must be success, blocked, or superseded-by-human with a reason")
    return path


def check_issue_evidence(repo_root: Path, issue: str, publish_root: Path | None = None) -> Path:
    repo_root = repo_root.resolve()
    current_issue_dir = issue_dir(repo_root, issue).resolve()
    if not current_issue_dir.is_dir():
        raise ValueError(f"missing issue workspace: {current_issue_dir}")
    if publish_root is not None:
        publish_path = resolve_repo_path(repo_root, publish_root)
        expected_publish_root = repo_root / ".xflow" / "publish" / "issues" / f"issue-{normalized_issue(issue)}"
        require_inside(publish_path, expected_publish_root, "publish files must stay under .xflow/publish/issues/issue-<id>")

    for file_path in current_issue_dir.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in ISSUE_WORKSPACE_TEXT_SUFFIXES:
            continue
        text = read_text(file_path)
        if ISSUE_WORKSPACE_REMOTE_RE.search(text):
            raise ValueError(f"issue workspace must not contain COS/OSS/published attachment URLs: {file_path}")
        if PUBLISHED_URL_RE.search(text):
            raise ValueError(f"issue workspace must not contain publishedUrl values: {file_path}")
    return current_issue_dir


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
