from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

from . import approval
from .classification import _load_yaml, _read_stable_text
from .project_config import load_project_config, require_safe_repo_path


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PLACEHOLDERS = {
    "-",
    "n/a",
    "na",
    "none",
    "placeholder",
    "tbd",
    "todo",
    "unknown",
    "待定",
    "待补充",
    "占位",
}
_ROOT_FIELDS = {
    "id",
    "version",
    "name",
    "status",
    "created",
    "note",
    "capabilityContract",
    "semanticValueContracts",
    "failureReasonContracts",
    "context",
    "contextRoles",
    "interactionContracts",
    "verificationMatrix",
    "engineeringProjections",
    "dependsOn",
    "preconditionsToResolve",
    "futureCapabilitiesOutOfScope",
    "references",
}
_OPTIONAL_OBJECT_FIELDS = {"supersedes"}
_CONTRACT_STATUSES = {
    "draft",
    "accepted-design",
    "active",
    "deprecated",
}
_PRECONDITION_STATUSES = {"open", "deferred", "resolved"}
_PRECONDITION_STAGES = {"accepted-design", "engineering-projection", "implementation", "never"}
_STAGE_ORDER = {"accepted-design": 1, "engineering-projection": 2, "implementation": 3}


@dataclass(frozen=True)
class ContractObject:
    id: str
    version: str
    kind: str
    value: dict[str, object]


@dataclass(frozen=True)
class ContractDocument:
    path: Path
    raw: Mapping[str, object]
    objects_by_id: Mapping[str, ContractObject]
    verification_by_id: Mapping[str, ContractObject]


def _mapping(value: object, label: str, required: set[str], optional: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a mapping")
    allowed = required | (optional or set())
    fields = set(value)
    missing = sorted(required - fields)
    unexpected = sorted(fields - allowed)
    if missing:
        raise ValueError(f"{label} missing required fields: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"{label} contains unexpected fields: {', '.join(unexpected)}")
    return dict(value)


def _meaningful(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text or text.casefold() in _PLACEHOLDERS:
        raise ValueError(f"{label} must be meaningful")
    return text


def _identifier(value: object, label: str) -> str:
    identifier = _meaningful(value, label)
    if any(character.isspace() for character in identifier):
        raise ValueError(f"{label} must not contain whitespace")
    return identifier


def _semver(value: object, label: str) -> str:
    version = _meaningful(value, label)
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"invalid semantic version for {label}: {version}")
    return version


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    entries = tuple(_meaningful(item, f"{label} item") for item in value)
    if len(entries) != len(set(entries)):
        raise ValueError(f"{label} must not contain duplicates")
    return entries


def _id_refs(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    entries = tuple(_identifier(item, f"{label} item") for item in value)
    if len(entries) != len(set(entries)):
        raise ValueError(f"{label} must not contain duplicates")
    return entries


def _items(value: object, label: str, *, allow_empty: bool = False) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not value and not allow_empty:
        raise ValueError(f"{label} must be a non-empty list")
    return value


def _object(value: object, label: str, kind: str, required: set[str]) -> ContractObject:
    mapped = _mapping(value, label, required, _OPTIONAL_OBJECT_FIELDS)
    identifier = _identifier(mapped["id"], f"{label}.id")
    version = _semver(mapped["version"], f"{label}.version")
    if "supersedes" in mapped:
        _id_refs(mapped["supersedes"], f"{label}.supersedes")
    return ContractObject(identifier, version, kind, mapped)


def _add_object(objects: dict[str, ContractObject], item: ContractObject) -> None:
    if item.id in objects:
        raise ValueError(f"duplicate contract object id: {item.id}")
    objects[item.id] = item


def _validate_date(value: object) -> None:
    if isinstance(value, date):
        return
    text = _meaningful(value, "created")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("created must be an ISO date") from exc


def _contract_path(repo_root: Path, file_path: Path) -> tuple[Path, Path]:
    root = repo_root.resolve()
    config = load_project_config(root)
    contract_root = require_safe_repo_path(root, root / config.contract_root, "contracts.root")
    requested = file_path if file_path.is_absolute() else root / file_path
    target = Path(os.path.abspath(requested))
    try:
        target.relative_to(contract_root)
    except ValueError as exc:
        raise ValueError(f"contract file must stay under contracts.root: {contract_root}") from exc
    safe_target = require_safe_repo_path(root, target, "contract file")
    return contract_root, safe_target


def _stable_contract_text(repo_root: Path, contract_root: Path, path: Path) -> str:
    try:
        return _read_stable_text(repo_root, path, contract_root)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "contract")) from exc


def _parse_contract_yaml(text: str) -> dict[str, object]:
    try:
        raw = _load_yaml(text)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "contract")) from exc
    document = _mapping(raw, "contract document", _ROOT_FIELDS)
    if isinstance(document["created"], date):
        document["created"] = document["created"].isoformat()
    return document


def _require_reference(objects: Mapping[str, ContractObject], identifier: str, label: str) -> ContractObject:
    try:
        return objects[identifier]
    except KeyError as exc:
        raise ValueError(f"missing referenced contract object for {label}: {identifier}") from exc


def _require_kind(objects: Mapping[str, ContractObject], identifier: str, label: str, kinds: set[str]) -> None:
    target = _require_reference(objects, identifier, label)
    if target.kind not in kinds:
        expected = ", ".join(sorted(kinds))
        raise ValueError(f"invalid reference kind for {label}: {identifier} must be {expected}")


def _validate_references(document: dict[str, object], objects: Mapping[str, ContractObject]) -> None:
    capability = objects[_identifier(document["capabilityContract"].get("id"), "capabilityContract.id")]  # type: ignore[union-attr]
    for identifier in _id_refs(capability.value["participants"], "capabilityContract.participants"):
        _require_kind(objects, identifier, "capabilityContract.participants", {"context-role"})
    for field in ("inputs", "outputs"):
        for identifier in _id_refs(capability.value[field], f"capabilityContract.{field}"):
            _require_kind(objects, identifier, f"capabilityContract.{field}", {"semantic-value"})
    for constraint in capability.value["constraints"]:  # type: ignore[union-attr]
        identifier = _identifier(constraint.get("id"), "capabilityContract.constraints.id")  # type: ignore[union-attr]
        _require_kind(objects, identifier, "capabilityContract.constraints", {"constraint"})

    for item in objects.values():
        value = item.value
        if "supersedes" in value:
            for identifier in _id_refs(value["supersedes"], f"{item.kind}.supersedes"):
                _require_reference(objects, identifier, f"{item.kind}.supersedes")
        if item.kind == "context-role":
            _require_kind(objects, _identifier(value["context"], "contextRoles.context"), "contextRoles.context", {"context"})
        elif item.kind == "interaction":
            _require_kind(objects, _identifier(value["context"], "interactionContracts.context"), "interactionContracts.context", {"context"})
            for identifier in _id_refs(value["participants"], "interactionContracts.participants"):
                _require_kind(objects, identifier, "interactionContracts.participants", {"context-role"})
            for field, kind in (("accepts", "semantic-value"), ("produces", "semantic-value"), ("constraints", "constraint")):
                for identifier in _id_refs(value[field], f"interactionContracts.{field}"):
                    _require_kind(objects, identifier, f"interactionContracts.{field}", {kind})
            for expectation in _items(value["failureExpectations"], "interactionContracts.failureExpectations"):
                mapped = _mapping(expectation, "interactionContracts.failureExpectations item", {"reason", "preserves"})
                _require_kind(objects, _identifier(mapped["reason"], "failureExpectations.reason"), "failureExpectations.reason", {"failure-reason"})
                _strings(mapped["preserves"], "failureExpectations.preserves")
        elif item.kind == "verification":
            for identifier in _id_refs(value["traces"], "verificationMatrix.traces"):
                traced = _require_reference(objects, identifier, "verificationMatrix.traces")
                if traced.kind == "future-capability":
                    raise ValueError(f"future capability cannot be required by current verification: {identifier}")
        elif item.kind == "projection":
            for identifier in _id_refs(value["traces"], "engineeringProjections.traces"):
                _require_kind(objects, identifier, "engineeringProjections.traces", {"interaction"})
            for identifier in _id_refs(value["preservedInvariants"], "engineeringProjections.preservedInvariants"):
                _require_kind(objects, identifier, "engineeringProjections.preservedInvariants", {"constraint"})
        elif item.kind == "dependency":
            for identifier in _id_refs(value["requiredFor"], "dependsOn.requiredFor"):
                _require_kind(objects, identifier, "dependsOn.requiredFor", {"interaction"})


def _validate_stage_blockers(document: dict[str, object], objects: Mapping[str, ContractObject]) -> None:
    status = _meaningful(document["status"], "status")
    stage = _STAGE_ORDER["accepted-design"] if status in {"accepted-design", "active", "deprecated"} else 0
    if any(item.kind == "projection" for item in objects.values()):
        stage = max(stage, _STAGE_ORDER["engineering-projection"])
    if status in {"active", "deprecated"}:
        stage = _STAGE_ORDER["implementation"]

    for item in objects.values():
        if item.kind != "precondition":
            continue
        required_before = _meaningful(item.value["requiredBefore"], "preconditionsToResolve.requiredBefore")
        if required_before == "never" or item.value["status"] != "open":
            continue
        if _STAGE_ORDER[required_before] <= stage:
            raise ValueError(
                f"blocking precondition {item.id} must be resolved before {required_before}"
            )


def _validate_coverage(objects: Mapping[str, ContractObject]) -> None:
    traced = {
        identifier
        for item in objects.values()
        if item.kind == "verification"
        for identifier in _id_refs(item.value["traces"], "verificationMatrix.traces")
    }
    for item in objects.values():
        if item.kind == "constraint" and item.id not in traced:
            raise ValueError(f"core constraint has no verification trace: {item.id}")


def _build_document(path: Path, raw: dict[str, object]) -> ContractDocument:
    root = _object(raw, "contract document", "contract", _ROOT_FIELDS)
    if _meaningful(raw["status"], "status") not in _CONTRACT_STATUSES:
        raise ValueError(f"invalid contract status: {raw['status']}")
    _validate_date(raw["created"])
    for field in ("name", "note"):
        _meaningful(raw[field], field)

    objects: dict[str, ContractObject] = {}
    _add_object(objects, root)
    capability = _object(
        raw["capabilityContract"],
        "capabilityContract",
        "capability",
        {"id", "version", "purpose", "participants", "inputs", "outputs", "constraints"},
    )
    _meaningful(capability.value["purpose"], "capabilityContract.purpose")
    _id_refs(capability.value["participants"], "capabilityContract.participants")
    _id_refs(capability.value["inputs"], "capabilityContract.inputs")
    _id_refs(capability.value["outputs"], "capabilityContract.outputs")
    _add_object(objects, capability)
    for value in _items(capability.value["constraints"], "capabilityContract.constraints"):
        constraint = _object(value, "capabilityContract.constraints item", "constraint", {"id", "version", "rule"})
        _meaningful(constraint.value["rule"], "capabilityContract.constraints.rule")
        _add_object(objects, constraint)

    for value in _items(raw["semanticValueContracts"], "semanticValueContracts"):
        item = _object(value, "semanticValueContracts item", "semantic-value", {"id", "version", "name", "meanings"})
        _meaningful(item.value["name"], "semanticValueContracts.name")
        _strings(item.value["meanings"], "semanticValueContracts.meanings")
        _add_object(objects, item)
    for value in _items(raw["failureReasonContracts"], "failureReasonContracts"):
        item = _object(value, "failureReasonContracts item", "failure-reason", {"id", "version", "code", "meaning", "preserves"})
        _meaningful(item.value["code"], "failureReasonContracts.code")
        _meaningful(item.value["meaning"], "failureReasonContracts.meaning")
        _strings(item.value["preserves"], "failureReasonContracts.preserves")
        _add_object(objects, item)

    context = _object(raw["context"], "context", "context", {"id", "version", "name", "entryConditions", "completionConditions", "responsibilities"})
    _meaningful(context.value["name"], "context.name")
    for field in ("entryConditions", "completionConditions", "responsibilities"):
        _strings(context.value[field], f"context.{field}")
    _add_object(objects, context)
    for value in _items(raw["contextRoles"], "contextRoles"):
        item = _object(value, "contextRoles item", "context-role", {"id", "version", "context", "responsibility", "doesNotOwn"})
        _identifier(item.value["context"], "contextRoles.context")
        _meaningful(item.value["responsibility"], "contextRoles.responsibility")
        _strings(item.value["doesNotOwn"], "contextRoles.doesNotOwn")
        _add_object(objects, item)
    for value in _items(raw["interactionContracts"], "interactionContracts"):
        item = _object(value, "interactionContracts item", "interaction", {"id", "version", "context", "participants", "accepts", "produces", "constraints", "failureExpectations"})
        _identifier(item.value["context"], "interactionContracts.context")
        for field in ("participants", "accepts", "produces", "constraints"):
            _id_refs(item.value[field], f"interactionContracts.{field}")
        _items(item.value["failureExpectations"], "interactionContracts.failureExpectations")
        _add_object(objects, item)
    for value in _items(raw["verificationMatrix"], "verificationMatrix"):
        item = _object(value, "verificationMatrix item", "verification", {"id", "version", "traces", "given", "when", "then", "verifyBy"})
        _id_refs(item.value["traces"], "verificationMatrix.traces")
        for field in ("given", "when", "then"):
            _meaningful(item.value[field], f"verificationMatrix.{field}")
        for method in _items(item.value["verifyBy"], "verificationMatrix.verifyBy"):
            mapped = _mapping(method, "verificationMatrix.verifyBy item", {"type", "target"})
            _meaningful(mapped["type"], "verificationMatrix.verifyBy.type")
            _meaningful(mapped["target"], "verificationMatrix.verifyBy.target")
        _add_object(objects, item)
    for value in _items(raw["engineeringProjections"], "engineeringProjections", allow_empty=True):
        item = _object(value, "engineeringProjections item", "projection", {"id", "version", "traces", "authorityRepresentation", "derivedRepresentations", "transformationBoundary", "preservedInvariants"})
        _id_refs(item.value["traces"], "engineeringProjections.traces")
        _meaningful(item.value["authorityRepresentation"], "engineeringProjections.authorityRepresentation")
        _strings(item.value["derivedRepresentations"], "engineeringProjections.derivedRepresentations")
        _meaningful(item.value["transformationBoundary"], "engineeringProjections.transformationBoundary")
        _id_refs(item.value["preservedInvariants"], "engineeringProjections.preservedInvariants")
        _add_object(objects, item)
    for value in _items(raw["dependsOn"], "dependsOn", allow_empty=True):
        item = _object(value, "dependsOn item", "dependency", {"id", "version", "contract", "requiredFor", "ownerRepository"})
        _identifier(item.value["contract"], "dependsOn.contract")
        _id_refs(item.value["requiredFor"], "dependsOn.requiredFor")
        _meaningful(item.value["ownerRepository"], "dependsOn.ownerRepository")
        _add_object(objects, item)
    for value in _items(raw["preconditionsToResolve"], "preconditionsToResolve", allow_empty=True):
        item = _object(value, "preconditionsToResolve item", "precondition", {"id", "version", "question", "requiredBefore", "status", "decision"})
        _meaningful(item.value["question"], "preconditionsToResolve.question")
        required_before = _meaningful(item.value["requiredBefore"], "preconditionsToResolve.requiredBefore")
        if required_before not in _PRECONDITION_STAGES:
            raise ValueError(f"invalid preconditionsToResolve.requiredBefore: {required_before}")
        precondition_status = _meaningful(item.value["status"], "preconditionsToResolve.status")
        if precondition_status not in _PRECONDITION_STATUSES:
            raise ValueError(f"invalid preconditionsToResolve.status: {precondition_status}")
        _meaningful(item.value["decision"], "preconditionsToResolve.decision")
        _add_object(objects, item)
    for value in _items(raw["futureCapabilitiesOutOfScope"], "futureCapabilitiesOutOfScope", allow_empty=True):
        item = _object(value, "futureCapabilitiesOutOfScope item", "future-capability", {"id", "version", "capability", "reason"})
        _meaningful(item.value["capability"], "futureCapabilitiesOutOfScope.capability")
        _meaningful(item.value["reason"], "futureCapabilitiesOutOfScope.reason")
        _add_object(objects, item)
    for value in _items(raw["references"], "references", allow_empty=True):
        reference = _mapping(value, "references item", {"kind", "target", "note"})
        for field in ("kind", "target", "note"):
            _meaningful(reference[field], f"references.{field}")

    _validate_references(raw, objects)
    _validate_coverage(objects)
    _validate_stage_blockers(raw, objects)
    verifications = {item.id: item for item in objects.values() if item.kind == "verification"}
    return ContractDocument(path=path, raw=raw, objects_by_id=objects, verification_by_id=verifications)


def load_contract(repo_root: Path, file_path: Path) -> ContractDocument:
    document, _ = _load_contract_snapshot(repo_root, file_path)
    return document


def _load_contract_snapshot(repo_root: Path, file_path: Path) -> tuple[ContractDocument, str]:
    root = repo_root.resolve()
    contract_root, path = _contract_path(root, file_path)
    if not path.is_file():
        raise ValueError(f"missing contract file: {path}")
    text = _stable_contract_text(root, contract_root, path)
    document = _build_document(path, _parse_contract_yaml(text))
    return document, hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_contract_acceptance(repo_root: Path, issue: str, contract: ContractDocument, object_ids: Sequence[str]) -> Path:
    root = repo_root.resolve()
    current, digest = _load_contract_snapshot(root, contract.path)
    accepted_objects = tuple(_identifier(item, "accepted contract object") for item in object_ids)
    if not accepted_objects:
        raise ValueError("accepted contract objects must not be empty")
    if len(accepted_objects) != len(set(accepted_objects)):
        raise ValueError("accepted contract objects must not contain duplicates")
    for identifier in accepted_objects:
        if identifier not in current.objects_by_id:
            raise ValueError(f"accepted contract object does not exist: {identifier}")
    _, path = _contract_path(root, current.path)
    grant = approval.require_exact_remote(root, "contract-acceptance", path, issue)
    if grant.source != "local-review" or grant.approved_sha256 != digest:
        raise ValueError("contract acceptance review must bind the exact current contract bytes")
    return approval.record_contract_acceptance(
        root,
        grant,
        contract_id=current.objects_by_id[_identifier(current.raw["id"], "id")].id,
        contract_version=_semver(current.raw["version"], "version"),
        contract_sha256=digest,
        accepted_objects=accepted_objects,
    )


def validate_task_contract_acceptance(
    repo_root: Path,
    issue: str,
    contract_name: str,
    contract_file: str,
    approval_reference: str,
) -> None:
    root = repo_root.resolve()
    try:
        document, digest = _load_contract_snapshot(root, Path(contract_file))
    except ValueError as exc:
        raise ValueError(f"missing matching human contract acceptance: {exc}") from exc
    contract_id = _identifier(document.raw["id"], "id")
    contract_version = _semver(document.raw["version"], "version")
    if contract_name != f"{contract_id}@{contract_version}":
        raise ValueError("missing matching human contract acceptance: task-state Contract does not match contract file")
    _, path = _contract_path(root, document.path)
    issue_root = root / ".xflow" / "issues" / f"issue-{issue}"
    reference = Path(approval_reference)
    history_path = require_safe_repo_path(root, issue_root / reference, "Human Approval Ref")
    try:
        history_path.relative_to(issue_root / "approvals" / "history")
    except ValueError as exc:
        raise ValueError("missing matching human contract acceptance") from exc
    if not history_path.is_file():
        raise ValueError("missing matching human contract acceptance")
    try:
        record = approval.parse_contract_acceptance_history(root, history_path)
    except ValueError as exc:
        raise ValueError(f"missing matching human contract acceptance: {exc}") from exc
    bindings = approval.resolve_bindings(root)
    expected = {
        "repository": bindings.repository,
        "worktree": bindings.worktree,
        "branch": bindings.branch,
        "issue": issue,
        "approvalIssue": issue,
        "approvedFile": approval.display_path(root, path),
        "approvedSha256": digest,
        "contractId": contract_id,
        "contractVersion": contract_version,
        "contractSha256": digest,
        "semanticDecision": "accepted-design",
        "source": "local-review",
        "action": "contract-acceptance",
    }
    if any(record.get(name) != value for name, value in expected.items()):
        raise ValueError("missing matching human contract acceptance")
    accepted = record.get("acceptedObjects")
    if not isinstance(accepted, list) or not accepted or any(item not in document.objects_by_id for item in accepted):
        raise ValueError("missing matching human contract acceptance")
