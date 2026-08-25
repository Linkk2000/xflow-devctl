from __future__ import annotations

from pathlib import Path


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def write_text_lf(path: Path, text: str, *, mode: str = "w") -> None:
    if mode not in {"w", "x"}:
        raise ValueError(f"unsupported text write mode: {mode}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="strict")
