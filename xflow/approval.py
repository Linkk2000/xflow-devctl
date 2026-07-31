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
from typing import Literal, Sequence

import yaml

from .bindings import GitBindings, resolve_bindings
from .classification import _decode_classification_bytes, _load_yaml, _read_stable_bytes
from .io import read_text
from .paths import default_approval_file, issue_dir, normalized_issue
from .project_config import require_safe_repo_path
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
HISTORY_ACTIONS = UNATTENDED_ACTIONS | {"contract-acceptance", "git-state-backfill"}
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
    "approvedReviewFile", "approvedReviewSha256", "approvalClaimFile", "approvalClaimSha256",
}
CONTRACT_CLAIM_FIELDS = {
    "version", "reusable", "approvalId", "repository", "worktree", "branch", "issue", "action",
    "approvedFile", "approvedSha256", "contractId", "contractVersion", "contractSha256",
    "acceptedObjects", "semanticDecision", "approvedReviewSha256", "claimedAt",
}
CONTRACT_REVIEW_FIELDS = {"version", "acceptedObjects", "semanticDecision"}
CONTRACT_REVIEW_HEADING = "## Contract Acceptance"
EFFECT_ACTION = "git-state-backfill"
EFFECT_PARENT_ACTION = "git-mr"
EFFECT_REVIEWER_SUMMARY = "subordinate-effect"
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
    accepted_objects: tuple[str, ...] = ()


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
        if not isinstance(item, str) or not item or item != item.strip() or any(char.isspace() for char in item):
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
    review_file.write_text(text, encoding="utf-8", newline="\n")
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
        return _read_stable_bytes(repo_root, safe_path, owner)
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


def _effect_identity(payload: dict[str, object]) -> str:
    identity_fields = {
        name: payload[name]
        for name in sorted(HISTORY_EFFECT_FIELDS - {"approvalId"})
    }
    material = json.dumps(identity_fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(f"xflow-approval-effect-v1\0{material}".encode("utf-8")).hexdigest()


def _parse_history(repo_root: Path, path: Path) -> dict[str, object]:
    try:
        issue_root = path.resolve().parents[2]
        raw_bytes = _stable_approval_bytes(repo_root, path, issue_root, "approval history")
        payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "approval history"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"approval history integrity error in {path}: invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"approval history integrity error in {path}: expected a mapping")
    source = payload.get("source")
    action = payload.get("action")
    expected_fields = (
        HISTORY_EFFECT_FIELDS
        if source == "effect"
        else HISTORY_CONTRACT_FIELDS
        if action == "contract-acceptance"
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
                for name in (
                    "approvedReviewFile",
                    "approvedReviewSha256",
                    "approvalClaimFile",
                    "approvalClaimSha256",
                ):
                    if not isinstance(payload[name], str) or not payload[name]:
                        raise ValueError(f"invalid {name}")
                for name in ("approvedReviewSha256", "approvalClaimSha256"):
                    if not FINGERPRINT_RE.fullmatch(str(payload[name])):
                        raise ValueError(f"invalid {name}")
        expected_path = _history_path(
            repo_root,
            str(payload["issue"]),
            str(payload["action"]),
            str(payload["recordedAt"]),
        )
        if path.resolve() != expected_path:
            raise ValueError("history path does not match record Issue, action, and timestamp")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"approval history integrity error in {path}: {exc}") from exc
    reject_credentials(yaml.safe_dump(payload, sort_keys=True, allow_unicode=False))
    return payload


def _history_records(repo_root: Path) -> tuple[dict[str, object], ...]:
    issues_root = repo_root.resolve() / ".xflow" / "issues"
    if not issues_root.is_dir():
        return ()
    located_records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(issues_root.glob("issue-*/approvals/history/*.yaml")):
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
    reject_credentials(json.dumps(asdict(grant), ensure_ascii=True, sort_keys=True))


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


def _contract_artifact_path(repo_root: Path, issue: str, category: str, name: str) -> Path:
    issue_root = issue_dir(repo_root.resolve(), normalized_issue(issue))
    history_root = issue_root / "approvals" / "history"
    if category not in {"claims", "consumed"}:
        raise ValueError(f"invalid contract acceptance artifact category: {category}")
    target = require_safe_repo_path(repo_root, history_root / category / name, "contract acceptance artifact")
    expected_parent = history_root / category
    if target.parent != expected_parent:
        raise ValueError("contract acceptance artifact path escapes approval history")
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
    repo_root = repo_root.resolve()
    _validate_grant(grant)
    reject_consumed_approval(repo_root, grant.approval_id)
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    issue = _record_target_issue(grant, target_issue)
    history_file = _history_path(repo_root, issue, grant.action, recorded_at)
    payload = _record_payload(grant, issue, recorded_at)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
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


def consume_contract_acceptance(
    repo_root: Path,
    issue: str,
    approved_file: Path,
    *,
    contract_id: str,
    contract_version: str,
    contract_sha256: str,
    accepted_objects: tuple[str, ...],
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
    grant, _, review_bytes = _contract_local_grant(
        repo_root,
        approved_file,
        issue,
        expected_sha256=contract_sha256,
        expected_objects=normalized_objects,
    )
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()
    claimed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    claim_payload: dict[str, object] = {
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
        "acceptedObjects": list(normalized_objects),
        "semanticDecision": "accepted-design",
        "approvedReviewSha256": review_sha256,
        "claimedAt": claimed_at,
    }
    claim_content = yaml.safe_dump(claim_payload, sort_keys=False, allow_unicode=False).encode("utf-8")
    claim_path = _contract_artifact_path(repo_root, issue, "claims", f"{grant.approval_id}.yaml")
    try:
        _write_immutable_bytes(claim_path, claim_content, "contract acceptance claim collision")
    except ValueError as exc:
        if "claim collision" in str(exc):
            raise ValueError(
                f"approval already consumed: {grant.approval_id} (approval already claimed)"
            ) from None
        raise

    archived_review = _contract_artifact_path(
        repo_root,
        issue,
        "consumed",
        f"{grant.approval_id}-local-review.md",
    )
    _write_immutable_bytes(
        archived_review,
        review_bytes,
        "archived contract approval collision",
    )
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    history_file = _history_path(repo_root, issue, grant.action, recorded_at)
    payload = _record_payload(grant, issue, recorded_at)
    issue_root = issue_dir(repo_root, issue)
    payload.update(
        {
            "contractId": contract_id,
            "contractVersion": contract_version,
            "contractSha256": contract_sha256,
            "acceptedObjects": list(normalized_objects),
            "semanticDecision": "accepted-design",
            "approvedReviewFile": archived_review.relative_to(issue_root).as_posix(),
            "approvedReviewSha256": review_sha256,
            "approvalClaimFile": claim_path.relative_to(issue_root).as_posix(),
            "approvalClaimSha256": hashlib.sha256(claim_content).hexdigest(),
        }
    )
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    reject_credentials(content)
    _write_history_atomic(history_file, content)
    return history_file


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


def _parse_contract_claim(repo_root: Path, path: Path, issue_root: Path) -> tuple[dict[str, object], bytes]:
    if not path.is_file():
        raise ValueError("missing atomic contract acceptance claim")
    raw_bytes = _stable_approval_bytes(repo_root, path, issue_root, "contract acceptance claim")
    try:
        payload = _load_yaml(_decode_approval_bytes(raw_bytes, path, "contract acceptance claim"))
    except ValueError as exc:
        raise ValueError(f"invalid contract acceptance claim YAML: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != CONTRACT_CLAIM_FIELDS:
        raise ValueError("contract acceptance claim has unexpected or missing fields")
    if payload["version"] != "0.1.0" or payload["reusable"] is not False:
        raise ValueError("contract acceptance claim has invalid fixed fields")
    _timestamp(str(payload["claimedAt"]), "claimedAt")
    accepted = payload["acceptedObjects"]
    if not isinstance(accepted, list) or tuple(accepted) != normalize_accepted_objects(accepted):
        raise ValueError("contract acceptance claim has invalid acceptedObjects")
    return payload, raw_bytes


def validate_contract_acceptance_history(repo_root: Path, path: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    record = parse_contract_acceptance_history(repo_root, path)
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
    if not review_path.is_file():
        raise ValueError("missing archived approved review")
    review_bytes = _stable_approval_bytes(repo_root, review_path, issue_root, "archived approved review")
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()
    if review_sha256 != record["approvedReviewSha256"]:
        raise ValueError("archived approved review SHA256 mismatch")
    review_text = _decode_approval_bytes(review_bytes, review_path, "archived approved review")
    if field(review_text, "Approved Action") != "contract-acceptance":
        raise ValueError("archived approved review action mismatch")
    if field(review_text, "Approval ID") != approval_id:
        raise ValueError("archived approved review approval ID mismatch")
    _timestamp(field(review_text, "Approved At"), "Approved At")
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

    claim_path = _contract_history_artifact(
        repo_root,
        issue_root,
        record["approvalClaimFile"],
        "claims",
        f"{approval_id}.yaml",
    )
    claim, claim_bytes = _parse_contract_claim(repo_root, claim_path, issue_root)
    if hashlib.sha256(claim_bytes).hexdigest() != record["approvalClaimSha256"]:
        raise ValueError("contract acceptance claim SHA256 mismatch")
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
        "approvedReviewSha256": record["approvedReviewSha256"],
    }
    if any(claim.get(name) != value for name, value in inherited.items()):
        raise ValueError("contract acceptance claim does not match history")
    return record


def record_subordinate_effect(
    repo_root: Path,
    parent_grant: ApprovalGrant,
    action: Literal["git-state-backfill"],
    result: Literal["success"],
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
