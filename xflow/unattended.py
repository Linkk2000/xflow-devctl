from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .bindings import resolve_bindings
from .paths import normalized_issue


CONFIRMATION = "XFLOW_HUMAN_UNATTENDED_ALL"
STATE_VERSION = 1
STATE_MODE = "task-unattended"
STATE_FIELDS = {"version", "mode", "repository", "worktree", "issue", "enabledAt"}
CURRENT_TASK_FIELD_RE = re.compile(r"(?im)^\s*(Issue|State)\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class UnattendedState:
    version: int
    mode: str
    repository: str
    worktree: str
    issue: str
    enabledAt: str


def state_path(repo_root: Path) -> Path:
    return repo_root.resolve() / ".xflow" / "local" / "unattended.json"


def _current_task_binding(repo_root: Path) -> tuple[str, str] | None:
    path = repo_root.resolve() / ".xflow" / "current-task.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    fields = {match.group(1).lower(): match.group(2).strip() for match in CURRENT_TASK_FIELD_RE.finditer(text)}
    issue = fields.get("issue", "")
    state = fields.get("state", "")
    return issue, state


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid unattended state: enabledAt must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid unattended state: enabledAt must include a timezone")


def _parse(payload: object) -> UnattendedState:
    if not isinstance(payload, dict) or set(payload) != STATE_FIELDS:
        raise ValueError("invalid unattended state: unexpected JSON fields")
    if type(payload["version"]) is not int or payload["version"] != STATE_VERSION:
        raise ValueError("invalid unattended state: unsupported version")
    for name in ("mode", "repository", "worktree", "issue", "enabledAt"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise ValueError(f"invalid unattended state: {name} must be a non-empty string")
    if payload["mode"] != STATE_MODE:
        raise ValueError("invalid unattended state: unsupported mode")
    for name in ("repository", "worktree"):
        value = payload[name]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"invalid unattended state: {name} fingerprint is malformed")
    try:
        issue = normalized_issue(payload["issue"])
    except ValueError as exc:
        raise ValueError(f"invalid unattended state: {exc}") from exc
    if issue != payload["issue"]:
        raise ValueError("invalid unattended state: Issue identifier is not normalized")
    _validate_timestamp(payload["enabledAt"])
    return UnattendedState(
        version=payload["version"],
        mode=payload["mode"],
        repository=payload["repository"],
        worktree=payload["worktree"],
        issue=issue,
        enabledAt=payload["enabledAt"],
    )


def _write(repo_root: Path, state: UnattendedState) -> None:
    path = state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(state), handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def enable(repo_root: Path, issue: str, confirmation: str) -> UnattendedState:
    if confirmation != CONFIRMATION:
        raise ValueError("confirmation does not exactly match the required unattended value")
    bindings = resolve_bindings(repo_root)
    state = UnattendedState(
        version=STATE_VERSION,
        mode=STATE_MODE,
        repository=bindings.repository,
        worktree=bindings.worktree,
        issue=normalized_issue(issue),
        enabledAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    _write(repo_root, state)
    return state


def load(repo_root: Path) -> UnattendedState | None:
    path = state_path(repo_root)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"invalid unattended state: expected a file at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid unattended state: cannot read {path}: {exc}") from exc
    state = _parse(payload)
    bindings = resolve_bindings(repo_root)
    if state.repository != bindings.repository:
        raise ValueError("unattended state repository mismatch")
    if state.worktree != bindings.worktree:
        raise ValueError("unattended state worktree mismatch")
    task_binding = _current_task_binding(repo_root)
    if task_binding is not None:
        task_issue, task_state = task_binding
        if task_state == "S10_DONE":
            disable(repo_root)
            raise ValueError("unattended state invalidated because the current task is completed")
        if not task_issue:
            disable(repo_root)
            raise ValueError("unattended state invalidated because the current task Issue is missing")
        try:
            normalized_task_issue = normalized_issue(task_issue)
        except ValueError as exc:
            disable(repo_root)
            raise ValueError(f"unattended state invalidated because the current task Issue is invalid: {exc}") from exc
        if normalized_task_issue != state.issue:
            disable(repo_root)
            raise ValueError(
                "unattended state invalidated by current task Issue mismatch: "
                f"expected {state.issue}, found {normalized_task_issue}"
            )
    return state


def require_active(repo_root: Path, issue: str) -> UnattendedState:
    state = load(repo_root)
    if state is None:
        raise ValueError("task-scoped unattended mode is not active")
    expected_issue = normalized_issue(issue)
    if state.issue != expected_issue:
        raise ValueError(f"unattended state Issue mismatch: expected {expected_issue}, found {state.issue}")
    return state


def disable(repo_root: Path) -> bool:
    path = state_path(repo_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def migrate_issue(repo_root: Path, old_issue: str, new_issue: str) -> UnattendedState:
    state = require_active(repo_root, old_issue)
    migrated = replace(state, issue=normalized_issue(new_issue))
    _write(repo_root, migrated)
    return migrated
