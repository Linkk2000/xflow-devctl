from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .paths import normalized_issue
from .task_state import CLASSIFICATIONS


CONTRACT_SEARCH_STATUSES = {"found", "not-found"}
SUPPORTED_VERSION = "0.1.0"
MAX_CLASSIFICATION_BYTES = 262_144
MAX_YAML_DEPTH = 32
MAX_YAML_NODES = 1_024
MAX_YAML_TOKENS = 8_192
MAX_COLLECTION_ITEMS = 256
MAX_SCALAR_CHARACTERS = 65_536
READ_CHUNK_SIZE = 65_536
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x10
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ROOT_FIELDS = {
    "version",
    "request",
    "contractSearch",
    "classification",
    "contractChangeRequired",
    "reason",
    "nextArtifact",
    "decisionSource",
}


@dataclass(frozen=True)
class ClassificationCheckResult:
    path: Path
    classification: str
    raw: Mapping[str, object]


@dataclass(frozen=True)
class RouteRule:
    contract_change_required: bool
    search_statuses: frozenset[str]
    next_artifacts: tuple[str, ...]


ROUTE_RULES = {
    "capability-change": RouteRule(True, frozenset(CONTRACT_SEARCH_STATUSES), ("contract-change-proposal.md",)),
    "implementation-gap": RouteRule(False, frozenset({"found"}), ("gap-analysis.md",)),
    "ui-defect": RouteRule(False, frozenset({"found"}), ("issue-draft.md",)),
    "infrastructure": RouteRule(
        False,
        frozenset(CONTRACT_SEARCH_STATUSES),
        ("dependency-issue-proposal.md",),
    ),
    "governance": RouteRule(False, frozenset(CONTRACT_SEARCH_STATUSES), ("issue-draft.md",)),
    "future": RouteRule(
        False,
        frozenset(CONTRACT_SEARCH_STATUSES),
        ("futureCapabilitiesOutOfScope", "future-task-proposal.md"),
    ),
}


def _decode_classification_bytes(content: bytes, path: Path) -> str:
    try:
        return content.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"classification file must be valid UTF-8: {path}") from exc


def _relative_classification_parts(repo_root: Path, path: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"classification file is outside repository: {path}") from exc
    if not relative.parts:
        raise ValueError(f"classification file must be a regular file: {path}")
    return relative.parts


def _read_stable_text_posix(repo_root: Path, path: Path) -> str:
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_RDONLY")) or os.open not in os.supports_dir_fd:
        raise ValueError("trustworthy descriptor-bound classification traversal is unavailable")

    descriptors: list[int] = []
    parts = _relative_classification_parts(repo_root, path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        try:
            root_descriptor = os.open(repo_root, directory_flags)
            descriptors.append(root_descriptor)
            if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
                raise ValueError(f"classification repository root must be a directory: {repo_root}")
        except FileNotFoundError as exc:
            raise ValueError(f"missing classification file: {path}") from exc
        except OSError as exc:
            raise ValueError(f"cannot open classification file safely: {path}: {exc}") from exc

        parent_descriptor = root_descriptor
        for part in parts[:-1]:
            try:
                descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
                descriptors.append(descriptor)
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise ValueError(f"classification file path component is not a directory: {part}")
            except FileNotFoundError as exc:
                raise ValueError(f"missing classification file: {path}") from exc
            except OSError as exc:
                raise ValueError(f"cannot open classification file safely: {path}: {exc}") from exc
            parent_descriptor = descriptor

        try:
            descriptor = os.open(parts[-1], file_flags, dir_fd=parent_descriptor)
            descriptors.append(descriptor)
            file_stat = os.fstat(descriptor)
        except FileNotFoundError as exc:
            raise ValueError(f"missing classification file: {path}") from exc
        except OSError as exc:
            raise ValueError(f"cannot open classification file safely: {path}: {exc}") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"classification file must be a regular file: {path}")
        if file_stat.st_size > MAX_CLASSIFICATION_BYTES:
            raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, min(READ_CHUNK_SIZE, MAX_CLASSIFICATION_BYTES + 1 - total))
            except OSError as exc:
                raise ValueError(f"cannot read classification file: {path}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CLASSIFICATION_BYTES:
                raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")
        try:
            os.fstat(descriptor)
        except OSError as exc:
            raise ValueError(f"classification file changed while reading: {path}: {exc}") from exc
        return _decode_classification_bytes(b"".join(chunks), path)
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = close_error or exc
        if close_error is not None:
            raise ValueError(f"cannot close classification file safely: {path}: {close_error}") from close_error


class _WindowsApi:
    """Small native layer kept injectable so no-reparse traversal is testable."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create_file = self._kernel32.CreateFileW
        self._create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self._create_file.restype = wintypes.HANDLE
        self._get_information = self._kernel32.GetFileInformationByHandle
        self._read_file = self._kernel32.ReadFile
        self._close_handle = self._kernel32.CloseHandle
        self._get_file_type = self._kernel32.GetFileType

        class FileTime(ctypes.Structure):
            _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", FileTime),
                ("ftLastAccessTime", FileTime),
                ("ftLastWriteTime", FileTime),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            )

        self._file_information_type = ByHandleFileInformation
        self._get_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
        self._get_information.restype = wintypes.BOOL
        self._read_file.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        self._read_file.restype = wintypes.BOOL
        self._close_handle.argtypes = (wintypes.HANDLE,)
        self._close_handle.restype = wintypes.BOOL
        self._get_file_type.argtypes = (wintypes.HANDLE,)
        self._get_file_type.restype = wintypes.DWORD

    def _information(self, handle: int) -> object:
        information = self._file_information_type()
        if not self._get_information(handle, self._ctypes.byref(information)):
            raise OSError(self._ctypes.get_last_error(), "GetFileInformationByHandle failed")
        return information

    def open(self, target: Path, *, directory: bool) -> int:
        desired_access = 0x80000000  # GENERIC_READ
        share_mode = 0x00000001 | 0x00000002  # FILE_SHARE_READ | FILE_SHARE_WRITE, never FILE_SHARE_DELETE.
        flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
        handle = self._create_file(str(target), desired_access, share_mode, None, 3, flags, None)
        invalid_handle = self._ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(self._ctypes.get_last_error(), f"CreateFileW failed for {target}")
        return int(handle)

    def attributes(self, handle: int) -> int:
        return int(self._information(handle).dwFileAttributes)  # type: ignore[union-attr]

    def file_size(self, handle: int) -> int:
        information = self._information(handle)
        return (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow)  # type: ignore[union-attr]

    def is_regular_file(self, handle: int) -> bool:
        return self._get_file_type(handle) == 1  # FILE_TYPE_DISK

    def read(self, handle: int, size: int) -> bytes:
        buffer = self._ctypes.create_string_buffer(size)
        read = self._wintypes.DWORD()
        if not self._read_file(handle, buffer, size, self._ctypes.byref(read), None):
            raise OSError(self._ctypes.get_last_error(), "ReadFile failed")
        return buffer.raw[: read.value]

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise OSError(self._ctypes.get_last_error(), "CloseHandle failed")


def _read_stable_text_windows(repo_root: Path, path: Path, issue_directory: Path, api: Any | None = None) -> str:
    del issue_directory  # Directory-handle traversal establishes this boundary directly.
    native = api if api is not None else _WindowsApi()
    handles: list[int] = []
    parts = _relative_classification_parts(repo_root, path)

    def open_checked(target: Path, *, directory: bool) -> int:
        try:
            handle = native.open(target, directory=directory)
            handles.append(handle)
            attributes = native.attributes(handle)
        except FileNotFoundError as exc:
            raise ValueError(f"missing classification file: {path}") from exc
        except OSError as exc:
            raise ValueError(f"cannot open classification file safely: {path}: {exc}") from exc
        if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(
                f"classification file must not traverse a symlink, junction, or reparse point: {target}"
            )
        is_directory = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
        if directory and not is_directory:
            raise ValueError(f"classification file path component is not a directory: {target}")
        if not directory and is_directory:
            raise ValueError(f"classification file must be a regular file: {path}")
        return handle

    try:
        open_checked(repo_root, directory=True)
        current = repo_root
        for part in parts[:-1]:
            current /= part
            open_checked(current, directory=True)
        current /= parts[-1]
        descriptor = open_checked(current, directory=False)
        try:
            if not native.is_regular_file(descriptor):
                raise ValueError(f"classification file must be a regular file: {path}")
            if native.file_size(descriptor) > MAX_CLASSIFICATION_BYTES:
                raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")
        except OSError as exc:
            raise ValueError(f"classification file changed while opening: {path}: {exc}") from exc

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = native.read(descriptor, min(READ_CHUNK_SIZE, MAX_CLASSIFICATION_BYTES + 1 - total))
            except OSError as exc:
                raise ValueError(f"cannot read classification file: {path}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CLASSIFICATION_BYTES:
                raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")
        try:
            native.file_size(descriptor)
        except OSError as exc:
            raise ValueError(f"classification file changed while reading: {path}: {exc}") from exc
        return _decode_classification_bytes(b"".join(chunks), path)
    finally:
        close_error: OSError | None = None
        for handle in reversed(handles):
            try:
                native.close(handle)
            except OSError as exc:
                close_error = close_error or exc
        if close_error is not None:
            raise ValueError(f"cannot close classification file safely: {path}: {close_error}") from close_error


def _read_stable_text(repo_root: Path, path: Path, issue_directory: Path) -> str:
    if os.name == "nt":
        return _read_stable_text_windows(repo_root, path, issue_directory)
    return _read_stable_text_posix(repo_root, path)


def _load_yaml(text: str) -> object:
    try:
        import yaml
        from yaml.tokens import AliasToken, AnchorToken, TagToken
    except ImportError as exc:
        raise ValueError(
            "dependency checks require PyYAML; run: python -m pip install -r requirements.txt"
        ) from exc

    class BoundedSafeLoader(yaml.SafeLoader):
        def __init__(self, stream: str) -> None:
            self._classification_depth = 0
            self._classification_nodes = 0
            super().__init__(stream)

        def compose_node(self, parent: object, index: object) -> object:
            self._classification_depth += 1
            self._classification_nodes += 1
            try:
                if self._classification_depth > MAX_YAML_DEPTH:
                    raise ValueError("classification YAML exceeds nesting limit")
                if self._classification_nodes > MAX_YAML_NODES:
                    raise ValueError("classification YAML exceeds node limit")
                return super().compose_node(parent, index)
            finally:
                self._classification_depth -= 1

        def construct_scalar(self, node: object) -> object:
            value = super().construct_scalar(node)
            if isinstance(value, str) and len(value) > MAX_SCALAR_CHARACTERS:
                raise ValueError(
                    f"classification YAML scalar exceeds {MAX_SCALAR_CHARACTERS} characters"
                )
            return value

        def construct_sequence(self, node: object, deep: bool = False) -> list[object]:
            if len(node.value) > MAX_COLLECTION_ITEMS:  # type: ignore[attr-defined]
                raise ValueError("classification YAML exceeds collection limit")
            return super().construct_sequence(node, deep=deep)

        def construct_mapping(self, node: object, deep: bool = False) -> dict[str, object]:
            if len(node.value) > MAX_COLLECTION_ITEMS:  # type: ignore[attr-defined]
                raise ValueError("classification YAML exceeds collection limit")
            mapping: dict[str, object] = {}
            for key_node, value_node in node.value:  # type: ignore[attr-defined]
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise ValueError("YAML mapping keys must be strings")
                if key in mapping:
                    raise ValueError(f"duplicate YAML key: {key}")
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    try:
        for token_count, token in enumerate(yaml.scan(text), start=1):
            if token_count > MAX_YAML_TOKENS:
                raise ValueError("classification YAML exceeds token limit")
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise ValueError("YAML aliases, anchors, and tags are not allowed")
        loader = BoundedSafeLoader(text)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()
    except ValueError:
        raise
    except RecursionError as exc:
        raise ValueError("classification YAML exceeds nesting limit") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid classification YAML: {exc}") from exc


def _mapping(value: object, label: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    fields = set(value)
    if fields != expected:
        missing = sorted(expected - fields)
        unexpected = sorted(fields - expected)
        if missing:
            raise ValueError(f"{label} missing required fields: {', '.join(missing)}")
        raise ValueError(f"{label} contains unexpected fields: {', '.join(str(field) for field in unexpected)}")
    return value


def _non_empty(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must be non-empty")
    return value.strip()


def _refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("contractSearch.refs must be a list")
    refs = tuple(_non_empty(item, "contractSearch.refs item") for item in value)
    if len(set(refs)) != len(refs):
        raise ValueError("contractSearch.refs must not contain duplicates")
    return refs


def _next_artifact(value: object) -> str:
    artifact = _non_empty(value, "nextArtifact")
    candidate = Path(artifact)
    if candidate.is_absolute() or len(candidate.parts) != 1 or artifact in {".", ".."}:
        raise ValueError("nextArtifact must be a file name")
    return artifact


def _classification_file(repo_root: Path, issue: str, file_path: Path | None) -> Path:
    root = repo_root.resolve()
    issue_directory = root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"
    requested = file_path if file_path is not None else issue_directory / "classification.yaml"
    path = requested if requested.is_absolute() else root / requested
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"classification file is outside repository: {path}") from exc
    try:
        path.relative_to(issue_directory)
    except ValueError as exc:
        raise ValueError("classification file must stay inside the issue directory") from exc
    return path


def check_classification(
    repo_root: Path,
    issue: str,
    file_path: Path | None = None,
) -> ClassificationCheckResult:
    path = _classification_file(repo_root, issue, file_path)
    issue_directory = repo_root.resolve() / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"
    document = _mapping(
        _load_yaml(_read_stable_text(repo_root.resolve(), path, issue_directory)),
        "classification document",
        _ROOT_FIELDS,
    )
    version = _non_empty(document["version"], "version")
    if version != SUPPORTED_VERSION:
        raise ValueError(f"unsupported classification version: {version}")
    request = _mapping(document["request"], "request", {"originalStatement"})
    _non_empty(request["originalStatement"], "originalStatement")
    contract_search = _mapping(document["contractSearch"], "contractSearch", {"status", "refs"})
    search_status = _non_empty(contract_search["status"], "contractSearch.status")
    if search_status not in CONTRACT_SEARCH_STATUSES:
        raise ValueError(f"invalid contractSearch.status: {search_status}")
    refs = _refs(contract_search["refs"])
    if search_status == "found" and not refs:
        raise ValueError("contractSearch.refs must identify found contracts")
    if search_status == "not-found" and refs:
        raise ValueError("contractSearch.refs must be empty when status is not-found")

    classification = _non_empty(document["classification"], "classification")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"invalid classification: {classification}")
    contract_change_required = document["contractChangeRequired"]
    if not isinstance(contract_change_required, bool):
        raise ValueError("contractChangeRequired must be a boolean")
    _non_empty(document["reason"], "reason")
    next_artifact = _next_artifact(document["nextArtifact"])
    _non_empty(document["decisionSource"], "decisionSource")

    route = ROUTE_RULES[classification]
    if contract_change_required is not route.contract_change_required:
        literal = "true" if route.contract_change_required else "false"
        raise ValueError(f"{classification} requires contractChangeRequired: {literal}")
    if search_status not in route.search_statuses:
        expected = " or ".join(sorted(route.search_statuses))
        raise ValueError(f"{classification} requires contractSearch.status: {expected}")
    if next_artifact not in route.next_artifacts:
        expected = " or ".join(route.next_artifacts)
        raise ValueError(f"{classification} requires nextArtifact: {expected}")

    return ClassificationCheckResult(path=path, classification=classification, raw=document)
