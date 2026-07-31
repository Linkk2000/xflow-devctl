from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import traceability as traceability_module
from xflow.checks import check_resolution_report
from xflow.contracts import load_contract
from xflow.task_state import TaskState, render_task_state
from xflow.traceability import check_traceability, check_traceability_resolution


CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
MATRIX_FIXTURE = Path(__file__).parent / "fixtures" / "traceability" / "valid.yaml"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


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
    write_bytes(target.parent / "evidence" / "screenshots" / "c-001-after.png", b"\x89PNG\r\n\x1a\ntrace-image")
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
        classification="implementation-gap",
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
classification: implementation-gap
contractChangeRequired: false
reason: The implementation must close the existing contract.
nextArtifact: gap-analysis.md
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
{'ui' if number == '001' else 'non-ui'}

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
{conclusion}: trace conclusions match this report.

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
        ("harness-claim", "surface: product", "surface: component-harness", "product-integration requires ui.surface: product"),
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
    original_state = state.read_text(encoding="utf-8")
    write(state, original_state.replace("Contract: example.contract.capability-name@0.1.0", "Contract: unrelated.contract@9.9.9"))
    contract = load_contract(repo, repo / "contracts" / "contract.yaml")
    assert_error("task-state Contract does not match matrix contract", lambda: check_traceability(repo, "101", contract, path))
    write(state, original_state)

    write(state, original_state.replace("Semantic Phase: classified", "Semantic Phase: accepted-design"))
    assert_error("Human Approval Ref is required", lambda: check_traceability(repo, "101", contract, path))
    write(state, original_state)

    classification = path.with_name("classification.yaml")
    original_classification = classification.read_text(encoding="utf-8")
    write(classification, original_classification.replace("refs: [contracts/contract.yaml]", "refs: [contracts/unrelated.yaml]"))
    assert_error("classification contractSearch.refs", lambda: check_traceability(repo, "101", contract, path))
    write(classification, original_classification)


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
    assert_error("supported PNG/JPEG/WebP signature", lambda: check_traceability(repo, "101", contract, path))
    write_bytes(screenshot, b"\x89PNG\r\n\x1a\ntrace-image")

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
        repo = init_repo(Path(raw))
        test_valid_chain_and_cli(repo)
        test_schema_and_reference_rejections(repo)
        test_path_and_evidence_rejections(repo)
        test_ui_identity_rejections(repo)
        test_resolution_consistency(repo)
        test_durable_closure_and_authoritative_bindings(repo)
        test_evidence_identity_and_digest_rejections(repo)
    test_contract_derived_ui_obligations(repo)
    test_criteria_schema_and_exact_conclusions(repo)
    test_exact_schema_rejections(repo)
    test_canonical_ids_object_store_variants_and_race(repo)
    print("trace core ok")


if __name__ == "__main__":
    main()
