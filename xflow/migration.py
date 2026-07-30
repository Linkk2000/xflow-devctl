from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .approval import CREDENTIAL_PATTERNS
from .commit_message import (
    LOCAL_FILE_URI_RE,
    POSIX_ABSOLUTE_PATH_RE,
    REMOTE_URL_RE,
    UNC_OR_DEVICE_PATH_RE,
    WINDOWS_ABSOLUTE_PATH_RE,
)
from .project_config import ProjectConfig, _is_reparse_point, parse_project_config, require_safe_repo_path


@dataclass(frozen=True)
class MigrationReport:
    legacy_ops_present: bool
    v2_ops_present: bool
    messages: tuple[str, ...]


FileIdentity = tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    exists: bool
    identity: FileIdentity | None
    digest: str | None
    content: bytes | None = field(repr=False, compare=True)


@dataclass(frozen=True)
class IssueScanSnapshot:
    tree: tuple[tuple[str, str, FileIdentity], ...]
    files: tuple[FileSnapshot, ...]


@dataclass(frozen=True)
class MigrationSnapshot:
    config: FileSnapshot
    gitignore: FileSnapshot
    issues: IssueScanSnapshot
    effective_ignore: str | None


@dataclass(frozen=True)
class IgnoreMatch:
    source: str
    source_path: Path | None
    line: int | None
    pattern: str
    raw: str


@dataclass(frozen=True)
class IssueWorkspaceMigrationReport:
    mode: Literal["tracked", "local"]
    contract_root: Path
    git_ignore_source: str | None
    exact_ignore_lines: tuple[str, ...]
    active_approvals: tuple[Path, ...]
    oversized_files: tuple[Path, ...]
    absolute_path_files: tuple[Path, ...]
    credential_files: tuple[Path, ...]
    scan_errors: tuple[str, ...]
    manual_actions: tuple[str, ...]
    _snapshot: MigrationSnapshot | None = field(default=None, repr=False, compare=False)

    @property
    def blockers(self) -> tuple[str, ...]:
        messages: list[str] = []
        if self.scan_errors:
            messages.append("scan errors: " + "; ".join(self.scan_errors))
        if self.active_approvals:
            messages.append("active approvals: " + ", ".join(str(path) for path in self.active_approvals))
        unsafe: list[str] = []
        if self.oversized_files:
            unsafe.append("files over 10 MiB: " + ", ".join(str(path) for path in self.oversized_files))
        if self.absolute_path_files:
            unsafe.append("local absolute paths: " + ", ".join(str(path) for path in self.absolute_path_files))
        if self.credential_files:
            unsafe.append("credential-like text: " + ", ".join(str(path) for path in self.credential_files))
        if unsafe:
            messages.append("unsafe issue workspace: " + "; ".join(unsafe))
        messages.extend(self.manual_actions)
        return tuple(messages)


EXACT_ISSUE_IGNORE_LINES = {".xflow/issues", ".xflow/issues/"}
MAX_ISSUE_FILE_SIZE = 10 * 1024 * 1024
MAX_METADATA_FILE_SIZE = 10 * 1024 * 1024
READ_CHUNK_SIZE = 64 * 1024
CREDENTIAL_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[\"']?"
    r"(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|private[_-]?key|"
    r"(?:[A-Za-z0-9]+[_-])*(?:token|secret|password|credential))"
    r"[\"']?\s*[:=]"
)
JOURNAL_RELATIVE_PATH = Path(".xflow/local/issue-workspace-migration-journal.json")


class FileTooLarge(ValueError):
    pass


def _identity(path_stat: os.stat_result) -> FileIdentity:
    return (
        int(path_stat.st_dev),
        int(path_stat.st_ino),
        int(path_stat.st_mode),
        int(path_stat.st_size),
        int(path_stat.st_mtime_ns),
        int(path_stat.st_ctime_ns),
    )


def _same_file_object(left: FileIdentity, right: FileIdentity) -> bool:
    if left[0] and left[1] and right[0] and right[1]:
        return left[:4] == right[:4]
    return left[2:] == right[2:]


def _read_stable_file(
    repo_root: Path,
    path: Path,
    allowed_root: Path,
    limit: int,
    label: str,
) -> FileSnapshot:
    path = require_safe_repo_path(repo_root, path, label)
    allowed_root = require_safe_repo_path(repo_root, allowed_root, f"{label} allowed root")
    try:
        path.resolve(strict=False).relative_to(allowed_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"{label} is outside its allowed root: {path}") from exc
    try:
        before_path = os.lstat(path)
    except FileNotFoundError:
        return FileSnapshot(path, False, None, None, None)
    except OSError as exc:
        raise ValueError(f"cannot inspect {label} {path}: {exc}") from exc
    if _is_reparse_point(before_path):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"{label} must be a regular file: {path}")
    if before_path.st_size > limit:
        raise FileTooLarge(f"{label} exceeds {limit} bytes: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    try:
        before_handle = os.fstat(descriptor)
        if _identity(before_handle) != _identity(before_path):
            raise ValueError(f"{label} changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, min(READ_CHUNK_SIZE, limit + 1 - total))
            except OSError as exc:
                raise ValueError(f"cannot read {label} {path}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise FileTooLarge(f"{label} exceeds {limit} bytes while reading: {path}")
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} changed while reading: {path}: {exc}") from exc
    if _identity(before_handle) != _identity(after_handle) or _identity(after_handle) != _identity(after_path):
        raise ValueError(f"{label} changed while reading: {path}")
    require_safe_repo_path(repo_root, path, label)
    content = b"".join(chunks)
    return FileSnapshot(path, True, _identity(after_path), hashlib.sha256(content).hexdigest(), content)


def _contains_local_absolute_path(text: str) -> bool:
    path_scan = REMOTE_URL_RE.sub("", text)
    return any(
        pattern.search(path_scan)
        for pattern in (
            LOCAL_FILE_URI_RE,
            WINDOWS_ABSOLUTE_PATH_RE,
            POSIX_ABSOLUTE_PATH_RE,
            UNC_OR_DEVICE_PATH_RE,
        )
    )


def _contains_credential(text: str) -> bool:
    return CREDENTIAL_KEY_RE.search(text) is not None or any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS)


def _scan_issue_workspace(repo_root: Path) -> tuple[
    IssueScanSnapshot,
    tuple[Path, ...],
    tuple[Path, ...],
    tuple[Path, ...],
    tuple[Path, ...],
    tuple[str, ...],
]:
    issues_root = repo_root / ".xflow" / "issues"
    tree: list[tuple[str, str, FileIdentity]] = []
    snapshots: list[FileSnapshot] = []
    active_approvals: list[Path] = []
    oversized_files: list[Path] = []
    absolute_path_files: list[Path] = []
    credential_files: list[Path] = []
    errors: list[str] = []
    try:
        issues_root = require_safe_repo_path(repo_root, issues_root, "issue workspace")
        root_stat = os.lstat(issues_root)
    except FileNotFoundError:
        return IssueScanSnapshot((), ()), (), (), (), (), ()
    except (OSError, ValueError) as exc:
        return IssueScanSnapshot((), ()), (), (), (), (), (str(exc),)
    if _is_reparse_point(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        return IssueScanSnapshot((), ()), (), (), (), (), (f"issue workspace is a reparse point or not a directory: {issues_root}",)
    tree.append((".", "dir", _identity(root_stat)))

    def walk(directory: Path) -> None:
        try:
            require_safe_repo_path(repo_root, directory, "issue workspace directory")
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except (OSError, ValueError) as exc:
            errors.append(f"cannot scan issue workspace directory {directory}: {exc}")
            return
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(issues_root).as_posix()
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(f"cannot inspect issue workspace entry {path}: {exc}")
                continue
            if _is_reparse_point(entry_stat):
                errors.append(f"issue workspace entry is a symlink, junction, or reparse point: {path}")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                tree.append((relative, "dir", _identity(entry_stat)))
                walk(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                errors.append(f"issue workspace entry is not a regular file: {path}")
                continue
            tree.append((relative, "file", _identity(entry_stat)))
            try:
                snapshot = _read_stable_file(repo_root, path, issues_root, MAX_ISSUE_FILE_SIZE, "issue file")
            except FileTooLarge:
                oversized_files.append(path)
                continue
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if snapshot.identity is None or not _same_file_object(snapshot.identity, _identity(entry_stat)):
                errors.append(f"issue file changed while reading: {path}")
                continue
            snapshots.append(snapshot)
            text = (snapshot.content or b"").decode("utf-8", errors="replace")
            if path.name == "local-review.md" and "Approved: yes" in text:
                active_approvals.append(path)
            if _contains_local_absolute_path(text):
                absolute_path_files.append(path)
            if _contains_credential(text):
                credential_files.append(path)

    walk(issues_root)
    return (
        IssueScanSnapshot(tuple(sorted(tree)), tuple(sorted(snapshots, key=lambda item: str(item.path)))),
        tuple(sorted(active_approvals)),
        tuple(sorted(oversized_files)),
        tuple(sorted(absolute_path_files)),
        tuple(sorted(credential_files)),
        tuple(sorted(errors)),
    )


def _parse_ignore_output(repo_root: Path, output: bytes) -> IgnoreMatch:
    fields = output.rstrip(b"\0").split(b"\0")
    if len(fields) != 4:
        raise ValueError("unexpected NUL-delimited git check-ignore output")
    try:
        source, raw_line, pattern, candidate = (field.decode("utf-8", errors="strict") for field in fields)
        line = int(raw_line) if raw_line else None
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("unexpected git check-ignore source fields") from exc
    source_path = Path(source)
    if source not in {"command line", "::"}:
        source_path = source_path if source_path.is_absolute() else repo_root / source_path
        source_path = source_path.resolve(strict=False)
    else:
        source_path = None
    line_display = str(line) if line is not None else ""
    return IgnoreMatch(source, source_path, line, pattern, f"{source}:{line_display}:{pattern}\t{candidate}")


def _fallback_ignore_match(repo_root: Path) -> IgnoreMatch | None:
    gitignore = repo_root / ".gitignore"
    try:
        snapshot = _read_stable_file(repo_root, gitignore, repo_root, MAX_METADATA_FILE_SIZE, ".gitignore")
    except FileTooLarge as exc:
        raise ValueError(str(exc)) from exc
    if not snapshot.exists:
        return None
    matched: IgnoreMatch | None = None
    candidate = ".xflow/issues/probe"
    text = (snapshot.content or b"").decode("utf-8-sig", errors="strict")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        pattern = raw_line.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        normalized = pattern.lstrip("/")
        if normalized in EXACT_ISSUE_IGNORE_LINES or fnmatch.fnmatch(candidate, normalized):
            matched = IgnoreMatch(".gitignore", gitignore.resolve(strict=False), line_number, pattern, f".gitignore:{line_number}:{pattern}")
    return matched


def _effective_ignore_match(repo_root: Path) -> IgnoreMatch | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "core.quotepath=false",
            "check-ignore",
            "-v",
            "-z",
            "--no-index",
            "--stdin",
        ],
        check=False,
        input=b".xflow/issues/probe\0",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return _parse_ignore_output(repo_root, result.stdout)
    if result.returncode == 1:
        return None
    if (repo_root / ".git").exists():
        details = (result.stderr.strip() or result.stdout.strip()).decode("utf-8", errors="replace")
        raise ValueError(f"cannot inspect effective Git ignore source: {details}")
    return _fallback_ignore_match(repo_root)


def _config_from_snapshot(repo_root: Path, snapshot: FileSnapshot) -> tuple[ProjectConfig, dict[str, object]]:
    config_path = snapshot.path
    if not snapshot.exists:
        raw: object = {}
    else:
        try:
            raw = json.loads((snapshot.content or b"").decode("utf-8-sig", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid .xflow/xflow.json ({config_path}): must contain a UTF-8 JSON object") from exc
    config = parse_project_config(repo_root, config_path, raw)
    if not isinstance(raw, dict):
        raise ValueError(f"invalid .xflow/xflow.json ({config_path}): must contain a JSON object")
    return config, raw


def _manual_actions(repo_root: Path, mode: Literal["tracked", "local"], match: IgnoreMatch | None) -> tuple[str, ...]:
    if mode != "tracked" or match is None:
        return ()
    root_gitignore = (repo_root / ".gitignore").resolve(strict=False)
    if match.source_path != root_gitignore:
        return (
            f"manual action required: effective ignore rule {match.pattern!r} is outside repository root .gitignore ({match.raw})",
        )
    if match.pattern not in EXACT_ISSUE_IGNORE_LINES:
        return (
            f"manual action required: effective broad ignore rule {match.pattern!r} must be reviewed without automatic rewrite ({match.raw})",
        )
    return ()


def inspect_issue_workspace_migration(
    repo_root: Path, mode: Literal["tracked", "local"]
) -> IssueWorkspaceMigrationReport:
    if mode not in {"tracked", "local"}:
        raise ValueError("issue workspace mode must be tracked or local")
    repo_root = repo_root.resolve(strict=False)
    config_path = repo_root / ".xflow" / "xflow.json"
    gitignore_path = repo_root / ".gitignore"
    config_snapshot = _read_stable_file(repo_root, config_path, repo_root, MAX_METADATA_FILE_SIZE, ".xflow/xflow.json")
    gitignore_snapshot = _read_stable_file(repo_root, gitignore_path, repo_root, MAX_METADATA_FILE_SIZE, ".gitignore")
    project_config, _raw_config = _config_from_snapshot(repo_root, config_snapshot)
    issue_snapshot, active_approvals, oversized_files, absolute_path_files, credential_files, scan_errors = _scan_issue_workspace(repo_root)
    try:
        ignore_match = _effective_ignore_match(repo_root)
    except ValueError as exc:
        ignore_match = None
        scan_errors = tuple(sorted((*scan_errors, str(exc))))
    exact_ignore_lines: tuple[str, ...] = ()
    if gitignore_snapshot.exists:
        gitignore_text = (gitignore_snapshot.content or b"").decode("utf-8-sig", errors="strict")
        exact_ignore_lines = tuple(line for line in gitignore_text.splitlines() if line in EXACT_ISSUE_IGNORE_LINES)
    manual_actions = _manual_actions(repo_root, mode, ignore_match)
    effective_raw = ignore_match.raw if ignore_match else None
    return IssueWorkspaceMigrationReport(
        mode=mode,
        contract_root=project_config.contract_root,
        git_ignore_source=effective_raw,
        exact_ignore_lines=exact_ignore_lines,
        active_approvals=active_approvals,
        oversized_files=oversized_files,
        absolute_path_files=absolute_path_files,
        credential_files=credential_files,
        scan_errors=scan_errors,
        manual_actions=manual_actions,
        _snapshot=MigrationSnapshot(config_snapshot, gitignore_snapshot, issue_snapshot, effective_raw),
    )


def _ensure_directory(repo_root: Path, path: Path, created: list[Path]) -> None:
    path = require_safe_repo_path(repo_root, path, "migration directory")
    missing: list[Path] = []
    current = path
    while not current.exists() and current != repo_root:
        missing.append(current)
        current = current.parent
    require_safe_repo_path(repo_root, current, "migration directory parent")
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)
        require_safe_repo_path(repo_root, directory, "created migration directory")
    if not path.is_dir():
        raise ValueError(f"migration directory is not a directory: {path}")


def _stage_bytes(repo_root: Path, target: Path, content: bytes, prefix: str, created: list[Path]) -> Path:
    _ensure_directory(repo_root, target.parent, created)
    require_safe_repo_path(repo_root, target, "migration target")
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    require_safe_repo_path(repo_root, temporary, "staged migration file")
    return temporary


@dataclass
class StagedTarget:
    original: FileSnapshot
    staged: Path
    backup: Path | None


def _safe_replace(repo_root: Path, source: Path, target: Path, label: str) -> None:
    source = require_safe_repo_path(repo_root, source, f"{label} source")
    target = require_safe_repo_path(repo_root, target, f"{label} target")
    source_stat = os.lstat(source)
    if _is_reparse_point(source_stat) or not stat.S_ISREG(source_stat.st_mode):
        raise ValueError(f"{label} source must be a regular non-reparse file: {source}")
    require_safe_repo_path(repo_root, target.parent, f"{label} parent")
    os.replace(source, target)


def _snapshot_matches(expected: FileSnapshot, actual: FileSnapshot) -> bool:
    return expected == actual


def _current_target_snapshot(repo_root: Path, expected: FileSnapshot, label: str) -> FileSnapshot:
    return _read_stable_file(repo_root, expected.path, repo_root, MAX_METADATA_FILE_SIZE, label)


def _revalidate_snapshot(repo_root: Path, report: IssueWorkspaceMigrationReport) -> None:
    snapshot = report._snapshot
    if snapshot is None:
        raise ValueError("migration inspection snapshot is missing")
    current_config = _current_target_snapshot(repo_root, snapshot.config, ".xflow/xflow.json")
    current_gitignore = _current_target_snapshot(repo_root, snapshot.gitignore, ".gitignore")
    current_issues, _approvals, _oversized, _paths, _credentials, errors = _scan_issue_workspace(repo_root)
    if errors:
        raise ValueError("issue workspace changed during migration: " + "; ".join(errors))
    current_ignore = _effective_ignore_match(repo_root)
    current_ignore_raw = current_ignore.raw if current_ignore else None
    if (
        not _snapshot_matches(snapshot.config, current_config)
        or not _snapshot_matches(snapshot.gitignore, current_gitignore)
        or snapshot.issues != current_issues
        or snapshot.effective_ignore != current_ignore_raw
    ):
        raise ValueError("relevant configuration or issue workspace changed during migration")
    _config_from_snapshot(repo_root, current_config)


def _journal_path(repo_root: Path) -> Path:
    return repo_root / JOURNAL_RELATIVE_PATH


def _persist_journal(repo_root: Path, journal: Path, payload: dict[str, object], created: list[Path]) -> None:
    content = (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    staged = _stage_bytes(repo_root, journal, content, ".issue-workspace-journal.", created)
    _safe_replace(repo_root, staged, journal, "migration journal")


def _write_journal(
    repo_root: Path, plans: list[StagedTarget], created: list[Path]
) -> tuple[Path, dict[str, object]]:
    journal = _journal_path(repo_root)
    entries = []
    for plan in plans:
        entries.append(
            {
                "target": plan.original.path.relative_to(repo_root).as_posix(),
                "existed": plan.original.exists,
                "backup": str(plan.backup) if plan.backup else None,
                "staged": str(plan.staged),
                "state": "pending",
            }
        )
    payload: dict[str, object] = {"version": 1, "entries": entries}
    _persist_journal(repo_root, journal, payload, created)
    return journal, payload


def _update_journal_state(
    repo_root: Path,
    journal: Path,
    payload: dict[str, object],
    index: int,
    state_value: Literal["committing", "committed"],
    created: list[Path],
) -> None:
    entries = payload.get("entries")
    if not isinstance(entries, list) or index >= len(entries) or not isinstance(entries[index], dict):
        raise ValueError("invalid in-memory issue workspace migration journal")
    entries[index]["state"] = state_value
    _persist_journal(repo_root, journal, payload, created)


def _validate_journal_entry(repo_root: Path, entry: object) -> tuple[Path, bool, Path | None, Path, str]:
    if not isinstance(entry, dict) or set(entry) != {"target", "existed", "backup", "staged", "state"}:
        raise ValueError("invalid issue workspace migration journal entry")
    target_name = entry["target"]
    existed = entry["existed"]
    backup_name = entry["backup"]
    staged_name = entry["staged"]
    state_value = entry["state"]
    if (
        target_name not in {".gitignore", ".xflow/xflow.json"}
        or not isinstance(existed, bool)
        or state_value not in {"pending", "committing", "committed"}
    ):
        raise ValueError("invalid issue workspace migration journal target")
    target = require_safe_repo_path(repo_root, repo_root / target_name, "journal target")
    if not isinstance(staged_name, str):
        raise ValueError("invalid issue workspace migration journal staged file")
    staged = require_safe_repo_path(repo_root, Path(staged_name), "journal staged file")
    expected_stage_prefix = ".gitignore.migration." if target_name == ".gitignore" else ".xflow-config.migration."
    if staged.parent != target.parent or not staged.name.startswith(expected_stage_prefix):
        raise ValueError("invalid issue workspace migration journal staged location")
    backup: Path | None = None
    if backup_name is not None:
        if not isinstance(backup_name, str):
            raise ValueError("invalid issue workspace migration journal backup")
        backup = require_safe_repo_path(repo_root, Path(backup_name), "journal backup")
        expected_backup_prefix = ".gitignore.backup." if target_name == ".gitignore" else ".xflow-config.backup."
        if backup.parent != target.parent or not backup.name.startswith(expected_backup_prefix) or not backup.is_file():
            raise ValueError("invalid issue workspace migration journal backup location")
    if existed != (backup is not None):
        raise ValueError("invalid issue workspace migration journal backup state")
    return target, existed, backup, staged, str(state_value)


def _recover_pending_transaction(repo_root: Path) -> None:
    journal = _journal_path(repo_root)
    try:
        journal = require_safe_repo_path(repo_root, journal, "migration journal")
        journal_snapshot = _read_stable_file(
            repo_root,
            journal,
            repo_root,
            MAX_METADATA_FILE_SIZE,
            "migration journal",
        )
        if not journal_snapshot.exists:
            return
        payload = json.loads((journal_snapshot.content or b"").decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or set(payload) != {"version", "entries"} or payload["version"] != 1:
            raise ValueError("invalid issue workspace migration journal")
        entries = payload["entries"]
        if not isinstance(entries, list):
            raise ValueError("invalid issue workspace migration journal entries")
        validated = [_validate_journal_entry(repo_root, entry) for entry in entries]
        for target, existed, backup, staged, state_value in reversed(validated):
            if state_value != "pending":
                if existed:
                    assert backup is not None
                    _safe_replace(repo_root, backup, target, "journal recovery")
                elif target.exists():
                    require_safe_repo_path(repo_root, target, "journal-created target")
                    target.unlink()
            if backup is not None:
                backup.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
        journal.unlink()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"pending issue workspace migration requires manual recovery at {journal}: {exc}") from exc


def _rollback_transaction(repo_root: Path, committed: list[StagedTarget], journal: Path) -> None:
    errors: list[str] = []
    for plan in reversed(committed):
        try:
            target = require_safe_repo_path(repo_root, plan.original.path, "rollback target")
            if plan.original.exists:
                if plan.backup is None or not plan.backup.exists():
                    raise ValueError(f"missing rollback backup for {target}")
                _safe_replace(repo_root, plan.backup, target, "migration rollback")
            elif target.exists():
                target.unlink()
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError(f"rollback incomplete; manual recovery required at {journal}: {'; '.join(errors)}")
    journal.unlink(missing_ok=True)


def _cleanup_transaction(plans: list[StagedTarget], journal: Path | None, created: list[Path], *, keep_backups: bool = False) -> None:
    for plan in plans:
        plan.staged.unlink(missing_ok=True)
        if not keep_backups and plan.backup is not None:
            plan.backup.unlink(missing_ok=True)
    if journal is not None and not keep_backups:
        journal.unlink(missing_ok=True)
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            pass


def _build_plans(
    repo_root: Path,
    report: IssueWorkspaceMigrationReport,
    created: list[Path],
) -> list[StagedTarget]:
    snapshot = report._snapshot
    if snapshot is None:
        raise ValueError("migration inspection snapshot is missing")
    _project_config, raw_config = _config_from_snapshot(repo_root, snapshot.config)
    raw_config["issueWorkspace"] = {"mode": report.mode}
    raw_config["contracts"] = {"root": report.contract_root.as_posix()}
    config_content = (json.dumps(raw_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    plans: list[StagedTarget] = []

    if report.mode == "tracked" and report.exact_ignore_lines:
        ignore_text = (snapshot.gitignore.content or b"").decode("utf-8-sig", errors="strict")
        remaining = [line for line in ignore_text.splitlines() if line not in EXACT_ISSUE_IGNORE_LINES]
        ignore_content = ("\n".join(remaining) + ("\n" if remaining else "")).encode("utf-8")
        ignore_stage = _stage_bytes(repo_root, snapshot.gitignore.path, ignore_content, ".gitignore.migration.", created)
        ignore_backup = (
            _stage_bytes(repo_root, snapshot.gitignore.path, snapshot.gitignore.content or b"", ".gitignore.backup.", created)
            if snapshot.gitignore.exists
            else None
        )
        plans.append(StagedTarget(snapshot.gitignore, ignore_stage, ignore_backup))

    config_stage = _stage_bytes(repo_root, snapshot.config.path, config_content, ".xflow-config.migration.", created)
    config_backup = (
        _stage_bytes(repo_root, snapshot.config.path, snapshot.config.content or b"", ".xflow-config.backup.", created)
        if snapshot.config.exists
        else None
    )
    plans.append(StagedTarget(snapshot.config, config_stage, config_backup))
    return plans


def apply_issue_workspace_migration(repo_root: Path, mode: Literal["tracked", "local"]) -> IssueWorkspaceMigrationReport:
    repo_root = repo_root.resolve(strict=False)
    _recover_pending_transaction(repo_root)
    report = inspect_issue_workspace_migration(repo_root, mode)
    if report.blockers:
        raise ValueError("cannot apply issue workspace migration: " + "; ".join(report.blockers))

    created: list[Path] = []
    plans: list[StagedTarget] = []
    committed: list[StagedTarget] = []
    journal: Path | None = None
    journal_payload: dict[str, object] | None = None
    try:
        plans = _build_plans(repo_root, report, created)
        journal, journal_payload = _write_journal(repo_root, plans, created)
        _revalidate_snapshot(repo_root, report)
        for index, plan in enumerate(plans):
            assert journal_payload is not None
            _update_journal_state(repo_root, journal, journal_payload, index, "committing", created)
            current = _current_target_snapshot(repo_root, plan.original, "migration target")
            if not _snapshot_matches(plan.original, current):
                raise ValueError(f"migration target changed during migration: {plan.original.path}")
            _safe_replace(repo_root, plan.staged, plan.original.path, "migration commit")
            committed.append(plan)
            _update_journal_state(repo_root, journal, journal_payload, index, "committed", created)
            if plan.original.path == repo_root / ".gitignore":
                remaining_match = _effective_ignore_match(repo_root)
                if remaining_match is not None:
                    raise ValueError(
                        ".xflow/issues is still effectively ignored; manual action required: "
                        f"{remaining_match.raw}"
                    )
    except Exception as exc:
        if journal is not None:
            try:
                _rollback_transaction(repo_root, committed, journal)
            except ValueError as rollback_exc:
                _cleanup_transaction(plans, journal, created, keep_backups=True)
                raise rollback_exc from exc
            _cleanup_transaction(plans, journal, created)
            raise ValueError(f"migration failed and rolled back: {exc}") from exc
        _cleanup_transaction(plans, journal, created)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"cannot stage issue workspace migration: {exc}") from exc
    _cleanup_transaction(plans, journal, created)
    return report


def inspect(repo_root: Path) -> MigrationReport:
    repo_root = repo_root.resolve()
    legacy = (repo_root / "_ops" / "devctl").exists() or (repo_root / "_ops" / "workflow").exists()
    v2 = (repo_root / ".xflow" / "ops" / "devctl").exists() or (repo_root / ".xflow" / "ops" / "workflow").exists()
    messages: list[str] = []
    if legacy:
        messages.append("legacy _ops layout detected")
    if v2:
        messages.append("v2 .xflow/ops layout detected")
    gitmodules = repo_root / ".gitmodules"
    if gitmodules.is_file():
        text = gitmodules.read_text(encoding="utf-8")
        if "https://github.com/Linkk2000/xflow-" in text:
            messages.append("https submodule URL detected")
        if ".xflow/ops/devctl" not in text or ".xflow/ops/workflow" not in text:
            messages.append("v2 submodule paths missing")
        if "ignore = untracked" not in text:
            messages.append("submodule ignore = untracked missing")
    else:
        messages.append(".gitmodules missing")
    return MigrationReport(legacy, v2, tuple(messages))


def wrapper_files() -> dict[str, str]:
    bash = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$ROOT/.xflow/ops/devctl"

if [[ ! -d "$TOOL_ROOT/xflow" ]]; then
  echo "[ERROR] Missing XFlow devctl Python core at .xflow/ops/devctl/xflow" >&2
  exit 1
fi

export DEVCTL_REPO_ROOT="$ROOT"
export DEVCTL_TOOL_ROOT="$TOOL_ROOT"
export DEVCTL_OPS_ROOT="$TOOL_ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$TOOL_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python -m xflow "$@"
"""
    ps1 = """$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolRoot = Join-Path $Root ".xflow\\ops\\devctl"
$Package = Join-Path $ToolRoot "xflow"

if (-not (Test-Path -LiteralPath $Package)) {
    Write-Error "[ERROR] Missing XFlow devctl Python core at .xflow\\ops\\devctl\\xflow"
    exit 1
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Error "[ERROR] Python 3.10+ is required for devctl Python core."
    exit 1
}

$env:DEVCTL_REPO_ROOT = $Root
$env:DEVCTL_TOOL_ROOT = $ToolRoot
$env:DEVCTL_OPS_ROOT = $ToolRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$ToolRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $ToolRoot
}

python -m xflow @args
exit $LASTEXITCODE
"""
    return {"devctl": bash, "devctl.ps1": ps1}


def write_wrappers(repo_root: Path) -> list[Path]:
    written: list[Path] = []
    for name, content in wrapper_files().items():
        path = repo_root / name
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written
