from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

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


def env_file_candidates(env: Mapping[str, str]) -> list[tuple[Path, str]]:
    home = home_from_env(env)
    repo_root = repo_root_from_env(env)
    candidates = [
        (home / "gitee.env.local", "user"),
        (home / ".xflow" / "env.local", "user"),
        (repo_root / ".xflow" / "local" / "env.local", "project"),
    ]
    explicit = env.get("XFLOW_ENV_FILE", "").strip()
    if explicit:
        candidates.append((canonical_path(Path(explicit)), "explicit"))
    return candidates


def load_env_files(env: MutableMapping[str, str]) -> list[Path]:
    original_keys = {key for key, value in env.items() if value}
    merged: dict[str, str] = {}
    loaded: list[Path] = []

    for path, scope in env_file_candidates(env):
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
        env["XFLOW_LOADED_ENV_FILES"] = os.pathsep.join(str(path) for path in loaded)
    return loaded


def token_status_lines(env: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for name in ("GITHUB_TOKEN", "GITEE_TOKEN"):
        lines.append(f"{name}={'SET' if env.get(name) else 'UNSET'}")
    return lines
