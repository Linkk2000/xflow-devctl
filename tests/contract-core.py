from __future__ import annotations

import builtins
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

import yaml


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval
from xflow import contracts as contracts_module
from xflow import project_config
from xflow.contracts import ContractDocument, load_contract, validate_contract_acceptance
from xflow.task_state import (
    TaskState,
    activate_task,
    list_task_states,
    migrate_legacy_current_task,
    parse_task_state,
    render_task_state,
)


FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
ACCEPTED_OBJECTS = (
    "example.capability.capability-name",
    "example.verify.case.operation-success",
)


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def approve(path: Path) -> None:
    write(path, path.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))


def run_devctl(repo_root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}: {' '.join(args)}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


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


def activate_legacy_current_task(repo: Path, issue: str) -> TaskState:
    write(repo / ".xflow" / "current-task.md", current_task(issue))
    return migrate_legacy_current_task(repo)


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
    assert contract.raw_bytes == path.read_bytes()
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
        ("semver", "version: 0.1.0", "version: '0.1'", "contract schema validation failed"),
        ("interaction", "    constraints: [example.constraint.preserve-state-on-rejection]\n", "", "contract schema validation failed"),
        ("uncovered", "      - example.constraint.preserve-state-on-rejection\n", "", "core constraint has no verification trace"),
        ("blocker", "requiredBefore: never\n    status: deferred", "requiredBefore: accepted-design\n    status: open", "blocking precondition"),
        ("future", "traces: [example.interaction.perform-operation]", "traces: [example.future.optional-extension]", "future capability cannot be required by current verification"),
        ("unknown", "note: 非规范性背景", "note: 非规范性背景\nunexpected: field", "contract schema validation failed"),
        ("placeholder", "purpose: 参与者可依赖的业务价值和边界", "purpose: TODO", "contract schema validation failed"),
    )
    for name, old, new, expected in cases:
        path = copied_contract(repo, f"{name}.yaml")
        replace(path, old, new)
        assert_value_error(expected, lambda path=path: load_contract(repo, path))

    implementation = copied_contract(repo, "implementation-blocker.yaml")
    replace(implementation, "status: accepted-design", "status: active")
    replace(implementation, "requiredBefore: never\n    status: deferred", "requiredBefore: implementation\n    status: open")
    assert_value_error("blocking precondition", lambda: load_contract(repo, implementation))


def test_schema_semantic_parity(repo: Path) -> None:
    cases = (
        ("padded-status", "status: accepted-design", "status: ' accepted-design '"),
        ("padded-open", "    status: deferred", "    status: ' open '"),
        ("invalid-date", "created: 2026-07-30", "created: '2026-02-30'"),
        ("compact-date", "created: 2026-07-30", "created: '20260730'"),
        ("terminal-lf-date", "created: 2026-07-30", 'created: "2026-07-30\\n"'),
        ("terminal-cr-date", "created: 2026-07-30", 'created: "2026-07-30\\r"'),
        ("placeholder", "name: 能力名称", "name: ToDo"),
        ("ascii-folded-placeholder", "name: 能力名称", "name: UnKnOwN"),
        ("chinese-placeholder", "name: 能力名称", "name: 待定"),
        ("whitespace-id", "id: example.value.request", "id: 'example.value request'"),
        ("leading-id", "id: example.value.request", "id: ' example.value.request'"),
        ("trailing-id", "id: example.value.request", "id: 'example.value.request '"),
        ("leading-text", "name: 能力名称", "name: ' 能力名称'"),
        ("trailing-text", "name: 能力名称", "name: '能力名称 '"),
        ("terminal-lf-text", "name: 能力名称", 'name: "能力名称\\n"'),
        ("terminal-cr-text", "name: 能力名称", 'name: "能力名称\\r"'),
        ("terminal-lf-id", "id: example.value.request", 'id: "example.value.request\\n"'),
        ("terminal-cr-id", "id: example.value.request", 'id: "example.value.request\\r"'),
        ("terminal-lf-semver", "version: 0.1.0", 'version: "0.1.0\\n"'),
        ("unknown-root", "note: 非规范性背景", "note: 非规范性背景\nsurprise: field"),
    )
    for name, old, new in cases:
        path = copied_contract(repo, f"parity-{name}.yaml")
        replace(path, old, new)
        raw = contracts_module._parse_contract_yaml(path.read_text(encoding="utf-8-sig"))
        assert_value_error("contract schema validation failed", lambda raw=raw: contracts_module._validate_contract_schema(raw))
        assert_value_error("", lambda raw=raw, path=path: contracts_module._build_document(path, raw, path.read_bytes()))

    valid_unicode_cases = (
        ("unicode-text", "name: 能力名称", "name: '能力 名称 Ω'", "name", "能力 名称 Ω"),
        ("kelvin-text", "name: 能力名称", "name: unKnown", "name", "unKnown"),
        (
            "kelvin-id",
            "id: example.contract.capability-name",
            "id: unKnown",
            "id",
            "unKnown",
        ),
        ("long-s-text", "name: 能力名称", "name: 'ſcope boundary'", "name", "ſcope boundary"),
    )
    for name, old, new, field_name, expected in valid_unicode_cases:
        unicode_path = copied_contract(repo, f"parity-valid-{name}.yaml")
        replace(unicode_path, old, new)
        unicode_raw = contracts_module._parse_contract_yaml(unicode_path.read_text(encoding="utf-8-sig"))
        contracts_module._validate_contract_schema(unicode_raw)
        document = contracts_module._build_document(unicode_path, unicode_raw, unicode_path.read_bytes())
        assert document.raw[field_name] == expected

    original_import = builtins.__import__

    def without_jsonschema(name: str, *args: object, **kwargs: object) -> object:
        if name == "jsonschema":
            raise ImportError("blocked for dependency diagnostic")
        return original_import(name, *args, **kwargs)

    valid_raw = contracts_module._parse_contract_yaml(FIXTURE.read_text(encoding="utf-8"))
    with patch("builtins.__import__", side_effect=without_jsonschema):
        assert_value_error("contract checks require jsonschema", lambda: contracts_module._validate_contract_schema(valid_raw))


def test_stage_blockers_reject_padded_open(repo: Path) -> None:
    stages = (
        ("accepted-design", "requiredBefore: never", "requiredBefore: accepted-design", None),
        ("engineering-projection", "requiredBefore: never", "requiredBefore: engineering-projection", None),
        ("implementation", "requiredBefore: never", "requiredBefore: implementation", ("status: accepted-design", "status: active")),
    )
    for name, old_stage, new_stage, status_change in stages:
        path = copied_contract(repo, f"padded-open-{name}.yaml")
        replace(path, old_stage, new_stage)
        replace(path, "    status: deferred", "    status: ' open '")
        if status_change is not None:
            replace(path, *status_change)
        assert_value_error("contract schema validation failed", lambda path=path: load_contract(repo, path))


def test_supersedes_rules(repo: Path) -> None:
    cases = (
        (
            "self",
            "      rule: 请求被拒绝时既有业务状态保持不变",
            "      rule: 请求被拒绝时既有业务状态保持不变\n      supersedes: [example.constraint.preserve-state-on-rejection]",
            "must not supersede itself",
        ),
        (
            "wrong-kind",
            "      rule: 请求被拒绝时既有业务状态保持不变",
            "      rule: 请求被拒绝时既有业务状态保持不变\n      supersedes: [example.context.operation]",
            "must have the same kind",
        ),
        (
            "current-future",
            "    reason: 不属于当前承诺且不进入当前验证",
            "    reason: 不属于当前承诺且不进入当前验证\n    supersedes: [example.interaction.perform-operation]",
            "must not cross current and future objects",
        ),
    )
    for name, old, new, expected in cases:
        path = copied_contract(repo, f"supersedes-{name}.yaml")
        replace(path, old, new)
        assert_value_error(expected, lambda path=path: load_contract(repo, path))

    historical = copied_contract(repo, "supersedes-historical.yaml")
    replace(
        historical,
        "      rule: 请求被拒绝时既有业务状态保持不变",
        "      rule: 请求被拒绝时既有业务状态保持不变\n      supersedes: [example.constraint.retired-predecessor]",
    )
    assert load_contract(repo, historical).objects_by_id["example.constraint.preserve-state-on-rejection"]


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
    accepted_state = task_state(issue, "feature/101-contract", "approvals/history/missing.yaml")
    pre_acceptance_state = dataclass_replace(
        accepted_state,
        semantic_phase="classified",
        human_approval_ref="none",
    )
    write(state_path, render_task_state(pre_acceptance_state))
    activate_task(repo, issue)
    write(state_path, render_task_state(accepted_state))
    assert_value_error("missing matching human contract acceptance", lambda: parse_task_state(state_path))
    write(state_path, render_task_state(pre_acceptance_state))
    assert_value_error(
        "local review approval required",
        lambda: validate_contract_acceptance(repo, issue, contract, ("example.capability.capability-name",)),
    )

    assert_value_error(
        "accepted object IDs",
        lambda: approval.prepare(repo, issue, "contract-acceptance", contract.path, reviewer="reviewer", force=True),
    )
    review = approval.prepare(
        repo,
        issue,
        "contract-acceptance",
        contract.path,
        reviewer="reviewer",
        force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    review_text = review.read_text(encoding="utf-8")
    assert "acceptedObjects:" in review_text
    for identifier in ACCEPTED_OBJECTS:
        assert identifier in review_text
    approve(review)
    grant = approval.require_exact_remote(repo, "contract-acceptance", contract.path, issue)
    assert grant.accepted_objects == ACCEPTED_OBJECTS
    assert_value_error(
        "must use contract acceptance history",
        lambda: approval.record_consumed_approval(repo, grant, "success"),
    )
    assert_value_error(
        "must consume the prepared local review",
        lambda: approval.record_contract_acceptance(
            repo,
            dataclass_replace(grant, approval_id="a" * 32),
            contract_id="example.contract.capability-name",
            contract_version="0.1.0",
            contract_sha256=contract.sha256,
            accepted_objects=ACCEPTED_OBJECTS,
        ),
    )
    assert_value_error(
        "accepted object set mismatch",
        lambda: validate_contract_acceptance(repo, issue, contract, (ACCEPTED_OBJECTS[0],)),
    )
    accepted = validate_contract_acceptance(
        repo,
        issue,
        contract,
        ACCEPTED_OBJECTS,
    )
    assert accepted.parent.name == "history"
    record = accepted.read_text(encoding="utf-8")
    assert "action: contract-acceptance" in record
    assert "source: local-review" in record
    assert "contractSha256:" in record
    assert "acceptedObjects:" in record
    assert "approvedReviewFile:" in record
    assert "approvedReviewSha256:" in record
    assert "approvalClaimFile:" in record
    assert "approvalClaimSha256:" not in record
    assert contract.path.read_bytes() == FIXTURE.read_bytes()
    assert "Semantic Phase: classified" in state_path.read_text(encoding="utf-8")

    reference = accepted.relative_to(state_path.parent).as_posix()
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", reference)))
    assert parse_task_state(state_path).human_approval_ref == reference
    assert_value_error(
        "approval already consumed",
        lambda: validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS),
    )
    assert_value_error(
        "accepted contract object does not exist",
        lambda: validate_contract_acceptance(repo, issue, contract, ("example.missing",)),
    )

    original_history = accepted.read_bytes()
    accepted.write_bytes(original_history + b"contractId: forged.contract\n")
    assert_value_error("duplicate YAML key", lambda: parse_task_state(state_path))
    accepted.write_bytes(original_history)

    payload = yaml.safe_load(original_history.decode("utf-8"))
    issue_root = state_path.parent
    claim_path = issue_root / payload["approvalClaimFile"]
    original_claim = claim_path.read_bytes()
    claim_payload = yaml.safe_load(original_claim.decode("utf-8"))
    history_reference = accepted.relative_to(issue_root).as_posix()
    assert claim_payload["historyFile"] == history_reference
    assert claim_payload["historySha256"] == hashlib.sha256(original_history).hexdigest()
    assert claim_payload["recordedAt"] == payload["recordedAt"]
    assert claim_payload["approvedReviewFile"] == payload["approvedReviewFile"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", claim_payload["claimedAt"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", claim_payload["recordedAt"])
    archived_review = issue_root / payload["approvedReviewFile"]
    original_review = archived_review.read_bytes()
    approved_at = approval.field(original_review.decode("utf-8"), "Approved At")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", approved_at)
    archived_review.write_bytes(original_review + b"\nmutated: yes\n")
    assert_value_error("archived approved review SHA256 mismatch", lambda: parse_task_state(state_path))
    archived_review.write_bytes(original_review)

    rejected_review = original_review.replace(b"Approved: yes", b"Approved: no")
    rejected_review_sha = hashlib.sha256(rejected_review).hexdigest()
    rewritten_history = dict(payload)
    rewritten_history["approvedReviewSha256"] = rejected_review_sha
    rewritten_history_bytes = yaml.safe_dump(rewritten_history, sort_keys=False).encode("utf-8")
    rewritten_claim_payload = dict(claim_payload)
    rewritten_claim_payload["approvedReviewSha256"] = rejected_review_sha
    rewritten_claim_payload["historySha256"] = hashlib.sha256(rewritten_history_bytes).hexdigest()
    rewritten_claim = yaml.safe_dump(rewritten_claim_payload, sort_keys=False).encode("utf-8")
    archived_review.write_bytes(rejected_review)
    claim_path.write_bytes(rewritten_claim)
    accepted.write_bytes(rewritten_history_bytes)
    assert_value_error("local approval required: Approved: yes", lambda: parse_task_state(state_path))
    archived_review.write_bytes(original_review)
    claim_path.write_bytes(original_claim)
    accepted.write_bytes(original_history)

    noncanonical_claim = dict(claim_payload)
    noncanonical_claim["claimedAt"] = str(noncanonical_claim["claimedAt"]).replace("Z", "+00:00")
    claim_path.write_bytes(yaml.safe_dump(noncanonical_claim, sort_keys=False).encode("utf-8"))
    assert_value_error("claimedAt must be canonical UTC", lambda: parse_task_state(state_path))
    claim_path.write_bytes(original_claim)

    future_review = original_review.replace(
        f"Approved At: {approved_at}".encode("utf-8"),
        b"Approved At: 2099-01-01T00:00:00Z",
    )
    future_review_sha = hashlib.sha256(future_review).hexdigest()
    chronology_history = dict(payload)
    chronology_history["approvedReviewSha256"] = future_review_sha
    chronology_history_bytes = yaml.safe_dump(chronology_history, sort_keys=False).encode("utf-8")
    chronology_claim = dict(claim_payload)
    chronology_claim["approvedReviewSha256"] = future_review_sha
    chronology_claim["historySha256"] = hashlib.sha256(chronology_history_bytes).hexdigest()
    archived_review.write_bytes(future_review)
    accepted.write_bytes(chronology_history_bytes)
    claim_path.write_bytes(yaml.safe_dump(chronology_claim, sort_keys=False).encode("utf-8"))
    assert_value_error("approval chronology must satisfy", lambda: parse_task_state(state_path))
    archived_review.write_bytes(original_review)
    accepted.write_bytes(original_history)
    claim_path.write_bytes(original_claim)

    changed_recorded_at = "2098-12-31T23:59:59.999999Z"
    renamed_payload = dict(payload)
    renamed_payload["recordedAt"] = changed_recorded_at
    renamed_history = approval._history_path(
        repo,
        issue,
        "contract-acceptance",
        changed_recorded_at,
        str(payload["approvalId"]),
    )
    renamed_history.write_bytes(yaml.safe_dump(renamed_payload, sort_keys=False).encode("utf-8"))
    renamed_ref = renamed_history.relative_to(issue_root).as_posix()
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", renamed_ref)))
    assert_value_error("claim does not seal exact history", lambda: parse_task_state(state_path))
    renamed_history.unlink()
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", reference)))

    forged_payload = dict(payload)
    forged_payload["approvalId"] = "f" * 32
    forged_payload["recordedAt"] = "2026-07-31T01:02:03.000004Z"
    forged_payload["approvedReviewFile"] = "approvals/history/consumed/" + "f" * 32 + "-local-review.md"
    forged_payload["approvedReviewSha256"] = "0" * 64
    forged_payload["approvalClaimFile"] = "approvals/history/claims/" + "f" * 32 + ".yaml"
    forged = accepted.parent / ("20260731T010203000004Z-contract-acceptance-" + "f" * 32 + ".yaml")
    write(forged, yaml.safe_dump(forged_payload, sort_keys=False))
    forged_ref = forged.relative_to(state_path.parent).as_posix()
    write(state_path, render_task_state(task_state(issue, "feature/101-contract", forged_ref)))
    assert_value_error("missing archived approved review", lambda: parse_task_state(state_path))
    forged.unlink()

    write(state_path, render_task_state(task_state(issue, "feature/101-contract", reference)))
    replace(contract.path, "note: 非规范性背景", "note: 合同内容已变更")
    assert_value_error("missing matching human contract acceptance", lambda: parse_task_state(state_path))
    replace(contract.path, "note: 合同内容已变更", "note: 非规范性背景")


def test_draft_rejection_and_bom_acceptance(repo: Path) -> None:
    issue = "102"
    legacy_state = activate_legacy_current_task(repo, issue)
    draft_path = copied_contract(repo, "draft.yaml")
    replace(draft_path, "status: accepted-design", "status: draft")
    draft = load_contract(repo, draft_path)
    review = approval.prepare(
        repo, issue, "contract-acceptance", draft.path, reviewer="reviewer", force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    duplicated = review.read_text(encoding="utf-8").replace(
        "semanticDecision: accepted-design\n",
        "semanticDecision: accepted-design\nsemanticDecision: accepted-design\n",
    )
    write(review, duplicated.replace("Approved: no", "Approved: yes"))
    assert_value_error(
        "duplicate YAML key",
        lambda: approval.require_exact_remote(repo, "contract-acceptance", draft.path, issue),
    )
    review = approval.prepare(
        repo, issue, "contract-acceptance", draft.path, reviewer="reviewer", force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    approve(review)
    assert_value_error(
        "candidate contract status must be accepted-design",
        lambda: validate_contract_acceptance(repo, issue, draft, ACCEPTED_OBJECTS),
    )
    stale_state_path = repo / ".xflow" / "issues" / "issue-102" / "task-state.md"
    stale_state = dataclass_replace(
        task_state(issue, "feature/101-contract", "approvals/history/missing.yaml"),
        contract_file="contracts/draft.yaml",
    )
    write(stale_state_path, render_task_state(stale_state))
    assert_value_error("contract status is incompatible", lambda: parse_task_state(stale_state_path))
    write(stale_state_path, render_task_state(legacy_state))
    activate_task(repo, issue)

    bom_path = repo / "contracts" / "bom.yaml"
    bom_path.write_bytes(b"\xef\xbb\xbf" + FIXTURE.read_bytes())
    bom = load_contract(repo, bom_path)
    assert bom.raw_bytes == bom_path.read_bytes()
    assert bom.sha256 == hashlib.sha256(bom_path.read_bytes()).hexdigest()
    review = approval.prepare(
        repo, issue, "contract-acceptance", bom.path, reviewer="reviewer", force=True,
        accepted_objects=ACCEPTED_OBJECTS,
    )
    approve(review)
    record = validate_contract_acceptance(repo, issue, bom, ACCEPTED_OBJECTS)
    assert bom.sha256 in record.read_text(encoding="utf-8")


def test_windows_final_path_normalization_keeps_lock_containment(repo: Path) -> None:
    assert project_config._normalize_windows_final_path(r"\\?\C:\repo\.xflow\claim.lock") == r"C:\repo\.xflow\claim.lock"
    assert project_config._normalize_windows_final_path(r"C:\repo\.xflow\claim.lock") == r"C:\repo\.xflow\claim.lock"
    assert project_config._normalize_windows_final_path(r"\\?\UNC\server\share\repo\.xflow\claim.lock") == r"\\server\share\repo\.xflow\claim.lock"
    assert project_config._normalize_windows_final_path(r"\\server\share\repo\.xflow\claim.lock") == r"\\server\share\repo\.xflow\claim.lock"

    outside = repo.parent / "outside.lock"
    assert_value_error(
        "outside repository",
        lambda: project_config.require_safe_repo_path(repo, outside, "contract acceptance finalizer lock"),
    )

    claim_path = repo / ".xflow" / "issues" / "issue-103" / "approvals" / "history" / "claims" / "lock.yaml"
    lock_path = claim_path.with_suffix(".lock")
    assert project_config.require_safe_repo_path(
        repo.parent / repo.name.upper(),
        lock_path,
        "contract acceptance finalizer lock",
    ) == lock_path
    original_resolve = project_config.Path.resolve

    def resolve_with_final_path_prefix(path: Path, *, strict: bool = False) -> Path:
        resolved = original_resolve(path, strict=strict)
        if path == lock_path:
            return project_config.Path("\\\\?\\" + str(resolved))
        return resolved

    with patch.object(project_config.Path, "resolve", new=resolve_with_final_path_prefix):
        with approval._contract_claim_lock(repo, claim_path, "a" * 32):
            pass


def test_atomic_contract_acceptance_claim(repo: Path) -> None:
    for run in range(5):
        issue = f"15{run}"
        activate_legacy_current_task(repo, issue)
        contract = load_contract(repo, copied_contract(repo, f"concurrent-{run}.yaml"))
        review = approval.prepare(
            repo, issue, "contract-acceptance", contract.path, reviewer="reviewer", force=True,
            accepted_objects=ACCEPTED_OBJECTS,
        )
        approve(review)
        approval_id = approval.field(review.read_text(encoding="utf-8"), "Approval ID")

        def accept() -> Path | ValueError:
            try:
                return validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS)
            except ValueError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: accept(), range(2)))
        assert sum(isinstance(result, Path) for result in results) == 1, results
        failures = [str(result) for result in results if isinstance(result, ValueError)]
        assert len(failures) == 1 and failures[0] in {
            f"approval already claimed: {approval_id}",
            f"approval already consumed: {approval_id}",
        }, failures
        history_root = repo / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "history"
        claims = tuple((history_root / "claims").glob("*.yaml"))
        archives = tuple((history_root / "consumed").glob("*.md"))
        histories = tuple(history_root.glob("*.yaml"))
        assert len(claims) == len(archives) == len(histories) == 1
        assert approval.validate_contract_acceptance_history(repo, histories[0])["approvalId"] == approval_id


def test_contract_acceptance_recovers_partial_publication(repo: Path) -> None:
    cases = (
        ("105", "contract acceptance claim collision", False),
        ("106", "archived contract approval collision", True),
    )
    for issue, failure_point, archive_expected in cases:
        legacy_state = activate_legacy_current_task(repo, issue)
        contract = load_contract(repo, copied_contract(repo, f"recover-{issue}.yaml"))
        review = approval.prepare(
            repo,
            issue,
            "contract-acceptance",
            contract.path,
            reviewer="reviewer",
            force=True,
            accepted_objects=ACCEPTED_OBJECTS,
        )
        approve(review)
        original_writer = approval._write_immutable_bytes
        injected = False

        def crash_after_write(path: Path, content: bytes, collision_message: str) -> None:
            nonlocal injected
            original_writer(path, content, collision_message)
            if not injected and collision_message == failure_point:
                injected = True
                raise RuntimeError(f"injected failure after {failure_point}")

        with patch.object(approval, "_write_immutable_bytes", side_effect=crash_after_write):
            try:
                validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS)
            except RuntimeError as exc:
                assert "injected failure" in str(exc)
            else:
                raise AssertionError("expected injected acceptance publication failure")

        history_root = repo / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "history"
        assert len(tuple((history_root / "claims").glob("*.yaml"))) == 1
        assert bool(tuple((history_root / "consumed").glob("*.md"))) is archive_expected
        assert not tuple(history_root.glob("*.yaml"))

        claim_file = next((history_root / "claims").glob("*.yaml"))
        original_claim = claim_file.read_bytes()
        if issue == "105":
            mismatched_claim = yaml.safe_load(original_claim.decode("utf-8"))
            mismatched_claim["contractSha256"] = "0" * 64
            claim_file.write_bytes(yaml.safe_dump(mismatched_claim, sort_keys=False).encode("utf-8"))
            assert_value_error(
                "claim replay or tampering",
                lambda: validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS),
            )
            claim_file.write_bytes(original_claim)
        else:
            archive_file = next((history_root / "consumed").glob("*.md"))
            original_archive = archive_file.read_bytes()
            archive_file.write_bytes(original_archive + b"mutated\n")
            assert_value_error(
                "existing archived review differs",
                lambda: validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS),
            )
            archive_file.write_bytes(original_archive)

        accepted = validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS)
        assert len(tuple((history_root / "claims").glob("*.yaml"))) == 1
        assert len(tuple((history_root / "consumed").glob("*.md"))) == 1
        assert tuple(history_root.glob("*.yaml")) == (accepted,)
        state_path = repo / ".xflow" / "issues" / f"issue-{issue}" / "task-state.md"
        reference = accepted.relative_to(state_path.parent).as_posix()
        recovered_state = dataclass_replace(
            task_state(issue, "feature/101-contract", reference),
            contract_file=f"contracts/recover-{issue}.yaml",
        )
        write(state_path, render_task_state(recovered_state))
        assert parse_task_state(state_path).human_approval_ref == reference
        write(state_path, render_task_state(legacy_state))
        activate_task(repo, issue)
        original_history = accepted.read_bytes()
        accepted.write_bytes(original_history.replace(b"reviewerSummary: reviewer", b"reviewerSummary: changed"))
        assert_value_error(
            "existing acceptance history differs",
            lambda: validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS),
        )
        accepted.write_bytes(original_history)
        assert_value_error(
            "approval already consumed",
            lambda: validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS),
        )
        assert_value_error(
            "accepted object set mismatch",
            lambda: validate_contract_acceptance(repo, issue, contract, (ACCEPTED_OBJECTS[0],)),
        )
        assert len(tuple(history_root.glob("*.yaml"))) == 1


def test_contract_acceptance_history_names_include_approval_id(repo: Path) -> None:
    issue = "107"
    activate_legacy_current_task(repo, issue)
    contract = load_contract(repo, copied_contract(repo, "same-recorded-at.yaml"))
    recorded_at = "2099-01-02T03:04:05.000006Z"
    approval_ids: list[str] = []
    records: list[Path] = []

    with patch.object(approval, "_canonical_utc_now", return_value=recorded_at):
        for reviewer in ("first-reviewer", "second-reviewer"):
            review = approval.prepare(
                repo,
                issue,
                "contract-acceptance",
                contract.path,
                reviewer=reviewer,
                force=True,
                accepted_objects=ACCEPTED_OBJECTS,
            )
            approval_ids.append(approval.field(review.read_text(encoding="utf-8"), "Approval ID"))
            approve(review)
            records.append(validate_contract_acceptance(repo, issue, contract, ACCEPTED_OBJECTS))

    assert records[0] != records[1]
    for record, approval_id in zip(records, approval_ids, strict=True):
        assert record.name == f"20990102T030405000006Z-contract-acceptance-{approval_id}.yaml"
        payload = approval.validate_contract_acceptance_history(repo, record)
        assert payload["approvalId"] == approval_id
        claim = yaml.safe_load(
            (
                repo
                / ".xflow"
                / "issues"
                / "issue-107"
                / "approvals"
                / "history"
                / "claims"
                / f"{approval_id}.yaml"
            ).read_text(encoding="utf-8")
        )
        assert claim["historyFile"] == record.relative_to(record.parents[2]).as_posix()


def test_cli_contract_edges_and_historical_list(repo: Path) -> None:
    bare = run_devctl(repo, "contract", expect=2)
    assert "usage: devctl contract" in bare.stderr
    assert "AttributeError" not in bare.stderr

    issue = "104"
    activate_legacy_current_task(repo, issue)
    contract_path = copied_contract(repo, "cli.yaml")
    missing = run_devctl(
        repo, "approval", "prepare", "--issue", issue, "--action", "contract-acceptance",
        "--file", str(contract_path), "--force", expect=1,
    )
    assert "--objects is required for contract-acceptance" in missing.stderr
    prepared = run_devctl(
        repo, "approval", "prepare", "--issue", issue, "--action", "contract-acceptance",
        "--file", str(contract_path), "--objects", ",".join(reversed(ACCEPTED_OBJECTS)), "--force",
    )
    assert "local review prepared" in prepared.stdout
    review = repo / ".xflow" / "issues" / "issue-104" / "approvals" / "local-review.md"
    text = review.read_text(encoding="utf-8")
    assert text.index(ACCEPTED_OBJECTS[0]) < text.index(ACCEPTED_OBJECTS[1])

    accepted_states = tuple((repo / ".xflow" / "issues").glob("issue-*/task-state.md"))
    assert accepted_states
    git(repo, "checkout", "main", "-q")
    assert list_task_states(repo)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = init_repo(Path(raw))
        contract = test_valid_contract_and_owned_object_locations(repo)
        test_schema_and_semantic_rejections(repo)
        test_schema_semantic_parity(repo)
        test_stage_blockers_reject_padded_open(repo)
        test_supersedes_rules(repo)
        test_optional_contract_lists_may_be_empty(repo)
        test_contract_root_containment(repo)
        test_exact_local_acceptance_and_task_state(repo, contract)
        test_draft_rejection_and_bom_acceptance(repo)
        test_windows_final_path_normalization_keeps_lock_containment(repo)
        test_atomic_contract_acceptance_claim(repo)
        test_contract_acceptance_recovers_partial_publication(repo)
        test_contract_acceptance_history_names_include_approval_id(repo)
        test_cli_contract_edges_and_historical_list(repo)
    print("contract core ok")


if __name__ == "__main__":
    main()
