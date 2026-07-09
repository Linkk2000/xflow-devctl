from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .io import read_text


@dataclass(frozen=True)
class RuleEntry:
    rule_id: str
    target: Path
    template: Path
    description: str


def templates_dir(repo_root: Path) -> Path:
    return repo_root / ".xflow" / "ops" / "workflow" / "templates"


def load_entries(repo_root: Path) -> list[RuleEntry]:
    root = templates_dir(repo_root)
    manifest = root / "ai-rules.json"
    if not manifest.is_file():
        raise ValueError(f"AI rule manifest not found: {manifest}")
    data = json.loads(read_text(manifest))
    raw_entries = data.get("rules")
    if not isinstance(raw_entries, list):
        raise ValueError("AI rule manifest must contain a rules list")
    entries: list[RuleEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("AI rule entries must be objects")
        rule_id = str(raw.get("id", "")).strip()
        target = Path(str(raw.get("target", "")).strip())
        template_name = Path(str(raw.get("template", "")).strip())
        description = str(raw.get("description", "")).strip()
        if not rule_id or not str(target) or not str(template_name):
            raise ValueError("AI rule entries require id, target, and template")
        if rule_id in seen:
            raise ValueError(f"duplicate AI rule id: {rule_id}")
        if target.is_absolute() or ".." in target.parts:
            raise ValueError(f"invalid AI rule target path: {target}")
        if template_name.is_absolute() or ".." in template_name.parts:
            raise ValueError(f"invalid AI rule template path: {template_name}")
        source = root / template_name
        if not source.is_file():
            raise ValueError(f"AI rule template not found: {source}")
        seen.add(rule_id)
        entries.append(RuleEntry(rule_id, target, source, description))
    return entries


def find_entry(repo_root: Path, rule_id: str) -> RuleEntry:
    for entry in load_entries(repo_root):
        if entry.rule_id == rule_id:
            return entry
    raise ValueError(f"unknown AI rule id: {rule_id}")


def sync(repo_root: Path, entry: RuleEntry, force: bool = False) -> Path:
    target = repo_root / entry.target
    source_bytes = entry.template.read_bytes()
    if target.exists() and target.read_bytes() != source_bytes and not force:
        raise ValueError(f"{entry.target} already exists and differs; re-run with --force after human review")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry.template, target)
    return target
