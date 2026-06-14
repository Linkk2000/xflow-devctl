from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuleEntry:
    rule_id: str
    target: Path
    template: Path
    description: str


def workflow_templates_dir(repo_root: Path) -> Path:
    return repo_root / ".xflow" / "ops" / "workflow" / "templates"


def load_rule_entries(repo_root: Path) -> list[RuleEntry]:
    templates_dir = workflow_templates_dir(repo_root)
    manifest = templates_dir / "ai-rules.json"
    if not manifest.exists():
        raise ValueError(f"AI rule manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    raw_entries = data.get("rules")
    if not isinstance(raw_entries, list):
        raise ValueError("AI rule manifest must contain a rules list")

    entries: list[RuleEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("AI rule manifest entries must be objects")
        rule_id = str(raw.get("id", "")).strip()
        target = str(raw.get("target", "")).strip()
        template = str(raw.get("template", "")).strip()
        description = str(raw.get("description", "")).strip()
        if not rule_id or not target or not template:
            raise ValueError("AI rule manifest entries require id, target, and template")
        if rule_id in seen:
            raise ValueError(f"duplicate AI rule id: {rule_id}")
        seen.add(rule_id)
        if Path(target).is_absolute() or ".." in Path(target).parts:
            raise ValueError(f"invalid AI rule target path: {target}")
        if Path(template).is_absolute() or ".." in Path(template).parts:
            raise ValueError(f"invalid AI rule template path: {template}")
        source = templates_dir / template
        if not source.exists():
            raise ValueError(f"AI rule template not found: {source}")
        entries.append(
            RuleEntry(
                rule_id=rule_id,
                target=Path(target),
                template=source,
                description=description,
            )
        )
    return entries


def find_rule_entry(repo_root: Path, rule_id: str) -> RuleEntry:
    for entry in load_rule_entries(repo_root):
        if entry.rule_id == rule_id:
            return entry
    raise ValueError(f"unknown AI rule id: {rule_id}")


def sync_rule(repo_root: Path, entry: RuleEntry, force: bool = False) -> Path:
    target = repo_root / entry.target
    source_bytes = entry.template.read_bytes()
    if target.exists():
        if target.read_bytes() == source_bytes:
            return target
        if not force:
            raise ValueError(f"{entry.target} already exists and differs; re-run with --force after human review")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry.template, target)
    return target
