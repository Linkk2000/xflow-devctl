from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.classification import check_classification
from xflow import classification as classification_module


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


def document(
    classification: str,
    contract_change_required: bool,
    next_artifact: str,
    *,
    search_status: str = "not-found",
    refs: tuple[str, ...] = (),
    version: object = "0.1.0",
) -> str:
    rendered_refs = "[" + ", ".join(refs) + "]"
    rendered_version = str(version).lower() if isinstance(version, bool) else str(version)
    return f"""version: {rendered_version}
request:
  originalStatement: 用户希望处理当前请求。
contractSearch:
  status: {search_status}
  refs: {rendered_refs}
classification: {classification}
contractChangeRequired: {str(contract_change_required).lower()}
reason: 分类理由完整且可审核。
nextArtifact: {next_artifact}
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


def run_devctl(
    repo_root: Path,
    *args: str,
    pythonpath: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "DEVCTL_SKIP_PROVIDER_LOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": pythonpath or str(OPS_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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
            "future requires nextArtifact: futureCapabilitiesOutOfScope",
        ),
    )
    for name, document, expected in cases:
        write(path, document)
        assert_value_error(expected, lambda: check_classification(repo_root, "IK152D"))
        assert name


def test_approved_route_table(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-route" / "classification.yaml"
    valid_routes = (
        document("capability-change", True, "contract-change-proposal.md"),
        document(
            "capability-change",
            True,
            "contract-change-proposal.md",
            search_status="found",
            refs=("docs/requirements/example/contract.yaml",),
        ),
        document(
            "implementation-gap",
            False,
            "gap-analysis.md",
            search_status="found",
            refs=("example.contract.current@1.0.0",),
        ),
        document(
            "ui-defect",
            False,
            "issue-draft.md",
            search_status="found",
            refs=("requirement:UI-17",),
        ),
        document("infrastructure", False, "dependency-issue-draft.md"),
        document(
            "infrastructure",
            False,
            "dependency-issue-draft.md",
            search_status="found",
            refs=("example.contract.shared-runtime@1.0.0",),
        ),
        document("governance", False, "issue-draft.md"),
        document(
            "governance",
            False,
            "issue-draft.md",
            search_status="found",
            refs=("requirement:GOV-3",),
        ),
        document("future", False, "futureCapabilitiesOutOfScope"),
        document(
            "future",
            False,
            "futureCapabilitiesOutOfScope",
            search_status="found",
            refs=("example.contract.current@1.0.0",),
        ),
    )
    for route in valid_routes:
        write(path, route)
        check_classification(repo_root, "route")

    invalid_routes = (
        (document("capability-change", False, "contract-change-proposal.md"), "capability-change requires contractChangeRequired: true"),
        (document("capability-change", True, "resolution-report.md"), "capability-change requires nextArtifact: contract-change-proposal.md"),
        (
            document("implementation-gap", True, "gap-analysis.md", search_status="found", refs=("contract:a",)),
            "implementation-gap requires contractChangeRequired: false",
        ),
        (document("implementation-gap", False, "gap-analysis.md"), "implementation-gap requires contractSearch.status: found"),
        (
            document("implementation-gap", False, "contract-change-proposal.md", search_status="found", refs=("contract:a",)),
            "implementation-gap requires nextArtifact: gap-analysis.md",
        ),
        (
            document("ui-defect", False, "issue-draft.md"),
            "ui-defect requires contractSearch.status: found",
        ),
        (
            document("ui-defect", True, "issue-draft.md", search_status="found", refs=("requirement:UI-17",)),
            "ui-defect requires contractChangeRequired: false",
        ),
        (
            document("ui-defect", False, "implementation-plan.md", search_status="found", refs=("requirement:UI-17",)),
            "ui-defect requires nextArtifact: issue-draft.md",
        ),
        (document("infrastructure", True, "dependency-issue-draft.md"), "infrastructure requires contractChangeRequired: false"),
        (document("infrastructure", False, "issue-draft.md"), "infrastructure requires nextArtifact: dependency-issue-draft.md"),
        (document("governance", True, "issue-draft.md"), "governance requires contractChangeRequired: false"),
        (document("governance", False, "implementation-plan.md"), "governance requires nextArtifact: issue-draft.md"),
        (document("future", True, "futureCapabilitiesOutOfScope"), "future requires contractChangeRequired: false"),
        (document("future", False, "build-plan.md"), "future requires nextArtifact: futureCapabilitiesOutOfScope"),
    )
    for route, expected in invalid_routes:
        write(path, route)
        assert_value_error(expected, lambda: check_classification(repo_root, "route"))


def test_supported_version(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-version" / "classification.yaml"
    write(path, replace(VALID, "version: 0.1.0", "version: 0.2.0"))
    assert_value_error("unsupported classification version: 0.2.0", lambda: check_classification(repo_root, "version"))
    write(path, replace(VALID, "version: 0.1.0", "version: 1"))
    assert_value_error("version must be a string", lambda: check_classification(repo_root, "version"))


def test_cli_failures_are_normalized(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-cli" / "classification.yaml"
    write(path, VALID)
    valid = run_devctl(repo_root, "check", "classification", "--issue", "cli")
    assert valid.returncode == 0, valid.stderr
    assert "classification check passed" in valid.stdout

    write(path, "version: [\n")
    malformed = run_devctl(repo_root, "check", "classification", "--issue", "cli")
    assert malformed.returncode == 1
    assert malformed.stderr.startswith("[ERROR] invalid classification YAML:")
    assert "Traceback" not in malformed.stderr

    missing = run_devctl(repo_root, "check", "classification", "--issue", "missing")
    assert missing.returncode == 1
    assert missing.stderr.startswith("[ERROR] missing classification file:")

    missing_issue = run_devctl(repo_root, "check", "classification")
    assert missing_issue.returncode == 2
    assert "--issue" in missing_issue.stderr

    shadow = repo_root / "shadow"
    write(
        shadow / "yaml.py",
        "raise ModuleNotFoundError(\"No module named 'yaml'\", name='yaml')\n",
    )
    write(path, VALID)
    no_yaml = run_devctl(
        repo_root,
        "check",
        "classification",
        "--issue",
        "cli",
        pythonpath=str(shadow) + os.pathsep + str(OPS_ROOT),
    )
    assert no_yaml.returncode == 1
    assert no_yaml.stderr.startswith(
        "[ERROR] dependency checks require PyYAML; run: python -m pip install -r requirements.txt"
    ), no_yaml.stderr
    assert "Traceback" not in no_yaml.stderr


def test_safe_yaml_and_containment(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-draft"
    path = issue_dir / "classification.yaml"
    write(path, VALID)

    invalid_documents = (
        ("root mapping", "- classification\n", "classification document must be a mapping"),
        ("boolean type", replace(VALID, "contractChangeRequired: true", "contractChangeRequired: 'true'"), "contractChangeRequired must be a boolean"),
        ("duplicate key", VALID + "classification: future\n", "duplicate YAML key: classification"),
        ("quoted duplicate key", VALID + "\"classification\": capability-change\n", "duplicate YAML key: classification"),
        ("non-string key", VALID + "1: unsafe\n", "YAML mapping keys must be strings"),
        ("alias", replace(VALID, "reason: 新增用户可依赖的协作结果与失败边界。", "reason: &shared 新增用户可依赖的协作结果与失败边界。"), "YAML aliases, anchors, and tags are not allowed"),
        ("tag", replace(VALID, "reason: 新增用户可依赖的协作结果与失败边界。", "reason: !!str 新增用户可依赖的协作结果与失败边界。"), "YAML aliases, anchors, and tags are not allowed"),
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


def test_safe_loader_duplicate_semantics(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-yaml" / "classification.yaml"
    quoted_unique = """"version": 0.1.0
"request":
  "originalStatement": 用户希望新增流程协作评论。
"contractSearch":
  "status": not-found
  "refs": []
"classification": capability-change
"contractChangeRequired": true
"reason": 新增用户可依赖的协作结果与失败边界。
"nextArtifact": contract-change-proposal.md
"decisionSource": ai-proposed
"""
    write(path, quoted_unique)
    check_classification(repo_root, "yaml")

    flow_duplicate = """{version: 0.1.0,
request: {originalStatement: 用户希望新增流程协作评论。},
contractSearch: {status: not-found, refs: []},
classification: capability-change,
classification: capability-change,
contractChangeRequired: true,
reason: 新增用户可依赖的协作结果与失败边界。,
nextArtifact: contract-change-proposal.md,
decisionSource: ai-proposed}
"""
    explicit_duplicate = replace(
        VALID,
        "classification: capability-change",
        "? classification\n: capability-change\n? classification\n: capability-change",
    )
    nested_duplicate = replace(
        VALID,
        "  originalStatement: 用户希望新增流程协作评论。",
        "  originalStatement: 用户希望新增流程协作评论。\n  originalStatement: 重复陈述。",
    )
    duplicate_documents = (
        VALID + "classification: capability-change\n",
        VALID + '"classification": capability-change\n',
        flow_duplicate,
        explicit_duplicate,
        nested_duplicate,
    )
    for duplicate in duplicate_documents:
        write(path, duplicate)
        assert_value_error("duplicate YAML key", lambda: check_classification(repo_root, "yaml"))


def test_decoder_limits(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-limits" / "classification.yaml"
    oversized = VALID + "#" + ("x" * 270_000) + "\n"
    token_heavy = replace(VALID, "  refs: []", "  refs: [" + ",".join("x" for _ in range(5_000)) + "]")
    node_heavy = replace(VALID, "  refs: []", "  refs: [" + ",".join(f"r{index}" for index in range(1_100)) + "]")
    collection_heavy = replace(VALID, "  refs: []", "  refs: [" + ",".join(f"r{index}" for index in range(257)) + "]")
    scalar_heavy = replace(
        VALID,
        "reason: 新增用户可依赖的协作结果与失败边界。",
        "reason: " + ("x" * 70_000),
    )
    nested_value = "leaf"
    for index in range(40):
        nested_value = "{" + f"level{index}: " + nested_value + "}"
    depth_heavy = replace(VALID, "reason: 新增用户可依赖的协作结果与失败边界。", "reason: " + nested_value)
    cases = (
        (oversized, "classification file exceeds 262144 bytes"),
        (token_heavy, "classification YAML exceeds token limit"),
        (node_heavy, "classification YAML exceeds node limit"),
        (collection_heavy, "classification YAML exceeds collection limit"),
        (scalar_heavy, "classification YAML scalar exceeds 65536 characters"),
        (depth_heavy, "classification YAML exceeds nesting limit"),
    )
    for payload, expected in cases:
        write(path, payload)
        assert_value_error(expected, lambda: check_classification(repo_root, "limits"))


def test_replacement_races_are_rejected(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-race"
    path = issue_dir / "classification.yaml"
    replacement = issue_dir / "replacement.yaml"
    write(path, VALID)
    write(replacement, VALID)

    real_open = classification_module.os.open
    opened = False

    def replace_before_open(target: object, *args: object, **kwargs: object) -> int:
        nonlocal opened
        if Path(target) == path and not opened:
            opened = True
            os.replace(replacement, path)
        return real_open(target, *args, **kwargs)  # type: ignore[arg-type]

    classification_module.os.open = replace_before_open  # type: ignore[assignment]
    try:
        assert_value_error("classification file changed while opening", lambda: check_classification(repo_root, "race"))
    finally:
        classification_module.os.open = real_open

    write(path, VALID)
    write(replacement, VALID)
    real_close = classification_module.os.close
    read_finished = False

    def replace_after_read(descriptor: int) -> None:
        nonlocal read_finished
        real_close(descriptor)
        if not read_finished:
            read_finished = True
            os.replace(replacement, path)

    classification_module.os.close = replace_after_read
    try:
        assert_value_error("classification file changed while reading", lambda: check_classification(repo_root, "race"))
    finally:
        classification_module.os.close = real_close


def test_native_windows_junction_swap_is_rejected(repo_root: Path) -> None:
    if os.name != "nt":
        return
    issue_dir = repo_root / ".xflow" / "issues" / "issue-junction"
    path = issue_dir / "classification.yaml"
    saved_dir = issue_dir.with_name("issue-junction-saved")
    outside_dir = repo_root / "outside-junction"
    write(path, VALID)
    outside_dir.mkdir(parents=True)
    os.link(path, outside_dir / "classification.yaml")

    real_open = classification_module.os.open
    swapped = False

    def swap_to_junction(target: object, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if Path(target) == path and not swapped:
            swapped = True
            os.rename(issue_dir, saved_dir)
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(issue_dir), str(outside_dir)],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if junction.returncode != 0:
                os.rename(saved_dir, issue_dir)
                swapped = False
                raise AssertionError(junction.stderr or junction.stdout)
        return real_open(target, *args, **kwargs)  # type: ignore[arg-type]

    classification_module.os.open = swap_to_junction  # type: ignore[assignment]
    try:
        assert_value_error(
            "classification file handle resolves outside the issue directory",
            lambda: check_classification(repo_root, "junction"),
        )
    finally:
        classification_module.os.open = real_open
        if swapped and os.path.lexists(issue_dir):
            os.rmdir(issue_dir)
        if saved_dir.exists():
            os.rename(saved_dir, issue_dir)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo_root = Path(raw)
        test_routes_and_document_shape(repo_root)
        test_approved_route_table(repo_root)
        test_supported_version(repo_root)
        test_safe_yaml_and_containment(repo_root)
        test_safe_loader_duplicate_semantics(repo_root)
        test_decoder_limits(repo_root)
        test_replacement_races_are_rejected(repo_root)
        test_native_windows_junction_swap_is_rejected(repo_root)
        test_cli_failures_are_normalized(repo_root)
    print("classification core ok")


if __name__ == "__main__":
    main()
