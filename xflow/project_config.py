from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal


DEFAULT_ISSUE_WORKSPACE_MODE: Literal["tracked", "local"] = "tracked"
DEFAULT_CONTRACT_ROOT = Path("docs/requirements")


@dataclass(frozen=True)
class ProjectConfig:
    issue_workspace_mode: Literal["tracked", "local"]
    contract_root: Path


def _invalid(path: Path, message: str) -> ValueError:
    return ValueError(f"invalid .xflow/xflow.json ({path}): {message}")


def _contract_root(path: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(path, "contracts.root must be a non-empty relative path")
    candidate = Path(value)
    windows_candidate = PureWindowsPath(value)
    if candidate.is_absolute() or windows_candidate.is_absolute() or ".." in candidate.parts or ".." in windows_candidate.parts:
        raise _invalid(path, "contracts.root must be a safe relative path")
    if str(candidate) in {"", "."}:
        raise _invalid(path, "contracts.root must name a directory below the repository root")
    return candidate


def load_project_config(repo_root: Path) -> ProjectConfig:
    config_path = repo_root.resolve() / ".xflow" / "xflow.json"
    if not config_path.is_file():
        return ProjectConfig(DEFAULT_ISSUE_WORKSPACE_MODE, DEFAULT_CONTRACT_ROOT)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid(config_path, "must contain a JSON object") from exc
    if not isinstance(raw, dict):
        raise _invalid(config_path, "must contain a JSON object")

    mode: Literal["tracked", "local"] = DEFAULT_ISSUE_WORKSPACE_MODE
    issue_workspace = raw.get("issueWorkspace")
    if issue_workspace is not None:
        if not isinstance(issue_workspace, dict) or set(issue_workspace) != {"mode"}:
            raise _invalid(config_path, "issueWorkspace must contain only mode")
        configured_mode = issue_workspace["mode"]
        if not isinstance(configured_mode, str) or configured_mode not in {"tracked", "local"}:
            raise _invalid(config_path, "issueWorkspace.mode must be tracked or local")
        mode = configured_mode

    contract_root = DEFAULT_CONTRACT_ROOT
    contracts = raw.get("contracts")
    if contracts is not None:
        if not isinstance(contracts, dict) or set(contracts) != {"root"}:
            raise _invalid(config_path, "contracts must contain only root")
        contract_root = _contract_root(config_path, contracts["root"])
    return ProjectConfig(mode, contract_root)
