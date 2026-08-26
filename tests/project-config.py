from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

from xflow import migration
from xflow.io import canonical_path
from xflow.migration import apply_issue_workspace_migration, inspect_issue_workspace_migration
from xflow.project_config import load_project_config, require_safe_repo_path


def write(path: Path, text: str) -> None:
    write_text_lf(path, text)


def write_json(path: Path, payload: dict[str, object]) -> None:
    write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def make_directory_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)


def assert_value_error(expected: str, callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing: {expected}")


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
    assert result.returncode == expect, (result.stdout, result.stderr)
    return result


def test_default_config(repo_root: Path) -> None:
    config = load_project_config(repo_root)
    assert config.issue_workspace_mode == "tracked"
    assert config.contract_root == Path("docs/requirements")


def test_namespaced_config_validation(repo_root: Path) -> None:
    config_path = repo_root / ".xflow" / "xflow.json"
    write_json(
        config_path,
        {
            "version": "0.1.0",
            "mode": "vendor",
            "workflow": {"source": "project"},
            "devctl": {"source": "project"},
            "humanGated": True,
            "projectOwned": {"keep": True},
            "issueWorkspace": {"mode": "local"},
            "contracts": {"root": "docs/contracts"},
        },
    )
    config = load_project_config(repo_root)
    assert config.issue_workspace_mode == "local"
    assert config.contract_root == Path("docs/contracts")

    for namespace in (
        {"issueWorkspace": None},
        {"contracts": None},
        {"issueWorkspace": {"mode": "invalid"}},
        {"issueWorkspace": {"mode": ["tracked"]}},
        {"contracts": {"root": "C:/absolute"}},
        {"contracts": {"root": "C:drive-relative"}},
        {"contracts": {"root": "/rooted"}},
        {"contracts": {"root": "\\rooted"}},
        {"contracts": {"root": "\\\\server\\share"}},
        {"contracts": {"root": "\\\\?\\C:\\device"}},
        {"contracts": {"root": "file:///C:/outside"}},
        {"contracts": {"root": "../escape"}},
        {"contracts": {"root": ["docs/requirements"]}},
    ):
        write_json(config_path, namespace)
        assert_value_error(".xflow/xflow.json", lambda: load_project_config(repo_root))


def test_check_reports_exact_ignore_source(repo_root: Path) -> None:
    write(repo_root / ".gitignore", ".xflow/issues/\n*.tmp\n")
    report = inspect_issue_workspace_migration(repo_root, "tracked")
    assert report.exact_ignore_lines == (".xflow/issues/",)
    assert report.git_ignore_source is not None
    check = run_devctl(repo_root, "migrate", "issue-workspace", "--mode", "tracked", "--check")
    assert "exact issue workspace ignore line: .xflow/issues/" in check.stdout
    assert "git ignore source:" in check.stdout
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == ".xflow/issues/\n*.tmp\n"
    apply = run_devctl(repo_root, "migrate", "issue-workspace", "--mode", "tracked", "--apply")
    assert "removed exact .gitignore lines: .xflow/issues/" in apply.stdout


def test_apply_preserves_config_and_removes_only_exact_ignore_lines(repo_root: Path) -> None:
    config_path = repo_root / ".xflow" / "xflow.json"
    original = {
        "version": "0.1.0",
        "mode": "vendor",
        "workflow": {"source": "project"},
        "devctl": {"source": "project"},
        "humanGated": True,
        "projectOwned": {"keep": True},
    }
    write_json(config_path, original)
    write(repo_root / ".gitignore", ".xflow/issues\n.xflow/issues/\n*.tmp\n")

    report = apply_issue_workspace_migration(repo_root, "tracked")
    assert report.blockers == ()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    for name, value in original.items():
        assert saved[name] == value
    assert saved["issueWorkspace"] == {"mode": "tracked"}
    assert saved["contracts"] == {"root": "docs/requirements"}
    assert list(saved)[:6] == list(original)
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "*.tmp\n"

    apply_issue_workspace_migration(repo_root, "local")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["issueWorkspace"] == {"mode": "local"}
    assert saved["contracts"] == {"root": "docs/requirements"}
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "*.tmp\n"


def test_apply_retains_configured_contract_root(repo_root: Path) -> None:
    config_path = repo_root / ".xflow" / "xflow.json"
    write_json(config_path, {"contracts": {"root": "docs/contracts"}})
    apply_issue_workspace_migration(repo_root, "local")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["issueWorkspace"] == {"mode": "local"}
    assert saved["contracts"] == {"root": "docs/contracts"}


def test_apply_blocks_active_approvals_and_unsafe_issue_content(repo_root: Path) -> None:
    approval = repo_root / ".xflow" / "issues" / "issue-IK152D" / "approvals" / "local-review.md"
    write(approval, "Approved: yes\n")
    report = inspect_issue_workspace_migration(repo_root, "tracked")
    assert report.active_approvals == (approval.resolve(strict=False),)
    assert "active approval" in report.blockers[0]
    assert_value_error("active approvals", lambda: apply_issue_workspace_migration(repo_root, "tracked"))
    assert not (repo_root / ".xflow" / "xflow.json").exists()

    approval.unlink()
    unsafe = repo_root / ".xflow" / "issues" / "issue-IK152D" / "walkthrough.md"
    write(unsafe, "token=secret\nC:\\Users\\person\\workspace\n")
    oversized = repo_root / ".xflow" / "issues" / "issue-IK152D" / "evidence.bin"
    oversized.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    report = inspect_issue_workspace_migration(repo_root, "tracked")
    assert report.credential_files == (unsafe.resolve(strict=False),)
    assert report.absolute_path_files == (unsafe.resolve(strict=False),)
    assert report.oversized_files == (oversized.resolve(strict=False),)
    assert_value_error("unsafe issue workspace", lambda: apply_issue_workspace_migration(repo_root, "tracked"))


def test_reparse_points_are_rejected(root: Path) -> None:
    outside_config = root / "outside-config"
    outside_config.mkdir(parents=True)
    write_json(outside_config / "xflow.json", {"contracts": {"root": "docs/requirements"}})
    linked_config_repo = root / "linked-config"
    linked_config_repo.mkdir()
    make_directory_link(linked_config_repo / ".xflow", outside_config)
    assert_value_error("reparse", lambda: load_project_config(linked_config_repo))

    outside_evidence = root / "outside-evidence"
    outside_evidence.mkdir()
    write(outside_evidence / "secret.txt", '"client_secret": "hidden"\n')
    linked_issue_repo = root / "linked-issue"
    issue_root = linked_issue_repo / ".xflow" / "issues" / "issue-1"
    issue_root.mkdir(parents=True)
    make_directory_link(issue_root / "evidence", outside_evidence)
    report = inspect_issue_workspace_migration(linked_issue_repo, "tracked")
    assert any("reparse" in error for error in report.scan_errors), report.scan_errors
    assert_value_error("scan errors", lambda: apply_issue_workspace_migration(linked_issue_repo, "tracked"))


def test_safe_repo_path_accepts_equivalent_root_spelling(root: Path) -> None:
    repo = root / "equivalent-root"
    write(repo / "inside.txt", "safe\n")
    alias = Path(str(repo).replace("/private/var/", "/var/"))
    if alias == repo:
        alias = root / "equivalent-root-alias"
        make_directory_link(alias, repo)
    accepted = require_safe_repo_path(alias, alias / "inside.txt", "safe path")
    assert accepted.resolve(strict=False) == (repo / "inside.txt").resolve(strict=False)


def test_safe_repo_path_rejects_explicit_symlink(root: Path) -> None:
    repo = root / "explicit-symlink"
    repo.mkdir(parents=True)
    outside = root / "outside-evidence.txt"
    write(outside, "outside\n")
    linked = repo / "linked.txt"
    try:
        linked.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    assert_value_error(
        "must not traverse a symlink, junction, or reparse point",
        lambda: require_safe_repo_path(repo, linked, "safe path"),
    )


def test_contract_root_must_resolve_inside_repository(root: Path) -> None:
    repo = root / "contract-link"
    outside = root / "outside-contracts"
    outside.mkdir(parents=True)
    make_directory_link(repo / "docs" / "contracts", outside)
    write_json(repo / ".xflow" / "xflow.json", {"contracts": {"root": "docs/contracts"}})
    assert_value_error("reparse", lambda: load_project_config(repo))


def test_effective_ignore_sources_block_manual_actions(root: Path) -> None:
    external_rule_repo = root / "external-ignore"
    external_rule_repo.mkdir(parents=True)
    git(external_rule_repo, "init", "-q")
    git_dir = Path(
        subprocess.run(
            ["git", "-C", str(external_rule_repo), "rev-parse", "--git-dir"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    if not git_dir.is_absolute():
        git_dir = external_rule_repo / git_dir
    write(git_dir / "info" / "exclude", ".xflow/issues/\n")
    report = inspect_issue_workspace_migration(external_rule_repo, "tracked")
    assert any("manual action" in blocker for blocker in report.blockers), report.blockers
    check = run_devctl(external_rule_repo, "migrate", "issue-workspace", "--mode", "tracked", "--check")
    assert "manual action required" in check.stdout
    assert_value_error("manual action", lambda: apply_issue_workspace_migration(external_rule_repo, "tracked"))

    broad_rule_repo = root / "broad-ignore"
    broad_rule_repo.mkdir(parents=True)
    git(broad_rule_repo, "init", "-q")
    write(broad_rule_repo / ".gitignore", ".xflow/issues/*\n")
    assert_value_error("manual action", lambda: apply_issue_workspace_migration(broad_rule_repo, "tracked"))
    assert not (broad_rule_repo / ".xflow" / "xflow.json").exists()


def test_tracked_apply_verifies_effective_ignore_and_rolls_back(root: Path) -> None:
    repo = root / "fallback-ignore"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    original_ignore = ".xflow/issues/*\n.xflow/issues/\n"
    original_config = '{"projectOwned": true}\n'
    write(repo / ".gitignore", original_ignore)
    write(repo / ".xflow" / "xflow.json", original_config)
    assert_value_error("still effectively ignored", lambda: apply_issue_workspace_migration(repo, "tracked"))
    assert (repo / ".gitignore").read_text(encoding="utf-8") == original_ignore
    assert (repo / ".xflow" / "xflow.json").read_text(encoding="utf-8") == original_config


def test_secret_and_local_path_detection(root: Path) -> None:
    repo = root / "patterns"
    issue = repo / ".xflow" / "issues" / "issue-1"
    secret_cases = {
        "json.json": '"token": "hidden"\n',
        "openai.json": '"openai_api_key": "hidden"\n',
        "aws.json": '"aws_access_key_id": "hidden"\n',
        "yaml.yaml": "client_secret: hidden\n",
        "prefixed.yaml": "azure_openai_api_key: hidden\n",
        "env.env": "API_KEY='hidden'\n",
        "prefixed.env": "AWS_ACCESS_KEY_ID='hidden'\n",
    }
    prose_cases = {
        "prose.txt": "Rotate the openai api key before release.\n",
        "prose-aws.txt": "The AWS access key id is documented by the provider.\n",
    }
    path_cases = {
        "posix.txt": "/opt/data/evidence.txt\n",
        "root.txt": "local root: /\n",
        "uri.txt": "file:///workspace/evidence.txt\n",
        "unc.txt": "\\\\server\\share\\evidence.txt\n",
        "device.txt": "\\\\.\\PhysicalDrive0\n",
    }
    for name, content in {**secret_cases, **prose_cases, **path_cases}.items():
        write(issue / name, content)
    report = inspect_issue_workspace_migration(repo, "tracked")
    assert {path.name for path in report.credential_files} == set(secret_cases)
    assert {path.name for path in report.absolute_path_files} == set(path_cases)
    assert_value_error("unsafe issue workspace", lambda: apply_issue_workspace_migration(repo, "tracked"))


def test_scan_failures_and_growth_block_apply(root: Path) -> None:
    unreadable_repo = root / "unreadable"
    unreadable = unreadable_repo / ".xflow" / "issues" / "issue-1" / "evidence.txt"
    write(unreadable, "safe evidence\n")
    real_open = migration.os.open

    def deny_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path).resolve(strict=False) == unreadable.resolve(strict=False):
            raise PermissionError("denied for test")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(migration.os, "open", side_effect=deny_open):
        report = inspect_issue_workspace_migration(unreadable_repo, "tracked")
        assert any("cannot read" in error for error in report.scan_errors), report.scan_errors
        assert_value_error("scan errors", lambda: apply_issue_workspace_migration(unreadable_repo, "tracked"))

    growing_repo = root / "growing"
    growing = growing_repo / ".xflow" / "issues" / "issue-1" / "evidence.txt"
    write(growing, "safe evidence\n")
    target_identity = growing.stat().st_ino
    real_read = migration.os.read
    changed = False

    def grow_during_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = real_read(descriptor, count)
        if not changed and os.fstat(descriptor).st_ino == target_identity:
            changed = True
            with growing.open("ab") as handle:
                handle.write(b"grew")
        return data

    with patch.object(migration.os, "read", side_effect=grow_during_read):
        report = inspect_issue_workspace_migration(growing_repo, "tracked")
    assert any("changed while reading" in error for error in report.scan_errors), report.scan_errors


def test_directory_timestamp_jitter_does_not_fake_issue_change(root: Path) -> None:
    repo = root / "directory-jitter"
    issue_directory = repo / ".xflow" / "issues" / "issue-1"
    write(issue_directory / "evidence.txt", "safe evidence\n")
    real_lstat = migration.os.lstat
    real_scandir = migration.os.scandir
    jitter = 0

    class JitteredStat:
        def __init__(self, value: os.stat_result) -> None:
            nonlocal jitter
            jitter += 1
            self._value = value
            self.st_mtime_ns = value.st_mtime_ns + jitter

        def __getattr__(self, name: str) -> object:
            return getattr(self._value, name)

    class JitteredEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            value = self._entry.stat(follow_symlinks=follow_symlinks)
            if Path(self.path) == issue_directory:
                return JitteredStat(value)  # type: ignore[return-value]
            return value

    class JitteredScandir:
        def __init__(self, path: object) -> None:
            self._context = real_scandir(path)  # type: ignore[arg-type]

        def __enter__(self) -> object:
            iterator = self._context.__enter__()
            return iter(JitteredEntry(entry) for entry in iterator)

        def __exit__(self, *args: object) -> object:
            return self._context.__exit__(*args)

    def jittered_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        value = real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path) == issue_directory:
            return JitteredStat(value)  # type: ignore[return-value]
        return value

    with patch.object(migration.os, "lstat", side_effect=jittered_lstat), patch.object(
        migration.os, "scandir", side_effect=JitteredScandir
    ):
        apply_issue_workspace_migration(repo, "local")
    saved = json.loads((repo / ".xflow" / "xflow.json").read_text(encoding="utf-8"))
    assert saved["issueWorkspace"] == {"mode": "local"}


def test_apply_revalidates_config_and_issue_snapshots(root: Path) -> None:
    config_repo = root / "config-race"
    config_path = config_repo / ".xflow" / "xflow.json"
    write_json(config_path, {"contracts": {"root": "docs/original"}})
    original_inspect = migration.inspect_issue_workspace_migration
    mutated = False

    def mutate_config(repo_root: Path, mode: str) -> object:
        nonlocal mutated
        report = original_inspect(repo_root, mode)  # type: ignore[arg-type]
        if not mutated:
            mutated = True
            write_json(config_path, {"contracts": {"root": "docs/concurrent"}})
        return report

    with patch.object(migration, "inspect_issue_workspace_migration", side_effect=mutate_config):
        assert_value_error("changed during migration", lambda: apply_issue_workspace_migration(config_repo, "local"))
    assert json.loads(config_path.read_text(encoding="utf-8"))["contracts"]["root"] == "docs/concurrent"

    issue_repo = root / "issue-race"
    issue_path = issue_repo / ".xflow" / "issues" / "issue-1" / "evidence.txt"
    write(issue_path, "safe evidence\n")
    mutated = False

    def mutate_issue(repo_root: Path, mode: str) -> object:
        nonlocal mutated
        report = original_inspect(repo_root, mode)  # type: ignore[arg-type]
        if not mutated:
            mutated = True
            write(issue_path, "token=concurrent-secret\n")
        return report

    with patch.object(migration, "inspect_issue_workspace_migration", side_effect=mutate_issue):
        assert_value_error("changed during migration", lambda: apply_issue_workspace_migration(issue_repo, "local"))
    assert not (issue_repo / ".xflow" / "xflow.json").exists()


def test_final_commit_window_changes_are_preserved(root: Path) -> None:
    target_repo = root / "target-window"
    config_path = target_repo / ".xflow" / "xflow.json"
    write_json(config_path, {"projectOwned": "original"})
    real_snapshot = migration._current_target_snapshot
    target_mutated = False

    def mutate_after_target_snapshot(
        repo_root: Path, expected: migration.FileSnapshot, label: str
    ) -> migration.FileSnapshot:
        nonlocal target_mutated
        current = real_snapshot(repo_root, expected, label)
        if not target_mutated and label == "migration target" and expected.path == canonical_path(config_path):
            target_mutated = True
            write_json(config_path, {"projectOwned": "concurrent"})
        return current

    with patch.object(migration, "_current_target_snapshot", side_effect=mutate_after_target_snapshot):
        assert_value_error("changed during migration", lambda: apply_issue_workspace_migration(target_repo, "local"))
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"projectOwned": "concurrent"}

    ignore_repo = root / "ignore-target-window"
    ignore_repo.mkdir(parents=True)
    git(ignore_repo, "init", "-q")
    ignore_path = ignore_repo / ".gitignore"
    concurrent_ignore = ".xflow/issues/\n# concurrent project edit\n"
    write(ignore_path, ".xflow/issues/\n")
    ignore_mutated = False

    def mutate_ignore_after_target_snapshot(
        repo_root: Path, expected: migration.FileSnapshot, label: str
    ) -> migration.FileSnapshot:
        nonlocal ignore_mutated
        current = real_snapshot(repo_root, expected, label)
        if not ignore_mutated and label == "migration target" and expected.path == canonical_path(ignore_path):
            ignore_mutated = True
            write(ignore_path, concurrent_ignore)
        return current

    with patch.object(migration, "_current_target_snapshot", side_effect=mutate_ignore_after_target_snapshot):
        assert_value_error("changed during migration", lambda: apply_issue_workspace_migration(ignore_repo, "tracked"))
    assert ignore_mutated
    assert ignore_path.read_text(encoding="utf-8") == concurrent_ignore
    assert not (ignore_repo / ".xflow" / "xflow.json").exists()

    issue_repo = root / "issue-window"
    issue_path = issue_repo / ".xflow" / "issues" / "issue-1" / "evidence.txt"
    write(issue_path, "safe evidence\n")
    real_update = migration._update_journal_state
    begin_edit = threading.Event()
    edit_complete = threading.Event()
    cancel_edit = threading.Event()
    boundary_reached = False
    editor_failures: list[BaseException] = []

    def edit_issue_at_commit_boundary() -> None:
        begin_edit.wait()
        if cancel_edit.is_set():
            edit_complete.set()
            return
        try:
            write(issue_path, "openai_api_key=concurrent-secret\n")
        except BaseException as exc:
            editor_failures.append(exc)
        finally:
            edit_complete.set()

    editor = threading.Thread(target=edit_issue_at_commit_boundary)
    editor.start()

    def mutate_after_journal_update(*args: object, **kwargs: object) -> None:
        nonlocal boundary_reached
        real_update(*args, **kwargs)  # type: ignore[arg-type]
        payload = args[2]
        index = args[3]
        state_value = args[4]
        entries = payload["entries"]  # type: ignore[index]
        if not boundary_reached and state_value == "committing" and entries[index]["target"] == ".xflow/xflow.json":
            boundary_reached = True
            begin_edit.set()
            if not edit_complete.wait(timeout=10):
                raise AssertionError("concurrent Issue edit did not complete at the commit boundary")

    try:
        with patch.object(migration, "_update_journal_state", side_effect=mutate_after_journal_update):
            assert_value_error("changed during migration", lambda: apply_issue_workspace_migration(issue_repo, "local"))
    finally:
        if not boundary_reached:
            cancel_edit.set()
            begin_edit.set()
        editor.join(timeout=10)
    assert not editor.is_alive()
    assert boundary_reached, "apply aborted before the synchronized Issue commit boundary"
    assert editor_failures == [], editor_failures
    assert not (issue_repo / ".xflow" / "xflow.json").exists()
    assert issue_path.read_text(encoding="utf-8") == "openai_api_key=concurrent-secret\n"

    success_repo = root / "success-window"
    success_config = success_repo / ".xflow" / "xflow.json"
    write_json(success_config, {"projectOwned": "original"})
    real_safe_replace = migration._safe_replace
    success_mutated = False

    def mutate_after_replacement(
        repo_root: Path, source: Path, target: Path, label: str, *args: object, **kwargs: object
    ) -> object:
        nonlocal success_mutated
        result = real_safe_replace(repo_root, source, target, label, *args, **kwargs)
        if not success_mutated and label == "migration commit" and target == canonical_path(success_config):
            success_mutated = True
            write_json(success_config, {"projectOwned": "edited-before-success"})
        return result

    with patch.object(migration, "_safe_replace", side_effect=mutate_after_replacement):
        assert_value_error("manual recovery", lambda: apply_issue_workspace_migration(success_repo, "local"))
    assert json.loads(success_config.read_text(encoding="utf-8")) == {"projectOwned": "edited-before-success"}


def test_repository_lock_blocks_parallel_apply(root: Path) -> None:
    repo = root / "repository-lock"
    write_json(repo / ".xflow" / "xflow.json", {"projectOwned": True})
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with migration._repository_migration_lock(repo.resolve(strict=False)):
                entered.set()
                release.wait(timeout=10)
        except BaseException as exc:
            failures.append(exc)
            entered.set()

    worker = threading.Thread(target=hold_lock)
    worker.start()
    try:
        assert entered.wait(timeout=10), "repository lock holder did not start"
        assert failures == [], failures
        assert_value_error("repository lock", lambda: apply_issue_workspace_migration(repo, "local"))
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert failures == [], failures
    assert json.loads((repo / ".xflow" / "xflow.json").read_text(encoding="utf-8")) == {"projectOwned": True}


def test_second_commit_failure_rolls_back_both_targets(root: Path) -> None:
    repo = root / "rollback"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    config_path = repo / ".xflow" / "xflow.json"
    ignore_path = repo / ".gitignore"
    original_config = '{"projectOwned": true}\n'
    original_ignore = ".xflow/issues/\n"
    write(config_path, original_config)
    write(ignore_path, original_ignore)
    real_safe_replace = migration._safe_replace
    failed = False

    def fail_second_replace(
        repo_root: Path, source: Path, target: Path, label: str, *args: object, **kwargs: object
    ) -> object:
        nonlocal failed
        if not failed and target == canonical_path(config_path) and label == "migration commit":
            failed = True
            raise OSError("second replacement failed for test")
        return real_safe_replace(repo_root, source, target, label, *args, **kwargs)

    with patch.object(migration, "_safe_replace", side_effect=fail_second_replace):
        assert_value_error("rolled back", lambda: apply_issue_workspace_migration(repo, "tracked"))
    assert config_path.read_text(encoding="utf-8") == original_config
    assert ignore_path.read_text(encoding="utf-8") == original_ignore
    assert failed


def test_interrupted_transaction_recovers_without_residue(root: Path) -> None:
    repo = root / "interrupted"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    config_path = repo / ".xflow" / "xflow.json"
    write(config_path, '{"projectOwned": true}\n')
    write(repo / ".gitignore", ".xflow/issues/\n")
    real_safe_replace = migration._safe_replace
    interrupted = False

    def interrupt_config_commit(
        repo_root: Path, source: Path, target: Path, label: str, *args: object, **kwargs: object
    ) -> object:
        nonlocal interrupted
        if not interrupted and label == "migration commit" and target == canonical_path(config_path):
            interrupted = True
            raise KeyboardInterrupt("simulated process interruption")
        return real_safe_replace(repo_root, source, target, label, *args, **kwargs)

    with patch.object(migration, "_safe_replace", side_effect=interrupt_config_commit):
        try:
            apply_issue_workspace_migration(repo, "tracked")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected simulated process interruption")
    assert interrupted
    assert (repo / ".xflow" / "local" / "issue-workspace-migration-journal.json").is_file()

    apply_issue_workspace_migration(repo, "tracked")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["projectOwned"] is True
    assert saved["issueWorkspace"] == {"mode": "tracked"}
    assert (repo / ".gitignore").read_text(encoding="utf-8") == ""
    residue = [
        path
        for path in repo.rglob("*")
        if any(marker in path.name for marker in (".migration.", ".backup.", "issue-workspace-migration-journal"))
    ]
    assert residue == [], residue


def _leave_interrupted_transaction(repo: Path) -> Path:
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    config_path = repo / ".xflow" / "xflow.json"
    write(config_path, '{"projectOwned": true}\n')
    write(repo / ".gitignore", ".xflow/issues/\n")
    real_safe_replace = migration._safe_replace
    interrupted = False

    def interrupt_config_commit(
        repo_root: Path, source: Path, target: Path, label: str, *args: object, **kwargs: object
    ) -> object:
        nonlocal interrupted
        if not interrupted and label == "migration commit" and target == canonical_path(config_path):
            interrupted = True
            raise KeyboardInterrupt("simulated process interruption")
        return real_safe_replace(repo_root, source, target, label, *args, **kwargs)

    with patch.object(migration, "_safe_replace", side_effect=interrupt_config_commit):
        try:
            apply_issue_workspace_migration(repo, "tracked")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected simulated process interruption")
    assert interrupted
    journal = repo / ".xflow" / "local" / "issue-workspace-migration-journal.json"
    assert journal.is_file()
    return journal


def _journal_artifact(repo: Path, value: object) -> Path:
    if isinstance(value, dict):
        value = value["path"]
    assert isinstance(value, str)
    path = Path(value)
    return path if path.is_absolute() else repo / path


def test_crafted_journal_artifact_is_rejected(root: Path) -> None:
    repo = root / "crafted-journal"
    journal = _leave_interrupted_transaction(repo)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    config_entry = next(entry for entry in payload["entries"] if entry["target"] == ".xflow/xflow.json")
    backup = _journal_artifact(repo, config_entry["backup"])
    write(backup, '{"crafted": true}\n')

    assert_value_error("manual recovery", lambda: apply_issue_workspace_migration(repo, "tracked"))
    assert json.loads((repo / ".xflow" / "xflow.json").read_text(encoding="utf-8")) == {"projectOwned": True}
    assert journal.is_file()

    tampered_repo = root / "tampered-journal"
    tampered_journal = _leave_interrupted_transaction(tampered_repo)
    tampered_payload = json.loads(tampered_journal.read_text(encoding="utf-8"))
    tampered_payload["entries"][0]["state"] = "pending"
    write(tampered_journal, json.dumps(tampered_payload, ensure_ascii=True, indent=2) + "\n")

    assert_value_error("manual recovery", lambda: apply_issue_workspace_migration(tampered_repo, "tracked"))
    assert json.loads((tampered_repo / ".xflow" / "xflow.json").read_text(encoding="utf-8")) == {
        "projectOwned": True
    }
    assert tampered_journal.is_file()


def test_post_crash_project_edit_is_preserved(root: Path) -> None:
    repo = root / "post-crash-edit"
    journal = _leave_interrupted_transaction(repo)
    config_path = repo / ".xflow" / "xflow.json"
    concurrent = {"projectOwned": "edited-after-crash"}
    write_json(config_path, concurrent)

    assert_value_error("manual recovery", lambda: apply_issue_workspace_migration(repo, "tracked"))
    assert json.loads(config_path.read_text(encoding="utf-8")) == concurrent
    assert journal.is_file()


def test_cleanup_interruption_leaves_no_live_journal(root: Path) -> None:
    repo = root / "cleanup-interruption"
    write_json(repo / ".xflow" / "xflow.json", {"projectOwned": True})
    live_journal = repo / ".xflow" / "local" / "issue-workspace-migration-journal.json"
    cleanup_reached = False

    def interrupt_cleanup(*args: object, **kwargs: object) -> None:
        nonlocal cleanup_reached
        cleanup_reached = True
        assert not live_journal.exists(), "live journal must be finalized before rollback material cleanup"
        raise KeyboardInterrupt("simulated cleanup interruption")

    with patch.object(migration, "_cleanup_transaction", side_effect=interrupt_cleanup):
        try:
            apply_issue_workspace_migration(repo, "local")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected simulated cleanup interruption")
    assert cleanup_reached
    assert not live_journal.exists()
    apply_issue_workspace_migration(repo, "local")
    saved = json.loads((repo / ".xflow" / "xflow.json").read_text(encoding="utf-8"))
    assert saved["projectOwned"] is True
    assert saved["issueWorkspace"] == {"mode": "local"}


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        test_default_config(root / "default")
        test_namespaced_config_validation(root / "config")
        test_check_reports_exact_ignore_source(root / "check")
        test_apply_preserves_config_and_removes_only_exact_ignore_lines(root / "apply")
        test_apply_retains_configured_contract_root(root / "configured-contract-root")
        test_apply_blocks_active_approvals_and_unsafe_issue_content(root / "unsafe")
        test_reparse_points_are_rejected(root / "reparse")
        test_safe_repo_path_accepts_equivalent_root_spelling(root / "safe-path-alias")
        test_safe_repo_path_rejects_explicit_symlink(root / "safe-path-symlink")
        test_contract_root_must_resolve_inside_repository(root / "contract-containment")
        test_effective_ignore_sources_block_manual_actions(root / "ignore-sources")
        test_tracked_apply_verifies_effective_ignore_and_rolls_back(root / "ignore-rollback")
        test_secret_and_local_path_detection(root / "patterns")
        test_scan_failures_and_growth_block_apply(root / "scan-failures")
        test_directory_timestamp_jitter_does_not_fake_issue_change(root / "directory-jitter")
        test_apply_revalidates_config_and_issue_snapshots(root / "toctou")
        test_final_commit_window_changes_are_preserved(root / "commit-window")
        test_repository_lock_blocks_parallel_apply(root / "repository-lock")
        test_second_commit_failure_rolls_back_both_targets(root / "transaction")
        test_interrupted_transaction_recovers_without_residue(root / "recovery")
        test_crafted_journal_artifact_is_rejected(root / "crafted-journal")
        test_post_crash_project_edit_is_preserved(root / "post-crash-edit")
        test_cleanup_interruption_leaves_no_live_journal(root / "cleanup-interruption")
    print("project config ok")


if __name__ == "__main__":
    main()
