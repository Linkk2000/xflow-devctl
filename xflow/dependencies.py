from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import normalized_issue


DEPENDENCY_TYPES = {"child-feature", "shared-infrastructure", "external"}
DEPENDENCY_STATUSES = {"discovered", "active", "available", "integrated", "superseded"}
BLOCKING_ASSESSMENTS = {"none", "partial", "full"}
DEVELOPMENT_DECISIONS = {"continue", "pause-affected-scope", "wait", "use-temporary-adapter"}
CLOSURE_DECISIONS = {"integrated", "not-required", "superseded"}


@dataclass(frozen=True)
class DependencyCheckResult:
    path: Path
    entries: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


def check_dependency_closure(
    result: DependencyCheckResult,
    conclusion: str,
) -> tuple[str, ...]:
    if conclusion not in {"resolved", "reduced", "blocked"}:
        raise ValueError(f"unknown resolution conclusion: {conclusion}")
    if conclusion != "resolved":
        return ()

    violations: list[str] = []
    for entry in result.entries:
        dependency = str(entry["issue"])
        closure = entry.get("closureAssessment")
        if not isinstance(closure, dict):
            violations.append(f"dependency #{dependency} requires closureAssessment for resolved")
            continue
        affects_closure = closure.get("affectsClosure")
        decision = closure.get("decision")
        status = entry.get("status")
        if affects_closure is False:
            if decision != "not-required":
                violations.append(
                    f"dependency #{dependency} with affectsClosure false requires closure decision not-required"
                )
            continue
        if status == "integrated" and decision == "integrated":
            continue
        if status == "superseded" and decision == "superseded":
            continue
        violations.append(
            f"dependency #{dependency} affects closure but is {status} with closure decision {decision}"
        )
    return tuple(violations)


def load_yaml(path: Path) -> object:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "dependency checks require PyYAML; run: python -m pip install -r requirements.txt"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _non_empty(value: object, label: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{label} must be non-empty")
    return str(value).strip()


def _non_empty_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    for item in value:
        _non_empty(item, label)
    return value


def _enum(value: object, choices: set[str], label: str) -> str:
    candidate = _non_empty(value, label)
    if candidate not in choices:
        raise ValueError(f"invalid {label}: {candidate}; expected one of {'|'.join(sorted(choices))}")
    return candidate


def _validate_closure_assessment(entry: dict[str, Any], dependency: str) -> None:
    if "closureAssessment" not in entry:
        return
    closure = _mapping(entry["closureAssessment"], f"dependency #{dependency} closureAssessment")
    if not isinstance(closure.get("affectsClosure"), bool):
        raise ValueError(f"dependency #{dependency} closureAssessment.affectsClosure must be true or false")
    _enum(
        closure.get("decision"),
        CLOSURE_DECISIONS,
        f"dependency #{dependency} closure decision",
    )
    _non_empty(closure.get("rationale"), f"dependency #{dependency} closureAssessment.rationale")


def _validate_delivery(entry: dict[str, Any], dependency: str) -> None:
    delivery = _mapping(entry.get("delivery"), f"dependency #{dependency} delivery")
    for field in ("branch", "commit", "mergeRequest"):
        _non_empty(delivery.get(field), f"dependency #{dependency} delivery.{field}")


def _validate_external_availability(entry: dict[str, Any], dependency: str) -> None:
    for field in ("provider", "availableVersion", "verificationEntry"):
        _non_empty(entry.get(field), f"dependency #{dependency} {field}")


def _validate_integration(entry: dict[str, Any], dependency: str, issue_directory: Path) -> None:
    integration = _mapping(entry.get("integration"), f"dependency #{dependency} integration")
    _non_empty(integration.get("commit"), f"dependency #{dependency} integration.commit")
    _non_empty_list(integration.get("verifiedBy"), f"dependency #{dependency} integration.verifiedBy")
    evidence = _non_empty_list(integration.get("evidence"), f"dependency #{dependency} integration.evidence")

    # Import lazily so checks.py can call this module without a module-import cycle.
    from .checks import check_issue_local_evidence

    raw_evidence = "\n".join(f"- {item}" for item in evidence)
    evidence_paths = check_issue_local_evidence(
        issue_directory,
        raw_evidence,
        f"dependency #{dependency} integration",
    )
    for evidence_path in evidence_paths:
        if not evidence_path.is_file():
            raise ValueError(
                f"dependency #{dependency} integration evidence must be a file: "
                f"{evidence_path.relative_to(issue_directory)}"
            )
    if any(Path(str(item)).name.lower() == "resolution-report.md" for item in evidence):
        raise ValueError(
            f"dependency #{dependency} integration evidence must be fresh parent-side evidence, not a dependency resolution-report"
        )


def check_dependencies(
    repo_root: Path,
    issue: str,
    file_path: Path | None = None,
) -> DependencyCheckResult:
    from .checks import issue_dir, require_inside, resolve_repo_path

    repo_root = repo_root.resolve()
    expected_issue = normalized_issue(issue)
    issue_directory = issue_dir(repo_root, expected_issue).resolve()
    path = resolve_repo_path(repo_root, file_path or issue_directory / "dependencies.yaml")
    require_inside(path, issue_directory, "dependencies file must stay inside the issue directory")
    if not path.is_file():
        raise ValueError(f"missing dependencies file: {path}")

    document = _mapping(load_yaml(path), "dependencies document")
    _non_empty(document.get("version"), "dependencies version")
    document_issue = normalized_issue(_non_empty(document.get("issue"), "top-level issue"))
    if document_issue != expected_issue:
        raise ValueError(f"top-level issue mismatch: expected {expected_issue}, found {document_issue}")
    raw_entries = document.get("dependencies")
    if not isinstance(raw_entries, list):
        raise ValueError("dependencies must be a list")

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw_entry in enumerate(raw_entries, start=1):
        entry = dict(_mapping(raw_entry, f"dependency entry {index}"))
        dependency = normalized_issue(_non_empty(entry.get("issue"), f"dependency entry {index} issue"))
        entry["issue"] = dependency
        _non_empty(entry.get("repository"), f"dependency #{dependency} repository")
        dependency_type = _enum(entry.get("type"), DEPENDENCY_TYPES, f"dependency #{dependency} type")
        if "delivery" in entry:
            _mapping(entry["delivery"], f"dependency #{dependency} delivery")
            if dependency_type == "external":
                raise ValueError(f"external dependency #{dependency} must not declare delivery")
        _non_empty_list(entry.get("requiredFor"), f"dependency #{dependency} requiredFor")
        status = _enum(entry.get("status"), DEPENDENCY_STATUSES, f"dependency #{dependency} status")
        _enum(
            entry.get("blockingAssessment"),
            BLOCKING_ASSESSMENTS,
            f"dependency #{dependency} blockingAssessment",
        )
        decision = _enum(
            entry.get("decision"),
            DEVELOPMENT_DECISIONS,
            f"dependency #{dependency} development decision",
        )
        if decision == "use-temporary-adapter":
            _non_empty(
                entry.get("removalCondition"),
                f"dependency #{dependency} removalCondition",
            )
        _non_empty(entry.get("rationale"), f"dependency #{dependency} rationale")
        _validate_closure_assessment(entry, dependency)

        if status in {"available", "integrated"}:
            if dependency_type == "external":
                _validate_external_availability(entry, dependency)
            else:
                _validate_delivery(entry, dependency)
        if status == "integrated":
            _validate_integration(entry, dependency, issue_directory)
        if status == "superseded":
            closure = _mapping(entry.get("closureAssessment"), f"superseded dependency #{dependency} closureAssessment")
            if closure.get("decision") != "superseded" or not str(closure.get("rationale", "")).strip():
                raise ValueError(
                    f"superseded dependency #{dependency} requires closure decision superseded and a non-empty rationale"
                )
        if status in {"discovered", "active", "available"}:
            warnings.append(f"dependency #{dependency} is {status}; developer decision remains {decision}")
        entries.append(entry)

    return DependencyCheckResult(path=path, entries=tuple(entries), warnings=tuple(warnings))
