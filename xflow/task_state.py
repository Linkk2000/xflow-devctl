from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .bindings import GitBindings, git_path, resolve_bindings
from .collaboration import repository_locked
from .json_safety import loads_unique_json
from .paths import (
    active_task_pointer_file,
    legacy_active_task_pointer_file,
    task_authority_file,
    task_state_file,
)


EXECUTION_STATES = (
    "S0_REQUEST",
    "S1_LOCAL_ISSUE_DRAFT",
    "S2_REMOTE_ISSUE_CREATED",
    "S3_TASK_BRANCH_STARTED",
    "S4_TDD_AND_IMPLEMENTATION",
    "S5_LOCAL_VERIFICATION",
    "S6_PREPARE_COMMIT_AND_MR_DRAFT",
    "S7_PUSH_BRANCH",
    "S8_CREATE_REMOTE_MR",
    "S9_REMOTE_REVIEW_AND_CI",
    "S10_DONE",
)
SEMANTIC_PHASES = (
    "none",
    "discovery",
    "classified",
    "declaring",
    "accepted-design",
    "verification-designed",
    "projected",
    "gap-analysis",
    "gap-recognized",
)
CLASSIFICATIONS = (
    "capability-change",
    "implementation-gap",
    "ui-defect",
    "infrastructure",
    "governance",
    "future",
)
POINTER_VERSION = 2
POINTER_V1_FIELDS = {"version", "repository", "worktree", "branch", "issue", "activatedAt"}
POINTER_FIELDS = POINTER_V1_FIELDS | {"taskMode", "contractId", "contractVersion", "contractFile"}
TASK_MODES = {"modern-contract", "legacy"}
AUTHORITY_VERSION = 1
AUTHORITY_FIELDS = {
    "version",
    "repository",
    "worktree",
    "issue",
    "taskMode",
    "contractId",
    "contractVersion",
    "contractFile",
    "legacySourceFile",
    "legacySourceDigest",
}
LEGACY_CONTRACT_ID = "legacy.current-task"
LEGACY_CONTRACT_VERSION = "0.1.0"
LEGACY_CONTRACT_REF = f"{LEGACY_CONTRACT_ID}@{LEGACY_CONTRACT_VERSION}"
LEGACY_CONTRACT_FILE = ".xflow/current-task.md"
TASK_ISSUE_ID_RE = re.compile(r"[A-Za-z0-9]+")
TASK_STATE_FIELDS = (
    "Issue",
    "Execution State",
    "Semantic Phase",
    "Classification",
    "Contract",
    "Contract File",
    "Contract Change Required",
    "Branch",
    "Base",
    "Human Gate",
    "Human Approval Ref",
)
TASK_STATE_TITLE = "# XFlow Task State"
ALLOWED_ACTIONS_HEADING = "## Allowed Actions"
FORBIDDEN_ACTIONS_HEADING = "## Forbidden Actions"
LEGACY_GATE_STATES = {
    "G1_APPROVE_ISSUE_CREATE": "S1_LOCAL_ISSUE_DRAFT",
    "G2_APPROVE_DEVELOPMENT_START": "S2_REMOTE_ISSUE_CREATED",
    "G3_APPROVE_RESULT": "S5_LOCAL_VERIFICATION",
    "G4_APPROVE_REMOTE_WRITE": "S6_PREPARE_COMMIT_AND_MR_DRAFT",
    "G5_APPROVE_MR_CREATE": "S7_PUSH_BRANCH",
    "G6_APPROVE_CLEANUP": "S9_REMOTE_REVIEW_AND_CI",
}


@dataclass(frozen=True)
class TaskState:
    issue: str
    execution_state: str
    semantic_phase: str
    classification: str
    contract: str
    contract_file: str
    contract_change_required: bool
    branch: str
    base: str
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    human_gate: str
    human_approval_ref: str


@dataclass(frozen=True)
class ActiveTaskPointer:
    version: int
    repository: str
    worktree: str
    branch: str
    issue: str
    taskMode: str
    contractId: str
    contractVersion: str
    contractFile: str
    activatedAt: str


@dataclass(frozen=True)
class TaskAuthority:
    version: int
    repository: str
    worktree: str
    issue: str
    taskMode: str
    contractId: str
    contractVersion: str
    contractFile: str
    legacySourceFile: str | None
    legacySourceDigest: str | None


def _field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^[ \t]*{re.escape(name)}[ \t]*:[ \t]*([^\r\n]*)[ \t]*\r?$", text)
    return match.group(1).strip() if match else ""


def _actions(text: str, heading: str) -> tuple[str, ...]:
    match = re.search(rf"(?ms)^[ \t]*{re.escape(heading)}[ \t]*\r?$\n(.*?)(?=^[ \t]*#{{1,6}}[ \t]+|\Z)", text)
    if not match:
        raise ValueError(f"missing required task-state section: {heading}")
    actions = tuple(
        item.group(1).strip()
        for line in match.group(1).splitlines()
        if (item := re.match(r"^[ \t]*-[ \t]+(\S.*?)[ \t]*$", line))
    )
    if not actions:
        raise ValueError(f"task-state {heading[3:]} must not be empty")
    return actions


def _heading_index(lines: list[str], heading: str) -> int:
    matches = [index for index, line in enumerate(lines) if line.strip() == heading]
    if not matches:
        raise ValueError(f"missing required task-state heading: {heading}")
    if len(matches) > 1:
        raise ValueError(f"duplicate task-state heading: {heading}")
    return matches[0]


def _structured_actions(lines: list[str], heading: str) -> tuple[str, ...]:
    actions = []
    for line in lines:
        if not line.strip():
            continue
        item = re.fullmatch(r"[ \t]*-[ \t]+(\S.*?)[ \t]*", line)
        if not item:
            raise ValueError(f"unexpected non-list line in {heading}: {line.strip()}")
        actions.append(item.group(1).strip())
    if not actions:
        raise ValueError(f"task-state {heading[3:]} must not be empty")
    return tuple(actions)


def _parse_task_state_markdown(text: str) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    lines = text.splitlines()
    title_index = _heading_index(lines, TASK_STATE_TITLE)
    allowed_index = _heading_index(lines, ALLOWED_ACTIONS_HEADING)
    forbidden_index = _heading_index(lines, FORBIDDEN_ACTIONS_HEADING)
    if not title_index < allowed_index < forbidden_index:
        raise ValueError("task-state headings are out of order")
    if any(line.strip() for line in lines[:title_index]):
        raise ValueError("unexpected content before task-state heading")

    fields: dict[str, str] = {}
    for line in lines[title_index + 1 : allowed_index]:
        if not line.strip():
            continue
        match = re.fullmatch(r"[ \t]*([^:\r\n]+?)[ \t]*:[ \t]*([^\r\n]*)[ \t]*", line)
        if not match:
            raise ValueError(f"unexpected task-state preamble line: {line.strip()}")
        name = match.group(1).strip()
        if name not in TASK_STATE_FIELDS:
            raise ValueError(f"unexpected task-state field: {name}")
        if name in fields:
            raise ValueError(f"duplicate task-state field: {name}")
        fields[name] = match.group(2).strip()

    allowed = _structured_actions(lines[allowed_index + 1 : forbidden_index], ALLOWED_ACTIONS_HEADING)
    forbidden = _structured_actions(lines[forbidden_index + 1 :], FORBIDDEN_ACTIONS_HEADING)
    for name in TASK_STATE_FIELDS:
        if name not in fields:
            raise ValueError(f"missing required task-state field: {name}")
    return fields, allowed, forbidden


def _normalized_issue(value: str) -> str:
    if not isinstance(value, str) or not TASK_ISSUE_ID_RE.fullmatch(value):
        raise ValueError("task-state Issue must contain only letters and numbers")
    return value


def _required(text: str, label: str) -> str:
    if not text:
        raise ValueError(f"missing required task-state field: {label}")
    return text


def _validate_relative_approval(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 3 or path.parts[:2] != ("approvals", "history"):
        raise ValueError("Human Approval Ref must be an Issue-relative path under approvals/history/")


def _task_state_snapshot(path: Path) -> object:
    absolute = Path(os.path.abspath(path))
    if (
        absolute.name != "task-state.md"
        or not absolute.parent.name.startswith("issue-")
        or absolute.parent.parent.name != "issues"
        or absolute.parent.parent.parent.name != ".xflow"
    ):
        raise ValueError("task-state file must be inside the matching Issue directory")
    from .local_artifacts import MAX_SMALL_ARTIFACT_BYTES, capture_stable_file

    root = absolute.parents[3]
    snapshot = capture_stable_file(
        root,
        absolute,
        absolute.parent,
        "task-state",
        max_bytes=MAX_SMALL_ARTIFACT_BYTES,
    )
    return snapshot


def _snapshot_text(snapshot: object, label: str) -> str:
    content = getattr(snapshot, "content", None)
    path = getattr(snapshot, "path", "<unknown>")
    try:
        return (content or b"").decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8: {path}") from exc


def _load_task_state(
    path: Path,
    *,
    binding_mode: str = "current",
    validate_acceptance: bool = True,
) -> tuple[object, TaskState]:
    snapshot = _task_state_snapshot(path)
    state = parse_task_state_text(
        getattr(snapshot, "path"),
        _snapshot_text(snapshot, "task-state"),
        binding_mode=binding_mode,
        validate_acceptance=validate_acceptance,
    )
    return snapshot, state


def parse_task_state(path: Path, *, binding_mode: str = "current") -> TaskState:
    return _load_task_state(path, binding_mode=binding_mode)[1]


def parse_task_state_text(
    path: Path,
    text: str,
    *,
    binding_mode: str = "current",
    validate_acceptance: bool = True,
) -> TaskState:
    fields, allowed_actions, forbidden_actions = _parse_task_state_markdown(text)
    issue = _normalized_issue(_required(fields["Issue"], "Issue"))
    resolved = Path(os.path.abspath(path))
    expected_parent = f"issue-{issue}"
    if (
        resolved.parent.name != expected_parent
        or resolved.parent.parent.name != "issues"
        or resolved.parent.parent.parent.name != ".xflow"
    ):
        raise ValueError("task-state file must be inside the matching Issue directory")
    execution_state = _required(fields["Execution State"], "Execution State")
    if execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown task-state Execution State: {execution_state}")
    semantic_phase = _required(fields["Semantic Phase"], "Semantic Phase")
    if semantic_phase not in SEMANTIC_PHASES:
        raise ValueError(f"unknown task-state Semantic Phase: {semantic_phase}")
    classification = _required(fields["Classification"], "Classification")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown task-state Classification: {classification}")
    boolean = _required(fields["Contract Change Required"], "Contract Change Required")
    if boolean not in {"yes", "no"}:
        raise ValueError("Contract Change Required must be yes or no")
    human_gate = _required(fields["Human Gate"], "Human Gate")
    human_approval_ref = _required(fields["Human Approval Ref"], "Human Approval Ref")
    needs_approval = SEMANTIC_PHASES.index(semantic_phase) >= SEMANTIC_PHASES.index("accepted-design")
    if needs_approval:
        if human_approval_ref == "none":
            raise ValueError("Human Approval Ref is required once Semantic Phase is accepted-design or later")
        _validate_relative_approval(human_approval_ref)
    elif human_approval_ref != "none":
        _validate_relative_approval(human_approval_ref)
    state = TaskState(
        issue=issue,
        execution_state=execution_state,
        semantic_phase=semantic_phase,
        classification=classification,
        contract=_required(fields["Contract"], "Contract"),
        contract_file=_required(fields["Contract File"], "Contract File"),
        contract_change_required=boolean == "yes",
        branch=_required(fields["Branch"], "Branch"),
        base=_required(fields["Base"], "Base"),
        allowed_actions=allowed_actions,
        forbidden_actions=forbidden_actions,
        human_gate=human_gate,
        human_approval_ref=human_approval_ref,
    )
    if needs_approval and validate_acceptance:
        from .contracts import validate_task_contract_acceptance

        validate_task_contract_acceptance(
            resolved.parents[3],
            state.issue,
            state.contract,
            state.contract_file,
            state.human_approval_ref,
            state.semantic_phase,
            binding_mode=binding_mode,
            recorded_branch=state.branch,
        )
    return state


def render_task_state(state: TaskState) -> str:
    _validate_state(state)
    return "\n".join(
        (
            "# XFlow Task State",
            "",
            f"Issue: {state.issue}",
            f"Execution State: {state.execution_state}",
            f"Semantic Phase: {state.semantic_phase}",
            f"Classification: {state.classification}",
            f"Contract: {state.contract}",
            f"Contract File: {state.contract_file}",
            f"Contract Change Required: {'yes' if state.contract_change_required else 'no'}",
            f"Branch: {state.branch}",
            f"Base: {state.base}",
            f"Human Gate: {state.human_gate}",
            f"Human Approval Ref: {state.human_approval_ref}",
            "",
            "## Allowed Actions",
            *(f"- {action}" for action in state.allowed_actions),
            "",
            "## Forbidden Actions",
            *(f"- {action}" for action in state.forbidden_actions),
            "",
        )
    )


def _validate_state(state: TaskState) -> None:
    _normalized_issue(state.issue)
    if state.execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown task-state Execution State: {state.execution_state}")
    if state.semantic_phase not in SEMANTIC_PHASES:
        raise ValueError(f"unknown task-state Semantic Phase: {state.semantic_phase}")
    if state.classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown task-state Classification: {state.classification}")
    if not isinstance(state.contract_change_required, bool):
        raise ValueError("Contract Change Required must be yes or no")
    for label, value in (
        ("Contract", state.contract),
        ("Contract File", state.contract_file),
        ("Branch", state.branch),
        ("Base", state.base),
        ("Human Gate", state.human_gate),
        ("Human Approval Ref", state.human_approval_ref),
    ):
        _required(value, label)
    if not state.allowed_actions or not state.forbidden_actions:
        raise ValueError("task-state action lists must not be empty")
    if any(not item.strip() for item in (*state.allowed_actions, *state.forbidden_actions)):
        raise ValueError("task-state actions must not be empty")
    needs_approval = SEMANTIC_PHASES.index(state.semantic_phase) >= SEMANTIC_PHASES.index("accepted-design")
    if needs_approval and state.human_approval_ref == "none":
        raise ValueError("Human Approval Ref is required once Semantic Phase is accepted-design or later")
    if state.human_approval_ref != "none":
        _validate_relative_approval(state.human_approval_ref)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid active task pointer: activatedAt must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid active task pointer: activatedAt must include a timezone")


def _pointer(payload: object) -> ActiveTaskPointer:
    if not isinstance(payload, dict):
        raise ValueError("invalid active task pointer: unexpected JSON fields")
    version = payload.get("version")
    expected_fields = POINTER_V1_FIELDS if version == 1 else POINTER_FIELDS
    if set(payload) != expected_fields:
        raise ValueError("invalid active task pointer: unexpected JSON fields")
    if type(version) is not int or version not in {1, POINTER_VERSION}:
        raise ValueError("invalid active task pointer: unsupported version")
    for name in ("repository", "worktree", "branch", "issue", "activatedAt"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"invalid active task pointer: {name} must be a non-empty string")
    for name in ("repository", "worktree"):
        value = payload[name]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"invalid active task pointer: {name} fingerprint is malformed")
    issue = _normalized_issue(payload["issue"])
    _timestamp(payload["activatedAt"])
    if version == 1:
        task_mode = contract_id = contract_version = contract_file = ""
    else:
        for name in ("taskMode", "contractId", "contractVersion", "contractFile"):
            if not isinstance(payload[name], str) or not payload[name]:
                raise ValueError(f"invalid active task pointer: {name} must be a non-empty string")
        task_mode = payload["taskMode"]
        contract_id = payload["contractId"]
        contract_version = payload["contractVersion"]
        contract_file = payload["contractFile"]
        if task_mode not in TASK_MODES:
            raise ValueError("invalid active task pointer: taskMode is unsupported")
        legacy_binding = (
            contract_id == LEGACY_CONTRACT_ID
            and contract_version == LEGACY_CONTRACT_VERSION
            and contract_file == LEGACY_CONTRACT_FILE
        )
        if (task_mode == "legacy") != legacy_binding:
            raise ValueError("invalid active task pointer: taskMode and contract binding disagree")
    return ActiveTaskPointer(
        version=version,
        repository=payload["repository"],
        worktree=payload["worktree"],
        branch=payload["branch"],
        issue=issue,
        taskMode=task_mode,
        contractId=contract_id,
        contractVersion=contract_version,
        contractFile=contract_file,
        activatedAt=payload["activatedAt"],
    )


def _capture_file(
    read_root: Path,
    path: Path,
    allowed_root: Path,
    label: str,
    *,
    required: bool = True,
) -> object:
    from .local_artifacts import MAX_SMALL_ARTIFACT_BYTES, capture_stable_file

    return capture_stable_file(
        read_root,
        path,
        allowed_root,
        label,
        required=required,
        max_bytes=MAX_SMALL_ARTIFACT_BYTES,
    )


def _json_snapshot(snapshot: object, label: str) -> object:
    try:
        return loads_unique_json(_snapshot_text(snapshot, label))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} JSON") from exc


def _read_pointer_snapshot(snapshot: object) -> ActiveTaskPointer:
    return _pointer(_json_snapshot(snapshot, "active task pointer"))


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"invalid task authority: {label} fingerprint is malformed")
    return value


def _authority(payload: object) -> TaskAuthority:
    if not isinstance(payload, dict) or set(payload) != AUTHORITY_FIELDS:
        raise ValueError("invalid task authority: unexpected JSON fields")
    if type(payload["version"]) is not int or payload["version"] != AUTHORITY_VERSION:
        raise ValueError("invalid task authority: unsupported version")
    repository = _fingerprint(payload["repository"], "repository")
    worktree = _fingerprint(payload["worktree"], "worktree")
    issue = _normalized_issue(payload["issue"])
    for name in ("taskMode", "contractId", "contractVersion", "contractFile"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"invalid task authority: {name} must be a non-empty string")
    task_mode = payload["taskMode"]
    binding = (payload["contractId"], payload["contractVersion"], payload["contractFile"])
    legacy_binding = (LEGACY_CONTRACT_ID, LEGACY_CONTRACT_VERSION, LEGACY_CONTRACT_FILE)
    if task_mode not in TASK_MODES or (task_mode == "legacy") != (binding == legacy_binding):
        raise ValueError("invalid task authority: taskMode and contract binding disagree")
    source_file = payload["legacySourceFile"]
    source_digest = payload["legacySourceDigest"]
    if task_mode == "modern-contract":
        if source_file is not None or source_digest is not None:
            raise ValueError("invalid task authority: modern authority must not carry legacy provenance")
    else:
        if source_file != LEGACY_CONTRACT_FILE:
            raise ValueError("invalid task authority: legacy source provenance is malformed")
        if (
            not isinstance(source_digest, str)
            or len(source_digest) != 64
            or any(char not in "0123456789abcdef" for char in source_digest)
        ):
            raise ValueError("invalid task authority: legacy source digest is malformed")
    return TaskAuthority(
        version=AUTHORITY_VERSION,
        repository=repository,
        worktree=worktree,
        issue=issue,
        taskMode=task_mode,
        contractId=payload["contractId"],
        contractVersion=payload["contractVersion"],
        contractFile=payload["contractFile"],
        legacySourceFile=source_file,
        legacySourceDigest=source_digest,
    )


def _authority_from_state(
    bindings: GitBindings,
    state: TaskState,
    *,
    legacy_source_digest: str | None = None,
) -> TaskAuthority:
    task_mode, contract_id, contract_version, contract_file = _task_contract_binding(state)
    return TaskAuthority(
        version=AUTHORITY_VERSION,
        repository=bindings.repository,
        worktree=bindings.worktree,
        issue=state.issue,
        taskMode=task_mode,
        contractId=contract_id,
        contractVersion=contract_version,
        contractFile=contract_file,
        legacySourceFile=LEGACY_CONTRACT_FILE if task_mode == "legacy" else None,
        legacySourceDigest=legacy_source_digest if task_mode == "legacy" else None,
    )


def _authority_snapshot(repo_root: Path, bindings: GitBindings, issue: str, *, required: bool) -> object:
    common_dir = git_path(repo_root, "--git-common-dir")
    path = task_authority_file(repo_root, bindings.worktree, issue)
    return _capture_file(common_dir, path, common_dir, "task authority", required=required)


def _load_authority(
    repo_root: Path,
    bindings: GitBindings,
    issue: str,
    *,
    required: bool,
) -> tuple[object, TaskAuthority | None]:
    snapshot = _authority_snapshot(repo_root, bindings, issue, required=required)
    if not getattr(snapshot, "exists"):
        return snapshot, None
    authority = _authority(_json_snapshot(snapshot, "task authority"))
    if authority.repository != bindings.repository:
        raise ValueError("task authority repository mismatch")
    if authority.worktree != bindings.worktree:
        raise ValueError("task authority worktree mismatch")
    if authority.issue != _normalized_issue(issue):
        raise ValueError(f"task authority Issue mismatch: expected {issue}, found {authority.issue}")
    return snapshot, authority


def _write_authority(path: Path, authority: TaskAuthority) -> None:
    _write_atomic(path, json.dumps(asdict(authority), ensure_ascii=True, indent=2) + "\n")


def _validate_authority_state(authority: TaskAuthority, state: TaskState) -> None:
    binding = _task_contract_binding(state)
    expected = (authority.taskMode, authority.contractId, authority.contractVersion, authority.contractFile)
    if authority.taskMode == "modern-contract" and binding[0] == "legacy":
        raise ValueError("modern task authority cannot downgrade to legacy")
    if binding != expected:
        raise ValueError("task authority contract binding mismatch")


def _validate_legacy_authority_provenance(
    repo_root: Path,
    bindings: GitBindings,
    authority: TaskAuthority,
    state: TaskState,
    *,
    task_snapshot: object | None = None,
) -> tuple[object, object]:
    if authority.taskMode != "legacy":
        raise ValueError("legacy provenance validation requires legacy task authority")
    source_snapshot, canonical_state, rendered = _legacy_migration_source(repo_root, bindings)
    source_digest = hashlib.sha256(getattr(source_snapshot, "content") or b"").hexdigest()
    if source_digest != authority.legacySourceDigest:
        raise ValueError("legacy task authority source provenance mismatch")
    if canonical_state != state:
        raise ValueError("legacy task-state does not match canonical migrated task-state")
    if task_snapshot is None:
        task_snapshot = _task_state_snapshot(task_state_file(repo_root, state.issue))
    if getattr(task_snapshot, "content") != rendered.encode("utf-8"):
        raise ValueError("legacy task-state does not match canonical migrated task-state")
    return source_snapshot, task_snapshot


def _revalidate_legacy_provenance(repo_root: Path, snapshots: tuple[object, ...]) -> None:
    from .local_artifacts import revalidate_snapshots

    revalidate_snapshots(repo_root, snapshots, "legacy task provenance")


def _validate_authority_pointer(authority: TaskAuthority, pointer: ActiveTaskPointer) -> None:
    expected = (
        authority.repository,
        authority.worktree,
        authority.issue,
        authority.taskMode,
        authority.contractId,
        authority.contractVersion,
        authority.contractFile,
    )
    actual = (
        pointer.repository,
        pointer.worktree,
        pointer.issue,
        pointer.taskMode,
        pointer.contractId,
        pointer.contractVersion,
        pointer.contractFile,
    )
    if actual != expected:
        raise ValueError("active task pointer and task authority disagree")


def task_authority_exists(repo_root: Path, issue: str) -> bool:
    bindings = resolve_bindings(repo_root)
    snapshot, _ = _load_authority(repo_root, bindings, issue, required=False)
    return bool(getattr(snapshot, "exists"))


def task_authority_issues(repo_root: Path) -> tuple[str, ...]:
    from .local_artifacts import revalidate_snapshots

    bindings = resolve_bindings(repo_root)
    common_dir = git_path(repo_root, "--git-common-dir")
    authority_root = task_authority_file(repo_root, bindings.worktree, "inventory").parent.parent

    def candidates() -> tuple[Path, ...]:
        if not authority_root.exists():
            return ()
        return tuple(sorted(authority_root.glob("issue-*/authority.json")))

    before = candidates()
    snapshots = []
    issues = []
    for path in before:
        issue = path.parent.name.removeprefix("issue-")
        normalized = _normalized_issue(issue)
        snapshot = _capture_file(common_dir, path, common_dir, "task authority")
        authority = _authority(_json_snapshot(snapshot, "task authority"))
        if authority.repository != bindings.repository:
            raise ValueError("task authority repository mismatch")
        if authority.worktree != bindings.worktree:
            raise ValueError("task authority worktree mismatch")
        if authority.issue != normalized:
            raise ValueError(f"task authority Issue mismatch: expected {normalized}, found {authority.issue}")
        snapshots.append(snapshot)
        issues.append(normalized)
    if candidates() != before:
        raise ValueError("task authority inventory changed while reading")
    revalidate_snapshots(common_dir, tuple(snapshots), "task authority inventory")
    return tuple(issues)


def _validate_pointer_bindings(pointer: ActiveTaskPointer, bindings: GitBindings) -> None:
    if pointer.repository != bindings.repository:
        raise ValueError("active task repository mismatch")
    if pointer.worktree != bindings.worktree:
        raise ValueError("active task worktree mismatch")
    if pointer.branch != bindings.branch:
        raise ValueError(f"active task branch mismatch: expected {bindings.branch}, found {pointer.branch}")


def _task_contract_binding(state: TaskState) -> tuple[str, str, str, str]:
    if state.contract == LEGACY_CONTRACT_REF and state.contract_file == LEGACY_CONTRACT_FILE:
        return "legacy", LEGACY_CONTRACT_ID, LEGACY_CONTRACT_VERSION, LEGACY_CONTRACT_FILE
    contract_id, separator, contract_version = state.contract.rpartition("@")
    if not separator or not contract_id or not contract_version:
        raise ValueError("task-state Contract must bind a contract id and version")
    if state.contract == LEGACY_CONTRACT_REF or state.contract_file == LEGACY_CONTRACT_FILE:
        raise ValueError("task-state legacy contract binding is incomplete")
    return "modern-contract", contract_id, contract_version, state.contract_file


def _validate_pointer_state(
    pointer: ActiveTaskPointer,
    state: TaskState,
    bindings: GitBindings,
) -> None:
    if state.issue != pointer.issue:
        raise ValueError(f"active task Issue mismatch: expected {pointer.issue}, found {state.issue}")
    if state.branch != pointer.branch or state.branch != bindings.branch:
        raise ValueError(f"task-state branch mismatch: expected {bindings.branch}, found {state.branch}")
    expected = _task_contract_binding(state)
    actual = (pointer.taskMode, pointer.contractId, pointer.contractVersion, pointer.contractFile)
    if actual != expected:
        raise ValueError("active task pointer contract binding mismatch")


def _v2_pointer(pointer: ActiveTaskPointer, state: TaskState) -> ActiveTaskPointer:
    task_mode, contract_id, contract_version, contract_file = _task_contract_binding(state)
    if task_mode == "legacy":
        raise ValueError("legacy active task pointer schema v1 cannot be migrated safely; reactivate the task")
    return ActiveTaskPointer(
        version=POINTER_VERSION,
        repository=pointer.repository,
        worktree=pointer.worktree,
        branch=pointer.branch,
        issue=pointer.issue,
        taskMode=task_mode,
        contractId=contract_id,
        contractVersion=contract_version,
        contractFile=contract_file,
        activatedAt=pointer.activatedAt,
    )


def _write_pointer(path: Path, pointer: ActiveTaskPointer) -> None:
    _write_atomic(path, json.dumps(asdict(pointer), ensure_ascii=True, indent=2) + "\n")


def _capture_pointer_snapshots(repo_root: Path, bindings: GitBindings) -> tuple[object, object]:
    common_dir = git_path(repo_root, "--git-common-dir")
    path = active_task_pointer_file(repo_root, bindings.worktree)
    legacy_path = legacy_active_task_pointer_file(repo_root, bindings.worktree)
    current = _capture_file(common_dir, path, common_dir, "active task pointer", required=False)
    legacy = _capture_file(repo_root.resolve(), legacy_path, repo_root.resolve(), "legacy active task pointer", required=False)
    return current, legacy


def _pointer_snapshots(repo_root: Path, bindings: GitBindings) -> tuple[object, object]:
    common_dir = git_path(repo_root, "--git-common-dir")
    legacy_path = legacy_active_task_pointer_file(repo_root, bindings.worktree)
    current, legacy = _capture_pointer_snapshots(repo_root, bindings)
    if getattr(current, "exists") and getattr(legacy, "exists"):
        if getattr(current, "content") != getattr(legacy, "content"):
            raise ValueError("conflicting active task pointers in git common-dir and legacy worktree location")
        current_pointer = _read_pointer_snapshot(current)
        legacy_pointer = _read_pointer_snapshot(legacy)
        _validate_pointer_bindings(current_pointer, bindings)
        _validate_pointer_bindings(legacy_pointer, bindings)
        from .local_artifacts import revalidate_snapshots

        revalidate_snapshots(common_dir, (current,), "active task pointer")
        revalidate_snapshots(repo_root.resolve(), (legacy,), "legacy active task pointer")
        legacy_path.unlink()
        legacy = _capture_file(
            repo_root.resolve(),
            legacy_path,
            repo_root.resolve(),
            "legacy active task pointer",
            required=False,
        )
    return current, legacy


def _legacy_migration_source(
    repo_root: Path,
    bindings: GitBindings,
) -> tuple[object, TaskState, str]:
    root = repo_root.resolve()
    legacy = root / LEGACY_CONTRACT_FILE
    snapshot = _capture_file(root, legacy, root, "current task state file")
    text = _snapshot_text(snapshot, "current task state file")
    raw_issue = _required(_legacy_field(text, "Issue"), "current task Issue")
    try:
        issue = _normalized_issue(raw_issue)
    except ValueError as exc:
        raise ValueError(f"current task Issue is invalid: {exc}") from exc
    raw_state = _required(_legacy_field(text, "State"), "current task State")
    execution_state = LEGACY_GATE_STATES.get(raw_state, raw_state)
    if execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown current task State: {raw_state}")
    state = TaskState(
        issue=issue,
        execution_state=execution_state,
        semantic_phase="none",
        classification="implementation-gap",
        contract=LEGACY_CONTRACT_REF,
        contract_file=LEGACY_CONTRACT_FILE,
        contract_change_required=False,
        branch=bindings.branch,
        base="main",
        allowed_actions=_actions(text, "## Allowed Actions"),
        forbidden_actions=_actions(text, "## Forbidden Actions"),
        human_gate="legacy task state requires human gate confirmation",
        human_approval_ref="none",
    )
    return snapshot, state, render_task_state(state)


def _require_exact_migration_state(repo_root: Path, state: TaskState, rendered: str) -> TaskState:
    target = task_state_file(repo_root, state.issue)
    root = repo_root.resolve()
    snapshot = _capture_file(root, target, target.parent, "task-state", required=False)
    expected = rendered.encode("utf-8")
    if getattr(snapshot, "exists"):
        if getattr(snapshot, "content") != expected:
            raise ValueError(f"conflicting task-state already exists: {target}")
    else:
        _write_atomic(target, rendered)
    loaded_snapshot, loaded = _load_task_state(target, binding_mode="recorded")
    if getattr(loaded_snapshot, "content") != expected or loaded != state:
        raise ValueError(f"conflicting task-state already exists: {target}")
    return loaded


def _load_pointer(repo_root: Path, bindings: GitBindings) -> ActiveTaskPointer:
    from .local_artifacts import revalidate_snapshots

    common_dir = git_path(repo_root, "--git-common-dir")
    root = repo_root.resolve()
    path = active_task_pointer_file(repo_root, bindings.worktree)
    legacy_path = legacy_active_task_pointer_file(repo_root, bindings.worktree)
    current_snapshot, legacy_snapshot = _capture_pointer_snapshots(repo_root, bindings)
    current_pointer = _read_pointer_snapshot(current_snapshot) if getattr(current_snapshot, "exists") else None
    legacy_pointer = _read_pointer_snapshot(legacy_snapshot) if getattr(legacy_snapshot, "exists") else None
    for candidate in (current_pointer, legacy_pointer):
        if candidate is not None:
            _validate_pointer_bindings(candidate, bindings)
    if (
        current_pointer is not None
        and legacy_pointer is not None
        and getattr(current_snapshot, "content") != getattr(legacy_snapshot, "content")
        and not (current_pointer.version == POINTER_VERSION and legacy_pointer.version == 1)
    ):
        raise ValueError("conflicting active task pointers in git common-dir and legacy worktree location")
    pointer_source_snapshot = current_snapshot if getattr(current_snapshot, "exists") else legacy_snapshot
    if not getattr(pointer_source_snapshot, "exists"):
        raise ValueError(f"missing active task pointer: {path}")
    pointer = current_pointer if current_pointer is not None else legacy_pointer
    assert pointer is not None
    state_snapshot, state = _load_task_state(
        task_state_file(repo_root, pointer.issue),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    if state.issue != pointer.issue:
        raise ValueError(f"active task Issue mismatch: expected {pointer.issue}, found {state.issue}")
    if state.branch != pointer.branch or state.branch != bindings.branch:
        raise ValueError(f"task-state branch mismatch: expected {bindings.branch}, found {state.branch}")
    if pointer.version == 1:
        pointer = _v2_pointer(pointer, state)
    else:
        _validate_pointer_state(pointer, state, bindings)
    if (
        current_pointer is not None
        and legacy_pointer is not None
        and getattr(current_snapshot, "content") != getattr(legacy_snapshot, "content")
    ):
        if current_pointer != _v2_pointer(legacy_pointer, state):
            raise ValueError("conflicting active task pointers in git common-dir and legacy worktree location")
        pointer = current_pointer

    authority_snapshot, authority = _load_authority(repo_root, bindings, pointer.issue, required=False)
    if authority is None:
        if pointer.taskMode == "legacy":
            source, migrated_state, rendered = _legacy_migration_source(repo_root, bindings)
            if migrated_state != state or getattr(_task_state_snapshot(task_state_file(repo_root, state.issue)), "content") != rendered.encode("utf-8"):
                raise ValueError("legacy task authority requires validated current-task migration")
            authority = _authority_from_state(
                bindings,
                state,
                legacy_source_digest=hashlib.sha256(getattr(source, "content") or b"").hexdigest(),
            )
        else:
            authority = _authority_from_state(bindings, state)
        _write_authority(task_authority_file(repo_root, bindings.worktree, state.issue), authority)
        authority_snapshot, recovered_authority = _load_authority(
            repo_root,
            bindings,
            pointer.issue,
            required=True,
        )
        assert recovered_authority is not None
        authority = recovered_authority
    _validate_authority_state(authority, state)
    _validate_authority_pointer(authority, pointer)
    legacy_snapshots: tuple[object, ...] = ()
    if authority.taskMode == "legacy":
        source_snapshot, validated_task_snapshot = _validate_legacy_authority_provenance(
            repo_root,
            bindings,
            authority,
            state,
            task_snapshot=state_snapshot,
        )
        legacy_snapshots = (source_snapshot, validated_task_snapshot, authority_snapshot)

    revalidate_snapshots(common_dir, (current_snapshot, authority_snapshot), "active task recovery")
    revalidate_snapshots(root, (legacy_snapshot, state_snapshot), "active task recovery")
    if legacy_snapshots:
        _revalidate_legacy_provenance(repo_root, legacy_snapshots)
    if pointer.version == POINTER_VERSION and (
        getattr(pointer_source_snapshot, "path") == legacy_path or getattr(current_snapshot, "content") != (
            json.dumps(asdict(pointer), ensure_ascii=True, indent=2) + "\n"
        ).encode("utf-8")
    ):
        _write_pointer(path, pointer)
    if getattr(legacy_snapshot, "exists"):
        legacy_path.unlink()
    return pointer


def _state_for_pointer(repo_root: Path, pointer: ActiveTaskPointer, bindings: GitBindings) -> TaskState:
    task_snapshot, state = _load_task_state(task_state_file(repo_root, pointer.issue))
    _validate_pointer_state(pointer, state, bindings)
    authority_snapshot, authority = _load_authority(repo_root, bindings, pointer.issue, required=True)
    assert authority is not None
    _validate_authority_state(authority, state)
    _validate_authority_pointer(authority, pointer)
    if authority.taskMode == "legacy":
        source_snapshot, task_snapshot = _validate_legacy_authority_provenance(
            repo_root,
            bindings,
            authority,
            state,
            task_snapshot=task_snapshot,
        )
        _revalidate_legacy_provenance(repo_root, (source_snapshot, task_snapshot, authority_snapshot))
    return state


def load_active_task_snapshot(repo_root: Path) -> tuple[GitBindings, TaskState]:
    from .local_artifacts import revalidate_snapshots

    bindings = resolve_bindings(repo_root)
    common_dir = git_path(repo_root, "--git-common-dir")
    root = repo_root.resolve()
    path = active_task_pointer_file(repo_root, bindings.worktree)
    legacy_path = legacy_active_task_pointer_file(repo_root, bindings.worktree)
    current = _capture_file(common_dir, path, common_dir, "active task pointer", required=False)
    legacy = _capture_file(root, legacy_path, root, "legacy active task pointer", required=False)
    available = tuple(snapshot for snapshot in (current, legacy) if getattr(snapshot, "exists"))
    if not available:
        raise ValueError(f"missing active task pointer: {path}")
    if len(available) == 2 and getattr(current, "content") != getattr(legacy, "content"):
        raise ValueError("conflicting active task pointers in git common-dir and legacy worktree location")
    pointer = _read_pointer_snapshot(current if getattr(current, "exists") else legacy)
    _validate_pointer_bindings(pointer, bindings)
    state_snapshot, state = _load_task_state(
        task_state_file(repo_root, pointer.issue),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    if pointer.version == 1:
        pointer = _v2_pointer(pointer, state)
    else:
        _validate_pointer_state(pointer, state, bindings)
    authority_snapshot, authority = _load_authority(repo_root, bindings, pointer.issue, required=True)
    assert authority is not None
    _validate_authority_state(authority, state)
    _validate_authority_pointer(authority, pointer)
    legacy_snapshots: tuple[object, ...] = ()
    if authority.taskMode == "legacy":
        source_snapshot, validated_task_snapshot = _validate_legacy_authority_provenance(
            repo_root,
            bindings,
            authority,
            state,
            task_snapshot=state_snapshot,
        )
        legacy_snapshots = (source_snapshot, validated_task_snapshot, authority_snapshot)

    revalidate_snapshots(common_dir, (current, authority_snapshot), "active task snapshot")
    revalidate_snapshots(root, (legacy, state_snapshot), "active task snapshot")
    if legacy_snapshots:
        _revalidate_legacy_provenance(repo_root, legacy_snapshots)
    return bindings, state


@repository_locked
def activate_task(repo_root: Path, issue: str) -> TaskState:
    bindings = resolve_bindings(repo_root)
    _, state = _load_task_state(task_state_file(repo_root, issue))
    if state.branch != bindings.branch:
        raise ValueError(f"task-state branch mismatch: expected {bindings.branch}, found {state.branch}")
    task_mode, contract_id, contract_version, contract_file = _task_contract_binding(state)
    _, existing_authority = _load_authority(repo_root, bindings, state.issue, required=False)
    if task_mode == "legacy":
        if existing_authority is not None and existing_authority.taskMode == "modern-contract":
            raise ValueError("modern task authority cannot downgrade to legacy")
        raise ValueError("legacy task mode requires validated current-task migration")
    authority = _authority_from_state(bindings, state)
    if existing_authority is not None and existing_authority.taskMode == "modern-contract":
        _validate_authority_state(existing_authority, state)
        authority = existing_authority
    _pointer_snapshots(repo_root, bindings)
    pointer = ActiveTaskPointer(
        version=POINTER_VERSION,
        repository=bindings.repository,
        worktree=bindings.worktree,
        branch=bindings.branch,
        issue=state.issue,
        taskMode=task_mode,
        contractId=contract_id,
        contractVersion=contract_version,
        contractFile=contract_file,
        activatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    _write_authority(task_authority_file(repo_root, bindings.worktree, state.issue), authority)
    _write_pointer(active_task_pointer_file(repo_root, bindings.worktree), pointer)
    legacy_active_task_pointer_file(repo_root, bindings.worktree).unlink(missing_ok=True)
    return state


@repository_locked
def load_active_pointer(repo_root: Path) -> ActiveTaskPointer:
    bindings = resolve_bindings(repo_root)
    return _load_pointer(repo_root, bindings)


@repository_locked
def load_active_task(repo_root: Path) -> TaskState:
    bindings = resolve_bindings(repo_root)
    return _state_for_pointer(repo_root, _load_pointer(repo_root, bindings), bindings)


def check_task_binding(repo_root: Path, issue: str | None = None) -> TaskState:
    state = load_active_task(repo_root)
    if issue is not None:
        expected = _normalized_issue(issue)
        if state.issue != expected:
            raise ValueError(f"active task Issue mismatch: expected {expected}, found {state.issue}")
    return state


def list_task_states(repo_root: Path) -> tuple[TaskState, ...]:
    issues = (repo_root.resolve() / ".xflow" / "issues")
    if not issues.is_dir():
        return ()
    states = []
    for path in issues.glob("issue-*/task-state.md"):
        if path.parent.parent == issues:
            states.append(parse_task_state(path, binding_mode="recorded"))
    return tuple(sorted(states, key=lambda item: item.issue))


def _legacy_field(text: str, name: str) -> str:
    return _field(text, name)


@repository_locked
def migrate_legacy_current_task(repo_root: Path) -> TaskState:
    bindings = resolve_bindings(repo_root)
    source, state, rendered = _legacy_migration_source(repo_root, bindings)
    digest = hashlib.sha256(getattr(source, "content") or b"").hexdigest()
    authority = _authority_from_state(bindings, state, legacy_source_digest=digest)
    _, existing_authority = _load_authority(repo_root, bindings, state.issue, required=False)
    if existing_authority is not None:
        if existing_authority.taskMode == "modern-contract":
            raise ValueError("modern task authority cannot downgrade to legacy")
        if existing_authority != authority:
            raise ValueError("legacy task authority source provenance mismatch")
        authority = existing_authority

    current_snapshot, legacy_snapshot = _pointer_snapshots(repo_root, bindings)
    existing_pointer: ActiveTaskPointer | None = None
    source_pointer = current_snapshot if getattr(current_snapshot, "exists") else legacy_snapshot
    if getattr(source_pointer, "exists"):
        existing_pointer = _read_pointer_snapshot(source_pointer)
        _validate_pointer_bindings(existing_pointer, bindings)
        if existing_pointer.issue != state.issue:
            raise ValueError(
                f"active task pointer Issue mismatch: expected {state.issue}, found {existing_pointer.issue}"
            )
        if existing_pointer.version != POINTER_VERSION:
            if existing_authority is None or existing_authority.taskMode != "legacy":
                raise ValueError("existing active task pointer cannot be migrated safely")
        else:
            existing_binding = (
                existing_pointer.taskMode,
                existing_pointer.contractId,
                existing_pointer.contractVersion,
                existing_pointer.contractFile,
            )
            if existing_pointer.taskMode == "modern-contract":
                raise ValueError("modern task authority cannot downgrade to legacy")
            expected_binding = ("legacy", LEGACY_CONTRACT_ID, LEGACY_CONTRACT_VERSION, LEGACY_CONTRACT_FILE)
            if existing_binding != expected_binding:
                raise ValueError("existing active task pointer conflicts with legacy migration")
    state = _require_exact_migration_state(repo_root, state, rendered)
    expected_binding = ("legacy", LEGACY_CONTRACT_ID, LEGACY_CONTRACT_VERSION, LEGACY_CONTRACT_FILE)
    if (
        existing_pointer is not None
        and existing_pointer.version == POINTER_VERSION
        and existing_pointer.issue == state.issue
        and (
            existing_pointer.taskMode,
            existing_pointer.contractId,
            existing_pointer.contractVersion,
            existing_pointer.contractFile,
        ) == expected_binding
    ):
        pointer = existing_pointer
    else:
        pointer = ActiveTaskPointer(
            version=POINTER_VERSION,
            repository=bindings.repository,
            worktree=bindings.worktree,
            branch=bindings.branch,
            issue=state.issue,
            taskMode="legacy",
            contractId=LEGACY_CONTRACT_ID,
            contractVersion=LEGACY_CONTRACT_VERSION,
            contractFile=LEGACY_CONTRACT_FILE,
            activatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    _write_authority(task_authority_file(repo_root, bindings.worktree, state.issue), authority)
    pointer_path = active_task_pointer_file(repo_root, bindings.worktree)
    encoded_pointer = (json.dumps(asdict(pointer), ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    if not getattr(current_snapshot, "exists") or getattr(current_snapshot, "content") != encoded_pointer:
        _write_pointer(pointer_path, pointer)
    if getattr(legacy_snapshot, "exists"):
        legacy_active_task_pointer_file(repo_root, bindings.worktree).unlink()
    return state
