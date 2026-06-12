from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class PythonRuntime:
    executable: str
    version_info: tuple[int, int, int]


@dataclass(frozen=True)
class RuntimeContext:
    tool_root: Path
    repo_root: Path
    product_line: str

    @classmethod
    def from_env(cls, tool_root: Path, env: Mapping[str, str]) -> "RuntimeContext":
        repo = Path(env.get("DEVCTL_REPO_ROOT", Path.cwd())).resolve()
        product = env.get("DEVCTL_PRODUCT_LINE", "")
        return cls(tool_root=tool_root.resolve(), repo_root=repo, product_line=product)


def detect_python_runtime() -> PythonRuntime:
    return PythonRuntime(
        executable=sys.executable,
        version_info=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
    )
