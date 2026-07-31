from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from . import approval
from .classification import (
    _decode_classification_bytes,
    _load_yaml,
    validate_classification_document,
)
from .contracts import (
    ContractDocument,
    _build_document,
    _contract_path,
    _identifier as contract_identifier,
    _parse_contract_yaml,
    _validate_contract_schema,
)
from .local_artifacts import (
    StableFileSnapshot,
    capture_stable_file,
    contains_forbidden_remote_reference,
    revalidate_snapshots,
    safe_relative_reference,
)
from .paths import normalized_issue
from .project_config import require_safe_repo_path
from .task_state import TaskState, parse_task_state_text


SUPPORTED_VERSION = "0.1.0"
ENTRY_CONCLUSIONS = {"resolved", "reduced", "blocked"}
UI_VERIFICATION_TYPES = {"ui", "browser", "visual", "product-integration"}
UI_CLAIM_SCOPES = {"product-integration", "component-harness"}
UI_SURFACES = {"product", "component-harness"}
PLACEHOLDERS = {"-", "n/a", "na", "none", "placeholder", "tbd", "todo", "unknown", "待定", "待补充", "占位"}
CRITERION_RE = re.compile(r"^criterion-(\d{3})$")
SOURCE_CRITERION_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)(?:criterion\s+)?C-(\d{3})\s*[:：-]\s*\S"
)
IMAGE_SIGNATURES = (
    ("PNG", b"\x89PNG\r\n\x1a\n"),
    ("JPEG", b"\xff\xd8\xff"),
)


@dataclass(frozen=True)
class TraceEntry:
    id: str
    contract_objects: tuple[str, ...]
    verification: str
    acceptance_criterion: str
    conclusion: str
    tests: tuple[StableFileSnapshot, ...]
    before_evidence: tuple[StableFileSnapshot, ...]
    after_evidence: tuple[StableFileSnapshot, ...]


@dataclass(frozen=True)
class TraceabilityResult:
    path: Path
    issue_directory: Path
    entries: tuple[TraceEntry, ...]
    _snapshots: tuple[tuple[StableFileSnapshot, str], ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class _ClosureContext:
    root: Path
    issue_directory: Path
    required: bool
    task_state: TaskState | None
    contract: ContractDocument | None
    matrix_snapshot: StableFileSnapshot | None
    matrix_document: Mapping[str, object] | None
    snapshots: tuple[tuple[StableFileSnapshot, str], ...]


def _meaningful(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if value != text:
        raise ValueError(f"{label} must not contain leading or trailing whitespace")
    if not text or text.lower() in PLACEHOLDERS:
        raise ValueError(f"{label} must be meaningful")
    return text


def _identifier(value: object, label: str) -> str:
    return contract_identifier(value, label)


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


def _string_list(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not value and not allow_empty:
        raise ValueError(f"{label} must be a non-empty list")
    values = tuple(_meaningful(item, f"{label} item") for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _issue_paths(repo_root: Path, issue: str, matrix: Path | None) -> tuple[Path, Path, Path]:
    root = repo_root.resolve(strict=False)
    issue_directory = require_safe_repo_path(
        root,
        root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}",
        "traceability Issue directory",
    )
    expected = require_safe_repo_path(root, issue_directory / "traceability-matrix.yaml", "traceability matrix")
    requested = expected if matrix is None else (matrix if matrix.is_absolute() else root / matrix)
    requested = Path(os.path.abspath(requested))
    if os.path.normcase(os.path.normpath(str(requested))) != os.path.normcase(os.path.normpath(str(expected))):
        raise ValueError("traceability matrix must be .xflow/issues/issue-<id>/traceability-matrix.yaml for the requested Issue")
    return root, issue_directory, expected


def _parse_yaml_snapshot(snapshot: StableFileSnapshot, label: str) -> object:
    assert snapshot.content is not None
    try:
        text = _decode_classification_bytes(snapshot.content, snapshot.path)
        return _load_yaml(text)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", label)) from exc


def _load_matrix_snapshot(root: Path, issue_directory: Path, path: Path) -> tuple[StableFileSnapshot, dict[str, object]]:
    snapshot = capture_stable_file(root, path, issue_directory, "required traceability matrix")
    document = _mapping(
        _parse_yaml_snapshot(snapshot, "traceability"),
        "traceability matrix",
        {"version", "issue", "contract", "entries"},
    )
    version = _meaningful(document["version"], "traceability matrix version")
    if version != SUPPORTED_VERSION:
        raise ValueError(f"unsupported traceability matrix version: {version}")
    matrix_issue = _meaningful(document["issue"], "traceability matrix issue")
    expected_issue = issue_directory.name.removeprefix("issue-")
    if matrix_issue != expected_issue:
        raise ValueError(f"matrix Issue mismatch: expected {expected_issue}, found {matrix_issue}")
    return snapshot, document


def _decode_utf8(snapshot: StableFileSnapshot, label: str) -> str:
    assert snapshot.content is not None
    try:
        return snapshot.content.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8: {snapshot.path}") from exc


def _load_contract_snapshot(root: Path, file_path: str) -> tuple[StableFileSnapshot, ContractDocument]:
    contract_root, path = _contract_path(root, Path(file_path))
    snapshot = capture_stable_file(root, path, contract_root, "contract file")
    assert snapshot.content is not None
    raw = _parse_contract_yaml(_decode_utf8(snapshot, "contract file"))
    _validate_contract_schema(raw)
    return snapshot, _build_document(path, raw, snapshot.content)


def _optional_snapshot(root: Path, issue_directory: Path, name: str, label: str) -> StableFileSnapshot:
    return capture_stable_file(root, issue_directory / name, issue_directory, label, required=False)


def _load_context(
    repo_root: Path,
    issue: str,
    matrix: Path | None,
    *,
    require_task_state: bool,
    supplied_contract: ContractDocument | None,
) -> _ClosureContext:
    root, issue_directory, matrix_path = _issue_paths(repo_root, issue, matrix)
    tracked: list[tuple[StableFileSnapshot, str]] = []
    task_snapshot = _optional_snapshot(root, issue_directory, "task-state.md", "task-state")
    classification_snapshot = _optional_snapshot(root, issue_directory, "classification.yaml", "classification")
    tracked.extend(((task_snapshot, "task-state"), (classification_snapshot, "classification")))

    state: TaskState | None = None
    if task_snapshot.exists:
        state = parse_task_state_text(
            task_snapshot.path,
            _decode_utf8(task_snapshot, "task-state"),
            binding_mode="recorded",
            validate_acceptance=False,
        )
        if state.issue != normalized_issue(issue):
            raise ValueError(f"task-state Issue mismatch: expected {normalized_issue(issue)}, found {state.issue}")
    elif require_task_state:
        raise ValueError("contract trace requires the current Issue task-state.md")

    classification = None
    refs: tuple[str, ...] = ()
    if classification_snapshot.exists:
        classification = validate_classification_document(
            classification_snapshot.path,
            issue,
            _parse_yaml_snapshot(classification_snapshot, "classification"),
        )
        contract_search = classification.raw["contractSearch"]
        assert isinstance(contract_search, dict)
        raw_refs = contract_search["refs"]
        assert isinstance(raw_refs, list)
        refs = tuple(str(item) for item in raw_refs)

    required = state is not None or bool(refs)
    if not required:
        return _ClosureContext(root, issue_directory, False, None, None, None, None, tuple(tracked))
    if state is None:
        raise ValueError("contract-bearing Issue requires current task-state.md")
    if classification is not None:
        if classification.classification != state.classification:
            raise ValueError("classification does not match task-state Classification")
        if refs and state.contract_file not in refs:
            raise ValueError("classification contractSearch.refs must contain the task-state Contract File")

    contract_snapshot, contract = _load_contract_snapshot(root, state.contract_file)
    tracked.append((contract_snapshot, "contract file"))
    expected_contract = f"{contract.raw['id']}@{contract.raw['version']}"
    if state.contract != expected_contract:
        raise ValueError("task-state Contract does not match matrix contract")
    if supplied_contract is not None and (
        supplied_contract.path != contract.path or supplied_contract.sha256 != contract.sha256
    ):
        raise ValueError("supplied ContractDocument bytes/path do not match the task-state contract")
    if state.human_approval_ref != "none":
        approval_snapshot = capture_stable_file(
            root,
            issue_directory / state.human_approval_ref,
            issue_directory,
            "accepted contract reference",
        )
        tracked.append((approval_snapshot, "accepted contract reference"))
        try:
            record = approval.validate_contract_acceptance_history(root, approval_snapshot.path)
        except ValueError as exc:
            raise ValueError(f"missing matching human contract acceptance: {exc}") from exc
        expected_record = {
            "issue": state.issue,
            "approvalIssue": state.issue,
            "branch": state.branch,
            "approvedFile": approval.display_path(root, contract.path),
            "approvedSha256": contract.sha256,
            "contractId": contract.raw["id"],
            "contractVersion": contract.raw["version"],
            "contractSha256": contract.sha256,
            "semanticDecision": "accepted-design",
            "source": "local-review",
            "action": "contract-acceptance",
        }
        accepted = record.get("acceptedObjects")
        if (
            contract.raw["status"] != "accepted-design"
            or any(record.get(name) != expected for name, expected in expected_record.items())
            or not isinstance(accepted, list)
            or tuple(accepted) != approval.normalize_accepted_objects(accepted)
            or any(identifier not in contract.objects_by_id for identifier in accepted)
        ):
            raise ValueError("accepted contract reference does not match the current task-state contract bytes/path")

    matrix_snapshot, matrix_document = _load_matrix_snapshot(root, issue_directory, matrix_path)
    tracked.append((matrix_snapshot, "traceability matrix"))
    matrix_contract = _mapping(matrix_document["contract"], "matrix contract", {"id", "version", "file"})
    matrix_id = _identifier(matrix_contract["id"], "matrix contract.id")
    matrix_version = _meaningful(matrix_contract["version"], "matrix contract.version")
    matrix_file = _meaningful(matrix_contract["file"], "matrix contract.file")
    if matrix_id != contract.raw["id"] or matrix_version != contract.raw["version"] or matrix_file != state.contract_file:
        raise ValueError("task-state Contract does not match matrix contract id/version/file")
    expected_file = contract.path.relative_to(root).as_posix()
    if matrix_file != expected_file:
        raise ValueError(f"matrix contract.file does not match current ContractDocument path: {expected_file}")
    return _ClosureContext(
        root,
        issue_directory,
        True,
        state,
        contract,
        matrix_snapshot,
        matrix_document,
        tuple(tracked),
    )


def _issue_file_snapshot(
    root: Path,
    issue_directory: Path,
    value: object,
    label: str,
    *,
    evidence: bool = False,
) -> StableFileSnapshot:
    relative = safe_relative_reference(value, label)
    target = require_safe_repo_path(root, issue_directory / relative, label)
    relative_target = target.relative_to(issue_directory)
    if evidence and (not relative_target.parts or relative_target.parts[0] != "evidence"):
        raise ValueError(f"{label} must stay under the issue evidence directory")
    snapshot = capture_stable_file(root, target, issue_directory, label)
    if not snapshot.content:
        raise ValueError(f"{label} must be non-empty: {relative.as_posix()}")
    return snapshot


def _criterion_number(value: object) -> tuple[str, str]:
    criterion = _meaningful(value, "acceptanceCriterion")
    match = CRITERION_RE.fullmatch(criterion)
    if not match:
        raise ValueError("acceptanceCriterion must use criterion-NNN")
    return criterion, match.group(1)


def _blocker(value: object) -> dict[str, str]:
    raw = _mapping(value, "blocker", {"condition", "owner", "requiredAction"})
    parsed = {name: _meaningful(raw[name], f"blocker.{name}") for name in ("condition", "owner", "requiredAction")}
    if len(parsed["condition"]) < 8 or len(parsed["requiredAction"]) < 8 or len(parsed["owner"]) < 2:
        raise ValueError("blocker must name a meaningful external condition, owner, and required action")
    return parsed


def _verification_types(verification: object) -> tuple[str, ...]:
    value = verification.value  # type: ignore[attr-defined]
    methods = value["verifyBy"]
    assert isinstance(methods, list)
    return tuple(str(method["type"]).strip().lower() for method in methods if isinstance(method, dict))


def _is_supported_image(content: bytes) -> bool:
    if any(content.startswith(signature) for _, signature in IMAGE_SIGNATURES):
        return True
    return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"


def _verify_ui(
    raw: object,
    verification: object,
    after_by_relative: Mapping[str, StableFileSnapshot],
) -> tuple[StableFileSnapshot, StableFileSnapshot]:
    ui = _mapping(raw, "ui", {"claimScope", "surface", "targetUrl", "pageTitle", "modelIdentity", "screenshot", "structured"})
    claim_scope = _meaningful(ui["claimScope"], "ui.claimScope")
    surface = _meaningful(ui["surface"], "ui.surface")
    if claim_scope not in UI_CLAIM_SCOPES or surface not in UI_SURFACES:
        raise ValueError("ui claimScope/surface is invalid")
    verification_types = _verification_types(verification)
    if claim_scope == "product-integration":
        if "product-integration" not in verification_types:
            raise ValueError("product-integration claim requires contract verification type product-integration")
        if surface != "product":
            raise ValueError("product-integration requires ui.surface: product")
    elif surface != "component-harness":
        raise ValueError("component-harness cannot claim product integration")
    target_url = _meaningful(ui["targetUrl"], "ui.targetUrl")
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ui.targetUrl must be a complete HTTP(S) URL")
    page_title = _meaningful(ui["pageTitle"], "ui.pageTitle")
    model_identity = _meaningful(ui["modelIdentity"], "ui.modelIdentity")
    if contains_forbidden_remote_reference(page_title) or contains_forbidden_remote_reference(model_identity):
        raise ValueError("ui.targetUrl is the only URL-bearing identity field")
    screenshot_ref = safe_relative_reference(ui["screenshot"], "ui.screenshot").as_posix()
    structured_ref = safe_relative_reference(ui["structured"], "ui.structured").as_posix()
    if screenshot_ref == structured_ref:
        raise ValueError("UI screenshot and structured evidence must be distinct")
    if screenshot_ref not in after_by_relative or structured_ref not in after_by_relative:
        raise ValueError("UI artifacts must be included in evidence.after")
    screenshot = after_by_relative[screenshot_ref]
    structured = after_by_relative[structured_ref]
    if screenshot.object_identity == structured.object_identity:
        raise ValueError("UI screenshot and structured evidence must have distinct identities")
    if not _is_supported_image(screenshot.content or b""):
        raise ValueError("ui.screenshot must have a supported PNG/JPEG/WebP signature")
    try:
        structured_document = json.loads((structured.content or b"").decode("utf-8-sig", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ui.structured evidence must be a JSON mapping") from exc
    required_identity = {"surface", "targetUrl", "pageTitle", "modelIdentity"}
    state_fields = {"dom", "runtime", "model"}
    if not isinstance(structured_document, dict) or any(not isinstance(key, str) for key in structured_document):
        raise ValueError("ui.structured evidence must be a JSON mapping")
    if not required_identity.issubset(structured_document) or set(structured_document) - required_identity - state_fields:
        raise ValueError("ui.structured evidence has invalid identity/state fields")
    present_state = state_fields & set(structured_document)
    if not present_state or any(not isinstance(structured_document[name], dict) or not structured_document[name] for name in present_state):
        raise ValueError("ui.structured evidence must contain non-empty DOM/runtime/model state")
    expected_identity = {
        "surface": surface,
        "targetUrl": target_url,
        "pageTitle": page_title,
        "modelIdentity": model_identity,
    }
    if any(type(structured_document.get(name)) is not str or structured_document[name] != expected for name, expected in expected_identity.items()):
        raise ValueError("ui.structured identity fields must exactly match the matrix ui identity")
    return screenshot, structured


def _verify_entry(
    context: _ClosureContext,
    index: int,
    raw: object,
) -> TraceEntry:
    assert context.contract is not None
    entry = _mapping(
        raw,
        f"traceability entries[{index}]",
        {"id", "contractObjects", "verification", "acceptanceCriterion", "tests", "evidence", "conclusion"},
        {"ui", "blocker"},
    )
    entry_id = _identifier(entry["id"], "trace entry id")
    object_ids = _string_list(entry["contractObjects"], "contractObjects")
    for identifier in object_ids:
        item = context.contract.objects_by_id.get(_identifier(identifier, "contractObjects item"))
        if item is None:
            raise ValueError(f"contract object does not exist: {identifier}")
        if item.kind not in {"interaction", "constraint"}:
            raise ValueError(f"contractObjects must reference interaction or constraint objects: {identifier}")
    verification_id = _identifier(entry["verification"], "verification")
    verification = context.contract.verification_by_id.get(verification_id)
    if verification is None:
        raise ValueError(f"verification does not exist: {verification_id}")
    if set(object_ids) != set(verification.value["traces"]):
        raise ValueError(f"trace entry contractObjects does not match verification traces: {verification_id}")
    criterion, _ = _criterion_number(entry["acceptanceCriterion"])

    raw_tests = entry["tests"]
    if not isinstance(raw_tests, list) or not raw_tests:
        raise ValueError("tests must be a non-empty list")
    tests: list[StableFileSnapshot] = []
    selectors: set[str] = set()
    for test_index, raw_test in enumerate(raw_tests):
        test = _mapping(raw_test, f"tests[{test_index}]", {"path", "selector"})
        selector = _meaningful(test["selector"], "tests selector")
        if selector in selectors:
            raise ValueError("tests selectors must be unique within a trace entry")
        selectors.add(selector)
        tests.append(_issue_file_snapshot(context.root, context.issue_directory, test["path"], "tests path"))

    evidence = _mapping(entry["evidence"], "evidence", {"before"}, {"after"})
    before_values = _string_list(evidence["before"], "evidence.before")
    if "after" in evidence and not isinstance(evidence["after"], list):
        raise ValueError("evidence.after must be a list")
    after_values = _string_list(evidence.get("after", []), "evidence.after", allow_empty=True)
    parsed_blocker = _blocker(entry["blocker"]) if "blocker" in entry else None
    if "ui" in entry and not isinstance(entry["ui"], dict):
        raise ValueError("ui must be a mapping")
    conclusion = _meaningful(entry["conclusion"], "conclusion")
    if conclusion not in ENTRY_CONCLUSIONS:
        raise ValueError("conclusion must be resolved, reduced, or blocked")
    if conclusion in {"resolved", "reduced"} and not after_values:
        raise ValueError(f"{conclusion} evidence.after must be non-empty")
    if conclusion == "blocked" and parsed_blocker is None:
        raise ValueError("every blocked entry requires a structured meaningful external blocker")
    if conclusion != "blocked" and parsed_blocker is not None:
        raise ValueError("blocker is only allowed for blocked entries")

    before = tuple(
        _issue_file_snapshot(context.root, context.issue_directory, value, "evidence.before", evidence=True)
        for value in before_values
    )
    after = tuple(
        _issue_file_snapshot(context.root, context.issue_directory, value, "evidence.after", evidence=True)
        for value in after_values
    )
    after_by_relative = {
        snapshot.path.relative_to(context.issue_directory).as_posix(): snapshot for snapshot in after
    }
    verification_types = set(_verification_types(verification))
    ui_required = bool(verification_types & UI_VERIFICATION_TYPES)
    if ui_required and "ui" not in entry:
        raise ValueError(f"UI-oriented verification requires ui: {verification_id}")
    if not ui_required and "ui" in entry:
        raise ValueError(f"non-UI verification must omit ui: {verification_id}")
    if ui_required:
        _verify_ui(entry["ui"], verification, after_by_relative)
    return TraceEntry(entry_id, object_ids, verification_id, criterion, conclusion, tuple(tests), before, after)


def _acceptance_criteria(context: _ClosureContext) -> tuple[set[str], tuple[tuple[StableFileSnapshot, str], ...]]:
    snapshots: list[tuple[StableFileSnapshot, str]] = []
    numbers: set[str] = set()
    for name, label in (("gap-analysis.md", "gap analysis"), ("issue-draft.md", "issue draft")):
        snapshot = _optional_snapshot(context.root, context.issue_directory, name, label)
        snapshots.append((snapshot, label))
        if not snapshot.exists:
            continue
        text = _decode_utf8(snapshot, label)
        match = re.search(r"(?ms)^\s*## Acceptance Criteria\s*$\n(.*?)(?=^\s*##\s+|\Z)", text)
        if match:
            body = match.group(1)
            numbers.update(SOURCE_CRITERION_RE.findall(body))
            for ordered in re.finditer(r"(?m)^\s*(\d{1,3})[.)]\s+\S", body):
                numbers.add(ordered.group(1).zfill(3))
    if not numbers:
        raise ValueError("contract trace requires numbered Acceptance Criteria in gap-analysis.md or issue-draft.md")
    return numbers, tuple(snapshots)


def _verify_closure(context: _ClosureContext, entries: tuple[TraceEntry, ...]) -> tuple[tuple[StableFileSnapshot, str], ...]:
    assert context.contract is not None
    entry_ids = tuple(entry.id for entry in entries)
    verification_ids = tuple(entry.verification for entry in entries)
    criteria = tuple(entry.acceptance_criterion for entry in entries)
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("trace entry ids must be unique")
    if len(verification_ids) != len(set(verification_ids)):
        raise ValueError("verification binding must be unique")
    if len(criteria) != len(set(criteria)):
        raise ValueError("acceptanceCriterion binding must be unique")
    if set(verification_ids) != set(context.contract.verification_by_id):
        missing = sorted(set(context.contract.verification_by_id) - set(verification_ids))
        raise ValueError(f"active verification has no Issue acceptanceCriterion trace entry: {missing[0] if missing else '<duplicate>'}")
    traced_objects = {identifier for entry in entries for identifier in entry.contract_objects}
    missing_constraints = sorted(
        item.id for item in context.contract.objects_by_id.values() if item.kind == "constraint" and item.id not in traced_objects
    )
    if missing_constraints:
        raise ValueError(f"active core constraint has no trace entry: {missing_constraints[0]}")
    source_criteria, source_snapshots = _acceptance_criteria(context)
    for criterion in criteria:
        _, number = _criterion_number(criterion)
        if number not in source_criteria:
            raise ValueError(f"acceptance criterion does not exist in Issue Acceptance Criteria: {criterion}")

    before = tuple(snapshot for entry in entries for snapshot in entry.before_evidence)
    after = tuple(snapshot for entry in entries for snapshot in entry.after_evidence)
    all_evidence = before + after
    paths = tuple(snapshot.path for snapshot in all_evidence)
    if len(paths) != len(set(paths)):
        raise ValueError("before/after/UI evidence paths must be globally distinct")
    identities = tuple(snapshot.object_identity for snapshot in all_evidence)
    if any(identity is None or identity == (0, 0) for identity in identities):
        raise ValueError("evidence filesystem identity is unavailable")
    if len(identities) != len(set(identities)):
        raise ValueError("before/after/UI evidence identities must be globally distinct")
    before_digests = {snapshot.digest for snapshot in before}
    if any(snapshot.digest in before_digests for snapshot in after):
        raise ValueError("after evidence digest must differ from every before evidence digest")
    artifact_snapshots = tuple(
        (snapshot, label)
        for entry in entries
        for label, group in (
            ("test source", entry.tests),
            ("before evidence", entry.before_evidence),
            ("after evidence", entry.after_evidence),
        )
        for snapshot in group
    )
    return source_snapshots + artifact_snapshots


def _entries_from_context(context: _ClosureContext) -> tuple[TraceEntry, ...]:
    assert context.matrix_document is not None
    raw_entries = context.matrix_document["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("traceability entries must be a non-empty list")
    return tuple(_verify_entry(context, index, raw) for index, raw in enumerate(raw_entries))


def _revalidate(root: Path, snapshots: tuple[tuple[StableFileSnapshot, str], ...]) -> None:
    seen: dict[Path, StableFileSnapshot] = {}
    for snapshot, label in snapshots:
        previous = seen.get(snapshot.path)
        if previous is not None:
            if previous != snapshot:
                raise ValueError(f"{label} changed between closure snapshots: {snapshot.path}")
            continue
        seen[snapshot.path] = snapshot
        revalidate_snapshots(root, (snapshot,), label)


def check_traceability(
    repo_root: Path,
    issue: str,
    contract: ContractDocument,
    matrix: Path | None = None,
) -> TraceabilityResult:
    context = _load_context(repo_root, issue, matrix, require_task_state=True, supplied_contract=contract)
    assert context.required and context.matrix_snapshot is not None
    entries = _entries_from_context(context)
    artifact_snapshots = _verify_closure(context, entries)
    snapshots = context.snapshots + artifact_snapshots
    _revalidate(context.root, snapshots)
    return TraceabilityResult(context.matrix_snapshot.path, context.issue_directory, entries, snapshots)


def check_traceability_resolution(
    repo_root: Path,
    issue: str,
    conclusion: str,
    report_evidence: tuple[StableFileSnapshot, ...] | set[Path],
    *,
    report_criteria: tuple[str, ...] | None = None,
    report_snapshot: StableFileSnapshot | None = None,
    support_snapshots: tuple[StableFileSnapshot, ...] = (),
) -> None:
    context = _load_context(repo_root, issue, None, require_task_state=False, supplied_contract=None)
    report_snapshots: tuple[StableFileSnapshot, ...]
    if isinstance(report_evidence, set):
        report_snapshots = tuple(
            capture_stable_file(context.root, path, context.issue_directory, "resolution-report indexed evidence")
            for path in sorted(report_evidence)
        )
    else:
        report_snapshots = report_evidence
    base_snapshots = tuple((snapshot, "resolution-report indexed evidence") for snapshot in report_snapshots)
    if report_snapshot is not None:
        base_snapshots += ((report_snapshot, "resolution report"),)
    base_snapshots += tuple((snapshot, "resolution-report supporting artifact") for snapshot in support_snapshots)
    if not context.required:
        _revalidate(context.root, context.snapshots + base_snapshots)
        return
    entries = _entries_from_context(context)
    artifact_snapshots = _verify_closure(context, entries)
    conclusions = tuple(entry.conclusion for entry in entries)
    if conclusion == "resolved":
        if any(value != "resolved" for value in conclusions):
            raise ValueError("resolved resolution-report requires every trace entry to be resolved")
    elif conclusion == "reduced":
        if "reduced" not in conclusions or "blocked" in conclusions:
            raise ValueError("reduced resolution-report requires at least one reduced trace entry and no blocked entries")
    elif conclusion == "blocked":
        if "blocked" not in conclusions:
            raise ValueError("blocked resolution-report requires at least one blocked trace entry")
    else:
        raise ValueError("resolution-report conclusion must be resolved, reduced, or blocked")
    required_after = {snapshot.path for entry in entries for snapshot in entry.after_evidence}
    indexed = {snapshot.path for snapshot in report_snapshots}
    if not required_after.issubset(indexed):
        raise ValueError("resolution-report evidence must reference every trace after-evidence file, including UI artifacts")
    if report_criteria is not None:
        expected_numbers = {_criterion_number(entry.acceptance_criterion)[1] for entry in entries}
        if set(report_criteria) != expected_numbers:
            raise ValueError("resolution-report Criterion C-NNN bindings must exactly match matrix acceptanceCriterion bindings")
    snapshots = context.snapshots + artifact_snapshots + base_snapshots
    _revalidate(context.root, snapshots)
