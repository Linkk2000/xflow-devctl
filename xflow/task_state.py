from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .bindings import GitBindings, resolve_bindings
from .io import read_text
from .paths import active_task_pointer_file, normalized_issue, task_state_file


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
POINTER_VERSION = 1
POINTER_FIELDS = {"version", "repository", "worktree", "branch", "issue", "activatedAt"}
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
    activatedAt: str


def _field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^[ \t]*{re.escape(name)}[ \t]*:[ \t]*([^\r\n]*)[ \t]*$", text)
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


def _normalized_issue(value: str) -> str:
    try:
        issue = normalized_issue(value)
    except ValueError as exc:
        raise ValueError(f"invalid task-state Issue: {exc}") from exc
    if issue != value:
        raise ValueError("task-state Issue identifier is not normalized")
    return issue


def _required(text: str, label: str) -> str:
    if not text:
        raise ValueError(f"missing required task-state field: {label}")
    return text


def _validate_relative_approval(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 3 or path.parts[:2] != ("approvals", "history"):
        raise ValueError("Human Approval Ref must be an Issue-relative path under approvals/history/")


def parse_task_state(path: Path) -> TaskState:
    if not path.is_file():
        raise ValueError(f"missing task-state file: {path}")
    text = read_text(path)
    if not re.search(r"(?m)^# XFlow Task State\s*$", text):
        raise ValueError("missing required task-state heading: # XFlow Task State")
    issue = _normalized_issue(_required(_field(text, "Issue"), "Issue"))
    resolved = path.resolve()
    expected_parent = f"issue-{issue}"
    if (
        resolved.parent.name != expected_parent
        or resolved.parent.parent.name != "issues"
        or resolved.parent.parent.parent.name != ".xflow"
    ):
        raise ValueError("task-state file must be inside the matching Issue directory")
    execution_state = _required(_field(text, "Execution State"), "Execution State")
    if execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown task-state Execution State: {execution_state}")
    semantic_phase = _required(_field(text, "Semantic Phase"), "Semantic Phase")
    if semantic_phase not in SEMANTIC_PHASES:
        raise ValueError(f"unknown task-state Semantic Phase: {semantic_phase}")
    classification = _required(_field(text, "Classification"), "Classification")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown task-state Classification: {classification}")
    boolean = _required(_field(text, "Contract Change Required"), "Contract Change Required")
    if boolean not in {"yes", "no"}:
        raise ValueError("Contract Change Required must be yes or no")
    human_gate = _required(_field(text, "Human Gate"), "Human Gate")
    human_approval_ref = _required(_field(text, "Human Approval Ref"), "Human Approval Ref")
    needs_approval = SEMANTIC_PHASES.index(semantic_phase) >= SEMANTIC_PHASES.index("accepted-design")
    if needs_approval:
        if human_approval_ref == "none":
            raise ValueError("Human Approval Ref is required once Semantic Phase is accepted-design or later")
        _validate_relative_approval(human_approval_ref)
    elif human_approval_ref != "none":
        _validate_relative_approval(human_approval_ref)
    return TaskState(
        issue=issue,
        execution_state=execution_state,
        semantic_phase=semantic_phase,
        classification=classification,
        contract=_required(_field(text, "Contract"), "Contract"),
        contract_file=_required(_field(text, "Contract File"), "Contract File"),
        contract_change_required=boolean == "yes",
        branch=_required(_field(text, "Branch"), "Branch"),
        base=_required(_field(text, "Base"), "Base"),
        allowed_actions=_actions(text, "## Allowed Actions"),
        forbidden_actions=_actions(text, "## Forbidden Actions"),
        human_gate=human_gate,
        human_approval_ref=human_approval_ref,
    )


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
    if not isinstance(payload, dict) or set(payload) != POINTER_FIELDS:
        raise ValueError("invalid active task pointer: unexpected JSON fields")
    if type(payload["version"]) is not int or payload["version"] != POINTER_VERSION:
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
    return ActiveTaskPointer(
        version=payload["version"], repository=payload["repository"], worktree=payload["worktree"],
        branch=payload["branch"], issue=issue, activatedAt=payload["activatedAt"],
    )


def _load_pointer(repo_root: Path, bindings: GitBindings) -> ActiveTaskPointer:
    path = active_task_pointer_file(repo_root, bindings.worktree)
    if not path.is_file():
        raise ValueError(f"missing active task pointer: {path}")
    try:
        pointer = _pointer(json.loads(read_text(path)))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid active task pointer: cannot read {path}: {exc}") from exc
    if pointer.repository != bindings.repository:
        raise ValueError("active task repository mismatch")
    if pointer.worktree != bindings.worktree:
        raise ValueError("active task worktree mismatch")
    if pointer.branch != bindings.branch:
        raise ValueError(f"active task branch mismatch: expected {bindings.branch}, found {pointer.branch}")
    return pointer


def _state_for_pointer(repo_root: Path, pointer: ActiveTaskPointer, bindings: GitBindings) -> TaskState:
    state = parse_task_state(task_state_file(repo_root, pointer.issue))
    if state.branch != bindings.branch:
        raise ValueError(f"active task branch mismatch: expected {bindings.branch}, found {state.branch}")
    return state


def activate_task(repo_root: Path, issue: str) -> TaskState:
    bindings = resolve_bindings(repo_root)
    state = parse_task_state(task_state_file(repo_root, issue))
    if state.branch != bindings.branch:
        raise ValueError(f"task-state branch mismatch: expected {bindings.branch}, found {state.branch}")
    pointer = ActiveTaskPointer(
        version=POINTER_VERSION, repository=bindings.repository, worktree=bindings.worktree, branch=bindings.branch,
        issue=state.issue, activatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    _write_atomic(active_task_pointer_file(repo_root, bindings.worktree), json.dumps(asdict(pointer), ensure_ascii=True, indent=2) + "\n")
    return state


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
            states.append(parse_task_state(path))
    return tuple(sorted(states, key=lambda item: item.issue))


def _legacy_field(text: str, name: str) -> str:
    return _field(text, name)


def migrate_legacy_current_task(repo_root: Path) -> TaskState:
    legacy = repo_root.resolve() / ".xflow" / "current-task.md"
    if not legacy.is_file():
        raise ValueError(f"missing current task state file: {legacy}")
    text = read_text(legacy)
    raw_issue = _required(_legacy_field(text, "Issue"), "current task Issue")
    try:
        issue = _normalized_issue(raw_issue)
    except ValueError as exc:
        raise ValueError(f"current task Issue is invalid: {exc}") from exc
    raw_state = _required(_legacy_field(text, "State"), "current task State")
    execution_state = LEGACY_GATE_STATES.get(raw_state, raw_state)
    if execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown current task State: {raw_state}")
    allowed = _actions(text, "## Allowed Actions")
    forbidden = _actions(text, "## Forbidden Actions")
    bindings = resolve_bindings(repo_root)
    state = TaskState(
        issue=issue, execution_state=execution_state, semantic_phase="none", classification="implementation-gap",
        contract="legacy.current-task@0.1.0", contract_file=".xflow/current-task.md", contract_change_required=False,
        branch=bindings.branch, base="main", allowed_actions=allowed, forbidden_actions=forbidden,
        human_gate="legacy task state requires human gate confirmation", human_approval_ref="none",
    )
    target = task_state_file(repo_root, issue)
    _write_atomic(target, render_task_state(state))
    return state
