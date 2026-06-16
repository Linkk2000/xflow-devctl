from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MigrationReport:
    legacy_ops_present: bool
    v2_ops_present: bool
    messages: tuple[str, ...]


def inspect(repo_root: Path) -> MigrationReport:
    repo_root = repo_root.resolve()
    legacy = (repo_root / "_ops" / "devctl").exists() or (repo_root / "_ops" / "workflow").exists()
    v2 = (repo_root / ".xflow" / "ops" / "devctl").exists() or (repo_root / ".xflow" / "ops" / "workflow").exists()
    messages: list[str] = []
    if legacy:
        messages.append("legacy _ops layout detected")
    if v2:
        messages.append("v2 .xflow/ops layout detected")
    gitmodules = repo_root / ".gitmodules"
    if gitmodules.is_file():
        text = gitmodules.read_text(encoding="utf-8")
        if "https://github.com/Linkk2000/xflow-" in text:
            messages.append("https submodule URL detected")
        if ".xflow/ops/devctl" not in text or ".xflow/ops/workflow" not in text:
            messages.append("v2 submodule paths missing")
        if "ignore = untracked" not in text:
            messages.append("submodule ignore = untracked missing")
    else:
        messages.append(".gitmodules missing")
    return MigrationReport(legacy, v2, tuple(messages))


def wrapper_files() -> dict[str, str]:
    bash = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$ROOT/.xflow/ops/devctl"

if [[ ! -d "$TOOL_ROOT/xflow" ]]; then
  echo "[ERROR] Missing XFlow devctl Python core at .xflow/ops/devctl/xflow" >&2
  exit 1
fi

export DEVCTL_REPO_ROOT="$ROOT"
export DEVCTL_TOOL_ROOT="$TOOL_ROOT"
export DEVCTL_OPS_ROOT="$TOOL_ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$TOOL_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python -m xflow "$@"
"""
    ps1 = """$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolRoot = Join-Path $Root ".xflow\\ops\\devctl"
$Package = Join-Path $ToolRoot "xflow"

if (-not (Test-Path -LiteralPath $Package)) {
    Write-Error "[ERROR] Missing XFlow devctl Python core at .xflow\\ops\\devctl\\xflow"
    exit 1
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Error "[ERROR] Python 3.10+ is required for devctl Python core."
    exit 1
}

$env:DEVCTL_REPO_ROOT = $Root
$env:DEVCTL_TOOL_ROOT = $ToolRoot
$env:DEVCTL_OPS_ROOT = $ToolRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$ToolRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $ToolRoot
}

python -m xflow @args
exit $LASTEXITCODE
"""
    return {"devctl": bash, "devctl.ps1": ps1}


def write_wrappers(repo_root: Path) -> list[Path]:
    written: list[Path] = []
    for name, content in wrapper_files().items():
        path = repo_root / name
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written
