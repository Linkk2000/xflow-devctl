from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import migration
from xflow.migration import apply_issue_workspace_migration, inspect_issue_workspace_migration
from xflow.project_config import load_project_config


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


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
    assert report.active_approvals == (approval,)
    assert "active approval" in report.blockers[0]
    assert_value_error("active approvals", lambda: apply_issue_workspace_migration(repo_root, "tracked"))
    assert not (repo_root / ".xflow" / "xflow.json").exists()

    approval.unlink()
    unsafe = repo_root / ".xflow" / "issues" / "issue-IK152D" / "walkthrough.md"
    write(unsafe, "token=secret\nC:\\Users\\person\\workspace\n")
    oversized = repo_root / ".xflow" / "issues" / "issue-IK152D" / "evidence.bin"
    oversized.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    report = inspect_issue_workspace_migration(repo_root, "tracked")
    assert report.credential_files == (unsafe,)
    assert report.absolute_path_files == (unsafe,)
    assert report.oversized_files == (oversized,)
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
        "yaml.yaml": "client_secret: hidden\n",
        "env.env": "API_KEY='hidden'\n",
    }
    path_cases = {
        "posix.txt": "/opt/data/evidence.txt\n",
        "root.txt": "local root: /\n",
        "uri.txt": "file:///workspace/evidence.txt\n",
        "unc.txt": "\\\\server\\share\\evidence.txt\n",
        "device.txt": "\\\\.\\PhysicalDrive0\n",
    }
    for name, content in {**secret_cases, **path_cases}.items():
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
        if Path(path) == unreadable:
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
    real_replace = migration.os.replace
    failed = False

    def fail_second_replace(source: object, target: object) -> None:
        nonlocal failed
        source_path = Path(source)
        target_path = Path(target)
        if not failed and target_path == config_path and source_path.name.startswith(".xflow-config.migration."):
            failed = True
            raise OSError("second replacement failed for test")
        real_replace(source, target)  # type: ignore[arg-type]

    with patch.object(migration.os, "replace", side_effect=fail_second_replace):
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

    def interrupt_config_commit(repo_root: Path, source: Path, target: Path, label: str) -> None:
        nonlocal interrupted
        if not interrupted and label == "migration commit" and target == config_path:
            interrupted = True
            raise KeyboardInterrupt("simulated process interruption")
        real_safe_replace(repo_root, source, target, label)

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
        test_contract_root_must_resolve_inside_repository(root / "contract-containment")
        test_effective_ignore_sources_block_manual_actions(root / "ignore-sources")
        test_tracked_apply_verifies_effective_ignore_and_rolls_back(root / "ignore-rollback")
        test_secret_and_local_path_detection(root / "patterns")
        test_scan_failures_and_growth_block_apply(root / "scan-failures")
        test_apply_revalidates_config_and_issue_snapshots(root / "toctou")
        test_second_commit_failure_rolls_back_both_targets(root / "transaction")
        test_interrupted_transaction_recovers_without_residue(root / "recovery")
    print("project config ok")


if __name__ == "__main__":
    main()
