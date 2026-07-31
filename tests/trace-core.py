from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.contracts import load_contract
from xflow.traceability import check_traceability, check_traceability_resolution


CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
MATRIX_FIXTURE = Path(__file__).parent / "fixtures" / "traceability" / "valid.yaml"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


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
    return repo


def matrix_path(repo: Path, issue: str = "101") -> Path:
    return repo / ".xflow" / "issues" / f"issue-{issue}" / "traceability-matrix.yaml"


def prepare_valid_chain(repo: Path, issue: str = "101") -> Path:
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
        "evidence/screenshots/c-001-after.png",
        "evidence/dom/c-001-after.json",
    ):
        write(target.parent / relative, f"stable trace fixture: {relative}\n")
    return target


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
        ("unknown-contract", "id: example.contract.capability-name", "id: other.contract", "matrix contract.id does not match"),
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
        ("missing-test", "tests/test_operation.py", "tests/missing.py", "referenced file does not exist"),
        ("absolute", "tests/test_operation.py", "C:/temp/test.py", "must be relative"),
        ("parent", "tests/test_operation.py", "../outside.py", "must not contain '..'"),
        ("remote", "tests/test_operation.py", "https://example.test/test.py", "must not use URL/COS/OSS"),
        ("outside", "evidence/api/operation-after.json", "evidence/../outside.json", "must not contain '..'"),
        ("no-after", "after: [evidence/api/operation-after.json]", "after: []", "resolved evidence.after must be non-empty"),
        ("reuse-before", "evidence/api/operation-after.json", "evidence/api/operation-before.json", "must not reuse before evidence"),
    )
    for name, old, new, expected in cases:
        assert old in baseline, name
        write(path, baseline.replace(old, new))
        assert_error(expected, lambda: check_traceability(repo, "101", contract, path))

    blocked_without_reason = baseline.replace("conclusion: resolved\n", "conclusion: blocked\n", 1).replace("after: [evidence/api/operation-after.json]", "after: []", 1)
    write(path, blocked_without_reason)
    assert_error("blocked entry requires a meaningful blocker", lambda: check_traceability(repo, "101", contract, path))
    write(path, blocked_without_reason.replace("conclusion: blocked\n", "conclusion: blocked\n    blocker: external environment is unavailable\n", 1))
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
    report_evidence = {
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "api" / "operation-after.json",
        repo / ".xflow" / "issues" / "issue-101" / "evidence" / "api" / "rejection-after.json",
    }
    check_traceability_resolution(repo, "101", "resolved", report_evidence)
    replace(path, "conclusion: resolved\n", "conclusion: reduced\n",)
    assert_error("resolved resolution-report requires every trace entry to be resolved", lambda: check_traceability_resolution(repo, "101", "resolved", report_evidence))
    check_traceability_resolution(repo, "101", "reduced", report_evidence)
    replace(path, "conclusion: reduced\n", "conclusion: blocked\n",)
    assert_error("reduced resolution-report cannot contain blocked trace entries", lambda: check_traceability_resolution(repo, "101", "reduced", report_evidence))
    check_traceability_resolution(repo, "101", "blocked", set())
    shutil.copyfile(MATRIX_FIXTURE, path)
    assert_error("must reference every trace after-evidence file", lambda: check_traceability_resolution(repo, "101", "reduced", {next(iter(report_evidence))}))


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = init_repo(Path(raw))
        test_valid_chain_and_cli(repo)
        test_schema_and_reference_rejections(repo)
        test_path_and_evidence_rejections(repo)
        test_ui_identity_rejections(repo)
        test_resolution_consistency(repo)
    print("trace core ok")


if __name__ == "__main__":
    main()
