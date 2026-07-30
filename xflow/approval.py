from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from .bindings import GitBindings, resolve_bindings
from .io import read_text
from .paths import default_approval_file, issue_dir, normalized_issue
from .unattended import require_active


UNATTENDED_ACTIONS = {
    "issue-create",
    "issue-comment",
    "issue-close",
    "git-push",
    "git-mr",
    "git-pr-merge",
}
APPROVAL_ACTIONS = UNATTENDED_ACTIONS | {
    "contract-acceptance",
    "git-cleanup",
    "git-cleanup-force",
}
HISTORY_ACTIONS = UNATTENDED_ACTIONS | {"git-state-backfill"}
REQUIRED_TEXT = (
    "# Local Review Approval",
    "Issue:",
    "Reviewer:",
    "Approved At:",
    "Approval ID:",
    "Repository ID:",
    "Worktree ID:",
    "Branch:",
    "Approved Action:",
    "Approved File:",
    "Approved SHA256:",
    "## Decision",
)
HISTORY_COMMON_FIELDS = {
    "version", "reusable", "source", "approvalId", "repository", "worktree", "branch", "issue",
    "approvalIssue", "action", "approvedFile", "approvedSha256", "reviewerSummary", "result", "recordedAt",
}
HISTORY_EFFECT_FIELDS = HISTORY_COMMON_FIELDS | {"parentAction", "parentApprovalId"}
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
APPROVAL_ID_RE = re.compile(r"[0-9a-f]{32,64}")
CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)(?:^|[^A-Za-z0-9])api[_-]?key\s*[:=]"),
    re.compile(r"(?i)(?:^|[^A-Za-z0-9])(?:token|secret|password|credential|access[_-]?key)\s*[:=]"),
)


@dataclass(frozen=True)
class ApprovalGrant:
    source: Literal["local-review", "unattended"]
    approval_id: str
    repository: str
    worktree: str
    branch: str
    approval_issue: str
    action: str
    approved_file: str
    approved_sha256: str
    reviewer_summary: str


def validate_action(action: str, *, history: bool = False) -> str:
    allowed = HISTORY_ACTIONS if history else APPROVAL_ACTIONS
    if not isinstance(action, str) or action not in allowed:
        raise ValueError(f"invalid approval action: {action}")
    return action


def reject_credentials(value: str) -> None:
    if any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS):
        raise ValueError("credential-like text is not allowed in approval history")


def safe_reviewer_summary(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 120 or not re.fullmatch(r"[A-Za-z0-9 .@()_+\-]+", candidate):
        return "human reviewer"
    if any(pattern.search(candidate) for pattern in CREDENTIAL_PATTERNS):
        return "human reviewer"
    return candidate


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"missing approved artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def field(text: str, name: str) -> str:
    prefix = f"{name}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise ValueError(f"missing required text '{prefix}'")


def reject_placeholder(text: str, name: str) -> None:
    value = field(text, name)
    upper = value.upper()
    if "<" in value or ">" in value or "TODO" in upper or "TBD" in upper:
        raise ValueError(f"placeholder remains in approval field: {name}")


def resolve_path(repo_root: Path, path: Path) -> Path:
    msys_path = resolve_msys_path(path)
    if msys_path is not None:
        return msys_path
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def resolve_msys_path(path: Path) -> Path | None:
    raw = path.as_posix()
    if os.name != "nt" or not raw.startswith("/") or raw.startswith("//"):
        return None
    result = subprocess.run(
        ["cygpath", "-w", raw],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    converted = result.stdout.strip()
    return Path(converted).resolve() if converted else None


def display_path(repo_root: Path, path: Path) -> str:
    resolved = resolve_path(repo_root, path)
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def suggested_command(action: str, approved_file: Path, issue: str, attachment_manifest: Path | None = None) -> str:
    path = approved_file.as_posix()
    attachments = f" --attachments {attachment_manifest.as_posix()}" if attachment_manifest else ""
    if action == "issue-create":
        return f'devctl issue create "<title>" --body-file {path}{attachments}'
    if action == "issue-comment":
        return f"devctl issue comment {issue} --body-file {path}{attachments}"
    if action == "issue-close":
        return f"devctl issue close {issue}"
    if action == "git-push":
        return f"devctl git push --issue {issue} --file {path}"
    if action == "git-mr":
        return f'devctl git mr --title "<title>" --body-file {path} --issue {issue}{attachments}'
    if action == "git-pr-merge":
        return f"devctl git pr-merge <number> --issue {issue} --file {path}"
    if action == "git-cleanup":
        return f"devctl git done --issue {issue} --file {path}"
    if action == "git-cleanup-force":
        return f"devctl git done --force --issue {issue} --file {path}"
    return f"devctl <remote-write-command> --body-file {path}"


def git_config(repo_root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", key],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def default_reviewer(repo_root: Path) -> str:
    name = git_config(repo_root, "user.name")
    email = git_config(repo_root, "user.email")
    if name and email:
        return f"{name} ({email})"
    if name:
        return name
    if email:
        return email
    return "human reviewer"


def prepare(
    repo_root: Path,
    issue: str,
    action: str,
    approved_file: Path,
    command: str | None = None,
    reviewer: str | None = None,
    force: bool = False,
    attachment_manifest: Path | None = None,
) -> Path:
    repo_root = repo_root.resolve()
    issue = normalized_issue(issue)
    action = validate_action(action)
    bindings = resolve_bindings(repo_root)
    approved_path = resolve_path(repo_root, approved_file)
    if not approved_path.is_file():
        raise ValueError(f"missing approved artifact: {approved_path}")
    review_file = default_approval_file(repo_root, issue)
    if review_file.exists() and not force:
        existing = read_text(review_file)
        if "Approved: yes" in existing:
            raise ValueError(f"refusing to overwrite approved local review: {review_file}")

    relative_file = display_path(repo_root, approved_path)
    digest = sha256_file(approved_path).lower()
    attachment_text = ""
    relative_manifest: str | None = None
    if attachment_manifest is not None:
        manifest_path = resolve_path(repo_root, attachment_manifest)
        if not manifest_path.is_file():
            raise ValueError(f"missing attachment manifest: {manifest_path}")
        relative_manifest = display_path(repo_root, manifest_path)
        manifest_digest = sha256_file(manifest_path).lower()
        attachment_text = (
            f"Attachment Manifest: {relative_manifest}\n"
            f"Attachment Manifest SHA256: {manifest_digest}\n"
        )
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    approval_id = uuid.uuid4().hex
    command = command or suggested_command(action, Path(relative_file), issue, Path(relative_manifest) if relative_manifest else None)
    reviewer = reviewer or default_reviewer(repo_root)
    text = (
        "# Local Review Approval\n\n"
        f"Issue: {issue}\n"
        f"Reviewer: {reviewer}\n"
        f"Approved At: {now}\n"
        f"Approval ID: {approval_id}\n"
        f"Repository ID: {bindings.repository}\n"
        f"Worktree ID: {bindings.worktree}\n"
        f"Branch: {bindings.branch}\n"
        f"Approved Action: {action}\n"
        f"Approved File: {relative_file}\n"
        f"Approved SHA256: {digest}\n"
        f"{attachment_text}\n"
        "## Decision\n"
        "Approved: no\n\n"
        "## Human Gate\n"
        "Prepared by AI or tooling does not mean approved.\n"
        "Only the human reviewer may change Approved: no to Approved: yes.\n"
        "If this file was approved by the AI, the approval is invalid.\n\n"
        "## Suggested Command\n"
        f"Suggested Command: {command}\n\n"
        "## Expected Effect\n"
        f"- Authorizes exactly one `{action}` action for `{relative_file}` after human approval.\n"
        "- If the approved file changes, run `devctl approval prepare` again before remote write.\n"
    )
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(text, encoding="utf-8", newline="\n")
    return review_file


def _validate_review_text(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    attachment_manifest: Path | None,
    text: str,
    bindings: GitBindings,
) -> tuple[Path, str]:
    for needle in REQUIRED_TEXT:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}'")
    for name in (
        "Issue", "Reviewer", "Approved At", "Repository ID", "Worktree ID", "Branch", "Approved Action",
        "Approved File", "Approved SHA256", "Approved",
    ):
        reject_placeholder(text, name)
    if field(text, "Approved").lower() != "yes":
        raise ValueError("local approval required: Approved: yes")

    expected_bindings = {
        "Repository ID": bindings.repository,
        "Worktree ID": bindings.worktree,
        "Branch": bindings.branch,
        "Issue": issue,
    }
    for name, expected_value in expected_bindings.items():
        actual_value = field(text, name)
        if actual_value != expected_value:
            label = "Issue" if name == "Issue" else name.removesuffix(" ID").lower()
            raise ValueError(f"approval {label} mismatch: expected {expected_value}, got {actual_value}")

    declared = resolve_path(repo_root, Path(field(text, "Approved File")))
    actual = resolve_path(repo_root, approved_file)
    if declared != actual:
        raise ValueError(f"approved file mismatch: expected {display_path(repo_root, actual)}")

    expected = field(text, "Approved SHA256").lower()
    actual_hash = sha256_file(actual).lower()
    if expected != actual_hash:
        raise ValueError(f"hash mismatch for {approved_file}: expected {expected}, actual {actual_hash}")

    if attachment_manifest is not None:
        declared_manifest = resolve_path(repo_root, Path(field(text, "Attachment Manifest")))
        actual_manifest = resolve_path(repo_root, attachment_manifest)
        if declared_manifest != actual_manifest:
            raise ValueError(f"attachment manifest mismatch: expected {display_path(repo_root, actual_manifest)}")
        expected_manifest = field(text, "Attachment Manifest SHA256").lower()
        actual_manifest_hash = sha256_file(actual_manifest).lower()
        if expected_manifest != actual_manifest_hash:
            raise ValueError(
                f"hash mismatch for attachment manifest {attachment_manifest}: "
                f"expected {expected_manifest}, actual {actual_manifest_hash}"
            )
    return actual, actual_hash


def check(repo_root: Path, issue: str, approved_file: Path, attachment_manifest: Path | None = None) -> None:
    repo_root = repo_root.resolve()
    issue = normalized_issue(issue)
    bindings = resolve_bindings(repo_root)
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    _validate_review_text(
        repo_root,
        issue,
        approved_file,
        attachment_manifest,
        read_text(review_file),
        bindings,
    )


def _timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _parse_history(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(read_text(path))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"approval history integrity error in {path}: invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"approval history integrity error in {path}: expected a mapping")
    source = payload.get("source")
    expected_fields = HISTORY_EFFECT_FIELDS if source == "effect" else HISTORY_COMMON_FIELDS
    if set(payload) != expected_fields:
        raise ValueError(f"approval history integrity error in {path}: unexpected or missing fields")
    try:
        if payload["version"] != "0.1.0" or payload["reusable"] is not False or payload["result"] != "success":
            raise ValueError("invalid fixed fields")
        if source not in {"local-review", "unattended", "effect"}:
            raise ValueError("invalid source")
        for name in ("repository", "worktree"):
            if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
                raise ValueError(f"invalid {name}")
        for name in ("issue", "approvalIssue"):
            if payload[name] != normalized_issue(payload[name]):
                raise ValueError(f"invalid {name}")
        if not isinstance(payload["approvalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["approvalId"]):
            raise ValueError("invalid approvalId")
        validate_action(payload["action"], history=True)
        if not isinstance(payload["approvedSha256"], str) or not FINGERPRINT_RE.fullmatch(payload["approvedSha256"]):
            raise ValueError("invalid approvedSha256")
        for name in ("branch", "approvedFile", "reviewerSummary", "recordedAt"):
            if not isinstance(payload[name], str) or not payload[name]:
                raise ValueError(f"invalid {name}")
        _timestamp(payload["recordedAt"], "recordedAt")
        if source == "effect":
            validate_action(payload["parentAction"])
            if not isinstance(payload["parentApprovalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["parentApprovalId"]):
                raise ValueError("invalid parentApprovalId")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"approval history integrity error in {path}: {exc}") from exc
    reject_credentials(yaml.safe_dump(payload, sort_keys=True, allow_unicode=False))
    return payload


def _history_records(repo_root: Path) -> tuple[dict[str, object], ...]:
    issues_root = repo_root.resolve() / ".xflow" / "issues"
    if not issues_root.is_dir():
        return ()
    records = []
    for path in sorted(issues_root.glob("issue-*/approvals/history/*.yaml")):
        records.append(_parse_history(path))
    return tuple(records)


def reject_consumed_approval(repo_root: Path, approval_id: str) -> None:
    for record in _history_records(repo_root):
        if record["source"] in {"local-review", "unattended"} and record["approvalId"] == approval_id:
            raise ValueError(f"approval already consumed: {approval_id}")


def check_reviewed_task_binding(repo_root: Path, issue: str, action: str) -> None:
    # This preserves legacy current-task.md workflows while using strict task bindings whenever present.
    from .checks import check_current_task

    check_current_task(repo_root, issue, check_stale_pr=not (issue == "draft" and action == "issue-create"))


def _validate_grant(grant: ApprovalGrant) -> None:
    if grant.source not in {"local-review", "unattended"}:
        raise ValueError(f"unknown approval source: {grant.source}")
    if not APPROVAL_ID_RE.fullmatch(grant.approval_id):
        raise ValueError("invalid approval identity")
    if not FINGERPRINT_RE.fullmatch(grant.repository) or not FINGERPRINT_RE.fullmatch(grant.worktree):
        raise ValueError("invalid approval bindings")
    normalized_issue(grant.approval_issue)
    validate_action(grant.action)
    if not FINGERPRINT_RE.fullmatch(grant.approved_sha256):
        raise ValueError("invalid approved SHA256")
    reject_credentials(json.dumps(asdict(grant), ensure_ascii=True, sort_keys=True))


def _local_grant(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
) -> ApprovalGrant:
    action = validate_action(action)
    issue = normalized_issue(issue)
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    text = read_text(review_file)
    approved_action = field(text, "Approved Action")
    if approved_action != action:
        qualifier = "exact " if action == "contract-acceptance" else ""
        raise ValueError(f"action mismatch: expected {qualifier}{action}, got {approved_action}")
    bindings = resolve_bindings(repo_root)
    approved_path, approved_hash = _validate_review_text(
        repo_root,
        issue,
        approved_file,
        attachment_manifest,
        text,
        bindings,
    )
    check_reviewed_task_binding(repo_root, issue, action)
    grant = ApprovalGrant(
        source="local-review",
        approval_id=field(text, "Approval ID"),
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        approval_issue=issue,
        action=action,
        approved_file=display_path(repo_root, approved_path),
        approved_sha256=approved_hash,
        reviewer_summary=safe_reviewer_summary(field(text, "Reviewer")),
    )
    _validate_grant(grant)
    reject_consumed_approval(repo_root, grant.approval_id)
    return grant


def require_remote(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
) -> ApprovalGrant:
    return _local_grant(repo_root, action, approved_file, issue, attachment_manifest)


def require_exact_remote(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
) -> ApprovalGrant:
    return _local_grant(repo_root, action, approved_file, issue)


def require_remote_or_unattended(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
    request_unattended: bool = False,
) -> ApprovalGrant:
    if action == "contract-acceptance":
        if request_unattended:
            raise ValueError("contract-acceptance is not eligible for unattended mode")
        return require_exact_remote(repo_root, action, approved_file, issue)
    if action not in UNATTENDED_ACTIONS:
        raise ValueError(f"remote action {action} is not eligible for unattended mode")

    sha256_file(resolve_path(repo_root, approved_file))
    if attachment_manifest is not None:
        sha256_file(resolve_path(repo_root, attachment_manifest))

    try:
        state = require_active(repo_root, issue)
    except ValueError:
        if request_unattended:
            raise ValueError("--no-local-review requires active task-scoped unattended mode") from None
        return require_remote(repo_root, action, approved_file, issue, attachment_manifest)

    print(f"[UNATTENDED] Human approval gate bypassed for current task {state.issue}.")
    bindings = resolve_bindings(repo_root)
    approved_path = resolve_path(repo_root, approved_file)
    approved_hash = sha256_file(approved_path).lower()
    grant = ApprovalGrant(
        source="unattended",
        approval_id=uuid.uuid4().hex,
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        approval_issue=normalized_issue(issue),
        action=action,
        approved_file=display_path(repo_root, approved_path),
        approved_sha256=approved_hash,
        reviewer_summary="task-scoped-unattended",
    )
    _validate_grant(grant)
    reject_consumed_approval(repo_root, grant.approval_id)
    return grant


def _write_history_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ValueError(f"consumed approval history collision: {path}") from None
    finally:
        if temporary.exists():
            temporary.unlink()


def _history_path(repo_root: Path, issue: str, action: str, recorded_at: str) -> Path:
    issue = normalized_issue(issue)
    action = validate_action(action, history=True)
    issue_root = issue_dir(repo_root.resolve(), issue).resolve()
    history_root = (issue_root / "approvals" / "history").resolve()
    try:
        history_root.relative_to(issue_root)
    except ValueError as exc:
        raise ValueError("approval history path escapes Issue directory") from exc
    timestamp = recorded_at.replace("-", "").replace(":", "").replace(".", "")
    target = (history_root / f"{timestamp}-{action}.yaml").resolve()
    if target.parent != history_root:
        raise ValueError("approval history path escapes history directory")
    return target


def _record_payload(
    grant: ApprovalGrant,
    target_issue: str,
    recorded_at: str,
    *,
    source: Literal["local-review", "unattended", "effect"] | None = None,
    action: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": "0.1.0",
        "reusable": False,
        "source": source or grant.source,
        "approvalId": grant.approval_id,
        "repository": grant.repository,
        "worktree": grant.worktree,
        "branch": grant.branch,
        "issue": normalized_issue(target_issue),
        "approvalIssue": grant.approval_issue,
        "action": action or grant.action,
        "approvedFile": grant.approved_file,
        "approvedSha256": grant.approved_sha256,
        "reviewerSummary": grant.reviewer_summary,
        "result": "success",
        "recordedAt": recorded_at,
    }
    return payload


def record_consumed_approval(
    repo_root: Path,
    grant: ApprovalGrant,
    result: Literal["success"],
    *,
    target_issue: str | None = None,
) -> Path:
    if result != "success":
        raise ValueError("consumed approval records require confirmed success")
    repo_root = repo_root.resolve()
    _validate_grant(grant)
    reject_consumed_approval(repo_root, grant.approval_id)
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    issue = target_issue or grant.approval_issue
    history_file = _history_path(repo_root, issue, grant.action, recorded_at)
    payload = _record_payload(grant, issue, recorded_at)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
    return history_file


def record_subordinate_effect(
    repo_root: Path,
    parent_grant: ApprovalGrant,
    action: Literal["git-state-backfill"],
    result: Literal["success"],
) -> Path:
    if result != "success":
        raise ValueError("subordinate effect records require confirmed success")
    if action != "git-state-backfill" or parent_grant.action != "git-mr":
        raise ValueError("git-state-backfill must be a subordinate effect of git-mr")
    repo_root = repo_root.resolve()
    _validate_grant(parent_grant)
    records = _history_records(repo_root)
    parent_records = [
        record
        for record in records
        if record["source"] in {"local-review", "unattended"}
        and record["approvalId"] == parent_grant.approval_id
    ]
    if not parent_records:
        raise ValueError("git-state-backfill requires a consumed git-mr parent approval")
    expected_parent = _record_payload(
        parent_grant,
        parent_grant.approval_issue,
        str(parent_records[0]["recordedAt"]),
    )
    if len(parent_records) != 1 or parent_records[0] != expected_parent:
        raise ValueError("git-state-backfill parent approval snapshot mismatch")
    if any(
        record["source"] == "effect"
        and record["parentApprovalId"] == parent_grant.approval_id
        and record["action"] == action
        for record in records
    ):
        raise ValueError("git-state-backfill effect already recorded")
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    effect_id = hashlib.sha256(f"effect\0{parent_grant.approval_id}\0{action}".encode("utf-8")).hexdigest()
    effect_grant = ApprovalGrant(
        source=parent_grant.source,
        approval_id=effect_id,
        repository=parent_grant.repository,
        worktree=parent_grant.worktree,
        branch=parent_grant.branch,
        approval_issue=parent_grant.approval_issue,
        action=parent_grant.action,
        approved_file=parent_grant.approved_file,
        approved_sha256=parent_grant.approved_sha256,
        reviewer_summary="subordinate-effect",
    )
    payload = _record_payload(effect_grant, parent_grant.approval_issue, recorded_at, source="effect", action=action)
    payload["parentAction"] = parent_grant.action
    payload["parentApprovalId"] = parent_grant.approval_id
    history_file = _history_path(repo_root, parent_grant.approval_issue, action, recorded_at)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
    return history_file
