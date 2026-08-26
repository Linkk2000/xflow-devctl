from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

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
    write_text_lf(path, text)


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
            "future requires nextArtifact: futureCapabilitiesOutOfScope or future-task-proposal.md",
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
            "lightweight-route-complete",
            search_status="found",
            refs=("requirement:UI-17",),
        ),
        document("ui-defect", False, "lightweight-route-complete"),
        document("infrastructure", False, "dependency-issue-proposal.md"),
        document(
            "infrastructure",
            False,
            "dependency-issue-proposal.md",
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
        document("future", False, "future-task-proposal.md"),
        document(
            "future",
            False,
            "future-task-proposal.md",
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
            "ui-defect requires nextArtifact: lightweight-route-complete",
        ),
        (
            document("ui-defect", True, "lightweight-route-complete", search_status="found", refs=("requirement:UI-17",)),
            "ui-defect requires contractChangeRequired: false",
        ),
        (
            document("ui-defect", False, "implementation-plan.md", search_status="found", refs=("requirement:UI-17",)),
            "ui-defect requires nextArtifact: lightweight-route-complete",
        ),
        (document("infrastructure", True, "dependency-issue-proposal.md"), "infrastructure requires contractChangeRequired: false"),
        (document("infrastructure", False, "dependency-issue-draft.md"), "infrastructure requires nextArtifact: dependency-issue-proposal.md"),
        (document("infrastructure", False, "issue-draft.md"), "infrastructure requires nextArtifact: dependency-issue-proposal.md"),
        (document("governance", True, "issue-draft.md"), "governance requires contractChangeRequired: false"),
        (document("governance", False, "implementation-plan.md"), "governance requires nextArtifact: issue-draft.md"),
        (document("future", True, "futureCapabilitiesOutOfScope"), "future requires contractChangeRequired: false"),
        (document("future", True, "future-task-proposal.md"), "future requires contractChangeRequired: false"),
        (
            document("future", False, "build-plan.md"),
            "future requires nextArtifact: futureCapabilitiesOutOfScope or future-task-proposal.md",
        ),
    )
    for route, expected in invalid_routes:
        write(path, route)
        assert_value_error(expected, lambda: check_classification(repo_root, "route"))


def test_ui_defect_lightweight_terminal_route(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-ui" / "classification.yaml"
    for route in (
        document("ui-defect", False, "lightweight-route-complete"),
        document(
            "ui-defect",
            False,
            "lightweight-route-complete",
            search_status="found",
            refs=("requirement:UI-17",),
        ),
    ):
        write(path, route)
        check_classification(repo_root, "ui")

    write(path, document("ui-defect", False, "issue-draft.md"))
    assert_value_error(
        "ui-defect requires nextArtifact: lightweight-route-complete",
        lambda: check_classification(repo_root, "ui"),
    )


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
        "classification file must not traverse a symlink, junction, or reparse point",
        lambda: check_classification(repo_root, "draft", linked),
    )

    self_loop = issue_dir / "self-loop.yaml"
    try:
        os.symlink(self_loop, self_loop)
    except OSError:
        return
    assert_value_error(
        "classification file must not traverse a symlink, junction, or reparse point",
        lambda: check_classification(repo_root, "draft", self_loop),
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


def test_native_descriptor_failures_are_normalized(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-race"
    path = issue_dir / "classification.yaml"
    class FakeWindowsApi:
        def __init__(self, *, fail_open: bool = False, fail_size: bool = False, fail_close: bool = False) -> None:
            self.fail_open = fail_open
            self.fail_size = fail_size
            self.fail_close = fail_close
            self._paths: dict[int, Path] = {}
            self._read = False

        def open(self, target: Path, *, directory: bool) -> int:
            if self.fail_open:
                raise OSError("simulated open race")
            handle = len(self._paths) + 1
            self._paths[handle] = target
            return handle

        def attributes(self, handle: int) -> int:
            return (
                classification_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                if self._paths[handle] != path
                else 0
            )

        def file_size(self, handle: int) -> int:
            if self.fail_size:
                raise OSError("simulated fstat race")
            return len(VALID.encode("utf-8"))

        def snapshot(self, handle: int) -> tuple[object, ...]:
            if self.fail_size:
                raise OSError("simulated fstat race")
            return (1, 2, len(VALID.encode("utf-8")), 3, 4)

        def final_path(self, handle: int) -> Path:
            return self._paths[handle]

        def is_regular_file(self, handle: int) -> bool:
            return True

        def read(self, handle: int, size: int) -> bytes:
            if self._read:
                return b""
            self._read = True
            return VALID.encode("utf-8")

        def close(self, handle: int) -> None:
            if self.fail_close:
                raise OSError("simulated close race")

    assert_value_error(
        "cannot open classification file safely",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, FakeWindowsApi(fail_open=True)),
    )
    assert_value_error(
        "classification file changed while opening",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, FakeWindowsApi(fail_size=True)),
    )
    assert_value_error(
        "cannot close classification file safely",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, FakeWindowsApi(fail_close=True)),
    )


def test_windows_reparse_component_never_opens_unc_target(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-unc"
    path = issue_dir / "classification.yaml"

    class FakeWindowsApi:
        def __init__(self) -> None:
            self.opened: list[Path] = []
            self.closed: list[int] = []
            self._paths: dict[int, Path] = {}

        def open(self, target: Path, *, directory: bool) -> int:
            assert not str(target).startswith("\\\\"), target
            self.opened.append(target)
            handle = len(self.opened)
            self._paths[handle] = target
            return handle

        def attributes(self, handle: int) -> int:
            if self._paths[handle] == issue_dir:
                return classification_module._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            return classification_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY

        def file_size(self, handle: int) -> int:
            return 0

        def final_path(self, handle: int) -> Path:
            return self._paths[handle]

        def read(self, handle: int, size: int) -> bytes:
            raise AssertionError("a reparse component must be rejected before reading")

        def close(self, handle: int) -> None:
            self.closed.append(handle)

    fake = FakeWindowsApi()
    assert_value_error(
        "classification file must not traverse a symlink, junction, or reparse point",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, fake),
    )
    assert fake.opened == [
        repo_root,
        repo_root / ".xflow",
        repo_root / ".xflow" / "issues",
        issue_dir,
    ]
    assert fake.closed == [4, 3, 2, 1]


def test_windows_final_path_mismatch_is_rejected_before_read(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-owner"
    path = issue_dir / "classification.yaml"

    class FakeWindowsApi:
        def __init__(self) -> None:
            self._paths: dict[int, Path] = {}
            self.read_called = False

        def open(self, target: Path, *, directory: bool) -> int:
            handle = len(self._paths) + 1
            self._paths[handle] = target
            return handle

        def attributes(self, handle: int) -> int:
            if self._paths[handle] == path:
                return 0
            return classification_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY

        def final_path(self, handle: int) -> Path:
            target = self._paths[handle]
            if target == path:
                return issue_dir / "other.yaml"
            return target

        def file_size(self, handle: int) -> int:
            return len(VALID.encode("utf-8"))

        def is_regular_file(self, handle: int) -> bool:
            return True

        def read(self, handle: int, size: int) -> bytes:
            self.read_called = True
            raise AssertionError("ownership mismatch must be rejected before reading")

        def close(self, handle: int) -> None:
            pass

    fake = FakeWindowsApi()
    assert_value_error(
        "classification file handle path mismatch",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, fake),
    )
    assert not fake.read_called


def test_windows_in_place_write_during_chunked_read_is_rejected(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-write-win"
    path = issue_dir / "classification.yaml"
    payload = VALID.encode("utf-8")

    class FakeWindowsApi:
        def __init__(self) -> None:
            self._paths: dict[int, Path] = {}
            self._read_count = 0
            self._changed = False

        def open(self, target: Path, *, directory: bool) -> int:
            handle = len(self._paths) + 1
            self._paths[handle] = target
            return handle

        def attributes(self, handle: int) -> int:
            if self._paths[handle] == path:
                return 0
            return classification_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY

        def final_path(self, handle: int) -> Path:
            return self._paths[handle]

        def snapshot(self, handle: int) -> tuple[object, ...]:
            return (7, 11, len(payload), 101, 202 if self._changed else 201)

        def file_size(self, handle: int) -> int:
            return len(payload)

        def is_regular_file(self, handle: int) -> bool:
            return True

        def read(self, handle: int, size: int) -> bytes:
            self._read_count += 1
            if self._read_count == 1:
                self._changed = True
                return payload[: len(payload) // 2]
            if self._read_count == 2:
                return payload[len(payload) // 2 :]
            return b""

        def close(self, handle: int) -> None:
            pass

    assert_value_error(
        "classification file changed while reading",
        lambda: classification_module._read_stable_text_windows(repo_root, path, issue_dir, FakeWindowsApi()),
    )


def test_posix_in_place_write_during_chunked_read_is_rejected(repo_root: Path) -> None:
    issue_dir = repo_root / ".xflow" / "issues" / "issue-write-posix"
    path = issue_dir / "classification.yaml"
    payload = VALID.encode("utf-8")
    mutated_payload = b"X" + payload[1:]

    class FakeStat:
        def __init__(self, *, directory: bool) -> None:
            self.st_mode = stat.S_IFDIR if directory else stat.S_IFREG
            self.st_dev = 3
            self.st_ino = 5
            self.st_size = len(payload)
            self.st_mtime_ns = 300
            self.st_ctime_ns = 400

    class FakePosixApi:
        def __init__(self) -> None:
            self._next_descriptor = 1
            self._file_descriptor = 0
            self._read_count = 0
            self._mutated = False

        def open_root(self, target: Path) -> int:
            return self._new_descriptor()

        def open_child(self, parent: int, name: str, *, directory: bool) -> int:
            descriptor = self._new_descriptor()
            if not directory:
                self._file_descriptor = descriptor
            return descriptor

        def stat(self, descriptor: int) -> FakeStat:
            return FakeStat(directory=descriptor != self._file_descriptor)

        def read(self, descriptor: int, size: int) -> bytes:
            self._read_count += 1
            current = mutated_payload if self._mutated else payload
            if self._read_count == 1:
                split = len(payload) // 2
                chunk = current[:split]
                self._mutated = True
                return chunk
            if self._read_count == 2:
                return current[len(payload) // 2 :]
            return b""

        def rewind(self, descriptor: int) -> None:
            self._read_count = 0

        def close(self, descriptor: int) -> None:
            pass

        def _new_descriptor(self) -> int:
            descriptor = self._next_descriptor
            self._next_descriptor += 1
            return descriptor

    assert_value_error(
        "classification file changed while reading",
        lambda: classification_module._read_stable_text_posix(repo_root, path, FakePosixApi()),
    )


def test_cli_delete_before_lstat_is_normalized(repo_root: Path) -> None:
    path = repo_root / ".xflow" / "issues" / "issue-deleted" / "classification.yaml"
    write(path, VALID)
    path.unlink()
    result = run_devctl(repo_root, "check", "classification", "--issue", "deleted")
    assert result.returncode == 1
    assert result.stderr.startswith("[ERROR] missing classification file:")
    assert "Traceback" not in result.stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo_root = Path(raw)
        test_routes_and_document_shape(repo_root)
        test_approved_route_table(repo_root)
        test_ui_defect_lightweight_terminal_route(repo_root)
        test_supported_version(repo_root)
        test_safe_yaml_and_containment(repo_root)
        test_safe_loader_duplicate_semantics(repo_root)
        test_decoder_limits(repo_root)
        test_native_descriptor_failures_are_normalized(repo_root)
        test_windows_reparse_component_never_opens_unc_target(repo_root)
        test_windows_final_path_mismatch_is_rejected_before_read(repo_root)
        test_windows_in_place_write_during_chunked_read_is_rejected(repo_root)
        test_posix_in_place_write_during_chunked_read_is_rejected(repo_root)
        test_cli_failures_are_normalized(repo_root)
        test_cli_delete_before_lstat_is_normalized(repo_root)
    print("classification core ok")


if __name__ == "__main__":
    main()
