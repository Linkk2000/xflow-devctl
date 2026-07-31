from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

from .io import read_text
from .paths import normalized_issue


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
GAP_ANALYSIS_REQUIRED_SECTIONS = (
    "## User Original Statement",
    "## Clarified Problem Or Gap",
    "## Gap Analysis",
    "## Evidence",
    "## Evidence-Backed Findings",
    "## Scope Boundaries",
    "## Proposed Modification Plan",
    "## Acceptance Criteria",
    "## Human Recognition",
)
RESOLUTION_REPORT_REQUIRED_SECTIONS = (
    "## Source Problem Or Gap",
    "## Actual Changes",
    "## Evidence Index",
    "## Completion Verification",
    "## Closure Conclusion",
    "## AI Self-Review Result",
    "## Remaining Risks",
    "## Human Review Request",
)
RESOLUTION_CONCLUSIONS = {"resolved", "reduced", "blocked"}
GAP_FINDING_REQUIRED_SECTIONS = (
    "#### Finding Type",
    "#### Observation",
    "#### User Impact",
    "#### Evidence",
    "#### Analysis",
    "#### Proposed Change",
    "#### Acceptance",
    "#### Human Review",
)
COMPLETION_VERIFICATION_REQUIRED_SECTIONS = (
    "#### Verification Type",
    "#### Expected Result",
    "#### Evidence",
    "#### Actual Result",
    "#### Human Review",
)
SUBTASK_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SUBTASK_NAME_RE = re.compile(r"subtask-(\d{3})")
FINDING_BLOCK_RE = re.compile(r"(?ms)^### Finding F-(\d{3}):\s*(\S[^\n]*)\n(.*?)(?=^### Finding F-\d{3}:|\Z)")
COMPLETION_CRITERION_RE = re.compile(r"(?ms)^### Criterion C-(\d{3}):\s*(\S[^\n]*)\n(.*?)(?=^### Criterion C-\d{3}:|\Z)")
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
        ["git", "-C", str(repo_root), "config", "--worktree", "--get", key],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def check_current_task(repo_root: Path, issue: str | None = None, *, check_stale_pr: bool = True) -> None:
    from .task_state import check_task_binding
    from .bindings import fingerprint, git_path
    from .paths import active_task_pointer_file

    worktree = git_path(repo_root, "--show-toplevel")
    worktree_fingerprint = fingerprint("worktree", worktree)
    if active_task_pointer_file(repo_root, worktree_fingerprint).exists():
        check_task_binding(repo_root, issue)
        return
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
    if check_stale_pr and pr_number and state in PRE_PR_STATES:
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


def subsection_text(text: str, heading: str) -> str:
    pattern = rf"(?ms)^\s*{re.escape(heading)}\s*$\n(.*?)(?=^\s*####\s+|\Z)"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def required_subsections(text: str, sections: tuple[str, ...], label: str) -> dict[str, str]:
    found = {}
    for section in sections:
        if not has_section(text, section):
            raise ValueError(f"missing required {label} field: {section}")
        found[section] = subsection_text(text, section)
        if not found[section]:
            raise ValueError(f"empty required {label} field: {section}")
    return found


def required_sections(text: str, sections: tuple[str, ...], label: str) -> dict[str, str]:
    found = {}
    for section in sections:
        if not has_section(text, section):
            raise ValueError(f"missing required {label} section: {section}")
        found[section] = section_text(text, section)
        if not found[section]:
            raise ValueError(f"empty required {label} section: {section}")
    return found


def issue_file_path(repo_root: Path, issue: str, file_path: Path | None, filename: str) -> Path:
    current_issue_dir = issue_dir(repo_root, issue).resolve()
    return resolve_repo_path(repo_root, file_path or current_issue_dir / filename)


def require_issue_workspace_file(repo_root: Path, issue: str, path: Path, label: str) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    current_issue_dir = issue_dir(repo_root, issue).resolve()
    resolved = resolve_repo_path(repo_root, path)
    require_inside(resolved, current_issue_dir, f"{label} must stay under .xflow/issues/issue-<id>")
    if not resolved.is_file():
        raise ValueError(f"missing {label}: {resolved}")
    return current_issue_dir, resolved


def check_issue_local_evidence(current_issue_dir: Path, raw: str, label: str) -> list[Path]:
    if REMOTE_EVIDENCE_RE.search(raw):
        raise ValueError(f"{label} evidence must stay in the repository; do not use COS/OSS/http(s) links")
    entries = section_entries(raw)
    if not entries:
        raise ValueError(f"{label} evidence must reference at least one repository-local file")
    evidence_paths = []
    for entry in entries:
        if not entry or entry.startswith("#"):
            continue
        if re.search(r"(?i)^[a-z][a-z0-9+.-]*://", entry):
            raise ValueError(f"{label} evidence links must be repository-local paths")
        evidence_path = resolve_repo_path(current_issue_dir, Path(entry))
        require_inside(evidence_path, current_issue_dir, f"{label} evidence links must stay inside the issue directory")
        relative = evidence_path.relative_to(current_issue_dir)
        if not relative.parts or relative.parts[0] != "evidence":
            raise ValueError(f"{label} evidence links must stay under the issue evidence directory")
        if not evidence_path.exists():
            raise ValueError(f"{label} evidence file does not exist: {entry}")
        evidence_paths.append(evidence_path)
    return evidence_paths


def require_checklist_item(raw: str, label: str) -> None:
    if not re.search(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+\S", raw):
        raise ValueError(f"{label} must contain at least one reviewable checklist item")


def validate_evidence_bundle(
    current_issue_dir: Path,
    fields: dict[str, str],
    type_field: str,
    evidence_field: str,
    human_review_field: str,
    label: str,
) -> None:
    evidence_paths = check_issue_local_evidence(current_issue_dir, fields[evidence_field], label)
    require_checklist_item(fields[human_review_field], f"{label} Human Review")
    evidence_type = fields[type_field].strip().lower()
    if evidence_type not in {"ui", "non-ui"}:
        raise ValueError(f"{label} {type_field[5:]} must be ui or non-ui")
    if evidence_type != "ui":
        return

    relative_paths = [path.relative_to(current_issue_dir) for path in evidence_paths]
    has_screenshot = any("screenshots" in path.parts for path in relative_paths)
    has_dom = any("dom" in path.parts for path in relative_paths)
    if not has_screenshot or not has_dom:
        raise ValueError(f"{label} UI evidence must include both evidence/screenshots and evidence/dom artifacts")


def validate_evidence_blocks(
    current_issue_dir: Path,
    raw: str,
    pattern: re.Pattern[str],
    required_fields: tuple[str, ...],
    type_field: str,
    label: str,
) -> None:
    blocks = list(pattern.finditer(raw))
    if not blocks:
        raise ValueError(f"{label} must contain at least one numbered evidence bundle")
    for block in blocks:
        identifier, title, content = block.groups()
        if not title.strip():
            raise ValueError(f"{label} {identifier} must have a title")
        fields = required_subsections(content, required_fields, f"{label} {identifier}")
        validate_evidence_bundle(
            current_issue_dir,
            fields,
            type_field,
            "#### Evidence",
            "#### Human Review",
            f"{label} {identifier}",
        )


def has_unchecked_checklist_item(raw: str) -> bool:
    return re.search(r"(?m)^\s*[-*]\s+\[\s\]", raw) is not None


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


def check_gap_analysis(repo_root: Path, issue: str, file_path: Path | None = None) -> Path:
    current_issue_dir, path = require_issue_workspace_file(
        repo_root,
        issue,
        issue_file_path(repo_root, issue, file_path, "gap-analysis.md"),
        "gap analysis",
    )
    text = read_text(path)
    sections = required_sections(text, GAP_ANALYSIS_REQUIRED_SECTIONS, "gap-analysis")
    check_issue_local_evidence(current_issue_dir, sections["## Evidence"], "gap-analysis")
    validate_evidence_blocks(
        current_issue_dir,
        sections["## Evidence-Backed Findings"],
        FINDING_BLOCK_RE,
        GAP_FINDING_REQUIRED_SECTIONS,
        "#### Finding Type",
        "gap-analysis finding",
    )

    recognition = markdown_field(sections["## Human Recognition"], "Recognized").lower()
    if recognition != "yes":
        raise ValueError("gap-analysis Human Recognition must contain Recognized: yes before implementation")
    return path


def check_resolution_report(repo_root: Path, issue: str, file_path: Path | None = None) -> Path:
    current_issue_dir, path = require_issue_workspace_file(
        repo_root,
        issue,
        issue_file_path(repo_root, issue, file_path, "resolution-report.md"),
        "resolution report",
    )
    text = read_text(path)
    sections = required_sections(text, RESOLUTION_REPORT_REQUIRED_SECTIONS, "resolution-report")
    report_evidence = set(check_issue_local_evidence(current_issue_dir, sections["## Evidence Index"], "resolution-report"))
    validate_evidence_blocks(
        current_issue_dir,
        sections["## Completion Verification"],
        COMPLETION_CRITERION_RE,
        COMPLETION_VERIFICATION_REQUIRED_SECTIONS,
        "#### Verification Type",
        "resolution-report criterion",
    )

    conclusion_match = re.search(
        r"\b(resolved|reduced|blocked)\b\s*[:\uFF1A-]\s*(\S.+)",
        sections["## Closure Conclusion"],
        re.IGNORECASE,
    )
    if not conclusion_match or conclusion_match.group(1).lower() not in RESOLUTION_CONCLUSIONS:
        raise ValueError("resolution-report Closure Conclusion must be resolved, reduced, or blocked with a reason")
    conclusion = conclusion_match.group(1).lower()
    dependencies_path = current_issue_dir / "dependencies.yaml"
    if dependencies_path.is_file():
        from .dependencies import check_dependencies, check_dependency_closure

        dependency_result = check_dependencies(repo_root, issue, dependencies_path)
        closure_violations = check_dependency_closure(dependency_result, conclusion)
        if closure_violations:
            raise ValueError(
                "resolution-report dependency closure is inconsistent: " + "; ".join(closure_violations)
            )
    if conclusion in {"resolved", "reduced"} and has_unchecked_checklist_item(sections["## AI Self-Review Result"]):
        raise ValueError("resolved/reduced resolution-report must not contain unchecked AI self-review items")
    matrix_path = current_issue_dir / "traceability-matrix.yaml"
    if matrix_path.exists():
        from .traceability import check_traceability_resolution

        check_traceability_resolution(repo_root, issue, conclusion, report_evidence)
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
