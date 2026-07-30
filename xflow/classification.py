from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .paths import normalized_issue
from .project_config import _is_reparse_point, require_safe_repo_path
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
    next_artifact: str


ROUTE_RULES = {
    "capability-change": RouteRule(True, frozenset(CONTRACT_SEARCH_STATUSES), "contract-change-proposal.md"),
    "implementation-gap": RouteRule(False, frozenset({"found"}), "gap-analysis.md"),
    "ui-defect": RouteRule(False, frozenset({"found"}), "issue-draft.md"),
    "infrastructure": RouteRule(False, frozenset(CONTRACT_SEARCH_STATUSES), "dependency-issue-draft.md"),
    "governance": RouteRule(False, frozenset(CONTRACT_SEARCH_STATUSES), "issue-draft.md"),
    "future": RouteRule(False, frozenset(CONTRACT_SEARCH_STATUSES), "futureCapabilitiesOutOfScope"),
}


def _identity(path_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(path_stat.st_dev),
        int(path_stat.st_ino),
        int(path_stat.st_mode),
        int(path_stat.st_size),
        int(path_stat.st_mtime_ns),
        int(path_stat.st_ctime_ns),
    )


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    left_identity = _identity(left)
    right_identity = _identity(right)
    if left_identity[0] and left_identity[1] and right_identity[0] and right_identity[1]:
        return left_identity[:3] == right_identity[:3]
    return left_identity == right_identity


def _component_snapshot(repo_root: Path, path: Path) -> tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...]:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"classification file is outside repository: {path}") from exc
    snapshots: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    current = repo_root
    for part in relative.parts:
        current /= part
        try:
            path_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise ValueError(f"missing classification file: {path}") from exc
        except OSError as exc:
            raise ValueError(f"cannot inspect classification file path component {current}: {exc}") from exc
        if _is_reparse_point(path_stat):
            raise ValueError(
                f"classification file must not traverse a symlink, junction, or reparse point: {current}"
            )
        snapshots.append((current, _identity(path_stat)))
    return tuple(snapshots)


def _verify_component_snapshot(
    snapshots: tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...],
) -> None:
    for component, expected_identity in snapshots:
        try:
            path_stat = os.lstat(component)
        except OSError as exc:
            raise ValueError(f"classification file changed while reading: {component}: {exc}") from exc
        if _is_reparse_point(path_stat) or _identity(path_stat) != expected_identity:
            raise ValueError(f"classification file changed while reading: {component}")


def _windows_final_handle_path(descriptor: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    size = 32_768
    buffer = ctypes.create_unicode_buffer(size)
    length = get_final_path(handle, buffer, size, 0)
    if not length:
        error = ctypes.get_last_error()
        raise ValueError(f"cannot resolve classification file handle: Windows error {error}")
    if length >= size:
        buffer = ctypes.create_unicode_buffer(length + 1)
        length = get_final_path(handle, buffer, length + 1, 0)
        if not length:
            error = ctypes.get_last_error()
            raise ValueError(f"cannot resolve classification file handle: Windows error {error}")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(os.path.abspath(value))


def _final_handle_path(descriptor: int, path: Path) -> Path:
    if os.name == "nt":
        return _windows_final_handle_path(descriptor)
    descriptor_path = Path(f"/proc/self/fd/{descriptor}")
    try:
        return Path(os.path.abspath(os.readlink(descriptor_path)))
    except OSError:
        return Path(os.path.abspath(path.resolve(strict=False)))


def _read_stable_text(repo_root: Path, path: Path, issue_directory: Path) -> str:
    snapshots = _component_snapshot(repo_root, path)
    before_path = os.lstat(path)
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"classification file must be a regular file: {path}")
    if before_path.st_size > MAX_CLASSIFICATION_BYTES:
        raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot read classification file: {path}: {exc}") from exc
    try:
        before_handle = os.fstat(descriptor)
        if not _same_file_object(before_path, before_handle):
            raise ValueError(f"classification file changed while opening: {path}")
        final_path = _final_handle_path(descriptor, path)
        try:
            final_path.relative_to(issue_directory)
        except ValueError as exc:
            raise ValueError(
                f"classification file handle resolves outside the issue directory: {final_path}"
            ) from exc

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(
                    descriptor,
                    min(READ_CHUNK_SIZE, MAX_CLASSIFICATION_BYTES + 1 - total),
                )
            except OSError as exc:
                raise ValueError(f"cannot read classification file: {path}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CLASSIFICATION_BYTES:
                raise ValueError(f"classification file exceeds {MAX_CLASSIFICATION_BYTES} bytes: {path}")
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"classification file changed while reading: {path}: {exc}") from exc
    if _identity(before_handle) != _identity(after_handle) or not _same_file_object(after_handle, after_path):
        raise ValueError(f"classification file changed while reading: {path}")
    _verify_component_snapshot(snapshots)
    require_safe_repo_path(repo_root, path, "classification file")
    content = b"".join(chunks)
    try:
        return content.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"classification file must be valid UTF-8: {path}") from exc


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
    require_safe_repo_path(root, issue_directory, "classification issue directory")
    requested = file_path if file_path is not None else issue_directory / "classification.yaml"
    path = requested if requested.is_absolute() else root / requested
    path = require_safe_repo_path(root, Path(os.path.abspath(path)), "classification file")
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
    if next_artifact != route.next_artifact:
        raise ValueError(f"{classification} requires nextArtifact: {route.next_artifact}")

    return ClassificationCheckResult(path=path, classification=classification, raw=document)
