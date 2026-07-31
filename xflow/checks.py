from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

from .io import read_text
from .local_artifacts import (
    MAX_IMAGE_EVIDENCE_BYTES,
    MAX_STRUCTURED_EVIDENCE_BYTES,
    MAX_TEXT_ARTIFACT_BYTES,
    StableFileSnapshot,
    capture_stable_file,
    contains_forbidden_object_storage_reference,
    contains_forbidden_remote_reference,
    safe_relative_reference,
)
from .collaboration import repository_locked
from .paths import normalized_issue
from .project_config import require_safe_repo_path


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
UI_EVIDENCE_VERIFICATION_TYPES = {"browser", "product-integration", "ui", "visual"}
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
    _validate_issue_draft_text(read_text(path), path)


def _validate_issue_draft_text(text: str, path: Path) -> None:
    for heading in ("# Issue Draft", "# Academic Issue Draft"):
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text):
            raise ValueError(f"internal draft heading is not allowed in remote body: {heading}")
    for needle in ISSUE_REQUIRED:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}' in {path}")


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


def issue_local_evidence_snapshots(current_issue_dir: Path, raw: str, label: str) -> list[StableFileSnapshot]:
    if contains_forbidden_remote_reference(raw):
        raise ValueError(f"{label} evidence must stay in the repository; do not use COS/OSS/http(s) links")
    entries = section_entries(raw)
    if not entries:
        raise ValueError(f"{label} evidence must reference at least one repository-local file")
    repo_root = current_issue_dir.parents[2]
    snapshots = []
    for entry in entries:
        if not entry or entry.startswith("#"):
            continue
        relative_path = safe_relative_reference(entry, f"{label} evidence link")
        evidence_path = require_safe_repo_path(repo_root, current_issue_dir / relative_path, f"{label} evidence link")
        relative = evidence_path.relative_to(current_issue_dir)
        if not relative.parts or relative.parts[0] != "evidence":
            raise ValueError(f"{label} evidence links must stay under the issue evidence directory")
        maximum = (
            MAX_IMAGE_EVIDENCE_BYTES
            if evidence_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            else MAX_STRUCTURED_EVIDENCE_BYTES
        )
        snapshots.append(
            capture_stable_file(
                repo_root,
                evidence_path,
                current_issue_dir,
                f"{label} evidence file",
                max_bytes=maximum,
            )
        )
    return snapshots


def check_issue_local_evidence(current_issue_dir: Path, raw: str, label: str) -> list[Path]:
    return [snapshot.path for snapshot in issue_local_evidence_snapshots(current_issue_dir, raw, label)]


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
) -> tuple[StableFileSnapshot, ...]:
    evidence_snapshots = issue_local_evidence_snapshots(current_issue_dir, fields[evidence_field], label)
    evidence_paths = [snapshot.path for snapshot in evidence_snapshots]
    require_checklist_item(fields[human_review_field], f"{label} Human Review")
    evidence_type = fields[type_field].strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", evidence_type):
        raise ValueError(f"{label} {type_field[5:]} must be a canonical verification type")
    if evidence_type not in UI_EVIDENCE_VERIFICATION_TYPES:
        return tuple(evidence_snapshots)

    relative_paths = [path.relative_to(current_issue_dir) for path in evidence_paths]
    has_screenshot = any("screenshots" in path.parts for path in relative_paths)
    has_dom = any("dom" in path.parts for path in relative_paths)
    if not has_screenshot or not has_dom:
        raise ValueError(f"{label} UI evidence must include both evidence/screenshots and evidence/dom artifacts")
    return tuple(evidence_snapshots)


def validate_evidence_blocks(
    current_issue_dir: Path,
    raw: str,
    pattern: re.Pattern[str],
    required_fields: tuple[str, ...],
    type_field: str,
    label: str,
) -> tuple[StableFileSnapshot, ...]:
    blocks = list(pattern.finditer(raw))
    if not blocks:
        raise ValueError(f"{label} must contain at least one numbered evidence bundle")
    snapshots: list[StableFileSnapshot] = []
    for block in blocks:
        identifier, title, content = block.groups()
        if not title.strip():
            raise ValueError(f"{label} {identifier} must have a title")
        fields = required_subsections(content, required_fields, f"{label} {identifier}")
        snapshots.extend(
            validate_evidence_bundle(
                current_issue_dir,
                fields,
                type_field,
                "#### Evidence",
                "#### Human Review",
                f"{label} {identifier}",
            )
        )
    return tuple(snapshots)


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
    if contains_forbidden_remote_reference(evidence):
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
    snapshot = capture_stable_file(
        repo_root.resolve(), path, current_issue_dir, "gap analysis", max_bytes=MAX_TEXT_ARTIFACT_BYTES
    )
    validate_gap_analysis_snapshot(repo_root, issue, snapshot)
    return path


def validate_gap_analysis_snapshot(
    repo_root: Path,
    issue: str,
    snapshot: StableFileSnapshot,
) -> tuple[StableFileSnapshot, ...]:
    current_issue_dir = issue_dir(repo_root.resolve(), issue).resolve()
    try:
        text = (snapshot.content or b"").decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"gap-analysis must be valid UTF-8: {snapshot.path}") from exc
    sections = required_sections(text, GAP_ANALYSIS_REQUIRED_SECTIONS, "gap-analysis")
    snapshots = issue_local_evidence_snapshots(current_issue_dir, sections["## Evidence"], "gap-analysis")
    snapshots.extend(
        validate_evidence_blocks(
            current_issue_dir,
            sections["## Evidence-Backed Findings"],
            FINDING_BLOCK_RE,
            GAP_FINDING_REQUIRED_SECTIONS,
            "#### Finding Type",
            "gap-analysis finding",
        )
    )

    recognition = markdown_field(sections["## Human Recognition"], "Recognized").lower()
    if recognition != "yes":
        raise ValueError("gap-analysis Human Recognition must contain Recognized: yes before implementation")
    return tuple(snapshots)


@repository_locked
def check_resolution_report(repo_root: Path, issue: str, file_path: Path | None = None) -> Path:
    root = repo_root.resolve(strict=False)
    current_issue_dir = require_safe_repo_path(root, issue_dir(root, issue), "resolution report Issue directory")
    requested = file_path or current_issue_dir / "resolution-report.md"
    path = require_safe_repo_path(root, requested if requested.is_absolute() else root / requested, "resolution report")
    try:
        path.relative_to(current_issue_dir)
    except ValueError as exc:
        raise ValueError("resolution report must stay under .xflow/issues/issue-<id>") from exc
    report_snapshot = capture_stable_file(
        root, path, current_issue_dir, "resolution report", max_bytes=MAX_TEXT_ARTIFACT_BYTES
    )
    try:
        text = (report_snapshot.content or b"").decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"resolution report must be valid UTF-8: {path}") from exc
    sections = required_sections(text, RESOLUTION_REPORT_REQUIRED_SECTIONS, "resolution-report")
    report_evidence = issue_local_evidence_snapshots(current_issue_dir, sections["## Evidence Index"], "resolution-report")
    criterion_evidence = validate_evidence_blocks(
        current_issue_dir,
        sections["## Completion Verification"],
        COMPLETION_CRITERION_RE,
        COMPLETION_VERIFICATION_REQUIRED_SECTIONS,
        "#### Verification Type",
        "resolution-report criterion",
    )

    conclusion_lines = [line.strip() for line in sections["## Closure Conclusion"].splitlines() if line.strip()]
    if len(conclusion_lines) > 2:
        raise ValueError("resolution-report Closure Conclusion contains unexpected prose")
    if not conclusion_lines or not re.fullmatch(
        r"Conclusion:\s*(resolved|reduced|blocked)", conclusion_lines[0], re.IGNORECASE
    ):
        raise ValueError("resolution-report Closure Conclusion requires one canonical Conclusion field")
    if len(conclusion_lines) != 2 or not conclusion_lines[1].lower().startswith("reason:"):
        raise ValueError("resolution-report Closure Conclusion requires a non-empty Reason field")
    reason = conclusion_lines[1][len("Reason:"):].strip()
    if not reason:
        raise ValueError("resolution-report Closure Conclusion requires a non-empty Reason field")
    conclusion_match = re.fullmatch(
        r"Conclusion:\s*(resolved|reduced|blocked)", conclusion_lines[0], re.IGNORECASE
    )
    assert conclusion_match is not None
    conclusion = conclusion_match.group(1).lower()
    report_criteria: dict[str, tuple[str, str]] = {}
    for match in COMPLETION_CRITERION_RE.finditer(sections["## Completion Verification"]):
        number, title, content = match.groups()
        fields = required_subsections(content, COMPLETION_VERIFICATION_REQUIRED_SECTIONS, f"resolution-report criterion {number}")
        if number in report_criteria:
            raise ValueError("resolution-report Criterion C-NNN bindings must be unique")
        report_criteria[number] = (title.strip(), fields["#### Verification Type"].strip().lower())
    if len(report_criteria) != len(set(report_criteria)):
        raise ValueError("resolution-report Criterion C-NNN bindings must be unique")
    dependency_snapshot = capture_stable_file(
        root,
        current_issue_dir / "dependencies.yaml",
        current_issue_dir,
        "resolution-report dependencies",
        required=False,
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    dependency_support: tuple[StableFileSnapshot, ...] = ()
    if dependency_snapshot.exists:
        from .dependencies import check_dependencies, check_dependency_closure

        dependency_result = check_dependencies(
            repo_root,
            issue,
            dependency_snapshot.path,
            content=dependency_snapshot.content,
        )
        dependency_support = dependency_result.snapshots
        closure_violations = check_dependency_closure(dependency_result, conclusion)
        if closure_violations:
            raise ValueError(
                "resolution-report dependency closure is inconsistent: " + "; ".join(closure_violations)
            )
    if conclusion in {"resolved", "reduced"} and has_unchecked_checklist_item(sections["## AI Self-Review Result"]):
        raise ValueError("resolved/reduced resolution-report must not contain unchecked AI self-review items")
    from .traceability import check_traceability_resolution

    check_traceability_resolution(
        root,
        issue,
        conclusion,
        tuple(report_evidence),
        report_criteria=report_criteria,
        report_snapshot=report_snapshot,
        support_snapshots=(
            (dependency_snapshot, "dependencies"),
            *((snapshot, "resolution-report criterion evidence") for snapshot in criterion_evidence),
            *((snapshot, "dependency integration evidence") for snapshot in dependency_support),
        ),
    )
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
        if ISSUE_WORKSPACE_REMOTE_RE.search(text) or contains_forbidden_object_storage_reference(text):
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
