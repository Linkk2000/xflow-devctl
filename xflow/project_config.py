from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from .io import canonical_path


if TYPE_CHECKING:
    from .local_artifacts import StableFileSnapshot


DEFAULT_ISSUE_WORKSPACE_MODE: Literal["tracked", "local"] = "tracked"
DEFAULT_CONTRACT_ROOT = Path("docs/requirements")


@dataclass(frozen=True)
class ProjectConfig:
    issue_workspace_mode: Literal["tracked", "local"]
    contract_root: Path


def _invalid(path: Path, message: str) -> ValueError:
    return ValueError(f"invalid .xflow/xflow.json ({path}): {message}")


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_attribute)


def _normalize_windows_final_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _canonical_repo_path(path: Path) -> Path:
    return Path(os.path.abspath(_normalize_windows_final_path(str(path))))


def _equivalent_root_relative_parts(root: Path, target: Path) -> tuple[str, ...] | None:
    target_parts = target.parts
    for index in range(1, len(target_parts) + 1):
        prefix = Path(*target_parts[:index])
        try:
            resolved_prefix = _canonical_repo_path(canonical_path(prefix))
        except (OSError, RuntimeError):
            continue
        if os.path.normcase(str(resolved_prefix)) == os.path.normcase(str(root)):
            return target_parts[index:]
    return None


def require_safe_repo_path(repo_root: Path, path: Path, label: str) -> Path:
    root = _canonical_repo_path(canonical_path(repo_root))
    target = path if path.is_absolute() else root / path
    target = _canonical_repo_path(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        relative_parts = _equivalent_root_relative_parts(root, target)
        if relative_parts is None:
            raise ValueError(f"{label} is outside repository: {target}") from exc
        target = root.joinpath(*relative_parts)
        relative = target.relative_to(root)

    current = root
    for part in relative.parts:
        current /= part
        try:
            path_stat = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"cannot inspect {label} path component {current}: {exc}") from exc
        if _is_reparse_point(path_stat):
            raise ValueError(f"{label} must not traverse a symlink, junction, or reparse point: {current}")

    resolved = _canonical_repo_path(target.resolve(strict=False))
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside repository: {target} -> {resolved}") from exc
    return target


def _contract_root(repo_root: Path, path: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(path, "contracts.root must be a non-empty relative path")
    if value.lower().startswith("file:"):
        raise _invalid(path, "contracts.root must not be a file URI")
    candidate = Path(value)
    windows_candidate = PureWindowsPath(value)
    posix_candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or windows_candidate.anchor
        or windows_candidate.drive
        or windows_candidate.root
        or posix_candidate.anchor
        or posix_candidate.root
        or ".." in candidate.parts
        or ".." in windows_candidate.parts
        or ".." in posix_candidate.parts
    ):
        raise _invalid(path, "contracts.root must be a safe relative path")
    if str(candidate) in {"", "."}:
        raise _invalid(path, "contracts.root must name a directory below the repository root")
    try:
        require_safe_repo_path(repo_root, repo_root / candidate, "contracts.root")
    except ValueError as exc:
        raise _invalid(path, str(exc)) from exc
    return candidate


def parse_project_config(repo_root: Path, config_path: Path, raw: object) -> ProjectConfig:
    if not isinstance(raw, dict):
        raise _invalid(config_path, "must contain a JSON object")

    mode: Literal["tracked", "local"] = DEFAULT_ISSUE_WORKSPACE_MODE
    if "issueWorkspace" in raw:
        issue_workspace = raw["issueWorkspace"]
        if not isinstance(issue_workspace, dict) or set(issue_workspace) != {"mode"}:
            raise _invalid(config_path, "issueWorkspace must contain only mode")
        configured_mode = issue_workspace["mode"]
        if not isinstance(configured_mode, str) or configured_mode not in {"tracked", "local"}:
            raise _invalid(config_path, "issueWorkspace.mode must be tracked or local")
        mode = configured_mode

    contract_root = DEFAULT_CONTRACT_ROOT
    if "contracts" in raw:
        contracts = raw["contracts"]
        if not isinstance(contracts, dict) or set(contracts) != {"root"}:
            raise _invalid(config_path, "contracts must contain only root")
        contract_root = _contract_root(repo_root.resolve(strict=False), config_path, contracts["root"])
    return ProjectConfig(mode, contract_root)


def load_project_config_snapshot(repo_root: Path) -> tuple[StableFileSnapshot, ProjectConfig]:
    from .local_artifacts import MAX_SMALL_ARTIFACT_BYTES, capture_stable_file

    repo_root = repo_root.resolve(strict=False)
    config_path = require_safe_repo_path(repo_root, repo_root / ".xflow" / "xflow.json", ".xflow/xflow.json")
    snapshot = capture_stable_file(
        repo_root,
        config_path,
        repo_root,
        ".xflow/xflow.json",
        required=False,
        max_bytes=MAX_SMALL_ARTIFACT_BYTES,
    )
    if not snapshot.exists:
        return snapshot, ProjectConfig(DEFAULT_ISSUE_WORKSPACE_MODE, DEFAULT_CONTRACT_ROOT)
    try:
        raw = json.loads((snapshot.content or b"").decode("utf-8-sig", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(config_path, "must contain a UTF-8 JSON object") from exc
    return snapshot, parse_project_config(repo_root, config_path, raw)


def load_project_config(repo_root: Path) -> ProjectConfig:
    return load_project_config_snapshot(repo_root)[1]
