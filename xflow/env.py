from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

from .io import canonical_path


TOKEN_NAMES = ("GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_TOKEN", "GITEE_TOKEN", "GITEE_ACCESS_TOKEN", "GITEE_PRIVATE_TOKEN")
PROJECT_SCOPED_KEYS = {"XFLOW_PLATFORM"}


@dataclass(frozen=True)
class RuntimeContext:
    tool_root: Path
    repo_root: Path
    product_line: str

    @classmethod
    def from_env(cls, tool_root: Path, env: Mapping[str, str]) -> "RuntimeContext":
        return cls(
            tool_root=canonical_path(tool_root),
            repo_root=canonical_path(Path(env.get("DEVCTL_REPO_ROOT", Path.cwd()))),
            product_line=env.get("DEVCTL_PRODUCT_LINE", ""),
        )


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def home_from_env(env: Mapping[str, str]) -> Path:
    for name in ("HOME", "USERPROFILE"):
        value = env.get(name, "").strip()
        if value:
            return canonical_path(Path(value))
    return canonical_path(Path.home())


def repo_root_from_env(env: Mapping[str, str]) -> Path:
    return canonical_path(Path(env.get("DEVCTL_REPO_ROOT", Path.cwd())))


def cockpit_root_from_env(env: Mapping[str, str]) -> Optional[Path]:
    """Return an explicitly configured cockpit root, if one was provided."""

    for name in ("XFLOW_COCKPIT_ROOT", "DEVCTL_COCKPIT_ROOT", "XFLOW_COCKPIT"):
        value = env.get(name, "").strip()
        if value:
            return canonical_path(Path(value))
    return None


def profile_path_from_env(env: Mapping[str, str]) -> Optional[Path]:
    """Return an explicitly configured cockpit profile, if one was provided."""

    for name in (
        "XFLOW_PROFILE",
        "XFLOW_COCKPIT_PROFILE",
        "DEVCTL_PROFILE",
        "DEVCTL_COCKPIT_PROFILE",
    ):
        value = env.get(name, "").strip()
        if value:
            return canonical_path(Path(value))
    return None


def env_file_candidates(
    env: Mapping[str, str],
    *,
    project_root: Optional[Path] = None,
    include_user: bool = True,
    include_project: bool = True,
    include_explicit: bool = True,
) -> list[tuple[Path, str]]:
    home = home_from_env(env)
    repo_root = repo_root_from_env(env) if project_root is None else canonical_path(project_root)
    candidates: list[tuple[Path, str]] = []
    if include_user:
        candidates.extend(
            [
                (home / "gitee.env.local", "user"),
                (home / ".xflow" / "env.local", "user"),
            ]
        )
    if include_project:
        candidates.append((repo_root / ".xflow" / "local" / "env.local", "project"))
    explicit = env.get("XFLOW_ENV_FILE", "").strip()
    if include_explicit and explicit:
        candidates.append((canonical_path(Path(explicit)), "explicit"))
    return candidates


def load_env_files(
    env: MutableMapping[str, str],
    *,
    project_root: Optional[Path] = None,
    include_user: bool = True,
    include_project: bool = True,
    include_explicit: bool = True,
) -> list[Path]:
    original_keys = {key for key, value in env.items() if value}
    merged: dict[str, str] = {}
    loaded: list[Path] = []

    for path, scope in env_file_candidates(
        env,
        project_root=project_root,
        include_user=include_user,
        include_project=include_project,
        include_explicit=include_explicit,
    ):
        expanded = canonical_path(path)
        if not expanded.is_file():
            continue
        values = parse_env_file(expanded)
        if scope == "user":
            values = {key: value for key, value in values.items() if key not in PROJECT_SCOPED_KEYS}
        merged.update(values)
        loaded.append(expanded)

    for key, value in merged.items():
        if key not in original_keys:
            env[key] = value

    if loaded:
        previous = [value for value in env.get("XFLOW_LOADED_ENV_FILES", "").split(os.pathsep) if value]
        env["XFLOW_LOADED_ENV_FILES"] = os.pathsep.join(previous + [str(path) for path in loaded])
    return loaded


def load_target_env_files(
    env: MutableMapping[str, str],
    repo_root: Path,
    *,
    preserve_keys: set[str] | frozenset[str] = frozenset(),
) -> list[Path]:
    """Load target-project context after a canonical repository is selected.

    User environment is intentionally loaded by ``load_env_files`` first.  A
    target project then overrides those values, while explicitly supplied host
    variables (and values from an explicitly selected env file) remain
    authoritative.
    """

    candidates = env_file_candidates(
        env,
        project_root=repo_root,
        include_user=False,
        include_project=True,
        include_explicit=True,
    )
    merged: dict[str, str] = {}
    loaded: list[Path] = []
    for path, _scope in candidates:
        expanded = canonical_path(path)
        if not expanded.is_file():
            continue
        merged.update(parse_env_file(expanded))
        loaded.append(expanded)

    preserved = set(preserve_keys)
    for key, value in merged.items():
        if key not in preserved:
            env[key] = value
    if loaded:
        previous = [value for value in env.get("XFLOW_LOADED_ENV_FILES", "").split(os.pathsep) if value]
        env["XFLOW_LOADED_ENV_FILES"] = os.pathsep.join(previous + [str(path) for path in loaded])
    return loaded


def token_status_lines(env: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for name in ("GITHUB_TOKEN", "GITEE_TOKEN"):
        lines.append(f"{name}={'SET' if env.get(name) else 'UNSET'}")
    return lines
