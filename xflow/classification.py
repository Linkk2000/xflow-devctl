from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .paths import normalized_issue
from .project_config import require_safe_repo_path
from .task_state import CLASSIFICATIONS


CONTRACT_SEARCH_STATUSES = {"found", "not-found"}
_ROOT_FIELDS = {
    "version",
    "request",
    "contractSearch",
    "classification",
    "contractChangeRequired",
    "reason",
    "nextArtifact",
    "decisionSource",
}
_KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z][A-Za-z0-9]*):(?:[ \t].*)?$")
_QUOTED_KEY_LINE = re.compile(r"^(?P<indent> *)[\"'](?P<key>[A-Za-z][A-Za-z0-9]*)[\"']\s*:")
_ANY_QUOTED_KEY_LINE = re.compile(r"^ *[\"'].*[\"']\s*:")


@dataclass(frozen=True)
class ClassificationCheckResult:
    path: Path
    classification: str
    raw: Mapping[str, object]


def _load_yaml(path: Path) -> object:
    try:
        import yaml
        from yaml.tokens import AliasToken, AnchorToken, TagToken
    except ImportError as exc:
        raise RuntimeError(
            "dependency checks require PyYAML; run: python -m pip install -r requirements.txt"
        ) from exc

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read classification file: {path}: {exc}") from exc
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise ValueError("YAML aliases, anchors, and tags are not allowed")
        _reject_duplicate_plain_keys(text)
        return yaml.safe_load(text)
    except ValueError:
        raise
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid classification YAML: {exc}") from exc


def _reject_duplicate_plain_keys(text: str) -> None:
    seen_by_indent: dict[int, set[str]] = {}
    for line in text.splitlines():
        match = _KEY_LINE.fullmatch(line)
        if not match:
            quoted = _QUOTED_KEY_LINE.match(line)
            if quoted:
                indent = len(quoted.group("indent"))
                key = quoted.group("key")
                seen = seen_by_indent.setdefault(indent, set())
                if key in seen:
                    raise ValueError(f"duplicate YAML key: {key}")
            if quoted or _ANY_QUOTED_KEY_LINE.match(line):
                raise ValueError("YAML mapping keys must be plain scalars")
            continue
        indent = len(match.group("indent"))
        key = match.group("key")
        seen = seen_by_indent.setdefault(indent, set())
        if key in seen:
            raise ValueError(f"duplicate YAML key: {key}")
        seen.add(key)


def _mapping(value: object, label: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    fields = set(value)
    if fields != expected:
        missing = sorted(expected - fields)
        unexpected = sorted(fields - expected)
        if missing:
            raise ValueError(f"{label} missing required fields: {', '.join(missing)}")
        raise ValueError(f"{label} contains unexpected fields: {', '.join(str(field) for field in unexpected)}")
    return value


def _non_empty(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must be non-empty")
    return value.strip()


def _refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("contractSearch.refs must be a list")
    refs = tuple(_non_empty(item, "contractSearch.refs item") for item in value)
    if len(set(refs)) != len(refs):
        raise ValueError("contractSearch.refs must not contain duplicates")
    return refs


def _next_artifact(value: object) -> str:
    artifact = _non_empty(value, "nextArtifact")
    candidate = Path(artifact)
    if candidate.is_absolute() or len(candidate.parts) != 1 or artifact in {".", ".."}:
        raise ValueError("nextArtifact must be a file name")
    return artifact


def _classification_file(repo_root: Path, issue: str, file_path: Path | None) -> Path:
    root = repo_root.resolve()
    issue_directory = root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"
    require_safe_repo_path(root, issue_directory, "classification issue directory")
    requested = file_path if file_path is not None else issue_directory / "classification.yaml"
    path = requested if requested.is_absolute() else root / requested
    path = require_safe_repo_path(root, Path(os.path.abspath(path)), "classification file")
    try:
        path.relative_to(issue_directory)
    except ValueError as exc:
        raise ValueError("classification file must stay inside the issue directory") from exc
    if not path.is_file():
        raise ValueError(f"missing classification file: {path}")
    return path


def check_classification(
    repo_root: Path,
    issue: str,
    file_path: Path | None = None,
) -> ClassificationCheckResult:
    path = _classification_file(repo_root, issue, file_path)
    document = _mapping(_load_yaml(path), "classification document", _ROOT_FIELDS)
    _non_empty(document["version"], "version")
    request = _mapping(document["request"], "request", {"originalStatement"})
    _non_empty(request["originalStatement"], "originalStatement")
    contract_search = _mapping(document["contractSearch"], "contractSearch", {"status", "refs"})
    search_status = _non_empty(contract_search["status"], "contractSearch.status")
    if search_status not in CONTRACT_SEARCH_STATUSES:
        raise ValueError(f"invalid contractSearch.status: {search_status}")
    refs = _refs(contract_search["refs"])
    if search_status == "found" and not refs:
        raise ValueError("contractSearch.refs must identify found contracts")
    if search_status == "not-found" and refs:
        raise ValueError("contractSearch.refs must be empty when status is not-found")

    classification = _non_empty(document["classification"], "classification")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"invalid classification: {classification}")
    contract_change_required = document["contractChangeRequired"]
    if not isinstance(contract_change_required, bool):
        raise ValueError("contractChangeRequired must be a boolean")
    _non_empty(document["reason"], "reason")
    next_artifact = _next_artifact(document["nextArtifact"])
    _non_empty(document["decisionSource"], "decisionSource")

    if classification == "capability-change" and not contract_change_required:
        raise ValueError("capability-change requires contractChangeRequired: true")
    if classification == "implementation-gap":
        if contract_change_required:
            raise ValueError("implementation-gap requires contractChangeRequired: false")
        if search_status != "found" or not refs:
            raise ValueError("implementation-gap requires contractSearch.refs for an existing contract")
    if classification == "future" and "implementation" in next_artifact.casefold():
        raise ValueError("future must not route to current implementation")

    return ClassificationCheckResult(path=path, classification=classification, raw=document)
