from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .approval import CREDENTIAL_PATTERNS
from .project_config import load_project_config


@dataclass(frozen=True)
class MigrationReport:
    legacy_ops_present: bool
    v2_ops_present: bool
    messages: tuple[str, ...]


@dataclass(frozen=True)
class IssueWorkspaceMigrationReport:
    mode: Literal["tracked", "local"]
    contract_root: Path
    git_ignore_source: str | None
    exact_ignore_lines: tuple[str, ...]
    active_approvals: tuple[Path, ...]
    oversized_files: tuple[Path, ...]
    absolute_path_files: tuple[Path, ...]
    credential_files: tuple[Path, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        messages: list[str] = []
        if self.active_approvals:
            messages.append("active approvals: " + ", ".join(str(path) for path in self.active_approvals))
        unsafe: list[str] = []
        if self.oversized_files:
            unsafe.append("files over 10 MiB: " + ", ".join(str(path) for path in self.oversized_files))
        if self.absolute_path_files:
            unsafe.append("local absolute paths: " + ", ".join(str(path) for path in self.absolute_path_files))
        if self.credential_files:
            unsafe.append("credential-like text: " + ", ".join(str(path) for path in self.credential_files))
        if unsafe:
            messages.append("unsafe issue workspace: " + "; ".join(unsafe))
        return tuple(messages)


EXACT_ISSUE_IGNORE_LINES = {".xflow/issues", ".xflow/issues/"}
MAX_ISSUE_FILE_SIZE = 10 * 1024 * 1024
LOCAL_ABSOLUTE_PATH = re.compile(r"(?m)(?:^|[\s\"'=(:])(?:[A-Za-z]:[\\/]|\\\\[^\\\r\n]+\\|/(?:Users|home|tmp|var|etc)/)")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _git_ignore_source(repo_root: Path) -> str | None:
    for candidate in (".xflow/issues", ".xflow/issues/"):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-v", "--no-index", candidate],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    gitignore = repo_root / ".gitignore"
    if gitignore.is_file():
        for number, line in enumerate(gitignore.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if line in EXACT_ISSUE_IGNORE_LINES:
                return f".gitignore:{number}:{line}"
    return None


def _issue_files(repo_root: Path) -> tuple[Path, ...]:
    issues_root = repo_root / ".xflow" / "issues"
    if not issues_root.is_dir():
        return ()
    return tuple(sorted(path for path in issues_root.rglob("*") if path.is_file()))


def _read_issue_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def inspect_issue_workspace_migration(
    repo_root: Path, mode: Literal["tracked", "local"]
) -> IssueWorkspaceMigrationReport:
    if mode not in {"tracked", "local"}:
        raise ValueError("issue workspace mode must be tracked or local")
    repo_root = repo_root.resolve()
    project_config = load_project_config(repo_root)
    gitignore = repo_root / ".gitignore"
    exact_ignore_lines: tuple[str, ...] = ()
    if gitignore.is_file():
        exact_ignore_lines = tuple(
            line
            for line in gitignore.read_text(encoding="utf-8-sig").splitlines()
            if line in EXACT_ISSUE_IGNORE_LINES
        )

    active_approvals: list[Path] = []
    oversized_files: list[Path] = []
    absolute_path_files: list[Path] = []
    credential_files: list[Path] = []
    for path in _issue_files(repo_root):
        if path.stat().st_size > MAX_ISSUE_FILE_SIZE:
            oversized_files.append(path)
            continue
        text = _read_issue_text(path)
        if path.name == "local-review.md" and "Approved: yes" in text:
            active_approvals.append(path)
        if LOCAL_ABSOLUTE_PATH.search(text):
            absolute_path_files.append(path)
        if any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS):
            credential_files.append(path)
    return IssueWorkspaceMigrationReport(
        mode=mode,
        contract_root=project_config.contract_root,
        git_ignore_source=_git_ignore_source(repo_root),
        exact_ignore_lines=exact_ignore_lines,
        active_approvals=tuple(active_approvals),
        oversized_files=tuple(oversized_files),
        absolute_path_files=tuple(absolute_path_files),
        credential_files=tuple(credential_files),
    )


def _load_config_object(config_path: Path) -> dict[str, object]:
    if not config_path.is_file():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid .xflow/xflow.json ({config_path}): must contain a JSON object") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid .xflow/xflow.json ({config_path}): must contain a JSON object")
    return raw


def _remove_exact_issue_ignores(gitignore: Path) -> tuple[str, ...]:
    if not gitignore.is_file():
        return ()
    lines = gitignore.read_text(encoding="utf-8-sig").splitlines()
    removed = tuple(line for line in lines if line in EXACT_ISSUE_IGNORE_LINES)
    if removed:
        remaining = [line for line in lines if line not in EXACT_ISSUE_IGNORE_LINES]
        _atomic_write(gitignore, "\n".join(remaining) + ("\n" if remaining else ""))
    return removed


def apply_issue_workspace_migration(repo_root: Path, mode: Literal["tracked", "local"]) -> IssueWorkspaceMigrationReport:
    report = inspect_issue_workspace_migration(repo_root, mode)
    if report.blockers:
        raise ValueError("cannot apply issue workspace migration: " + "; ".join(report.blockers))
    repo_root = repo_root.resolve()
    config_path = repo_root / ".xflow" / "xflow.json"
    config = _load_config_object(config_path)
    config["issueWorkspace"] = {"mode": mode}
    config["contracts"] = {"root": report.contract_root.as_posix()}
    _atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    if mode == "tracked":
        _remove_exact_issue_ignores(repo_root / ".gitignore")
    return report


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
