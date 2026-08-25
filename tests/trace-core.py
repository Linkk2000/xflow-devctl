from __future__ import annotations

import os
import io
import hashlib
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

from tests.support import write_text_lf

from xflow import approval, dependencies as dependencies_module, traceability as traceability_module
from xflow.bindings import git_path, resolve_bindings
from xflow.checks import check_resolution_report
from xflow.collaboration import repository_lock
from xflow.contracts import load_contract, validate_contract_acceptance
from xflow.paths import active_task_pointer_file, legacy_active_task_pointer_file, task_authority_file
from xflow.task_state import TaskState, activate_task, migrate_legacy_current_task, render_task_state
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
    write_text_lf(path, text)


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
nextArtifact: lightweight-route-complete
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

    command = [sys.executable, "-m", "xflow", "trace", "check", "--issue", "101", "--matrix", str(path)]
    completed = subprocess.run(command, cwd=repo, env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert completed.returncode != 0
    assert "non-acceptance trace check requires --contract" in completed.stderr


def test_capability_closure_requires_semantic_exit(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    issue_root = path.parent
    state = TaskState(
        issue="101",
        execution_state="S10_DONE",
        semantic_phase="classified",
        classification="capability-change",
        contract="example.contract.capability-name@0.1.0",
        contract_file="contracts/contract.yaml",
        contract_change_required=True,
        branch="main",
        base="main",
        allowed_actions=("inspect closure",),
        forbidden_actions=("close without semantic acceptance",),
        human_gate="capability design acceptance required",
        human_approval_ref="none",
    )
    write(issue_root / "task-state.md", render_task_state(state))
    write(
        issue_root / "classification.yaml",
        """version: 0.1.0
request:
  originalStatement: Deliver a new participant-visible capability.
contractSearch:
  status: found
  refs: [contracts/contract.yaml]
classification: capability-change
contractChangeRequired: true
reason: The request changes a participant-visible result.
nextArtifact: contract-change-proposal.md
decisionSource: ai-proposed
""",
    )
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    assert_error(
        "capability-change requires accepted-design",
        lambda: check_traceability(repo, "101", contract, path),
    )


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


def test_strict_product_url_identity() -> None:
    invalid = (
        "https://example.test/a\x01b",
        "https://exa mple.test/path",
        "https://exa\u00a0mple.test/path",
        "https://@example.test/path",
        "https://user@example.test/path",
        "https://example.test:/path",
        "https://example.test:not-a-port/path",
        "https://[example.test]/path",
        "https://-bad.example/path",
        "https://exa_mple.test/path",
        "https://xn--abc.example/path",
        "https:///missing-host",
        "https://example.test/path%",
        "https://example.test/path?value=%Z0",
        "https://example.test/path#value=%0Z",
        "https://example.test/%ZZ",
    )
    for value in invalid:
        assert_error(
            "must be a complete HTTP(S) URL",
            lambda value=value: traceability_module._normalize_http_url(value, "product target"),
        )

    assert traceability_module._normalize_http_url(
        "HTTP://EXAMPLE.TEST:80", "product target"
    ) == ("HTTP://EXAMPLE.TEST:80", "http://example.test/")
    assert traceability_module._normalize_http_url(
        "https://EXAMPLE.TEST:443/path?mode=1#result", "product target"
    ) == (
        "https://EXAMPLE.TEST:443/path?mode=1#result",
        "https://example.test/path?mode=1#result",
    )
    assert traceability_module._normalize_http_url(
        "https://EXAMPLE.TEST/a%20b?next=%2Fdone#value=%7E", "product target"
    ) == (
        "https://EXAMPLE.TEST/a%20b?next=%2Fdone#value=%7E",
        "https://example.test/a%20b?next=%2Fdone#value=%7E",
    )


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

    write(
        state,
        original_state.replace("Semantic Phase: classified", "Semantic Phase: accepted-design")
        .replace("Classification: ui-defect", "Classification: capability-change")
        .replace("Contract Change Required: no", "Contract Change Required: yes"),
    )
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
    contract = load_contract(repo, contract_path)
    structured_text = structured.read_text(encoding="utf-8")
    for field, duplicate in (
        ('"surface": "product",', '"surface": "component-harness", "surface": "product",'),
        (
            '"targetUrl": "http://127.0.0.1:5173/design/42",',
            '"targetUrl": "https://substituted.example/", '
            '"targetUrl": "http://127.0.0.1:5173/design/42",',
        ),
    ):
        write(structured, structured_text.replace(field, duplicate))
        assert_error("duplicate JSON key", lambda: check_traceability(repo, "101", contract, path))
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


def test_duplicate_resolution_report_sections(repo: Path) -> None:
    prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    baseline = report.read_text(encoding="utf-8")
    write(
        report,
        baseline
        + "\n## Closure Conclusion\nConclusion: blocked\nReason: Contradictory duplicate.\n",
    )
    assert_error("duplicate required resolution-report section: ## Closure Conclusion", lambda: check_resolution_report(repo, "101"))

    subsection_cases = (
        (
            "#### Verification Type\nproduct-integration",
            "#### Verification Type\nproduct-integration\n\n#### Verification Type\nmanual",
            "#### Verification Type",
        ),
        (
            "#### Expected Result\nThe contract verification reaches its declared result.",
            "#### Expected Result\nThe contract verification reaches its declared result.\n\n"
            "#### Expected Result\nA contradictory result is expected.",
            "#### Expected Result",
        ),
        (
            "#### Actual Result\nThe declared result was observed in fresh local evidence.",
            "#### Actual Result\nThe declared result was observed in fresh local evidence.\n\n"
            "#### Actual Result\nThe result was not observed.",
            "#### Actual Result",
        ),
        (
            "#### Human Review\n- [ ] Confirm this evidence supports criterion C-001.",
            "#### Human Review\n- [ ] Confirm this evidence supports criterion C-001.\n\n"
            "#### Human Review\n- [ ] Reject the contradictory evidence.",
            "#### Human Review",
        ),
    )
    for old, new, heading in subsection_cases:
        write(report, baseline.replace(old, new, 1))
        assert_error(
            f"duplicate required resolution-report criterion 001 field: {heading}",
            lambda: check_resolution_report(repo, "101"),
        )

    write(
        report,
        baseline.replace(
            "#### Actual Result\nThe declared result was observed in fresh local evidence.",
            "#### Evidence\n- https://object.example/forbidden.json\n\n"
            "#### Actual Result\nThe declared result was observed in fresh local evidence.",
            1,
        ),
    )
    assert_error(
        "duplicate required resolution-report criterion 001 field: #### Evidence",
        lambda: check_resolution_report(repo, "101"),
    )


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


def bind_gap_recognition(repo: Path, state_path: Path, gap_path: Path) -> None:
    state = traceability_module.parse_task_state_text(
        state_path,
        state_path.read_text(encoding="utf-8"),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    candidate = dataclass_replace(
        state,
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase="gap-analysis",
        classification="implementation-gap",
        contract_change_required=False,
        human_approval_ref="none",
    )
    write(state_path, render_task_state(candidate))
    review = approval.prepare(
        repo,
        state.issue,
        "gap-recognition",
        gap_path,
        reviewer="human reviewer",
        force=True,
    )
    approve(review)
    history = approval.consume_gap_recognition(repo, state.issue, gap_path)
    reference = history.relative_to(state_path.parent).as_posix()
    write(
        state_path,
        render_task_state(
            dataclass_replace(
                candidate,
                execution_state="S5_LOCAL_VERIFICATION",
                semantic_phase="gap-recognized",
                human_approval_ref=reference,
            )
        ),
    )


def test_single_authoritative_criterion_source(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    state_path = path.with_name("task-state.md")
    state = traceability_module.parse_task_state_text(state_path, state_path.read_text(encoding="utf-8"), binding_mode="recorded", validate_acceptance=False)
    write(state_path, render_task_state(dataclass_replace(state, classification="implementation-gap")))
    classification = path.with_name("classification.yaml")
    replace(classification, "classification: ui-defect", "classification: implementation-gap")
    replace(classification, "nextArtifact: lightweight-route-complete", "nextArtifact: gap-analysis.md")
    gap = path.with_name("gap-analysis.md")
    write(gap, valid_gap_analysis_text(recognized="no"))
    assert_error("gap-recognized", lambda: check_traceability(repo, "101", contract, path))

    write(gap, valid_gap_analysis_text(include_second=False))
    bind_gap_recognition(repo, state_path, gap)
    assert_error("acceptance criterion does not exist", lambda: check_traceability(repo, "101", contract, path))

    write(gap, valid_gap_analysis_text())
    bind_gap_recognition(repo, state_path, gap)
    check_traceability(repo, "101", contract, path)

    gap.unlink()
    assert_error("missing matching human gap recognition", lambda: check_traceability(repo, "101", contract, path))

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
    write(
        source_path.with_name("task-state.md"),
        render_task_state(
            dataclass_replace(
                source_state,
                semantic_phase="accepted-design",
                classification="capability-change",
                contract_change_required=True,
                human_approval_ref=history_ref,
            )
        ),
    )
    source_classification = source_path.with_name("classification.yaml")
    replace(source_classification, "classification: ui-defect", "classification: capability-change")
    replace(source_classification, "contractChangeRequired: false", "contractChangeRequired: true")
    replace(source_classification, "nextArtifact: lightweight-route-complete", "nextArtifact: contract-change-proposal.md")

    target_path = prepare_valid_chain(repo)
    shutil.copytree(source_path.parent / "approvals", target_path.parent / "approvals", dirs_exist_ok=True)
    target_state = traceability_module.parse_task_state_text(
        target_path.with_name("task-state.md"),
        target_path.with_name("task-state.md").read_text(encoding="utf-8"),
        binding_mode="recorded",
        validate_acceptance=False,
    )
    write(
        target_path.with_name("task-state.md"),
        render_task_state(
            dataclass_replace(
                target_state,
                semantic_phase="accepted-design",
                classification="capability-change",
                contract_change_required=True,
                human_approval_ref=history_ref,
            )
        ),
    )
    target_classification = target_path.with_name("classification.yaml")
    replace(target_classification, "classification: ui-defect", "classification: capability-change")
    replace(target_classification, "contractChangeRequired: false", "contractChangeRequired: true")
    replace(target_classification, "nextArtifact: lightweight-route-complete", "nextArtifact: contract-change-proposal.md")
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
    write(
        state_path,
        render_task_state(
            dataclass_replace(
                state,
                semantic_phase="accepted-design",
                classification="capability-change",
                contract_change_required=True,
                human_approval_ref=history_ref,
            )
        ),
    )
    classification_path = path.with_name("classification.yaml")
    replace(classification_path, "classification: ui-defect", "classification: capability-change")
    replace(classification_path, "contractChangeRequired: false", "contractChangeRequired: true")
    replace(
        classification_path,
        "nextArtifact: lightweight-route-complete",
        "nextArtifact: contract-change-proposal.md",
    )
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


def _prepare_historical_contract_trace(repo: Path) -> tuple[Path, Path, Path]:
    path = prepare_valid_chain(repo)
    contract_path = repo / "contracts" / "contract.yaml"
    contract = load_contract(repo, contract_path)
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
    write(
        state_path,
        render_task_state(
            dataclass_replace(
                state,
                semantic_phase="accepted-design",
                classification="capability-change",
                contract_change_required=True,
                human_approval_ref=history.relative_to(path.parent).as_posix(),
            )
        ),
    )
    classification_path = path.with_name("classification.yaml")
    replace(classification_path, "classification: ui-defect", "classification: capability-change")
    replace(classification_path, "contractChangeRequired: false", "contractChangeRequired: true")
    replace(
        classification_path,
        "nextArtifact: lightweight-route-complete",
        "nextArtifact: contract-change-proposal.md",
    )
    return path, contract_path, history


def _run_trace_cli(repo: Path, matrix: Path, contract: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "xflow", "trace", "check", "--issue", "101", "--matrix", str(matrix)]
    if contract is not None:
        command.extend(("--contract", contract))
    return subprocess.run(
        command,
        cwd=repo,
        env={**os.environ, "DEVCTL_REPO_ROOT": str(repo), "PYTHONPATH": str(OPS_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_historical_contract_trace_cli_survives_deleted_current_contract(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    contract_path.unlink()

    completed = _run_trace_cli(repo, path)
    assert completed.returncode == 0, completed.stderr
    assert "trace check passed" in completed.stdout


def test_historical_contract_trace_cli_accepts_moved_evolution_after_root_migration(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    moved_contract = repo / "specifications" / "contract.yaml"
    moved_contract.parent.mkdir(parents=True)
    contract_path.replace(moved_contract)
    moved_text = moved_contract.read_text(encoding="utf-8")
    moved_text = moved_text.replace("version: 0.1.0", "version: 0.1.1", 1)
    moved_text = moved_text.replace("note: 非规范性背景", "note: 非规范性背景（迁移后路径）", 1)
    write(moved_contract, moved_text)
    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"specifications"}}\n')

    completed = _run_trace_cli(repo, path, "specifications/contract.yaml")
    assert completed.returncode == 0, completed.stderr
    assert "trace check passed" in completed.stdout


def test_historical_contract_trace_rejects_invalid_supplied_evolution(repo: Path) -> None:
    cases = (
        (
            "id: example.contract.capability-name",
            "id: example.contract.unrelated",
            "same contract identity",
        ),
        ("version: 0.1.0", "version: 0.0.9", "non-regressing evolution"),
        ("note: 非规范性背景", "note: Changed without a version advance", "without a version advance"),
        ("status: accepted-design", "status: draft", "accepted contract evolution"),
    )
    for original, replacement, expected in cases:
        path, contract_path, _ = _prepare_historical_contract_trace(repo)
        candidate_path = contract_path.with_name("candidate.yaml")
        shutil.copyfile(contract_path, candidate_path)
        replace(candidate_path, original, replacement)

        completed = _run_trace_cli(repo, path, "contracts/candidate.yaml")
        assert completed.returncode != 0
        assert expected in completed.stderr, completed.stderr


def test_historical_contract_trace_allows_same_version_status_lifecycle(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    candidate_path = contract_path.with_name("active-status.yaml")
    candidate = contract_path.read_text(encoding="utf-8").replace(
        "status: accepted-design",
        "status: active",
        1,
    )
    write(candidate_path, candidate)

    completed = _run_trace_cli(repo, path, "contracts/active-status.yaml")
    assert completed.returncode == 0, completed.stderr


def test_historical_contract_trace_rejects_breaking_change_with_minor_bump(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    candidate_path = contract_path.with_name("breaking-minor.yaml")
    candidate = contract_path.read_text(encoding="utf-8")
    candidate = candidate.replace("version: 0.1.0", "version: 0.2.0", 1)
    candidate = candidate.replace(
        "  version: 0.1.0\n  purpose: 参与者可依赖的业务价值和边界",
        "  version: 1.0.0\n  purpose: 参与者不再获得原有可观察结果",
        1,
    )
    write(candidate_path, candidate)

    completed = _run_trace_cli(repo, path, "contracts/breaking-minor.yaml")
    assert completed.returncode != 0
    assert "under-bumped contract root" in completed.stderr, completed.stderr


def test_historical_contract_trace_rejects_replacement_without_supersedes(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    candidate_path = contract_path.with_name("missing-supersedes.yaml")
    candidate = contract_path.read_text(encoding="utf-8")
    candidate = candidate.replace("version: 0.1.0", "version: 1.0.0", 1)
    candidate = candidate.replace(
        "id: example.verify.case.operation-success",
        "id: example.verify.case.operation-success-v2",
        1,
    )
    write(candidate_path, candidate)

    completed = _run_trace_cli(repo, path, "contracts/missing-supersedes.yaml")
    assert completed.returncode != 0
    assert "one-to-one supersedes" in completed.stderr, completed.stderr


def test_historical_contract_trace_rejects_human_review_ambiguity(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    candidate_path = contract_path.with_name("human-review.yaml")
    candidate = contract_path.read_text(encoding="utf-8")
    candidate = candidate.replace("version: 0.1.0", "version: 0.1.1", 1)
    candidate = candidate.replace(
        "  - kind: issue\n    target: issue-101\n    note: 能力来源记录",
        "  - kind: issue\n    target: issue-101\n    note: 能力来源记录\n"
        "  - kind: issue\n    target: issue-102\n    note: 后续审查来源",
        1,
    )
    write(candidate_path, candidate)

    completed = _run_trace_cli(repo, path, "contracts/human-review.yaml")
    assert completed.returncode != 0
    assert "requires new human acceptance authority" in completed.stderr, completed.stderr
    assert "[WARN] changed contract references require human review" in completed.stderr, completed.stderr


def test_supplied_contract_is_revalidated_at_trace_closure(repo: Path) -> None:
    path, contract_path, _ = _prepare_historical_contract_trace(repo)
    candidate_path = contract_path.with_name("closure-race.yaml")
    candidate_text = contract_path.read_text(encoding="utf-8")
    candidate_text = candidate_text.replace("version: 0.1.0", "version: 0.1.1", 1)
    candidate_text = candidate_text.replace("note: 非规范性背景", "note: 非规范性背景（补充说明）", 1)
    write(candidate_path, candidate_text)
    candidate = load_contract(repo, candidate_path)
    original_verify_closure = traceability_module._verify_closure

    def mutate_supplied_contract(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        write(candidate_path, candidate_text + "\n")
        return result

    with patch.object(traceability_module, "_verify_closure", side_effect=mutate_supplied_contract):
        assert_error(
            "supplied contract changed during closure validation",
            lambda: check_traceability(repo, "101", candidate, path),
        )


def _sealed_acceptance_fixture(
    repo: Path,
) -> tuple[Path, dict[str, object], tuple[object, ...], object]:
    path, _, history = _prepare_historical_contract_trace(repo)
    validated = approval.validate_contract_acceptance_history(repo, history, return_snapshots=True)
    assert isinstance(validated, tuple)
    record, snapshots = validated
    snapshot_path = path.parent / Path(str(record["contractSnapshotFile"]))
    sealed = next(snapshot for snapshot in snapshots if snapshot.path == snapshot_path)
    return path, record, snapshots, sealed


def _record_for_sealed_bytes(record: dict[str, object], content: bytes) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        **record,
        "approvedSha256": digest,
        "contractSha256": digest,
        "contractSnapshotSha256": digest,
    }


def test_sealed_acceptance_loader_rejects_missing_duplicate_and_schema(repo: Path) -> None:
    path, record, snapshots, sealed = _sealed_acceptance_fixture(repo)
    without_sealed = tuple(snapshot for snapshot in snapshots if snapshot is not sealed)
    assert_error(
        "exactly one sealed contract snapshot",
        lambda: traceability_module._load_sealed_acceptance_contract(repo, path.parent, record, without_sealed),
    )
    assert_error(
        "exactly one sealed contract snapshot",
        lambda: traceability_module._load_sealed_acceptance_contract(repo, path.parent, record, snapshots + (sealed,)),
    )

    invalid_content = b"not: [valid contract yaml\n"
    invalid_snapshot = dataclass_replace(sealed, content=invalid_content)
    invalid_snapshots = tuple(invalid_snapshot if snapshot is sealed else snapshot for snapshot in snapshots)
    assert_error(
        "invalid sealed contract snapshot",
        lambda: traceability_module._load_sealed_acceptance_contract(
            repo,
            path.parent,
            _record_for_sealed_bytes(record, invalid_content),
            invalid_snapshots,
        ),
    )


def test_sealed_acceptance_loader_rejects_status_and_objects(repo: Path) -> None:
    path, record, snapshots, sealed = _sealed_acceptance_fixture(repo)
    assert sealed.content is not None
    draft_content = sealed.content.replace(b"status: accepted-design", b"status: draft")
    assert draft_content != sealed.content
    draft_snapshot = dataclass_replace(sealed, content=draft_content)
    draft_snapshots = tuple(draft_snapshot if snapshot is sealed else snapshot for snapshot in snapshots)
    assert_error(
        "sealed contract status must be accepted-design",
        lambda: traceability_module._load_sealed_acceptance_contract(
            repo,
            path.parent,
            _record_for_sealed_bytes(record, draft_content),
            draft_snapshots,
        ),
    )

    invalid_objects = {**record, "acceptedObjects": [*record["acceptedObjects"], "example.object.missing"]}
    assert_error(
        "sealed contract accepted object set mismatch",
        lambda: traceability_module._load_sealed_acceptance_contract(repo, path.parent, invalid_objects, snapshots),
    )


def test_historical_contract_trace_uses_sealed_acceptance(repo: Path) -> None:
    for mutation in (
        lambda contract_path: replace(contract_path, "status: accepted-design", "status: active"),
        lambda contract_path: replace(contract_path, "version: 0.1.0", "version: 0.2.0"),
        lambda contract_path: contract_path.unlink(),
    ):
        path, contract_path, _ = _prepare_historical_contract_trace(repo)
        mutation(contract_path)
        check_traceability(repo, "101", None, path)

    path, _, history = _prepare_historical_contract_trace(repo)
    import yaml

    record = yaml.safe_load(history.read_text(encoding="utf-8"))
    sealed_contract = path.parent / record["contractSnapshotFile"]
    write(sealed_contract, sealed_contract.read_text(encoding="utf-8") + "\nchanged after acceptance\n")
    assert_error("archived contract snapshot SHA256 mismatch", lambda: check_traceability(repo, "101", None, path))

    matrix_mismatches = (
        ("  id: example.contract.capability-name", "  id: example.contract.other"),
        ("  version: 0.1.0", "  version: 0.2.0"),
        ("  file: contracts/contract.yaml", "  file: contracts/other.yaml"),
    )
    for original, replacement in matrix_mismatches:
        path, _, _ = _prepare_historical_contract_trace(repo)
        replace(path, original, replacement)
        assert_error(
            "task-state Contract does not match matrix contract id/version/file",
            lambda: check_traceability(repo, "101", None, path),
        )


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


def test_final_authority_and_git_revalidation(repo: Path) -> None:
    path = prepare_valid_chain(repo)
    report = write_resolution_report(repo)
    bindings = resolve_bindings(repo)
    authority = task_authority_file(repo, bindings.worktree, "101")
    original_authority = authority.read_text(encoding="utf-8")
    original_verify_closure = traceability_module._verify_closure

    def mutate_authority(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        write(authority, original_authority + "\n")
        return result

    try:
        with patch.object(traceability_module, "_verify_closure", side_effect=mutate_authority):
            assert_error("task authority changed during closure validation", lambda: check_resolution_report(repo, "101", report))
    finally:
        write(authority, original_authority)

    legacy_pointer = legacy_active_task_pointer_file(repo, bindings.worktree)
    active_pointer = active_task_pointer_file(repo, bindings.worktree)

    def create_legacy_pointer(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        write(legacy_pointer, active_pointer.read_text(encoding="utf-8"))
        return result

    try:
        with patch.object(traceability_module, "_verify_closure", side_effect=create_legacy_pointer):
            assert_error("legacy active task pointer changed during closure validation", lambda: check_resolution_report(repo, "101", report))
    finally:
        legacy_pointer.unlink(missing_ok=True)

    branch = "feature/closure-race"

    def switch_branch(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        git(repo, "checkout", "-b", branch, "-q")
        return result

    try:
        with patch.object(traceability_module, "_verify_closure", side_effect=switch_branch):
            assert_error("Git branch changed during closure validation", lambda: check_resolution_report(repo, "101", report))
    finally:
        if resolve_bindings(repo).branch == branch:
            git(repo, "checkout", "main", "-q")
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def advance_head(*args: object, **kwargs: object) -> object:
        result = original_verify_closure(*args, **kwargs)
        git(repo, "commit", "--allow-empty", "-m", "test: advance closure HEAD", "-q")
        return result

    with patch.object(traceability_module, "_verify_closure", side_effect=advance_head):
        assert_error("Git HEAD changed during closure validation", lambda: check_resolution_report(repo, "101", report))

    real_run = subprocess.run

    def fail_head(command: object, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(command, list) and command[-3:] == ["rev-parse", "--verify", "HEAD"]:
            return subprocess.CompletedProcess(command, 128, "", "fatal: corrupt HEAD")
        return real_run(command, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(subprocess, "run", side_effect=fail_head):
        assert_error("cannot determine current Git HEAD", lambda: check_resolution_report(repo, "101", report))

    unborn = repo.parent / "unborn-head"
    git(repo.parent, "init", "-q", str(unborn))
    git(unborn, "checkout", "-b", "feature/unborn", "-q")
    assert traceability_module._git_head(unborn) is None


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
    source = (
        "# XFlow Current Task\n\nIssue: legacy\nState: S5_LOCAL_VERIFICATION\n\n"
        "## Allowed Actions\n- verify\n\n## Forbidden Actions\n- push\n"
    )
    write(current_task, source)
    check_resolution_report(repo, "legacy")

    write(current_task, "# XFlow Current Task\n\nIssue: other\nState: S5_LOCAL_VERIFICATION\n")
    assert_error("legacy current task Issue mismatch", lambda: check_resolution_report(repo, "legacy"))

    current_task.unlink()
    assert_error("missing legacy current task", lambda: check_resolution_report(repo, "legacy"))

    write(current_task, source)
    migrate_legacy_current_task(repo)
    check_resolution_report(repo, "legacy")
    task_path = issue_root / "task-state.md"
    original_state = task_path.read_text(encoding="utf-8")

    current_task.unlink()
    assert_error("missing current task state file", lambda: check_resolution_report(repo, "legacy"))
    write(current_task, source.replace("- verify", "- inspect"))
    assert_error("legacy task authority source provenance mismatch", lambda: check_resolution_report(repo, "legacy"))
    write(current_task, source)
    write(task_path, original_state.replace("Base: main", "Base: develop"))
    assert_error("canonical migrated task-state", lambda: check_resolution_report(repo, "legacy"))
    write(task_path, original_state)

    original_revalidate = traceability_module._revalidate

    def mutate_legacy_source(*args: object, **kwargs: object) -> object:
        write(current_task, source.replace("- verify", "- race"))
        return original_revalidate(*args, **kwargs)

    try:
        with patch.object(traceability_module, "_revalidate", side_effect=mutate_legacy_source):
            assert_error("legacy current task changed during closure validation", lambda: check_resolution_report(repo, "legacy"))
    finally:
        write(current_task, source)


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
    replacement_id = "example.verify.case.operation-success-v2"
    write(contract_path, contract_text.replace("example.verify.case.operation-success", replacement_id, 1))
    matrix = path.read_text(encoding="utf-8").replace("example.verify.case.operation-success", replacement_id, 1)
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
        test_capability_closure_requires_semantic_exit(repo)
        test_schema_and_reference_rejections(repo)
        test_path_and_evidence_rejections(repo)
        test_ui_identity_rejections(repo)
        test_strict_product_url_identity()
        test_resolution_consistency(repo)
        test_durable_closure_and_authoritative_bindings(repo)
        test_evidence_identity_and_digest_rejections(repo)
        test_contract_derived_ui_obligations(repo)
        test_criteria_schema_and_exact_conclusions(repo)
        test_duplicate_resolution_report_sections(repo)
        test_single_authoritative_criterion_source(repo)
        test_current_repository_acceptance_binding(repo, root)
        test_snapshot_content_and_transitive_revalidation(repo)
        test_historical_contract_trace_cli_survives_deleted_current_contract(repo)
        test_historical_contract_trace_cli_accepts_moved_evolution_after_root_migration(repo)
        test_historical_contract_trace_rejects_invalid_supplied_evolution(repo)
        test_historical_contract_trace_allows_same_version_status_lifecycle(repo)
        test_historical_contract_trace_rejects_breaking_change_with_minor_bump(repo)
        test_historical_contract_trace_rejects_replacement_without_supersedes(repo)
        test_historical_contract_trace_rejects_human_review_ambiguity(repo)
        test_supplied_contract_is_revalidated_at_trace_closure(repo)
        test_sealed_acceptance_loader_rejects_missing_duplicate_and_schema(repo)
        test_sealed_acceptance_loader_rejects_status_and_objects(repo)
        test_historical_contract_trace_uses_sealed_acceptance(repo)
        test_repository_collaboration_lock(repo)
        test_final_authority_and_git_revalidation(repo)
        test_legacy_resolution_still_binds_current_issue(repo)
        test_provider_endpoint_families(repo)
        test_caller_specific_size_limits(repo)
        test_exact_schema_rejections(repo)
        test_canonical_ids_object_store_variants_and_race(repo)
    print("trace core ok")


if __name__ == "__main__":
    main()
