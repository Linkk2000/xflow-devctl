from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MigrationReport:
    legacy_ops_present: bool
    v2_ops_present: bool
    messages: tuple[str, ...]


def inspect_migration(repo_root: Path) -> MigrationReport:
    repo_root = repo_root.resolve()
    legacy_ops_present = (repo_root / "_ops" / "devctl").exists() or (repo_root / "_ops" / "workflow").exists()
    v2_ops_present = (repo_root / ".xflow" / "ops" / "devctl").exists() or (
        repo_root / ".xflow" / "ops" / "workflow"
    ).exists()
    messages: list[str] = []

    if legacy_ops_present:
        messages.append("legacy _ops layout detected")
    if v2_ops_present:
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

    return MigrationReport(
        legacy_ops_present=legacy_ops_present,
        v2_ops_present=v2_ops_present,
        messages=tuple(messages),
    )


def build_v2_wrapper_files() -> dict[str, str]:
    bash = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_DEVCTL="$ROOT/.xflow/ops/devctl/devctl"

if [[ ! -f "$REAL_DEVCTL" ]]; then
  echo "[ERROR] Missing XFlow devctl at .xflow/ops/devctl/devctl" >&2
  echo "[INFO] Initialize reviewed tool submodules before running devctl." >&2
  exit 1
fi

export DEVCTL_REPO_ROOT="$ROOT"
export DEVCTL_PRODUCT_LINE="${DEVCTL_PRODUCT_LINE:-academic}"
exec bash "$REAL_DEVCTL" "$@"
"""
    ps1 = """# Windows devctl wrapper for Academic XFlow v2.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolRoot = Join-Path $Root ".xflow\\ops\\devctl"
$XflowPackage = Join-Path $ToolRoot "xflow"

if (-not (Test-Path -LiteralPath $XflowPackage)) {
    Write-Error "[ERROR] Missing XFlow Python core at .xflow\\ops\\devctl\\xflow"
    Write-Host "[INFO] Initialize reviewed tool submodules before running devctl."
    exit 1
}

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Error "[ERROR] Python 3.10+ is required for devctl Python core."
    Write-Host "[INFO] No installation was performed. Install Python only after human review."
    exit 1
}

$env:DEVCTL_REPO_ROOT = $Root
$env:DEVCTL_TOOL_ROOT = $ToolRoot
$env:DEVCTL_OPS_ROOT = $ToolRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
if (-not $env:DEVCTL_PRODUCT_LINE) {
    $env:DEVCTL_PRODUCT_LINE = "academic"
}
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$ToolRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $ToolRoot
}

python -m xflow @args
exit $LASTEXITCODE
"""
    return {"devctl": bash, "devctl.ps1": ps1}


def write_v2_wrapper_files(repo_root: Path) -> list[Path]:
    repo_root = repo_root.resolve()
    written: list[Path] = []
    for relative, content in build_v2_wrapper_files().items():
        target = repo_root / relative
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)
    return written
