from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.project_config import load_project_config
from xflow.migration import apply_issue_workspace_migration, inspect_issue_workspace_migration


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: dict[str, object]) -> None:
    write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


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
        {"issueWorkspace": {"mode": "invalid"}},
        {"issueWorkspace": {"mode": ["tracked"]}},
        {"contracts": {"root": "C:/absolute"}},
        {"contracts": {"root": "../escape"}},
        {"contracts": {"root": ["docs/requirements"]}},
    ):
        write_json(config_path, namespace)
        assert_value_error(".xflow/xflow.json", lambda: load_project_config(repo_root))


def test_check_reports_exact_ignore_source(repo_root: Path) -> None:
    write(repo_root / ".gitignore", ".xflow/issues/\n.xflow/issues/*\n")
    report = inspect_issue_workspace_migration(repo_root, "tracked")
    assert report.exact_ignore_lines == (".xflow/issues/",)
    assert report.git_ignore_source is not None
    check = run_devctl(repo_root, "migrate", "issue-workspace", "--mode", "tracked", "--check")
    assert "exact issue workspace ignore line: .xflow/issues/" in check.stdout
    assert "git ignore source:" in check.stdout
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == ".xflow/issues/\n.xflow/issues/*\n"
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
    write(repo_root / ".gitignore", ".xflow/issues\n.xflow/issues/\n.xflow/issues/*\n*.tmp\n")

    report = apply_issue_workspace_migration(repo_root, "tracked")
    assert report.blockers == ()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    for name, value in original.items():
        assert saved[name] == value
    assert saved["issueWorkspace"] == {"mode": "tracked"}
    assert saved["contracts"] == {"root": "docs/requirements"}
    assert list(saved)[:6] == list(original)
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == ".xflow/issues/*\n*.tmp\n"

    apply_issue_workspace_migration(repo_root, "local")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["issueWorkspace"] == {"mode": "local"}
    assert saved["contracts"] == {"root": "docs/requirements"}
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == ".xflow/issues/*\n*.tmp\n"


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


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        test_default_config(root / "default")
        test_namespaced_config_validation(root / "config")
        test_check_reports_exact_ignore_source(root / "check")
        test_apply_preserves_config_and_removes_only_exact_ignore_lines(root / "apply")
        test_apply_retains_configured_contract_root(root / "configured-contract-root")
        test_apply_blocks_active_approvals_and_unsafe_issue_content(root / "unsafe")
    print("project config ok")


if __name__ == "__main__":
    main()
