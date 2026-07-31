from __future__ import annotations

import os
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval, dependencies as dependencies_module, traceability as traceability_module
from xflow.bindings import git_path, resolve_bindings
from xflow.checks import check_resolution_report
from xflow.collaboration import repository_lock
from xflow.contracts import load_contract, validate_contract_acceptance
from xflow.paths import active_task_pointer_file
from xflow.task_state import TaskState, activate_task, render_task_state
from xflow.traceability import check_traceability, check_traceability_resolution


CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
MATRIX_FIXTURE = Path(__file__).parent / "fixtures" / "traceability" / "valid.yaml"
ACCEPTED_OBJECTS = (
    "example.capability.capability-name",
    "example.verify.case.operation-success",
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_image(
    path: Path,
    *,
    size: tuple[int, int] = (4, 4),
    random_pixels: bool = False,
    image_format: str = "PNG",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if random_pixels:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (18, 92, 140))
    image.save(path, format=image_format)


def approve(path: Path) -> None:
    write(path, path.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# Trace fixture\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize trace fixture", "-q")
    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"contracts"}}\n')
    (repo / "contracts").mkdir()
    shutil.copyfile(CONTRACT_FIXTURE, repo / "contracts" / "contract.yaml")
    contract_path = repo / "contracts" / "contract.yaml"
    contract_text = contract_path.read_text(encoding="utf-8")
    write(
        contract_path,
        contract_text.replace(
            "      - type: automated\n        target: contract-test",
            "      - type: product-integration\n        target: http://127.0.0.1:5173/design/42",
            1,
        ),
    )
    return repo


def matrix_path(repo: Path, issue: str = "101") -> Path:
    return repo / ".xflow" / "issues" / f"issue-{issue}" / "traceability-matrix.yaml"


def prepare_valid_chain(repo: Path, issue: str = "101") -> Path:
    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"contracts"}}\n')
    contract_path = repo / "contracts" / "contract.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONTRACT_FIXTURE, contract_path)
    contract_text = contract_path.read_text(encoding="utf-8")
    write(
        contract_path,
        contract_text.replace(
            "      - type: automated\n        target: contract-test",
            "      - type: product-integration\n        target: http://127.0.0.1:5173/design/42",
            1,
        ),
    )
    target = matrix_path(repo, issue)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MATRIX_FIXTURE, target)
    for relative in (
        "tests/test_operation.py",
        "tests/test_rejection.py",
        "evidence/api/operation-before.json",
        "evidence/api/operation-after.json",
        "evidence/api/rejection-before.json",
        "evidence/api/rejection-after.json",
    ):
        write(target.parent / relative, f"stable trace fixture: {relative}\n")
    write_image(target.parent / "evidence" / "screenshots" / "c-001-after.png")
    write(
        target.parent / "evidence" / "dom" / "c-001-after.json",
        json.dumps(
            {
                "surface": "product",
                "targetUrl": "http://127.0.0.1:5173/design/42",
                "pageTitle": "XFlow Studio",
                "modelIdentity": "model-42",
                "runtime": {"route": "/design/42", "modelLoaded": True},
            },
            ensure_ascii=True,
        )
        + "\n",
    )
    state = TaskState(
        issue=issue,
        execution_state="S5_LOCAL_VERIFICATION",
        semantic_phase="classified",
        classification="ui-defect",
        contract="example.contract.capability-name@0.1.0",
        contract_file="contracts/contract.yaml",
        contract_change_required=False,
        branch="main",
        base="main",
        allowed_actions=("verify contract closure",),
        forbidden_actions=("push",),
        human_gate="human review required",
        human_approval_ref="none",
    )
    write(target.parent / "task-state.md", render_task_state(state))
    write(
        target.parent / "classification.yaml",
        """version: 0.1.0
request:
  originalStatement: Verify the accepted capability implementation.
contractSearch:
  status: found
  refs: [contracts/contract.yaml]
classification: ui-defect
contractChangeRequired: false
reason: The implementation must close the existing contract.
nextArtifact: issue-draft.md
decisionSource: ai-proposed
""",
    )
    write(
        target.parent / "issue-draft.md",
        """<!-- xflow: issue-draft -->

## Background
Verify the capability contract.

## Problem
The implementation needs closure evidence.

## Goal
Close the declared verification scenarios.

## Scope
- Includes contract verification.

## Acceptance Criteria
- [ ] C-001: The successful operation is verified.
- [ ] C-002: The rejected operation preserves state.

## Verification Plan
- Run the declared tests and collect local evidence.
""",
    )
    base_time = time.time_ns() - 2_000_000_000
    for relative in ("evidence/api/operation-before.json", "evidence/api/rejection-before.json"):
        os.utime(target.parent / relative, ns=(base_time, base_time))
    for relative in (
        "evidence/api/operation-after.json",
        "evidence/api/rejection-after.json",
        "evidence/screenshots/c-001-after.png",
        "evidence/dom/c-001-after.json",
    ):
        os.utime(target.parent / relative, ns=(base_time + 1_000_000_000, base_time + 1_000_000_000))
    activate_task(repo, issue)
    return target


def resolution_report_text(conclusion: str) -> str:
    evidence = (
        "- [operation after](evidence/api/operation-after.json)\n"
        "- [UI screenshot](evidence/screenshots/c-001-after.png)\n"
        "- [UI state](evidence/dom/c-001-after.json)\n"
        "- [rejection after](evidence/api/rejection-after.json)"
    )
    blocks = []
    for number, title in (("001", "The successful operation is verified"), ("002", "The rejected operation preserves state")):
        blocks.append(
            f"""### Criterion C-{number}: {title}

#### Verification Type
{'product-integration' if number == '001' else 'automated'}

#### Expected Result
The contract verification reaches its declared result.

#### Evidence
{evidence}

#### Actual Result
The declared result was observed in fresh local evidence.

#### Human Review
- [ ] Confirm this evidence supports criterion C-{number}.
"""
        )
    return f"""# Resolution Report

## Source Problem Or Gap
- issue-draft.md

## Actual Changes
- Closed the capability verification chain.

## Evidence Index
{evidence}

## Completion Verification

{''.join(blocks)}
## Closure Conclusion
Conclusion: {conclusion}
Reason: Trace conclusions match this report.

## AI Self-Review Result
- [x] Trace and report evidence are consistent.

## Remaining Risks
- External capture authorship still requires human review.

## Human Review Request
- Review the evidence identities and conclusions.
"""


def write_resolution_report(repo: Path, conclusion: str = "resolved") -> Path:
    report = matrix_path(repo).with_name("resolution-report.md")
    write(report, resolution_report_text(conclusion))
    return report


def assert_error(expected: str, callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    write(path, text.replace(old, new))


def test_valid_chain_and_cli(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    result = check_traceability(repo, "101", contract, path)
    assert result.path == path
    assert tuple(entry.id for entry in result.entries) == ("trace-001", "trace-002")
    env = {**os.environ, "DEVCTL_REPO_ROOT": str(repo), "PYTHONPATH": str(OPS_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-m", "xflow", "trace", "check", "--issue", "101", "--contract", "contracts/contract.yaml", "--matrix", str(path)]
    completed = subprocess.run(command, cwd=repo, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert completed.returncode == 0, completed.stderr
    assert "trace check passed" in completed.stdout


def test_schema_and_reference_rejections(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    cases = (
        ("unknown-contract", "id: example.contract.capability-name", "id: other.contract", "task-state Contract does not match matrix contract"),
        ("unknown-object", "example.interaction.perform-operation", "example.interaction.missing", "contract object does not exist"),
        ("unknown-verification", "example.verify.case.operation-success", "example.verify.case.missing", "verification does not exist"),
        ("wrong-kind", "example.constraint.preserve-state-on-rejection", "example.value.request", "must reference interaction or constraint"),
        ("wrong-trace", "example.verify.case.operation-success", "example.verify.case.operation-rejection", "does not match verification traces"),
        ("issue", 'issue: "101"', 'issue: "102"', "matrix Issue mismatch"),
        ("criterion", "acceptanceCriterion: criterion-001", "acceptanceCriterion: TBD", "acceptanceCriterion must be meaningful"),
        ("selector", "selector: test_operation_success", "selector: ''", "tests selector must be meaningful"),
    )
    for name, old, new, expected in cases:
        assert old in baseline, name
        write(path, baseline.replace(old, new))
        assert_error(expected, lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline)
    assert_error("traceability matrix must be .xflow/issues/issue-<id>/traceability-matrix.yaml", lambda: check_traceability(repo, "101", contract, path.with_name("other.yaml")))


def test_path_and_evidence_rejections(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    cases = (
        ("missing-test", "tests/test_operation.py", "tests/missing.py", "missing tests path"),
        ("absolute", "tests/test_operation.py", "C:/temp/test.py", "must be relative"),
        ("parent", "tests/test_operation.py", "../outside.py", "must not contain '..'"),
        ("remote", "tests/test_operation.py", "https://example.test/test.py", "must not use a URI or object-storage domain"),
        ("outside", "evidence/api/operation-after.json", "evidence/../outside.json", "must not contain '..'"),
        (
            "no-after",
            "      after:\n        - evidence/api/operation-after.json\n        - evidence/screenshots/c-001-after.png\n        - evidence/dom/c-001-after.json",
            "      after: []",
            "resolved evidence.after must be non-empty",
        ),
        ("reuse-before", "evidence/api/operation-after.json", "evidence/api/operation-before.json", "before/after/UI evidence paths must be globally distinct"),
    )
    for name, old, new, expected in cases:
        assert old in baseline, name
        write(path, baseline.replace(old, new))
        assert_error(expected, lambda: check_traceability(repo, "101", contract, path))

    blocked_without_reason = baseline.replace("conclusion: resolved\n", "conclusion: blocked\n", 1)
    write(path, blocked_without_reason)
    assert_error("every blocked entry requires a structured meaningful external blocker", lambda: check_traceability(repo, "101", contract, path))
    write(
        path,
        blocked_without_reason.replace(
            "conclusion: blocked\n",
            "conclusion: blocked\n    blocker:\n      condition: Product environment is unavailable.\n      owner: Platform operations\n      requiredAction: Restore the product environment.\n",
            1,
        ),
    )
    check_traceability(repo, "101", contract, path)
    write(path, baseline)

    outside = repo / "outside-evidence.json"
    write(outside, "outside trace evidence\n")
    escaped = path.parent / "evidence" / "api" / "escaped.json"
    try:
        escaped.symlink_to(outside)
    except OSError:
        return
    write(path, baseline.replace("evidence/api/operation-after.json", "evidence/api/escaped.json", 1))
    assert_error("must not traverse a symlink, junction, or reparse point", lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline)


def test_ui_identity_rejections(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    cases = (
        ("missing-shot", "      screenshot: evidence/screenshots/c-001-after.png\n", "", "ui missing required fields: screenshot"),
        ("missing-structured", "      structured: evidence/dom/c-001-after.json\n", "", "ui missing required fields: structured"),
        ("harness-claim", "surface: product", "surface: component-harness", "product-integration verification requires"),
        ("bad-url", "http://127.0.0.1:5173/design/42", "not-a-url", "ui.targetUrl must be a complete HTTP(S) URL"),
    )
    for name, old, new, expected in cases:
        assert old in baseline, name
        write(path, baseline.replace(old, new))
        assert_error(expected, lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline)


def test_resolution_consistency(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    baseline = path.read_text(encoding="utf-8")
    report_evidence = {
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "api" / "operation-after.json",
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "screenshots" / "c-001-after.png",
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "dom" / "c-001-after.json",
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "api" / "rejection-after.json",
    }
    check_traceability_resolution(repo, "101", "resolved", report_evidence)
    write(path, baseline.replace("conclusion: resolved\n", "conclusion: reduced\n", 1))
    assert_error("resolved resolution-report requires every trace entry to be resolved", lambda: check_traceability_resolution(repo, "101", "resolved", report_evidence))
    check_traceability_resolution(repo, "101", "reduced", report_evidence)
    write_resolution_report(repo, "reduced")
    check_resolution_report(repo, "101")
    blocked = baseline.replace(
        "    conclusion: resolved\n    ui:\n",
        "    conclusion: blocked\n    blocker:\n      condition: Product environment is unavailable.\n      owner: Platform operations\n      requiredAction: Restore the product environment.\n    ui:\n",
        1,
    )
    write(path, blocked)
    assert_error("reduced resolution-report requires at least one reduced trace entry and no blocked entries", lambda: check_traceability_resolution(repo, "101", "reduced", report_evidence))
    check_traceability_resolution(repo, "101", "blocked", report_evidence)
    write_resolution_report(repo, "blocked")
    check_resolution_report(repo, "101")
    write(path, baseline.replace("conclusion: resolved\n", "conclusion: reduced\n", 1))
    assert_error("must reference every trace after-evidence file", lambda: check_traceability_resolution(repo, "101", "reduced", {next(iter(report_evidence))}))


def test_durable_closure_and_authoritative_bindings(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    write_resolution_report(repo)
    check_resolution_report(repo, "101")

    hidden = path.with_name("traceability-matrix.hidden")
    path.rename(hidden)
    assert_error("required traceability matrix", lambda: check_resolution_report(repo, "101"))
    hidden.rename(path)

    contract_path = repo / "contracts" / "contract.yaml"
    hidden_contract = contract_path.with_suffix(".hidden")
    contract_path.rename(hidden_contract)
    assert_error("missing contract file", lambda: check_resolution_report(repo, "101"))
    hidden_contract.rename(contract_path)

    state = path.with_name("task-state.md")
    classification = path.with_name("classification.yaml")
    original_state = state.read_text(encoding="utf-8")
    write(state, original_state.replace("Contract: example.contract.capability-name@0.1.0", "Contract: unrelated.contract@9.9.9"))
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    assert_error("active task pointer contract binding mismatch", lambda: check_traceability(repo, "101", contract, path))
    write(state, original_state)

    write(state, original_state.replace("Branch: main", "Branch: feature/stale"))
    assert_error("task-state branch mismatch", lambda: check_resolution_report(repo, "101"))
    write(state, original_state)

    downgraded_state = original_state.replace(
        "Contract: example.contract.capability-name@0.1.0",
        "Contract: legacy.current-task@0.1.0",
    ).replace("Contract File: contracts/contract.yaml", "Contract File: .xflow/current-task.md")
    write(state, downgraded_state)
    classification.rename(hidden_classification := classification.with_suffix(".downgrade-hidden"))
    path.rename(hidden_matrix := path.with_suffix(".downgrade-hidden"))
    assert_error("active task pointer contract binding mismatch", lambda: check_resolution_report(repo, "101"))
    hidden_classification.rename(classification)
    hidden_matrix.rename(path)
    write(state, original_state)

    write(state, original_state.replace("Semantic Phase: classified", "Semantic Phase: accepted-design"))
    assert_error("Human Approval Ref is required", lambda: check_traceability(repo, "101", contract, path))
    write(state, original_state)

    original_classification = classification.read_text(encoding="utf-8")
    write(classification, original_classification.replace("refs: [contracts/contract.yaml]", "refs: [contracts/unrelated.yaml]"))
    assert_error("classification contractSearch.refs", lambda: check_traceability(repo, "101", contract, path))
    write(classification, original_classification)

    classification.unlink()
    assert_error("classification.yaml", lambda: check_resolution_report(repo, "101"))
    write(classification, original_classification)

    state.unlink()
    assert_error("task-state.md", lambda: check_resolution_report(repo, "101"))
    write(state, original_state)

    pointer = active_task_pointer_file(repo, resolve_bindings(repo).worktree)
    assert pointer.is_relative_to(git_path(repo, "--git-common-dir"))
    pointer.unlink()
    assert_error("active task pointer", lambda: check_resolution_report(repo, "101"))
    activate_task(repo, "101")

    hidden_state = state.with_suffix(".hidden")
    hidden_classification = classification.with_suffix(".hidden")
    hidden_matrix = path.with_suffix(".hidden")
    state.rename(hidden_state)
    classification.rename(hidden_classification)
    path.rename(hidden_matrix)
    assert_error("task-state.md", lambda: check_resolution_report(repo, "101"))
    hidden_state.rename(state)
    hidden_classification.rename(classification)
    hidden_matrix.rename(path)


def test_evidence_identity_and_digest_rejections(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    issue_root = path.parent
    before = issue_root / "evidence" / "api" / "operation-before.json"
    after = issue_root / "evidence" / "api" / "operation-after.json"
    after.unlink()
    os.link(before, after)
    assert_error("exactly one filesystem link", lambda: check_traceability(repo, "101", contract, path))

    after.unlink()
    shutil.copyfile(before, after)
    assert_error("after evidence digest must differ from every before evidence digest", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    rejection_after = issue_root / "evidence" / "api" / "rejection-after.json"
    shutil.copyfile(after, rejection_after)
    fresh = max(before.stat().st_mtime_ns, (issue_root / "evidence" / "api" / "rejection-before.json").stat().st_mtime_ns) + 1
    os.utime(after, ns=(fresh, fresh))
    os.utime(rejection_after, ns=(fresh, fresh))
    assert_error("after evidence digest must be unique across verifications", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    after = issue_root / "evidence" / "api" / "operation-after.json"
    before = issue_root / "evidence" / "api" / "operation-before.json"
    old = before.stat().st_mtime_ns
    os.utime(after, ns=(old, old))
    assert_error("after evidence mtime_ns must be strictly later than its verification before evidence", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    after = issue_root / "evidence" / "api" / "operation-after.json"
    before = issue_root / "evidence" / "api" / "operation-before.json"
    inverted = before.stat().st_mtime_ns - 1
    os.utime(after, ns=(inverted, inverted))
    assert_error("after evidence mtime_ns must be strictly later than its verification before evidence", lambda: check_traceability(repo, "101", contract, path))


def test_contract_derived_ui_obligations(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract_path = repo / "contracts" / "contract.yaml"
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")
    ui_block = """    ui:
      claimScope: product-integration
      surface: product
      targetUrl: http://127.0.0.1:5173/design/42
      pageTitle: XFlow Studio
      modelIdentity: model-42
      screenshot: evidence/screenshots/c-001-after.png
      structured: evidence/dom/c-001-after.json
"""
    write(path, baseline.replace(ui_block, ""))
    assert_error("UI-oriented verification requires ui", lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline)

    screenshot = path.parent / "evidence" / "screenshots" / "c-001-after.png"
    write(screenshot, "plain text is not an image\n")
    assert_error("fully decodable PNG/JPEG/WebP", lambda: check_traceability(repo, "101", contract, path))
    write_bytes(screenshot, b"\x89PNG\r\n\x1a\ntrace-image")
    assert_error("fully decodable PNG/JPEG/WebP", lambda: check_traceability(repo, "101", contract, path))
    write_image(screenshot)

    structured = path.parent / "evidence" / "dom" / "c-001-after.json"
    write(structured, "{not-json}\n")
    assert_error("structured evidence must be a JSON mapping", lambda: check_traceability(repo, "101", contract, path))
    prepare_valid_chain(repo)
    baseline = path.read_text(encoding="utf-8")
    contract = load_contract(repo, contract_path)

    without_ui_after = baseline.replace(
        "        - evidence/screenshots/c-001-after.png\n        - evidence/dom/c-001-after.json\n",
        "",
        1,
    )
    write(path, without_ui_after)
    assert_error("UI artifacts must be included in evidence.after", lambda: check_traceability(repo, "101", contract, path))

    shared = path.parent / "evidence" / "screenshots" / "dom" / "shared.png"
    write_bytes(shared, b"\x89PNG\r\n\x1a\nshared")
    same_ui_file = baseline.replace(
        "structured: evidence/dom/c-001-after.json",
        "structured: evidence/screenshots/c-001-after.png",
    )
    write(path, same_ui_file)
    assert_error("screenshot and structured evidence must be distinct", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    contract_text = contract_path.read_text(encoding="utf-8")
    write(contract_path, contract_text.replace("type: product-integration", "type: ui", 1))
    contract = load_contract(repo, contract_path)
    assert_error("product-integration claim requires contract verification type product-integration", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")
    write(path, baseline.replace("pageTitle: XFlow Studio", "pageTitle: https://example.test/title"))
    assert_error("targetUrl is the only URL-bearing identity field", lambda: check_traceability(repo, "101", contract, path))

    write(path, baseline.replace("claimScope: product-integration\n      surface: product", "claimScope: component-harness\n      surface: component-harness"))
    assert_error("product-integration verification requires", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")
    structured = path.parent / "evidence" / "dom" / "c-001-after.json"
    structured_text = structured.read_text(encoding="utf-8")
    write(path, baseline.replace("http://127.0.0.1:5173/design/42", "https://example.test/component-harness"))
    write(structured, structured_text.replace("http://127.0.0.1:5173/design/42", "https://example.test/component-harness"))
    assert_error("must match product-integration verifyBy.target", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    contract_text = contract_path.read_text(encoding="utf-8")
    write(
        contract_path,
        contract_text.replace(
            "target: http://127.0.0.1:5173/design/42",
            "target: product-environment",
            1,
        ),
    )
    contract = load_contract(repo, contract_path)
    assert_error("product-integration verifyBy.target must be a complete HTTP(S) URL", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    contract_text = contract_path.read_text(encoding="utf-8")
    write(
        contract_path,
        contract_text.replace(
            "target: http://127.0.0.1:5173/design/42",
            "target: HTTP://127.0.0.1:80/design/42",
            1,
        ),
    )
    baseline = path.read_text(encoding="utf-8")
    write(path, baseline.replace("http://127.0.0.1:5173/design/42", "http://127.0.0.1/design/42"))
    structured = path.parent / "evidence" / "dom" / "c-001-after.json"
    write(
        structured,
        structured.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:5173/design/42",
            "http://127.0.0.1/design/42",
        ),
    )
    check_traceability(repo, "101", load_contract(repo, contract_path), path)

    prepare_valid_chain(repo)
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")
    screenshot = path.parent / "evidence" / "screenshots" / "c-001-after.png"
    write(path, baseline)
    write_image(screenshot, size=(512, 512), random_pixels=True)
    assert screenshot.stat().st_size > 262_144
    os.utime(screenshot, ns=(time.time_ns(), time.time_ns()))
    check_traceability(repo, "101", contract, path)

    for image_format in ("PNG", "JPEG", "WEBP"):
        prepare_valid_chain(repo)
        write_image(screenshot, image_format=image_format)
        os.utime(screenshot, ns=(time.time_ns(), time.time_ns()))
        check_traceability(repo, "101", contract, path)

    prepare_valid_chain(repo)
    screenshot = path.parent / "evidence" / "screenshots" / "c-001-after.png"
    with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
        assert_error(
            "fully decodable PNG/JPEG/WebP",
            lambda: traceability_module._validate_image(screenshot.read_bytes()),
        )


def test_criteria_schema_and_exact_conclusions(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    write(path, baseline.replace("criterion-002", "criterion-999"))
    assert_error("acceptance criterion does not exist", lambda: check_traceability(repo, "101", contract, path))

    duplicate = baseline + """  - id: trace-003
    contractObjects: [example.interaction.perform-operation, example.constraint.preserve-state-on-rejection]
    verification: example.verify.case.operation-rejection
    acceptanceCriterion: criterion-001
    tests:
      - path: tests/test_operation.py
        selector: test_operation_success
    evidence:
      before: [evidence/api/operation-before.json]
      after: [evidence/api/operation-after.json]
    conclusion: resolved
"""
    write(path, duplicate)
    assert_error("verification binding must be unique", lambda: check_traceability(repo, "101", contract, path))

    invalid_after = baseline.replace("conclusion: resolved", "conclusion: blocked", 1).replace(
        "      after:\n        - evidence/api/operation-after.json\n        - evidence/screenshots/c-001-after.png\n        - evidence/dom/c-001-after.json",
        "      after: false",
        1,
    ).replace(
        "    ui:\n",
        "    blocker: blocked\n    ui:\n",
        1,
    )
    write(path, invalid_after)
    assert_error("evidence.after must be a list", lambda: check_traceability(repo, "101", contract, path))

    write(path, baseline)
    write_resolution_report(repo, "reduced")
    assert_error("reduced resolution-report requires at least one reduced trace entry", lambda: check_resolution_report(repo, "101"))
    write_resolution_report(repo, "blocked")
    assert_error("blocked resolution-report requires at least one blocked trace entry", lambda: check_resolution_report(repo, "101"))

    prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    report_text = report.read_text(encoding="utf-8")
    write(report, report_text.replace("Criterion C-001: The successful operation is verified", "Criterion C-001: An unrelated result is verified"))
    assert_error("title must match authoritative Acceptance Criteria", lambda: check_resolution_report(repo, "101"))

    write(report, report_text.replace("#### Verification Type\nproduct-integration", "#### Verification Type\nui"))
    assert_error("Verification Type must match matrix verification", lambda: check_resolution_report(repo, "101"))

    for verification_type in ("manual review", "人工验收"):
        prepare_valid_chain(repo)
        contract_path = repo / "contracts" / "contract.yaml"
        write(
            contract_path,
            contract_path.read_text(encoding="utf-8").replace(
                "type: automated",
                f"type: {verification_type}",
                1,
            ),
        )
        report = write_resolution_report(repo)
        write(
            report,
            report.read_text(encoding="utf-8").replace(
                "#### Verification Type\nautomated",
                f"#### Verification Type\n{verification_type}",
                1,
            ),
        )
        check_resolution_report(repo, "101")

    prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    report_text = report.read_text(encoding="utf-8")
    conclusion_cases = (
        ("Conclusion: resolved\nReason: Trace conclusions match this report.", "Not resolved: blocked by an unavailable environment", "canonical Conclusion field"),
        ("Conclusion: resolved\nReason: Trace conclusions match this report.", "Conclusion: resolved blocked\nReason: contradictory", "canonical Conclusion field"),
        ("Conclusion: resolved\nReason: Trace conclusions match this report.", "Conclusion: resolved\nReason:", "non-empty Reason"),
        ("Conclusion: resolved\nReason: Trace conclusions match this report.", "Conclusion: resolved\nReason: resolved here\nblocked: elsewhere", "unexpected prose"),
    )
    for old, new, expected in conclusion_cases:
        write(report, report_text.replace(old, new))
        assert_error(expected, lambda: check_resolution_report(repo, "101"))


def valid_gap_analysis_text(*, recognized: str = "yes", include_second: bool = True) -> str:
    second = "- [ ] C-002: The rejected operation preserves state.\n" if include_second else ""
    return f"""# Gap Analysis

## User Original Statement
Verify the accepted capability implementation.

## Clarified Problem Or Gap
The implementation requires contract closure evidence.

## Gap Analysis
The existing implementation has unverified contract behavior.

## Evidence
- evidence/api/operation-before.json

## Evidence-Backed Findings
### Finding F-001: Missing closure evidence

#### Finding Type
non-ui

#### Observation
The contract has not been closed.

#### User Impact
The result cannot be trusted.

#### Evidence
- evidence/api/operation-before.json

#### Analysis
Verification evidence is required.

#### Proposed Change
Run the declared verification.

#### Acceptance
The matrix and report agree.

#### Human Review
- [x] Review the finding.

## Scope Boundaries
Only the current contract is in scope.

## Proposed Modification Plan
Run tests and collect evidence.

## Acceptance Criteria
- [ ] C-001: The successful operation is verified.
{second}
## Human Recognition
Recognized: {recognized}
"""


def test_single_authoritative_criterion_source(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    state_path = path.with_name("task-state.md")
    state = traceability_module.parse_task_state_text(state_path, state_path.read_text(encoding="utf-8"), binding_mode="recorded", validate_acceptance=False)
    write(state_path, render_task_state(dataclass_replace(state, classification="implementation-gap")))
    classification = path.with_name("classification.yaml")
    replace(classification, "classification: ui-defect", "classification: implementation-gap")
    replace(classification, "nextArtifact: issue-draft.md", "nextArtifact: gap-analysis.md")
    gap = path.with_name("gap-analysis.md")
    write(gap, valid_gap_analysis_text(recognized="no"))
    assert_error("Recognized: yes", lambda: check_traceability(repo, "101", contract, path))

    write(gap, valid_gap_analysis_text(include_second=False))
    assert_error("acceptance criterion does not exist", lambda: check_traceability(repo, "101", contract, path))

    write(gap, valid_gap_analysis_text())
    check_traceability(repo, "101", contract, path)

    gap.unlink()
    assert_error("implementation-gap requires gap-analysis.md", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    issue_draft = path.with_name("issue-draft.md")
    write(issue_draft, "## Acceptance Criteria\n- [ ] C-001: forged\n- [ ] C-002: forged\n")
    assert_error("issue-draft", lambda: check_traceability(repo, "101", contract, path))


def test_current_repository_acceptance_binding(repo: Path, root: Path) -> None:
    source_parent = root / "source-parent"
    source_parent.mkdir()
    source = init_repo(source_parent)
    source_path = prepare_valid_chain(source)
    source_contract = load_contract(source, source / "contracts" / "contract.yaml")
    review = approval.prepare(
        source,
        "101",
        "contract-acceptance",
        source_contract.path,
        reviewer="reviewer",
        force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    approve(review)
    history = validate_contract_acceptance(source, "101", source_contract, ACCEPTED_OBJECTS)
    source_state = traceability_module.parse_task_state_text(
        source_path.with_name("task-state.md"),
        source_path.with_name("task-state.md").read_text(encoding="utf-8"),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    history_ref = history.relative_to(source_path.parent).as_posix()
    write(source_path.with_name("task-state.md"), render_task_state(dataclass_replace(source_state, semantic_phase="accepted-design", human_approval_ref=history_ref)))

    target_path = prepare_valid_chain(repo)
    shutil.copytree(source_path.parent / "approvals", target_path.parent / "approvals", dirs_exist_ok=True)
    target_state = traceability_module.parse_task_state_text(
        target_path.with_name("task-state.md"),
        target_path.with_name("task-state.md").read_text(encoding="utf-8"),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    write(target_path.with_name("task-state.md"), render_task_state(dataclass_replace(target_state, semantic_phase="accepted-design", human_approval_ref=history_ref)))
    assert_error("current repository", lambda: check_traceability(repo, "101", load_contract(repo, repo / "contracts" / "contract.yaml"), target_path))


def test_snapshot_content_and_transitive_revalidation(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    dependencies = path.with_name("dependencies.yaml")
    write(dependencies, 'version: "0.1.0"\nissue: "101"\ndependencies: []\n')
    original_check = dependencies_module.check_dependencies

    def replace_dependencies_after_capture(*args: object, **kwargs: object) -> object:
        write(dependencies, 'version: "0.1.0"\nissue: "999"\ndependencies: []\n')
        return original_check(*args, **kwargs)

    with patch.object(dependencies_module, "check_dependencies", side_effect=replace_dependencies_after_capture):
        assert_error("dependencies changed during closure validation", lambda: check_resolution_report(repo, "101", report))
    write(dependencies, 'version: "0.1.0"\nissue: "101"\ndependencies: []\n')

    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    review = approval.prepare(
        repo,
        "101",
        "contract-acceptance",
        contract.path,
        reviewer="reviewer",
        force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    approve(review)
    history = validate_contract_acceptance(repo, "101", contract, ACCEPTED_OBJECTS)
    state_path = path.with_name("task-state.md")
    state = traceability_module.parse_task_state_text(
        state_path,
        state_path.read_text(encoding="utf-8"),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    history_ref = history.relative_to(path.parent).as_posix()
    write(state_path, render_task_state(dataclass_replace(state, semantic_phase="accepted-design", human_approval_ref=history_ref)))
    import yaml

    history_payload = yaml.safe_load(history.read_text(encoding="utf-8"))
    archived_review = path.parent / history_payload["approvedReviewFile"]
    original_review = archived_review.read_text(encoding="utf-8")
    original_validate = approval.validate_contract_acceptance_history

    def replace_review_after_acceptance(*args: object, **kwargs: object) -> object:
        result = original_validate(*args, **kwargs)
        write(archived_review, original_review + "\nchanged after acceptance validation\n")
        return result

    with patch.object(approval, "validate_contract_acceptance_history", side_effect=replace_review_after_acceptance):
        assert_error(
            "contract acceptance supporting artifact changed during closure validation",
            lambda: check_traceability(repo, "101", contract, path),
        )
    write(archived_review, original_review)


def test_repository_collaboration_lock(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo),
        "PYTHONPATH": str(OPS_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "XFLOW_COLLABORATION_LOCK_TIMEOUT": "0",
    }
    command = [
        sys.executable,
        "-m",
        "xflow",
        "trace",
        "check",
        "--issue",
        "101",
        "--contract",
        "contracts/contract.yaml",
        "--matrix",
        str(path),
    ]
    with repository_lock(repo):
        completed = subprocess.run(
            command,
            cwd=repo,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    assert completed.returncode == 1
    assert "another devctl process holds the repository collaboration lock" in completed.stderr


def test_legacy_resolution_still_binds_current_issue(repo: Path) -> None:
    pointer = active_task_pointer_file(repo, resolve_bindings(repo).worktree)
    if pointer.exists():
        pointer.unlink()
    issue_root = repo / ".xflow" / "issues" / "issue-legacy"
    write(issue_root / "evidence" / "result.txt", "legacy result evidence\n")
    write(
        issue_root / "resolution-report.md",
        """# Resolution Report

## Source Problem Or Gap
- legacy current task

## Actual Changes
- Verified legacy behavior.

## Evidence Index
- evidence/result.txt

## Completion Verification
### Criterion C-001: Legacy behavior is verified

#### Verification Type
manual

#### Expected Result
The legacy behavior remains available.

#### Evidence
- evidence/result.txt

#### Actual Result
The legacy behavior was observed.

#### Human Review
- [x] Review the legacy evidence.

## Closure Conclusion
Conclusion: resolved
Reason: The legacy behavior is verified.

## AI Self-Review Result
- [x] Legacy evidence is present.

## Remaining Risks
- none

## Human Review Request
- Review the legacy result.
""",
    )
    current_task = repo / ".xflow" / "current-task.md"
    write(current_task, "# XFlow Current Task\n\nIssue: legacy\nState: S5_LOCAL_VERIFICATION\n")
    check_resolution_report(repo, "legacy")

    write(current_task, "# XFlow Current Task\n\nIssue: other\nState: S5_LOCAL_VERIFICATION\n")
    assert_error("legacy current task Issue mismatch", lambda: check_resolution_report(repo, "legacy"))

    current_task.unlink()
    assert_error("missing legacy current task", lambda: check_resolution_report(repo, "legacy"))


def test_provider_endpoint_families(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    domains = (
        "account.blob.core.windows.net",
        "account.dfs.core.windows.net",
        "account.file.core.windows.net",
        "account.blob.core.usgovcloudapi.net",
        "account.dfs.core.usgovcloudapi.net",
        "account.file.core.usgovcloudapi.net",
        "account.blob.core.chinacloudapi.cn",
        "account.dfs.core.chinacloudapi.cn",
        "account.file.core.chinacloudapi.cn",
        "public-bucket.r2.dev",
    )
    for domain in domains:
        candidate = f"tests/{domain}/proof.py"
        write(path.parent / candidate, "provider endpoint masquerading as a local path\n")
        write(path, baseline.replace("tests/test_operation.py", candidate, 1))
        assert_error("object-storage", lambda: check_traceability(repo, "101", contract, path))


def test_caller_specific_size_limits(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract_path = repo / "contracts" / "contract.yaml"
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")

    write_bytes(path, b"#" * (262_144 + 1))
    assert_error("traceability matrix exceeds 262144 bytes", lambda: check_traceability(repo, "101", contract, path))

    write(path, baseline)
    source = path.parent / "tests" / "test_operation.py"
    write_bytes(source, b"x" * (4 * 1024 * 1024 + 1))
    assert_error("tests path exceeds 4194304 bytes", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    structured = path.parent / "evidence" / "dom" / "c-001-after.json"
    write_bytes(structured, b"{" + b" " * (16 * 1024 * 1024) + b"}")
    assert_error("structured evidence exceeds 16777216 bytes", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    original_contract = contract_path.read_bytes()
    write_bytes(contract_path, original_contract + b"#" * (4 * 1024 * 1024))
    assert_error("contract file exceeds 4194304 bytes", lambda: check_traceability(repo, "101", contract, path))

    prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    write_bytes(report, report.read_bytes() + b" " * (4 * 1024 * 1024))
    assert_error("resolution report exceeds 4194304 bytes", lambda: check_resolution_report(repo, "101", report))

    prepare_valid_chain(repo)
    screenshot = path.parent / "evidence" / "screenshots" / "c-001-after.png"
    with screenshot.open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    assert_error("screenshot evidence exceeds 67108864 bytes", lambda: check_traceability(repo, "101", load_contract(repo, contract_path), path))

    prepare_valid_chain(repo)
    screenshot = path.parent / "evidence" / "screenshots" / "c-001-after.png"
    write_image(screenshot, size=(512, 512), random_pixels=True)
    assert screenshot.stat().st_size > 262_144
    os.utime(screenshot, ns=(time.time_ns(), time.time_ns()))
    report_evidence = {
        path.parent / "evidence" / "api" / "operation-after.json",
        screenshot,
        path.parent / "evidence" / "dom" / "c-001-after.json",
        path.parent / "evidence" / "api" / "rejection-after.json",
    }
    check_traceability_resolution(repo, "101", "resolved", report_evidence)


def test_exact_schema_rejections(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    baseline = path.read_text(encoding="utf-8")
    after_block = "      after:\n        - evidence/api/operation-after.json\n        - evidence/screenshots/c-001-after.png\n        - evidence/dom/c-001-after.json"
    for wrong_type in ("false", "0", "{}", "''"):
        write(path, baseline.replace(after_block, f"      after: {wrong_type}", 1))
        assert_error("evidence.after must be a list", lambda: check_traceability(repo, "101", contract, path))

    write(path, baseline.replace("    conclusion: resolved\n", "    conclusion: resolved\n    unexpected: true\n", 1))
    assert_error("contains unexpected fields: unexpected", lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline.replace("      modelIdentity: model-42\n", "      modelIdentity: model-42\n      unexpected: true\n", 1))
    assert_error("ui contains unexpected fields: unexpected", lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline.replace('issue: "101"\n', 'issue: "101"\nissue: "101"\n', 1))
    assert_error("duplicate YAML key: issue", lambda: check_traceability(repo, "101", contract, path))
    write(path, baseline[: baseline.index("entries:")] + "entries: []\n")
    assert_error("traceability entries must be a non-empty list", lambda: check_traceability(repo, "101", contract, path))
    write(
        path,
        baseline.replace(
            "    contractObjects:\n      - example.interaction.perform-operation\n",
            "    contractObjects: []\n",
            1,
        ),
    )
    assert_error("contractObjects must be a non-empty list", lambda: check_traceability(repo, "101", contract, path))
    weak_blocker = baseline.replace("conclusion: resolved\n", "conclusion: blocked\n", 1).replace(
        "    ui:\n",
        "    blocker:\n      condition: done\n      owner: x\n      requiredAction: done\n    ui:\n",
        1,
    )
    write(path, weak_blocker)
    assert_error("meaningful external condition", lambda: check_traceability(repo, "101", contract, path))


def test_canonical_ids_object_store_variants_and_race(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract_path = repo / "contracts" / "contract.yaml"
    contract_text = contract_path.read_text(encoding="utf-8")
    write(contract_path, contract_text.replace("example.verify.case.operation-success", "验证.成功", 1))
    matrix = path.read_text(encoding="utf-8").replace("example.verify.case.operation-success", "验证.成功", 1)
    write(path, matrix)
    check_traceability(repo, "101", load_contract(repo, contract_path), path)

    prepare_valid_chain(repo)
    contract = load_contract(repo, contract_path)
    baseline = path.read_text(encoding="utf-8")
    domains = (
        "oss-cn.example.aliyuncs.com",
        "bucket.cos.ap-shanghai.myqcloud.com",
        "bucket.s3.amazonaws.com",
        "cdn.cloudfront.net",
        "account.dfs.core.windows.net",
        "storage.googleapis.com",
        "bucket.obs.cn.myhuaweicloud.com",
        "bucket.r2.cloudflarestorage.com",
        "namespace.objectstorage.us.oraclecloud.com",
        "s3.us.cloud-object-storage.appdomain.cloud",
        "bucket.nyc3.digitaloceanspaces.com",
        "bucket.wasabisys.com",
        "f000.backblazeb2.com",
    )
    for domain in domains:
        candidate = f"tests/{domain}/proof.py"
        write(path.parent / candidate, "object storage is not local evidence\n")
        write(path, baseline.replace("tests/test_operation.py", candidate, 1))
        assert_error("object-storage", lambda: check_traceability(repo, "101", contract, path))

    write(path, baseline)
    write_resolution_report(repo)
    report = path.with_name("resolution-report.md")
    report_text = report.read_text(encoding="utf-8")
    remote_relative = "evidence/bucket.wasabisys.com/proof.json"
    write(path.parent / remote_relative, "local path with object-store identity\n")
    write(report, report_text.replace("evidence/api/operation-after.json", remote_relative))
    assert_error("do not use COS/OSS", lambda: check_resolution_report(repo, "101"))
    write(report, report_text)

    original_load_context = traceability_module._load_context
    after_file = path.parent / "evidence" / "api" / "operation-after.json"
    original_after = after_file.read_text(encoding="utf-8")

    def replace_evidence_after_report_index(*args: object, **kwargs: object) -> object:
        write(after_file, "replacement captured only by matrix phase\n")
        return original_load_context(*args, **kwargs)

    with patch.object(traceability_module, "_load_context", side_effect=replace_evidence_after_report_index):
        assert_error("changed between closure snapshots", lambda: check_resolution_report(repo, "101"))
    write(after_file, original_after)

    original_verify_closure = traceability_module._verify_closure

    def replace_matrix_after_selection(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        write(path, baseline + "# replaced after verification\n")
        return result

    with patch.object(traceability_module, "_verify_closure", side_effect=replace_matrix_after_selection):
        assert_error("traceability matrix changed during closure validation", lambda: check_resolution_report(repo, "101"))


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo = init_repo(root)
        test_valid_chain_and_cli(repo)
        test_schema_and_reference_rejections(repo)
        test_path_and_evidence_rejections(repo)
        test_ui_identity_rejections(repo)
        test_resolution_consistency(repo)
        test_durable_closure_and_authoritative_bindings(repo)
        test_evidence_identity_and_digest_rejections(repo)
        test_contract_derived_ui_obligations(repo)
        test_criteria_schema_and_exact_conclusions(repo)
        test_single_authoritative_criterion_source(repo)
        test_current_repository_acceptance_binding(repo, root)
        test_snapshot_content_and_transitive_revalidation(repo)
        test_repository_collaboration_lock(repo)
        test_legacy_resolution_still_binds_current_issue(repo)
        test_provider_endpoint_families(repo)
        test_caller_specific_size_limits(repo)
        test_exact_schema_rejections(repo)
        test_canonical_ids_object_store_variants_and_race(repo)
    print("trace core ok")


if __name__ == "__main__":
    main()
