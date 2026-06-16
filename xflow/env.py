from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
