from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Sequence

import yaml

from .bindings import GitBindings, git_path, resolve_bindings
from .classification import _decode_classification_bytes, _load_yaml, _read_stable_bytes
from .io import read_text, write_text_lf
from .local_artifacts import MAX_TEXT_ARTIFACT_BYTES, StableFileSnapshot, capture_stable_file
from .paths import (
    active_task_pointer_file,
    default_approval_file,
    issue_dir,
    local_issue_dir,
    normalized_issue,
    task_authority_file,
    task_state_file,
)
from .project_config import require_safe_repo_path
from .stable_ids import is_stable_id
from .unattended import require_active


UNATTENDED_ACTIONS = {
    "issue-create",
    "issue-comment",
    "issue-close",
    "git-push",
    "git-mr",
    "git-pr-merge",
}
LOCAL_RECEIPT_ACTIONS = {
    "issue-comment",
    "issue-close",
    "git-push",
    "git-mr",
    "git-pr-merge",
    "git-state-backfill",
}
APPROVAL_ACTIONS = UNATTENDED_ACTIONS | {
    "task-contract-relocate",
    "contract-acceptance",
    "gap-recognition",
    "task-branch-start",
    "git-cleanup",
    "git-cleanup-force",
}
HISTORY_ACTIONS = UNATTENDED_ACTIONS | {
    "task-contract-relocate",
    "contract-acceptance",
    "gap-recognition",
    "task-branch-start",
    "git-state-backfill",
}
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
HISTORY_CONTRACT_FIELDS = HISTORY_COMMON_FIELDS | {
    "contractId", "contractVersion", "contractSha256", "acceptedObjects", "semanticDecision",
    "approvedReviewFile", "approvedReviewSha256", "contractSnapshotFile",
    "contractSnapshotSha256", "approvalClaimFile",
}
HISTORY_GAP_FIELDS = HISTORY_COMMON_FIELDS | {
    "semanticDecision",
    "approvedReviewFile",
    "approvedReviewSha256",
    "gapSnapshotFile",
    "gapSnapshotSha256",
}
HISTORY_BRANCH_FIELDS = HISTORY_COMMON_FIELDS | {
    "targetBranch",
    "baseCommit",
    "approvedReviewFile",
    "approvedReviewSha256",
    "taskStateSnapshotFile",
    "taskStateSnapshotSha256",
    "branchStartClaimFile",
}
HISTORY_REMOTE_FIELDS = HISTORY_COMMON_FIELDS | {
    "approvedReviewFile",
    "approvedReviewSha256",
    "approvedSnapshotFile",
    "approvedSnapshotSha256",
    "remoteClaimFile",
    "providerReceipt",
}
CONTRACT_CLAIM_FIELD_ORDER = (
    "version", "reusable", "approvalId", "repository", "worktree", "branch", "issue", "action",
    "approvedFile", "approvedSha256", "contractId", "contractVersion", "contractSha256",
    "acceptedObjects", "semanticDecision", "approvedReviewFile", "approvedReviewSha256",
    "contractSnapshotFile", "contractSnapshotSha256",
    "claimedAt", "recordedAt", "historyFile", "historySha256",
)
CONTRACT_CLAIM_FIELDS = set(CONTRACT_CLAIM_FIELD_ORDER)
CONTRACT_REVIEW_FIELDS = {"version", "acceptedObjects", "semanticDecision"}
CONTRACT_REVIEW_HEADING = "## Contract Acceptance"
EFFECT_ACTION = "git-state-backfill"
EFFECT_PARENT_ACTION = "git-mr"
EFFECT_REVIEWER_SUMMARY = "subordinate-effect"
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
APPROVAL_ID_RE = re.compile(r"[0-9a-f]{32,64}")
CANONICAL_REVIEW_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
CANONICAL_ACCEPTANCE_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)(?:^|[^A-Za-z0-9])api[_-]?key\s*[:=]"),
    re.compile(r"(?i)(?:^|[^A-Za-z0-9])(?:token|secret|password|credential|access[_-]?key)\s*[:=]"),
)
REMOTE_RECONCILIATION_CONFIRMATION = "XFLOW_HUMAN_REMOTE_RECONCILED"
TASK_BRANCH_SUPERSEDE_CONFIRMATION = "XFLOW_HUMAN_SUPERSEDE_TASK_BRANCH_START"
REMOTE_CLAIM_STATES = {
    "reserved",
    "outcome-unknown",
    "retryable",
    "post-effects-pending",
    "remote-confirmed",
    "completed",
}
REMOTE_CLAIM_FIELD_ORDER = (
    "version", "approvalId", "repository", "worktree", "branch", "approvalIssue", "action",
    "approvedFile", "approvedSha256", "reviewerSummary", "approvedReviewFile",
    "approvedReviewSha256", "approvedSnapshotFile", "approvedSnapshotSha256", "remoteClaimFile",
    "state", "attempt",
    "reservedAt", "updatedAt", "targetIssue", "providerReceipt", "failureReason", "recordedAt",
    "historyFile", "historySha256",
)
REMOTE_CLAIM_FIELDS = set(REMOTE_CLAIM_FIELD_ORDER)
TASK_BRANCH_CLAIM_STATES = {"reserved", "branch-created", "activated", "completed", "superseded"}
TASK_BRANCH_CLAIM_FIELD_ORDER = (
    "version", "approvalId", "repository", "worktree", "branch", "baseBranch", "targetBranch",
    "approvalIssue", "action", "approvedFile", "approvedSha256", "reviewerSummary",
    "approvedReviewFile", "approvedReviewSha256", "taskStateSnapshotFile",
    "taskStateSnapshotSha256", "branchStartClaimFile", "state", "baseCommit", "reservedAt",
    "updatedAt", "recordedAt", "historyFile", "historySha256",
)
TASK_BRANCH_CLAIM_FIELDS = set(TASK_BRANCH_CLAIM_FIELD_ORDER)
TASK_BRANCH_SUPERSEDED_FIELD_ORDER = TASK_BRANCH_CLAIM_FIELD_ORDER + (
    "supersededAt",
    "supersededReason",
)
TASK_BRANCH_SUPERSEDED_FIELDS = set(TASK_BRANCH_SUPERSEDED_FIELD_ORDER)
TASK_BRANCH_BASE_SYNC_CLAIM_VERSION = "0.3.0"
TASK_BRANCH_BASE_SYNC_SUPERSEDED_VERSION = "0.4.0"
TASK_BRANCH_BASE_SYNC_CLAIM_FIELD_ORDER = (
    "version", "approvalId", "repository", "worktree", "branch", "baseBranch", "targetBranch",
    "approvalIssue", "action", "approvedFile", "approvedSha256", "reviewerSummary",
    "approvedReviewFile", "approvedReviewSha256", "taskStateSnapshotFile",
    "taskStateSnapshotSha256", "branchStartClaimFile", "state", "baseCommit",
    "preSyncBaseCommit", "reservedAt", "updatedAt", "recordedAt", "historyFile", "historySha256",
)
TASK_BRANCH_BASE_SYNC_SUPERSEDED_FIELD_ORDER = TASK_BRANCH_BASE_SYNC_CLAIM_FIELD_ORDER + (
    "supersededAt",
    "supersededReason",
)
TASK_BRANCH_CLAIM_SCHEMAS = {
    "0.1.0": TASK_BRANCH_CLAIM_FIELD_ORDER,
    "0.2.0": TASK_BRANCH_SUPERSEDED_FIELD_ORDER,
    TASK_BRANCH_BASE_SYNC_CLAIM_VERSION: TASK_BRANCH_BASE_SYNC_CLAIM_FIELD_ORDER,
    TASK_BRANCH_BASE_SYNC_SUPERSEDED_VERSION: TASK_BRANCH_BASE_SYNC_SUPERSEDED_FIELD_ORDER,
}
TASK_BRANCH_SUPERSEDED_VERSIONS = {"0.2.0", TASK_BRANCH_BASE_SYNC_SUPERSEDED_VERSION}
TASK_BRANCH_BASE_SYNC_VERSIONS = {
    TASK_BRANCH_BASE_SYNC_CLAIM_VERSION,
    TASK_BRANCH_BASE_SYNC_SUPERSEDED_VERSION,
}
GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")


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
    accepted_objects: tuple[str, ...] = ()
    target_branch: str = ""


@dataclass(frozen=True)
class RemoteActionReservation:
    grant: ApprovalGrant
    claim_path: Path
    approved_snapshot_path: Path
    approved_bytes: bytes
    attempt: int
    provider_required: bool
    target_issue: str = ""
    provider_receipt: str = ""

    def approved_text(self) -> str:
        try:
            return self.approved_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("approved remote body snapshot must be valid UTF-8") from exc


@dataclass(frozen=True)
class TaskBranchStartReservation:
    grant: ApprovalGrant
    claim_path: Path
    task_state_snapshot_path: Path
    task_state_bytes: bytes
    approved_review_path: Path
    approved_review_bytes: bytes
    state: str
    base_commit: str


def validate_action(action: str, *, history: bool = False) -> str:
    allowed = HISTORY_ACTIONS if history else APPROVAL_ACTIONS
    if not isinstance(action, str) or action not in allowed:
        raise ValueError(f"invalid approval action: {action}")
    return action


def normalize_accepted_objects(object_ids: Sequence[str] | None) -> tuple[str, ...]:
    if object_ids is None or isinstance(object_ids, (str, bytes)):
        raise ValueError("contract-acceptance requires accepted object IDs")
    normalized: list[str] = []
    for item in object_ids:
        if not is_stable_id(item):
            raise ValueError("contract-acceptance requires valid accepted object IDs")
        normalized.append(item)
    if not normalized:
        raise ValueError("contract-acceptance requires accepted object IDs")
    if len(normalized) != len(set(normalized)):
        raise ValueError("contract-acceptance requires unique accepted object IDs")
    return tuple(sorted(normalized))


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
    values = [line[len(prefix) :].strip() for line in text.splitlines() if line.startswith(prefix)]
    if len(values) > 1:
        raise ValueError(f"duplicate approval field: {name}")
    if values:
        return values[0]
    raise ValueError(f"missing required text '{prefix}'")


def decision_is_approved(text: str) -> bool:
    try:
        return field(text, "Approved").lower() == "yes"
    except ValueError:
        return False


def approval_is_consumed(repo_root: Path, approval_id: str) -> bool:
    try:
        reject_consumed_approval(repo_root, approval_id)
    except ValueError as exc:
        if str(exc).startswith("approval already consumed:"):
            return True
        raise
    return False


def live_review_blocks_prepare(repo_root: Path, review_file: Path) -> bool:
    existing = read_text(review_file)
    if not decision_is_approved(existing):
        return False
    try:
        approval_id = field(existing, "Approval ID")
    except ValueError:
        return True
    return not approval_is_consumed(repo_root, approval_id)


def retire_live_review(repo_root: Path, issue: str, approval_id: str) -> None:
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        return
    try:
        live_id = field(read_text(review_file), "Approval ID")
    except ValueError:
        return
    if live_id != approval_id:
        return
    review_file.unlink()


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


def suggested_command(
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
    accepted_objects: tuple[str, ...] = (),
) -> str:
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
    if action == "contract-acceptance":
        return f"devctl contract accept --issue {issue} --file {path} --objects {','.join(accepted_objects)}"
    if action == "gap-recognition":
        return f"devctl gap recognize --issue {issue} --file {path}"
    if action == "task-branch-start":
        return f"devctl git start <slug> --issue {issue} --file {path}"
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
    accepted_objects: Sequence[str] | None = None,
) -> Path:
    repo_root = repo_root.resolve()
    issue = normalized_issue(issue)
    action = validate_action(action)
    if action == "contract-acceptance":
        normalized_objects = normalize_accepted_objects(accepted_objects)
    else:
        if accepted_objects:
            raise ValueError("accepted object IDs are only valid for contract-acceptance")
        normalized_objects = ()
    bindings = resolve_bindings(repo_root)
    approved_path = resolve_path(repo_root, approved_file)
    if not approved_path.is_file():
        raise ValueError(f"missing approved artifact: {approved_path}")
    review_file = default_approval_file(repo_root, issue)
    if review_file.exists() and not force:
        if live_review_blocks_prepare(repo_root, review_file):
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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    approval_id = uuid.uuid4().hex
    command = command or suggested_command(
        action,
        Path(relative_file),
        issue,
        Path(relative_manifest) if relative_manifest else None,
        normalized_objects,
    )
    reviewer = reviewer or default_reviewer(repo_root)
    contract_review_text = ""
    if normalized_objects:
        contract_review_payload = {
            "version": "0.1.0",
            "acceptedObjects": list(normalized_objects),
            "semanticDecision": "accepted-design",
        }
        contract_review_yaml = yaml.safe_dump(
            contract_review_payload,
            sort_keys=False,
            allow_unicode=True,
        )
        contract_review_text = (
            f"{CONTRACT_REVIEW_HEADING}\n"
            "```yaml\n"
            f"{contract_review_yaml}"
            "```\n\n"
        )
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
        f"{contract_review_text}"
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
    write_text_lf(review_file, text)
    return review_file


def _contract_review_objects(text: str) -> tuple[str, ...]:
    if text.count(CONTRACT_REVIEW_HEADING) != 1:
        raise ValueError("contract-acceptance review must contain one Contract Acceptance block")
    section = text.split(CONTRACT_REVIEW_HEADING, 1)[1]
    if not section.startswith("\n```yaml\n") or "\n```" not in section[len("\n```yaml\n") :]:
        raise ValueError("contract-acceptance review has an invalid YAML block")
    yaml_text, remainder = section[len("\n```yaml\n") :].split("\n```", 1)
    if remainder and not remainder.startswith("\n"):
        raise ValueError("contract-acceptance review has an invalid YAML block terminator")
    try:
        payload = _load_yaml(yaml_text + "\n")
    except ValueError as exc:
        raise ValueError(f"invalid contract-acceptance review YAML: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != CONTRACT_REVIEW_FIELDS:
        raise ValueError("contract-acceptance review YAML has unexpected or missing fields")
    if payload["version"] != "0.1.0" or payload["semanticDecision"] != "accepted-design":
        raise ValueError("contract-acceptance review YAML has invalid fixed fields")
    accepted = payload["acceptedObjects"]
    if not isinstance(accepted, list):
        raise ValueError("contract-acceptance review acceptedObjects must be a list")
    normalized = normalize_accepted_objects(accepted)
    if tuple(accepted) != normalized:
        raise ValueError("contract-acceptance review acceptedObjects must be normalized")
    return normalized


def _validate_review_text(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    attachment_manifest: Path | None,
    text: str,
    bindings: GitBindings,
    expected_sha256: str | None = None,
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
    actual_hash = expected_sha256.lower() if expected_sha256 is not None else sha256_file(actual).lower()
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


def _stable_approval_bytes(repo_root: Path, path: Path, owner: Path, label: str) -> bytes:
    safe_path = require_safe_repo_path(repo_root, path, label)
    try:
        return _read_stable_bytes(repo_root, safe_path, owner, max_bytes=MAX_TEXT_ARTIFACT_BYTES)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", label)) from exc


def _decode_approval_bytes(content: bytes, path: Path, label: str) -> str:
    try:
        return _decode_classification_bytes(content, path)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", label)) from exc


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


def _canonical_utc_timestamp(value: object, label: str, *, microseconds: bool) -> datetime:
    pattern = CANONICAL_ACCEPTANCE_TIMESTAMP_RE if microseconds else CANONICAL_REVIEW_TIMESTAMP_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be canonical UTC")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be canonical UTC") from exc


def _canonical_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _effect_identity(payload: dict[str, object]) -> str:
    identity_fields = {
        name: payload[name]
        for name in sorted(HISTORY_EFFECT_FIELDS - {"approvalId"})
    }
    material = json.dumps(identity_fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(f"xflow-approval-effect-v1\0{material}".encode("utf-8")).hexdigest()


def _parse_history_snapshot(
    repo_root: Path,
    path: Path,
    content: bytes | None = None,
) -> tuple[dict[str, object], bytes]:
    try:
        issue_root = path.resolve().parents[2]
        raw_bytes = content if content is not None else _stable_approval_bytes(repo_root, path, issue_root, "approval history")
        payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "approval history"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"approval history integrity error in {path}: invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"approval history integrity error in {path}: expected a mapping")
    source = payload.get("source")
    action = payload.get("action")
    remote_snapshot_history = (
        source == "local-review"
        and action in UNATTENDED_ACTIONS
        and "approvedSnapshotFile" in payload
    )
    expected_fields = (
        HISTORY_EFFECT_FIELDS
        if source == "effect"
        else HISTORY_CONTRACT_FIELDS
        if action == "contract-acceptance"
        else HISTORY_GAP_FIELDS
        if action == "gap-recognition"
        else HISTORY_BRANCH_FIELDS
        if action == "task-branch-start"
        else HISTORY_REMOTE_FIELDS
        if remote_snapshot_history
        else HISTORY_COMMON_FIELDS
    )
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
        if not isinstance(payload["approvedSha256"], str) or not FINGERPRINT_RE.fullmatch(payload["approvedSha256"]):
            raise ValueError("invalid approvedSha256")
        for name in ("branch", "approvedFile", "reviewerSummary", "recordedAt"):
            if not isinstance(payload[name], str) or not payload[name]:
                raise ValueError(f"invalid {name}")
        _timestamp(payload["recordedAt"], "recordedAt")
        if source == "effect":
            if payload["action"] != EFFECT_ACTION:
                raise ValueError(f"effect action must be {EFFECT_ACTION}")
            if payload["parentAction"] != EFFECT_PARENT_ACTION:
                raise ValueError(f"effect parentAction must be {EFFECT_PARENT_ACTION}")
            if payload["reviewerSummary"] != EFFECT_REVIEWER_SUMMARY:
                raise ValueError(f"effect reviewerSummary must be {EFFECT_REVIEWER_SUMMARY}")
            if payload["issue"] != payload["approvalIssue"]:
                raise ValueError("effect Issue must match approvalIssue")
            if not isinstance(payload["parentApprovalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["parentApprovalId"]):
                raise ValueError("invalid parentApprovalId")
            if payload["approvalId"] != _effect_identity(payload):
                raise ValueError("invalid effect approvalId")
        else:
            validate_action(payload["action"])
            if source == "unattended" and payload["reviewerSummary"] != "task-scoped-unattended":
                raise ValueError("invalid unattended reviewerSummary")
            if source == "local-review" and payload["reviewerSummary"] == EFFECT_REVIEWER_SUMMARY:
                raise ValueError("reserved local-review reviewerSummary")
            if payload["action"] == "contract-acceptance":
                if source != "local-review":
                    raise ValueError("contract acceptance must use local-review")
                for name in ("contractId", "contractVersion", "semanticDecision"):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                if not FINGERPRINT_RE.fullmatch(str(payload["contractSha256"])):
                    raise ValueError("invalid contractSha256")
                accepted_objects = payload["acceptedObjects"]
                if (
                    not isinstance(accepted_objects, list)
                    or not accepted_objects
                    or any(not isinstance(item, str) or not item for item in accepted_objects)
                    or len(accepted_objects) != len(set(accepted_objects))
                    or tuple(accepted_objects) != normalize_accepted_objects(accepted_objects)
                ):
                    raise ValueError("invalid acceptedObjects")
                if payload["semanticDecision"] != "accepted-design":
                    raise ValueError("invalid semanticDecision")
                _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
                for name in (
                    "approvedReviewFile",
                    "approvedReviewSha256",
                    "contractSnapshotFile",
                    "contractSnapshotSha256",
                    "approvalClaimFile",
                ):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                for name in ("approvedReviewSha256", "contractSnapshotSha256"):
                    if not FINGERPRINT_RE.fullmatch(str(payload[name])):
                        raise ValueError(f"invalid {name}")
                if payload["contractSnapshotSha256"] != payload["contractSha256"]:
                    raise ValueError("contract snapshot SHA256 must match accepted contract SHA256")
            elif payload["action"] == "gap-recognition":
                if source != "local-review" or payload["semanticDecision"] != "gap-recognized":
                    raise ValueError("gap recognition must be a local human decision")
                for name in ("approvedReviewFile", "gapSnapshotFile"):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                for name in ("approvedReviewSha256", "gapSnapshotSha256"):
                    if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
                        raise ValueError(f"invalid {name}")
                _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
            elif payload["action"] == "task-branch-start":
                if source != "local-review":
                    raise ValueError("task branch identity approval must use local-review")
                if (
                    not isinstance(payload["targetBranch"], str)
                    or not payload["targetBranch"]
                    or payload["targetBranch"] == payload["branch"]
                ):
                    raise ValueError("invalid targetBranch")
                if not isinstance(payload["baseCommit"], str) or not GIT_COMMIT_RE.fullmatch(payload["baseCommit"]):
                    raise ValueError("invalid baseCommit")
                for name in ("approvedReviewFile", "taskStateSnapshotFile", "branchStartClaimFile"):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                for name in ("approvedReviewSha256", "taskStateSnapshotSha256"):
                    if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
                        raise ValueError(f"invalid {name}")
                if payload["taskStateSnapshotSha256"] != payload["approvedSha256"]:
                    raise ValueError("task-state snapshot SHA256 mismatch")
                _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
            elif remote_snapshot_history:
                for name in ("approvedReviewFile", "approvedSnapshotFile", "remoteClaimFile", "providerReceipt"):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                for name in ("approvedReviewSha256", "approvedSnapshotSha256"):
                    if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
                        raise ValueError(f"invalid {name}")
                if payload["approvedSnapshotSha256"] != payload["approvedSha256"]:
                    raise ValueError("approved snapshot SHA256 mismatch")
                receipt = json.dumps(
                    json.loads(str(payload["providerReceipt"])),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if receipt != payload["providerReceipt"]:
                    raise ValueError("providerReceipt must be canonical JSON")
                _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
        expected_paths = _history_path_candidates(
            repo_root,
            str(payload["issue"]),
            str(payload["action"]),
            str(payload["recordedAt"]),
            str(payload["approvalId"])
            if payload["action"] in {"contract-acceptance", "gap-recognition", "task-branch-start"}
            or remote_snapshot_history
            else None,
        )
        if path.resolve() not in expected_paths:
            raise ValueError("history path does not match record Issue, action, and timestamp")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"approval history integrity error in {path}: {exc}") from exc
    reject_credentials(yaml.safe_dump(payload, sort_keys=True, allow_unicode=False))
    return payload, raw_bytes


def _parse_history(repo_root: Path, path: Path) -> dict[str, object]:
    return _parse_history_snapshot(repo_root, path)[0]


def _history_records(repo_root: Path) -> tuple[dict[str, object], ...]:
    located_records: list[tuple[Path, dict[str, object]]] = []
    for path in _history_yaml_paths(repo_root):
        located_records.append((path, _parse_history(repo_root, path)))

    direct_by_id: dict[str, tuple[Path, dict[str, object]]] = {}
    for path, record in located_records:
        if record["source"] == "effect":
            continue
        approval_id = str(record["approvalId"])
        if approval_id in direct_by_id:
            raise ValueError(f"approval history integrity error in {path}: duplicate direct approvalId")
        direct_by_id[approval_id] = (path, record)

    seen_effects: set[tuple[str, str]] = set()
    inherited_fields = (
        "repository",
        "worktree",
        "branch",
        "issue",
        "approvalIssue",
        "approvedFile",
        "approvedSha256",
    )
    for path, record in located_records:
        if record["source"] != "effect":
            continue
        parent_id = str(record["parentApprovalId"])
        parent_entry = direct_by_id.get(parent_id)
        if parent_entry is None:
            raise ValueError(f"approval history integrity error in {path}: missing direct parent approval")
        _, parent = parent_entry
        if parent["action"] != EFFECT_PARENT_ACTION:
            raise ValueError(f"approval history integrity error in {path}: parent is not a git-mr approval")
        if any(record[name] != parent[name] for name in inherited_fields):
            raise ValueError(f"approval history integrity error in {path}: effect parent snapshot mismatch")
        effect_key = (parent_id, str(record["action"]))
        if effect_key in seen_effects:
            raise ValueError(f"approval history integrity error in {path}: duplicate subordinate effect")
        seen_effects.add(effect_key)

    return tuple(record for _, record in located_records)


def reject_consumed_approval(repo_root: Path, approval_id: str) -> None:
    for record in _history_records(repo_root):
        if record["source"] in {"local-review", "unattended"} and record["approvalId"] == approval_id:
            remote_claim_file = record.get("remoteClaimFile")
            if record["source"] == "local-review" and isinstance(remote_claim_file, str):
                claim_path = require_safe_repo_path(
                    repo_root,
                    repo_root / Path(remote_claim_file),
                    "remote approval claim",
                )
                claim = _parse_remote_claim(repo_root, claim_path, str(record["approvalIssue"]))
                if claim["state"] != "completed" and _remote_history_payload(claim) == record:
                    continue
            branch_claim_file = record.get("branchStartClaimFile")
            if record["action"] == "task-branch-start" and isinstance(branch_claim_file, str):
                claim_path = require_safe_repo_path(
                    repo_root,
                    repo_root / Path(branch_claim_file),
                    "task branch start claim",
                )
                claim = _parse_task_branch_claim(repo_root, claim_path, str(record["approvalIssue"]))
                if claim["state"] != "completed" and _task_branch_history_payload(claim) == record:
                    continue
            raise ValueError(f"approval already consumed: {approval_id}")


def task_binding_evidence_exists(repo_root: Path) -> bool:
    from .bindings import resolve_bindings
    from .task_state import _pointer_snapshots, task_authority_issues

    root = repo_root.resolve()
    bindings = resolve_bindings(root)
    pointer, legacy_pointer = _pointer_snapshots(root, bindings)
    if pointer.exists or legacy_pointer.exists or task_authority_issues(root):
        return True
    if (root / ".xflow" / "current-task.md").is_file():
        return True
    issues = root / ".xflow" / "issues"
    return issues.is_dir() and any(
        candidate.is_file()
        for pattern in ("issue-*/task-state.md", "issue-*/classification.yaml", "issue-*/traceability-matrix.yaml")
        for candidate in issues.glob(pattern)
    )


def check_reviewed_task_binding(repo_root: Path, issue: str | None, action: str) -> None:
    # This preserves legacy current-task.md workflows while using strict task bindings whenever present.
    from .checks import check_current_task
    from .local_artifacts import revalidate_snapshots
    from .semantic_routes import require_route_semantics
    from .bindings import resolve_bindings
    from .task_state import _legacy_migration_source, _pointer_snapshots, check_task_binding

    is_draft_create = issue is not None and normalized_issue(issue) == "draft" and action == "issue-create"
    is_task_branch_start = action == "task-branch-start"
    if is_draft_create:
        bindings = resolve_bindings(repo_root)
        pointer, legacy_pointer = _pointer_snapshots(repo_root, bindings)
        if pointer.exists or legacy_pointer.exists:
            check_current_task(repo_root, issue, check_stale_pr=False)
        else:
            source, legacy_state, _ = _legacy_migration_source(repo_root, bindings)
            if legacy_state.issue != "draft":
                raise ValueError(
                    f"current task Issue mismatch: expected draft, found {legacy_state.issue or '<missing>'}"
                )
            revalidate_snapshots(repo_root, (source,), "current task state file")
    elif not is_task_branch_start:
        check_current_task(repo_root, issue, check_stale_pr=not is_draft_create)
    bindings = resolve_bindings(repo_root)
    pointer, legacy_pointer = _pointer_snapshots(repo_root, bindings)
    if not is_draft_create and not is_task_branch_start and (pointer.exists or legacy_pointer.exists):
        require_route_semantics(check_task_binding(repo_root, issue), action)


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
    if grant.source == "unattended" and grant.reviewer_summary != "task-scoped-unattended":
        raise ValueError("invalid unattended reviewer summary")
    if grant.source == "local-review" and grant.reviewer_summary in {
        "task-scoped-unattended",
        EFFECT_REVIEWER_SUMMARY,
    }:
        raise ValueError("reserved local-review reviewer summary")
    if grant.action == "contract-acceptance":
        if grant.source != "local-review":
            raise ValueError("contract acceptance requires local-review")
        if grant.accepted_objects != normalize_accepted_objects(grant.accepted_objects):
            raise ValueError("contract acceptance grant has invalid accepted object IDs")
    elif grant.accepted_objects:
        raise ValueError("accepted object IDs are only valid for contract-acceptance")
    if grant.action == "task-branch-start":
        if grant.source != "local-review":
            raise ValueError("task branch identity approval requires local-review")
        if not grant.target_branch or grant.target_branch == grant.branch:
            raise ValueError("task branch identity approval requires a distinct target branch")
    elif grant.target_branch:
        raise ValueError("target branch is only valid for task-branch-start")
    reject_credentials(json.dumps(asdict(grant), ensure_ascii=True, sort_keys=True))


def require_task_branch_start(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    target_branch: str,
    base_branch: str,
) -> ApprovalGrant:
    from .task_state import parse_task_state_text

    root = repo_root.resolve()
    issue = normalized_issue(issue)
    issue_root = issue_dir(root, issue)
    approved_path = resolve_path(root, approved_file)
    expected_path = task_state_file(root, issue).resolve()
    if approved_path != expected_path:
        raise ValueError("task branch identity approval requires the canonical Issue task-state.md")
    state_snapshot = capture_stable_file(
        root,
        approved_path,
        issue_root,
        "task branch identity state",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    try:
        state_text = (state_snapshot.content or b"").decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("task-state must be valid UTF-8") from exc
    state = parse_task_state_text(
        approved_path,
        state_text,
        binding_mode="recorded",
        validate_acceptance=False,
    )
    if state.issue != issue or state.classification == "ui-defect":
        raise ValueError("task branch identity approval requires a matching non-UI-defect Issue")
    if state.execution_state != "S2_REMOTE_ISSUE_CREATED":
        raise ValueError("task branch identity approval requires S2_REMOTE_ISSUE_CREATED")
    if state.semantic_phase not in {"classified", "declaring"}:
        raise ValueError("task branch identity approval requires classified or declaring")
    if state.branch != target_branch or state.base != base_branch:
        raise ValueError("task branch identity does not match task-state Branch and Base")

    review_file = default_approval_file(root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    review_bytes = _stable_approval_bytes(root, review_file, issue_root, "approval review")
    review_text = _decode_approval_bytes(review_bytes, review_file, "approval review")
    if field(review_text, "Approved Action") != "task-branch-start":
        raise ValueError("action mismatch: expected exact task-branch-start")
    bindings = resolve_bindings(root)
    if bindings.branch != base_branch:
        raise ValueError(f"task branch identity approval requires active base branch {base_branch}")
    approved_path, approved_hash = _validate_review_text(
        root,
        issue,
        approved_path,
        None,
        review_text,
        bindings,
        hashlib.sha256(state_snapshot.content or b"").hexdigest(),
    )
    grant = ApprovalGrant(
        source="local-review",
        approval_id=field(review_text, "Approval ID"),
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        approval_issue=issue,
        action="task-branch-start",
        approved_file=display_path(root, approved_path),
        approved_sha256=approved_hash,
        reviewer_summary=safe_reviewer_summary(field(review_text, "Reviewer")),
        target_branch=target_branch,
    )
    _validate_grant(grant)
    reject_consumed_approval(root, grant.approval_id)
    return grant


def _contract_local_grant(
    repo_root: Path,
    approved_file: Path,
    issue: str,
    *,
    expected_sha256: str | None = None,
    expected_objects: tuple[str, ...] | None = None,
) -> tuple[ApprovalGrant, Path, bytes]:
    issue = normalized_issue(issue)
    issue_root = issue_dir(repo_root, issue)
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    review_bytes = _stable_approval_bytes(repo_root, review_file, issue_root, "approval review")
    text = _decode_approval_bytes(review_bytes, review_file, "approval review")
    approved_action = field(text, "Approved Action")
    if approved_action != "contract-acceptance":
        raise ValueError(f"action mismatch: expected exact contract-acceptance, got {approved_action}")
    accepted_objects = _contract_review_objects(text)
    if expected_objects is not None and accepted_objects != expected_objects:
        raise ValueError(
            "contract acceptance accepted object set mismatch: "
            f"review has {','.join(accepted_objects)}"
        )
    bindings = resolve_bindings(repo_root)
    approved_path, approved_hash = _validate_review_text(
        repo_root,
        issue,
        approved_file,
        None,
        text,
        bindings,
        expected_sha256,
    )
    check_reviewed_task_binding(repo_root, issue, "contract-acceptance")
    grant = ApprovalGrant(
        source="local-review",
        approval_id=field(text, "Approval ID"),
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        approval_issue=issue,
        action="contract-acceptance",
        approved_file=display_path(repo_root, approved_path),
        approved_sha256=approved_hash,
        reviewer_summary=safe_reviewer_summary(field(text, "Reviewer")),
        accepted_objects=accepted_objects,
    )
    _validate_grant(grant)
    return grant, review_file, review_bytes


def _local_grant(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
) -> ApprovalGrant:
    action = validate_action(action)
    issue = normalized_issue(issue)
    if action == "contract-acceptance":
        grant, _, _ = _contract_local_grant(repo_root, approved_file, issue)
        reject_consumed_approval(repo_root, grant.approval_id)
        return grant
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
    if action in {"contract-acceptance", "gap-recognition"}:
        if request_unattended:
            raise ValueError(f"{action} is not eligible for unattended mode")
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

    if normalized_issue(issue) != "draft" and task_binding_evidence_exists(repo_root):
        check_reviewed_task_binding(repo_root, issue, action)

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


def _write_immutable_bytes(path: Path, content: bytes, collision_message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ValueError(f"{collision_message}: {path}") from None
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_history_atomic(path: Path, content: str) -> None:
    _write_immutable_bytes(
        path,
        content.encode("utf-8"),
        "consumed approval history collision",
    )


def _history_workspace_root(repo_root: Path, issue: str, action: str) -> Path:
    if action in LOCAL_RECEIPT_ACTIONS:
        return local_issue_dir(repo_root, issue)
    return issue_dir(repo_root, issue)


def _history_file_in_workspace(
    issue_root: Path,
    action: str,
    recorded_at: str,
    approval_suffix: str,
) -> Path:
    issue_root = issue_root.resolve()
    history_root = (issue_root / "approvals" / "history").resolve()
    try:
        history_root.relative_to(issue_root)
    except ValueError as exc:
        raise ValueError("approval history path escapes Issue directory") from exc
    timestamp = recorded_at.replace("-", "").replace(":", "").replace(".", "")
    target = (history_root / f"{timestamp}-{action}{approval_suffix}.yaml").resolve()
    if target.parent != history_root:
        raise ValueError("approval history path escapes history directory")
    return target


def _history_suffix(action: str, approval_id: str | None) -> str:
    if action in {"contract-acceptance", "gap-recognition", "task-branch-start"} or (
        action in UNATTENDED_ACTIONS and approval_id is not None
    ):
        if not isinstance(approval_id, str) or not APPROVAL_ID_RE.fullmatch(approval_id):
            raise ValueError(f"{action} history requires a valid approval ID")
        return f"-{approval_id}"
    if approval_id is not None:
        raise ValueError("approval ID filename suffix is reserved for one-time decision history")
    return ""


def _history_path(
    repo_root: Path,
    issue: str,
    action: str,
    recorded_at: str,
    approval_id: str | None = None,
) -> Path:
    issue = normalized_issue(issue)
    action = validate_action(action, history=True)
    suffix = _history_suffix(action, approval_id)
    return _history_file_in_workspace(
        _history_workspace_root(repo_root.resolve(), issue, action),
        action,
        recorded_at,
        suffix,
    )


def _history_path_candidates(
    repo_root: Path,
    issue: str,
    action: str,
    recorded_at: str,
    approval_id: str | None = None,
) -> tuple[Path, ...]:
    canonical = _history_path(repo_root, issue, action, recorded_at, approval_id)
    if action not in LOCAL_RECEIPT_ACTIONS:
        return (canonical,)
    suffix = _history_suffix(action, approval_id)
    legacy = _history_file_in_workspace(
        issue_dir(repo_root.resolve(), issue),
        action,
        recorded_at,
        suffix,
    )
    if legacy == canonical:
        return (canonical,)
    return (canonical, legacy)


def _history_yaml_paths(repo_root: Path) -> list[Path]:
    located: list[Path] = []
    for root in (
        repo_root.resolve() / ".xflow" / "issues",
        repo_root.resolve() / ".xflow" / "local" / "issues",
    ):
        if not root.is_dir():
            continue
        located.extend(sorted(root.glob("issue-*/approvals/history/*.yaml")))
    return located


def _history_artifact_path(
    repo_root: Path,
    issue: str,
    category: str,
    name: str,
    *,
    action: str | None = None,
    workspace_root: Path | None = None,
) -> Path:
    if category not in {"claims", "consumed"}:
        raise ValueError(f"invalid contract acceptance artifact category: {category}")
    if workspace_root is None:
        if action is None:
            workspace_root = issue_dir(repo_root.resolve(), normalized_issue(issue))
        else:
            workspace_root = _history_workspace_root(repo_root.resolve(), issue, action)
    history_root = workspace_root / "approvals" / "history"
    target = require_safe_repo_path(repo_root, history_root / category / name, "contract acceptance artifact")
    expected_parent = history_root / category
    if target.parent != expected_parent:
        raise ValueError("contract acceptance artifact path escapes approval history")
    return target


def _contract_artifact_path(repo_root: Path, issue: str, category: str, name: str) -> Path:
    return _history_artifact_path(repo_root, issue, category, name)


def _contract_claim_lock_path(repo_root: Path, approval_id: str) -> Path:
    if not APPROVAL_ID_RE.fullmatch(approval_id):
        raise ValueError("contract acceptance finalizer requires a valid approval ID")
    bindings = resolve_bindings(repo_root)
    return (
        git_path(repo_root, "--git-common-dir")
        / "xflow"
        / "runtime"
        / "contract-acceptance"
        / bindings.worktree
        / f"{approval_id}.lock"
    )


@contextmanager
def _contract_claim_lock(repo_root: Path, approval_id: str) -> Iterator[None]:
    lock_path = _contract_claim_lock_path(repo_root, approval_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError(f"approval already claimed: {approval_id}") from None
        acquired = True
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        if acquired:
            try:
                lock_path.unlink(missing_ok=True)
            except PermissionError:
                # A Windows contender may still have this runtime lock open.
                pass


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


def _record_target_issue(grant: ApprovalGrant, target_issue: str | None) -> str:
    approval_issue = normalized_issue(grant.approval_issue)
    if grant.action == "issue-create" and approval_issue == "draft":
        if target_issue is None:
            raise ValueError("draft issue-create history requires a provider-confirmed non-draft target")
        target = normalized_issue(target_issue)
        if target == "draft":
            raise ValueError("draft issue-create history requires a provider-confirmed non-draft target")
        return target

    target = approval_issue if target_issue is None else normalized_issue(target_issue)
    if target != approval_issue:
        raise ValueError(f"approval history target Issue mismatch: expected {approval_issue}, got {target}")
    return target


def record_consumed_approval(
    repo_root: Path,
    grant: ApprovalGrant,
    result: Literal["success"],
    *,
    target_issue: str | None = None,
) -> Path:
    if result != "success":
        raise ValueError("consumed approval records require confirmed success")
    if grant.action == "contract-acceptance":
        raise ValueError("contract-acceptance must use contract acceptance history")
    if grant.action == "gap-recognition":
        raise ValueError("gap-recognition must use gap recognition history")
    if grant.action == "task-branch-start":
        raise ValueError("task-branch-start must use task branch claim completion")
    repo_root = repo_root.resolve()
    _validate_grant(grant)
    reject_consumed_approval(repo_root, grant.approval_id)
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    issue = _record_target_issue(grant, target_issue)
    history_file = _history_path(
        repo_root,
        issue,
        grant.action,
        recorded_at,
        None,
    )
    payload = _record_payload(grant, issue, recorded_at)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
    retire_live_review(repo_root, grant.approval_issue, grant.approval_id)
    return history_file


def record_contract_acceptance(
    repo_root: Path,
    grant: ApprovalGrant,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    accepted_objects: tuple[str, ...],
) -> Path:
    del repo_root, grant, contract_id, contract_version, contract_sha256, accepted_objects
    raise ValueError("contract acceptance must consume the prepared local review directly")


def _yaml_bytes(payload: dict[str, object]) -> bytes:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).encode("utf-8")


def _contract_history_payload(
    grant: ApprovalGrant,
    issue: str,
    recorded_at: str,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    accepted_objects: tuple[str, ...],
    approved_review_file: str,
    approved_review_sha256: str,
    contract_snapshot_file: str,
    contract_snapshot_sha256: str,
    approval_claim_file: str,
) -> dict[str, object]:
    payload = _record_payload(grant, issue, recorded_at)
    payload.update(
        {
            "contractId": contract_id,
            "contractVersion": contract_version,
            "contractSha256": contract_sha256,
            "acceptedObjects": list(accepted_objects),
            "semanticDecision": "accepted-design",
            "approvedReviewFile": approved_review_file,
            "approvedReviewSha256": approved_review_sha256,
            "contractSnapshotFile": contract_snapshot_file,
            "contractSnapshotSha256": contract_snapshot_sha256,
            "approvalClaimFile": approval_claim_file,
        }
    )
    return payload


def _contract_claim_payload(
    grant: ApprovalGrant,
    issue: str,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    accepted_objects: tuple[str, ...],
    approved_review_file: str,
    approved_review_sha256: str,
    contract_snapshot_file: str,
    contract_snapshot_sha256: str,
    claimed_at: str,
    recorded_at: str,
    history_file: str,
    history_sha256: str,
) -> dict[str, object]:
    return {
        "version": "0.1.0",
        "reusable": False,
        "approvalId": grant.approval_id,
        "repository": grant.repository,
        "worktree": grant.worktree,
        "branch": grant.branch,
        "issue": issue,
        "action": grant.action,
        "approvedFile": grant.approved_file,
        "approvedSha256": grant.approved_sha256,
        "contractId": contract_id,
        "contractVersion": contract_version,
        "contractSha256": contract_sha256,
        "acceptedObjects": list(accepted_objects),
        "semanticDecision": "accepted-design",
        "approvedReviewFile": approved_review_file,
        "approvedReviewSha256": approved_review_sha256,
        "contractSnapshotFile": contract_snapshot_file,
        "contractSnapshotSha256": contract_snapshot_sha256,
        "claimedAt": claimed_at,
        "recordedAt": recorded_at,
        "historyFile": history_file,
        "historySha256": history_sha256,
    }


def _publish_exact_artifact(
    repo_root: Path,
    issue_root: Path,
    path: Path,
    content: bytes,
    *,
    label: str,
    collision_message: str,
) -> bool:
    if path.is_file():
        existing = _stable_approval_bytes(repo_root, path, issue_root, label)
        if existing != content:
            raise ValueError(f"existing {label} differs from sealed acceptance bytes")
        return False
    try:
        _write_immutable_bytes(path, content, collision_message)
        return True
    except ValueError as exc:
        if "collision" not in str(exc) or not path.is_file():
            raise
        existing = _stable_approval_bytes(repo_root, path, issue_root, label)
        if existing != content:
            raise ValueError(f"existing {label} differs from sealed acceptance bytes") from None
        return False


def _replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _task_branch_claim_path(repo_root: Path, issue: str, approval_id: str) -> Path:
    return _contract_artifact_path(repo_root, issue, "claims", f"{approval_id}-task-branch.yaml")


def _task_branch_state_snapshot_path(repo_root: Path, issue: str, approval_id: str) -> Path:
    return _contract_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{approval_id}-task-branch-start-task-state.md",
    )


def _task_branch_review_path(repo_root: Path, issue: str, approval_id: str) -> Path:
    return _contract_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{approval_id}-task-branch-start-local-review.md",
    )


def _task_branch_claim_order(version: object) -> tuple[str, ...] | None:
    if not isinstance(version, str):
        return None
    return TASK_BRANCH_CLAIM_SCHEMAS.get(version)


def _task_branch_claim_bytes(payload: dict[str, object]) -> bytes:
    version = payload.get("version")
    order = _task_branch_claim_order(version)
    if order is None or set(payload) != set(order):
        raise ValueError("task branch start claim has unexpected or missing fields")
    if version in TASK_BRANCH_BASE_SYNC_VERSIONS:
        pre_sync_base_commit = payload.get("preSyncBaseCommit")
        if (
            not isinstance(pre_sync_base_commit, str)
            or not GIT_COMMIT_RE.fullmatch(pre_sync_base_commit)
        ):
            raise ValueError("task branch start claim has invalid preSyncBaseCommit")
    return _yaml_bytes({name: payload[name] for name in order})


@contextmanager
def _approval_claim_lock(
    repo_root: Path,
    approval_id: str,
    runtime_scope: str,
    busy_message: str,
) -> Iterator[None]:
    if runtime_scope not in {"remote-approvals", "task-branch-start"}:
        raise ValueError("invalid approval claim lock scope")
    if not APPROVAL_ID_RE.fullmatch(approval_id):
        raise ValueError("approval claim lock requires a valid approval identity")
    bindings = resolve_bindings(repo_root)
    lock_path = (
        git_path(repo_root, "--git-common-dir")
        / "xflow"
        / "runtime"
        / runtime_scope
        / bindings.worktree
        / "claims.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            raise ValueError(f"{busy_message}: {approval_id}") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def _task_branch_claim_lock(repo_root: Path, approval_id: str) -> Iterator[None]:
    with _approval_claim_lock(
        repo_root,
        approval_id,
        "task-branch-start",
        "task branch approval reservation is busy",
    ):
        yield


def _git_ref_commit(repo_root: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def _parse_task_branch_claim(repo_root: Path, path: Path, issue: str) -> dict[str, object]:
    issue_root = issue_dir(repo_root, issue)
    raw_bytes = _stable_approval_bytes(repo_root, path, issue_root, "task branch start claim")
    payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "task branch start claim"))
    if not isinstance(payload, dict):
        raise ValueError("task branch start claim has unexpected or missing fields")
    version = payload.get("version")
    order = _task_branch_claim_order(version)
    if order is None:
        raise ValueError("task branch start claim has unsupported schema version")
    expected_fields = set(order)
    if set(payload) != expected_fields:
        raise ValueError("task branch start claim has unexpected or missing fields")
    if (
        payload["action"] != "task-branch-start"
        or payload["state"] not in TASK_BRANCH_CLAIM_STATES
    ):
        raise ValueError("task branch start claim has invalid fixed fields")
    if (version in TASK_BRANCH_SUPERSEDED_VERSIONS) != (payload["state"] == "superseded"):
        raise ValueError("task branch start claim has invalid supersede lifecycle")
    for name in (
        "repository",
        "worktree",
        "approvedSha256",
        "approvedReviewSha256",
        "taskStateSnapshotSha256",
    ):
        if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
            raise ValueError(f"task branch start claim has invalid {name}")
    if payload["approvedSha256"] != payload["taskStateSnapshotSha256"]:
        raise ValueError("task branch start claim task-state snapshot SHA256 mismatch")
    if not isinstance(payload["approvalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["approvalId"]):
        raise ValueError("task branch start claim has invalid approvalId")
    if payload["approvalIssue"] != normalized_issue(payload["approvalIssue"]):
        raise ValueError("task branch start claim has invalid approvalIssue")
    for name in (
        "branch",
        "baseBranch",
        "targetBranch",
        "approvedFile",
        "reviewerSummary",
        "approvedReviewFile",
        "taskStateSnapshotFile",
        "branchStartClaimFile",
        "reservedAt",
        "updatedAt",
        "recordedAt",
        "historyFile",
        "historySha256",
    ):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"task branch start claim has invalid {name}")
    if payload["branch"] != payload["baseBranch"] or payload["targetBranch"] == payload["baseBranch"]:
        raise ValueError("task branch start claim has invalid branch bindings")
    _canonical_utc_timestamp(payload["reservedAt"], "reservedAt", microseconds=True)
    _canonical_utc_timestamp(payload["updatedAt"], "updatedAt", microseconds=True)
    base_commit = payload["baseCommit"]
    if base_commit != "pending" and (not isinstance(base_commit, str) or not GIT_COMMIT_RE.fullmatch(base_commit)):
        raise ValueError("task branch start claim has invalid baseCommit")
    if version in TASK_BRANCH_BASE_SYNC_VERSIONS:
        pre_sync_base_commit = payload["preSyncBaseCommit"]
        if (
            not isinstance(pre_sync_base_commit, str)
            or not GIT_COMMIT_RE.fullmatch(pre_sync_base_commit)
        ):
            raise ValueError("task branch start claim has invalid preSyncBaseCommit")
    if payload["state"] != "reserved" and base_commit == "pending":
        if payload["state"] != "superseded":
            raise ValueError("task branch start claim is missing the exact base commit")
    history_names = ("recordedAt", "historyFile", "historySha256")
    history_values = tuple(payload[name] for name in history_names)
    if payload["state"] == "completed" or history_values != ("none", "none", "none"):
        if "none" in history_values:
            raise ValueError("task branch start claim has incomplete history identity")
        _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
        if not FINGERPRINT_RE.fullmatch(str(payload["historySha256"])):
            raise ValueError("task branch start claim has invalid historySha256")
    if payload["state"] in {"reserved", "branch-created", "superseded"} and history_values != ("none", "none", "none"):
        raise ValueError("task branch start claim records history before activation")
    if version in TASK_BRANCH_SUPERSEDED_VERSIONS:
        if not isinstance(payload["supersededAt"], str):
            raise ValueError("task branch start claim has invalid supersededAt")
        _canonical_utc_timestamp(payload["supersededAt"], "supersededAt", microseconds=True)
        reason = payload["supersededReason"]
        if not isinstance(reason, str) or not reason or len(reason) > 200 or reason != " ".join(reason.split()):
            raise ValueError("task branch start claim has invalid supersededReason")
        reject_credentials(reason)
    if raw_bytes != _task_branch_claim_bytes(payload):
        raise ValueError("task branch start claim bytes are not canonical")
    return payload


def _task_branch_grant(claim: dict[str, object]) -> ApprovalGrant:
    grant = ApprovalGrant(
        source="local-review",
        approval_id=str(claim["approvalId"]),
        repository=str(claim["repository"]),
        worktree=str(claim["worktree"]),
        branch=str(claim["branch"]),
        approval_issue=str(claim["approvalIssue"]),
        action="task-branch-start",
        approved_file=str(claim["approvedFile"]),
        approved_sha256=str(claim["approvedSha256"]),
        reviewer_summary=str(claim["reviewerSummary"]),
        target_branch=str(claim["targetBranch"]),
    )
    _validate_grant(grant)
    return grant


def _validate_task_branch_claim_grant(claim: dict[str, object], grant: ApprovalGrant) -> None:
    expected = {
        "approvalId": grant.approval_id,
        "repository": grant.repository,
        "worktree": grant.worktree,
        "branch": grant.branch,
        "baseBranch": grant.branch,
        "targetBranch": grant.target_branch,
        "approvalIssue": grant.approval_issue,
        "action": grant.action,
        "approvedFile": grant.approved_file,
        "approvedSha256": grant.approved_sha256,
        "reviewerSummary": grant.reviewer_summary,
    }
    if any(claim.get(name) != value for name, value in expected.items()):
        raise ValueError("task branch start claim does not match exact local approval")


def _task_branch_artifact_bytes(repo_root: Path, claim: dict[str, object], field_name: str, label: str) -> tuple[Path, bytes]:
    path = require_safe_repo_path(repo_root, repo_root / Path(str(claim[field_name])), label)
    content = _stable_approval_bytes(repo_root, path, issue_dir(repo_root, str(claim["approvalIssue"])), label)
    digest_field = "taskStateSnapshotSha256" if field_name == "taskStateSnapshotFile" else "approvedReviewSha256"
    if hashlib.sha256(content).hexdigest() != claim[digest_field]:
        raise ValueError(f"{label} SHA256 mismatch")
    return path, content


def _validate_task_branch_sealed_content(
    repo_root: Path,
    claim: dict[str, object],
    state_bytes: bytes,
    review_bytes: bytes,
) -> None:
    from .task_state import parse_task_state_text

    issue = str(claim["approvalIssue"])
    state_path = task_state_file(repo_root, issue).resolve()
    if display_path(repo_root, state_path) != claim["approvedFile"]:
        raise ValueError("task branch claim approved file is not the canonical task-state")
    try:
        state_text = state_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("approved task-state snapshot must be valid UTF-8") from exc
    state = parse_task_state_text(
        state_path,
        state_text,
        binding_mode="recorded",
        validate_acceptance=False,
    )
    if (
        state.issue != issue
        or state.classification == "ui-defect"
        or state.execution_state != "S2_REMOTE_ISSUE_CREATED"
        or state.semantic_phase not in {"classified", "declaring"}
        or state.base != claim["baseBranch"]
        or state.branch != claim["targetBranch"]
    ):
        raise ValueError("task branch claim does not seal exact approved task-state bindings")
    review_path = default_approval_file(repo_root, issue)
    review_text = _decode_approval_bytes(review_bytes, review_path, "approved task branch review")
    sealed_bindings = GitBindings(
        repository=str(claim["repository"]),
        worktree=str(claim["worktree"]),
        branch=str(claim["baseBranch"]),
    )
    _validate_review_text(
        repo_root,
        issue,
        state_path,
        None,
        review_text,
        sealed_bindings,
        str(claim["approvedSha256"]),
    )
    if (
        field(review_text, "Approval ID") != claim["approvalId"]
        or field(review_text, "Approved Action") != "task-branch-start"
        or safe_reviewer_summary(field(review_text, "Reviewer")) != claim["reviewerSummary"]
    ):
        raise ValueError("task branch claim does not seal the exact approval identity")


def _task_branch_reservation(
    repo_root: Path,
    claim_path: Path,
    claim: dict[str, object],
) -> TaskBranchStartReservation:
    issue = str(claim["approvalIssue"])
    approval_id = str(claim["approvalId"])
    if claim["state"] == "superseded":
        raise ValueError(f"task branch approval claim was superseded: {approval_id}")
    expected_claim = _task_branch_claim_path(repo_root, issue, approval_id)
    if claim_path.resolve() != expected_claim or claim["branchStartClaimFile"] != claim_path.relative_to(repo_root).as_posix():
        raise ValueError("task branch start claim path identity mismatch")
    state_path, state_bytes = _task_branch_artifact_bytes(
        repo_root,
        claim,
        "taskStateSnapshotFile",
        "approved task-state snapshot",
    )
    review_path, review_bytes = _task_branch_artifact_bytes(
        repo_root,
        claim,
        "approvedReviewFile",
        "approved task branch review",
    )
    if state_path != _task_branch_state_snapshot_path(repo_root, issue, approval_id):
        raise ValueError("task branch start claim task-state snapshot path mismatch")
    if review_path != _task_branch_review_path(repo_root, issue, approval_id):
        raise ValueError("task branch start claim review snapshot path mismatch")
    _validate_task_branch_sealed_content(repo_root, claim, state_bytes, review_bytes)
    return TaskBranchStartReservation(
        grant=_task_branch_grant(claim),
        claim_path=claim_path,
        task_state_snapshot_path=state_path,
        task_state_bytes=state_bytes,
        approved_review_path=review_path,
        approved_review_bytes=review_bytes,
        state=str(claim["state"]),
        base_commit=str(claim["baseCommit"]),
    )


def _task_branch_claims_for_target(
    repo_root: Path,
    issue: str,
    target_branch: str,
) -> list[tuple[Path, dict[str, object]]]:
    claims_root = _contract_artifact_path(repo_root, issue, "claims", "placeholder").parent
    if not claims_root.is_dir():
        return []
    matches: list[tuple[Path, dict[str, object]]] = []
    for claim_path in sorted(claims_root.glob("*-task-branch.yaml")):
        claim = _parse_task_branch_claim(repo_root, claim_path, issue)
        if claim["targetBranch"] != target_branch:
            continue
        expected_path = _task_branch_claim_path(repo_root, issue, str(claim["approvalId"]))
        expected_relative = claim_path.relative_to(repo_root).as_posix()
        if claim_path.resolve() != expected_path or claim["branchStartClaimFile"] != expected_relative:
            raise ValueError("task branch start claim path identity mismatch")
        if claim["state"] != "superseded":
            matches.append((claim_path, claim))
    return matches


def _revalidate_task_branch_current(repo_root: Path, reservation: TaskBranchStartReservation) -> None:
    from .local_artifacts import revalidate_snapshots

    issue = reservation.grant.approval_issue
    issue_root = issue_dir(repo_root, issue)
    state_path = resolve_path(repo_root, Path(reservation.grant.approved_file))
    state_snapshot = capture_stable_file(
        repo_root,
        state_path,
        issue_root,
        "task branch identity state",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    review_path = default_approval_file(repo_root, issue)
    review_snapshot = capture_stable_file(
        repo_root,
        review_path,
        issue_root,
        "task branch identity review",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    if state_snapshot.content != reservation.task_state_bytes:
        raise ValueError("exact approved task-state bytes changed before task branch effect")
    if review_snapshot.content != reservation.approved_review_bytes:
        raise ValueError("exact approved local-review bytes changed before task branch effect")
    revalidate_snapshots(repo_root, (state_snapshot, review_snapshot), "exact approved task branch snapshots")


def reserve_task_branch_start(repo_root: Path, grant: ApprovalGrant, base_branch: str) -> TaskBranchStartReservation:
    from .local_artifacts import revalidate_snapshots
    from .task_state import parse_task_state_text

    root = repo_root.resolve()
    _validate_grant(grant)
    if grant.source != "local-review" or grant.action != "task-branch-start" or grant.branch != base_branch:
        raise ValueError("task branch reservation requires the exact local branch-start approval")
    issue = grant.approval_issue
    issue_root = issue_dir(root, issue)
    claim_path = _task_branch_claim_path(root, issue, grant.approval_id)
    with _task_branch_claim_lock(root, grant.approval_id):
        active_claims = _task_branch_claims_for_target(root, issue, grant.target_branch)
        conflicts = [claim for _, claim in active_claims if claim["approvalId"] != grant.approval_id]
        if conflicts:
            raise ValueError(
                "existing task branch claim must complete or be human-superseded before a replacement approval"
            )
        if claim_path.is_file():
            claim = _parse_task_branch_claim(root, claim_path, issue)
            _validate_task_branch_claim_grant(claim, grant)
            if claim["state"] == "completed":
                raise ValueError(f"approval already consumed: {grant.approval_id}")
            reservation = _task_branch_reservation(root, claim_path, claim)
            _revalidate_task_branch_current(root, reservation)
            return reservation
        if _git_ref_commit(root, f"refs/heads/{grant.target_branch}"):
            raise ValueError(f"branch already exists without exact task branch claim: {grant.target_branch}")
        bindings = resolve_bindings(root)
        if bindings.repository != grant.repository or bindings.worktree != grant.worktree or bindings.branch != base_branch:
            raise ValueError("task branch reservation Git bindings changed after approval")
        pre_sync_base_commit = _git_ref_commit(root, f"refs/heads/{base_branch}")
        if not GIT_COMMIT_RE.fullmatch(pre_sync_base_commit):
            raise ValueError("cannot seal exact local task branch base commit")
        approved_path = resolve_path(root, Path(grant.approved_file))
        state_snapshot = capture_stable_file(
            root,
            approved_path,
            issue_root,
            "task branch identity state",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        state_bytes = state_snapshot.content or b""
        if hashlib.sha256(state_bytes).hexdigest() != grant.approved_sha256:
            raise ValueError("exact approved task-state bytes changed before reservation")
        try:
            state_text = state_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("task-state must be valid UTF-8") from exc
        state = parse_task_state_text(approved_path, state_text, binding_mode="recorded", validate_acceptance=False)
        if (
            state.issue != issue
            or state.branch != grant.target_branch
            or state.base != base_branch
            or state.classification == "ui-defect"
        ):
            raise ValueError("task branch reservation does not match exact task-state bindings")
        review_path = default_approval_file(root, issue)
        review_snapshot = capture_stable_file(
            root,
            review_path,
            issue_root,
            "approval review",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        review_bytes = review_snapshot.content or b""
        review_text = _decode_approval_bytes(review_bytes, review_path, "approval review")
        if field(review_text, "Approval ID") != grant.approval_id:
            raise ValueError("active local review no longer matches task branch approval")
        _validate_review_text(
            root,
            issue,
            approved_path,
            None,
            review_text,
            bindings,
            grant.approved_sha256,
        )
        revalidate_snapshots(root, (state_snapshot, review_snapshot), "task branch approval reservation")
        state_archive = _task_branch_state_snapshot_path(root, issue, grant.approval_id)
        review_archive = _task_branch_review_path(root, issue, grant.approval_id)
        _publish_exact_artifact(
            root,
            issue_root,
            state_archive,
            state_bytes,
            label="approved task-state snapshot",
            collision_message="approved task-state snapshot collision",
        )
        _publish_exact_artifact(
            root,
            issue_root,
            review_archive,
            review_bytes,
            label="approved task branch review",
            collision_message="approved task branch review collision",
        )
        now = _canonical_utc_now()
        claim: dict[str, object] = {
            "version": TASK_BRANCH_BASE_SYNC_CLAIM_VERSION,
            "approvalId": grant.approval_id,
            "repository": grant.repository,
            "worktree": grant.worktree,
            "branch": grant.branch,
            "baseBranch": base_branch,
            "targetBranch": grant.target_branch,
            "approvalIssue": issue,
            "action": "task-branch-start",
            "approvedFile": grant.approved_file,
            "approvedSha256": grant.approved_sha256,
            "reviewerSummary": grant.reviewer_summary,
            "approvedReviewFile": review_archive.relative_to(root).as_posix(),
            "approvedReviewSha256": hashlib.sha256(review_bytes).hexdigest(),
            "taskStateSnapshotFile": state_archive.relative_to(root).as_posix(),
            "taskStateSnapshotSha256": grant.approved_sha256,
            "branchStartClaimFile": claim_path.relative_to(root).as_posix(),
            "state": "reserved",
            "baseCommit": "pending",
            "preSyncBaseCommit": pre_sync_base_commit,
            "reservedAt": now,
            "updatedAt": now,
            "recordedAt": "none",
            "historyFile": "none",
            "historySha256": "none",
        }
        _write_immutable_bytes(claim_path, _task_branch_claim_bytes(claim), "task branch start claim collision")
        return _task_branch_reservation(root, claim_path, claim)


def resume_task_branch_start(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    target_branch: str,
    base_branch: str,
) -> TaskBranchStartReservation | None:
    root = repo_root.resolve()
    issue = normalized_issue(issue)
    requested_file = display_path(root, approved_file)
    matches = _task_branch_claims_for_target(root, issue, target_branch)
    for claim_path, claim in matches:
        expected = {
            "approvalIssue": issue,
            "approvedFile": requested_file,
            "baseBranch": base_branch,
            "branch": base_branch,
            "targetBranch": target_branch,
        }
        if any(claim.get(name) != value for name, value in expected.items()):
            raise ValueError("existing task branch claim does not match exact requested bindings")
    if len(matches) > 1:
        raise ValueError("multiple task branch claims match the requested target branch")
    if not matches:
        return None
    claim_path, claim = matches[0]
    if claim["state"] == "completed":
        raise ValueError(f"approval already consumed: {claim['approvalId']}")
    bindings = resolve_bindings(root)
    if claim["repository"] != bindings.repository or claim["worktree"] != bindings.worktree:
        raise ValueError("task branch claim does not match exact repository/worktree bindings")
    if bindings.branch not in {base_branch, target_branch}:
        raise ValueError("task branch recovery requires the exact base or target branch")
    reservation = _task_branch_reservation(root, claim_path, claim)
    _revalidate_task_branch_current(root, reservation)
    return reservation


def _task_branch_metadata(repo_root: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("slug", "issue", "base", "pr", "pr-url"):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", f"devctl.{key}"],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0 and result.stdout.strip():
            metadata[key] = result.stdout.strip()
        elif result.returncode not in {0, 1}:
            raise ValueError(f"cannot verify task branch metadata before supersede: {key}")
    return metadata


def _remote_task_branch_exists(repo_root: Path, branch: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return bool(result.stdout.strip())
    if result.returncode == 2:
        return False
    raise ValueError("cannot verify remote target branch before supersede")


def supersede_task_branch_start_by_id(
    repo_root: Path,
    issue: str,
    approval_id: str,
    *,
    reason: str,
    confirmation: str,
) -> Path:
    if confirmation != TASK_BRANCH_SUPERSEDE_CONFIRMATION:
        raise ValueError(
            "task branch supersede requires exact human confirmation: "
            f"{TASK_BRANCH_SUPERSEDE_CONFIRMATION}"
        )
    normalized = normalized_issue(issue)
    if not APPROVAL_ID_RE.fullmatch(approval_id):
        raise ValueError("task branch supersede requires a valid approval ID")
    summary = " ".join(str(reason).split())
    if not summary or len(summary) > 200:
        raise ValueError("task branch supersede requires a concise reason")
    reject_credentials(summary)
    root = repo_root.resolve()
    claim_path = _task_branch_claim_path(root, normalized, approval_id)
    with _task_branch_claim_lock(root, approval_id):
        claim = _parse_task_branch_claim(root, claim_path, normalized)
        if claim["state"] != "reserved":
            raise ValueError("task branch claim cannot be superseded after a branch effect")
        if any(claim[name] != "none" for name in ("recordedAt", "historyFile", "historySha256")):
            raise ValueError("task branch claim cannot be superseded after a history effect")
        if claim["version"] not in TASK_BRANCH_BASE_SYNC_VERSIONS:
            raise ValueError(
                "task branch claim cannot be superseded without a sealed pre-effect base identity"
            )
        current_base_commit = _git_ref_commit(root, f"refs/heads/{claim['baseBranch']}")
        if current_base_commit != claim["preSyncBaseCommit"]:
            raise ValueError("task branch claim cannot be superseded after a base-branch effect")
        target_branch = str(claim["targetBranch"])
        if _git_ref_commit(root, f"refs/heads/{target_branch}") or resolve_bindings(root).branch == target_branch:
            raise ValueError("task branch claim cannot be superseded after a branch effect")
        if _remote_task_branch_exists(root, target_branch):
            raise ValueError("task branch claim cannot be superseded after a remote branch effect")
        if _task_branch_metadata(root):
            raise ValueError("task branch claim cannot be superseded after a branch metadata effect")
        pointer = active_task_pointer_file(root, str(claim["worktree"]))
        authority = task_authority_file(root, str(claim["worktree"]), normalized)
        if pointer.exists() or authority.exists():
            raise ValueError("task branch claim cannot be superseded after a task activation effect")
        _, sealed_state = _task_branch_artifact_bytes(
            root,
            claim,
            "taskStateSnapshotFile",
            "approved task-state snapshot",
        )
        current_state = capture_stable_file(
            root,
            task_state_file(root, normalized),
            issue_dir(root, normalized),
            "current task-state before branch supersede",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        if current_state.content != sealed_state:
            raise ValueError("task branch claim cannot be superseded after a task-state effect")
        superseded_at = _canonical_utc_now()
        claim.update(
            {
                "version": TASK_BRANCH_BASE_SYNC_SUPERSEDED_VERSION,
                "state": "superseded",
                "updatedAt": superseded_at,
                "supersededAt": superseded_at,
                "supersededReason": summary,
            }
        )
        _replace_bytes(claim_path, _task_branch_claim_bytes(claim))
        return claim_path


def _reload_task_branch_reservation(
    repo_root: Path,
    reservation: TaskBranchStartReservation,
) -> tuple[dict[str, object], TaskBranchStartReservation]:
    claim = _parse_task_branch_claim(
        repo_root,
        reservation.claim_path,
        reservation.grant.approval_issue,
    )
    _validate_task_branch_claim_grant(claim, reservation.grant)
    current = _task_branch_reservation(repo_root, reservation.claim_path, claim)
    if (
        current.task_state_bytes != reservation.task_state_bytes
        or current.approved_review_bytes != reservation.approved_review_bytes
    ):
        raise ValueError("task branch claim sealed snapshot identity changed")
    return claim, current


def bind_task_branch_base(
    repo_root: Path,
    reservation: TaskBranchStartReservation,
    remote_base_commit: str,
) -> TaskBranchStartReservation:
    root = repo_root.resolve()
    sealed_commit = remote_base_commit.strip().lower()
    if not GIT_COMMIT_RE.fullmatch(sealed_commit):
        raise ValueError("cannot seal exact remote task branch base commit")
    with _task_branch_claim_lock(root, reservation.grant.approval_id):
        claim, current = _reload_task_branch_reservation(root, reservation)
        if claim["state"] != "reserved":
            raise ValueError("task branch base commit can only be bound while reserved")
        _revalidate_task_branch_current(root, current)
        bindings = resolve_bindings(root)
        if bindings.branch != claim["baseBranch"]:
            raise ValueError("task branch base binding requires the exact base branch")
        if claim["baseCommit"] not in {"pending", sealed_commit}:
            raise ValueError("task branch claim base commit mismatch")
        if claim["baseCommit"] == "pending":
            claim.update({"baseCommit": sealed_commit, "updatedAt": _canonical_utc_now()})
            _replace_bytes(reservation.claim_path, _task_branch_claim_bytes(claim))
        return _task_branch_reservation(root, reservation.claim_path, claim)


def revalidate_task_branch_start(
    repo_root: Path,
    reservation: TaskBranchStartReservation,
) -> TaskBranchStartReservation:
    root = repo_root.resolve()
    with _task_branch_claim_lock(root, reservation.grant.approval_id):
        claim, current = _reload_task_branch_reservation(root, reservation)
        _revalidate_task_branch_current(root, current)
        return _task_branch_reservation(root, reservation.claim_path, claim)


def mark_task_branch_created(
    repo_root: Path,
    reservation: TaskBranchStartReservation,
) -> TaskBranchStartReservation:
    root = repo_root.resolve()
    with _task_branch_claim_lock(root, reservation.grant.approval_id):
        claim, _ = _reload_task_branch_reservation(root, reservation)
        if claim["baseCommit"] == "pending":
            raise ValueError("task branch cannot be created before binding the exact base commit")
        target_commit = _git_ref_commit(root, f"refs/heads/{claim['targetBranch']}")
        if target_commit != claim["baseCommit"]:
            raise ValueError("task branch exact start point mismatch")
        if resolve_bindings(root).branch != claim["targetBranch"]:
            raise ValueError("task branch creation transition requires the exact target branch")
        if claim["state"] == "reserved":
            claim.update({"state": "branch-created", "updatedAt": _canonical_utc_now()})
            _replace_bytes(reservation.claim_path, _task_branch_claim_bytes(claim))
        elif claim["state"] not in {"branch-created", "activated"}:
            raise ValueError("task branch claim is not recoverable at branch creation")
        return _task_branch_reservation(root, reservation.claim_path, claim)


def mark_task_branch_activated(
    repo_root: Path,
    reservation: TaskBranchStartReservation,
) -> TaskBranchStartReservation:
    root = repo_root.resolve()
    with _task_branch_claim_lock(root, reservation.grant.approval_id):
        claim, _ = _reload_task_branch_reservation(root, reservation)
        if _git_ref_commit(root, f"refs/heads/{claim['targetBranch']}") != claim["baseCommit"]:
            raise ValueError("task branch exact start point mismatch")
        if resolve_bindings(root).branch != claim["targetBranch"]:
            raise ValueError("task branch activation transition requires the exact target branch")
        if claim["state"] == "branch-created":
            claim.update({"state": "activated", "updatedAt": _canonical_utc_now()})
            _replace_bytes(reservation.claim_path, _task_branch_claim_bytes(claim))
        elif claim["state"] != "activated":
            raise ValueError("task branch claim is not ready for activation")
        return _task_branch_reservation(root, reservation.claim_path, claim)


def _task_branch_history_payload(claim: dict[str, object]) -> dict[str, object]:
    payload = _record_payload(
        _task_branch_grant(claim),
        str(claim["approvalIssue"]),
        str(claim["recordedAt"]),
    )
    payload.update(
        {
            "targetBranch": claim["targetBranch"],
            "baseCommit": claim["baseCommit"],
            "approvedReviewFile": claim["approvedReviewFile"],
            "approvedReviewSha256": claim["approvedReviewSha256"],
            "taskStateSnapshotFile": claim["taskStateSnapshotFile"],
            "taskStateSnapshotSha256": claim["taskStateSnapshotSha256"],
            "branchStartClaimFile": claim["branchStartClaimFile"],
        }
    )
    return payload


def complete_task_branch_start(repo_root: Path, reservation: TaskBranchStartReservation) -> Path:
    root = repo_root.resolve()
    with _task_branch_claim_lock(root, reservation.grant.approval_id):
        claim, _ = _reload_task_branch_reservation(root, reservation)
        if claim["state"] not in {"activated", "completed"}:
            raise ValueError("task branch claim is not activated")
        if claim["recordedAt"] == "none":
            recorded_at = _canonical_utc_now()
            history_path = _history_path(
                root,
                str(claim["approvalIssue"]),
                "task-branch-start",
                recorded_at,
                str(claim["approvalId"]),
            )
            claim.update(
                {
                    "recordedAt": recorded_at,
                    "historyFile": history_path.relative_to(root).as_posix(),
                    "historySha256": "pending",
                    "updatedAt": recorded_at,
                }
            )
            history_bytes = _yaml_bytes(_task_branch_history_payload(claim))
            claim["historySha256"] = hashlib.sha256(history_bytes).hexdigest()
            _replace_bytes(reservation.claim_path, _task_branch_claim_bytes(claim))
        canonical_history_path = _history_path(
            root,
            str(claim["approvalIssue"]),
            "task-branch-start",
            str(claim["recordedAt"]),
            str(claim["approvalId"]),
        )
        canonical_history_file = canonical_history_path.relative_to(root).as_posix()
        if claim["historyFile"] != canonical_history_file:
            raise ValueError("task branch claim historyFile is not the canonical task branch approval history")
        history_path = require_safe_repo_path(
            root,
            root / Path(str(claim["historyFile"])),
            "task branch approval history",
        )
        if history_path != canonical_history_path:
            raise ValueError("task branch approval history path identity mismatch")
        history_bytes = _yaml_bytes(_task_branch_history_payload(claim))
        if hashlib.sha256(history_bytes).hexdigest() != claim["historySha256"]:
            raise ValueError("task branch claim does not seal exact history")
        if history_path.is_file():
            existing = _stable_approval_bytes(
                root,
                history_path,
                issue_dir(root, str(claim["approvalIssue"])),
                "task branch approval history",
            )
            if existing != history_bytes:
                raise ValueError("task branch approval history collision")
        else:
            _write_history_atomic(history_path, history_bytes.decode("utf-8"))
        if claim["state"] != "completed":
            claim.update({"state": "completed", "updatedAt": _canonical_utc_now()})
            _replace_bytes(reservation.claim_path, _task_branch_claim_bytes(claim))
        retire_live_review(root, str(claim["approvalIssue"]), str(claim["approvalId"]))
        return history_path


@contextmanager
def _remote_action_lock(repo_root: Path, approval_id: str) -> Iterator[None]:
    with _approval_claim_lock(
        repo_root,
        approval_id,
        "remote-approvals",
        "remote approval reservation is busy",
    ):
        yield


def _remote_claim_path(repo_root: Path, issue: str, approval_id: str, action: str | None = None) -> Path:
    return _history_artifact_path(
        repo_root,
        issue,
        "claims",
        f"{approval_id}-remote.yaml",
        action=action if action is not None else "git-push",
    )


def _find_remote_claim_path(repo_root: Path, issue: str, approval_id: str) -> Path:
    name = f"{approval_id}-remote.yaml"
    candidates = (
        _history_artifact_path(repo_root, issue, "claims", name, action="git-push"),
        _history_artifact_path(repo_root, issue, "claims", name, action="issue-create"),
    )
    existing = [path for path in candidates if path.is_file()]
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in existing:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    if len(unique) > 1:
        raise ValueError("duplicate remote approval claim")
    if unique:
        return unique[0]
    raise ValueError("remote approval claim not found")


def _remote_snapshot_path(repo_root: Path, issue: str, grant: ApprovalGrant) -> Path:
    return _history_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{grant.approval_id}-{grant.action}-approved.snapshot",
        action=grant.action,
    )


def _remote_review_path(repo_root: Path, issue: str, grant: ApprovalGrant) -> Path:
    return _history_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{grant.approval_id}-{grant.action}-local-review.md",
        action=grant.action,
    )


def _remote_claim_bytes(payload: dict[str, object]) -> bytes:
    return _yaml_bytes({name: payload[name] for name in REMOTE_CLAIM_FIELD_ORDER})


def _parse_remote_claim(repo_root: Path, path: Path, issue: str) -> dict[str, object]:
    owner = path.resolve().parents[3]
    raw_bytes = _stable_approval_bytes(repo_root, path, owner, "remote approval claim")
    payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "remote approval claim"))
    if not isinstance(payload, dict) or set(payload) != REMOTE_CLAIM_FIELDS:
        raise ValueError("remote approval claim has unexpected or missing fields")
    if payload["version"] != "0.1.0" or payload["state"] not in REMOTE_CLAIM_STATES:
        raise ValueError("remote approval claim has invalid fixed fields")
    for name in ("repository", "worktree", "approvedSha256", "approvedReviewSha256", "approvedSnapshotSha256"):
        if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
            raise ValueError(f"remote approval claim has invalid {name}")
    if payload["approvedSha256"] != payload["approvedSnapshotSha256"]:
        raise ValueError("remote approval claim snapshot SHA256 mismatch")
    if not isinstance(payload["approvalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["approvalId"]):
        raise ValueError("remote approval claim has invalid approvalId")
    if payload["approvalIssue"] != normalized_issue(payload["approvalIssue"]):
        raise ValueError("remote approval claim has invalid approvalIssue")
    if payload["action"] not in UNATTENDED_ACTIONS:
        raise ValueError("remote approval claim has invalid action")
    if type(payload["attempt"]) is not int or payload["attempt"] < 1:
        raise ValueError("remote approval claim has invalid attempt")
    for name in (
        "branch", "approvedFile", "reviewerSummary", "approvedReviewFile", "approvedSnapshotFile", "remoteClaimFile",
        "reservedAt", "updatedAt", "targetIssue", "providerReceipt", "failureReason", "recordedAt",
        "historyFile", "historySha256",
    ):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"remote approval claim has invalid {name}")
    _canonical_utc_timestamp(payload["reservedAt"], "reservedAt", microseconds=True)
    _canonical_utc_timestamp(payload["updatedAt"], "updatedAt", microseconds=True)
    if payload["state"] in {"post-effects-pending", "remote-confirmed", "completed"}:
        if payload["targetIssue"] == "none" or payload["providerReceipt"] == "none":
            raise ValueError("remote approval claim is missing provider confirmation")
        normalized_issue(payload["targetIssue"])
        _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
        if not FINGERPRINT_RE.fullmatch(payload["historySha256"]):
            raise ValueError("remote approval claim has invalid historySha256")
    elif any(payload[name] != "none" for name in ("targetIssue", "providerReceipt", "recordedAt", "historyFile", "historySha256")):
        raise ValueError("unconfirmed remote approval claim contains provider outcome fields")
    if raw_bytes != _remote_claim_bytes(payload):
        raise ValueError("remote approval claim bytes are not canonical")
    return payload


def issue_create_history_paths(repo_root: Path, issue: str) -> set[str]:
    """Allow only sealed draft evidence belonging to this Issue and worktree.

    This validates provenance, not permission to start a branch. The separate
    task-branch-start approval is still required. Never relocate sealed files.
    """
    root = repo_root.resolve()
    issue = normalized_issue(issue)
    bindings = resolve_bindings(root)
    allowed: set[str] = set()
    history_root = issue_dir(root, issue) / "approvals" / "history"
    for path in sorted(history_root.glob("*-issue-create-*.yaml")):
        record, content = _parse_history_snapshot(root, path)
        if record["source"] != "local-review" or record["approvalIssue"] != "draft":
            continue
        if (record["issue"] != issue or record["action"] != "issue-create"
                or record["repository"] != bindings.repository
                or record["worktree"] != bindings.worktree):
            raise ValueError("issue-create history binding mismatch")
        approval_id = str(record["approvalId"])
        claim_path = _remote_claim_path(root, "draft", approval_id, "issue-create")
        if record.get("remoteClaimFile") != claim_path.relative_to(root).as_posix():
            raise ValueError("issue-create history claim path mismatch")
        claim = _parse_remote_claim(root, claim_path, "draft")
        if (claim["state"] != "completed" or claim["targetIssue"] != issue
                or claim["action"] != "issue-create" or claim["approvalIssue"] != "draft"
                or claim["historyFile"] != path.relative_to(root).as_posix()
                or claim["historySha256"] != hashlib.sha256(content).hexdigest()
                or _remote_history_payload(claim) != record):
            raise ValueError("issue-create history does not match completed claim")
        receipt = json.loads(str(claim["providerReceipt"]))
        if not isinstance(receipt, dict) or str(receipt.get("number")) != issue:
            raise ValueError("issue-create history provider Issue mismatch")
        grant = _grant_from_remote_claim(claim)
        snapshot_path = _remote_snapshot_path(root, "draft", grant)
        review_path = _remote_review_path(root, "draft", grant)
        for key, expected in (("approvedSnapshotFile", snapshot_path),
                              ("approvedReviewFile", review_path)):
            if claim[key] != expected.relative_to(root).as_posix():
                raise ValueError("issue-create history artifact path mismatch")
        _remote_snapshot_from_claim(root, claim)
        review_bytes = _stable_approval_bytes(root, review_path, issue_dir(root, "draft"), "issue-create review")
        if hashlib.sha256(review_bytes).hexdigest() != claim["approvedReviewSha256"]:
            raise ValueError("issue-create history review SHA256 mismatch")
        text = _decode_approval_bytes(review_bytes, review_path, "issue-create review")
        expected_fields = {
            "Approval ID": approval_id, "Issue": "draft", "Approved": "yes",
            "Approved Action": "issue-create", "Repository ID": bindings.repository,
            "Worktree ID": bindings.worktree, "Branch": claim["branch"],
            "Approved File": claim["approvedFile"], "Approved SHA256": claim["approvedSha256"],
        }
        if any(field(text, name) != value for name, value in expected_fields.items()):
            raise ValueError("issue-create history review binding mismatch")
        allowed.update(p.relative_to(root).as_posix() for p in (claim_path, snapshot_path, review_path))
    return allowed


def _grant_from_remote_claim(claim: dict[str, object]) -> ApprovalGrant:
    grant = ApprovalGrant(
        source="local-review",
        approval_id=str(claim["approvalId"]),
        repository=str(claim["repository"]),
        worktree=str(claim["worktree"]),
        branch=str(claim["branch"]),
        approval_issue=str(claim["approvalIssue"]),
        action=str(claim["action"]),
        approved_file=str(claim["approvedFile"]),
        approved_sha256=str(claim["approvedSha256"]),
        reviewer_summary=str(claim["reviewerSummary"]),
    )
    _validate_grant(grant)
    return grant


def _validate_remote_claim_grant(claim: dict[str, object], grant: ApprovalGrant) -> None:
    expected = {
        "approvalId": grant.approval_id,
        "repository": grant.repository,
        "worktree": grant.worktree,
        "branch": grant.branch,
        "approvalIssue": grant.approval_issue,
        "action": grant.action,
        "approvedFile": grant.approved_file,
        "approvedSha256": grant.approved_sha256,
        "reviewerSummary": grant.reviewer_summary,
    }
    if any(claim.get(name) != value for name, value in expected.items()):
        raise ValueError("remote approval claim does not match exact local approval")


def _remote_snapshot_from_claim(repo_root: Path, claim: dict[str, object]) -> tuple[Path, bytes]:
    path = require_safe_repo_path(
        repo_root,
        repo_root / Path(str(claim["approvedSnapshotFile"])),
        "approved remote snapshot",
    )
    content = _stable_approval_bytes(repo_root, path, repo_root, "approved remote snapshot")
    if hashlib.sha256(content).hexdigest() != claim["approvedSnapshotSha256"]:
        raise ValueError("approved remote snapshot SHA256 mismatch")
    return path, content


def _reservation_from_claim(repo_root: Path, claim_path: Path, claim: dict[str, object]) -> RemoteActionReservation:
    grant = _grant_from_remote_claim(claim)
    snapshot_path, approved_bytes = _remote_snapshot_from_claim(repo_root, claim)
    return RemoteActionReservation(
        grant=grant,
        claim_path=claim_path,
        approved_snapshot_path=snapshot_path,
        approved_bytes=approved_bytes,
        attempt=int(claim["attempt"]),
        provider_required=claim["state"] not in {"post-effects-pending", "remote-confirmed"},
        target_issue="" if claim["targetIssue"] == "none" else str(claim["targetIssue"]),
        provider_receipt="" if claim["providerReceipt"] == "none" else str(claim["providerReceipt"]),
    )


def _remote_claims_for_scope(
    repo_root: Path,
    issue: str,
    action: str,
    bindings: GitBindings,
) -> list[tuple[Path, dict[str, object]]]:
    claim_roots = {
        _history_artifact_path(repo_root, issue, "claims", "placeholder", action=action).parent,
        _history_artifact_path(repo_root, issue, "claims", "placeholder", action="issue-create").parent,
    }
    scoped: list[tuple[Path, dict[str, object]]] = []
    seen: set[Path] = set()
    for claims_root in claim_roots:
        if not claims_root.is_dir():
            continue
        for claim_path in sorted(claims_root.glob("*-remote.yaml")):
            resolved = claim_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            claim = _parse_remote_claim(repo_root, claim_path, issue)
            scope_matches = (
                claim["repository"] == bindings.repository
                and claim["worktree"] == bindings.worktree
                and claim["branch"] == bindings.branch
                and claim["approvalIssue"] == issue
                and claim["action"] == action
            )
            if not scope_matches:
                continue
            expected_paths = {
                _remote_claim_path(repo_root, issue, str(claim["approvalId"]), action),
                _remote_claim_path(repo_root, issue, str(claim["approvalId"]), "issue-create"),
            }
            expected_relative = claim_path.relative_to(repo_root).as_posix()
            if claim_path.resolve() not in {path.resolve() for path in expected_paths} or claim["remoteClaimFile"] != expected_relative:
                raise ValueError("remote approval claim path identity mismatch")
            scoped.append((claim_path, claim))
    return scoped


def _arbitrate_remote_claim_scope(
    repo_root: Path,
    issue: str,
    action: str,
    approved_file: str,
    bindings: GitBindings,
) -> tuple[Path, dict[str, object]] | None:
    claims = _remote_claims_for_scope(repo_root, issue, action, bindings)
    unresolved = [
        (path, claim)
        for path, claim in claims
        if claim["state"] in {"reserved", "outcome-unknown"}
    ]
    if unresolved:
        identities = ", ".join(str(claim["approvalId"]) for _, claim in unresolved)
        raise ValueError(
            "remote approval outcome unresolved; scope-wide human reconciliation is required: "
            f"{identities}"
        )
    confirmed = [
        (path, claim)
        for path, claim in claims
        if claim["state"] in {"post-effects-pending", "remote-confirmed"}
    ]
    if len(confirmed) > 1:
        if action == "git-mr":
            raise ValueError("multiple matching pending git-mr claims")
        raise ValueError(f"multiple provider-confirmed remote approval claims for {action}")
    if not confirmed:
        return None
    claim_path, claim = confirmed[0]
    if claim["approvedFile"] != approved_file:
        if action == "git-mr":
            raise ValueError("conflicting pending git-mr claim uses a different approved file")
        raise ValueError(f"provider-confirmed {action} claim uses a different approved file")
    return claim_path, claim


def resume_pending_remote_action(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
) -> RemoteActionReservation | None:
    root = repo_root.resolve()
    normalized = normalized_issue(issue)
    if action != "git-mr":
        raise ValueError("deferred remote recovery is only supported for git-mr")
    requested_file = display_path(root, approved_file)
    bindings = resolve_bindings(root)
    pending = _arbitrate_remote_claim_scope(
        root,
        normalized,
        action,
        requested_file,
        bindings,
    )
    if pending is None:
        return None
    claim_path, claim = pending
    return _reservation_from_claim(root, claim_path, claim)


def reserve_remote_action(repo_root: Path, grant: ApprovalGrant) -> RemoteActionReservation:
    from .local_artifacts import revalidate_snapshots

    root = repo_root.resolve()
    _validate_grant(grant)
    if grant.source != "local-review" or grant.action not in UNATTENDED_ACTIONS:
        raise ValueError("persistent remote reservation requires an ordinary local-review remote action")
    issue = normalized_issue(grant.approval_issue)
    claim_path = _remote_claim_path(root, issue, grant.approval_id, grant.action)
    with _remote_action_lock(root, grant.approval_id):
        pending = _arbitrate_remote_claim_scope(
            root,
            issue,
            grant.action,
            grant.approved_file,
            resolve_bindings(root),
        )
        if pending is not None:
            pending_path, pending_claim = pending
            if pending_claim["approvalId"] != grant.approval_id:
                raise ValueError(
                    "provider-confirmed remote approval claim must finish before a replacement approval"
                )
            _validate_remote_claim_grant(pending_claim, grant)
            return _reservation_from_claim(root, pending_path, pending_claim)
        if claim_path.is_file():
            claim = _parse_remote_claim(root, claim_path, issue)
            _validate_remote_claim_grant(claim, grant)
            if claim["state"] == "completed":
                raise ValueError(f"approval already consumed: {grant.approval_id}")
            if claim["state"] in {"post-effects-pending", "remote-confirmed"}:
                return _reservation_from_claim(root, claim_path, claim)
            if claim["state"] != "retryable":
                raise ValueError(f"remote approval outcome unresolved: {grant.approval_id}")
        else:
            claim = None

        issue_root = issue_dir(root, issue)
        receipt_root = _history_workspace_root(root, issue, grant.action)
        approved_path = resolve_path(root, Path(grant.approved_file))
        approved_snapshot = capture_stable_file(
            root,
            approved_path,
            root,
            "approved remote file",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        approved_bytes = approved_snapshot.content or b""
        if hashlib.sha256(approved_bytes).hexdigest() != grant.approved_sha256:
            raise ValueError("approved remote file changed before reservation")
        review_path = default_approval_file(root, issue)
        review_snapshot = capture_stable_file(
            root,
            review_path,
            issue_root,
            "approval review",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        review_bytes = review_snapshot.content or b""
        review_text = _decode_approval_bytes(review_bytes, review_path, "approval review")
        if field(review_text, "Approval ID") != grant.approval_id or field(review_text, "Approved Action") != grant.action:
            raise ValueError("active local review no longer matches remote approval")
        _validate_review_text(
            root,
            issue,
            approved_path,
            None,
            review_text,
            resolve_bindings(root),
            grant.approved_sha256,
        )
        revalidate_snapshots(root, (approved_snapshot, review_snapshot), "remote approval reservation")
        snapshot_path = _remote_snapshot_path(root, issue, grant)
        archived_review = _remote_review_path(root, issue, grant)
        _publish_exact_artifact(
            root,
            receipt_root,
            snapshot_path,
            approved_bytes,
            label="approved remote snapshot",
            collision_message="approved remote snapshot collision",
        )
        _publish_exact_artifact(
            root,
            receipt_root,
            archived_review,
            review_bytes,
            label="approved remote review",
            collision_message="approved remote review collision",
        )
        now = _canonical_utc_now()
        payload: dict[str, object] = {
            "version": "0.1.0",
            "approvalId": grant.approval_id,
            "repository": grant.repository,
            "worktree": grant.worktree,
            "branch": grant.branch,
            "approvalIssue": issue,
            "action": grant.action,
            "approvedFile": grant.approved_file,
            "approvedSha256": grant.approved_sha256,
            "reviewerSummary": grant.reviewer_summary,
            "approvedReviewFile": archived_review.relative_to(root).as_posix(),
            "approvedReviewSha256": hashlib.sha256(review_bytes).hexdigest(),
            "approvedSnapshotFile": snapshot_path.relative_to(root).as_posix(),
            "approvedSnapshotSha256": grant.approved_sha256,
            "remoteClaimFile": claim_path.relative_to(root).as_posix(),
            "state": "reserved",
            "attempt": 1 if claim is None else int(claim["attempt"]) + 1,
            "reservedAt": now if claim is None else claim["reservedAt"],
            "updatedAt": now,
            "targetIssue": "none",
            "providerReceipt": "none",
            "failureReason": "none",
            "recordedAt": "none",
            "historyFile": "none",
            "historySha256": "none",
        }
        if claim is None:
            _write_immutable_bytes(claim_path, _remote_claim_bytes(payload), "remote approval claim collision")
        else:
            _replace_bytes(claim_path, _remote_claim_bytes(payload))
        return _reservation_from_claim(root, claim_path, payload)


def mark_remote_action_unknown(
    repo_root: Path,
    reservation: RemoteActionReservation,
    reason: str,
) -> None:
    root = repo_root.resolve()
    summary = " ".join(str(reason).split())[:200] or "provider outcome unknown"
    reject_credentials(summary)
    with _remote_action_lock(root, reservation.grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, reservation.grant.approval_issue)
        _validate_remote_claim_grant(claim, reservation.grant)
        if claim["state"] != "reserved" or claim["attempt"] != reservation.attempt:
            raise ValueError("remote approval reservation is not active")
        claim.update({"state": "outcome-unknown", "failureReason": summary, "updatedAt": _canonical_utc_now()})
        _replace_bytes(reservation.claim_path, _remote_claim_bytes(claim))


def mark_remote_action_retryable(
    repo_root: Path,
    reservation: RemoteActionReservation,
    reason: str,
) -> None:
    root = repo_root.resolve()
    summary = " ".join(str(reason).split())[:200] or "no remote effect"
    reject_credentials(summary)
    with _remote_action_lock(root, reservation.grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, reservation.grant.approval_issue)
        _validate_remote_claim_grant(claim, reservation.grant)
        if claim["state"] != "reserved" or claim["attempt"] != reservation.attempt:
            raise ValueError("remote approval reservation is not active")
        claim.update({"state": "retryable", "failureReason": summary, "updatedAt": _canonical_utc_now()})
        _replace_bytes(reservation.claim_path, _remote_claim_bytes(claim))


def reconcile_remote_action(
    repo_root: Path,
    grant: ApprovalGrant,
    *,
    outcome: Literal["no-effect", "success"],
    confirmation: str,
    target_issue: str | None = None,
    provider_receipt: dict[str, object] | None = None,
) -> RemoteActionReservation | Path:
    if confirmation != REMOTE_RECONCILIATION_CONFIRMATION:
        raise ValueError(
            "remote reconciliation requires exact human confirmation: "
            f"{REMOTE_RECONCILIATION_CONFIRMATION}"
        )
    root = repo_root.resolve()
    claim_path = _find_remote_claim_path(root, grant.approval_issue, grant.approval_id)
    with _remote_action_lock(root, grant.approval_id):
        claim = _parse_remote_claim(root, claim_path, grant.approval_issue)
        _validate_remote_claim_grant(claim, grant)
        if claim["state"] not in {"reserved", "outcome-unknown"}:
            raise ValueError("remote approval claim is not awaiting reconciliation")
        if outcome == "no-effect":
            claim.update(
                {
                    "state": "retryable",
                    "failureReason": "human confirmed no remote effect",
                    "updatedAt": _canonical_utc_now(),
                }
            )
            _replace_bytes(claim_path, _remote_claim_bytes(claim))
            return _reservation_from_claim(root, claim_path, claim)
    reservation = _reservation_from_claim(root, claim_path, claim)
    confirmed = confirm_remote_action(
        root,
        reservation,
        target_issue=target_issue,
        provider_receipt=provider_receipt or {},
        reconciled=True,
    )
    if grant.action == "git-mr":
        return confirmed
    return complete_remote_action(root, confirmed)


def reconcile_remote_action_by_id(
    repo_root: Path,
    issue: str,
    approval_id: str,
    *,
    outcome: Literal["no-effect", "success"],
    confirmation: str,
    target_issue: str | None = None,
    provider_receipt: dict[str, object] | None = None,
) -> RemoteActionReservation | Path:
    normalized = normalized_issue(issue)
    if not APPROVAL_ID_RE.fullmatch(approval_id):
        raise ValueError("remote reconciliation requires a valid approval ID")
    root = repo_root.resolve()
    claim_path = _find_remote_claim_path(root, normalized, approval_id)
    with _remote_action_lock(root, approval_id):
        claim = _parse_remote_claim(root, claim_path, normalized)
        grant = _grant_from_remote_claim(claim)
    return reconcile_remote_action(
        root,
        grant,
        outcome=outcome,
        confirmation=confirmation,
        target_issue=target_issue,
        provider_receipt=provider_receipt,
    )


def _canonical_provider_receipt(receipt: dict[str, object]) -> str:
    if not isinstance(receipt, dict) or not receipt:
        raise ValueError("provider receipt must be a non-empty mapping")
    text = json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    reject_credentials(text)
    if len(text) > 4096:
        raise ValueError("provider receipt is too large")
    return text


def _remote_history_payload(claim: dict[str, object]) -> dict[str, object]:
    grant = _grant_from_remote_claim(claim)
    payload = _record_payload(grant, str(claim["targetIssue"]), str(claim["recordedAt"]))
    payload.update(
        {
            "approvedReviewFile": claim["approvedReviewFile"],
            "approvedReviewSha256": claim["approvedReviewSha256"],
            "approvedSnapshotFile": claim["approvedSnapshotFile"],
            "approvedSnapshotSha256": claim["approvedSnapshotSha256"],
            "remoteClaimFile": claim["remoteClaimFile"],
            "providerReceipt": claim["providerReceipt"],
        }
    )
    return payload


def confirm_remote_action(
    repo_root: Path,
    reservation: RemoteActionReservation,
    *,
    target_issue: str | None,
    provider_receipt: dict[str, object],
    reconciled: bool = False,
) -> RemoteActionReservation:
    root = repo_root.resolve()
    grant = reservation.grant
    target = _record_target_issue(grant, target_issue)
    receipt = _canonical_provider_receipt(provider_receipt)
    with _remote_action_lock(root, grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, grant.approval_issue)
        _validate_remote_claim_grant(claim, grant)
        allowed = {"reserved", "outcome-unknown"} if reconciled else {"reserved"}
        if claim["state"] not in allowed or claim["attempt"] != reservation.attempt:
            raise ValueError("remote approval reservation is not active")
        recorded_at = _canonical_utc_now()
        history_path = _history_path(root, target, grant.action, recorded_at, grant.approval_id)
        claim.update(
            {
                "state": "post-effects-pending" if grant.action == "git-mr" else "remote-confirmed",
                "targetIssue": target,
                "providerReceipt": receipt,
                "failureReason": "none",
                "recordedAt": recorded_at,
                "historyFile": history_path.relative_to(root).as_posix(),
                "updatedAt": recorded_at,
            }
        )
        history_bytes = _yaml_bytes(_remote_history_payload(claim))
        claim["historySha256"] = hashlib.sha256(history_bytes).hexdigest()
        _replace_bytes(reservation.claim_path, _remote_claim_bytes(claim))
        return _reservation_from_claim(root, reservation.claim_path, claim)


def _publish_remote_action_history(root: Path, claim: dict[str, object]) -> Path:
    history_path = require_safe_repo_path(root, root / Path(str(claim["historyFile"])), "remote approval history")
    action = str(claim["action"])
    target = str(claim["targetIssue"])
    expected_parents = {
        _history_workspace_root(root, target, action).resolve() / "approvals" / "history",
        issue_dir(root, target).resolve() / "approvals" / "history",
    }
    if history_path.parent not in expected_parents:
        raise ValueError("remote approval history path does not match target Issue")
    history_bytes = _yaml_bytes(_remote_history_payload(claim))
    if hashlib.sha256(history_bytes).hexdigest() != claim["historySha256"]:
        raise ValueError("remote approval claim does not seal exact history")
    _publish_exact_artifact(
        root,
        history_path.parents[2],
        history_path,
        history_bytes,
        label="remote approval history",
        collision_message="remote approval history collision",
    )
    return history_path


def publish_remote_action_history(repo_root: Path, reservation: RemoteActionReservation) -> Path:
    root = repo_root.resolve()
    grant = reservation.grant
    with _remote_action_lock(root, grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, grant.approval_issue)
        _validate_remote_claim_grant(claim, grant)
        if claim["state"] not in {"post-effects-pending", "remote-confirmed"}:
            raise ValueError("remote approval has not been provider-confirmed")
        return _publish_remote_action_history(root, claim)


def mark_remote_post_effects_complete(
    repo_root: Path,
    reservation: RemoteActionReservation,
) -> RemoteActionReservation:
    root = repo_root.resolve()
    grant = reservation.grant
    if grant.action != "git-mr":
        raise ValueError("deferred remote post-effects are only supported for git-mr")
    with _remote_action_lock(root, grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, grant.approval_issue)
        _validate_remote_claim_grant(claim, grant)
        if claim["state"] == "remote-confirmed":
            return _reservation_from_claim(root, reservation.claim_path, claim)
        if claim["state"] != "post-effects-pending":
            raise ValueError("remote approval post-effects are not pending")
        history_path = require_safe_repo_path(root, root / Path(str(claim["historyFile"])), "remote approval history")
        history_bytes = _stable_approval_bytes(
            root,
            history_path,
            history_path.parents[2],
            "remote approval history",
        )
        if hashlib.sha256(history_bytes).hexdigest() != claim["historySha256"]:
            raise ValueError("remote approval post-effects require exact published history")
        effects = [
            record
            for record in _history_records(root)
            if record["source"] == "effect"
            and record["action"] == EFFECT_ACTION
            and record["parentApprovalId"] == grant.approval_id
            and record["result"] == "success"
        ]
        if len(effects) != 1:
            raise ValueError("remote approval post-effects require exactly one git-state-backfill effect")
        claim.update({"state": "remote-confirmed", "updatedAt": _canonical_utc_now()})
        _replace_bytes(reservation.claim_path, _remote_claim_bytes(claim))
        return _reservation_from_claim(root, reservation.claim_path, claim)


def complete_remote_action(repo_root: Path, reservation: RemoteActionReservation) -> Path:
    root = repo_root.resolve()
    grant = reservation.grant
    with _remote_action_lock(root, grant.approval_id):
        claim = _parse_remote_claim(root, reservation.claim_path, grant.approval_issue)
        _validate_remote_claim_grant(claim, grant)
        if claim["state"] == "completed":
            retire_live_review(root, grant.approval_issue, grant.approval_id)
            raise ValueError(f"approval already consumed: {grant.approval_id}")
        if claim["state"] == "post-effects-pending":
            raise ValueError("remote approval post-effects are not complete")
        if claim["state"] != "remote-confirmed":
            raise ValueError("remote approval has not been provider-confirmed")
        history_path = _publish_remote_action_history(root, claim)
        claim.update({"state": "completed", "updatedAt": _canonical_utc_now()})
        _replace_bytes(reservation.claim_path, _remote_claim_bytes(claim))
        retire_live_review(root, grant.approval_issue, grant.approval_id)
        return history_path


def _validate_acceptance_chronology(review_bytes: bytes, review_path: Path, claim: dict[str, object]) -> None:
    review_text = _decode_approval_bytes(review_bytes, review_path, "archived approved review")
    approved_at = _canonical_utc_timestamp(field(review_text, "Approved At"), "Approved At", microseconds=False)
    claimed_at = _canonical_utc_timestamp(claim["claimedAt"], "claimedAt", microseconds=True)
    recorded_at = _canonical_utc_timestamp(claim["recordedAt"], "recordedAt", microseconds=True)
    if not approved_at <= claimed_at <= recorded_at:
        raise ValueError("approval chronology must satisfy Approved At <= claimedAt <= recordedAt")


def _finalize_contract_acceptance(
    repo_root: Path,
    issue: str,
    grant: ApprovalGrant,
    review_bytes: bytes,
    contract_bytes: bytes,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    normalized_objects: tuple[str, ...],
) -> Path:
    issue_root = issue_dir(repo_root, issue)
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()
    snapshot_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    if snapshot_sha256 != contract_sha256:
        raise ValueError("contract snapshot bytes do not match accepted contract SHA256")
    claim_path = _contract_artifact_path(repo_root, issue, "claims", f"{grant.approval_id}.yaml")
    archived_review = _contract_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{grant.approval_id}-local-review.md",
    )
    archived_contract = _contract_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{grant.approval_id}-contract.yaml",
    )
    approved_review_file = archived_review.relative_to(issue_root).as_posix()
    contract_snapshot_file = archived_contract.relative_to(issue_root).as_posix()
    approval_claim_file = claim_path.relative_to(issue_root).as_posix()
    claim_preexisting = claim_path.is_file()

    if claim_preexisting:
        try:
            claim, _ = _parse_contract_claim(repo_root, claim_path, issue_root)
        except ValueError as exc:
            raise ValueError(f"contract acceptance claim replay or tampering: {exc}") from exc
        expected_claim = {
            "approvalId": grant.approval_id,
            "repository": grant.repository,
            "worktree": grant.worktree,
            "branch": grant.branch,
            "issue": issue,
            "action": grant.action,
            "approvedFile": grant.approved_file,
            "approvedSha256": grant.approved_sha256,
            "contractId": contract_id,
            "contractVersion": contract_version,
            "contractSha256": contract_sha256,
            "acceptedObjects": list(normalized_objects),
            "semanticDecision": "accepted-design",
            "approvedReviewFile": approved_review_file,
            "approvedReviewSha256": review_sha256,
            "contractSnapshotFile": contract_snapshot_file,
            "contractSnapshotSha256": snapshot_sha256,
        }
        if any(claim.get(name) != value for name, value in expected_claim.items()):
            raise ValueError("contract acceptance claim replay or tampering")
        recorded_at = str(claim["recordedAt"])
        history_file = _history_path(repo_root, issue, grant.action, recorded_at, grant.approval_id)
        history_relative = history_file.relative_to(issue_root).as_posix()
        if claim["historyFile"] != history_relative:
            raise ValueError("contract acceptance claim replay or tampering")
        history_payload = _contract_history_payload(
            grant,
            issue,
            recorded_at,
            contract_id=contract_id,
            contract_version=contract_version,
            contract_sha256=contract_sha256,
            accepted_objects=normalized_objects,
            approved_review_file=approved_review_file,
            approved_review_sha256=review_sha256,
            contract_snapshot_file=contract_snapshot_file,
            contract_snapshot_sha256=snapshot_sha256,
            approval_claim_file=approval_claim_file,
        )
        history_bytes = _yaml_bytes(history_payload)
        if claim["historySha256"] != hashlib.sha256(history_bytes).hexdigest():
            raise ValueError("contract acceptance claim replay or tampering")
        _validate_acceptance_chronology(review_bytes, archived_review, claim)
    else:
        claimed_at = _canonical_utc_now()
        recorded_at = claimed_at
        history_file = _history_path(repo_root, issue, grant.action, recorded_at, grant.approval_id)
        history_relative = history_file.relative_to(issue_root).as_posix()
        history_payload = _contract_history_payload(
            grant,
            issue,
            recorded_at,
            contract_id=contract_id,
            contract_version=contract_version,
            contract_sha256=contract_sha256,
            accepted_objects=normalized_objects,
            approved_review_file=approved_review_file,
            approved_review_sha256=review_sha256,
            contract_snapshot_file=contract_snapshot_file,
            contract_snapshot_sha256=snapshot_sha256,
            approval_claim_file=approval_claim_file,
        )
        history_bytes = _yaml_bytes(history_payload)
        claim = _contract_claim_payload(
            grant,
            issue,
            contract_id=contract_id,
            contract_version=contract_version,
            contract_sha256=contract_sha256,
            accepted_objects=normalized_objects,
            approved_review_file=approved_review_file,
            approved_review_sha256=review_sha256,
            contract_snapshot_file=contract_snapshot_file,
            contract_snapshot_sha256=snapshot_sha256,
            claimed_at=claimed_at,
            recorded_at=recorded_at,
            history_file=history_relative,
            history_sha256=hashlib.sha256(history_bytes).hexdigest(),
        )
        _validate_acceptance_chronology(review_bytes, archived_review, claim)
        claim_bytes = _yaml_bytes(claim)
        try:
            _write_immutable_bytes(claim_path, claim_bytes, "contract acceptance claim collision")
        except ValueError as exc:
            if "claim collision" not in str(exc) or not claim_path.is_file():
                raise
            existing_claim = _stable_approval_bytes(repo_root, claim_path, issue_root, "contract acceptance claim")
            if existing_claim != claim_bytes:
                raise ValueError(f"approval already claimed: {grant.approval_id}") from None

    complete_before_retry = (
        claim_preexisting
        and archived_review.is_file()
        and archived_contract.is_file()
        and history_file.is_file()
    )
    _publish_exact_artifact(
        repo_root,
        issue_root,
        archived_contract,
        contract_bytes,
        label="archived contract snapshot",
        collision_message="archived contract snapshot collision",
    )
    _publish_exact_artifact(
        repo_root,
        issue_root,
        archived_review,
        review_bytes,
        label="archived review",
        collision_message="archived contract approval collision",
    )
    _publish_exact_artifact(
        repo_root,
        issue_root,
        history_file,
        history_bytes,
        label="acceptance history",
        collision_message="consumed approval history collision",
    )
    if complete_before_retry:
        raise ValueError(f"approval already consumed: {grant.approval_id}")
    return history_file


def consume_contract_acceptance(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    accepted_objects: tuple[str, ...],
    contract_bytes: bytes,
) -> Path:
    repo_root = repo_root.resolve()
    issue = normalized_issue(issue)
    if not FINGERPRINT_RE.fullmatch(contract_sha256):
        raise ValueError("contract acceptance requires a valid contract SHA256")
    if not isinstance(contract_id, str) or not contract_id or not isinstance(contract_version, str) or not contract_version:
        raise ValueError("contract acceptance requires contract ID and version")
    normalized_objects = normalize_accepted_objects(accepted_objects)
    if normalized_objects != accepted_objects:
        raise ValueError("contract acceptance requires a normalized accepted object set")
    if not isinstance(contract_bytes, bytes) or hashlib.sha256(contract_bytes).hexdigest() != contract_sha256:
        raise ValueError("contract acceptance requires exact contract snapshot bytes")
    grant, _, review_bytes = _contract_local_grant(
        repo_root,
        approved_file,
        issue,
        expected_sha256=contract_sha256,
        expected_objects=normalized_objects,
    )
    claim_path = _contract_artifact_path(repo_root, issue, "claims", f"{grant.approval_id}.yaml")
    with _contract_claim_lock(repo_root, grant.approval_id):
        try:
            history = _finalize_contract_acceptance(
                repo_root,
                issue,
                grant,
                review_bytes,
                contract_bytes,
                contract_id=contract_id,
                contract_version=contract_version,
                contract_sha256=contract_sha256,
                normalized_objects=normalized_objects,
            )
        except ValueError as exc:
            if str(exc).startswith("approval already consumed:"):
                retire_live_review(repo_root, issue, grant.approval_id)
            raise
        retire_live_review(repo_root, issue, grant.approval_id)
        return history


@contextmanager
def _gap_recognition_lock(repo_root: Path, approval_id: str) -> Iterator[None]:
    bindings = resolve_bindings(repo_root)
    common_dir = git_path(repo_root, "--git-common-dir")
    lock_path = (
        common_dir
        / "xflow"
        / "runtime"
        / "gap-recognition"
        / bindings.worktree
        / f"{approval_id}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError(f"approval already claimed: {approval_id}") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        lock_path.unlink(missing_ok=True)


def _gap_local_grant(
    repo_root: Path,
    issue: str,
    gap_path: Path,
    expected_sha256: str,
) -> tuple[ApprovalGrant, bytes]:
    issue_root = issue_dir(repo_root, issue)
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    review_bytes = _stable_approval_bytes(repo_root, review_file, issue_root, "approval review")
    text = _decode_approval_bytes(review_bytes, review_file, "approval review")
    if field(text, "Approved Action") != "gap-recognition":
        raise ValueError(
            f"action mismatch: expected exact gap-recognition, got {field(text, 'Approved Action')}"
        )
    bindings = resolve_bindings(repo_root)
    approved_path, approved_hash = _validate_review_text(
        repo_root,
        issue,
        gap_path,
        None,
        text,
        bindings,
        expected_sha256,
    )
    check_reviewed_task_binding(repo_root, issue, "gap-recognition")
    grant = ApprovalGrant(
        source="local-review",
        approval_id=field(text, "Approval ID"),
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        approval_issue=issue,
        action="gap-recognition",
        approved_file=display_path(repo_root, approved_path),
        approved_sha256=approved_hash,
        reviewer_summary=safe_reviewer_summary(field(text, "Reviewer")),
    )
    _validate_grant(grant)
    return grant, review_bytes


def consume_gap_recognition(repo_root: Path, issue: str, gap_file: Path) -> Path:
    from .checks import validate_gap_analysis_snapshot
    from .local_artifacts import revalidate_snapshots

    root = repo_root.resolve()
    issue = normalized_issue(issue)
    issue_root = issue_dir(root, issue)
    expected_gap = issue_root / "gap-analysis.md"
    gap_path = resolve_path(root, gap_file)
    if gap_path != expected_gap:
        raise ValueError("gap recognition requires the canonical Issue gap-analysis.md")
    gap_snapshot = capture_stable_file(
        root,
        gap_path,
        issue_root,
        "gap analysis",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    supporting = validate_gap_analysis_snapshot(root, issue, gap_snapshot)
    gap_bytes = gap_snapshot.content or b""
    gap_sha256 = hashlib.sha256(gap_bytes).hexdigest()
    grant, review_bytes = _gap_local_grant(root, issue, gap_path, gap_sha256)
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()

    with _gap_recognition_lock(root, grant.approval_id):
        reject_consumed_approval(root, grant.approval_id)
        revalidate_snapshots(root, (gap_snapshot, *supporting), "gap recognition")
        recorded_at = _canonical_utc_now()
        history_file = _history_path(root, issue, "gap-recognition", recorded_at, grant.approval_id)
        archived_review = _contract_artifact_path(
            root,
            issue,
            "consumed",
            f"{grant.approval_id}-gap-recognition-local-review.md",
        )
        archived_gap = _contract_artifact_path(
            root,
            issue,
            "consumed",
            f"{grant.approval_id}-gap-analysis.md",
        )
        payload = _record_payload(grant, issue, recorded_at)
        payload.update(
            {
                "semanticDecision": "gap-recognized",
                "approvedReviewFile": archived_review.relative_to(issue_root).as_posix(),
                "approvedReviewSha256": review_sha256,
                "gapSnapshotFile": archived_gap.relative_to(issue_root).as_posix(),
                "gapSnapshotSha256": gap_sha256,
            }
        )
        history_bytes = _yaml_bytes(payload)
        _publish_exact_artifact(
            root,
            issue_root,
            archived_review,
            review_bytes,
            label="archived gap review",
            collision_message="archived gap approval collision",
        )
        _publish_exact_artifact(
            root,
            issue_root,
            archived_gap,
            gap_bytes,
            label="archived gap analysis",
            collision_message="archived gap analysis collision",
        )
        _publish_exact_artifact(
            root,
            issue_root,
            history_file,
            history_bytes,
            label="gap recognition history",
            collision_message="gap recognition history collision",
        )
        retire_live_review(root, issue, grant.approval_id)
        return history_file


def validate_gap_recognition_history(
    repo_root: Path,
    path: Path,
    *,
    history_snapshot: StableFileSnapshot | None = None,
) -> tuple[dict[str, object], tuple[StableFileSnapshot, ...]]:
    root = repo_root.resolve()
    record, _ = _parse_history_snapshot(
        root,
        path,
        history_snapshot.content if history_snapshot is not None else None,
    )
    if record.get("action") != "gap-recognition" or record.get("source") != "local-review":
        raise ValueError("record is not a local gap recognition")
    issue = normalized_issue(str(record["issue"]))
    issue_root = issue_dir(root, issue)
    approval_id = str(record["approvalId"])
    review_path = _contract_history_artifact(
        root,
        issue_root,
        record["approvedReviewFile"],
        "consumed",
        f"{approval_id}-gap-recognition-local-review.md",
    )
    review_snapshot = capture_stable_file(
        root,
        review_path,
        issue_root,
        "archived gap review",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    review_bytes = review_snapshot.content or b""
    if hashlib.sha256(review_bytes).hexdigest() != record["approvedReviewSha256"]:
        raise ValueError("archived gap review SHA256 mismatch")
    review_text = _decode_approval_bytes(review_bytes, review_path, "archived gap review")
    if field(review_text, "Approved Action") != "gap-recognition":
        raise ValueError("archived gap review action mismatch")
    if field(review_text, "Approval ID") != approval_id:
        raise ValueError("archived gap review approval ID mismatch")
    bindings = GitBindings(
        repository=str(record["repository"]),
        worktree=str(record["worktree"]),
        branch=str(record["branch"]),
    )
    _validate_review_text(
        root,
        issue,
        Path(str(record["approvedFile"])),
        None,
        review_text,
        bindings,
        str(record["approvedSha256"]),
    )

    gap_snapshot_path = _contract_history_artifact(
        root,
        issue_root,
        record["gapSnapshotFile"],
        "consumed",
        f"{approval_id}-gap-analysis.md",
    )
    gap_snapshot = capture_stable_file(
        root,
        gap_snapshot_path,
        issue_root,
        "archived gap analysis",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    if hashlib.sha256(gap_snapshot.content or b"").hexdigest() != record["gapSnapshotSha256"]:
        raise ValueError("archived gap analysis SHA256 mismatch")
    if record["gapSnapshotSha256"] != record["approvedSha256"]:
        raise ValueError("gap recognition snapshot does not match approved bytes")
    return record, (review_snapshot, gap_snapshot)


def validate_task_gap_recognition(
    repo_root: Path,
    issue: str,
    approval_reference: str,
    *,
    binding_mode: str = "current",
    recorded_branch: str | None = None,
) -> None:
    validate_task_gap_recognition_snapshots(
        repo_root,
        issue,
        approval_reference,
        binding_mode=binding_mode,
        recorded_branch=recorded_branch,
    )


def validate_task_gap_recognition_snapshots(
    repo_root: Path,
    issue: str,
    approval_reference: str,
    *,
    binding_mode: str = "current",
    recorded_branch: str | None = None,
) -> tuple[StableFileSnapshot, ...]:
    from .checks import validate_gap_analysis_snapshot

    root = repo_root.resolve()
    issue = normalized_issue(issue)
    issue_root = issue_dir(root, issue)
    history_path = require_safe_repo_path(root, issue_root / Path(approval_reference), "Human Approval Ref")
    try:
        history_path.relative_to(issue_root / "approvals" / "history")
    except ValueError as exc:
        raise ValueError("missing matching human gap recognition") from exc
    try:
        history_snapshot = capture_stable_file(
            root,
            history_path,
            issue_root,
            "gap recognition reference",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        record, support = validate_gap_recognition_history(
            root,
            history_path,
            history_snapshot=history_snapshot,
        )
        current_gap = capture_stable_file(
            root,
            issue_root / "gap-analysis.md",
            issue_root,
            "gap analysis",
            max_bytes=MAX_TEXT_ARTIFACT_BYTES,
        )
        evidence = validate_gap_analysis_snapshot(root, issue, current_gap)
    except ValueError as exc:
        raise ValueError(f"missing matching human gap recognition: {exc}") from exc
    bindings = resolve_bindings(root)
    expected: dict[str, object] = {
        "repository": bindings.repository,
        "issue": issue,
        "approvalIssue": issue,
        "action": "gap-recognition",
        "source": "local-review",
        "semanticDecision": "gap-recognized",
        "approvedFile": display_path(root, issue_root / "gap-analysis.md"),
        "approvedSha256": hashlib.sha256(current_gap.content or b"").hexdigest(),
    }
    if binding_mode == "current":
        expected.update({"worktree": bindings.worktree, "branch": bindings.branch})
    elif binding_mode == "recorded":
        if not recorded_branch:
            raise ValueError("missing matching human gap recognition: missing recorded task branch")
        expected["branch"] = recorded_branch
    else:
        raise ValueError(f"invalid gap recognition binding mode: {binding_mode}")
    if any(record.get(name) != value for name, value in expected.items()):
        raise ValueError("missing matching human gap recognition")
    return (current_gap, history_snapshot, *support, *evidence)


def parse_contract_acceptance_history(repo_root: Path, path: Path) -> dict[str, object]:
    record = _parse_history(repo_root.resolve(), path)
    if record.get("action") != "contract-acceptance" or record.get("source") != "local-review":
        raise ValueError("record is not a local contract acceptance")
    return record


def _contract_history_artifact(
    repo_root: Path,
    issue_root: Path,
    value: object,
    category: str,
    expected_name: str,
) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"invalid {category} artifact path")
    relative = Path(value)
    expected_relative = Path("approvals") / "history" / category / expected_name
    if relative != expected_relative:
        raise ValueError(f"invalid {category} artifact path")
    target = require_safe_repo_path(repo_root, issue_root / relative, f"contract acceptance {category} artifact")
    if target.parent != issue_root / "approvals" / "history" / category:
        raise ValueError(f"contract acceptance {category} artifact escapes approval history")
    return target


def _parse_contract_claim(
    repo_root: Path,
    path: Path,
    issue_root: Path,
    content: bytes | None = None,
) -> tuple[dict[str, object], bytes]:
    if not path.is_file():
        raise ValueError("missing atomic contract acceptance claim")
    raw_bytes = content if content is not None else _stable_approval_bytes(
        repo_root, path, issue_root, "contract acceptance claim"
    )
    try:
        payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "contract acceptance claim"))
    except ValueError as exc:
        raise ValueError(f"invalid contract acceptance claim YAML: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != CONTRACT_CLAIM_FIELDS:
        raise ValueError("contract acceptance claim has unexpected or missing fields")
    if payload["version"] != "0.1.0" or payload["reusable"] is not False:
        raise ValueError("contract acceptance claim has invalid fixed fields")
    for name in (
        "repository",
        "worktree",
        "approvedSha256",
        "contractSha256",
        "approvedReviewSha256",
        "contractSnapshotSha256",
        "historySha256",
    ):
        if not isinstance(payload[name], str) or not FINGERPRINT_RE.fullmatch(payload[name]):
            raise ValueError(f"contract acceptance claim has invalid {name}")
    if not isinstance(payload["approvalId"], str) or not APPROVAL_ID_RE.fullmatch(payload["approvalId"]):
        raise ValueError("contract acceptance claim has invalid approvalId")
    if payload["issue"] != normalized_issue(payload["issue"]):
        raise ValueError("contract acceptance claim has invalid Issue")
    if payload["action"] != "contract-acceptance" or payload["semanticDecision"] != "accepted-design":
        raise ValueError("contract acceptance claim has invalid decision fields")
    for name in (
        "branch",
        "approvedFile",
        "contractId",
        "contractVersion",
        "approvedReviewFile",
        "contractSnapshotFile",
        "historyFile",
    ):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"contract acceptance claim has invalid {name}")
    if payload["contractSnapshotSha256"] != payload["contractSha256"]:
        raise ValueError("contract acceptance claim snapshot SHA256 mismatch")
    _canonical_utc_timestamp(payload["claimedAt"], "claimedAt", microseconds=True)
    _canonical_utc_timestamp(payload["recordedAt"], "recordedAt", microseconds=True)
    accepted = payload["acceptedObjects"]
    if not isinstance(accepted, list) or tuple(accepted) != normalize_accepted_objects(accepted):
        raise ValueError("contract acceptance claim has invalid acceptedObjects")
    canonical_payload = {name: payload[name] for name in CONTRACT_CLAIM_FIELD_ORDER}
    if raw_bytes != _yaml_bytes(canonical_payload):
        raise ValueError("contract acceptance claim bytes are not canonical")
    return payload, raw_bytes


def validate_contract_acceptance_history(
    repo_root: Path,
    path: Path,
    *,
    history_snapshot: StableFileSnapshot | None = None,
    return_snapshots: bool = False,
) -> dict[str, object] | tuple[dict[str, object], tuple[StableFileSnapshot, ...]]:
    repo_root = repo_root.resolve()
    if history_snapshot is not None and history_snapshot.path != path:
        raise ValueError("contract acceptance history snapshot path mismatch")
    record, history_bytes = _parse_history_snapshot(
        repo_root,
        path,
        history_snapshot.content if history_snapshot is not None else None,
    )
    if record.get("action") != "contract-acceptance" or record.get("source") != "local-review":
        raise ValueError("record is not a local contract acceptance")
    issue = normalized_issue(str(record["issue"]))
    issue_root = issue_dir(repo_root, issue)
    approval_id = str(record["approvalId"])
    review_path = _contract_history_artifact(
        repo_root,
        issue_root,
        record["approvedReviewFile"],
        "consumed",
        f"{approval_id}-local-review.md",
    )
    review_snapshot = capture_stable_file(
        repo_root,
        review_path,
        issue_root,
        "archived approved review",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    review_bytes = review_snapshot.content or b""
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()
    if review_sha256 != record["approvedReviewSha256"]:
        raise ValueError("archived approved review SHA256 mismatch")
    review_text = _decode_approval_bytes(review_bytes, review_path, "archived approved review")
    if field(review_text, "Approved Action") != "contract-acceptance":
        raise ValueError("archived approved review action mismatch")
    if field(review_text, "Approval ID") != approval_id:
        raise ValueError("archived approved review approval ID mismatch")
    _canonical_utc_timestamp(field(review_text, "Approved At"), "Approved At", microseconds=False)
    accepted_objects = _contract_review_objects(review_text)
    if list(accepted_objects) != record["acceptedObjects"]:
        raise ValueError("archived approved review accepted object set mismatch")
    bindings = GitBindings(
        repository=str(record["repository"]),
        worktree=str(record["worktree"]),
        branch=str(record["branch"]),
    )
    approved_path, approved_sha256 = _validate_review_text(
        repo_root,
        issue,
        Path(str(record["approvedFile"])),
        None,
        review_text,
        bindings,
        str(record["approvedSha256"]),
    )
    if display_path(repo_root, approved_path) != record["approvedFile"] or approved_sha256 != record["approvedSha256"]:
        raise ValueError("archived approved review file binding mismatch")
    if safe_reviewer_summary(field(review_text, "Reviewer")) != record["reviewerSummary"]:
        raise ValueError("archived approved review reviewer mismatch")

    contract_snapshot_path = _contract_history_artifact(
        repo_root,
        issue_root,
        record["contractSnapshotFile"],
        "consumed",
        f"{approval_id}-contract.yaml",
    )
    contract_snapshot = capture_stable_file(
        repo_root,
        contract_snapshot_path,
        issue_root,
        "archived contract snapshot",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    snapshot_sha256 = hashlib.sha256(contract_snapshot.content or b"").hexdigest()
    if (
        snapshot_sha256 != record["contractSnapshotSha256"]
        or snapshot_sha256 != record["contractSha256"]
        or snapshot_sha256 != record["approvedSha256"]
    ):
        raise ValueError("archived contract snapshot SHA256 mismatch")

    claim_path = _contract_history_artifact(
        repo_root,
        issue_root,
        record["approvalClaimFile"],
        "claims",
        f"{approval_id}.yaml",
    )
    claim_snapshot = capture_stable_file(
        repo_root,
        claim_path,
        issue_root,
        "contract acceptance claim",
        max_bytes=MAX_TEXT_ARTIFACT_BYTES,
    )
    claim, _ = _parse_contract_claim(repo_root, claim_path, issue_root, claim_snapshot.content)
    inherited = {
        "approvalId": record["approvalId"],
        "repository": record["repository"],
        "worktree": record["worktree"],
        "branch": record["branch"],
        "issue": record["issue"],
        "action": record["action"],
        "approvedFile": record["approvedFile"],
        "approvedSha256": record["approvedSha256"],
        "contractId": record["contractId"],
        "contractVersion": record["contractVersion"],
        "contractSha256": record["contractSha256"],
        "acceptedObjects": record["acceptedObjects"],
        "semanticDecision": record["semanticDecision"],
        "approvedReviewFile": record["approvedReviewFile"],
        "approvedReviewSha256": record["approvedReviewSha256"],
        "contractSnapshotFile": record["contractSnapshotFile"],
        "contractSnapshotSha256": record["contractSnapshotSha256"],
    }
    if any(claim.get(name) != value for name, value in inherited.items()):
        raise ValueError("contract acceptance claim does not match history")
    history_relative = path.relative_to(issue_root).as_posix()
    if (
        claim.get("recordedAt") != record["recordedAt"]
        or claim.get("historyFile") != history_relative
        or claim.get("historySha256") != hashlib.sha256(history_bytes).hexdigest()
    ):
        raise ValueError("contract acceptance claim does not seal exact history")
    _validate_acceptance_chronology(review_bytes, review_path, claim)
    if return_snapshots:
        return record, (review_snapshot, contract_snapshot, claim_snapshot)
    return record


def record_subordinate_effect(
    repo_root: Path,
    parent_grant: ApprovalGrant,
    action: Literal["git-state-backfill"],
    result: Literal["success"],
    *,
    idempotent: bool = False,
) -> Path:
    if result != "success":
        raise ValueError("subordinate effect records require confirmed success")
    if action != EFFECT_ACTION or parent_grant.action != EFFECT_PARENT_ACTION:
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
    if len(parent_records) != 1 or any(
        parent_records[0].get(name) != value for name, value in expected_parent.items()
    ):
        raise ValueError("git-state-backfill parent approval snapshot mismatch")
    existing_effect = next(
        (
            path
            for path in _history_yaml_paths(repo_root)
            if (record := _parse_history(repo_root, path))["source"] == "effect"
            and record["parentApprovalId"] == parent_grant.approval_id
            and record["action"] == action
        ),
        None,
    )
    if existing_effect is not None:
        if idempotent:
            return existing_effect
        raise ValueError("git-state-backfill effect already recorded")
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload: dict[str, object] = {
        "version": "0.1.0",
        "reusable": False,
        "source": "effect",
        "approvalId": "",
        "repository": parent_grant.repository,
        "worktree": parent_grant.worktree,
        "branch": parent_grant.branch,
        "issue": parent_grant.approval_issue,
        "approvalIssue": parent_grant.approval_issue,
        "action": EFFECT_ACTION,
        "approvedFile": parent_grant.approved_file,
        "approvedSha256": parent_grant.approved_sha256,
        "reviewerSummary": EFFECT_REVIEWER_SUMMARY,
        "result": "success",
        "recordedAt": recorded_at,
        "parentAction": EFFECT_PARENT_ACTION,
        "parentApprovalId": parent_grant.approval_id,
    }
    payload["approvalId"] = _effect_identity(payload)
    history_file = _history_path(repo_root, parent_grant.approval_issue, action, recorded_at)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
    return history_file
