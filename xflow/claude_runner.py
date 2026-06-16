from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .checks import (
    check_claude_package,
    claude_invocation_from_package,
    claude_skill_source_from_package,
    load_academicforge_skill_names,
)
from .env import RuntimeContext


@dataclass(frozen=True)
class ClaudeRunResult:
    task_file: Path
    output_file: Path
    dry_run: bool


@dataclass(frozen=True)
class ClaudeDoctorResult:
    claude_cli_ok: bool
    academicforge_ok: bool
    source_root: Path | None
    resolvable_skills: tuple[Path, ...]
    checked_skill_roots: tuple[Path, ...]
    install_hint: str = (
        "Install or mirror AcademicForge skills into Claude-resolvable paths such as "
        ".claude/skills/peer-review/SKILL.md."
    )


GENERIC_CLAUDE_OUTPUT_MARKERS = (
    "unknown command:",
    "what would you like me to do",
    "what would you like me to help",
)


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
    parts = shlex.split(command, posix=os.name != "nt")
    executable = parts[0]
    if not Path(executable).is_absolute():
        resolved = shutil.which(executable)
        if resolved:
            parts[0] = resolved
    return parts


def resolve_claude_args(env: Mapping[str, str]) -> list[str]:
    args = env.get("DEVCTL_CLAUDE_ARGS", "").strip()
    if not args:
        return []
    parsed = shlex.split(args, posix=os.name != "nt")
    return ["" if arg in ('""', "''") else arg for arg in parsed]


def resolve_claude_timeout(env: Mapping[str, str]) -> float:
    raw = env.get("DEVCTL_CLAUDE_TIMEOUT_SECONDS", "300").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid DEVCTL_CLAUDE_TIMEOUT_SECONDS: {raw}") from exc
    if timeout <= 0:
        raise ValueError("DEVCTL_CLAUDE_TIMEOUT_SECONDS must be positive")
    return timeout


def _safe_home() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def _configured_path(env: Mapping[str, str], name: str) -> Path | None:
    configured = env.get(name, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return None


def academicforge_install_candidates(env: Mapping[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    explicit = _configured_path(env, "DEVCTL_ACADEMICFORGE_SKILL_FILE")
    if explicit:
        candidates.append(explicit.parent)
    explicit_dir = _configured_path(env, "DEVCTL_ACADEMICFORGE_SKILL_DIR")
    if explicit_dir:
        candidates.append(explicit_dir)
    repo_root = _configured_path(env, "DEVCTL_REPO_ROOT")
    if repo_root:
        candidates.append(repo_root / ".claude" / "skills" / "academic-forge")
    home = _safe_home()
    if home:
        candidates.append(home / ".claude" / "skills" / "academic-forge")
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def claude_skill_roots(env: Mapping[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    repo_root = _configured_path(env, "DEVCTL_REPO_ROOT")
    if repo_root:
        candidates.append(repo_root / ".claude" / "skills")
    home = _safe_home()
    if home:
        candidates.append(home / ".claude" / "skills")
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _has_skill_file(root: Path) -> bool:
    if not root.is_dir():
        return False
    return any(root.rglob("SKILL.md"))


def find_academicforge_install_root(env: Mapping[str, str]) -> Path | None:
    for candidate in academicforge_install_candidates(env):
        if _has_skill_file(candidate):
            return candidate
    return None


def find_resolvable_claude_skill(env: Mapping[str, str], skill_name: str) -> Path | None:
    for root in claude_skill_roots(env):
        candidate = root / skill_name / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def find_resolvable_academicforge_skills(env: Mapping[str, str]) -> tuple[Path, ...]:
    names = load_academicforge_skill_names()
    found: list[Path] = []
    for root in claude_skill_roots(env):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and child.name in names and (child / "SKILL.md").is_file():
                found.append((child / "SKILL.md").resolve())
    return tuple(sorted(dict.fromkeys(found)))


def find_installed_academicforge_skill(env: Mapping[str, str], skill_name: str) -> Path | None:
    return find_resolvable_claude_skill(env, skill_name)


def require_installed_academicforge_skill(env: Mapping[str, str], skill_name: str) -> None:
    source_root = find_academicforge_install_root(env)
    if find_resolvable_claude_skill(env, skill_name) is None:
        if source_root is not None:
            raise ValueError(
                f"installed AcademicForge source exists at {source_root}, but /{skill_name} is not Claude-resolvable. "
                f"Mirror or copy it to .claude/skills/{skill_name}/SKILL.md before running Claude."
            )
        raise ValueError(
            f"AcademicForge skill is not installed in a Claude-resolvable path: /{skill_name}. "
            "Run `devctl claude doctor` and obtain human approval before installing or mirroring skills."
        )


def validate_claude_output(text: str) -> None:
    if not text.strip():
        raise ValueError("Claude output is empty")
    lowered = text.lower()
    for marker in GENERIC_CLAUDE_OUTPUT_MARKERS:
        if marker in lowered:
            raise ValueError(f"Claude output appears non-actionable: {marker}")


def run_claude_doctor(env: Mapping[str, str]) -> ClaudeDoctorResult:
    command = resolve_claude_command(env)
    executable = command[0]
    cli_ok = Path(executable).exists() if Path(executable).is_absolute() else shutil.which(executable) is not None
    source_root = find_academicforge_install_root(env)
    resolvable_skills = find_resolvable_academicforge_skills(env)
    return ClaudeDoctorResult(
        claude_cli_ok=cli_ok,
        academicforge_ok=bool(resolvable_skills),
        source_root=source_root,
        resolvable_skills=resolvable_skills,
        checked_skill_roots=claude_skill_roots(env),
    )


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

    task_text = task_file.read_text(encoding="utf-8")
    invocation = claude_invocation_from_package(task_file)
    skill_name, skill_source = claude_skill_source_from_package(task_file)
    if "academicforge" in skill_source.lower():
        require_installed_academicforge_skill(env, skill_name)
    prompt = f"{invocation}\n\n{task_text}"
    command = resolve_claude_command(env)
    args = resolve_claude_args(env)
    timeout = resolve_claude_timeout(env)
    try:
        completed = subprocess.run(
            [*command, *args, "-p", prompt],
            cwd=context.repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ValueError("Claude CLI not found. Install or expose `claude`, or set DEVCTL_CLAUDE_COMMAND.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"Claude CLI timed out after {timeout:g} seconds. "
            "Use DEVCTL_CLAUDE_ARGS to constrain tools or DEVCTL_CLAUDE_TIMEOUT_SECONDS to adjust the limit."
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or "no output"
        raise ValueError(f"Claude CLI failed with exit code {completed.returncode}: {detail}")

    validate_claude_output(completed.stdout)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(completed.stdout, encoding="utf-8")
    return ClaudeRunResult(task_file=task_file, output_file=resolved_output, dry_run=False)
