from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

from xflow.contracts import diff_contracts, load_contract


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    (repo / "README.md").write_text("# Contract diff fixture\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize contract diff fixture", "-q")
    (repo / ".xflow").mkdir()
    (repo / ".xflow" / "xflow.json").write_text('{"contracts":{"root":"contracts"}}\n', encoding="utf-8")
    return repo


def copied_contract(repo: Path, fixture: str, name: str | None = None) -> Path:
    target = repo / "contracts" / (name or fixture)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def write(path: Path, payload: dict[str, object]) -> None:
    write_text_lf(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def run_devctl(repo_root: Path, *args: str, expect: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env={**os.environ, "DEVCTL_REPO_ROOT": str(repo_root), "PYTHONPATH": str(OPS_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        raise AssertionError(f"expected exit {expect}: {' '.join(args)}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def test_required_evolution_bumps(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    patch = load_contract(repo, copied_contract(repo, "patch.yaml"))
    minor = load_contract(repo, copied_contract(repo, "minor.yaml"))
    major = load_contract(repo, copied_contract(repo, "major.yaml"))
    implementation_gap = load_contract(repo, copied_contract(repo, "implementation-gap-unchanged.yaml"))

    assert diff_contracts(base, patch).required_bump == "patch"
    assert diff_contracts(base, minor).required_bump == "minor"
    assert diff_contracts(base, major).required_bump == "major"
    assert diff_contracts(base, implementation_gap).required_bump == "none"
    assert diff_contracts(base, implementation_gap).actual_bump == "none"
    assert diff_contracts(base, implementation_gap).unchanged == tuple(sorted(base.objects_by_id))
    major_impacts = "\n".join(diff_contracts(base, major).review_impacts)
    assert "affected verification IDs: example.verify.case.operation-rejection, example.verify.case.operation-success" in major_impacts
    assert "affected projection IDs: example.projection.primary-implementation" in major_impacts


def test_rejects_stale_versions_and_exact_unlinked_replacements(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)

    stale_path = copied_contract(repo, "major.yaml")
    stale_payload = yaml.safe_load(stale_path.read_text(encoding="utf-8"))
    stale_payload["semanticValueContracts"][1]["version"] = "0.1.0"
    write(stale_path, stale_payload)
    stale = load_contract(repo, stale_path)
    stale_diff = diff_contracts(base, stale)
    assert stale_diff.required_bump == "major"
    assert any("example.value.result" in impact for impact in stale_diff.review_impacts)
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(stale_path), expect=1)
    assert "under-bumped objects:" in result.stdout, result.stdout + result.stderr
    assert "example.value.result" in result.stdout

    renamed_path = copied_contract(repo, "valid.yaml", "renamed.yaml")
    renamed_payload = yaml.safe_load(renamed_path.read_text(encoding="utf-8"))
    renamed_payload["version"] = "0.2.0"
    renamed_payload["futureCapabilitiesOutOfScope"][0]["id"] = "example.future.optional-extension-v2"
    write(renamed_path, renamed_payload)
    renamed = load_contract(repo, renamed_path)
    renamed_diff = diff_contracts(base, renamed)
    assert renamed_diff.required_bump == "human-review"
    assert (
        "[ERROR] exact stable-ID replacement lacks one-to-one supersedes: "
        "example.future.optional-extension -> example.future.optional-extension-v2"
        in "\n".join(renamed_diff.review_impacts)
    )
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(renamed_path), expect=1)
    assert "[ERROR]" in result.stdout


def test_accepts_historical_supersedes_and_prints_stable_impacts(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    successor_path = copied_contract(repo, "valid.yaml", "successor.yaml")
    successor_payload = yaml.safe_load(successor_path.read_text(encoding="utf-8"))
    successor_payload["version"] = "0.2.0"
    successor_payload["futureCapabilitiesOutOfScope"][0]["id"] = "example.future.optional-extension-v2"
    successor_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = ["example.future.optional-extension"]
    write(successor_path, successor_payload)
    successor = load_contract(repo, successor_path)
    diff = diff_contracts(base, successor)
    assert diff.added == ("example.future.optional-extension-v2",)
    assert diff.removed == ("example.future.optional-extension",)
    assert not any("stable-ID replacement" in impact for impact in diff.review_impacts)
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(successor_path), expect=0)
    repeated = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(successor_path), expect=0)
    assert result.stdout == repeated.stdout
    assert "added: example.future.optional-extension-v2" in result.stdout
    assert "affected verification IDs: none" in result.stdout
    assert "affected projection IDs: none" in result.stdout


def test_human_review_retains_known_major_floor_and_root_failure(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "major-plus-ambiguity.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["capabilityContract"]["constraints"][0]["version"] = "1.0.0"
    payload["capabilityContract"]["constraints"][0]["rule"] = "请求被拒绝时保持业务状态和审计状态不变"
    payload["futureCapabilitiesOutOfScope"] = []
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert diff.actual_bump == "none"
    assert "mechanical floor: major" in impacts
    assert "[ERROR] under-bumped contract root: required major, actual none" in impacts
    result = run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=1)
    assert "mechanical floor: major" in result.stdout


def test_major_object_rejects_patch_bump(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "object-under-bump.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "1.0.0"
    payload["capabilityContract"]["constraints"][0]["version"] = "0.1.1"
    payload["capabilityContract"]["constraints"][0]["rule"] = "请求被拒绝时保持业务状态和审计状态不变"
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "major"
    assert "under-bumped objects:" in impacts
    assert "example.constraint.preserve-state-on-rejection" in impacts
    assert "required major, actual patch" in impacts
    run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=1)


def test_unchanged_object_versions_must_be_exact(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    cases = (("0.1.1", "version-only"), ("0.0.9", "decreasing"))
    for version, label in cases:
        candidate_path = copied_contract(repo, "valid.yaml", f"unchanged-object-{label}.yaml")
        payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
        payload["semanticValueContracts"][0]["version"] = version
        write(candidate_path, payload)
        diff = diff_contracts(base, load_contract(repo, candidate_path))
        impacts = "\n".join(diff.review_impacts)
        assert diff.required_bump == "none"
        assert "example.value.request" in diff.unchanged
        assert f"[ERROR] unchanged object version changed: example.value.request 0.1.0 -> {version}" in impacts


def test_unchanged_root_rejects_version_only_bump(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "root-version-only.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "0.1.1"
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "none"
    assert diff.actual_bump == "patch"
    assert "mechanical floor: none" in impacts
    assert "[ERROR] unchanged contract root version changed: 0.1.0 -> 0.1.1" in impacts
    run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=1)


def test_set_like_reordering_is_not_semantic_change(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "set-order-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["capabilityContract"]["constraints"].append(
        {"id": "example.constraint.audit-state", "version": "0.1.0", "rule": "审计状态保持一致"}
    )
    old_payload["contextRoles"].append(
        {
            "id": "example.role.auditor",
            "version": "0.1.0",
            "context": "example.context.operation",
            "responsibility": "确认审计状态",
            "doesNotOwn": ["业务请求", "协议选择"],
        }
    )
    old_payload["failureReasonContracts"].append(
        {
            "id": "example.failure-reason.timeout",
            "version": "0.1.0",
            "code": "timeout",
            "meaning": "请求超时",
            "preserves": ["既有业务状态", "审计状态"],
        }
    )
    old_payload["semanticValueContracts"][0]["meanings"].append("携带可选审计意图")
    old_payload["failureReasonContracts"][0]["preserves"].append("审计状态")
    old_payload["context"]["entryConditions"].append("审计上下文可用")
    old_payload["context"]["completionConditions"].append("审计结论稳定")
    old_payload["context"]["responsibilities"].append("保持审计一致性")
    old_payload["contextRoles"][0]["doesNotOwn"].append("审计存储")
    old_payload["capabilityContract"]["participants"].append("example.role.auditor")
    old_payload["capabilityContract"]["inputs"].append("example.value.result")
    old_payload["capabilityContract"]["outputs"].append("example.value.request")
    interaction = old_payload["interactionContracts"][0]
    interaction["participants"].append("example.role.auditor")
    interaction["accepts"].append("example.value.result")
    interaction["produces"].append("example.value.request")
    interaction["constraints"].append("example.constraint.audit-state")
    interaction["failureExpectations"].append(
        {"reason": "example.failure-reason.timeout", "preserves": ["既有业务状态", "审计状态"]}
    )
    second_interaction = {
        "id": "example.interaction.inspect-operation",
        "version": "0.1.0",
        "context": "example.context.operation",
        "participants": ["example.role.operator"],
        "accepts": ["example.value.request"],
        "produces": ["example.value.result"],
        "constraints": ["example.constraint.preserve-state-on-rejection"],
        "failureExpectations": [
            {"reason": "example.failure-reason.invalid-state", "preserves": ["既有业务状态"]}
        ],
    }
    old_payload["interactionContracts"].append(second_interaction)
    rejection = old_payload["verificationMatrix"][1]
    rejection["traces"].append("example.constraint.audit-state")
    rejection["verifyBy"].append({"type": "manual", "target": "audit-review"})
    projection = old_payload["engineeringProjections"][0]
    projection["traces"].append("example.interaction.inspect-operation")
    projection["preservedInvariants"].append("example.constraint.audit-state")
    old_payload["dependsOn"][0]["requiredFor"].append("example.interaction.inspect-operation")
    write(old_path, old_payload)

    new_path = copied_contract(repo, "valid.yaml", "set-order-new.yaml")
    new_payload = copy.deepcopy(old_payload)
    for field in ("participants", "inputs", "outputs", "constraints"):
        new_payload["capabilityContract"][field].reverse()
    new_payload["semanticValueContracts"][0]["meanings"].reverse()
    new_payload["failureReasonContracts"][0]["preserves"].reverse()
    new_payload["failureReasonContracts"][1]["preserves"].reverse()
    for field in ("entryConditions", "completionConditions", "responsibilities"):
        new_payload["context"][field].reverse()
    new_payload["contextRoles"][0]["doesNotOwn"].reverse()
    new_payload["contextRoles"][1]["doesNotOwn"].reverse()
    for field in ("participants", "accepts", "produces", "constraints", "failureExpectations"):
        new_payload["interactionContracts"][0][field].reverse()
    new_payload["verificationMatrix"][1]["traces"].reverse()
    new_payload["verificationMatrix"][1]["verifyBy"].reverse()
    new_payload["engineeringProjections"][0]["traces"].reverse()
    new_payload["engineeringProjections"][0]["derivedRepresentations"].reverse()
    new_payload["engineeringProjections"][0]["preservedInvariants"].reverse()
    new_payload["dependsOn"][0]["requiredFor"].reverse()
    write(new_path, new_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, new_path))
    assert diff.required_bump == "none"
    assert diff.changed == ()
    assert not any(impact.startswith("[ERROR]") for impact in diff.review_impacts)


def test_nested_constraint_changes_do_not_double_bump_capability(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))

    version_path = copied_contract(repo, "valid.yaml", "constraint-version-only.yaml")
    version_payload = yaml.safe_load(version_path.read_text(encoding="utf-8"))
    version_payload["capabilityContract"]["constraints"][0]["version"] = "0.1.1"
    write(version_path, version_payload)
    version_diff = diff_contracts(base, load_contract(repo, version_path))
    assert "example.capability.capability-name" in version_diff.unchanged
    assert "example.capability.capability-name" not in version_diff.changed
    assert "unchanged object version changed: example.constraint.preserve-state-on-rejection" in "\n".join(
        version_diff.review_impacts
    )

    rule_path = copied_contract(repo, "valid.yaml", "constraint-rule.yaml")
    rule_payload = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    rule_payload["version"] = "1.0.0"
    rule_payload["capabilityContract"]["constraints"][0]["version"] = "1.0.0"
    rule_payload["capabilityContract"]["constraints"][0]["rule"] = "请求被拒绝时业务状态和审计状态保持不变"
    write(rule_path, rule_payload)
    rule_diff = diff_contracts(base, load_contract(repo, rule_path))
    assert rule_diff.changed == ("example.constraint.preserve-state-on-rejection",)
    assert "example.capability.capability-name" in rule_diff.unchanged


def test_reference_changes_are_visible_and_ambiguous(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "references-changed.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "0.1.1"
    payload["references"][0]["note"] = "更新后的能力来源记录"
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert diff.changed == ("example.contract.capability-name",)
    assert "mechanical floor: patch" in impacts
    assert "[WARN] changed contract references require human review" in impacts
    run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=0)


def test_changed_object_supersedes_must_resolve_in_old_document(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "changed-supersedes.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "0.1.1"
    payload["futureCapabilitiesOutOfScope"][0]["version"] = "0.1.1"
    payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = ["example.future.missing"]
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    assert "[ERROR] invalid historical supersedes for example.future.optional-extension: example.future.missing" in "\n".join(
        diff.review_impacts
    )
    run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=1)

    wrong_kind_path = copied_contract(repo, "valid.yaml", "changed-supersedes-wrong-kind.yaml")
    wrong_kind_payload = yaml.safe_load(wrong_kind_path.read_text(encoding="utf-8"))
    wrong_kind_payload["version"] = "0.1.1"
    wrong_kind_payload["semanticValueContracts"][0]["version"] = "0.1.1"
    wrong_kind_payload["semanticValueContracts"][0]["supersedes"] = ["example.future.optional-extension"]
    wrong_kind_payload["futureCapabilitiesOutOfScope"] = []
    write(wrong_kind_path, wrong_kind_payload)
    wrong_kind_diff = diff_contracts(base, load_contract(repo, wrong_kind_path))
    assert (
        "[ERROR] invalid historical supersedes kind for example.value.request: example.future.optional-extension"
        in "\n".join(wrong_kind_diff.review_impacts)
    )

    old_with_predecessor_path = copied_contract(repo, "valid.yaml", "changed-supersedes-old.yaml")
    old_with_predecessor = yaml.safe_load(old_with_predecessor_path.read_text(encoding="utf-8"))
    old_with_predecessor["semanticValueContracts"].append(
        {
            "id": "example.value.retired-request",
            "version": "0.1.0",
            "name": "已退役请求",
            "meanings": ["旧请求语义"],
        }
    )
    write(old_with_predecessor_path, old_with_predecessor)
    valid_path = copied_contract(repo, "valid.yaml", "changed-supersedes-valid.yaml")
    valid_payload = yaml.safe_load(valid_path.read_text(encoding="utf-8"))
    valid_payload["version"] = "1.0.0"
    valid_payload["semanticValueContracts"][0]["version"] = "0.1.1"
    valid_payload["semanticValueContracts"][0]["supersedes"] = ["example.value.retired-request"]
    write(valid_path, valid_payload)
    valid_diff = diff_contracts(load_contract(repo, old_with_predecessor_path), load_contract(repo, valid_path))
    assert not any("invalid historical supersedes" in impact for impact in valid_diff.review_impacts)


def test_supersedes_many_to_many_is_human_review(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "many-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"].append(
        {
            "id": "example.future.optional-extension-b",
            "version": "0.1.0",
            "capability": "另一个可选扩展能力",
            "reason": "不属于当前承诺且不进入当前验证",
        }
    )
    write(old_path, old_payload)

    merged_path = copied_contract(repo, "valid.yaml", "many-merged.yaml")
    merged_payload = yaml.safe_load(merged_path.read_text(encoding="utf-8"))
    merged_payload["version"] = "0.2.0"
    merged_payload["futureCapabilitiesOutOfScope"] = [
        {
            "id": "example.future.optional-extension-merged",
            "version": "0.1.0",
            "capability": "合并后的可选扩展能力",
            "reason": "不属于当前承诺且不进入当前验证",
            "supersedes": ["example.future.optional-extension", "example.future.optional-extension-b"],
        }
    ]
    write(merged_path, merged_payload)
    merged_diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, merged_path))
    assert merged_diff.required_bump == "human-review"
    assert "[WARN] one new object supersedes multiple old objects: example.future.optional-extension-merged" in "\n".join(
        merged_diff.review_impacts
    )

    split_path = copied_contract(repo, "valid.yaml", "many-split.yaml")
    split_payload = yaml.safe_load(split_path.read_text(encoding="utf-8"))
    split_payload["version"] = "0.2.0"
    split_payload["futureCapabilitiesOutOfScope"] = [
        {
            "id": f"example.future.optional-extension-{suffix}",
            "version": "0.1.0",
            "capability": f"拆分后的可选扩展能力 {suffix}",
            "reason": "不属于当前承诺且不进入当前验证",
            "supersedes": ["example.future.optional-extension"],
        }
        for suffix in ("a", "b")
    ]
    write(split_path, split_payload)
    split_diff = diff_contracts(load_contract(repo, copied_contract(repo, "valid.yaml", "split-old.yaml")), load_contract(repo, split_path))
    assert split_diff.required_bump == "human-review"
    assert "[WARN] one old object is superseded by multiple new objects: example.future.optional-extension" in "\n".join(
        split_diff.review_impacts
    )


def test_exact_split_warns_without_false_missing_lineage_error(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "exact-split-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    predecessor = old_payload["futureCapabilitiesOutOfScope"][0]

    candidate_path = copied_contract(repo, "valid.yaml", "exact-split-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.2.0"
    exact_successor = copy.deepcopy(predecessor)
    exact_successor["id"] = "example.future.optional-extension-v2"
    exact_successor["supersedes"] = ["example.future.optional-extension"]
    edited_successor = copy.deepcopy(predecessor)
    edited_successor["id"] = "example.future.optional-extension-audit"
    edited_successor["capability"] = "可选审计扩展能力"
    edited_successor["supersedes"] = ["example.future.optional-extension"]
    candidate_payload["futureCapabilitiesOutOfScope"] = [exact_successor, edited_successor]
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert (
        "[WARN] one old object is superseded by multiple new objects: example.future.optional-extension"
        in impacts
    )
    assert "exact stable-ID replacement lacks one-to-one supersedes" not in impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=0)


def test_exact_merge_warns_without_false_missing_lineage_error(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "exact-merge-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"].append(
        {
            "id": "example.future.optional-extension-b",
            "version": "0.1.0",
            "capability": "另一个可选扩展能力",
            "reason": "不属于当前承诺且不进入当前验证",
        }
    )
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "exact-merge-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.2.0"
    exact_successor = copy.deepcopy(old_payload["futureCapabilitiesOutOfScope"][0])
    exact_successor["id"] = "example.future.optional-extension-merged"
    exact_successor["supersedes"] = [
        "example.future.optional-extension",
        "example.future.optional-extension-b",
    ]
    candidate_payload["futureCapabilitiesOutOfScope"] = [exact_successor]
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert (
        "[WARN] one new object supersedes multiple old objects: example.future.optional-extension-merged"
        in impacts
    )
    assert "exact stable-ID replacement lacks one-to-one supersedes" not in impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=0)


def test_persisted_edges_warn_when_predecessor_is_removed(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "persisted-split-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"].extend(
        [
            {
                "id": f"example.future.optional-extension-{suffix}",
                "version": "0.1.0",
                "capability": f"已建立血缘的可选扩展能力 {suffix}",
                "reason": "不属于当前承诺且不进入当前验证",
                "supersedes": ["example.future.optional-extension"],
            }
            for suffix in ("a", "b")
        ]
    )
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "persisted-split-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.1.1"
    candidate_payload["futureCapabilitiesOutOfScope"] = candidate_payload["futureCapabilitiesOutOfScope"][1:]
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert (
        "[WARN] one old object is superseded by multiple new objects: example.future.optional-extension"
        in impacts
    )
    assert "invalid historical supersedes" not in impacts
    assert not any(impact.startswith("[ERROR]") for impact in diff.review_impacts)
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=0)


def test_rejects_same_kind_historical_id_resurrection(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "same-kind-resurrection-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = [
        "example.future.retired-extension"
    ]
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "same-kind-resurrection-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.2.0"
    candidate_payload["futureCapabilitiesOutOfScope"].append(
        {
            "id": "example.future.retired-extension",
            "version": "0.1.0",
            "capability": "已退役标识对应的可选扩展能力",
            "reason": "不属于当前承诺且不进入当前验证",
        }
    )
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert "[ERROR] stable-ID resurrection: example.future.retired-extension" in impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=1)


def test_rejects_cross_kind_historical_id_resurrection(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "cross-kind-resurrection-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = [
        "example.value.retired-extension"
    ]
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "cross-kind-resurrection-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.2.0"
    candidate_payload["futureCapabilitiesOutOfScope"] = []
    candidate_payload["semanticValueContracts"].append(
        {
            "id": "example.value.retired-extension",
            "version": "0.1.0",
            "name": "已退役标识对应的语义值",
            "meanings": ["不得以其他对象种类重新启用的历史标识"],
        }
    )
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert "[ERROR] stable-ID resurrection: example.value.retired-extension" in impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=1)


def test_old_current_predecessor_remains_or_retires_without_resurrection(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "current-predecessor-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"].append(
        {
            "id": "example.future.optional-extension-successor",
            "version": "0.1.0",
            "capability": "已声明当前前任的可选扩展能力",
            "reason": "不属于当前承诺且不进入当前验证",
            "supersedes": ["example.future.optional-extension"],
        }
    )
    write(old_path, old_payload)
    old = load_contract(repo, old_path)

    retained_path = copied_contract(repo, "valid.yaml", "current-predecessor-retained.yaml")
    write(retained_path, copy.deepcopy(old_payload))
    retained_diff = diff_contracts(old, load_contract(repo, retained_path))
    assert retained_diff.required_bump == "none"
    assert "stable-ID resurrection" not in "\n".join(retained_diff.review_impacts)
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(retained_path), expect=0)

    retired_path = copied_contract(repo, "valid.yaml", "current-predecessor-retired.yaml")
    retired_payload = copy.deepcopy(old_payload)
    retired_payload["version"] = "0.1.1"
    retired_payload["futureCapabilitiesOutOfScope"] = retired_payload["futureCapabilitiesOutOfScope"][1:]
    write(retired_path, retired_payload)
    retired_diff = diff_contracts(old, load_contract(repo, retired_path))
    retired_impacts = "\n".join(retired_diff.review_impacts)
    assert retired_diff.required_bump == "human-review"
    assert "stable-ID resurrection" not in retired_impacts
    assert "invalid historical supersedes" not in retired_impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(retired_path), expect=0)


def test_rejects_scalar_list_stable_id_kind_change(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "scalar-kind-change-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["semanticValueContracts"].append(
        {
            "id": "example.shared.scalar-kind",
            "version": "0.1.0",
            "name": "额外语义值",
            "meanings": ["用于验证全局稳定标识种类不变性"],
        }
    )
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "scalar-kind-change-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "1.0.0"
    candidate_payload["semanticValueContracts"] = candidate_payload["semanticValueContracts"][:-1]
    candidate_payload["failureReasonContracts"].append(
        {
            "id": "example.shared.scalar-kind",
            "version": "1.0.0",
            "code": "reused_scalar_kind",
            "meaning": "不得复用其他对象种类的稳定标识",
            "preserves": ["既有业务状态"],
        }
    )
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert (
        "[ERROR] stable-ID kind change: example.shared.scalar-kind "
        "semantic-value -> failure-reason"
        in impacts
    )
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=1)


def test_rejects_singleton_stable_id_kind_change(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "singleton-kind-change-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "singleton-kind-change-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "1.0.0"
    candidate_payload["capabilityContract"]["id"] = "example.context.operation"
    candidate_payload["capabilityContract"]["version"] = "1.0.0"
    candidate_payload["capabilityContract"]["purpose"] = "参与者可依赖且不可混淆标识种类的业务价值和边界"
    candidate_payload["context"]["id"] = "example.capability.capability-name"
    candidate_payload["context"]["version"] = "1.0.0"
    candidate_payload["context"]["name"] = "稳定标识种类隔离上下文"
    candidate_payload["contextRoles"][0]["version"] = "1.0.0"
    candidate_payload["contextRoles"][0]["context"] = "example.capability.capability-name"
    candidate_payload["interactionContracts"][0]["version"] = "1.0.0"
    candidate_payload["interactionContracts"][0]["context"] = "example.capability.capability-name"
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert (
        "[ERROR] stable-ID kind change: example.capability.capability-name "
        "capability -> context"
        in impacts
    )
    assert (
        "[ERROR] stable-ID kind change: example.context.operation "
        "context -> capability"
        in impacts
    )
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=1)


def test_same_kind_semantic_change_does_not_trigger_kind_error(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml")
    candidate_path = copied_contract(repo, "major.yaml")
    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    assert diff.required_bump == "major"
    assert "stable-ID kind change" not in "\n".join(diff.review_impacts)
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=0)


def test_persisted_supersedes_edges_are_canonical_sets(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "lineage-order-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = [
        "example.future.lineage-a",
        "example.future.lineage-b",
    ]
    write(old_path, old_payload)

    reordered_path = copied_contract(repo, "valid.yaml", "lineage-order-new.yaml")
    reordered_payload = copy.deepcopy(old_payload)
    reordered_payload["futureCapabilitiesOutOfScope"][0]["supersedes"].reverse()
    write(reordered_path, reordered_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, reordered_path))
    assert diff.required_bump == "none"
    assert diff.changed == ()
    assert not any(impact.startswith("[ERROR]") for impact in diff.review_impacts)
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(reordered_path), expect=0)


def test_removed_historical_supersedes_edge_is_error(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "lineage-removal-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = [
        "example.future.lineage-a",
        "example.future.lineage-b",
    ]
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "lineage-removal-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.1.1"
    candidate_payload["futureCapabilitiesOutOfScope"][0]["version"] = "0.1.1"
    candidate_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = ["example.future.lineage-a"]
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert (
        "[ERROR] removed historical supersedes edge: "
        "example.future.optional-extension -> example.future.lineage-b"
        in impacts
    )
    assert "invalid historical supersedes for example.future.optional-extension: example.future.lineage-a" not in impacts
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=1)


def test_only_new_supersedes_edges_require_old_snapshot_validation(repo: Path) -> None:
    old_path = copied_contract(repo, "valid.yaml", "lineage-addition-old.yaml")
    old_payload = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    old_payload["futureCapabilitiesOutOfScope"][0]["supersedes"] = ["example.future.older-lineage"]
    old_payload["futureCapabilitiesOutOfScope"].append(
        {
            "id": "example.future.retired-extension",
            "version": "0.1.0",
            "capability": "即将被替代的可选扩展",
            "reason": "不属于当前承诺且不进入当前验证",
        }
    )
    write(old_path, old_payload)

    candidate_path = copied_contract(repo, "valid.yaml", "lineage-addition-new.yaml")
    candidate_payload = copy.deepcopy(old_payload)
    candidate_payload["version"] = "0.2.0"
    candidate_payload["futureCapabilitiesOutOfScope"] = [candidate_payload["futureCapabilitiesOutOfScope"][0]]
    candidate_payload["futureCapabilitiesOutOfScope"][0]["version"] = "0.1.1"
    candidate_payload["futureCapabilitiesOutOfScope"][0]["supersedes"].append(
        "example.future.retired-extension"
    )
    write(candidate_path, candidate_payload)

    diff = diff_contracts(load_contract(repo, old_path), load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert "invalid historical supersedes" not in impacts
    assert "removed historical supersedes edge" not in impacts
    assert diff.required_bump == "human-review"
    run_devctl(repo, "contract", "diff", "--old", str(old_path), "--new", str(candidate_path), expect=0)


def test_unmapped_changed_replacement_is_human_review(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml")
    base = load_contract(repo, base_path)
    candidate_path = copied_contract(repo, "valid.yaml", "unmapped-active-replacement.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "1.0.0"
    payload["capabilityContract"]["version"] = "1.0.0"
    payload["capabilityContract"]["inputs"] = ["example.value.request-v2"]
    payload["semanticValueContracts"][0] = {
        "id": "example.value.request-v2",
        "version": "1.0.0",
        "name": "请求意图",
        "meanings": ["表达参与者希望系统承担并审计的业务动作"],
    }
    payload["interactionContracts"][0]["version"] = "1.0.0"
    payload["interactionContracts"][0]["accepts"] = ["example.value.request-v2"]
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert "mechanical floor: major" in impacts
    assert (
        "[WARN] unmapped same-kind replacements: semantic-value removed example.value.request; added example.value.request-v2"
        in impacts
    )
    assert "exact stable-ID replacement" not in impacts
    run_devctl(repo, "contract", "diff", "--old", str(base_path), "--new", str(candidate_path), expect=0)


def test_capability_semantics_impact_all_coverage(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    candidate_path = copied_contract(repo, "valid.yaml", "capability-purpose.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "1.0.0"
    payload["capabilityContract"]["version"] = "1.0.0"
    payload["capabilityContract"]["purpose"] = "参与者可依赖、审计并恢复的业务价值和边界"
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "major"
    assert (
        "affected verification IDs: example.verify.case.operation-rejection, example.verify.case.operation-success"
        in impacts
    )
    assert "affected projection IDs: example.projection.primary-implementation" in impacts


def test_optional_interaction_bundle_is_minor(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    candidate_path = copied_contract(repo, "valid.yaml", "optional-interaction.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "0.2.0"
    payload["semanticValueContracts"].extend(
        [
            {
                "id": "example.value.optional-request",
                "version": "0.1.0",
                "name": "可选请求",
                "meanings": ["仅供新增可选交互使用的请求"],
            },
            {
                "id": "example.value.optional-result",
                "version": "0.1.0",
                "name": "可选结果",
                "meanings": ["仅供新增可选交互产生的结果"],
            },
        ]
    )
    payload["failureReasonContracts"].append(
        {
            "id": "example.failure-reason.optional-rejected",
            "version": "0.1.0",
            "code": "optional_rejected",
            "meaning": "可选交互被拒绝",
            "preserves": ["既有业务状态"],
        }
    )
    payload["interactionContracts"].append(
        {
            "id": "example.interaction.optional-operation",
            "version": "0.1.0",
            "context": "example.context.operation",
            "participants": ["example.role.operator"],
            "accepts": ["example.value.optional-request"],
            "produces": ["example.value.optional-result"],
            "constraints": ["example.constraint.preserve-state-on-rejection"],
            "failureExpectations": [
                {"reason": "example.failure-reason.optional-rejected", "preserves": ["既有业务状态"]}
            ],
        }
    )
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    assert diff.required_bump == "minor"
    assert "mechanical floor: minor" in "\n".join(diff.review_impacts)
    assert not any(impact.startswith("[ERROR]") for impact in diff.review_impacts)


def test_added_value_in_required_capability_membership_is_major(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    candidate_path = copied_contract(repo, "valid.yaml", "required-membership.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "1.0.0"
    payload["capabilityContract"]["version"] = "1.0.0"
    payload["capabilityContract"]["inputs"].append("example.value.audit-request")
    payload["semanticValueContracts"].append(
        {
            "id": "example.value.audit-request",
            "version": "0.1.0",
            "name": "审计请求",
            "meanings": ["当前能力现在要求支持的审计输入"],
        }
    )
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    assert diff.required_bump == "major"
    assert "mechanical floor: major" in "\n".join(diff.review_impacts)


def test_uncertain_optional_interaction_is_human_review(repo: Path) -> None:
    base = load_contract(repo, copied_contract(repo, "valid.yaml"))
    candidate_path = copied_contract(repo, "valid.yaml", "uncertain-optional-interaction.yaml")
    payload = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    payload["version"] = "0.2.0"
    payload["contextRoles"].append(
        {
            "id": "example.role.optional-observer",
            "version": "0.1.0",
            "context": "example.context.operation",
            "responsibility": "观察可选交互",
            "doesNotOwn": ["业务状态"],
        }
    )
    payload["interactionContracts"].append(
        {
            "id": "example.interaction.optional-observation",
            "version": "0.1.0",
            "context": "example.context.operation",
            "participants": ["example.role.optional-observer"],
            "accepts": ["example.value.request"],
            "produces": ["example.value.result"],
            "constraints": ["example.constraint.preserve-state-on-rejection"],
            "failureExpectations": [
                {"reason": "example.failure-reason.invalid-state", "preserves": ["既有业务状态"]}
            ],
        }
    )
    write(candidate_path, payload)

    diff = diff_contracts(base, load_contract(repo, candidate_path))
    impacts = "\n".join(diff.review_impacts)
    assert diff.required_bump == "human-review"
    assert "mechanical floor: minor" in impacts
    assert "added interaction optionality requires human review: example.interaction.optional-observation" in impacts


def test_dependency_add_change_remove_impacts_required_interaction_coverage(repo: Path) -> None:
    base_path = copied_contract(repo, "valid.yaml", "dependency-base.yaml")
    base_payload = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    base_payload["interactionContracts"].append(
        {
            "id": "example.interaction.audit-operation",
            "version": "0.1.0",
            "context": "example.context.operation",
            "participants": ["example.role.operator"],
            "accepts": ["example.value.request"],
            "produces": ["example.value.result"],
            "constraints": ["example.constraint.preserve-state-on-rejection"],
            "failureExpectations": [
                {"reason": "example.failure-reason.invalid-state", "preserves": ["既有业务状态"]}
            ],
        }
    )
    base_payload["verificationMatrix"].append(
        {
            "id": "example.verify.case.audit-operation",
            "version": "0.1.0",
            "traces": ["example.interaction.audit-operation"],
            "given": "审计操作满足当前合法前态",
            "when": "参与者发起审计操作",
            "then": "产生可验证的审计结果",
            "verifyBy": [{"type": "automated", "target": "audit-contract-test"}],
        }
    )
    base_payload["engineeringProjections"].append(
        {
            "id": "example.projection.audit-implementation",
            "version": "0.1.0",
            "traces": ["example.interaction.audit-operation"],
            "authorityRepresentation": "领域模型中的审计状态",
            "derivedRepresentations": ["审计响应"],
            "transformationBoundary": "适配层只转换审计表示",
            "preservedInvariants": ["example.constraint.preserve-state-on-rejection"],
        }
    )
    write(base_path, base_payload)
    base = load_contract(repo, base_path)
    candidates: list[tuple[str, dict[str, object]]] = []

    changed = copy.deepcopy(base_payload)
    changed["version"] = "0.1.1"
    changed["dependsOn"][0]["version"] = "0.1.1"
    changed["dependsOn"][0]["requiredFor"] = ["example.interaction.audit-operation"]
    candidates.append(("changed", changed))

    added = copy.deepcopy(base_payload)
    added["version"] = "1.0.0"
    added["dependsOn"].append(
        {
            "id": "example.dependency.audit-foundation",
            "version": "0.1.0",
            "contract": "foundation.contract.audit-capability",
            "requiredFor": ["example.interaction.perform-operation"],
            "ownerRepository": "audit-foundation-repository",
        }
    )
    candidates.append(("added", added))

    removed = copy.deepcopy(base_payload)
    removed["version"] = "1.0.0"
    removed["dependsOn"] = []
    candidates.append(("removed", removed))

    expected = {
        "changed": (
            "affected verification IDs: example.verify.case.audit-operation, "
            "example.verify.case.operation-rejection, example.verify.case.operation-success",
            "affected projection IDs: example.projection.audit-implementation, "
            "example.projection.primary-implementation",
        ),
        "added": (
            "affected verification IDs: example.verify.case.operation-rejection, "
            "example.verify.case.operation-success",
            "affected projection IDs: example.projection.primary-implementation",
        ),
        "removed": (
            "affected verification IDs: example.verify.case.operation-rejection, "
            "example.verify.case.operation-success",
            "affected projection IDs: example.projection.primary-implementation",
        ),
    }
    for label, payload in candidates:
        candidate_path = copied_contract(repo, "valid.yaml", f"dependency-{label}.yaml")
        write(candidate_path, payload)
        diff = diff_contracts(base, load_contract(repo, candidate_path))
        impacts = "\n".join(diff.review_impacts)
        expected_verifications, expected_projection = expected[label]
        assert expected_verifications in impacts, (label, impacts)
        assert expected_projection in impacts, (label, impacts)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        test_required_evolution_bumps(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_stale_versions_and_exact_unlinked_replacements(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_accepts_historical_supersedes_and_prints_stable_impacts(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_human_review_retains_known_major_floor_and_root_failure(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_major_object_rejects_patch_bump(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_unchanged_object_versions_must_be_exact(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_unchanged_root_rejects_version_only_bump(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_set_like_reordering_is_not_semantic_change(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_nested_constraint_changes_do_not_double_bump_capability(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_reference_changes_are_visible_and_ambiguous(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_changed_object_supersedes_must_resolve_in_old_document(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_supersedes_many_to_many_is_human_review(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_exact_split_warns_without_false_missing_lineage_error(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_exact_merge_warns_without_false_missing_lineage_error(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_persisted_edges_warn_when_predecessor_is_removed(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_same_kind_historical_id_resurrection(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_cross_kind_historical_id_resurrection(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_old_current_predecessor_remains_or_retires_without_resurrection(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_scalar_list_stable_id_kind_change(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_rejects_singleton_stable_id_kind_change(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_same_kind_semantic_change_does_not_trigger_kind_error(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_persisted_supersedes_edges_are_canonical_sets(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_removed_historical_supersedes_edge_is_error(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_only_new_supersedes_edges_require_old_snapshot_validation(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_unmapped_changed_replacement_is_human_review(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_capability_semantics_impact_all_coverage(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_optional_interaction_bundle_is_minor(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_added_value_in_required_capability_membership_is_major(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_uncertain_optional_interaction_is_human_review(init_repo(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        test_dependency_add_change_remove_impacts_required_interaction_coverage(init_repo(Path(raw)))
    print("contract diff ok")


if __name__ == "__main__":
    main()
