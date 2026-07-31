from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval
from xflow.contracts import ContractDocument, load_contract, validate_contract_acceptance
from xflow.task_state import TaskState, parse_task_state, render_task_state


FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def assert_value_error(expected: str, callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def copied_contract(repo_root: Path, name: str = "contract.yaml") -> Path:
    path = repo_root / "contracts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, path)
    return path


def replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8", newline="\n")


def configure_contract_root(repo_root: Path) -> None:
    write(repo_root / ".xflow" / "xflow.json", '{"contracts":{"root":"contracts"}}\n')


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# Contract fixture\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize contract fixture", "-q")
    git(repo, "checkout", "-b", "feature/101-contract", "-q")
    configure_contract_root(repo)
    return repo


def current_task(issue: str) -> str:
    return (
        "# XFlow Current Task\n\n"
        f"Issue: {issue}\n"
        "State: S2_REMOTE_ISSUE_CREATED\n\n"
        "## Allowed Actions\n"
        "- Review the contract.\n\n"
        "## Forbidden Actions\n"
        "- Push.\n"
    )


def task_state(issue: str, branch: str, approval_ref: str) -> TaskState:
    return TaskState(
        issue=issue,
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase="accepted-design",
        classification="capability-change",
        contract="example.contract.capability-name@0.1.0",
        contract_file="contracts/contract.yaml",
        contract_change_required=True,
        branch=branch,
        base="main",
        allowed_actions=("review-contract",),
        forbidden_actions=("implement",),
        human_gate="human contract acceptance required",
        human_approval_ref=approval_ref,
    )


def test_valid_contract_and_owned_object_locations(repo: Path) -> ContractDocument:
    path = copied_contract(repo)
    contract = load_contract(repo, path)
    assert isinstance(contract, ContractDocument)
    assert contract.path == path.resolve()
    assert contract.raw["id"] == "example.contract.capability-name"
    assert contract.raw["created"] == "2026-07-30"
    assert len(contract.objects_by_id) == 15
    assert set(contract.verification_by_id) == {
        "example.verify.case.operation-success",
        "example.verify.case.operation-rejection",
    }
    assert "issue-101" not in contract.objects_by_id
    return contract


def test_schema_and_semantic_rejections(repo: Path) -> None:
    cases = (
        ("duplicate", "id: example.value.result", "id: example.value.request", "duplicate contract object id"),
        ("broken-ref", "participants: [example.role.operator]", "participants: [example.role.missing]", "missing referenced contract object"),
        ("semver", "version: 0.1.0", "version: '0.1'", "invalid semantic version"),
        ("interaction", "    constraints: [example.constraint.preserve-state-on-rejection]\n", "", "interactionContracts item missing required fields: constraints"),
        ("uncovered", "      - example.constraint.preserve-state-on-rejection\n", "", "core constraint has no verification trace"),
        ("blocker", "requiredBefore: never\n    status: deferred", "requiredBefore: accepted-design\n    status: open", "blocking precondition"),
        ("future", "traces: [example.interaction.perform-operation]", "traces: [example.future.optional-extension]", "future capability cannot be required by current verification"),
        ("unknown", "note: 非规范性背景", "note: 非规范性背景\nunexpected: field", "contract document contains unexpected fields"),
        ("placeholder", "purpose: 参与者可依赖的业务价值和边界", "purpose: TODO", "must be meaningful"),
    )
    for name, old, new, expected in cases:
        path = copied_contract(repo, f"{name}.yaml")
        replace(path, old, new)
        assert_value_error(expected, lambda path=path: load_contract(repo, path))

    implementation = copied_contract(repo, "implementation-blocker.yaml")
    replace(implementation, "status: accepted-design", "status: active")
    replace(implementation, "requiredBefore: never\n    status: deferred", "requiredBefore: implementation\n    status: open")
    assert_value_error("blocking precondition", lambda: load_contract(repo, implementation))


def test_optional_contract_lists_may_be_empty(repo: Path) -> None:
    path = copied_contract(repo, "optional-empty.yaml")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    for field in (
        "engineeringProjections",
        "dependsOn",
        "preconditionsToResolve",
        "futureCapabilitiesOutOfScope",
        "references",
    ):
        payload[field] = []
    write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    assert load_contract(repo, path).raw["engineeringProjections"] == []


def test_contract_root_containment(repo: Path) -> None:
    outside = repo.parent / "outside.yaml"
    shutil.copyfile(FIXTURE, outside)
    assert_value_error("contract file must stay under contracts.root", lambda: load_contract(repo, outside))


def test_exact_local_acceptance_and_task_state(repo: Path, contract: ContractDocument) -> None:
    issue = "101"
    write(repo / ".xflow" / "current-task.md", current_task(issue))
    state_path = repo / ".xflow" / "issues" / "issue-101" / "task-state.md"
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", "approvals/history/missing.yaml")))
    assert_value_error("missing matching human contract acceptance", lambda: parse_task_state(state_path))
    assert_value_error(
        "local review approval required",
        lambda: validate_contract_acceptance(repo, issue, contract, ("example.capability.capability-name",)),
    )

    review = approval.prepare(repo, issue, "contract-acceptance", contract.path, reviewer="reviewer", force=True)
    review.write_text(review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8", newline="\n")
    grant = approval.require_exact_remote(repo, "contract-acceptance", contract.path, issue)
    assert_value_error(
        "must use contract acceptance history",
        lambda: approval.record_consumed_approval(repo, grant, "success"),
    )
    accepted = validate_contract_acceptance(
        repo,
        issue,
        contract,
        ("example.capability.capability-name", "example.verify.case.operation-success"),
    )
    assert accepted.parent.name == "history"
    record = accepted.read_text(encoding="utf-8")
    assert "action: contract-acceptance" in record
    assert "source: local-review" in record
    assert "contractSha256:" in record
    assert "acceptedObjects:" in record
    assert contract.path.read_bytes() == FIXTURE.read_bytes()
    assert "Semantic Phase: accepted-design" in state_path.read_text(encoding="utf-8")

    reference = accepted.relative_to(state_path.parent).as_posix()
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", reference)))
    assert parse_task_state(state_path).human_approval_ref == reference
    assert_value_error(
        "approval already consumed",
        lambda: validate_contract_acceptance(repo, issue, contract, ("example.capability.capability-name",)),
    )
    assert_value_error(
        "accepted contract object does not exist",
        lambda: validate_contract_acceptance(repo, issue, contract, ("example.missing",)),
    )
    replace(contract.path, "note: 非规范性背景", "note: 合同内容已变更")
    assert_value_error("missing matching human contract acceptance", lambda: parse_task_state(state_path))


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = init_repo(Path(raw))
        contract = test_valid_contract_and_owned_object_locations(repo)
        test_schema_and_semantic_rejections(repo)
        test_optional_contract_lists_may_be_empty(repo)
        test_contract_root_containment(repo)
        test_exact_local_acceptance_and_task_state(repo, contract)
    print("contract core ok")


if __name__ == "__main__":
    main()
