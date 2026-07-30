from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.classification import check_classification


VALID = """version: 0.1.0
request:
  originalStatement: 用户希望新增流程协作评论。
contractSearch:
  status: not-found
  refs: []
classification: capability-change
contractChangeRequired: true
reason: 新增用户可依赖的协作结果与失败边界。
nextArtifact: contract-change-proposal.md
decisionSource: ai-proposed
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def assert_value_error(expected: str, callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing: {expected}")


def replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_routes_and_document_shape(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-IK152D" / "classification.yaml"
    write(path, VALID)
    result = check_classification(repo_root, "IK152D")
    assert result.path == path.resolve()
    assert result.classification == "capability-change"

    cases = (
        ("originalStatement", replace(VALID, "originalStatement: 用户希望新增流程协作评论。", "originalStatement: ''"), "originalStatement must be non-empty"),
        ("search status", replace(VALID, "  status: not-found\n", ""), "contractSearch missing required fields: status"),
        ("classification", replace(VALID, "classification: capability-change", "classification: unsupported"), "invalid classification"),
        ("capability change", replace(VALID, "contractChangeRequired: true", "contractChangeRequired: false"), "capability-change requires contractChangeRequired: true"),
        (
            "implementation gap refs",
            replace(
                replace(replace(VALID, "classification: capability-change", "classification: implementation-gap"), "  status: not-found", "  status: found"),
                "contractChangeRequired: true",
                "contractChangeRequired: false",
            ),
            "contractSearch.refs must identify found contracts",
        ),
        (
            "future implementation",
            replace(
                replace(replace(VALID, "classification: capability-change", "classification: future"), "contractChangeRequired: true", "contractChangeRequired: false"),
                "nextArtifact: contract-change-proposal.md",
                "nextArtifact: implementation-plan.md",
            ),
            "future must not route to current implementation",
        ),
    )
    for name, document, expected in cases:
        write(path, document)
        assert_value_error(expected, lambda: check_classification(repo_root, "IK152D"))
        assert name


def test_safe_yaml_and_containment(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-draft"
    path = issue_dir / "classification.yaml"
    write(path, VALID)

    invalid_documents = (
        ("root mapping", "- classification\n", "classification document must be a mapping"),
        ("boolean type", replace(VALID, "contractChangeRequired: true", "contractChangeRequired: 'true'"), "contractChangeRequired must be a boolean"),
        ("duplicate key", VALID + "classification: future\n", "duplicate YAML key: classification"),
        ("quoted duplicate key", VALID + "\"classification\": capability-change\n", "duplicate YAML key: classification"),
        ("non-string key", VALID + "1: unsafe\n", "classification document keys must be strings"),
        ("alias", replace(VALID, "reason: 新增用户可依赖的协作结果与失败边界。", "reason: &shared 新增用户可依赖的协作结果与失败边界。"), "YAML aliases, anchors, and tags are not allowed"),
    )
    for name, document, expected in invalid_documents:
        write(path, document)
        assert_value_error(expected, lambda: check_classification(repo_root, "draft"))
        assert name

    outside = repo_root / "classification.yaml"
    write(outside, VALID)
    assert_value_error(
        "classification file must stay inside the issue directory",
        lambda: check_classification(repo_root, "draft", outside),
    )

    write(path, VALID)
    linked = issue_dir / "linked.yaml"
    try:
        os.symlink(outside, linked)
    except OSError:
        return
    assert_value_error(
        "classification file must stay inside the issue directory",
        lambda: check_classification(repo_root, "draft", linked),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo_root = Path(raw)
        test_routes_and_document_shape(repo_root)
        test_safe_yaml_and_containment(repo_root)
    print("classification core ok")


if __name__ == "__main__":
    main()
