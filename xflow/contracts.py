from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Mapping, Sequence

from . import approval
from .classification import _decode_classification_bytes, _load_yaml, _read_stable_bytes
from .local_artifacts import MAX_TEXT_ARTIFACT_BYTES
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
_ASCII_CASEFOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
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
    raw_bytes: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()


@dataclass(frozen=True)
class ContractDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    required_bump: Literal["none", "patch", "minor", "major", "human-review"]
    actual_bump: Literal["none", "patch", "minor", "major", "invalid"]
    review_impacts: tuple[str, ...]


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
    if value != text:
        raise ValueError(f"{label} must not contain leading or trailing whitespace")
    if not text or text.translate(_ASCII_CASEFOLD) in _PLACEHOLDERS:
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


def _object(
    value: object,
    label: str,
    kind: str,
    required: set[str],
    *,
    allow_supersedes: bool = True,
) -> ContractObject:
    mapped = _mapping(value, label, required, _OPTIONAL_OBJECT_FIELDS if allow_supersedes else set())
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
    text = _meaningful(value, "created")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("created must use YYYY-MM-DD")
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


def _stable_contract_bytes(repo_root: Path, contract_root: Path, path: Path) -> bytes:
    try:
        return _read_stable_bytes(repo_root, path, contract_root, max_bytes=MAX_TEXT_ARTIFACT_BYTES)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "contract")) from exc


def _parse_contract_yaml(text: str) -> dict[str, object]:
    try:
        raw = _load_yaml(text)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "contract")) from exc
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("contract document must be a mapping")
    document = dict(raw)
    if isinstance(document.get("created"), date):
        document["created"] = document["created"].isoformat()
    return document


def _validate_contract_schema(document: dict[str, object]) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError(
            "contract checks require jsonschema; run: python -m pip install -r requirements.txt"
        ) from exc

    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "capability-contract.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8", errors="strict"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise ValueError(f"invalid canonical contract schema: {schema_path}: {exc}") from exc
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(
        validator.iter_errors(document),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ValueError(f"contract schema validation failed at {location}: {error.message}")


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
                if identifier == item.id:
                    raise ValueError(f"{item.kind} must not supersede itself: {identifier}")
                target = objects.get(identifier)
                if target is None:
                    # An absent ID is a declared historical predecessor. Task 8 validates it
                    # against the previous contract document during evolution checks.
                    continue
                if (item.kind == "future-capability") != (target.kind == "future-capability"):
                    raise ValueError(
                        f"{item.kind}.supersedes must not cross current and future objects: {identifier}"
                    )
                if target.kind != item.kind:
                    raise ValueError(
                        f"{item.kind}.supersedes target must have the same kind: {identifier}"
                    )
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


def _build_document(path: Path, raw: dict[str, object], raw_bytes: bytes) -> ContractDocument:
    root = _object(raw, "contract document", "contract", _ROOT_FIELDS, allow_supersedes=False)
    status = _meaningful(raw["status"], "status")
    if status not in _CONTRACT_STATUSES:
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
    return ContractDocument(
        path=path,
        raw=raw,
        objects_by_id=objects,
        verification_by_id=verifications,
        raw_bytes=raw_bytes,
    )


_ROOT_SEMANTIC_FIELDS = {"id", "name", "status", "created", "note", "references"}
_BUMP_ORDER = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_SET_LIKE_FIELDS = {
    "participants",
    "inputs",
    "outputs",
    "meanings",
    "preserves",
    "entryConditions",
    "completionConditions",
    "responsibilities",
    "doesNotOwn",
    "accepts",
    "produces",
    "traces",
    "derivedRepresentations",
    "preservedInvariants",
    "requiredFor",
    "supersedes",
}


def _objects_by_kind_and_id(document: ContractDocument) -> dict[tuple[str, str], ContractObject]:
    return {(item.kind, item.id): item for item in document.objects_by_id.values()}


def _canonical_record(record: object) -> tuple[tuple[str, object], ...]:
    if not isinstance(record, dict):
        raise ValueError("canonical contract record must be a mapping")
    return tuple(
        sorted(
            (
                field,
                tuple(sorted(value)) if field == "preserves" and isinstance(value, list) else value,
            )
            for field, value in record.items()
            if field != "version"
        )
    )


def _canonical_field(item: ContractObject, field: str, value: object) -> object:
    if item.kind == "capability" and field == "constraints":
        return tuple(sorted(constraint["id"] for constraint in value))  # type: ignore[union-attr]
    if field in {"failureExpectations", "verifyBy", "references"}:
        return tuple(sorted(_canonical_record(record) for record in value))  # type: ignore[union-attr]
    if field == "constraints" or field in _SET_LIKE_FIELDS:
        return tuple(sorted(value))  # type: ignore[arg-type]
    return value


def _semantic_object_value(item: ContractObject) -> dict[str, object]:
    fields = _ROOT_SEMANTIC_FIELDS if item.kind == "contract" else set(item.value) - {"version"}
    return {
        field: _canonical_field(item, field, item.value[field])
        for field in fields
    }


def _replacement_value(item: ContractObject) -> dict[str, object]:
    return {
        field: value
        for field, value in _semantic_object_value(item).items()
        if field not in {"id", "supersedes"}
    }


def _semver_parts(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _actual_bump(old: str, new: str) -> Literal["none", "patch", "minor", "major", "invalid"]:
    before = _semver_parts(old)
    after = _semver_parts(new)
    if after == before:
        return "none"
    if after[0] == before[0] and after[1] == before[1] and after[2] == before[2] + 1:
        return "patch"
    if after[0] == before[0] and after[1] == before[1] + 1 and after[2] == 0:
        return "minor"
    if after[0] == before[0] + 1 and after[1:] == (0, 0):
        return "major"
    return "invalid"


def _changed_fields(old: ContractObject, new: ContractObject) -> set[str]:
    before = _semantic_object_value(old)
    after = _semantic_object_value(new)
    return {field for field in set(before) | set(after) if before.get(field) != after.get(field)}


def _is_optional_addition(
    item: ContractObject,
    document: ContractDocument,
    added_ids: set[str],
) -> bool | None:
    if item.kind in {"future-capability", "verification", "projection"}:
        return True
    if item.kind == "precondition":
        return item.value["requiredBefore"] == "never"
    if item.kind == "interaction":
        newly_owned = {
            identifier
            for identifier in _referenced_object_ids(item)
            if identifier in added_ids
        }
        if any(
            document.objects_by_id[identifier].kind not in {"semantic-value", "failure-reason"}
            for identifier in newly_owned
        ):
            return None
        return True
    if item.kind in {"semantic-value", "failure-reason"}:
        referrers = [
            candidate
            for candidate in document.objects_by_id.values()
            if item.id in _referenced_object_ids(candidate)
        ]
        return all(candidate.kind == "interaction" and candidate.id in added_ids for candidate in referrers)
    if item.kind == "context-role":
        referrers = [
            candidate
            for candidate in document.objects_by_id.values()
            if item.id in _referenced_object_ids(candidate)
        ]
        return True if not referrers else None
    if item.kind == "dependency":
        required_for = set(item.value["requiredFor"])  # type: ignore[arg-type]
        return None if required_for <= added_ids else False
    return False


def _is_major_change(item: ContractObject, fields: set[str]) -> bool:
    if fields <= {"name", "note"}:
        return False
    if item.kind in {"constraint", "capability", "failure-reason", "context-role", "interaction"}:
        return True
    if item.kind == "semantic-value":
        return "meanings" in fields
    if item.kind == "context":
        return bool(fields & {"entryConditions", "completionConditions", "responsibilities"})
    return False


def _object_mechanical_floor(item: ContractObject, fields: set[str]) -> Literal["none", "patch", "major"]:
    if not fields:
        return "none"
    if fields <= {"name", "note"}:
        return "patch"
    if _is_major_change(item, fields):
        return "major"
    return "patch"


def _ambiguous_change_impact(item: ContractObject, fields: set[str]) -> str:
    if item.kind == "contract" and fields == {"references"}:
        return "changed contract references require human review"
    return f"changed {item.kind} requires human review: {item.id}"


def _referenced_object_ids(item: ContractObject) -> tuple[str, ...]:
    value = item.value
    if item.kind == "capability":
        return tuple(value["participants"]) + tuple(value["inputs"]) + tuple(value["outputs"]) + tuple(  # type: ignore[arg-type]
            constraint["id"] for constraint in value["constraints"]  # type: ignore[union-attr]
        )
    if item.kind == "context-role":
        return (value["context"],)  # type: ignore[return-value]
    if item.kind == "interaction":
        return (
            tuple(value["participants"])  # type: ignore[arg-type]
            + tuple(value["accepts"])  # type: ignore[arg-type]
            + tuple(value["produces"])  # type: ignore[arg-type]
            + tuple(value["constraints"])  # type: ignore[arg-type]
            + (value["context"],)  # type: ignore[operator]
            + tuple(expectation["reason"] for expectation in value["failureExpectations"])  # type: ignore[union-attr]
        )
    if item.kind == "verification":
        return tuple(value["traces"])  # type: ignore[arg-type]
    if item.kind == "projection":
        return tuple(value["traces"]) + tuple(value["preservedInvariants"])  # type: ignore[arg-type]
    if item.kind == "dependency":
        return tuple(value["requiredFor"])  # type: ignore[arg-type]
    return ()


def _impacted_ids(old: ContractDocument, new: ContractDocument, ids: set[str], kind: str) -> tuple[str, ...]:
    affected = set(ids)
    objects = tuple(old.objects_by_id.values()) + tuple(new.objects_by_id.values())
    for item in objects:
        if item.kind == "dependency" and item.id in affected:
            affected.update(item.value["requiredFor"])  # type: ignore[arg-type]
    if any(item.kind == "capability" and item.id in affected for item in objects):
        affected.update(item.id for item in objects if item.kind == "interaction")
    changed = True
    while changed:
        changed = False
        for item in objects:
            if item.id in affected or not (set(_referenced_object_ids(item)) & affected):
                continue
            affected.add(item.id)
            changed = True
    return tuple(sorted({item.id for item in objects if item.kind == kind and item.id in affected}))


def _display_ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def diff_contracts(old: ContractDocument, new: ContractDocument) -> ContractDiff:
    """Compare two fully validated canonical contracts by stable object kind and ID."""
    before = _objects_by_kind_and_id(old)
    after = _objects_by_kind_and_id(new)
    old_keys = set(before)
    new_keys = set(after)
    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    shared_keys = old_keys & new_keys

    added = tuple(sorted(identifier for _, identifier in added_keys))
    removed = tuple(sorted(identifier for _, identifier in removed_keys))
    changed = tuple(sorted(after[key].id for key in shared_keys if _semantic_object_value(before[key]) != _semantic_object_value(after[key])))
    unchanged = tuple(sorted(after[key].id for key in shared_keys if _semantic_object_value(before[key]) == _semantic_object_value(after[key])))

    mechanical_floor = "none"
    ambiguous: list[str] = []
    errors: list[str] = []
    under_bumped: list[str] = []
    object_version_errors: list[str] = []
    old_by_id = old.objects_by_id
    new_by_id = new.objects_by_id
    for identifier in sorted(old_by_id.keys() & new_by_id.keys()):
        previous_kind = old_by_id[identifier].kind
        current_kind = new_by_id[identifier].kind
        if previous_kind != current_kind:
            errors.append(f"stable-ID kind change: {identifier} {previous_kind} -> {current_kind}")

    added_ids = set(added)
    retired_historical_ids = {
        predecessor
        for item in old_by_id.values()
        for predecessor in item.value.get("supersedes", ())
        if predecessor not in old_by_id
    }
    for identifier in sorted(retired_historical_ids & added_ids):
        errors.append(f"stable-ID resurrection: {identifier}")

    for key in shared_keys:
        previous = before[key]
        current = after[key]
        fields = _changed_fields(previous, current)
        if not fields:
            if current.kind != "contract" and current.version != previous.version:
                object_version_errors.append(
                    f"unchanged object version changed: {current.id} {previous.version} -> {current.version}"
                )
            continue
        object_floor = _object_mechanical_floor(current, fields)
        mechanical_floor = max((mechanical_floor, object_floor), key=lambda value: _BUMP_ORDER[value])
        if current.kind != "contract":
            object_actual = _actual_bump(previous.version, current.version)
            if object_actual == "invalid" or _BUMP_ORDER[object_actual] < _BUMP_ORDER[object_floor]:
                under_bumped.append(current.id)
                object_version_errors.append(
                    f"under-bumped object version: {current.id} required {object_floor}, actual {object_actual}"
                )
        if not _is_major_change(current, fields) and not fields <= {"name", "note"}:
            ambiguous.append(_ambiguous_change_impact(current, fields))

    for key in added_keys:
        item = after[key]
        optional = _is_optional_addition(item, new, added_ids)
        if optional is True:
            mechanical_floor = max((mechanical_floor, "minor"), key=lambda value: _BUMP_ORDER[value])
        elif optional is False:
            mechanical_floor = "major"
        else:
            mechanical_floor = max((mechanical_floor, "minor"), key=lambda value: _BUMP_ORDER[value])
            ambiguous.append(f"added {item.kind} optionality requires human review: {item.id}")
    for key in removed_keys:
        item = before[key]
        if item.kind == "future-capability":
            mechanical_floor = max((mechanical_floor, "patch"), key=lambda value: _BUMP_ORDER[value])
            ambiguous.append(f"removed future-capability requires human review: {item.id}")
        else:
            mechanical_floor = "major"

    removed_by_id = {item.id: item for key, item in before.items() if key in removed_keys}
    transition_supersedes: dict[str, set[str]] = {}
    lineage_keys = set(added_keys) | shared_keys
    for key in lineage_keys:
        item = after[key]
        old_edges = set(before[key].value.get("supersedes", ())) if key in shared_keys else set()
        new_edges = set(item.value.get("supersedes", ()))
        for predecessor in sorted(old_edges - new_edges):
            errors.append(f"removed historical supersedes edge: {item.id} -> {predecessor}")
        for predecessor in new_edges:
            if predecessor in removed_by_id:
                transition_supersedes.setdefault(item.id, set()).add(predecessor)
        for predecessor in sorted(new_edges - old_edges):
            historical = old_by_id.get(predecessor)
            if historical is None:
                errors.append(f"invalid historical supersedes for {item.id}: {predecessor}")
                continue
            if predecessor == item.id:
                errors.append(f"invalid historical supersedes self-reference for {item.id}")
                continue
            if historical.kind != item.kind:
                errors.append(f"invalid historical supersedes kind for {item.id}: {predecessor}")
                continue
            if (item.kind == "future-capability") != (historical.kind == "future-capability"):
                errors.append(f"invalid historical supersedes current/future crossing for {item.id}: {predecessor}")
                continue
            if predecessor not in removed_by_id:
                errors.append(f"invalid historical supersedes target is not removed for {item.id}: {predecessor}")
                continue

    for successor, predecessors in transition_supersedes.items():
        if len(predecessors) > 1:
            ambiguous.append(f"one new object supersedes multiple old objects: {successor}")
    successors_by_predecessor: dict[str, set[str]] = {}
    for successor, predecessors in transition_supersedes.items():
        for predecessor in predecessors:
            successors_by_predecessor.setdefault(predecessor, set()).add(successor)
    for predecessor, successors in successors_by_predecessor.items():
        if len(successors) > 1:
            ambiguous.append(f"one old object is superseded by multiple new objects: {predecessor}")

    replacement_kinds = {kind for kind, _ in added_keys} & {kind for kind, _ in removed_keys}
    for kind in sorted(replacement_kinds):
        kind_added_ids = {identifier for candidate_kind, identifier in added_keys if candidate_kind == kind}
        removed_ids = {identifier for candidate_kind, identifier in removed_keys if candidate_kind == kind}
        mapped = {
            successor: transition_supersedes.get(successor, set()) & removed_ids
            for successor in kind_added_ids
        }
        mapped_back = {
            predecessor: {successor for successor, predecessors in mapped.items() if predecessor in predecessors}
            for predecessor in removed_ids
        }
        exact_candidates = {
            predecessor: {
                successor
                for successor in kind_added_ids
                if _replacement_value(before[(kind, predecessor)]) == _replacement_value(after[(kind, successor)])
            }
            for predecessor in removed_ids
        }
        exact_candidates_back = {
            successor: {
                predecessor
                for predecessor, successors in exact_candidates.items()
                if successor in successors
            }
            for successor in kind_added_ids
        }
        for predecessor, successors in exact_candidates.items():
            if len(successors) != 1:
                continue
            successor = next(iter(successors))
            if len(exact_candidates_back[successor]) != 1:
                continue
            if predecessor not in transition_supersedes.get(successor, set()):
                errors.append(
                    f"exact stable-ID replacement lacks one-to-one supersedes: {predecessor} -> {successor}"
                )
        if any(len(predecessors) != 1 for predecessors in mapped.values()) or any(
            len(successors) != 1 for successors in mapped_back.values()
        ):
            ambiguous.append(
                f"unmapped same-kind replacements: {kind} removed {_display_ids(tuple(sorted(removed_ids)))}; "
                f"added {_display_ids(tuple(sorted(kind_added_ids)))}"
            )

    if ambiguous or errors:
        required: Literal["none", "patch", "minor", "major", "human-review"] = "human-review"
    else:
        required = mechanical_floor  # type: ignore[assignment]
    old_root = old.objects_by_id[_identifier(old.raw["id"], "id")]
    new_root = new.objects_by_id[_identifier(new.raw["id"], "id")]
    actual = _actual_bump(old_root.version, new_root.version)

    impacts: list[str] = [
        f"mechanical floor: {mechanical_floor}",
        f"affected verification IDs: {_display_ids(_impacted_ids(old, new, set(added) | set(removed) | set(changed), 'verification'))}",
        f"affected projection IDs: {_display_ids(_impacted_ids(old, new, set(added) | set(removed) | set(changed), 'projection'))}",
        f"removed IDs: {_display_ids(removed)}",
        f"under-bumped objects: {_display_ids(tuple(sorted(under_bumped)))}",
    ]
    if actual == "invalid":
        impacts.append(
            f"[ERROR] invalid contract root version relation: {old_root.version} -> {new_root.version}"
        )
    elif mechanical_floor == "none" and actual != "none":
        impacts.append(f"[ERROR] unchanged contract root version changed: {old_root.version} -> {new_root.version}")
    elif _BUMP_ORDER[actual] < _BUMP_ORDER[mechanical_floor]:
        impacts.append(f"[ERROR] under-bumped contract root: required {mechanical_floor}, actual {actual}")
    impacts.extend(f"[ERROR] {message}" for message in sorted(object_version_errors))
    impacts.extend(f"[ERROR] {message}" for message in sorted(set(errors)))
    impacts.extend(f"[WARN] {message}" for message in sorted(set(ambiguous)))
    return ContractDiff(added, removed, changed, unchanged, required, actual, tuple(impacts))


def render_contract_diff(diff: ContractDiff) -> str:
    lines = (
        f"added: {_display_ids(diff.added)}",
        f"removed: {_display_ids(diff.removed)}",
        f"changed: {_display_ids(diff.changed)}",
        f"unchanged: {_display_ids(diff.unchanged)}",
        f"required bump: {diff.required_bump}",
        f"actual bump: {diff.actual_bump}",
        *diff.review_impacts,
    )
    return "\n".join(lines)


def contract_diff_exit_code(diff: ContractDiff) -> int:
    return 1 if any(impact.startswith("[ERROR]") for impact in diff.review_impacts) else 0


def load_contract(repo_root: Path, file_path: Path) -> ContractDocument:
    document, _ = _load_contract_snapshot(repo_root, file_path)
    return document


def _load_contract_snapshot(repo_root: Path, file_path: Path) -> tuple[ContractDocument, str]:
    root = repo_root.resolve()
    contract_root, path = _contract_path(root, file_path)
    if not path.is_file():
        raise ValueError(f"missing contract file: {path}")
    raw_bytes = _stable_contract_bytes(root, contract_root, path)
    try:
        text = _decode_classification_bytes(raw_bytes, path)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "contract")) from exc
    raw = _parse_contract_yaml(text)
    _validate_contract_schema(raw)
    document = _build_document(path, raw, raw_bytes)
    return document, document.sha256


def validate_contract_acceptance(repo_root: Path, issue: str, contract: ContractDocument, object_ids: Sequence[str]) -> Path:
    root = repo_root.resolve()
    current, digest = _load_contract_snapshot(root, contract.path)
    accepted_objects = approval.normalize_accepted_objects(object_ids)
    for identifier in accepted_objects:
        if identifier not in current.objects_by_id:
            raise ValueError(f"accepted contract object does not exist: {identifier}")
    if current.raw["status"] != "accepted-design":
        raise ValueError("candidate contract status must be accepted-design")
    _, path = _contract_path(root, current.path)
    return approval.consume_contract_acceptance(
        root,
        issue,
        path,
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
    semantic_phase: str,
    *,
    binding_mode: str = "current",
    recorded_branch: str | None = None,
) -> None:
    root = repo_root.resolve()
    try:
        document, digest = _load_contract_snapshot(root, Path(contract_file))
    except ValueError as exc:
        raise ValueError(f"missing matching human contract acceptance: {exc}") from exc
    contract_id = _identifier(document.raw["id"], "id")
    contract_version = _semver(document.raw["version"], "version")
    if document.raw["status"] != "accepted-design":
        raise ValueError(
            "missing matching human contract acceptance: contract status is incompatible "
            f"with task semantic phase {semantic_phase}"
        )
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
        record = approval.validate_contract_acceptance_history(root, history_path)
    except ValueError as exc:
        raise ValueError(f"missing matching human contract acceptance: {exc}") from exc
    bindings = approval.resolve_bindings(root)
    expected = {
        "repository": bindings.repository,
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
    if binding_mode == "current":
        expected.update({"worktree": bindings.worktree, "branch": bindings.branch})
    elif binding_mode == "recorded":
        if not recorded_branch:
            raise ValueError("missing matching human contract acceptance: missing recorded task branch")
        expected["branch"] = recorded_branch
    else:
        raise ValueError(f"invalid contract acceptance binding mode: {binding_mode}")
    if any(record.get(name) != value for name, value in expected.items()):
        raise ValueError("missing matching human contract acceptance")
    accepted = record.get("acceptedObjects")
    if (
        not isinstance(accepted, list)
        or tuple(accepted) != approval.normalize_accepted_objects(accepted)
        or any(item not in document.objects_by_id for item in accepted)
    ):
        raise ValueError("missing matching human contract acceptance")
