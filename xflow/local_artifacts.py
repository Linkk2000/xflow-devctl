from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .classification import _read_stable_bytes
from .project_config import _is_reparse_point, require_safe_repo_path


URI_REFERENCE_RE = re.compile(r"(?i)(?:^|[\\/\s(\[{'\"`])(?:[a-z][a-z0-9+.-]*):(?=\S)")
OBJECT_STORAGE_DOMAIN_RE = re.compile(
    r"(?i)(?:"
    r"aliyuncs\.com|"
    r"(?:cos\.[a-z0-9-]+\.)?myqcloud\.com|qcloudcos|"
    r"(?:^|[./])s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com|amazonaws\.com|cloudfront\.net|"
    r"(?:blob|dfs|file)\.core\.windows\.net|"
    r"storage\.googleapis\.com|storage\.cloud\.google\.com|"
    r"(?:^|[./])obs(?:[.-][a-z0-9-]+)*\.myhuaweicloud\.com|myhuaweicloud\.com|"
    r"r2\.cloudflarestorage\.com|"
    r"objectstorage(?:\.[a-z0-9-]+)*\.oraclecloud\.com|"
    r"cloud-object-storage\.appdomain\.cloud|"
    r"digitaloceanspaces\.com|wasabisys\.com|backblazeb2\.com"
    r")"
)
OBJECT_STORAGE_SCHEME_RE = re.compile(r"(?i)(?:^|[\\/\s(\[{'\"`])(?:oss|cos|s3|gs|az|obs|r2)://")


@dataclass(frozen=True)
class StableFileSnapshot:
    path: Path
    allowed_root: Path
    exists: bool
    object_identity: tuple[int, int] | None
    digest: str | None
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    link_count: int | None
    content: bytes | None = field(repr=False, compare=True)


def contains_forbidden_remote_reference(value: str) -> bool:
    return URI_REFERENCE_RE.search(value) is not None or contains_forbidden_object_storage_reference(value)


def contains_forbidden_object_storage_reference(value: str) -> bool:
    return OBJECT_STORAGE_SCHEME_RE.search(value) is not None or OBJECT_STORAGE_DOMAIN_RE.search(value) is not None


def safe_relative_reference(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    raw = value.strip()
    if value != raw or not raw:
        raise ValueError(f"{label} must be a non-empty path without surrounding whitespace")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root or raw.startswith(("\\\\", "/")):
        raise ValueError(f"{label} must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError(f"{label} must not contain '..'")
    if contains_forbidden_remote_reference(raw):
        raise ValueError(f"{label} must not use a URI or object-storage domain")
    if raw == "." or any(part in {"", "."} for part in posix.parts):
        raise ValueError(f"{label} must name a file")
    return Path(raw)


def _stat_record(path_stat: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(path_stat.st_dev),
        int(path_stat.st_ino),
        int(path_stat.st_mode),
        int(path_stat.st_size),
        int(path_stat.st_mtime_ns),
        int(path_stat.st_ctime_ns),
        int(path_stat.st_nlink),
    )


def capture_stable_file(
    repo_root: Path,
    path: Path,
    allowed_root: Path,
    label: str,
    *,
    required: bool = True,
) -> StableFileSnapshot:
    root = repo_root.resolve(strict=False)
    allowed = require_safe_repo_path(root, allowed_root, f"{label} owner")
    target = require_safe_repo_path(root, path, label)
    try:
        target.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside {allowed}") from exc
    try:
        before = os.lstat(target)
    except FileNotFoundError as exc:
        if required:
            raise ValueError(f"missing {label}: {target}") from exc
        return StableFileSnapshot(target, allowed, False, None, None, None, None, None, None, None)
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}: {target}: {exc}") from exc
    if _is_reparse_point(before):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {target}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file: {target}")
    if int(before.st_nlink) != 1:
        raise ValueError(f"{label} must have exactly one filesystem link: {target}")
    try:
        content = _read_stable_bytes(root, target, allowed)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification file", label).replace("classification", label)) from exc
    try:
        after = os.lstat(target)
    except OSError as exc:
        raise ValueError(f"{label} changed while reading: {target}: {exc}") from exc
    if _stat_record(before) != _stat_record(after):
        raise ValueError(f"{label} changed while reading: {target}")
    if _is_reparse_point(after) or not stat.S_ISREG(after.st_mode) or int(after.st_nlink) != 1:
        raise ValueError(f"{label} identity changed while reading: {target}")
    return StableFileSnapshot(
        target,
        allowed,
        True,
        (int(after.st_dev), int(after.st_ino)),
        hashlib.sha256(content).hexdigest(),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
        int(after.st_nlink),
        content,
    )


def revalidate_snapshots(repo_root: Path, snapshots: tuple[StableFileSnapshot, ...], label: str) -> None:
    seen: set[Path] = set()
    for expected in snapshots:
        if expected.path in seen:
            continue
        seen.add(expected.path)
        actual = capture_stable_file(
            repo_root,
            expected.path,
            expected.allowed_root,
            label,
            required=expected.exists,
        )
        if actual != expected:
            raise ValueError(f"{label} changed during closure validation: {expected.path}")
