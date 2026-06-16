from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .claude_runner import find_resolvable_claude_skill, resolve_repo_path
from .checks import load_academicforge_skill_names
from .env import RuntimeContext


DEFAULT_SOURCE_REPO = "git@github.com:HughYau/AcademicForge.git"
DEFAULT_SOURCE_REF = "main"
DEFAULT_SOURCE_DIR = ".xflow/local/academicforge-source"
DEFAULT_TARGET_ROOT = ".claude/skills"
DEFAULT_SETUP_SKILLS = ("literature-review", "exa-search", "peer-review")


@dataclass(frozen=True)
class ClaudeSetupPlan:
    approved: bool
    source_repo: str
    source_ref: str
    source_dir: Path
    target_root: Path
    skills: tuple[tuple[str, str], ...]


def parse_skill_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_SETUP_SKILLS
    skills = tuple(part.strip().lstrip("/") for part in value.split(",") if part.strip())
    if not skills:
        raise ValueError("--skills did not contain any skill names")
    return skills


def workflow_catalog_path(repo_root: Path) -> Path:
    return repo_root / ".xflow" / "ops" / "workflow" / "references" / "academicforge-skill-catalog.md"


def load_academicforge_skill_paths(repo_root: Path) -> dict[str, str]:
    catalog = workflow_catalog_path(repo_root)
    if not catalog.is_file():
        raise ValueError(f"missing AcademicForge path catalog: {catalog}")
    paths: dict[str, str] = {}
    pattern = re.compile(r"^\|\s*`/([^`]+)`\s*\|\s*`([^`]+)`\s*\|")
    for line in catalog.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            paths[match.group(1)] = match.group(2)
    if not paths:
        raise ValueError(f"empty AcademicForge path catalog: {catalog}")
    return paths


def setup_status_lines(context: RuntimeContext) -> list[str]:
    repo_root = context.repo_root
    target_root = (repo_root / DEFAULT_TARGET_ROOT).resolve()
    lines = [
        f"target_root: {target_root}",
        "skills:",
    ]
    for name in sorted(load_academicforge_skill_names()):
        installed = find_resolvable_claude_skill({"DEVCTL_REPO_ROOT": str(repo_root)}, name)
        status = "installed" if installed else "missing"
        lines.append(f"{name}\t{status}\t{installed or ''}")
    return lines


def write_setup_plan(
    context: RuntimeContext,
    skills: tuple[str, ...],
    source_repo: str = DEFAULT_SOURCE_REPO,
    source_ref: str = DEFAULT_SOURCE_REF,
    source_dir: str = DEFAULT_SOURCE_DIR,
    target_root: str = DEFAULT_TARGET_ROOT,
    plan_file: Path | None = None,
) -> Path:
    repo_root = context.repo_root
    allowed = load_academicforge_skill_names()
    paths = load_academicforge_skill_paths(repo_root)
    selected: list[tuple[str, str]] = []
    for skill in skills:
        name = skill.lstrip("/")
        if name not in allowed:
            raise ValueError(f"unknown AcademicForge skill: {name}")
        if name not in paths:
            raise ValueError(f"missing source path for AcademicForge skill: {name}")
        selected.append((name, paths[name]))

    target = plan_file or repo_root / ".xflow" / "local" / "claude-setup-plan.md"
    if not target.is_absolute():
        target = repo_root / target
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Claude Skill Setup Plan",
        "",
        "Approved: no",
        f"Source Repo: {source_repo}",
        f"Source Ref: {source_ref}",
        f"Source Directory: {source_dir}",
        f"Target Root: {target_root}",
        "",
        "## Skills",
        "",
    ]
    lines.extend(f"- {name}: {relative}" for name, relative in selected)
    lines.extend(
        [
            "",
            "## Human Review Gate",
            "",
            "Review the source repository, target root, and skill list. Change `Approved: no` to `Approved: yes` only after approval.",
        ]
    )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _simple_field(text: str, field: str) -> str:
    match = re.search(rf"(?m)^{re.escape(field)}\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def read_setup_plan(repo_root: Path, plan_file: Path) -> ClaudeSetupPlan:
    path = plan_file if plan_file.is_absolute() else repo_root / plan_file
    if not path.is_file():
        raise ValueError(f"missing Claude setup plan: {path}")
    text = path.read_text(encoding="utf-8")
    approved = _simple_field(text, "Approved:").lower() == "yes"
    source_repo = _simple_field(text, "Source Repo:")
    source_ref = _simple_field(text, "Source Ref:")
    source_dir_text = _simple_field(text, "Source Directory:")
    target_root_text = _simple_field(text, "Target Root:")
    if not source_repo or not source_ref or not source_dir_text or not target_root_text:
        raise ValueError("Claude setup plan is missing required fields")
    skills: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        match = re.match(r"^\s*-\s*([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$", raw_line)
        if match:
            skills.append((match.group(1), match.group(2).strip()))
    if not skills:
        raise ValueError("Claude setup plan has no skills")
    return ClaudeSetupPlan(
        approved=approved,
        source_repo=source_repo,
        source_ref=source_ref,
        source_dir=resolve_repo_path(repo_root, source_dir_text),
        target_root=resolve_repo_path(repo_root, target_root_text),
        skills=tuple(skills),
    )


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"git command failed: git {' '.join(args)}: {detail}")


def ensure_source_tree(plan: ClaudeSetupPlan) -> None:
    if plan.source_dir.exists():
        if not any(plan.source_dir.rglob("SKILL.md")):
            raise ValueError(f"AcademicForge source directory has no SKILL.md files: {plan.source_dir}")
        return
    plan.source_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["clone", "--depth", "1", "--branch", plan.source_ref, plan.source_repo, str(plan.source_dir)], cwd=plan.source_dir.parent)


def apply_setup_plan(repo_root: Path, plan_file: Path) -> list[Path]:
    plan = read_setup_plan(repo_root, plan_file)
    if not plan.approved:
        raise ValueError("Claude setup plan must contain Approved: yes before apply")
    ensure_source_tree(plan)
    written: list[Path] = []
    plan.target_root.mkdir(parents=True, exist_ok=True)
    for skill, relative in plan.skills:
        source = (plan.source_dir / relative).resolve()
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            raise ValueError(f"missing source skill directory for /{skill}: {source}")
        target = (plan.target_root / skill).resolve()
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        written.append(target / "SKILL.md")
    return written
