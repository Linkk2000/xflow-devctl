from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def approval(repo_root: Path, issue: str, action: str, approved_file: Path) -> None:
    digest = hashlib.sha256(approved_file.read_bytes()).hexdigest()
    relative = approved_file.relative_to(repo_root).as_posix()
    write(
        repo_root / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "local-review.md",
        f"""# Local Review Approval

Issue: {issue}
Reviewer: user
Approved At: 2026-06-16T00:00:00+08:00
Approved Action: {action}
Approved File: {relative}
Approved SHA256: {digest}

## Decision
Approved: yes
""",
    )


def run_devctl(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["DEVCTL_SKIP_PROVIDER_LOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(OPS_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected success: {' '.join(args)}")
    return result


def assert_no_legacy_run_command() -> None:
    entrypoint = (OPS_ROOT / "devctl").read_text(encoding="utf-8")
    assert "\n    run)" not in entrypoint
    assert 'run_script "$OPS/run.sh"' not in entrypoint
    assert 'run_script "$OPS/git/start.sh"' not in entrypoint
    assert 'run_script "$OPS/git/commit-msg.sh"' not in entrypoint
    assert 'run_script "$OPS/git/done.sh"' not in entrypoint
    assert 'run_script "$OPS/git/status.sh"' not in entrypoint
    assert 'run_script "$OPS/app/start-frontend.sh"' not in entrypoint
    assert 'run_script "$OPS/app/stop-frontend.sh"' not in entrypoint
    assert 'run_script "$OPS/app/status.sh"' not in entrypoint
    assert not (OPS_ROOT / "run.sh").exists()


def assert_powershell_help_alias() -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OPS_ROOT / "devctl.ps1"),
            "help",
        ],
        cwd=OPS_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError("devctl.ps1 help must succeed")
    assert "AI call recipes" in result.stdout


def main() -> None:
    assert_no_legacy_run_command()
    assert_powershell_help_alias()

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo = root / "work"
        repo.mkdir()
        origin = root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        git(repo, "checkout", "-b", "main", "-q")
        write(repo / "README.md", "# Demo\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "init", "-q")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-u", "origin", "main", "-q")
        git(repo, "checkout", "-b", "feature/1-python-entrypoint", "-q")
        git(repo, "config", "--local", "devctl.issue", "1")
        git(repo, "config", "--local", "devctl.base", "main")

        issue_file = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
        write(
            issue_file,
            """<!-- xflow: issue-draft -->

## Background
Need Python entrypoint routing.

## Problem
Shell entrypoint may use old curl/jq scripts.

## Goal
Route remote-write commands through Python core.

## Scope
- Includes: issue and git-mr commands.

## Acceptance Criteria
- [ ] Bash entrypoint reaches Python skip-provider messages.

## Verification Plan
- python tests/entrypoint-routing.py
""",
        )
        approval(repo, "draft", "issue-create", issue_file)

        current_task = repo / ".xflow" / "current-task.md"
        write(
            current_task,
            """# XFlow Current Task

Issue: 1
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Verify entrypoint routing.

## Forbidden Actions
- Create PR before push approval.
""",
        )
        walkthrough = repo / ".xflow" / "issues" / "issue-1" / "walkthrough.md"
        write(walkthrough, "# Walkthrough\n\n- python tests/entrypoint-routing.py\n")

        mr_file = repo / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
        write(
            mr_file,
            """<!-- xflow: mr-draft -->

Closes #1

## Summary
- Route bash entrypoint to Python core.

## Test Plan
- python tests/entrypoint-routing.py

## Risk
- Low.

## Review Request
- Please review routing behavior.
""",
        )
        git(repo, "add", ".")
        git(repo, "commit", "-m", "test: add routing task artifacts", "-q")

        issue_result = run_devctl(repo, "issue", "create", "Python routing", "--body-file", str(issue_file))
        assert "issue-create gate passed; provider skipped" in issue_result.stdout

        approval(repo, "1", "git-push", walkthrough)
        push_result = run_devctl(repo, "git", "push", "--issue", "1", "--file", str(walkthrough))
        assert "pushed feature/1-python-entrypoint" in push_result.stdout

        approval(repo, "1", "git-mr", mr_file)
        mr_result = run_devctl(repo, "git", "mr", "--body-file", str(mr_file), "--issue", "1", "--base", "main")
        assert "git-mr gate passed; provider skipped" in mr_result.stdout

        check_result = run_devctl(repo, "check", "mr-draft", "--issue", "1")
        assert "mr-draft check passed" in check_result.stdout

        pasted = repo / "route-image.png"
        pasted.write_bytes(b"\x89PNG\r\n\x1a\nroute-test")
        attachment_result = run_devctl(repo, "attachment", "add", "--issue", "draft", "--file", str(pasted), "--as", "image")
        assert "xflow-attachment://att-001" in attachment_result.stdout

    print("entrypoint routing ok")


if __name__ == "__main__":
    main()
