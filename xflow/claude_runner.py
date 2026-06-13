from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .checks import check_claude_package
from .env import RuntimeContext


@dataclass(frozen=True)
class ClaudeRunResult:
    task_file: Path
    output_file: Path
    dry_run: bool


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    candidate = Path(value.strip())
    if not candidate:
        raise ValueError("empty path")
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, repo_root.resolve()):
        raise ValueError(f"path escapes repo root: {value}")
    return resolved


def parse_output_file(task_file: Path, repo_root: Path) -> Path:
    for line in task_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("Output File:"):
            value = line.split(":", 1)[1].strip()
            if not value:
                raise ValueError("Output File is empty")
            return resolve_repo_path(repo_root, value)
    raise ValueError("Output File: is required")


def resolve_claude_command(env: Mapping[str, str]) -> list[str]:
    command = env.get("DEVCTL_CLAUDE_COMMAND", "claude").strip()
    if not command:
        raise ValueError("DEVCTL_CLAUDE_COMMAND is empty")
    return shlex.split(command, posix=os.name != "nt")


def run_claude_task(
    context: RuntimeContext,
    task_file: Path,
    output_file: Path | None,
    dry_run: bool,
    env: Mapping[str, str],
) -> ClaudeRunResult:
    task_file = task_file.resolve()
    check_claude_package(task_file)
    resolved_output = output_file.resolve() if output_file else parse_output_file(task_file, context.repo_root)
    if not _is_relative_to(resolved_output, context.repo_root.resolve()):
        raise ValueError(f"output path escapes repo root: {resolved_output}")

    if dry_run:
        return ClaudeRunResult(task_file=task_file, output_file=resolved_output, dry_run=True)

    prompt = task_file.read_text(encoding="utf-8")
    command = resolve_claude_command(env)
    try:
        completed = subprocess.run(
            [*command, "-p", prompt],
            cwd=context.repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("Claude CLI not found. Install or expose `claude`, or set DEVCTL_CLAUDE_COMMAND.") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no stderr"
        raise ValueError(f"Claude CLI failed with exit code {completed.returncode}: {stderr}")

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(completed.stdout, encoding="utf-8")
    return ClaudeRunResult(task_file=task_file, output_file=resolved_output, dry_run=False)
