from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping
from urllib.parse import urlparse

from .classification import _decode_classification_bytes, _load_yaml, _read_stable_bytes
from .contracts import ContractDocument, ContractObject, load_contract
from .paths import normalized_issue
from .project_config import require_safe_repo_path


SUPPORTED_VERSION = "0.1.0"
ENTRY_CONCLUSIONS = {"resolved", "reduced", "blocked"}
UI_CLAIM_SCOPES = {"product-integration", "component-harness"}
UI_SURFACES = {"product", "component-harness"}
PLACEHOLDERS = {"-", "n/a", "na", "none", "placeholder", "tbd", "todo", "unknown", "待定", "待补充", "占位"}
REMOTE_OR_OBJECT_STORE_RE = re.compile(
    r"(?i)(https?://|oss://|cos://|s3://|gs://|azure://|aliyuncs\.com|myqcloud\.com|qcloudcos|cos\.|amazonaws\.com|storage\.googleapis\.com|blob\.core\.windows\.net)"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class TraceEntry:
    id: str
    contract_objects: tuple[str, ...]
    verification: str
    acceptance_criterion: str
    conclusion: str
    after_evidence: tuple[Path, ...]


@dataclass(frozen=True)
class TraceabilityResult:
    path: Path
    issue_directory: Path
    entries: tuple[TraceEntry, ...]


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
    identifier = _meaningful(value, label)
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"{label} must be a stable identifier")
    return identifier


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


def _unique_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    values = tuple(_meaningful(item, f"{label} item") for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _safe_relative_path(value: object, label: str) -> Path:
    raw = _meaningful(value, label)
    if REMOTE_OR_OBJECT_STORE_RE.search(raw) or re.match(r"(?i)^[a-z][a-z0-9+.-]*://", raw):
        raise ValueError(f"{label} must not use URL/COS/OSS/object-storage paths")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root or raw.startswith(("\\\\", "/")):
        raise ValueError(f"{label} must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError(f"{label} must not contain '..'")
    if raw in {".", ""} or any(part in {"", "."} for part in posix.parts):
        raise ValueError(f"{label} must name a file")
    return Path(raw)


def _safe_issue_file(repo_root: Path, issue_directory: Path, value: object, label: str, *, evidence: bool = False) -> Path:
    relative = _safe_relative_path(value, label)
    target = require_safe_repo_path(repo_root, issue_directory / relative, label)
    try:
        target.relative_to(issue_directory)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the issue directory") from exc
    if evidence:
        relative_target = target.relative_to(issue_directory)
        if not relative_target.parts or relative_target.parts[0] != "evidence":
            raise ValueError(f"{label} must stay under the issue evidence directory")
    try:
        content = _read_stable_bytes(repo_root, target, issue_directory)
    except ValueError as exc:
        message = str(exc).replace("classification file", "traceability referenced file")
        if message.startswith("missing traceability referenced file:"):
            message = message.replace("missing traceability referenced file:", "referenced file does not exist:", 1)
        raise ValueError(message) from exc
    if not content:
        raise ValueError(f"{label} must be non-empty: {relative.as_posix()}")
    return target


def _safe_contract_file(repo_root: Path, value: object) -> Path:
    relative = _safe_relative_path(value, "matrix contract.file")
    return require_safe_repo_path(repo_root, repo_root / relative, "matrix contract.file")


def _issue_paths(repo_root: Path, issue: str, matrix: Path | None) -> tuple[Path, Path, Path]:
    root = repo_root.resolve(strict=False)
    issue_directory = root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"
    issue_directory = require_safe_repo_path(root, issue_directory, "traceability issue directory")
    expected = require_safe_repo_path(root, issue_directory / "traceability-matrix.yaml", "traceability matrix")
    requested = expected if matrix is None else (matrix if matrix.is_absolute() else root / matrix)
    requested = Path(os.path.abspath(requested))
    if os.path.normcase(os.path.normpath(str(requested))) != os.path.normcase(os.path.normpath(str(expected))):
        raise ValueError("traceability matrix must be .xflow/issues/issue-<id>/traceability-matrix.yaml for the requested Issue")
    return root, issue_directory, expected


def _load_matrix(repo_root: Path, issue: str, matrix: Path | None) -> tuple[Path, Path, dict[str, object]]:
    root, issue_directory, path = _issue_paths(repo_root, issue, matrix)
    try:
        raw_bytes = _read_stable_bytes(root, path, issue_directory)
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification file", "traceability matrix")) from exc
    try:
        raw = _load_yaml(_decode_classification_bytes(raw_bytes, path))
    except ValueError as exc:
        raise ValueError(str(exc).replace("classification", "traceability")) from exc
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("traceability matrix must be a mapping")
    document = _mapping(raw, "traceability matrix", {"version", "issue", "contract", "entries"})
    if _meaningful(document["version"], "traceability matrix version") != SUPPORTED_VERSION:
        raise ValueError(f"unsupported traceability matrix version: {document['version']}")
    matrix_issue = _meaningful(document["issue"], "traceability matrix issue")
    if normalized_issue(matrix_issue) != normalized_issue(issue) or matrix_issue != normalized_issue(issue):
        raise ValueError(f"matrix Issue mismatch: expected {normalized_issue(issue)}, found {matrix_issue}")
    return issue_directory, path, document


def _verify_contract(repo_root: Path, contract: ContractDocument, raw: object) -> ContractDocument:
    matrix_contract = _mapping(raw, "matrix contract", {"id", "version", "file"})
    path = _safe_contract_file(repo_root, matrix_contract["file"])
    current = load_contract(repo_root, path)
    if contract.path != current.path or contract.sha256 != current.sha256:
        raise ValueError("current ContractDocument bytes/path no longer match the matrix contract file")
    expected_file = current.path.relative_to(repo_root.resolve(strict=False)).as_posix()
    if _meaningful(matrix_contract["file"], "matrix contract.file") != expected_file:
        raise ValueError(f"matrix contract.file does not match current ContractDocument path: {expected_file}")
    if _meaningful(matrix_contract["id"], "matrix contract.id") != current.raw["id"]:
        raise ValueError("matrix contract.id does not match current ContractDocument")
    if _meaningful(matrix_contract["version"], "matrix contract.version") != current.raw["version"]:
        raise ValueError("matrix contract.version does not match current ContractDocument")
    return current


def _verify_entry(
    repo_root: Path,
    issue_directory: Path,
    index: int,
    raw: object,
    contract: ContractDocument,
) -> TraceEntry:
    entry = _mapping(raw, f"traceability entries[{index}]", {"id", "contractObjects", "verification", "acceptanceCriterion", "tests", "evidence", "conclusion"}, {"ui", "blocker"})
    entry_id = _identifier(entry["id"], "trace entry id")
    object_ids = _unique_strings(entry["contractObjects"], "contractObjects")
    for identifier in object_ids:
        item = contract.objects_by_id.get(identifier)
        if item is None:
            raise ValueError(f"contract object does not exist: {identifier}")
        if item.kind not in {"interaction", "constraint"}:
            raise ValueError(f"contractObjects must reference interaction or constraint objects: {identifier}")

    verification_id = _identifier(entry["verification"], "verification")
    verification = contract.verification_by_id.get(verification_id)
    if verification is None:
        raise ValueError(f"verification does not exist: {verification_id}")
    verification_traces = tuple(verification.value["traces"])
    if set(object_ids) != set(verification_traces):
        raise ValueError(f"trace entry contractObjects does not match verification traces: {verification_id}")
    _identifier(entry["acceptanceCriterion"], "acceptanceCriterion")

    tests = entry["tests"]
    if not isinstance(tests, list) or not tests:
        raise ValueError("tests must be a non-empty list")
    test_paths: set[Path] = set()
    for test_index, raw_test in enumerate(tests):
        test = _mapping(raw_test, f"tests[{test_index}]", {"path", "selector"})
        path = _safe_issue_file(repo_root, issue_directory, test["path"], "tests path")
        if path in test_paths:
            raise ValueError("tests must not contain duplicate paths")
        test_paths.add(path)
        _meaningful(test["selector"], "tests selector")

    evidence = _mapping(entry["evidence"], "evidence", {"before"}, {"after"})
    before_values = _unique_strings(evidence["before"], "evidence.before")
    before_paths = tuple(_safe_issue_file(repo_root, issue_directory, value, "evidence.before", evidence=True) for value in before_values)
    conclusion = _meaningful(entry["conclusion"], "conclusion")
    if conclusion not in ENTRY_CONCLUSIONS:
        raise ValueError("conclusion must be resolved, reduced, or blocked")
    after_values = tuple()
    if "after" in evidence:
        after_values = _unique_strings(evidence["after"], "evidence.after") if evidence["after"] else tuple()
    if conclusion in {"resolved", "reduced"} and not after_values:
        raise ValueError(f"{conclusion} evidence.after must be non-empty")
    if conclusion == "blocked" and not after_values:
        if "blocker" not in entry:
            raise ValueError("blocked entry requires a meaningful blocker when evidence.after is omitted")
        _meaningful(entry["blocker"], "blocker")
    elif "blocker" in entry:
        raise ValueError("blocker is only allowed when a blocked entry omits evidence.after")
    after_paths = tuple(_safe_issue_file(repo_root, issue_directory, value, "evidence.after", evidence=True) for value in after_values)
    if set(before_paths) & set(after_paths):
        raise ValueError("evidence.after must not reuse before evidence")

    if "ui" in entry:
        _verify_ui(repo_root, issue_directory, entry["ui"])
    return TraceEntry(entry_id, object_ids, verification_id, _identifier(entry["acceptanceCriterion"], "acceptanceCriterion"), conclusion, after_paths)


def _verify_ui(repo_root: Path, issue_directory: Path, raw: object) -> None:
    ui = _mapping(raw, "ui", {"claimScope", "surface", "targetUrl", "pageTitle", "modelIdentity", "screenshot", "structured"})
    claim_scope = _meaningful(ui["claimScope"], "ui.claimScope")
    surface = _meaningful(ui["surface"], "ui.surface")
    if claim_scope not in UI_CLAIM_SCOPES:
        raise ValueError("ui.claimScope must be product-integration or component-harness")
    if surface not in UI_SURFACES:
        raise ValueError("ui.surface must be product or component-harness")
    if claim_scope == "product-integration" and surface != "product":
        raise ValueError("product-integration requires ui.surface: product")
    if claim_scope == "component-harness" and surface != "component-harness":
        raise ValueError("component-harness cannot claim product integration")
    target_url = _meaningful(ui["targetUrl"], "ui.targetUrl")
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ui.targetUrl must be a complete HTTP(S) URL")
    _meaningful(ui["pageTitle"], "ui.pageTitle")
    _meaningful(ui["modelIdentity"], "ui.modelIdentity")
    screenshot = _safe_issue_file(repo_root, issue_directory, ui["screenshot"], "ui.screenshot", evidence=True)
    structured = _safe_issue_file(repo_root, issue_directory, ui["structured"], "ui.structured", evidence=True)
    screenshot_relative = screenshot.relative_to(issue_directory)
    structured_relative = structured.relative_to(issue_directory)
    if "screenshots" not in screenshot_relative.parts:
        raise ValueError("ui.screenshot must stay under evidence/screenshots")
    if "dom" not in structured_relative.parts:
        raise ValueError("ui.structured must stay under evidence/dom")


def _verify_closure(contract: ContractDocument, entries: tuple[TraceEntry, ...]) -> None:
    entry_ids = tuple(entry.id for entry in entries)
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("trace entry ids must be unique")
    by_verification = {entry.verification for entry in entries}
    missing_verifications = sorted(set(contract.verification_by_id) - by_verification)
    if missing_verifications:
        raise ValueError(f"active verification has no Issue acceptanceCriterion trace entry: {missing_verifications[0]}")
    traced_objects = {identifier for entry in entries for identifier in entry.contract_objects}
    missing_constraints = sorted(
        item.id for item in contract.objects_by_id.values() if item.kind == "constraint" and item.id not in traced_objects
    )
    if missing_constraints:
        raise ValueError(f"active core constraint has no trace entry: {missing_constraints[0]}")


def check_traceability(repo_root: Path, issue: str, contract: ContractDocument, matrix: Path | None = None) -> TraceabilityResult:
    root = repo_root.resolve(strict=False)
    issue_directory, path, document = _load_matrix(root, issue, matrix)
    current_contract = _verify_contract(root, contract, document["contract"])
    raw_entries = document["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("traceability entries must be a non-empty list")
    entries = tuple(_verify_entry(root, issue_directory, index, raw, current_contract) for index, raw in enumerate(raw_entries))
    _verify_closure(current_contract, entries)
    return TraceabilityResult(path, issue_directory, entries)


def check_traceability_resolution(repo_root: Path, issue: str, conclusion: str, report_evidence: set[Path]) -> None:
    root = repo_root.resolve(strict=False)
    _, _, document = _load_matrix(root, issue, None)
    contract_data = _mapping(document["contract"], "matrix contract", {"id", "version", "file"})
    contract = load_contract(root, _safe_contract_file(root, contract_data["file"]))
    result = check_traceability(root, issue, contract)
    conclusions = {entry.conclusion for entry in result.entries}
    if conclusion == "resolved" and conclusions != {"resolved"}:
        raise ValueError("resolved resolution-report requires every trace entry to be resolved")
    if conclusion == "reduced" and "blocked" in conclusions:
        raise ValueError("reduced resolution-report cannot contain blocked trace entries")
    if conclusion not in {"resolved", "reduced", "blocked"}:
        raise ValueError("resolution-report conclusion must be resolved, reduced, or blocked")
    if conclusion == "blocked":
        return
    required_after = {path.resolve() for entry in result.entries for path in entry.after_evidence}
    actual = {path.resolve() for path in report_evidence}
    if not required_after.issubset(actual):
        raise ValueError("resolution-report evidence must reference every trace after-evidence file, not only before evidence")
