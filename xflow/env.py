from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping


TOKEN_NAMES = ("GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_TOKEN", "GITEE_TOKEN", "GITEE_ACCESS_TOKEN", "GITEE_PRIVATE_TOKEN")


@dataclass(frozen=True)
class RuntimeContext:
    tool_root: Path
    repo_root: Path
    product_line: str

    @classmethod
    def from_env(cls, tool_root: Path, env: Mapping[str, str]) -> "RuntimeContext":
        return cls(
            tool_root=tool_root.resolve(),
            repo_root=Path(env.get("DEVCTL_REPO_ROOT", Path.cwd())).resolve(),
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


def env_file_candidates(env: Mapping[str, str]) -> list[Path]:
    home = Path.home()
    candidates = [home / "gitee.env.local", home / ".xflow" / "env.local"]
    explicit = env.get("XFLOW_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    return candidates


def load_env_files(env: MutableMapping[str, str]) -> list[Path]:
    original_keys = {key for key, value in env.items() if value}
    merged: dict[str, str] = {}
    loaded: list[Path] = []

    for path in env_file_candidates(env):
        expanded = path.expanduser()
        if not expanded.is_file():
            continue
        merged.update(parse_env_file(expanded))
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
