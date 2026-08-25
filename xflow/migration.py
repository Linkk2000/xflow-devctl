from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from .approval import CREDENTIAL_PATTERNS
from .commit_message import (
    LOCAL_FILE_URI_RE,
    POSIX_ABSOLUTE_PATH_RE,
    REMOTE_URL_RE,
    UNC_OR_DEVICE_PATH_RE,
    WINDOWS_ABSOLUTE_PATH_RE,
)
from .io import write_text_lf
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
    r"(?im)(?:^[ \t]*(?:export[ \t]+)?|(?<=[{,])[ \t]*)"
    r"(?:\"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|private[_-]?key|"
    r"token|secret|password|credential)\"|"
    r"'(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|private[_-]?key|"
    r"token|secret|password|credential)'|"
    r"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|private[_-]?key|"
    r"token|secret|password|credential))[ \t]*[:=]"
)
JOURNAL_RELATIVE_PATH = Path(".xflow/local/issue-workspace-migration-journal.json")
TRANSACTION_ID_RE = re.compile(r"[0-9a-f]{32}")


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


def _tree_directory_identity(path_stat: os.stat_result) -> FileIdentity:
    identity = _identity(path_stat)
    return identity[0], identity[1], identity[2], 0, 0, 0


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
    tree.append((".", "dir", _tree_directory_identity(root_stat)))

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
                entry_stat = os.lstat(path)
            except OSError as exc:
                errors.append(f"cannot inspect issue workspace entry {path}: {exc}")
                continue
            if _is_reparse_point(entry_stat):
                errors.append(f"issue workspace entry is a symlink, junction, or reparse point: {path}")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                tree.append((relative, "dir", _tree_directory_identity(entry_stat)))
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


@contextmanager
def _repository_migration_lock(repo_root: Path) -> Iterator[None]:
    lock_key = hashlib.sha256(os.path.normcase(str(repo_root)).encode("utf-8")).hexdigest()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = (wintypes.HANDLE,)
        release_mutex.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = create_mutex(None, False, f"Local\\XFlowIssueWorkspaceMigration-{lock_key}")
        if not handle:
            raise OSError(ctypes.get_last_error(), "cannot create issue workspace migration mutex")
        wait_result = wait_for_single_object(handle, 0)
        if wait_result not in {0, 0x80}:
            close_handle(handle)
            if wait_result == 0x102:
                raise ValueError("another issue workspace migration holds the repository lock")
            raise OSError(ctypes.get_last_error(), "cannot acquire issue workspace migration mutex")
        try:
            yield
        finally:
            release_mutex(handle)
            close_handle(handle)
        return

    import fcntl

    lock_path = Path(tempfile.gettempdir()) / f"xflow-issue-workspace-{lock_key}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another issue workspace migration holds the repository lock") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


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


def _reserve_backup_path(repo_root: Path, target: Path, prefix: str, created: list[Path]) -> Path:
    _ensure_directory(repo_root, target.parent, created)
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = require_safe_repo_path(repo_root, Path(temporary_name), "migration backup reservation")
    temporary.unlink()
    return temporary


@dataclass
class StagedTarget:
    original: FileSnapshot
    staged: Path
    staged_snapshot: FileSnapshot
    backup: Path | None


@dataclass(frozen=True)
class TransactionOwner:
    transaction_id: str
    path: Path
    snapshot: FileSnapshot
    key: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class JournalTarget:
    target: Path
    original_exists: bool
    original_identity: FileIdentity | None
    original_digest: str | None
    staged: Path
    staged_identity: FileIdentity
    staged_digest: str
    backup: Path | None
    state: str


def _rename_no_overwrite(source: Path, target: Path) -> None:
    try:
        os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"refusing to overwrite existing transaction path: {target}")
    if os.name == "nt":
        os.rename(source, target)
        return
    os.link(source, target, follow_symlinks=False)
    os.unlink(source)


def _durable_rename_no_overwrite(source: Path, target: Path) -> None:
    if os.name != "nt":
        _rename_no_overwrite(source, target)
        try:
            descriptor = os.open(target.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    import ctypes
    from ctypes import wintypes

    try:
        os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"refusing to overwrite existing finalized journal: {target}")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if not move_file(str(source), str(target), 0x8):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot durably finalize issue workspace migration journal", str(source), str(target))


def _file_state_matches(
    actual: FileSnapshot,
    expected_exists: bool,
    expected_identity: FileIdentity | None,
    expected_digest: str | None,
) -> bool:
    if actual.exists != expected_exists:
        return False
    if not expected_exists:
        return True
    if actual.identity is None or expected_identity is None:
        return False
    return actual.digest == expected_digest and _same_file_object(actual.identity, expected_identity)


def _safe_replace(
    repo_root: Path,
    source: Path,
    target: Path,
    label: str,
    *,
    expected: FileSnapshot | None = None,
    backup: Path | None = None,
) -> FileSnapshot:
    source = require_safe_repo_path(repo_root, source, f"{label} source")
    target = require_safe_repo_path(repo_root, target, f"{label} target")
    source_snapshot = _read_stable_file(repo_root, source, repo_root, MAX_METADATA_FILE_SIZE, f"{label} source")
    if not source_snapshot.exists:
        raise ValueError(f"{label} source is missing: {source}")
    require_safe_repo_path(repo_root, target.parent, f"{label} parent")
    if expected is None:
        os.replace(source, target)
        return _read_stable_file(repo_root, target, repo_root, MAX_METADATA_FILE_SIZE, f"{label} result")

    if target != expected.path:
        raise ValueError(f"{label} target does not match its expected snapshot: {target}")
    current = _current_target_snapshot(repo_root, expected, "migration target")
    if not _snapshot_matches(expected, current):
        raise ValueError(f"migration target changed during migration: {target}")

    if expected.exists:
        if backup is None:
            raise ValueError(f"migration backup is missing for existing target: {target}")
        backup = require_safe_repo_path(repo_root, backup, "migration backup")
        _rename_no_overwrite(target, backup)
        captured = _read_stable_file(repo_root, backup, repo_root, MAX_METADATA_FILE_SIZE, "captured migration target")
        if not _file_state_matches(captured, True, expected.identity, expected.digest):
            if not target.exists():
                _rename_no_overwrite(backup, target)
            raise ValueError(f"migration target changed during migration: {target}")
    elif backup is not None:
        raise ValueError(f"migration backup is unexpected for absent target: {target}")

    try:
        _rename_no_overwrite(source, target)
    except Exception:
        if expected.exists and backup is not None and backup.exists() and not target.exists():
            _rename_no_overwrite(backup, target)
        raise
    result = _read_stable_file(repo_root, target, repo_root, MAX_METADATA_FILE_SIZE, "migration committed target")
    if not _file_state_matches(result, True, source_snapshot.identity, source_snapshot.digest):
        raise ValueError(f"migration target changed during migration: {target}")
    return result


def _snapshot_matches(expected: FileSnapshot, actual: FileSnapshot) -> bool:
    return expected == actual


def _current_target_snapshot(repo_root: Path, expected: FileSnapshot, label: str) -> FileSnapshot:
    return _read_stable_file(repo_root, expected.path, repo_root, MAX_METADATA_FILE_SIZE, label)


_EXPECTED_IGNORE_UNSET = object()


def _revalidate_snapshot(
    repo_root: Path,
    report: IssueWorkspaceMigrationReport,
    expected_targets: dict[Path, FileSnapshot] | None = None,
    expected_effective_ignore: str | None | object = _EXPECTED_IGNORE_UNSET,
) -> None:
    snapshot = report._snapshot
    if snapshot is None:
        raise ValueError("migration inspection snapshot is missing")
    expected_targets = expected_targets or {
        snapshot.config.path: snapshot.config,
        snapshot.gitignore.path: snapshot.gitignore,
    }
    expected_config = expected_targets[snapshot.config.path]
    expected_gitignore = expected_targets[snapshot.gitignore.path]
    current_config = _current_target_snapshot(repo_root, expected_config, ".xflow/xflow.json")
    current_gitignore = _current_target_snapshot(repo_root, expected_gitignore, ".gitignore")
    current_issues, _approvals, _oversized, _paths, _credentials, errors = _scan_issue_workspace(repo_root)
    if errors:
        raise ValueError("issue workspace changed during migration: " + "; ".join(errors))
    current_ignore = _effective_ignore_match(repo_root)
    current_ignore_raw = current_ignore.raw if current_ignore else None
    if expected_effective_ignore is _EXPECTED_IGNORE_UNSET:
        expected_effective_ignore = snapshot.effective_ignore
    if (
        not _snapshot_matches(expected_config, current_config)
        or not _snapshot_matches(expected_gitignore, current_gitignore)
        or snapshot.issues != current_issues
        or expected_effective_ignore != current_ignore_raw
    ):
        raise ValueError("relevant configuration or issue workspace changed during migration")
    _config_from_snapshot(repo_root, current_config)


def _journal_path(repo_root: Path) -> Path:
    return repo_root / JOURNAL_RELATIVE_PATH


def _identity_record(identity: FileIdentity | None) -> list[int] | None:
    return list(identity) if identity is not None else None


def _snapshot_record(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "exists": snapshot.exists,
        "identity": _identity_record(snapshot.identity),
        "digest": snapshot.digest,
    }


def _artifact_record(repo_root: Path, snapshot: FileSnapshot) -> dict[str, object]:
    if not snapshot.exists or snapshot.identity is None or snapshot.digest is None:
        raise ValueError(f"transaction artifact is missing: {snapshot.path}")
    return {
        "path": snapshot.path.relative_to(repo_root).as_posix(),
        "identity": _identity_record(snapshot.identity),
        "digest": snapshot.digest,
    }


def _journal_authentication(payload: dict[str, object], key: bytes) -> str:
    body = {name: value for name, value in payload.items() if name != "authentication"}
    canonical = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _persist_journal(
    repo_root: Path,
    journal: Path,
    payload: dict[str, object],
    created: list[Path],
    owner: TransactionOwner,
) -> None:
    payload["authentication"] = _journal_authentication(payload, owner.key)
    content = (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    staged = _stage_bytes(
        repo_root,
        journal,
        content,
        f".issue-workspace-journal.{owner.transaction_id}.",
        created,
    )
    _safe_replace(repo_root, staged, journal, "migration journal")
    saved = _read_stable_file(repo_root, journal, repo_root, MAX_METADATA_FILE_SIZE, "migration journal")
    if json.loads((saved.content or b"").decode("utf-8", errors="strict")) != payload:
        raise ValueError("migration journal changed while persisting")


def _create_transaction_owner(repo_root: Path, transaction_id: str, created: list[Path]) -> TransactionOwner:
    owner_path = repo_root / ".xflow" / "local" / f"issue-workspace-migration.{transaction_id}.owner"
    _ensure_directory(repo_root, owner_path.parent, created)
    owner_path = require_safe_repo_path(repo_root, owner_path, "migration transaction owner")
    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(owner_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        owner_path.unlink(missing_ok=True)
        raise
    snapshot = _read_stable_file(repo_root, owner_path, repo_root, 1024, "migration transaction owner")
    return TransactionOwner(transaction_id, owner_path, snapshot, key)


def _write_journal(
    repo_root: Path,
    plans: list[StagedTarget],
    created: list[Path],
    owner: TransactionOwner,
) -> tuple[Path, dict[str, object]]:
    journal = _journal_path(repo_root)
    existing = _read_stable_file(repo_root, journal, repo_root, MAX_METADATA_FILE_SIZE, "migration journal")
    if existing.exists:
        raise ValueError(f"pending issue workspace migration journal already exists: {journal}")
    entries = []
    for plan in plans:
        original = _snapshot_record(plan.original)
        staged = _artifact_record(repo_root, plan.staged_snapshot)
        backup = None
        if plan.backup is not None:
            backup = {
                "path": plan.backup.relative_to(repo_root).as_posix(),
                "identity": _identity_record(plan.original.identity),
                "digest": plan.original.digest,
            }
        entries.append(
            {
                "target": plan.original.path.relative_to(repo_root).as_posix(),
                "original": original,
                "backup": backup,
                "staged": staged,
                "state": "pending",
            }
        )
    payload: dict[str, object] = {
        "version": 2,
        "transactionId": owner.transaction_id,
        "repositoryRoot": os.path.normcase(str(repo_root)),
        "owner": _artifact_record(repo_root, owner.snapshot),
        "entries": entries,
        "authentication": "",
    }
    _persist_journal(repo_root, journal, payload, created, owner)
    return journal, payload


def _update_journal_state(
    repo_root: Path,
    journal: Path,
    payload: dict[str, object],
    index: int,
    state_value: Literal["committing", "committed"],
    created: list[Path],
    owner: TransactionOwner,
) -> None:
    current = _read_stable_file(repo_root, journal, repo_root, MAX_METADATA_FILE_SIZE, "migration journal")
    if not current.exists:
        raise ValueError("live issue workspace migration journal disappeared")
    try:
        current_payload = json.loads((current.content or b"").decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("live issue workspace migration journal is invalid") from exc
    if current_payload != payload or not hmac.compare_digest(
        str(payload.get("authentication", "")), _journal_authentication(payload, owner.key)
    ):
        raise ValueError("live issue workspace migration journal changed")
    entries = payload.get("entries")
    if not isinstance(entries, list) or index >= len(entries) or not isinstance(entries[index], dict):
        raise ValueError("invalid in-memory issue workspace migration journal")
    entries[index]["state"] = state_value
    _persist_journal(repo_root, journal, payload, created, owner)


def _parse_identity(value: object, label: str) -> FileIdentity:
    if not isinstance(value, list) or len(value) != 6 or any(type(part) is not int for part in value):
        raise ValueError(f"invalid {label} identity")
    return tuple(value)  # type: ignore[return-value]


def _parse_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"invalid {label} digest")
    return value


def _journal_artifact_path(repo_root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label} path")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or relative.root or ".." in relative.parts:
        raise ValueError(f"invalid {label} path")
    return require_safe_repo_path(repo_root, repo_root / relative, label)


def _validate_journal_entry(repo_root: Path, entry: object, transaction_id: str) -> JournalTarget:
    if not isinstance(entry, dict) or set(entry) != {"target", "original", "backup", "staged", "state"}:
        raise ValueError("invalid issue workspace migration journal entry")
    target_name = entry["target"]
    state_value = entry["state"]
    if target_name not in {".gitignore", ".xflow/xflow.json"} or state_value not in {
        "pending",
        "committing",
        "committed",
    }:
        raise ValueError("invalid issue workspace migration journal target")
    target = require_safe_repo_path(repo_root, repo_root / target_name, "journal target")

    original = entry["original"]
    if not isinstance(original, dict) or set(original) != {"exists", "identity", "digest"}:
        raise ValueError("invalid issue workspace migration journal original state")
    original_exists = original["exists"]
    if not isinstance(original_exists, bool):
        raise ValueError("invalid issue workspace migration journal original state")
    if original_exists:
        original_identity = _parse_identity(original["identity"], "journal original")
        original_digest = _parse_digest(original["digest"], "journal original")
    else:
        if original["identity"] is not None or original["digest"] is not None:
            raise ValueError("invalid absent journal original state")
        original_identity = None
        original_digest = None

    staged_record = entry["staged"]
    if not isinstance(staged_record, dict) or set(staged_record) != {"path", "identity", "digest"}:
        raise ValueError("invalid issue workspace migration journal staged file")
    staged = _journal_artifact_path(repo_root, staged_record["path"], "journal staged file")
    staged_identity = _parse_identity(staged_record["identity"], "journal staged")
    staged_digest = _parse_digest(staged_record["digest"], "journal staged")
    expected_stage_prefix = f"{target.name}.issue-migration.{transaction_id}.stage."
    if (
        staged.parent != target.parent
        or not staged.name.startswith(expected_stage_prefix)
        or not staged.name.endswith(".tmp")
    ):
        raise ValueError("invalid issue workspace migration journal staged location")

    backup: Path | None = None
    backup_record = entry["backup"]
    if backup_record is not None:
        if not isinstance(backup_record, dict) or set(backup_record) != {"path", "identity", "digest"}:
            raise ValueError("invalid issue workspace migration journal backup")
        backup = _journal_artifact_path(repo_root, backup_record["path"], "journal backup")
        backup_identity = _parse_identity(backup_record["identity"], "journal backup")
        backup_digest = _parse_digest(backup_record["digest"], "journal backup")
        expected_backup_prefix = f"{target.name}.issue-migration.{transaction_id}.backup."
        if (
            backup.parent != target.parent
            or not backup.name.startswith(expected_backup_prefix)
            or not backup.name.endswith(".tmp")
        ):
            raise ValueError("invalid issue workspace migration journal backup location")
        if backup_identity != original_identity or backup_digest != original_digest:
            raise ValueError("journal backup does not authenticate the original target")
    if original_exists != (backup is not None):
        raise ValueError("invalid issue workspace migration journal backup state")
    return JournalTarget(
        target,
        original_exists,
        original_identity,
        original_digest,
        staged,
        staged_identity,
        staged_digest,
        backup,
        str(state_value),
    )


def _load_pending_transaction(
    repo_root: Path,
) -> tuple[Path, dict[str, object], TransactionOwner, list[JournalTarget]] | None:
    journal = require_safe_repo_path(repo_root, _journal_path(repo_root), "migration journal")
    journal_snapshot = _read_stable_file(repo_root, journal, repo_root, MAX_METADATA_FILE_SIZE, "migration journal")
    if not journal_snapshot.exists:
        return None
    payload = json.loads((journal_snapshot.content or b"").decode("utf-8", errors="strict"))
    expected_keys = {"version", "transactionId", "repositoryRoot", "owner", "entries", "authentication"}
    if not isinstance(payload, dict) or set(payload) != expected_keys or payload["version"] != 2:
        raise ValueError("invalid issue workspace migration journal")
    transaction_id = payload["transactionId"]
    if not isinstance(transaction_id, str) or TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        raise ValueError("invalid issue workspace migration transaction id")
    if payload["repositoryRoot"] != os.path.normcase(str(repo_root)):
        raise ValueError("issue workspace migration journal belongs to another repository")

    owner_record = payload["owner"]
    if not isinstance(owner_record, dict) or set(owner_record) != {"path", "identity", "digest"}:
        raise ValueError("invalid issue workspace migration transaction owner")
    owner_path = _journal_artifact_path(repo_root, owner_record["path"], "journal transaction owner")
    expected_owner = repo_root / ".xflow" / "local" / f"issue-workspace-migration.{transaction_id}.owner"
    if owner_path != expected_owner:
        raise ValueError("invalid issue workspace migration transaction owner location")
    owner_identity = _parse_identity(owner_record["identity"], "journal transaction owner")
    owner_digest = _parse_digest(owner_record["digest"], "journal transaction owner")
    owner_snapshot = _read_stable_file(repo_root, owner_path, repo_root, 1024, "journal transaction owner")
    if not _file_state_matches(owner_snapshot, True, owner_identity, owner_digest):
        raise ValueError("issue workspace migration transaction owner changed")
    owner = TransactionOwner(transaction_id, owner_path, owner_snapshot, owner_snapshot.content or b"")
    authentication = payload["authentication"]
    if not isinstance(authentication, str) or not hmac.compare_digest(
        authentication, _journal_authentication(payload, owner.key)
    ):
        raise ValueError("issue workspace migration journal authentication failed")

    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("invalid issue workspace migration journal entries")
    validated = [_validate_journal_entry(repo_root, entry, transaction_id) for entry in entries]
    if len({entry.target for entry in validated}) != len(validated):
        raise ValueError("duplicate issue workspace migration journal target")
    artifact_paths = [entry.staged for entry in validated]
    artifact_paths.extend(entry.backup for entry in validated if entry.backup is not None)
    if len(set(artifact_paths)) != len(artifact_paths):
        raise ValueError("duplicate issue workspace migration journal artifact")
    return journal, payload, owner, validated


def _read_journal_artifact(
    repo_root: Path,
    path: Path,
    identity: FileIdentity,
    digest: str,
    label: str,
) -> FileSnapshot:
    snapshot = _read_stable_file(repo_root, path, repo_root, MAX_METADATA_FILE_SIZE, label)
    if snapshot.exists and not _file_state_matches(snapshot, True, identity, digest):
        raise ValueError(f"{label} identity or digest changed: {path}")
    return snapshot


def _classify_journal_target(repo_root: Path, entry: JournalTarget) -> tuple[str, FileSnapshot, FileSnapshot]:
    current = _read_stable_file(repo_root, entry.target, repo_root, MAX_METADATA_FILE_SIZE, "journal current target")
    staged = _read_journal_artifact(
        repo_root,
        entry.staged,
        entry.staged_identity,
        entry.staged_digest,
        "journal staged artifact",
    )
    if entry.backup is not None:
        assert entry.original_identity is not None and entry.original_digest is not None
        backup = _read_journal_artifact(
            repo_root,
            entry.backup,
            entry.original_identity,
            entry.original_digest,
            "journal backup artifact",
        )
    else:
        backup = FileSnapshot(entry.target, False, None, None, None)

    if _file_state_matches(current, entry.original_exists, entry.original_identity, entry.original_digest):
        kind = "original"
    elif _file_state_matches(current, True, entry.staged_identity, entry.staged_digest):
        kind = "staged"
    elif not current.exists:
        kind = "absent"
    else:
        kind = "unexpected"

    if entry.original_exists:
        allowed = {
            ("original", True, False),
            ("absent", True, True),
            ("staged", False, True),
        }
    else:
        allowed = {
            ("absent", True, False),
            ("staged", False, False),
        }
    if entry.state == "pending":
        allowed = {combination for combination in allowed if combination[0] in {"original", "absent"}}
    if (kind, staged.exists, backup.exists) not in allowed:
        raise ValueError(
            f"unexpected current target or transaction artifacts for {entry.target}; refusing to overwrite project edits"
        )
    return kind, staged, backup


def _finalize_journal(
    repo_root: Path,
    journal: Path,
    transaction_id: str,
    *,
    validate_targets: bool = True,
) -> Path:
    journal = require_safe_repo_path(repo_root, journal, "live migration journal")
    loaded = _load_pending_transaction(repo_root)
    if loaded is None or loaded[0] != journal or loaded[2].transaction_id != transaction_id:
        raise ValueError("live issue workspace migration journal changed before finalization")
    if validate_targets:
        for entry in loaded[3]:
            _classify_journal_target(repo_root, entry)
    finalized = require_safe_repo_path(
        repo_root,
        journal.parent / f"issue-workspace-migration.{transaction_id}.finalized.json",
        "finalized migration journal",
    )
    _durable_rename_no_overwrite(journal, finalized)
    return finalized


def _recover_pending_transaction(repo_root: Path) -> None:
    journal = _journal_path(repo_root)
    try:
        loaded = _load_pending_transaction(repo_root)
        if loaded is None:
            return
        journal, _payload, owner, entries = loaded
        classified = [(entry, *_classify_journal_target(repo_root, entry)) for entry in entries]
        for entry, kind, _staged, _backup in reversed(classified):
            current_kind, _current_staged, current_backup = _classify_journal_target(repo_root, entry)
            if current_kind != kind:
                raise ValueError(f"journal target changed at recovery boundary: {entry.target}")
            if kind == "staged":
                _rename_no_overwrite(entry.target, entry.staged)
                displaced = _read_journal_artifact(
                    repo_root,
                    entry.staged,
                    entry.staged_identity,
                    entry.staged_digest,
                    "recovery displaced migration output",
                )
                if not displaced.exists:
                    raise ValueError(f"recovery lost displaced migration output: {entry.target}")
                if entry.original_exists:
                    assert entry.backup is not None and current_backup.exists
                    _rename_no_overwrite(entry.backup, entry.target)
            elif kind == "absent" and entry.original_exists:
                assert entry.backup is not None and current_backup.exists
                _rename_no_overwrite(entry.backup, entry.target)
            restored = _read_stable_file(
                repo_root,
                entry.target,
                repo_root,
                MAX_METADATA_FILE_SIZE,
                "recovered migration target",
            )
            if not _file_state_matches(
                restored,
                entry.original_exists,
                entry.original_identity,
                entry.original_digest,
            ):
                raise ValueError(f"journal recovery could not restore target: {entry.target}")
        finalized = _finalize_journal(repo_root, journal, owner.transaction_id)
        plans = [
            StagedTarget(
                FileSnapshot(
                    entry.target,
                    entry.original_exists,
                    entry.original_identity,
                    entry.original_digest,
                    None,
                ),
                entry.staged,
                FileSnapshot(entry.staged, True, entry.staged_identity, entry.staged_digest, None),
                entry.backup,
            )
            for entry in entries
        ]
        _cleanup_transaction(plans, finalized, [], owner=owner.path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"pending issue workspace migration requires manual recovery at {journal}: {exc}") from exc


def _rollback_transaction(
    repo_root: Path,
    plans: list[StagedTarget],
    journal: Path,
    transaction_id: str,
) -> Path:
    errors: list[str] = []
    for plan in reversed(plans):
        try:
            target = plan.original.path
            current = _read_stable_file(repo_root, target, repo_root, MAX_METADATA_FILE_SIZE, "rollback target")
            staged = _read_stable_file(
                repo_root,
                plan.staged,
                repo_root,
                MAX_METADATA_FILE_SIZE,
                "rollback staged artifact",
            )
            backup = (
                _read_stable_file(repo_root, plan.backup, repo_root, MAX_METADATA_FILE_SIZE, "rollback backup")
                if plan.backup is not None
                else FileSnapshot(target, False, None, None, None)
            )
            if staged.exists and not _file_state_matches(
                staged,
                True,
                plan.staged_snapshot.identity,
                plan.staged_snapshot.digest,
            ):
                raise ValueError(f"rollback staged artifact changed: {plan.staged}")
            if backup.exists and not _file_state_matches(
                backup,
                True,
                plan.original.identity,
                plan.original.digest,
            ):
                raise ValueError(f"rollback backup changed: {plan.backup}")
            current_is_original = _file_state_matches(
                current,
                plan.original.exists,
                plan.original.identity,
                plan.original.digest,
            )
            current_is_staged = _file_state_matches(
                current,
                True,
                plan.staged_snapshot.identity,
                plan.staged_snapshot.digest,
            )
            if current_is_staged:
                if staged.exists:
                    raise ValueError(f"rollback staged path already exists: {plan.staged}")
                _rename_no_overwrite(target, plan.staged)
                displaced = _read_stable_file(
                    repo_root,
                    plan.staged,
                    repo_root,
                    MAX_METADATA_FILE_SIZE,
                    "rollback displaced migration output",
                )
                if not _file_state_matches(
                    displaced,
                    True,
                    plan.staged_snapshot.identity,
                    plan.staged_snapshot.digest,
                ):
                    if not target.exists():
                        _rename_no_overwrite(plan.staged, target)
                    raise ValueError(f"rollback target changed at commit boundary: {target}")
                if plan.original.exists:
                    if plan.backup is None or not backup.exists:
                        raise ValueError(f"missing rollback backup for {target}")
                    _rename_no_overwrite(plan.backup, target)
            elif not current.exists and plan.original.exists and backup.exists:
                assert plan.backup is not None
                _rename_no_overwrite(plan.backup, target)
            elif current_is_original:
                pass
            elif staged.exists and not backup.exists:
                # The commit protocol restored a last-window project edit before raising.
                pass
            else:
                raise ValueError(f"rollback refuses to overwrite an unexpected current target: {target}")
            restored = _read_stable_file(repo_root, target, repo_root, MAX_METADATA_FILE_SIZE, "rollback result")
            if staged.exists and not backup.exists and not current_is_original and not current_is_staged:
                continue
            if plan.original.exists:
                if not _file_state_matches(restored, True, plan.original.identity, plan.original.digest):
                    raise ValueError(f"rollback could not restore target: {target}")
            elif restored.exists:
                raise ValueError(f"rollback could not remove migration-created target: {target}")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError(f"rollback incomplete; manual recovery required at {journal}: {'; '.join(errors)}")
    return _finalize_journal(repo_root, journal, transaction_id, validate_targets=False)


def _cleanup_transaction(
    plans: list[StagedTarget],
    journal: Path | None,
    created: list[Path],
    *,
    keep_backups: bool = False,
    owner: Path | None = None,
) -> None:
    for plan in plans:
        plan.staged.unlink(missing_ok=True)
        if not keep_backups and plan.backup is not None:
            plan.backup.unlink(missing_ok=True)
    if not keep_backups and owner is not None:
        owner.unlink(missing_ok=True)
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
    transaction_id: str,
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
        ignore_stage = _stage_bytes(
            repo_root,
            snapshot.gitignore.path,
            ignore_content,
            f"{snapshot.gitignore.path.name}.issue-migration.{transaction_id}.stage.",
            created,
        )
        ignore_stage_snapshot = _read_stable_file(
            repo_root, ignore_stage, repo_root, MAX_METADATA_FILE_SIZE, "staged .gitignore"
        )
        ignore_backup = (
            _reserve_backup_path(
                repo_root,
                snapshot.gitignore.path,
                f"{snapshot.gitignore.path.name}.issue-migration.{transaction_id}.backup.",
                created,
            )
            if snapshot.gitignore.exists
            else None
        )
        plans.append(StagedTarget(snapshot.gitignore, ignore_stage, ignore_stage_snapshot, ignore_backup))

    config_stage = _stage_bytes(
        repo_root,
        snapshot.config.path,
        config_content,
        f"{snapshot.config.path.name}.issue-migration.{transaction_id}.stage.",
        created,
    )
    config_stage_snapshot = _read_stable_file(
        repo_root, config_stage, repo_root, MAX_METADATA_FILE_SIZE, "staged .xflow/xflow.json"
    )
    config_backup = (
        _reserve_backup_path(
            repo_root,
            snapshot.config.path,
            f"{snapshot.config.path.name}.issue-migration.{transaction_id}.backup.",
            created,
        )
        if snapshot.config.exists
        else None
    )
    plans.append(StagedTarget(snapshot.config, config_stage, config_stage_snapshot, config_backup))
    return plans


def apply_issue_workspace_migration(
    repo_root: Path,
    mode: Literal["tracked", "local"],
) -> IssueWorkspaceMigrationReport:
    repo_root = repo_root.resolve(strict=False)
    with _repository_migration_lock(repo_root):
        _recover_pending_transaction(repo_root)
        report = inspect_issue_workspace_migration(repo_root, mode)
        if report.blockers:
            raise ValueError("cannot apply issue workspace migration: " + "; ".join(report.blockers))

        snapshot = report._snapshot
        if snapshot is None:
            raise ValueError("migration inspection snapshot is missing")
        created: list[Path] = []
        plans: list[StagedTarget] = []
        journal: Path | None = None
        journal_payload: dict[str, object] | None = None
        transaction_id = secrets.token_hex(16)
        owner: TransactionOwner | None = None
        expected_targets = {
            snapshot.config.path: snapshot.config,
            snapshot.gitignore.path: snapshot.gitignore,
        }
        expected_ignore: str | None = snapshot.effective_ignore
        try:
            owner = _create_transaction_owner(repo_root, transaction_id, created)
            plans = _build_plans(repo_root, report, created, transaction_id)
            journal, journal_payload = _write_journal(repo_root, plans, created, owner)
            _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
            for index, plan in enumerate(plans):
                assert journal_payload is not None
                _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
                _update_journal_state(
                    repo_root,
                    journal,
                    journal_payload,
                    index,
                    "committing",
                    created,
                    owner,
                )
                _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
                committed_snapshot = _safe_replace(
                    repo_root,
                    plan.staged,
                    plan.original.path,
                    "migration commit",
                    expected=plan.original,
                    backup=plan.backup,
                )
                expected_targets[plan.original.path] = committed_snapshot
                if plan.original.path == repo_root / ".gitignore":
                    expected_ignore = None
                    remaining_match = _effective_ignore_match(repo_root)
                    if remaining_match is not None:
                        raise ValueError(
                            ".xflow/issues is still effectively ignored; manual action required: "
                            f"{remaining_match.raw}"
                        )
                _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
                _update_journal_state(
                    repo_root,
                    journal,
                    journal_payload,
                    index,
                    "committed",
                    created,
                    owner,
                )
                _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
            _revalidate_snapshot(repo_root, report, expected_targets, expected_ignore)
        except Exception as exc:
            if journal is not None:
                try:
                    finalized = _rollback_transaction(repo_root, plans, journal, transaction_id)
                except ValueError as rollback_exc:
                    raise rollback_exc from exc
                _cleanup_transaction(plans, finalized, created, owner=owner.path if owner else None)
                raise ValueError(f"migration failed and rolled back: {exc}") from exc
            _cleanup_transaction(plans, None, created, owner=owner.path if owner else None)
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"cannot stage issue workspace migration: {exc}") from exc

        finalized = _finalize_journal(repo_root, journal, transaction_id)
        _cleanup_transaction(plans, finalized, created, owner=owner.path if owner else None)
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
        write_text_lf(path, content)
        written.append(path)
    return written
