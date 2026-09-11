"""Exact, human-approved recovery of a pre-design contract location.

No identity is deleted or downgraded. Sealed before images let a retry finish
only the same writes after interruption; ordinary task checks fail closed on
an intermediate state. This command is never an unattended action.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from . import approval, task_state
from .bindings import git_path, resolve_bindings
from .collaboration import repository_locked
from .contracts import _contract_path, load_contract
from .json_safety import loads_unique_json
from .local_artifacts import capture_stable_file
from .paths import active_task_pointer_file, issue_dir, normalized_issue, task_authority_file, task_state_file
from .project_config import require_safe_repo_path

ACTION = "task-contract-relocate"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(root: Path, path: Path, *, required: bool = True) -> bytes:
    snapshot = capture_stable_file(root, path, root, "contract relocation artifact", required=required)
    return snapshot.content if snapshot.exists else b""


def _json(data: bytes) -> dict:
    value = loads_unique_json(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("contract relocation requires a JSON object")
    return value


def _encode(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode()


def _state_paths(root: Path, issue: str):
    bindings = resolve_bindings(root)
    common = git_path(root, "--git-common-dir")
    return {
        "task": (root, task_state_file(root, issue)),
        "pointer": (common, active_task_pointer_file(root, bindings.worktree)),
        "authority": (common, task_authority_file(root, bindings.worktree, issue)),
    }


def _check_stage(state) -> None:
    if (state.classification != "capability-change"
            or state.execution_state not in {"S2_REMOTE_ISSUE_CREATED", "S3_TASK_BRANCH_STARTED"}
            or state.semantic_phase not in {"classified", "declaring"}
            or state.human_approval_ref != "none"):
        raise ValueError("contract relocation is only allowed before design acceptance and implementation")


def _check_contracts(root: Path, request: dict) -> None:
    config = _read(root, root / ".xflow/xflow.json", required=False)
    if _digest(config) != request["configSha256"]:
        raise ValueError("contract relocation configuration changed")
    old = require_safe_repo_path(root, root / request["oldFile"], "old contract")
    _, new = _contract_path(root, Path(request["newFile"]))
    if old == new or new.relative_to(root).as_posix() != request["newFile"]:
        raise ValueError("contract relocation requires a distinct canonical destination")
    old_bytes, new_bytes = _read(root, old), _read(root, new)
    if old_bytes != new_bytes or _digest(old_bytes) != request["contractSha256"]:
        raise ValueError("contract relocation requires unchanged identical bytes")
    contract = load_contract(root, new)
    if f'{contract.raw["id"]}@{contract.raw["version"]}' != request["contract"]:
        raise ValueError("contract relocation cannot change contract identity")
    if contract.raw["status"] not in {"draft", "declaring"}:
        raise ValueError("contract relocation requires an unaccepted candidate")
    history = issue_dir(root, request["issue"]) / "approvals/history"
    if list(history.glob("*-contract-acceptance-*.yaml")) or list((history / "claims").glob("*-contract*.yaml")):
        raise ValueError("contract acceptance history or pending claim prevents relocation")


def _request(root: Path, issue: str, destination: Path) -> tuple[dict, dict[str, bytes]]:
    bindings, state = task_state.load_active_task_snapshot(root)
    if state.issue != issue:
        raise ValueError("contract relocation Issue mismatch")
    _check_stage(state)
    _, new = _contract_path(root, destination)
    originals = {name: _read(owner, path) for name, (owner, path) in _state_paths(root, issue).items()}
    result = {
        "version": "0.1.0", "issue": issue,
        "repository": bindings.repository, "worktree": bindings.worktree, "branch": bindings.branch,
        "contract": state.contract, "oldFile": state.contract_file,
        "newFile": new.relative_to(root).as_posix(),
        "contractSha256": _digest(_read(root, root / state.contract_file)),
        "configSha256": _digest(_read(root, root / ".xflow/xflow.json", required=False)),
        "before": {name: _digest(data) for name, data in originals.items()},
    }
    _check_contracts(root, result)
    return result, originals


@repository_locked
def prepare(root: Path, issue: str, destination: Path) -> Path:
    root, issue = root.resolve(), normalized_issue(issue)
    request, _ = _request(root, issue, destination)
    path = issue_dir(root, issue) / "contract-relocation.json"
    review = approval.default_approval_file(root, issue)
    if review.exists() and approval.live_review_blocks_prepare(root, review):
        raise ValueError("unused approved review prevents preparing contract relocation")
    task_state._write_atomic(path, _encode(request).decode())
    approval.prepare(root, issue, ACTION, path)
    return path


def _publish(root: Path, archive: Path, name: str, data: bytes) -> None:
    target = require_safe_repo_path(root, archive / name, "relocation archive")
    approval._publish_exact_artifact(root, archive, target, data,
        label="contract relocation snapshot", collision_message="contract relocation snapshot collision")


def _review_grant(root: Path, issue: str, path: Path, data: bytes, review: bytes):
    bindings = resolve_bindings(root)
    text = review.decode("utf-8")
    approval._validate_review_text(root, issue, path, None, text, bindings, _digest(data))
    if approval.field(text, "Approved Action") != ACTION:
        raise ValueError("contract relocation requires exact human approval")
    grant = approval.ApprovalGrant(source="local-review",
        approval_id=approval.field(text, "Approval ID"), repository=bindings.repository,
        worktree=bindings.worktree, branch=bindings.branch, approval_issue=issue,
        action=ACTION, approved_file=path.relative_to(root).as_posix(),
        approved_sha256=_digest(data), reviewer_summary=approval.safe_reviewer_summary(approval.field(text, "Reviewer")))
    approval._validate_grant(grant)
    return grant


@repository_locked
def relocate(root: Path, issue: str, path: Path) -> Path:
    root, issue = root.resolve(), normalized_issue(issue)
    path = require_safe_repo_path(root, path, "relocation request")
    if path != issue_dir(root, issue) / "contract-relocation.json":
        raise ValueError("contract relocation request must use its canonical Issue path")
    data = _read(root, path)
    request = _json(data)
    archive = issue_dir(root, issue) / "approvals/history/contract-relocations" / _digest(data)
    ready = archive / "ready"
    if not ready.exists():
        expected, originals = _request(root, issue, Path(request["newFile"]))
        if request != expected:
            raise ValueError("contract relocation request is stale or has unexpected fields")
        grant = approval.require_remote(root, ACTION, path, issue)
        review = _read(root, approval.default_approval_file(root, issue))
        if _review_grant(root, issue, path, data, review) != grant:
            raise ValueError("contract relocation review changed")
        _publish(root, archive, "request.json", data)
        _publish(root, archive, "review.md", review)
        for name, content in originals.items():
            _publish(root, archive, name + ".before", content)
        # No task/authority/pointer writes occur until all before images exist.
        _publish(root, archive, "ready", b"reserved\n")
    if _read(root, ready) != b"reserved\n" or _read(root, archive / "request.json") != data:
        raise ValueError("contract relocation reservation changed")
    review = _read(root, archive / "review.md")
    grant = _review_grant(root, issue, path, data, review)
    bindings = resolve_bindings(root)
    if (request["issue"] != issue or request["repository"] != bindings.repository
            or request["worktree"] != bindings.worktree or request["branch"] != bindings.branch):
        raise ValueError("contract relocation binding mismatch")
    _check_contracts(root, request)
    originals = {name: _read(root, archive / (name + ".before")) for name in ("task", "pointer", "authority")}
    if request["before"] != {name: _digest(content) for name, content in originals.items()}:
        raise ValueError("contract relocation before images changed")
    state = task_state.parse_task_state_text(task_state_file(root, issue),
        originals["task"].decode("utf-8"), binding_mode="recorded")
    _check_stage(state)
    if (state.issue != issue or state.branch != bindings.branch or state.contract != request["contract"]
            or state.contract_file != request["oldFile"]):
        raise ValueError("contract relocation before state mismatch")
    replacements = {"task": task_state.render_task_state(replace(state, contract_file=request["newFile"])).encode()}
    for name in ("pointer", "authority"):
        value = _json(originals[name])
        if (value["repository"] != bindings.repository or value["worktree"] != bindings.worktree
                or value["issue"] != issue or value["contractFile"] != request["oldFile"]):
            raise ValueError("contract relocation before identity mismatch")
        value["contractFile"] = request["newFile"]
        replacements[name] = _encode(value)
    paths = _state_paths(root, issue)
    current = {name: _read(owner, target) for name, (owner, target) in paths.items()}
    if any(current[name] not in (originals[name], replacements[name]) for name in paths):
        raise ValueError("contract relocation encountered unrelated state changes")
    records = [r for r in approval._history_records(root) if r["approvalId"] == grant.approval_id]
    if records:
        if (len(records) != 1 or records[0] != approval._record_payload(grant, issue, str(records[0]["recordedAt"]))
                or any(current[name] != replacements[name] for name in paths)):
            raise ValueError("contract relocation approval already used or state changed")
        approval.retire_live_review(root, issue, grant.approval_id)
    else:
        # A crash between these atomic writes fails normal task checks closed.
        # Retry validates every file as precisely its sealed before/after image.
        for name, (owner, target) in paths.items():
            if resolve_bindings(root) != bindings or _read(owner, target) != current[name]:
                raise ValueError("contract relocation state changed during correction")
            _check_contracts(root, request)
            if current[name] != replacements[name]:
                task_state._write_atomic(target, replacements[name].decode())
        task_state.check_task_binding(root, issue)
        if (_read(root, archive / "request.json") != data or _read(root, archive / "review.md") != review
                or any(_read(root, archive / (name + ".before")) != value for name, value in originals.items())):
            raise ValueError("contract relocation evidence changed during correction")
        approval.record_consumed_approval(root, grant, "success")
    return archive
