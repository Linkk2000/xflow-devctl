from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml
from PIL import Image


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

from xflow.checks import check_resolution_report, write_pr_state_update_suggestion
from xflow import approval as approval_gate
from xflow import cli as cli_module
from xflow import providers as provider_module
from xflow.bindings import resolve_bindings
from xflow.commit_message import check_commit_message
from xflow.dependencies import DependencyCheckResult, check_dependencies
from xflow.env import load_env_files
from xflow.providers import (
    close_issue,
    comment_issue,
    create_issue,
    create_pull_request,
    get_pull_request,
    list_issues,
    show_issue,
)
from xflow.paths import active_task_pointer_file, default_approval_file, default_issue_file, issue_dir, task_authority_file
from xflow.cli import branch_name_from_slug, commit_and_push_pr_backfill, summarize_commit_message
from xflow.task_state import TaskState, activate_task, render_task_state
from xflow.unattended import disable, enable, load, migrate_issue, require_active


def run_devctl(repo_root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["DEVCTL_TOOL_ROOT"] = str(OPS_ROOT)
    env["DEVCTL_OPS_ROOT"] = str(OPS_ROOT)
    env["PYTHONPATH"] = str(OPS_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("DEVCTL_SKIP_PROVIDER_LOAD", "1")
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
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected exit {expect}, got {result.returncode}: {' '.join(args)}")
    return result


def run_devctl_with_env(repo_root: Path, extra_env: dict[str, str], *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env)
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["DEVCTL_TOOL_ROOT"] = str(OPS_ROOT)
    env["DEVCTL_OPS_ROOT"] = str(OPS_ROOT)
    env["PYTHONPATH"] = str(OPS_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("DEVCTL_SKIP_PROVIDER_LOAD", "1")
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
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected exit {expect}, got {result.returncode}: {' '.join(args)}")
    return result


def write(path: Path, text: str) -> None:
    write_text_lf(path, text)


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_text(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def init_test_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    write(repo / "README.md", "# Demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init", "-q")


def assert_value_error(expected: str, action: object) -> None:
    try:
        action()
    except ValueError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def test_unattended_state_lifecycle(parent: Path) -> None:
    repo = parent / "repo"
    other_repo = parent / "other-repo"
    sibling = parent / "sibling"
    init_test_repo(repo)
    init_test_repo(other_repo)

    assert load(repo) is None
    assert disable(repo) is False

    invalid_confirmations = (
        "xflow_human_unattended_all",
        " XFLOW_HUMAN_UNATTENDED_ALL",
        "XFLOW_HUMAN_UNATTENDED_ALL ",
        "XFLOW_HUMAN_UNATTENDED_AL",
        "XFLOW_HUMAN_UNATTENDED_ALL!",
    )
    for confirmation in invalid_confirmations:
        assert_value_error("confirmation", lambda value=confirmation: enable(repo, "IK152D", value))
        assert load(repo) is None

    state = enable(repo, "#IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    state_path = repo / ".xflow" / "local" / "unattended.json"
    raw = state_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert state.issue == "IK152D"
    assert require_active(repo, "IK152D") == state
    assert payload["version"] == 1
    assert payload["mode"] == "task-unattended"
    assert set(payload) == {"version", "mode", "repository", "worktree", "issue", "enabledAt"}
    assert "XFLOW_HUMAN_UNATTENDED_ALL" not in raw
    assert "confirm" not in raw.lower()

    original = state_path.read_bytes()
    assert_value_error("Issue mismatch", lambda: require_active(repo, "IK152E"))
    assert state_path.read_bytes() == original

    copied = other_repo / ".xflow" / "local" / "unattended.json"
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(original)
    assert_value_error("repository mismatch", lambda: require_active(other_repo, "IK152D"))
    assert copied.read_bytes() == original

    git(repo, "worktree", "add", "-b", "feature/sibling", str(sibling), "HEAD")
    sibling_state = sibling / ".xflow" / "local" / "unattended.json"
    sibling_state.parent.mkdir(parents=True, exist_ok=True)
    sibling_state.write_bytes(original)
    assert_value_error("worktree mismatch", lambda: require_active(sibling, "IK152D"))
    assert sibling_state.read_bytes() == original

    state_path.write_bytes(b"\xef\xbb\xbf" + original)
    assert require_active(repo, "IK152D").issue == "IK152D"
    assert state_path.read_bytes().startswith(b"\xef\xbb\xbf")

    state_path.write_text("{broken", encoding="utf-8")
    assert_value_error("invalid unattended state", lambda: load(repo))
    assert state_path.read_text(encoding="utf-8") == "{broken"

    state_path.write_text(json.dumps({**payload, "version": "1"}), encoding="utf-8")
    assert_value_error("invalid unattended state", lambda: load(repo))

    enable(repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    migrated = migrate_issue(repo, "draft", "42")
    assert migrated.issue == "42"
    assert require_active(repo, "42").issue == "42"
    assert_value_error("Issue mismatch", lambda: require_active(repo, "draft"))
    assert not list(state_path.parent.glob("unattended.json.*.tmp"))

    assert disable(repo) is True
    assert disable(repo) is False
    assert load(repo) is None


def test_unattended_cli_lifecycle(repo: Path) -> None:
    init_test_repo(repo)
    inactive = run_devctl(repo, "unattended", "status")
    assert "inactive" in inactive.stdout.lower()

    rejected = run_devctl(
        repo,
        "unattended",
        "enable",
        "--issue",
        "IK152D",
        "--confirm",
        "xflow_human_unattended_all",
        expect=1,
    )
    assert "confirmation" in rejected.stderr.lower()

    enabled = run_devctl(
        repo,
        "unattended",
        "enable",
        "--issue",
        "IK152D",
        "--confirm",
        "XFLOW_HUMAN_UNATTENDED_ALL",
    )
    assert "enabled" in enabled.stdout.lower()
    active = run_devctl(repo, "unattended", "status")
    assert "active" in active.stdout.lower()
    assert "IK152D" in active.stdout

    state_path = repo / ".xflow" / "local" / "unattended.json"
    state_bytes = state_path.read_bytes()
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "IK152D")
    write(repo / ".xflow" / "current-task.md", current_task_text("OTHER"))
    mismatched = run_devctl(repo, "unattended", "status")
    assert "[WARN]" in mismatched.stdout
    assert "invalid" in mismatched.stdout.lower()
    assert "current task Issue mismatch" in mismatched.stdout
    assert not state_path.exists()

    state_path.write_text("not-json", encoding="utf-8")
    invalid = run_devctl(repo, "unattended", "status")
    assert "[WARN]" in invalid.stdout
    assert "invalid" in invalid.stdout.lower()
    assert state_path.read_text(encoding="utf-8") == "not-json"

    disabled = run_devctl(repo, "unattended", "disable")
    assert "disabled" in disabled.stdout.lower()
    run_devctl(repo, "unattended", "disable")


def test_completed_task_invalidates_unattended_state(repo: Path) -> None:
    init_test_repo(repo)
    enable(repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    write(
        repo / ".xflow" / "current-task.md",
        current_task_text("IK152D").replace("G5_APPROVE_MR_CREATE", "S10_DONE"),
    )
    assert_value_error("current task is completed", lambda: require_active(repo, "IK152D"))
    assert load(repo) is None

    enable(repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    write(
        repo / ".xflow" / "current-task.md",
        current_task_text("IK152D").replace("Issue: IK152D\n", ""),
    )
    assert_value_error("current task Issue is missing", lambda: require_active(repo, "IK152D"))
    assert load(repo) is None

    enable(repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    write(
        repo / ".xflow" / "current-task.md",
        current_task_text("IK152D").replace("Issue: IK152D", "Issue: ../invalid"),
    )
    assert_value_error("current task Issue is invalid", lambda: require_active(repo, "IK152D"))
    assert load(repo) is None


def current_task_text(issue: str) -> str:
    return f"""# XFlow Current Task

Issue: {issue}
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Run the current task remote workflow.

## Forbidden Actions
- Run a different task remote workflow.
"""


def issue_draft_text(goal: str) -> str:
    return f"""<!-- xflow: issue-draft -->

## Background
Need a task-scoped unattended workflow.

## Problem
Repeated human gates interrupt one approved task.

## Goal
{goal}

## Scope
- Includes: ordinary remote writes for one task.

## Acceptance Criteria
- [ ] Human approval files are bypassed only by matching state.

## Verification Plan
- python tests/python-core.py
"""


def mr_draft_text(issue: str) -> str:
    return f"""<!-- xflow: mr-draft -->

Closes #{issue}

## Summary
- Merge the recorded task pull request.

## Test Plan
- python tests/python-core.py

## Risk
- Low.

## Review Request
- Verify the recorded pull request identity.
"""


def test_no_local_review_requires_active_state(parent: Path) -> None:
    repo = parent / "repo"
    init_test_repo(repo)
    git(repo, "remote", "add", "origin", "git@gitee.com:Linkk2000/paper-demo.git")
    issue_id = "IJZT85"
    write(repo / ".xflow" / "current-task.md", current_task_text(issue_id))
    comment = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "comment.md"
    write(comment, "<!-- xflow: issue-comment -->\n\nTask status update.\n")

    with RecordingApiServer() as server:
        env = {
            "GITEE_API_BASE": server.base_url,
            "GITEE_TOKEN": "gitee-token",
            "XFLOW_PLATFORM": "gitee",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        rejected = run_devctl_with_env(
            repo,
            env,
            "issue",
            "comment",
            issue_id,
            "--body-file",
            str(comment),
            "--no-local-review",
            expect=1,
        )
        assert "--no-local-review requires active task-scoped unattended mode" in rejected.stderr
        assert not server.requests

        enable(repo, issue_id, "XFLOW_HUMAN_UNATTENDED_ALL")
        automatic = run_devctl_with_env(
            repo,
            env,
            "issue",
            "comment",
            issue_id,
            "--body-file",
            str(comment),
        )
        assert f"[UNATTENDED] Human approval gate bypassed for current task {issue_id}." in automatic.stdout
        compatible = run_devctl_with_env(
            repo,
            env,
            "issue",
            "comment",
            issue_id,
            "--body-file",
            str(comment),
            "--no-local-review",
        )
        assert f"[UNATTENDED] Human approval gate bypassed for current task {issue_id}." in compatible.stdout
        assert len(server.requests) == 2

        write(repo / ".xflow" / "current-task.md", current_task_text("OTHER"))
        mismatched = run_devctl_with_env(
            repo,
            env,
            "issue",
            "comment",
            issue_id,
            "--body-file",
            str(comment),
            expect=1,
        )
        assert "Issue identity mismatch" in mismatched.stderr
        assert len(server.requests) == 2

    write(repo / ".xflow" / "current-task.md", current_task_text(issue_id))
    enable(repo, issue_id, "XFLOW_HUMAN_UNATTENDED_ALL")
    image = repo / "evidence.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nunattended-attachment")
    run_devctl(repo, "attachment", "add", "--issue", issue_id, "--file", str(image), "--as", "image")
    manifest = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "attachments" / "manifest.json"
    attachment_result = run_devctl(
        repo,
        "issue",
        "comment",
        issue_id,
        "--body-file",
        str(comment),
        "--attachments",
        str(manifest),
        "--no-local-review",
        expect=1,
    )
    assert "issue/comment image attachments are disabled" in attachment_result.stderr


def test_successful_issue_close_invalidates_unattended_state(parent: Path) -> None:
    repo = parent / "repo"
    init_test_repo(repo)
    issue_id = "IJZT85"
    git(repo, "remote", "add", "origin", "git@gitee.com:Linkk2000/paper-demo.git")
    write(repo / ".xflow" / "current-task.md", current_task_text(issue_id))
    walkthrough = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "walkthrough.md"
    write(walkthrough, "# Walkthrough\n\nVerified completion evidence.\n")
    enable(repo, issue_id, "XFLOW_HUMAN_UNATTENDED_ALL")

    with RecordingApiServer() as server:
        closed = run_devctl_with_env(
            repo,
            {
                "GITEE_API_BASE": server.base_url,
                "GITEE_TOKEN": "gitee-token",
                "XFLOW_PLATFORM": "gitee",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
                "DEVCTL_APPROVED_FILE": str(walkthrough),
            },
            "issue",
            "close",
            issue_id,
        )
        assert f"Issue #{issue_id} closed" in closed.stdout
        assert [item["method"] for item in server.requests] == ["PATCH"]
    history = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "approvals" / "history"
    history_text = "\n".join(path.read_text(encoding="utf-8") for path in history.glob("*.yaml"))
    assert "source: unattended" in history_text
    assert "action: issue-close" in history_text
    assert "reviewerSummary: task-scoped-unattended" in history_text
    assert load(repo) is None


def test_inline_attachment_upload_is_rejected_before_gate_and_provider(parent: Path) -> None:
    no_local_repo = parent / "no-local"
    init_test_repo(no_local_repo)
    git(no_local_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    no_local_body = no_local_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    inline_file = no_local_repo / "notes.txt"
    write(no_local_body, issue_draft_text("Reject inline attachment publication before authorization."))
    write(inline_file, "attachment notes\n")

    with RecordingApiServer() as server:
        env = {
            "GITHUB_API_BASE": server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        rejected = run_devctl_with_env(
            no_local_repo,
            env,
            "issue",
            "create",
            "No local review",
            "--body-file",
            str(no_local_body),
            "--attach-file",
            str(inline_file),
            "--upload-attachments",
            "github",
            "--no-local-review",
            expect=1,
        )
        assert "--no-local-review requires active task-scoped unattended mode" in rejected.stderr
        assert not server.requests
        assert not (no_local_repo / ".xflow" / "issues" / "issue-draft" / "attachments" / "manifest.json").exists()

    review_repo = parent / "review-required"
    init_test_repo(review_repo)
    git(review_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    review_body = review_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    review_file = review_repo / "notes.txt"
    write(review_body, issue_draft_text("Reject implicit provider upload before local review."))
    write(review_file, "attachment notes\n")
    with RecordingApiServer() as server:
        env = {
            "GITHUB_API_BASE": server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        rejected = run_devctl_with_env(
            review_repo,
            env,
            "issue",
            "create",
            "Review required",
            "--body-file",
            str(review_body),
            "--attach-file",
            str(review_file),
            expect=1,
        )
        assert "inline issue attachments are disabled" in rejected.stderr
        assert not server.requests

    published_repo = parent / "prepublished"
    init_test_repo(published_repo)
    git(published_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    source_body = published_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    published_file = published_repo / "notes.txt"
    write(published_file, "published attachment notes\n")
    run_devctl(published_repo, "attachment", "add", "--issue", "draft", "--file", str(published_file), "--as", "file")
    write(
        source_body,
        issue_draft_text("Publish a pre-reviewed attachment manifest.")
        + "\n## Attachments\n- [notes.txt](xflow-attachment://att-001)\n",
    )
    final_body = published_repo / ".xflow" / "publish" / "issues" / "issue-draft" / "issue.final.md"
    run_devctl(
        published_repo,
        "attachment",
        "publish",
        "--issue",
        "draft",
        "--backend",
        "manual",
        "--url",
        "att-001=https://public.example/notes.txt",
        "--body-file",
        str(source_body),
        "--output",
        str(final_body),
    )
    published_manifest = published_repo / ".xflow" / "publish" / "issues" / "issue-draft" / "attachments" / "manifest.json"
    enable(published_repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    with RecordingApiServer() as server:
        env = {
            "GITHUB_API_BASE": server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        created = run_devctl_with_env(
            published_repo,
            env,
            "issue",
            "create",
            "Prepublished attachment",
            "--body-file",
            str(final_body),
            "--attachments",
            str(published_manifest),
            "--no-local-review",
        )
        assert "Issue #42 created" in created.stdout
        assert len([item for item in server.requests if item["method"] == "POST" and item["path"].endswith("/issues")]) == 1

    unpublished_repo = parent / "unpublished"
    init_test_repo(unpublished_repo)
    git(unpublished_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    unpublished_body = unpublished_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    unpublished_file = unpublished_repo / "notes.txt"
    write(unpublished_file, "unpublished attachment notes\n")
    run_devctl(unpublished_repo, "attachment", "add", "--issue", "draft", "--file", str(unpublished_file), "--as", "file")
    write(
        unpublished_body,
        issue_draft_text("Reject an unpublished attachment manifest.")
        + "\n## Attachments\n- [notes.txt](xflow-attachment://att-001)\n",
    )
    unpublished_manifest = unpublished_repo / ".xflow" / "issues" / "issue-draft" / "attachments" / "manifest.json"
    enable(unpublished_repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    with RecordingApiServer() as server:
        env = {
            "GITHUB_API_BASE": server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        rejected = run_devctl_with_env(
            unpublished_repo,
            env,
            "issue",
            "create",
            "Unpublished attachment",
            "--body-file",
            str(unpublished_body),
            "--attachments",
            str(unpublished_manifest),
            "--no-local-review",
            expect=1,
        )
        assert "published" in rejected.stderr.lower() or "placeholder" in rejected.stderr.lower()
        assert not server.requests


def test_remote_gate_matrix(parent: Path) -> None:
    repo = parent / "repo"
    init_test_repo(repo)
    issue_id = "IK152D"
    approved_file = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "walkthrough.md"
    manifest = repo / ".xflow" / "issues" / f"issue-{issue_id}" / "attachments" / "manifest.json"
    write(approved_file, "# Walkthrough\n\nVerified task evidence.\n")
    write(manifest, '{"version":1,"issue":"IK152D","items":[]}\n')
    enable(repo, issue_id, "XFLOW_HUMAN_UNATTENDED_ALL")

    actions = (
        "issue-create",
        "issue-comment",
        "issue-close",
        "git-push",
        "git-mr",
        "git-pr-merge",
    )
    output = io.StringIO()
    with redirect_stdout(output):
        for action in actions:
            selected = approval_gate.require_remote_or_unattended(
                repo,
                action,
                approved_file,
                issue_id,
                manifest if action in {"issue-create", "issue-comment", "git-mr"} else None,
            )
            assert selected.source == "unattended"
    expected_log = f"[UNATTENDED] Human approval gate bypassed for current task {issue_id}."
    assert output.getvalue().count(expected_log) == len(actions)
    assert_value_error(
        "not eligible",
        lambda: approval_gate.require_remote_or_unattended(repo, "git-state-backfill", approved_file, issue_id),
    )

    assert_value_error(
        "not eligible",
        lambda: approval_gate.require_remote_or_unattended(
            repo,
            "git-force-push",
            approved_file,
            issue_id,
        ),
    )
    disable(repo)
    assert_value_error(
        "local review approval required",
        lambda: approval_gate.require_remote_or_unattended(repo, "git-push", approved_file, issue_id),
    )
    assert_value_error(
        "--no-local-review requires active task-scoped unattended mode",
        lambda: approval_gate.require_remote_or_unattended(
            repo,
            "issue-comment",
            approved_file,
            issue_id,
            request_unattended=True,
        ),
    )


def test_pr_merge_requires_recorded_and_remote_identity(parent: Path) -> None:
    repo = parent / "repo"
    init_test_repo(repo)
    git(repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    base = git_text(repo, "branch", "--show-current")
    branch = "feature/IK152D-merge"
    git(repo, "checkout", "-b", branch, "-q")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "IK152D")
    git(repo, "config", "--worktree", "devctl.base", base)
    git(repo, "config", "--worktree", "devctl.pr", "42")
    write(
        repo / ".xflow" / "current-task.md",
        current_task_text("IK152D").replace("G5_APPROVE_MR_CREATE", "S9_REMOTE_REVIEW_AND_CI"),
    )
    mr_file = repo / ".xflow" / "issues" / "issue-IK152D" / "mr-draft.md"
    write(mr_file, mr_draft_text("IK152D"))
    enable(repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    env_base = {
        "GITHUB_TOKEN": "github-token",
        "XFLOW_PLATFORM": "github",
        "DEVCTL_SKIP_PROVIDER_LOAD": "0",
    }

    with RecordingApiServer(
        pull_request_payload=json.dumps(
            {
                "number": 999,
                "state": "open",
                "head": {"ref": branch},
                "base": {"ref": base},
            }
        )
    ) as server:
        wrong = run_devctl_with_env(
            repo,
            {**env_base, "GITHUB_API_BASE": server.base_url},
            "git",
            "pr-merge",
            "999",
            "--issue",
            "IK152D",
            "--file",
            str(mr_file),
            expect=1,
        )
        assert "recorded PR mismatch: expected 42, got 999" in wrong.stderr
        assert "[UNATTENDED]" not in wrong.stdout
        assert not server.requests

    correct_payload = json.dumps(
        {
            "number": 42,
            "state": "open",
            "head": {"ref": branch},
            "base": {"ref": base},
        }
    )
    with RecordingApiServer(pull_request_payload=correct_payload) as server:
        merged = run_devctl_with_env(
            repo,
            {**env_base, "GITHUB_API_BASE": server.base_url},
            "git",
            "pr-merge",
            "42",
            "--issue",
            "IK152D",
            "--file",
            str(mr_file),
        )
        assert "PR #42 merged" in merged.stdout
        assert [item["method"] for item in server.requests] == ["GET", "PUT"]

    invalid_payloads = (
        ({"number": 41, "state": "open", "head": {"ref": branch}, "base": {"ref": base}}, "number mismatch"),
        ({"number": 42, "state": "closed", "head": {"ref": branch}, "base": {"ref": base}}, "state mismatch"),
        ({"number": 42, "state": "open", "head": {"ref": "feature/OTHER"}, "base": {"ref": base}}, "head branch mismatch"),
        ({"number": 42, "state": "open", "head": {"ref": branch}, "base": {"ref": "other-base"}}, "base branch mismatch"),
    )
    for payload, expected in invalid_payloads:
        with RecordingApiServer(pull_request_payload=json.dumps(payload)) as server:
            rejected = run_devctl_with_env(
                repo,
                {**env_base, "GITHUB_API_BASE": server.base_url},
                "git",
                "pr-merge",
                "42",
                "--issue",
                "IK152D",
                "--file",
                str(mr_file),
                expect=1,
            )
            assert expected in rejected.stderr
            assert "[UNATTENDED]" not in rejected.stdout
            assert [item["method"] for item in server.requests] == ["GET"]

    github = provider_module.normalize_pull_request_identity(
        {"number": 42, "state": "open", "head": {"ref": branch}, "base": {"ref": base}}
    )
    assert (github.number, github.state, github.head, github.base) == ("42", "open", branch, base)
    gitee = provider_module.normalize_pull_request_identity(
        {"number": "7", "state": "open", "head": branch, "base": {"ref": base}}
    )
    assert (gitee.number, gitee.state, gitee.head, gitee.base) == ("7", "open", branch, base)


def test_issue_identity_sources_must_all_match(parent: Path) -> None:
    push_repo = parent / "push-conflict"
    init_test_repo(push_repo)
    base = git_text(push_repo, "branch", "--show-current")
    git(push_repo, "checkout", "-b", "feature/IK152D-conflict", "-q")
    git(push_repo, "config", "extensions.worktreeConfig", "true")
    git(push_repo, "config", "--worktree", "devctl.base", base)
    enable(push_repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    git(push_repo, "config", "--worktree", "devctl.issue", "OTHER")
    write(push_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    walkthrough = push_repo / ".xflow" / "issues" / "issue-IK152D" / "walkthrough.md"
    write(walkthrough, "# Walkthrough\n\nVerified push evidence.\n")
    conflicted_push = run_devctl_with_env(
        push_repo,
        {"DEVCTL_SKIP_PUSH": "1"},
        "git",
        "push",
        "--issue",
        "IK152D",
        "--file",
        str(walkthrough),
        expect=1,
    )
    assert "Issue identity mismatch" in conflicted_push.stderr
    assert "branch=OTHER" in conflicted_push.stderr
    assert "[UNATTENDED]" not in conflicted_push.stdout

    enable_repo = parent / "enable-draft-conflict"
    init_test_repo(enable_repo)
    write(enable_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    rejected_enable = run_devctl(
        enable_repo,
        "unattended",
        "enable",
        "--issue",
        "draft",
        "--confirm",
        "XFLOW_HUMAN_UNATTENDED_ALL",
        expect=1,
    )
    assert "Issue identity mismatch" in rejected_enable.stderr
    assert load(enable_repo) is None

    draft_repo = parent / "active-draft-conflict"
    init_test_repo(draft_repo)
    git(draft_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    enable(draft_repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    write(draft_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    invalid_status = run_devctl(draft_repo, "unattended", "status")
    assert "[WARN]" in invalid_status.stdout
    assert "current task Issue mismatch" in invalid_status.stdout
    assert load(draft_repo) is None
    draft_body = draft_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(draft_body, issue_draft_text("Reject draft state inherited by another current task."))
    with RecordingApiServer() as server:
        rejected_create = run_devctl_with_env(
            draft_repo,
            {
                "GITHUB_API_BASE": server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            },
            "issue",
            "create",
            "Draft identity conflict",
            "--body-file",
            str(draft_body),
            "--no-local-review",
            expect=1,
        )
        assert "Issue identity mismatch" in rejected_create.stderr
        assert "[UNATTENDED]" not in rejected_create.stdout
        assert not server.requests


def test_git_mechanical_checks_run_before_unattended_gate(parent: Path) -> None:
    push_repo = parent / "push-on-base"
    init_test_repo(push_repo)
    base = git_text(push_repo, "branch", "--show-current")
    git(push_repo, "config", "extensions.worktreeConfig", "true")
    git(push_repo, "config", "--worktree", "devctl.issue", "IK152D")
    git(push_repo, "config", "--worktree", "devctl.base", base)
    write(push_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    walkthrough = push_repo / ".xflow" / "issues" / "issue-IK152D" / "walkthrough.md"
    write(walkthrough, "# Walkthrough\n\nVerified push evidence.\n")
    enable(push_repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    push_result = run_devctl_with_env(
        push_repo,
        {"DEVCTL_SKIP_PUSH": "1"},
        "git",
        "push",
        "--issue",
        "IK152D",
        "--file",
        str(walkthrough),
        expect=1,
    )
    assert f"current branch is {base}" in push_result.stderr
    assert "[UNATTENDED]" not in push_result.stdout

    no_upstream_repo = parent / "mr-no-upstream"
    init_test_repo(no_upstream_repo)
    no_upstream_base = git_text(no_upstream_repo, "branch", "--show-current")
    no_upstream_branch = "feature/IK152D-no-upstream"
    git(no_upstream_repo, "checkout", "-b", no_upstream_branch, "-q")
    git(no_upstream_repo, "config", "extensions.worktreeConfig", "true")
    git(no_upstream_repo, "config", "--worktree", "devctl.issue", "IK152D")
    git(no_upstream_repo, "config", "--worktree", "devctl.base", no_upstream_base)
    write(no_upstream_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    no_upstream_mr = no_upstream_repo / ".xflow" / "issues" / "issue-IK152D" / "mr-draft.md"
    write(no_upstream_mr, mr_draft_text("IK152D"))
    enable(no_upstream_repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    no_upstream = run_devctl(
        no_upstream_repo,
        "git",
        "mr",
        "--issue",
        "IK152D",
        "--body-file",
        str(no_upstream_mr),
        expect=1,
    )
    assert "no upstream" in no_upstream.stderr
    assert "[UNATTENDED]" not in no_upstream.stdout

    ahead_repo = parent / "mr-ahead"
    init_test_repo(ahead_repo)
    ahead_base = git_text(ahead_repo, "branch", "--show-current")
    ahead_branch = "feature/IK152D-ahead"
    git(ahead_repo, "checkout", "-b", ahead_branch, "-q")
    git(ahead_repo, "branch", "--set-upstream-to", ahead_base, ahead_branch)
    write(ahead_repo / "feature.txt", "unpushed task change\n")
    git(ahead_repo, "add", "feature.txt")
    git(ahead_repo, "commit", "-m", "task change", "-q")
    git(ahead_repo, "config", "extensions.worktreeConfig", "true")
    git(ahead_repo, "config", "--worktree", "devctl.issue", "IK152D")
    git(ahead_repo, "config", "--worktree", "devctl.base", ahead_base)
    write(ahead_repo / ".xflow" / "current-task.md", current_task_text("IK152D"))
    ahead_mr = ahead_repo / ".xflow" / "issues" / "issue-IK152D" / "mr-draft.md"
    write(ahead_mr, mr_draft_text("IK152D"))
    enable(ahead_repo, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    ahead = run_devctl(
        ahead_repo,
        "git",
        "mr",
        "--issue",
        "IK152D",
        "--body-file",
        str(ahead_mr),
        expect=1,
    )
    assert "unpushed commit" in ahead.stderr
    assert "[UNATTENDED]" not in ahead.stdout


def test_mr_rejects_branch_behind_remote_base(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    origin = parent / "origin.git"
    seed = parent / "seed"
    work = parent / "work"
    git(parent, "init", "--bare", str(origin))
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test User")
    git(seed, "checkout", "-b", "main", "-q")
    write(seed / "README.md", "# Demo\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "init", "-q")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main", "-q")

    subprocess.run(["git", "clone", str(origin), str(work), "-q"], check=True)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    git(work, "checkout", "-b", "feature/IK152D-behind-base", "origin/main", "-q")
    git(work, "config", "extensions.worktreeConfig", "true")
    git(work, "config", "--worktree", "devctl.issue", "IK152D")
    git(work, "config", "--worktree", "devctl.base", "main")
    write(work / ".xflow" / "current-task.md", current_task_text("IK152D"))
    mr_file = work / ".xflow" / "issues" / "issue-IK152D" / "mr-draft.md"
    write(mr_file, mr_draft_text("IK152D"))
    write(work / "feature.txt", "feature is ready before base advances\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "feature ready", "-q")
    git(work, "push", "-u", "origin", "feature/IK152D-behind-base", "-q")

    write(seed / "base-change.txt", "new target baseline\n")
    git(seed, "add", "base-change.txt")
    git(seed, "commit", "-m", "advance base", "-q")
    git(seed, "push", "origin", "main", "-q")

    enable(work, "IK152D", "XFLOW_HUMAN_UNATTENDED_ALL")
    rejected = run_devctl(
        work,
        "git",
        "mr",
        "--issue",
        "IK152D",
        "--body-file",
        str(mr_file),
        "--base",
        "main",
        expect=1,
    )
    assert "does not contain origin/main" in rejected.stderr
    assert "[UNATTENDED]" not in rejected.stdout


def test_draft_state_migrates_only_after_confirmed_issue_creation(parent: Path) -> None:
    repo = parent / "repo"
    init_test_repo(repo)
    git(repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    draft = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(draft, issue_draft_text("Create the confirmed remote Issue."))
    enable(repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")

    skipped = run_devctl(repo, "issue", "create", "Skipped provider", "--body-file", str(draft))
    assert "provider skipped" in skipped.stdout
    assert require_active(repo, "draft").issue == "draft"
    assert not tuple((repo / ".xflow" / "issues").glob("issue-*/approvals/history/*.yaml"))

    with RecordingApiServer() as server:
        env = {
            "GITHUB_API_BASE": server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        created = run_devctl_with_env(
            repo,
            env,
            "issue",
            "create",
            "Confirmed provider result",
            "--body-file",
            str(draft),
        )
        assert "Issue #42 created" in created.stdout
        assert require_active(repo, "42").issue == "42"
        assert not list((repo / ".xflow" / "local").glob("unattended.json.*.tmp"))
        created_history = tuple((repo / ".xflow" / "issues" / "issue-42").glob("approvals/history/*.yaml"))
        assert len(created_history) == 1
        assert not tuple((repo / ".xflow" / "issues" / "issue-draft").glob("approvals/history/*.yaml"))

    failed_repo = parent / "failed-repo"
    init_test_repo(failed_repo)
    git(failed_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    failed_draft = failed_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(failed_draft, issue_draft_text("Keep draft state after provider failure."))
    enable(failed_repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    failed = run_devctl_with_env(
        failed_repo,
        {
            "GITHUB_API_BASE": "http://127.0.0.1:1",
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        },
        "issue",
        "create",
        "Provider failure",
        "--body-file",
        str(failed_draft),
        expect=1,
    )
    assert "request failed" in failed.stderr.lower()
    assert require_active(failed_repo, "draft").issue == "draft"
    assert not tuple((failed_repo / ".xflow" / "issues").glob("issue-*/approvals/history/*.yaml"))

    ambiguous_repo = parent / "ambiguous-repo"
    init_test_repo(ambiguous_repo)
    git(ambiguous_repo, "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git")
    ambiguous_draft = ambiguous_repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(ambiguous_draft, issue_draft_text("Keep draft state without a confirmed Issue ID."))
    enable(ambiguous_repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
    with RecordingApiServer(issue_create_payload="{}") as server:
        ambiguous = run_devctl_with_env(
            ambiguous_repo,
            {
                "GITHUB_API_BASE": server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            },
            "issue",
            "create",
            "Ambiguous provider result",
            "--body-file",
            str(ambiguous_draft),
            expect=1,
        )
        assert "response missing number" in ambiguous.stderr.lower()
    assert require_active(ambiguous_repo, "draft").issue == "draft"
    assert not tuple((ambiguous_repo / ".xflow" / "issues").glob("issue-*/approvals/history/*.yaml"))


def test_env_loading_policy(repo: Path) -> None:
    fake_home = repo / "fake-home"
    global_env = fake_home / ".xflow" / "env.local"
    project_env = repo / ".xflow" / "local" / "env.local"
    write(global_env, "GITHUB_TOKEN=global-gh\nGITEE_TOKEN=global-ge\nXFLOW_PLATFORM=github\n")
    write(project_env, "XFLOW_PLATFORM=gitee\n")
    env = {"USERPROFILE": str(fake_home), "HOME": str(fake_home), "DEVCTL_REPO_ROOT": str(repo)}
    loaded = load_env_files(env)
    assert global_env.resolve() in loaded
    assert project_env.resolve() in loaded
    assert env["GITHUB_TOKEN"] == "global-gh"
    assert env["GITEE_TOKEN"] == "global-ge"
    assert env["XFLOW_PLATFORM"] == "gitee"

    project_env.unlink()
    env = {"USERPROFILE": str(fake_home), "HOME": str(fake_home), "DEVCTL_REPO_ROOT": str(repo)}
    load_env_files(env)
    assert env["GITHUB_TOKEN"] == "global-gh"
    assert env["GITEE_TOKEN"] == "global-ge"
    assert "XFLOW_PLATFORM" not in env


def test_python_core_rejects_inline_remote_bodies(repo: Path) -> None:
    run_devctl(repo, "issue", "create", "Inline body", "--body", "simple status update", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "line1\nline2", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", r"line1\nline2", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "uses `code`", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "uses $(cmd)", "--no-local-review", expect=1)


def test_issue_identifiers_are_portable(repo: Path) -> None:
    assert issue_dir(repo, "#IJZT85") == repo / ".xflow" / "issues" / "issue-IJZT85"
    assert default_issue_file(repo, "IJZT85", "mr-draft.md") == repo / ".xflow" / "issues" / "issue-IJZT85" / "mr-draft.md"
    assert default_approval_file(repo, "#IJZT85") == repo / ".xflow" / "issues" / "issue-IJZT85" / "approvals" / "local-review.md"
    assert branch_name_from_slug("gitee-work", "#IJZT85") == "feat/IJZT85-gitee-work"

    for unsafe in ("", "#", "../1", "issue/1", "A\\B", ".", "..", "bad:id"):
        try:
            issue_dir(repo, unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe issue identifier should be rejected: {unsafe!r}")


def assert_dependency_error(repo: Path, issue: str, yaml_text: str, expected: str) -> None:
    path = repo / ".xflow" / "issues" / f"issue-{issue}" / "dependencies.yaml"
    write(path, yaml_text)
    try:
        check_dependencies(repo, issue)
    except ValueError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"dependency check should reject: {expected}")


def dependency_yaml(issue: str = "IK152D", dependency: str = "IK17AW") -> str:
    return f"""version: 0.1.0
issue: {issue}
dependencies:
  - issue: {dependency}
    repository: xflow-web
    type: shared-infrastructure
    requiredFor:
      - C-004
    integrationTarget: mainline
    status: integrated
    blockingAssessment: partial
    decision: continue
    rationale: 属性编辑可继续，最终验证依赖统一端点能力。
    delivery:
      branch: fix/{dependency}-canonical-endpoints
      commit: abc1234
      mergeRequest: "56"
    integration:
      commit: def5678
      verifiedBy:
        - C-004
      evidence:
        - evidence/logs/c-004-integration-tests.txt
    closureAssessment:
      affectsClosure: true
      decision: integrated
      rationale: 相关验收已在主功能分支重新验证。
"""


def test_dependency_parser(repo: Path) -> None:
    gitee_root = repo / "gitee-dependencies"
    evidence = gitee_root / ".xflow" / "issues" / "issue-IK152D" / "evidence" / "logs" / "c-004-integration-tests.txt"
    write(evidence, "parent integration evidence\n")
    write(evidence.parents[2] / "dependencies.yaml", dependency_yaml())
    result = check_dependencies(gitee_root, "IK152D")
    assert isinstance(result, DependencyCheckResult)
    assert result.path.name == "dependencies.yaml"
    assert result.entries[0]["issue"] == "IK17AW"
    assert result.warnings == ()

    active_yaml = dependency_yaml().replace("status: integrated", "status: active")
    write(evidence.parents[2] / "dependencies.yaml", active_yaml)
    active_check = run_devctl(gitee_root, "check", "dependencies", "--issue", "IK152D")
    assert "[WARN] dependency #IK17AW is active" in active_check.stdout
    explicit_check = run_devctl(
        gitee_root,
        "check",
        "dependencies",
        "--issue",
        "IK152D",
        "--file",
        str(evidence.parents[2] / "dependencies.yaml"),
    )
    assert "dependencies check passed" in explicit_check.stdout
    write(evidence.parents[2] / "dependencies.yaml", dependency_yaml())

    github_root = repo / "github-dependencies"
    github_evidence = github_root / ".xflow" / "issues" / "issue-123" / "evidence" / "logs" / "c-004-integration-tests.txt"
    write(github_evidence, "numeric parent integration evidence\n")
    write(github_evidence.parents[2] / "dependencies.yaml", dependency_yaml("123", "456"))
    numeric = check_dependencies(github_root, "123")
    assert numeric.entries[0]["issue"] == "456"

    invalid_root = repo / "invalid-dependencies"
    base = dependency_yaml()
    write(invalid_root / ".xflow" / "issues" / "issue-IK152D" / "evidence" / "logs" / "c-004-integration-tests.txt", "evidence\n")
    cases = (
        (base.replace("issue: IK152D", "issue: WRONG", 1), "top-level issue"),
        (base.replace("type: shared-infrastructure", "type: unknown"), "type"),
        (base.replace("status: integrated", "status: done"), "status"),
        (base.replace("blockingAssessment: partial", "blockingAssessment: maybe"), "blockingAssessment"),
        (base.replace("decision: continue", "decision: integrated", 1), "development decision"),
        (base.replace("    requiredFor:\n      - C-004", "    requiredFor: []"), "requiredFor"),
        (base.replace("rationale: 属性编辑可继续，最终验证依赖统一端点能力。", "rationale: ''"), "rationale"),
        (base.replace("repository: xflow-web", "repository: {name: xflow-web}"), "repository must be a string"),
        (base.replace("      - C-004", "      - {case: C-004}", 1), "requiredFor items must be strings"),
        (base.replace("rationale: 属性编辑可继续，最终验证依赖统一端点能力。", "rationale: [invalid]"), "rationale must be a string"),
        (base.replace("status: integrated", "status: discovered"), "invalid dependency #IK17AW status"),
        (base.replace("      branch: fix/IK17AW-canonical-endpoints", "      branch: [invalid]"), "delivery.branch must be a string"),
        (
            base.replace("status: integrated", "status: active").replace(
                "      branch: fix/IK17AW-canonical-endpoints", "      branch: [invalid]"
            ),
            "delivery.branch must be a string",
        ),
        (
            base.replace("status: integrated", "status: active").replace(
                """    integration:
      commit: def5678
      verifiedBy:
        - C-004
      evidence:
        - evidence/logs/c-004-integration-tests.txt
""",
                "    integration: [invalid]\n",
            ),
            "integration must be a mapping",
        ),
        (base.replace("        - C-004", "        - false"), "integration.verifiedBy items must be strings"),
        (base.replace("      rationale: 相关验收已在主功能分支重新验证。", "      rationale: {invalid: true}"), "closureAssessment.rationale must be a string"),
        (base.replace("status: integrated", "status: available").replace("      commit: abc1234", "      commit: ''"), "delivery.commit"),
        (base.replace("      commit: def5678", "      commit: ''"), "integration.commit"),
        (base.replace("      verifiedBy:\n        - C-004", "      verifiedBy: []"), "integration.verifiedBy"),
        (base.replace("        - evidence/logs/c-004-integration-tests.txt", "        - https://example.test/evidence.txt"), "stay in the repository"),
        (base.replace("        - evidence/logs/c-004-integration-tests.txt", "        - oss://bucket/evidence.txt"), "stay in the repository"),
        (base.replace("        - evidence/logs/c-004-integration-tests.txt", "        - cos://bucket/evidence.txt"), "stay in the repository"),
        (base.replace("        - evidence/logs/c-004-integration-tests.txt", "        - ../issue-IK17AW/resolution-report.md"), "must not contain '..'"),
    )
    for yaml_text, expected in cases:
        assert_dependency_error(invalid_root, "IK152D", yaml_text, expected)

    available_external = """version: 0.1.0
issue: IK152D
dependencies:
  - issue: EXT-1
    repository: external-service
    type: external
    requiredFor: [C-009]
    status: available
    blockingAssessment: partial
    decision: continue
    rationale: 外部服务可用后继续集成验证。
    provider: ''
    availableVersion: ''
    verificationEntry: ''
"""
    for field in ("provider", "availableVersion", "verificationEntry"):
        candidate = available_external
        for other in ("provider", "availableVersion", "verificationEntry"):
            candidate = candidate.replace(f"    {other}: ''", f"    {other}: value" if other != field else f"    {other}: ''")
        assert_dependency_error(invalid_root, "IK152D", candidate, field)
    assert_dependency_error(
        invalid_root,
        "IK152D",
        available_external.replace("    provider: ''", "    provider: true"),
        "provider must be a string",
    )
    assert_dependency_error(
        invalid_root,
        "IK152D",
        available_external.replace("status: available", "status: active").replace("    provider: ''", "    provider: true"),
        "provider must be a string",
    )

    valid_external = available_external
    for field in ("provider", "availableVersion", "verificationEntry"):
        valid_external = valid_external.replace(f"    {field}: ''", f"    {field}: value")
    write(
        invalid_root / ".xflow" / "issues" / "issue-IK152D" / "dependencies.yaml",
        valid_external,
    )
    external_result = check_dependencies(invalid_root, "IK152D")
    assert external_result.entries[0]["type"] == "external"

    assert_dependency_error(
        invalid_root,
        "IK152D",
        valid_external + "    delivery: local-commit\n",
        "delivery must be a mapping",
    )
    assert_dependency_error(
        invalid_root,
        "IK152D",
        valid_external + "    delivery: {}\n",
        "external dependency #EXT-1 must not declare delivery",
    )

    superseded = base.replace("status: integrated", "status: superseded").replace(
        "      decision: integrated\n      rationale: 相关验收已在主功能分支重新验证。",
        "      decision: superseded\n      rationale: ''",
    )
    assert_dependency_error(invalid_root, "IK152D", superseded, "closureAssessment.rationale")

    temporary_adapter = base.replace("status: integrated", "status: active").replace(
        "decision: continue",
        "decision: use-temporary-adapter",
        1,
    )
    assert_dependency_error(invalid_root, "IK152D", temporary_adapter, "removalCondition")
    assert_dependency_error(
        invalid_root,
        "IK152D",
        temporary_adapter.replace(
            "    decision: use-temporary-adapter\n",
            "    decision: use-temporary-adapter\n    removalCondition: ''\n",
        ),
        "removalCondition",
    )
    valid_temporary_adapter = temporary_adapter.replace(
        "    decision: use-temporary-adapter\n",
        "    decision: use-temporary-adapter\n    removalCondition: 依赖集成并通过 C-004 后移除。\n",
    )
    write(
        invalid_root / ".xflow" / "issues" / "issue-IK152D" / "dependencies.yaml",
        valid_temporary_adapter,
    )
    temporary_result = check_dependencies(invalid_root, "IK152D")
    assert temporary_result.warnings == (
        "dependency #IK17AW is active; developer decision remains use-temporary-adapter",
    )

    dependency_report = base.replace(
        "evidence/logs/c-004-integration-tests.txt",
        "evidence/resolution-report.md",
    )
    write(
        invalid_root / ".xflow" / "issues" / "issue-IK152D" / "evidence" / "resolution-report.md",
        "dependency resolution report\n",
    )
    assert_dependency_error(invalid_root, "IK152D", dependency_report, "fresh parent-side evidence")

    evidence_directory = base.replace(
        "evidence/logs/c-004-integration-tests.txt",
        "evidence/logs",
    )
    assert_dependency_error(invalid_root, "IK152D", evidence_directory, "must be a regular file")

    issue_root = invalid_root / ".xflow" / "issues" / "issue-IK152D"
    outside_evidence = invalid_root / "outside-evidence.txt"
    write(outside_evidence, "outside issue workspace\n")
    outside_link = issue_root / "evidence" / "logs" / "outside-link.txt"
    broken_link = issue_root / "evidence" / "logs" / "broken-link.txt"
    try:
        outside_link.symlink_to(outside_evidence)
        broken_link.symlink_to(issue_root / "evidence" / "logs" / "missing-target.txt")
    except (OSError, NotImplementedError):
        outside_link.unlink(missing_ok=True)
        broken_link.unlink(missing_ok=True)
    else:
        assert_dependency_error(
            invalid_root,
            "IK152D",
            base.replace("evidence/logs/c-004-integration-tests.txt", "evidence/logs/outside-link.txt"),
            "must not traverse a symlink, junction, or reparse point",
        )
        assert_dependency_error(
            invalid_root,
            "IK152D",
            base.replace("evidence/logs/c-004-integration-tests.txt", "evidence/logs/broken-link.txt"),
            "must not traverse a symlink, junction, or reparse point",
        )


def resolution_report_text(conclusion: str) -> str:
    return f"""# Resolution Report

## Source Problem Or Gap
- gap-analysis.md

## Actual Changes
- Added dependency closure checks.

## Evidence Index
- [resolution note](evidence/resolution-note.txt)

## Completion Verification

### Criterion C-001: Dependency closure matches the report

#### Verification Type
non-ui

#### Expected Result
The report conclusion matches the parent dependency state.

#### Evidence
- [resolution note](evidence/resolution-note.txt)

#### Actual Result
The dependency closure matrix returned the expected result.

#### Human Review
- [ ] Confirm this evidence supports the reported closure.

## Closure Conclusion
Conclusion: {conclusion}
Reason: Dependency impact is recorded.

## AI Self-Review Result
- [x] Dependency state and closure assessment are consistent.

## Remaining Risks
- none

## Human Review Request
- Please review the dependency conclusion.
"""


def closure_yaml(status: str, closure: str | None) -> str:
    text = dependency_yaml().replace("status: integrated", f"status: {status}")
    marker = "    closureAssessment:\n"
    prefix = text.split(marker, 1)[0]
    if closure is None:
        return prefix
    return prefix + marker + closure


def assert_resolution_closure_error(repo: Path, dependencies: str, expected_issue: str = "IK17AW") -> None:
    issue_root = repo / ".xflow" / "issues" / "issue-IK152D"
    write(issue_root / "dependencies.yaml", dependencies)
    try:
        check_resolution_report(repo, "IK152D")
    except ValueError as exc:
        assert expected_issue in str(exc), str(exc)
    else:
        raise AssertionError(f"resolved report should reject dependency #{expected_issue}")


def test_resolution_report_dependency_closure(repo: Path) -> None:
    issue_root = repo / ".xflow" / "issues" / "issue-IK152D"
    write(
        repo / ".xflow" / "current-task.md",
        "# XFlow Current Task\n\nIssue: IK152D\nState: S5_LOCAL_VERIFICATION\n",
    )
    write(issue_root / "evidence" / "resolution-note.txt", "resolution evidence\n")
    write(issue_root / "evidence" / "logs" / "c-004-integration-tests.txt", "integration evidence\n")
    report = issue_root / "resolution-report.md"
    write(report, resolution_report_text("resolved"))

    integrated = dependency_yaml()
    write(issue_root / "dependencies.yaml", integrated)
    check_resolution_report(repo, "IK152D")

    available = closure_yaml(
        "available",
        "      affectsClosure: true\n      decision: integrated\n      rationale: 最终验收依赖该能力。\n",
    )
    assert_resolution_closure_error(repo, available)

    active_not_required = closure_yaml(
        "active",
        "      affectsClosure: false\n      decision: not-required\n      rationale: 当前验收条件不依赖后续增强。\n",
    )
    write(issue_root / "dependencies.yaml", active_not_required)
    check_resolution_report(repo, "IK152D")

    active_without_closure = closure_yaml("active", None)
    assert_resolution_closure_error(repo, active_without_closure)

    superseded = closure_yaml(
        "superseded",
        "      affectsClosure: true\n      decision: superseded\n      rationale: 经审核的设计变更已移除该依赖。\n",
    )
    write(issue_root / "dependencies.yaml", superseded)
    check_resolution_report(repo, "IK152D")

    for conclusion in ("reduced", "blocked"):
        write(report, resolution_report_text(conclusion))
        write(issue_root / "dependencies.yaml", active_without_closure)
        check_resolution_report(repo, "IK152D")


def test_resolution_report_traceability_closure(repo: Path) -> None:
    fixture_root = OPS_ROOT / "tests" / "fixtures"
    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"contracts"}}\n')
    (repo / "contracts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_root / "contracts" / "valid.yaml", repo / "contracts" / "contract.yaml")
    contract_path = repo / "contracts" / "contract.yaml"
    write(
        contract_path,
        contract_path.read_text(encoding="utf-8").replace(
            "      - type: automated\n        target: contract-test",
            "      - type: product-integration\n        target: http://127.0.0.1:5173/design/42",
            1,
        ),
    )
    issue_root = repo / ".xflow" / "issues" / "issue-101"
    issue_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_root / "traceability" / "valid.yaml", issue_root / "traceability-matrix.yaml")
    for relative in (
        "tests/test_operation.py",
        "tests/test_rejection.py",
        "evidence/api/operation-before.json",
        "evidence/api/operation-after.json",
        "evidence/api/rejection-before.json",
        "evidence/api/rejection-after.json",
    ):
        write(issue_root / relative, f"trace fixture: {relative}\n")
    screenshot = issue_root / "evidence" / "screenshots" / "c-001-after.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), (18, 92, 140)).save(screenshot, format="PNG")
    write(
        issue_root / "evidence" / "dom" / "c-001-after.json",
        json.dumps(
            {
                "surface": "product",
                "targetUrl": "http://127.0.0.1:5173/design/42",
                "pageTitle": "XFlow Studio",
                "modelIdentity": "model-42",
                "runtime": {"route": "/design/42", "modelLoaded": True},
            },
            ensure_ascii=True,
        )
        + "\n",
    )
    state = TaskState(
        issue="101",
        execution_state="S5_LOCAL_VERIFICATION",
        semantic_phase="classified",
        classification="ui-defect",
        contract="example.contract.capability-name@0.1.0",
        contract_file="contracts/contract.yaml",
        contract_change_required=False,
        branch=resolve_bindings(repo).branch,
        base="main",
        allowed_actions=("verify contract closure",),
        forbidden_actions=("push",),
        human_gate="human review required",
        human_approval_ref="none",
    )
    write(issue_root / "task-state.md", render_task_state(state))
    write(
        issue_root / "classification.yaml",
        """version: 0.1.0
request:
  originalStatement: Verify the accepted capability implementation.
contractSearch:
  status: found
  refs: [contracts/contract.yaml]
classification: ui-defect
contractChangeRequired: false
reason: The implementation must close the existing contract.
nextArtifact: lightweight-route-complete
decisionSource: ai-proposed
""",
    )
    write(
        issue_root / "issue-draft.md",
        """<!-- xflow: issue-draft -->

## Background
Verify the capability contract.

## Problem
The implementation needs closure evidence.

## Goal
Close the declared verification scenarios.

## Scope
- Includes contract verification.

## Acceptance Criteria
- [ ] C-001: The successful operation is verified.
- [ ] C-002: The rejected operation preserves state.

## Verification Plan
- Run the declared tests and collect local evidence.
""",
    )
    evidence = """- [operation after](evidence/api/operation-after.json)
- [UI screenshot](evidence/screenshots/c-001-after.png)
- [UI state](evidence/dom/c-001-after.json)
- [rejection after](evidence/api/rejection-after.json)"""
    report = f"""# Resolution Report

## Source Problem Or Gap
- issue-draft.md

## Actual Changes
- Closed the capability verification chain.

## Evidence Index
{evidence}

## Completion Verification

### Criterion C-001: The successful operation is verified

#### Verification Type
product-integration

#### Expected Result
The product operation succeeds.

#### Evidence
{evidence}

#### Actual Result
The product result was observed.

#### Human Review
- [ ] Confirm criterion C-001.

### Criterion C-002: The rejected operation preserves state

#### Verification Type
automated

#### Expected Result
The rejection preserves state.

#### Evidence
{evidence}

#### Actual Result
The preserved state was observed.

#### Human Review
- [ ] Confirm criterion C-002.

## Closure Conclusion
Conclusion: resolved
Reason: Trace conclusions match this report.

## AI Self-Review Result
- [x] Trace and report evidence are consistent.

## Remaining Risks
- External capture authorship requires human review.

## Human Review Request
- Review the evidence identities and conclusions.
"""
    before_time = screenshot.stat().st_mtime_ns - 1_000_000_000
    for relative in ("evidence/api/operation-before.json", "evidence/api/rejection-before.json"):
        os.utime(issue_root / relative, ns=(before_time, before_time))
    after_time = screenshot.stat().st_mtime_ns
    for relative in (
        "evidence/api/operation-after.json",
        "evidence/api/rejection-after.json",
        "evidence/screenshots/c-001-after.png",
        "evidence/dom/c-001-after.json",
    ):
        os.utime(issue_root / relative, ns=(after_time, after_time))
    activate_task(repo, "101")
    write(issue_root / "resolution-report.md", report)
    check_resolution_report(repo, "101")

    matrix = issue_root / "traceability-matrix.yaml"
    write(matrix, matrix.read_text(encoding="utf-8").replace("conclusion: resolved", "conclusion: reduced", 1))
    try:
        check_resolution_report(repo, "101")
    except ValueError as exc:
        assert "every trace entry to be resolved" in str(exc), str(exc)
    else:
        raise AssertionError("resolved report should reject reduced trace entry")
    write(issue_root / "resolution-report.md", report.replace("Conclusion: resolved", "Conclusion: reduced"))
    check_resolution_report(repo, "101")
    active_task_pointer_file(repo, resolve_bindings(repo).worktree).unlink()


def assert_commit_message_error(message: str, expected: str, branch_issue: str | None = None) -> None:
    try:
        check_commit_message(message, branch_issue=branch_issue)
    except ValueError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"commit message should reject: {expected}")


def test_commit_message_validator() -> None:
    gitee = (
        "feat(canvas): 修复稳定端点定位[#IK152D]\n\n"
        "- 调整统一端点计算\n"
        "- 覆盖 C-004 并记录测试证据\n"
    )
    assert check_commit_message(gitee, branch_issue="IK152D") == ("IK152D",)

    github = (
        "fix(api): 修复请求签名校验[#123]\n\n"
        "- 调整请求签名的校验顺序\n"
        "- 覆盖数字 Issue 的回归测试\n"
    )
    assert check_commit_message(github, branch_issue="#123") == ("123",)

    merge = (
        "merge(canvas): 集成统一容器事务能力[#IK152D][#IK17AW]\n\n"
        "- 合并主功能与依赖能力的实现\n"
        "- 完成联合回归并记录本地证据\n"
    )
    assert check_commit_message(merge, branch_issue="IK152D") == ("IK152D", "IK17AW")

    cases = (
        (gitee.replace("feat(canvas)", "feat"), "scope"),
        (gitee.replace("feat(canvas)", "feat(   )"), "scope"),
        (gitee.replace("[#IK152D]", ""), "Issue"),
        (gitee.replace("修复稳定端点定位", "fix stable endpoint"), "Chinese-dominant"),
        ("feat(canvas): 修复稳定端点定位[#IK152D]", "blank separator"),
        (gitee.rsplit("\n- ", 1)[0] + "\n", "at least two"),
        (gitee.replace("- 覆盖 C-004 并记录测试证据", "- verify C-004 with tests"), "Chinese-dominant"),
        (gitee.replace("[#IK152D]", "[#IK152D][#IK17AW]"), "only merge"),
        (merge.replace("[#IK152D][#IK17AW]", "[#IK152D][#IK17AW][#789]"), "one or two"),
        (gitee + "Co-authored-by: Claude <bot@example.test>\n", "AI-client trailer"),
        (gitee + "Generated-by: tool\n", "AI-client trailer"),
        (gitee + "OpenAI-Codex\n", "AI-client trailer"),
        (gitee + "- 证据位于 C:\\temp\\evidence.txt\n", "absolute Windows path"),
        (gitee + "- 证据位于 /home/user/evidence.txt\n", "local absolute path"),
        (gitee + "- 证据位于 `/workspace/repo/evidence.txt`\n", "local absolute path"),
        (gitee + "- 证据位于 `file:///etc/passwd`\n", "local absolute path"),
        (gitee + "- 证据位于根目录 `/`\n", "local absolute path"),
        (gitee + "- 证据位于 \\\\server\\share\\evidence.txt\n", "local absolute path"),
        (gitee + "- 证据位于（`\\\\server\\share\\evidence.txt`）\n", "local absolute path"),
        (gitee + "- 证据位于 \\\\.\\PhysicalDrive0\n", "local absolute path"),
        (gitee + "- 证据位于（`\\\\.\\PhysicalDrive0`）\n", "local absolute path"),
        (gitee + "GitHub-PR: 42\n", "provider-only metadata"),
    )
    for message, expected in cases:
        assert_commit_message_error(message, expected)
    assert_commit_message_error(gitee, "first Issue", branch_issue="IK17AW")
    assert_commit_message_error(gitee.replace("[#IK152D]", "[#..]"), "Issue identifier")
    assert_commit_message_error(merge.replace("[#IK17AW]", "[#IK152D]"), "distinct")
    check_commit_message(
        gitee + "- 远端验证证据链接已经发布并可供人工复核：https://example.test/workspace/evidence.txt\n"
    )
    check_commit_message(
        gitee + "- 远端验证证据链接已经发布并可供人工复核：http://example.test/workspace/evidence.txt\n"
    )
    check_commit_message(
        gitee + "- 远端仓库链接已经确认并可供人工复核：ssh://git@example.test/repository/project.git\n"
    )
    check_commit_message(gitee + "- 前端/后端均已完成中文验证并保留人工可见证据\n")


def test_commit_message_cli(repo: Path) -> None:
    message_file = repo / ".xflow" / "local" / "commit-message.txt"
    write(
        message_file,
        "fix(api): 修复请求签名校验[#123]\n\n"
        "- 调整请求签名的校验顺序\n"
        "- 覆盖数字 Issue 的回归测试\n",
    )
    result = run_devctl(repo, "check", "commit-msg", "--file", str(message_file), "--issue", "123")
    assert "associated Issues: #123" in result.stdout
    assert "commit-msg check passed" in result.stdout

    write(message_file, "fix(api): invalid English summary[#123]\n\n- 中文主体一\n- 中文主体二\n")
    run_devctl(repo, "check", "commit-msg", "--file", str(message_file), "--issue", "123", expect=1)


def test_commit_message_generator(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "feature/IK152D-commit-policy", "-q")
    write(repo / "README.md", "# Demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "初始化仓库", "-q")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "IK152D")
    write(repo / "feature.txt", "commit generator fixture\n")
    git(repo, "add", "feature.txt")

    generated = summarize_commit_message(repo, None, None)
    assert generated.startswith("chore(feature.txt): 更新 feature.txt[#IK152D]\n\n")
    assert len([line for line in generated.splitlines()[2:] if line.strip()]) >= 2
    check_commit_message(generated, branch_issue="IK152D")

    positional = summarize_commit_message(repo, None, "修复稳定端点定位")
    assert positional.startswith("chore(feature.txt): 修复稳定端点定位[#IK152D]\n\n")
    check_commit_message(positional, branch_issue="IK152D")

    git(repo, "commit", "-m", generated, "-q")
    multi_file_names = (
        "alpha-long-feature-name.py",
        "beta-shared-service-layer.py",
        "gamma-verification-entry-point.py",
    )
    for name in multi_file_names:
        write(repo / name, f"# {name}\n")
        git(repo, "add", name)
    multi_default = summarize_commit_message(repo, None, None)
    check_commit_message(multi_default, branch_issue="IK152D")
    assert "3 个任务文件" in multi_default
    multi_default_body = "\n".join(multi_default.splitlines()[2:])
    assert all(name not in multi_default_body for name in multi_file_names)

    multi_positional = summarize_commit_message(repo, None, "调整依赖检查行为")
    check_commit_message(multi_positional, branch_issue="IK152D")
    assert "- 修改范围包含 3 个任务文件" in multi_positional
    multi_positional_body = "\n".join(multi_positional.splitlines()[2:])
    assert all(name not in multi_positional_body for name in multi_file_names)

    full_message = (
        "fix(canvas): 修复稳定端点定位[#IK152D]\n\n"
        "- 调整统一端点计算\n"
        "- 覆盖 C-004 并记录测试证据\n"
    )
    assert summarize_commit_message(repo, full_message, None) == full_message
    try:
        summarize_commit_message(repo, "修复稳定端点定位", None)
    except ValueError as exc:
        assert "commit subject" in str(exc)
    else:
        raise AssertionError("-m must reject an incomplete commit message")

    no_issue_repo = repo.parent / "no-issue"
    no_issue_repo.mkdir()
    git(no_issue_repo, "init", "-q")
    write(no_issue_repo / "change.txt", "change\n")
    try:
        summarize_commit_message(no_issue_repo, full_message, None)
    except ValueError as exc:
        assert "Issue identity" in str(exc)
    else:
        raise AssertionError("commit generation requires branch Issue metadata")


def test_pr_backfill_commit_message_without_push(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "feature/8-pr-backfill", "-q")
    write(repo / "README.md", "# Demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "初始化仓库", "-q")
    suggestion = repo / ".xflow" / "issues" / "issue-8" / "state-update-suggestion.md"
    write(suggestion, "PR: 42\n")
    previous = os.environ.get("DEVCTL_SKIP_PUSH")
    os.environ["DEVCTL_SKIP_PUSH"] = "1"
    try:
        skipped_push = commit_and_push_pr_backfill(repo, "feature/8-pr-backfill", [suggestion], "42", "8")
        assert skipped_push is not None
        assert not skipped_push.performed
        assert not skipped_push.success
    finally:
        if previous is None:
            os.environ.pop("DEVCTL_SKIP_PUSH", None)
        else:
            os.environ["DEVCTL_SKIP_PUSH"] = previous
    message = git_text(repo, "log", "-1", "--pretty=%B")
    assert message.startswith("chore(xflow): 回填合并请求状态[#8]\n\n")
    assert "- 记录合并请求编号与远端链接" in message
    assert "- 同步当前任务状态文件" in message
    check_commit_message(message, branch_issue="8")

    write(repo / "business.py", "print('business change')\n")
    git(repo, "add", "business.py")
    write(suggestion, "PR: 43\n")
    previous_head = git_text(repo, "rev-parse", "HEAD")
    previous = os.environ.get("DEVCTL_SKIP_PUSH")
    os.environ["DEVCTL_SKIP_PUSH"] = "1"
    try:
        try:
            commit_and_push_pr_backfill(repo, "feature/8-pr-backfill", [suggestion], "43", "8")
        except ValueError as exc:
            assert "staged" in str(exc) or "index" in str(exc)
        else:
            raise AssertionError("PR backfill must reject a pre-populated index")
    finally:
        if previous is None:
            os.environ.pop("DEVCTL_SKIP_PUSH", None)
        else:
            os.environ["DEVCTL_SKIP_PUSH"] = previous
    assert git_text(repo, "rev-parse", "HEAD") == previous_head
    assert git_text(repo, "diff", "--cached", "--name-only") == "business.py"
    assert "business.py" not in git_text(repo, "show", "--format=", "--name-only", "HEAD")


def test_pr_backfill_replays_real_commit_and_push_windows(parent: Path) -> None:
    parent.mkdir(parents=True)
    origin = parent / "origin.git"
    repo = parent / "repo"
    git(parent, "init", "--bare", str(origin))
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# Demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "初始化仓库", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")
    branch = "feature/8-pr-backfill-replay"
    git(repo, "checkout", "-b", branch, "-q")
    write(repo / "feature.txt", "feature content\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat(xflow): 添加回填恢复场景", "-q")
    git(repo, "push", "-u", "origin", branch, "-q")

    suggestion = repo / ".xflow" / "issues" / "issue-8" / "state-update-suggestion.md"
    write(suggestion, "PR: 42\nPR URL: https://example.invalid/pulls/42\n")
    with patch.object(cli_module, "push_branch", side_effect=RuntimeError("injected failure before backfill push")):
        try:
            commit_and_push_pr_backfill(repo, branch, [suggestion], "42", "8")
        except RuntimeError as exc:
            assert "injected failure" in str(exc)
        else:
            raise AssertionError("expected failure after local backfill commit")
    assert git_text(repo, "rev-list", "--count", f"origin/{branch}..HEAD") == "1"

    pushed = commit_and_push_pr_backfill(repo, branch, [suggestion], "42", "8")
    assert pushed is not None and pushed.performed and pushed.success
    assert git_text(repo, "rev-parse", "HEAD") == git_text(repo, "rev-parse", f"origin/{branch}")

    already_pushed = commit_and_push_pr_backfill(repo, branch, [suggestion], "42", "8")
    assert already_pushed is not None and already_pushed.performed and already_pushed.success


def test_python_core_git_and_app_commands(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    origin = parent / "origin.git"
    seed = parent / "seed"
    work = parent / "work"

    git(parent, "init", "--bare", str(origin))
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test User")
    git(seed, "checkout", "-b", "main", "-q")
    write(seed / "README.md", "# Demo\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "init", "-q")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main", "-q")

    subprocess.run(["git", "clone", str(origin), str(work), "-q"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    git(work, "checkout", "main", "-q")
    write(
        work / ".git" / "info" / "exclude",
        ".xflow/local/\n.xflow/current-task.md\n.xflow/issues/**/approvals/\n",
    )

    status = run_devctl(work, "git", "status").stdout
    assert "branch:  main" in status
    assert "worktree: clean" in status

    enable(work, "8", "XFLOW_HUMAN_UNATTENDED_ALL")
    run_devctl(work, "git", "start", "wsl-free", "--issue", "9", "--base", "main")
    assert load(work) is None
    assert git_text(work, "branch", "--show-current") == "feat/9-wsl-free"

    status = run_devctl(work, "git", "status").stdout
    assert "branch:  feat/9-wsl-free" in status
    assert "issue:   #9" in status

    write(work / "feature.txt", "python core git command\n")
    msg = run_devctl(work, "git", "commit-msg", "-a").stdout
    assert "chore(feature.txt): 更新 feature.txt[#9]" in msg
    assert "- 修改范围包含 feature.txt" in msg
    assert "- 验证结果由提交前检查确认" in msg

    positional = run_devctl(work, "git", "commit-msg", "修复稳定端点定位").stdout
    assert "chore(feature.txt): 修复稳定端点定位[#9]" in positional

    complete = (
        "fix(devctl): 修复提交消息生成[#9]\n\n"
        "- 校验完整消息并保持内容不变\n"
        "- 覆盖位置摘要兼容行为\n"
    )
    complete_result = run_devctl(work, "git", "commit-msg", "-m", complete).stdout
    assert complete in complete_result
    run_devctl(work, "git", "commit-msg", "-m", "修复提交消息生成", expect=1)

    write(work / "must-not-stage.txt", "invalid message must leave index untouched\n")
    before_invalid_index = git_text(work, "diff", "--cached", "--name-only")
    run_devctl(work, "git", "commit-msg", "-a", "-m", "修复提交消息生成", expect=1)
    assert git_text(work, "diff", "--cached", "--name-only") == before_invalid_index
    (work / "must-not-stage.txt").unlink()

    run_devctl(work, "git", "commit-msg", "-a", "-c")
    committed_message = git_text(work, "log", "-1", "--pretty=%B")
    assert "chore(feature.txt): 更新 feature.txt[#9]" in committed_message
    check_commit_message(committed_message, branch_issue="9")

    cleanup_evidence = work / ".xflow" / "issues" / "issue-9" / "resolution-report.md"
    write(cleanup_evidence, "# Resolution Report\n\nCleanup reviewed by the human.\n")
    write(work / ".xflow" / "current-task.md", current_task_text("9"))
    git(work, "add", str(cleanup_evidence.relative_to(work)))
    git(work, "commit", "-m", "record cleanup evidence", "-q")
    enable(work, "9", "XFLOW_HUMAN_UNATTENDED_ALL")
    assert require_active(work, "9").issue == "9"
    unattended_cleanup = run_devctl(
        work,
        "git",
        "done",
        "--force",
        "--base",
        "main",
        "--issue",
        "9",
        "--file",
        str(cleanup_evidence),
        expect=1,
    )
    assert "local review approval required" in unattended_cleanup.stderr
    assert git_text(work, "branch", "--show-current") == "feat/9-wsl-free"
    assert require_active(work, "9").issue == "9"
    wrong_cleanup_review = approval_gate.prepare(
        work,
        "9",
        "git-cleanup",
        cleanup_evidence,
    )
    write_text_lf(wrong_cleanup_review, wrong_cleanup_review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    wrong_cleanup = run_devctl(
        work,
        "git",
        "done",
        "--force",
        "--base",
        "main",
        "--issue",
        "9",
        "--file",
        str(cleanup_evidence),
        expect=1,
    )
    assert "action mismatch: expected git-cleanup-force, got git-cleanup" in wrong_cleanup.stderr
    cleanup_review = approval_gate.prepare(
        work,
        "9",
        "git-cleanup-force",
        cleanup_evidence,
        force=True,
    )
    cleanup_review_text = cleanup_review.read_text(encoding="utf-8")
    assert (
        f"Suggested Command: devctl git done --force --issue 9 --file "
        f"{cleanup_evidence.relative_to(work).as_posix()}"
    ) in cleanup_review_text
    write_text_lf(cleanup_review, cleanup_review_text.replace("Approved: no", "Approved: yes"))
    cleanup_status = git_text(work, "status", "--porcelain")
    assert not cleanup_status, cleanup_status
    run_devctl(
        work,
        "git",
        "done",
        "--force",
        "--base",
        "main",
        "--issue",
        "9",
        "--file",
        str(cleanup_evidence),
    )
    assert git_text(work, "branch", "--show-current") == "main"
    assert "feat/9-wsl-free" not in git_text(work, "branch", "--format=%(refname:short)")
    assert load(work) is None

    removed_app = run_devctl(work, "app", expect=2)
    assert "invalid choice" in removed_app.stderr


def test_git_done_requires_exact_human_cleanup_approval(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    origin = parent / "origin.git"
    work = parent / "work"
    git(parent, "init", "--bare", str(origin))
    work.mkdir()
    git(work, "init", "-q")
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    git(work, "checkout", "-b", "main", "-q")
    write(work / "README.md", "# Demo\n")
    evidence = work / ".xflow" / "issues" / "issue-8" / "resolution-report.md"
    write(evidence, "# Resolution Report\n\nCleanup reviewed by the human.\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "init cleanup fixture", "-q")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-u", "origin", "main", "-q")
    write(
        work / ".git" / "info" / "exclude",
        ".xflow/local/\n.xflow/current-task.md\n.xflow/issues/**/approvals/\n",
    )

    run_devctl(work, "git", "start", "safe-cleanup", "--issue", "8", "--base", "main")
    write(work / ".xflow" / "current-task.md", current_task_text("8"))
    enable(work, "8", "XFLOW_HUMAN_UNATTENDED_ALL")
    assert_value_error(
        "invalid approval action",
        lambda: approval_gate.prepare(work, "8", "remote-write", evidence),
    )
    wrong_action = approval_gate.prepare(work, "8", "git-push", evidence)
    write_text_lf(wrong_action, wrong_action.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    rejected = run_devctl(
        work,
        "git",
        "done",
        "--base",
        "main",
        "--issue",
        "8",
        "--file",
        str(evidence),
        expect=1,
    )
    assert "action mismatch: expected git-cleanup, got git-push" in rejected.stderr
    assert git_text(work, "branch", "--show-current") == "feat/8-safe-cleanup"
    assert require_active(work, "8").issue == "8"

    exact = approval_gate.prepare(work, "8", "git-cleanup", evidence, force=True)
    exact_text = exact.read_text(encoding="utf-8")
    assert f"Suggested Command: devctl git done --issue 8 --file {evidence.relative_to(work).as_posix()}" in exact_text
    write_text_lf(exact, exact_text.replace("Approved: no", "Approved: yes"))
    cleanup_status = git_text(work, "status", "--porcelain")
    assert not cleanup_status, cleanup_status
    run_devctl(
        work,
        "git",
        "done",
        "--base",
        "main",
        "--issue",
        "8",
        "--file",
        str(evidence),
    )
    assert git_text(work, "branch", "--show-current") == "main"
    assert "feat/8-safe-cleanup" not in git_text(work, "branch", "--format=%(refname:short)")
    assert load(work) is None

    run_devctl(work, "git", "start", "unmerged-cleanup", "--issue", "9", "--base", "main")
    write(work / ".xflow" / "current-task.md", current_task_text("9"))
    unmerged_evidence = work / ".xflow" / "issues" / "issue-9" / "resolution-report.md"
    write(unmerged_evidence, "# Resolution Report\n\nUnmerged cleanup evidence.\n")
    write(work / "unmerged.txt", "unmerged task work\n")
    git(work, "add", ".xflow/issues/issue-9/resolution-report.md", "unmerged.txt")
    git(work, "commit", "-m", "unmerged cleanup fixture", "-q")
    enable(work, "9", "XFLOW_HUMAN_UNATTENDED_ALL")
    unmerged_review = approval_gate.prepare(work, "9", "git-cleanup", unmerged_evidence)
    write_text_lf(unmerged_review, unmerged_review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    rejected_unmerged = run_devctl(
        work,
        "git",
        "done",
        "--base",
        "main",
        "--issue",
        "9",
        "--file",
        str(unmerged_evidence),
        expect=1,
    )
    assert "branch -d" in rejected_unmerged.stderr
    assert "feat/9-unmerged-cleanup" in git_text(work, "branch", "--format=%(refname:short)")
    assert require_active(work, "9").issue == "9"


def test_git_task_metadata_is_scoped_to_each_worktree(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    origin = parent / "origin.git"
    main_worktree = parent / "main-worktree"
    sibling_worktree = parent / "sibling-worktree"

    git(parent, "init", "--bare", str(origin))
    main_worktree.mkdir()
    git(main_worktree, "init", "-q")
    git(main_worktree, "config", "user.email", "test@example.com")
    git(main_worktree, "config", "user.name", "Test User")
    git(main_worktree, "checkout", "-b", "main", "-q")
    write(main_worktree / "README.md", "# Demo\n")
    git(main_worktree, "add", "README.md")
    git(main_worktree, "commit", "-m", "init", "-q")
    git(main_worktree, "remote", "add", "origin", str(origin))
    git(main_worktree, "push", "-u", "origin", "main", "-q")
    git(
        main_worktree,
        "worktree",
        "add",
        "-b",
        "feature/202-sibling-task",
        str(sibling_worktree),
        "main",
    )

    run_devctl(main_worktree, "git", "start", "main-task", "--issue", "101", "--base", "main")
    run_devctl(sibling_worktree, "git", "start", "sibling-task", "--issue", "202", "--base", "main")

    main_status = run_devctl(main_worktree, "git", "status").stdout
    sibling_status = run_devctl(sibling_worktree, "git", "status").stdout

    assert "branch:  feat/101-main-task" in main_status
    assert "slug:    main-task" in main_status
    assert "issue:   #101" in main_status
    assert "branch:  feat/202-sibling-task" in sibling_status
    assert "slug:    sibling-task" in sibling_status
    assert "issue:   #202" in sibling_status
    common_issue = subprocess.run(
        ["git", "-C", str(main_worktree), "config", "--local", "--get", "devctl.issue"],
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert common_issue.returncode != 0
    assert git_text(main_worktree, "config", "--worktree", "--get", "devctl.issue") == "101"
    assert git_text(sibling_worktree, "config", "--worktree", "--get", "devctl.issue") == "202"


def test_git_push_and_mr_are_separate_with_state_backfill(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    origin = parent / "origin.git"
    seed = parent / "seed"
    work = parent / "work"

    git(parent, "init", "--bare", str(origin))
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test User")
    git(seed, "checkout", "-b", "main", "-q")
    write(seed / "README.md", "# Demo\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "初始化仓库", "-q")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main", "-q")

    subprocess.run(["git", "clone", str(origin), str(work), "-q"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    git(work, "checkout", "main", "-q")

    run_devctl(work, "git", "start", "pr-state", "--issue", "8", "--base", "main")
    branch = "feat/8-pr-state"
    approval_state = TaskState(
        issue="8",
        execution_state="S6_PREPARE_COMMIT_AND_MR_DRAFT",
        semantic_phase="classified",
        classification="ui-defect",
        contract="example.contract.approval-history@0.1.0",
        contract_file="docs/requirements/example/contract.yaml",
        contract_change_required=False,
        branch=branch,
        base="main",
        allowed_actions=("prepare-verification",),
        forbidden_actions=("edit-implementation",),
        human_gate="local human approval required",
        human_approval_ref="none",
    )
    write(work / ".xflow" / "issues" / "issue-8" / "task-state.md", render_task_state(approval_state))
    activate_task(work, "8")
    write(
        work / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 8
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Create the approved PR after branch publication.

## Forbidden Actions
- Push code changes after PR creation without XFlow metadata-only scope.
""",
    )
    walkthrough = work / ".xflow" / "issues" / "issue-8" / "walkthrough.md"
    write(
        walkthrough,
        """# Walkthrough

Issue: 8

## Verification
- python tests/python-core.py
""",
    )
    mr_file = work / ".xflow" / "issues" / "issue-8" / "mr-draft.md"
    write(
        mr_file,
        """<!-- xflow: mr-draft -->

Closes #8

## Summary
- Add a task branch change.

## Test Plan
- python tests/python-core.py

## Risk
- Low.

## Review Request
- Please review local artifacts before remote write.
""",
    )
    write(work / "feature.txt", "task branch content\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "feat(xflow): 添加任务分支内容", "-q")

    run_devctl(work, "approval", "prepare", "--issue", "8", "--action", "git-mr", "--file", str(mr_file), "--force")
    approval = work / ".xflow" / "issues" / "issue-8" / "approvals" / "local-review.md"
    approval.write_text(approval.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8")

    mr_before_push = run_devctl(
        work,
        "git",
        "mr",
        "--title",
        "PR state backfill",
        "--body-file",
        str(mr_file),
        "--issue",
        "8",
        expect=1,
    )
    assert "devctl git push" in mr_before_push.stderr, mr_before_push.stderr
    assert branch not in git_text(origin, "branch", "--format=%(refname:short)")
    history = work / ".xflow" / "issues" / "issue-8" / "approvals" / "history"
    assert not tuple(history.glob("*.yaml"))

    run_devctl(work, "approval", "prepare", "--issue", "8", "--action", "git-push", "--file", str(walkthrough), "--force")
    approval.write_text(approval.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8")
    push_result = run_devctl(work, "git", "push", "--issue", "8", "--file", str(walkthrough))
    assert f"pushed {branch}" in push_result.stdout
    assert branch in git_text(origin, "branch", "--format=%(refname:short)")
    assert any("action: git-push" in path.read_text(encoding="utf-8") for path in history.glob("*.yaml"))

    run_devctl(work, "approval", "prepare", "--issue", "8", "--action", "git-mr", "--file", str(mr_file), "--force")
    approval.write_text(approval.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8")
    with RecordingApiServer() as github_server:
        env = {
            "DEVCTL_OWNER": "Linkk2000",
            "DEVCTL_REPO": "paper-demo",
            "GITHUB_API_BASE": github_server.base_url,
            "GITHUB_TOKEN": "github-token",
            "XFLOW_PLATFORM": "github",
            "DEVCTL_SKIP_PROVIDER_LOAD": "0",
        }
        mr_result = run_devctl_with_env(
            work,
            env,
            "git",
            "mr",
            "--title",
            "PR state backfill",
            "--body-file",
            str(mr_file),
            "--issue",
            "8",
        )
        assert "PR #42 created" in mr_result.stdout
        assert "state backfill pushed" in mr_result.stdout
        pr_requests = [item for item in github_server.requests if item["method"] == "POST" and item["path"].endswith("/pulls")]
        assert pr_requests

    latest_remote_message = git_text(origin, "log", f"refs/heads/{branch}", "-1", "--pretty=%B")
    assert latest_remote_message.startswith("chore(xflow): 回填合并请求状态[#8]\n\n")
    check_commit_message(latest_remote_message, branch_issue="8")
    remote_task = git_text(origin, "show", f"refs/heads/{branch}:.xflow/current-task.md")
    assert "State: S9_REMOTE_REVIEW_AND_CI" in remote_task
    assert "PR: 42" in remote_task
    assert "PR URL: https://github.test/pulls/42" in remote_task
    remote_suggestion = git_text(origin, "show", f"refs/heads/{branch}:.xflow/issues/issue-8/state-update-suggestion.md")
    assert "PR: 42" in remote_suggestion
    history_text = "\n".join(path.read_text(encoding="utf-8") for path in history.glob("*.yaml"))
    assert "action: git-mr" in history_text
    assert "action: git-state-backfill" in history_text
    assert "source: effect" in history_text
    assert "parentAction: git-mr" in history_text
    assert "Approved: yes" not in history_text


def test_ai_call_guidance_is_visible(repo: Path) -> None:
    issue_help = run_devctl(repo, "issue", "create", "--help").stdout
    assert "AI call recipes" in issue_help
    assert "Restricted unattended issue" not in issue_help
    assert "current-turn explicit human authorization" not in issue_help
    assert 'devctl issue create "<title>" --body-file issue.md --no-local-review' not in issue_help
    assert "Issue/comment image attachments are disabled" in issue_help
    assert "Do not use GitHub release assets as an issue image store" in issue_help
    assert "aliyun-oss" in issue_help
    assert "For non-image files, use a reviewed manifest" in issue_help
    assert "Inline --attach-file and --upload-attachments are disabled" in issue_help

    publish_help = run_devctl(repo, "attachment", "publish", "--help").stdout
    assert "Attachment publishing" in publish_help
    assert "writes publishedUrl" in publish_help
    assert "Do not use this backend as issue/comment image storage" in publish_help
    assert "Aliyun OSS mode" in publish_help
    assert "ALIYUN_OSS_ACCESS_KEY_SECRET" in publish_help

    git_help = run_devctl(repo, "git", "--help").stdout
    assert "push" in git_help
    assert "mr" in git_help

    help_text = (OPS_ROOT / "help.txt").read_text(encoding="utf-8")
    assert "AI call recipes" in help_text
    assert "Human Approval Is Non-Delegable" in help_text
    assert "Restricted unattended issue" not in help_text
    assert "explicitly authorized that exact unattended" not in help_text
    assert 'devctl issue create "<title>" --body-file issue.md --no-local-review' not in help_text
    assert "devctl unattended enable --issue <id|draft> --confirm XFLOW_HUMAN_UNATTENDED_ALL" in help_text
    assert "devctl unattended status" in help_text
    assert "devctl unattended disable" in help_text
    assert "devctl approval reconcile --issue <id|draft> --approval-id <id>" in help_text
    assert "[UNATTENDED] Human approval gate bypassed for current task <id>." in help_text
    assert "--no-local-review alone is invalid" in help_text
    for exclusion in ("force push", "history rewrite", "destructive deletion", "secret or permission changes"):
        assert exclusion in help_text
    assert "AI must never satisfy a human gate itself" in help_text
    assert "Only the human reviewer may change Approved: no to Approved: yes" in help_text
    assert "Issue/comment image attachments are disabled" in help_text
    assert "Inline --attach-file and --upload-attachments are disabled" in help_text
    assert "Do not use GitHub release assets as an issue image store" in help_text
    assert "devctl attachment publish --issue draft --backend aliyun-oss" in help_text
    assert "%USERPROFILE%\\.xflow\\env.local" in help_text
    assert "devctl git push --issue" in help_text
    assert "state backfill commit" in help_text
    assert "Do not run bare bash/Git-Bash/WSL for normal XFlow validation on Windows" in help_text
    assert "devctl check subtask --issue" in help_text
    assert "devctl check issue-evidence --issue" in help_text
    assert "devctl check gap-analysis --issue" in help_text
    assert "devctl check resolution-report --issue" in help_text
    assert "devctl check dependencies --issue IK152D" in help_text
    assert "devctl check commit-msg --file .xflow/local/commit-message.txt --issue IK152D" in help_text
    assert "type(scope): 中文核心摘要[#Issue编号]" in help_text
    assert "active dependencies warn but do not block local development" in help_text
    assert "removalCondition" in help_text
    assert "Problem/Gap Closure Loop" in help_text
    assert "resolved|reduced|blocked" in help_text
    assert "one evidence bundle per finding" in help_text
    assert "A code diff or \"tests passed\" claim is" in help_text
    assert "evidence/screenshots and" in help_text
    assert ".xflow/publish/issues" in help_text
    assert "subtask-001" in help_text
    assert "subtask evidence/ directory" in help_text

    readme_text = (OPS_ROOT / "README.md").read_text(encoding="utf-8")
    assert "AI Call Recipes" in readme_text
    assert "Human Approval Is Non-Delegable" in readme_text
    assert "Restricted unattended issue" not in readme_text
    assert "explicitly authorized an unattended issue/comment" not in readme_text
    assert 'devctl issue create "Title" --body-file issue.md --no-local-review' not in readme_text
    assert "devctl unattended enable --issue <id|draft> --confirm XFLOW_HUMAN_UNATTENDED_ALL" in readme_text
    assert "devctl unattended status" in readme_text
    assert "devctl unattended disable" in readme_text
    assert "[UNATTENDED] Human approval gate bypassed for current task <id>." in readme_text
    assert "--no-local-review alone is invalid" in readme_text
    for exclusion in ("force push", "history rewrite", "destructive deletion", "secret or permission changes"):
        assert exclusion in readme_text
    assert "AI must never satisfy a human gate" in readme_text
    assert "Only the human reviewer may" in readme_text
    assert "Issue/comment image attachments are disabled" in readme_text
    assert "Inline `--attach-file` and `--upload-attachments` are disabled" in readme_text
    assert "GitHub release assets" in readme_text
    assert "issue image store" in readme_text
    assert "Aliyun OSS attachment backend" in readme_text
    assert "ALIYUN_OSS_ACCESS_KEY_SECRET" in readme_text
    assert "must not be written to attachment manifests" in readme_text
    assert "devctl git push --issue" in readme_text
    assert "state backfill commit" in readme_text
    assert "Normal Git, Issue, Attachment, Approval, Rules, and Migration commands route" in readme_text
    assert "App commands route" not in readme_text
    assert "repository-local `devctl.ps1`" in readme_text
    assert "do not run bare `bash`, Git Bash, or WSL for normal XFlow validation" in readme_text
    assert "devctl check subtask --issue" in readme_text
    assert "devctl check issue-evidence --issue" in readme_text
    assert "devctl check gap-analysis --issue" in readme_text
    assert "devctl check resolution-report --issue" in readme_text
    assert "devctl check dependencies --issue IK152D" in readme_text
    assert "devctl check commit-msg --file .xflow/local/commit-message.txt --issue IK152D" in readme_text
    assert "type(scope): 中文核心摘要[#Issue编号]" in readme_text
    assert "active dependencies warn but do not block local development" in readme_text
    assert "removalCondition" in readme_text
    assert "Problem/Gap Closure Loop" in readme_text
    assert "resolved|reduced|blocked" in readme_text
    assert "numbered evidence bundle" in readme_text
    assert "not completion evidence" in readme_text
    assert "evidence/screenshots/" in readme_text
    assert ".xflow/publish/issues" in readme_text
    assert "Subtask evidence must stay in the repository" in readme_text
    assert "subtask `evidence/` directory" in readme_text


def test_capability_contract_guidance_is_visible() -> None:
    expected = (
        "devctl task activate --issue IK3RR6",
        "devctl task status",
        "devctl task list",
        "devctl task migrate-current",
        "devctl check classification --issue IK3RR6",
        "devctl contract lint --file docs/requirements/example/contract.yaml",
        "devctl contract accept --issue IK3RR6 --file docs/requirements/example/contract.yaml --objects <id,id,...>",
        "devctl contract diff --old <old.yaml> --new <new.yaml>",
        "devctl trace check --issue IK3RR6 --contract <contract.yaml> --matrix <traceability-matrix.yaml>",
        "devctl migrate issue-workspace --mode tracked --check",
        "devctl migrate issue-workspace --mode local --check",
        ".xflow/issues/ is tracked by default",
        "contract acceptance never supports unattended mode",
        "approval history",
        "parallel worktrees",
        "devctl hook task-status",
    )
    for path in (OPS_ROOT / "help.txt", OPS_ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        for anchor in expected:
            assert anchor in text, (path, anchor)
        assert "Do not use `devctl hook task-status` as a normal user or AI command." in text


class RecordingApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    release_created: bool = False
    issue_create_payload: str = '{"number":42,"html_url":"https://github.test/issue/42"}'
    pull_request_payload: str | None = None

    def log_message(self, format: str, *args: object) -> None:
        return

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {key: values[-1] for key, values in parse_qs(raw).items()}

    def send_json(self, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_status(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.requests.append({"method": "GET", "path": parsed.path, "query": query})
        if parsed.path.endswith("/releases/tags/xflow-attachments"):
            if type(self).release_created:
                self.send_json(
                    '{"id":77,"tag_name":"xflow-attachments",'
                    f'"upload_url":"http://127.0.0.1:{self.server.server_port}/repos/Linkk2000/paper-demo/releases/77/assets{{?name,label}}"'  # type: ignore[attr-defined]
                    "}"
                )
            else:
                self.send_status(404)
        elif parsed.path.endswith("/issues/IJZT85"):
            self.send_json('{"number":"IJZT85","state":"open","title":"Gitee Issue","body":"body","html_url":"https://gitee.test/issue/IJZT85"}')
        elif parsed.path.endswith("/issues"):
            self.send_json('[{"number":"IJZT85","state":"open","title":"Gitee Issue","body":"body","html_url":"https://gitee.test/issue/IJZT85"}]')
        elif parsed.path.endswith("/pulls/7"):
            self.send_json('{"number":"7","state":"open","title":"Gitee PR","html_url":"https://gitee.test/pulls/7"}')
        elif "/pulls/" in parsed.path and type(self).pull_request_payload is not None:
            self.send_json(type(self).pull_request_payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if parsed.path.endswith("/releases"):
            payload = self.read_body().decode("utf-8")
            type(self).release_created = True
            self.requests.append({"method": "POST", "path": parsed.path, "json": payload})
            self.send_json(
                '{"id":77,"tag_name":"xflow-attachments",'
                f'"upload_url":"http://127.0.0.1:{self.server.server_port}/repos/Linkk2000/paper-demo/releases/77/assets{{?name,label}}"'  # type: ignore[attr-defined]
                "}"
            )
        elif parsed.path.endswith("/releases/77/assets"):
            body = self.read_body()
            self.requests.append(
                {
                    "method": "POST",
                    "path": parsed.path,
                    "query": query,
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": body,
                }
            )
            name = query.get("name", "asset.png")
            self.send_json(
                '{"id":88,"name":"%s","browser_download_url":"https://github.com/Linkk2000/paper-demo/releases/download/xflow-attachments/%s"}'
                % (name, name)
            )
        elif parsed.path.endswith("/Linkk2000/issues"):
            form = self.read_form()
            self.requests.append({"method": "POST", "path": parsed.path, "form": form})
            self.send_json('{"number":"IJZT85","html_url":"https://gitee.test/issue/IJZT85"}')
        elif parsed.path.endswith("/issues/IJZT85/comments"):
            form = self.read_form()
            self.requests.append({"method": "POST", "path": parsed.path, "form": form})
            self.send_json('{"id":"99","body":"comment"}')
        elif parsed.path.endswith("/pulls"):
            payload = self.read_body().decode("utf-8")
            if "application/json" in self.headers.get("Content-Type", ""):
                self.requests.append({"method": "POST", "path": parsed.path, "json": payload})
                self.send_json('{"number":42,"html_url":"https://github.test/pulls/42"}')
            else:
                form = {key: values[-1] for key, values in parse_qs(payload).items()}
                self.requests.append({"method": "POST", "path": parsed.path, "form": form})
                self.send_json('{"number":"7","html_url":"https://gitee.test/pulls/7"}')
        elif parsed.path.endswith("/issues"):
            payload = self.read_body().decode("utf-8")
            self.requests.append({"method": "POST", "path": parsed.path, "json": payload})
            self.send_json(type(self).issue_create_payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        payload = self.read_body().decode("utf-8")
        self.requests.append({"method": "PUT", "path": parsed.path, "json": payload})
        if parsed.path.endswith("/pulls/42/merge"):
            self.send_json('{"sha":"abc123","merged":true,"message":"Pull Request successfully merged"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        form = self.read_form()
        self.requests.append({"method": "PATCH", "path": parsed.path, "form": form})
        if parsed.path.endswith("/Linkk2000/issues/IJZT85"):
            self.send_json('{"number":"IJZT85","state":"closed","title":"Gitee Issue"}')
        else:
            self.send_response(404)
            self.end_headers()


class RecordingApiServer:
    def __init__(
        self,
        issue_create_payload: str | None = None,
        pull_request_payload: str | None = None,
    ) -> None:
        self.issue_create_payload = issue_create_payload
        self.pull_request_payload = pull_request_payload

    def __enter__(self) -> "RecordingApiServer":
        RecordingApiHandler.requests = []
        RecordingApiHandler.release_created = False
        RecordingApiHandler.issue_create_payload = (
            self.issue_create_payload
            if self.issue_create_payload is not None
            else '{"number":42,"html_url":"https://github.test/issue/42"}'
        )
        RecordingApiHandler.pull_request_payload = self.pull_request_payload
        self.server = HTTPServer(("127.0.0.1", 0), RecordingApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def requests(self) -> list[dict[str, object]]:
        return RecordingApiHandler.requests


class RecordingOssHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.requests.append(
            {
                "method": "PUT",
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()


class RecordingOssServer:
    def __enter__(self) -> "RecordingOssServer":
        RecordingOssHandler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), RecordingOssHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def requests(self) -> list[dict[str, object]]:
        return RecordingOssHandler.requests


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        git(repo, "remote", "add", "origin", "git@gitee.com:Linkk2000/paper-demo.git")
        test_unattended_state_lifecycle(repo / "unattended-state")
        test_unattended_cli_lifecycle(repo / "unattended-cli")
        test_completed_task_invalidates_unattended_state(repo / "unattended-completed-task")
        test_no_local_review_requires_active_state(repo / "unattended-compatibility")
        test_successful_issue_close_invalidates_unattended_state(repo / "unattended-issue-close")
        test_inline_attachment_upload_is_rejected_before_gate_and_provider(repo / "inline-attachment-gate")
        test_remote_gate_matrix(repo / "unattended-gate-matrix")
        test_pr_merge_requires_recorded_and_remote_identity(repo / "pr-merge-identity")
        test_issue_identity_sources_must_all_match(repo / "issue-identity-consistency")
        test_git_mechanical_checks_run_before_unattended_gate(repo / "gate-ordering")
        test_mr_rejects_branch_behind_remote_base(repo / "mr-behind-base")
        test_draft_state_migrates_only_after_confirmed_issue_creation(repo / "unattended-draft-migration")
        test_env_loading_policy(repo)
        test_python_core_rejects_inline_remote_bodies(repo)
        test_issue_identifiers_are_portable(repo)
        test_dependency_parser(repo)
        test_resolution_report_dependency_closure(repo / "dependency-closure")
        test_resolution_report_traceability_closure(repo / "traceability-closure")
        test_commit_message_validator()
        test_commit_message_cli(repo / "commit-message-cli")
        test_commit_message_generator(repo / "commit-message-generator")
        test_pr_backfill_commit_message_without_push(repo / "pr-backfill-message")
        test_pr_backfill_replays_real_commit_and_push_windows(repo / "pr-backfill-replay")
        test_python_core_git_and_app_commands(repo / "core-routing")
        test_git_done_requires_exact_human_cleanup_approval(repo / "git-done-exact-approval")
        test_git_task_metadata_is_scoped_to_each_worktree(repo / "worktree-metadata")
        test_git_push_and_mr_are_separate_with_state_backfill(repo / "push-mr-state")
        test_ai_call_guidance_is_visible(repo)
        test_capability_contract_guidance_is_visible()

        issue_file = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
        write(
            issue_file,
            """<!-- xflow: issue-draft -->

## Background
Need reviewable issue creation.

## Problem
Remote writes need human review.

## Goal
Create a remote issue after approval.

## Scope
- Includes: approval gate.

## Acceptance Criteria
- [ ] Remote write is blocked until approval.

## Verification Plan
- python tests/python-core.py
""",
        )

        mr_file = repo / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
        write(
            mr_file,
            """<!-- xflow: mr-draft -->

Closes #1

## Summary
- Add generic Python core checks.

## Test Plan
- python tests/python-core.py

## Risk
- Low.

## Review Request
- Please review local artifacts before remote write.
""",
        )

        run_devctl(repo, "preflight")

        env_file = repo / ".xflow" / "local" / "env.local"
        write(env_file, "GITHUB_TOKEN=secret-token-value\nGITEE_TOKEN=other-secret\n")
        preflight = run_devctl_with_env(repo, {"XFLOW_ENV_FILE": str(env_file)}, "preflight")
        assert f"env_file: {env_file.resolve()}" in preflight.stdout
        assert "GITHUB_TOKEN=SET" in preflight.stdout
        assert "GITEE_TOKEN=SET" in preflight.stdout
        assert "secret-token-value" not in preflight.stdout
        assert "other-secret" not in preflight.stdout

        with RecordingApiServer() as server:
            gitee_env = {"GITEE_API_BASE": server.base_url, "GITEE_TOKEN": "gitee-token"}
            created = create_issue(repo, "Gitee title", "Gitee body", "bug,docs", gitee_env)
            assert created.number == "IJZT85"
            rows = list_issues(repo, "open", 20, gitee_env)
            assert rows[0]["number"] == "IJZT85"
            shown = show_issue(repo, "IJZT85", gitee_env)
            assert shown["title"] == "Gitee Issue"
            comment = comment_issue(repo, "IJZT85", "Gitee comment", gitee_env)
            assert comment["id"] == "99"
            closed = close_issue(repo, "IJZT85", gitee_env)
            assert closed["state"] == "closed"
            pr = create_pull_request(repo, "Gitee PR", "PR body", "feature/demo", "main", gitee_env)
            assert pr.number == "7"
            fetched_pr = get_pull_request(repo, "7", gitee_env)
            assert fetched_pr["title"] == "Gitee PR"

            assert server.requests[0]["method"] == "POST"
            assert server.requests[0]["path"] == "/repos/Linkk2000/issues"
            assert server.requests[0]["form"]["repo"] == "paper-demo"
            assert server.requests[0]["form"]["access_token"] == "gitee-token"
            assert server.requests[3]["path"] == "/repos/Linkk2000/paper-demo/issues/IJZT85/comments"
            assert server.requests[4]["method"] == "PATCH"
            assert server.requests[4]["path"] == "/repos/Linkk2000/issues/IJZT85"
            assert server.requests[4]["form"]["repo"] == "paper-demo"
            assert server.requests[4]["form"]["state"] == "closed"
            assert server.requests[5]["path"] == "/repos/Linkk2000/paper-demo/pulls"
            assert server.requests[5]["form"]["head"] == "feature/demo"
            assert server.requests[5]["form"]["base"] == "main"

        run_devctl(repo, "check", "issue-draft", "--file", str(issue_file))
        run_devctl(repo, "check", "mr-draft", "--issue", "1")

        current_task = repo / ".xflow" / "current-task.md"
        write(
            current_task,
            """# XFlow Current Task

Issue: 1
State: S6_PREPARE_COMMIT_AND_MR_DRAFT

## Allowed Actions
- Draft MR body.

## Forbidden Actions
- Create PR before local human approval.
""",
        )
        bindings = resolve_bindings(repo)
        authority_root = task_authority_file(repo, bindings.worktree, "1").parent.parent
        for candidate in authority_root.glob("issue-*/authority.json"):
            candidate.unlink()
        run_devctl(repo, "check", "current-task", "--issue", "1")
        git(repo, "config", "--local", "devctl.pr", "9")
        run_devctl(repo, "check", "current-task", "--issue", "1", expect=1)
        current_task.write_text(
            current_task.read_text(encoding="utf-8").replace(
                "S6_PREPARE_COMMIT_AND_MR_DRAFT", "S9_REMOTE_REVIEW_AND_CI"
            ),
            encoding="utf-8",
        )
        run_devctl(repo, "check", "current-task", "--issue", "1")
        suggestion = write_pr_state_update_suggestion(repo, "1", "9", "https://example.test/pull/9")
        assert suggestion.is_file()
        expected_suggestion = (
            "# State Update Suggestion\n\n"
            "Issue: 1\n"
            "PR: 9\n"
            "PR URL: https://example.test/pull/9\n"
            "Suggested State: S9_REMOTE_REVIEW_AND_CI\n\n"
            "## Suggested Local Update\n"
            "- Update `.xflow/current-task.md` to `State: S9_REMOTE_REVIEW_AND_CI`.\n"
            "- Record the PR number and URL in the task evidence if needed.\n"
            "- Do not create a follow-up PR only to commit this local state note after the PR has already been merged.\n"
        ).encode("utf-8")
        assert suggestion.read_bytes() == expected_suggestion

        walkthrough = repo / ".xflow" / "issues" / "issue-1" / "walkthrough.md"
        write(walkthrough, "# Walkthrough\n\nIssue evidence source.\n")
        subtask = repo / ".xflow" / "issues" / "issue-1" / "subtask-001"
        write(subtask / "evidence" / "screenshot.png", "local image evidence")
        write(
            subtask / "README.md",
            """# Subtask 001

## Source
- walkthrough.md

## Purpose
Split a large issue into a focused local task.

## Implementation Plan
- [ ] Implement a focused slice.

## Evidence
- [screenshot](evidence/screenshot.png)

## AI Review Checkpoints
- [ ] Confirm local evidence stays in the repository.

## Human Review Checkpoints
- [ ] Review conclusion and evidence.

## Conclusion
success: implemented and verified locally.
""",
        )
        run_devctl(repo, "check", "subtask", "--issue", "1")

        bom_subtask = repo / ".xflow" / "issues" / "issue-1" / "subtask-012"
        write(bom_subtask / "evidence" / "screenshot.png", "local image evidence")
        bom_readme = "## Source" + (subtask / "README.md").read_text(encoding="utf-8").split("## Source", 1)[1]
        (bom_subtask / "README.md").write_bytes(b"\xef\xbb\xbf" + bom_readme.encode("utf-8"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(bom_subtask))

        zero_number = repo / ".xflow" / "issues" / "issue-1" / "subtask-000"
        write(zero_number / "README.md", (subtask / "README.md").read_text(encoding="utf-8"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(zero_number), expect=1)

        bad_number = repo / ".xflow" / "issues" / "issue-1" / "subtask-1"
        write(bad_number / "README.md", (subtask / "README.md").read_text(encoding="utf-8"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(bad_number), expect=1)

        missing_readme = repo / ".xflow" / "issues" / "issue-1" / "subtask-002"
        missing_readme.mkdir(parents=True)
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(missing_readme), expect=1)

        missing_section = repo / ".xflow" / "issues" / "issue-1" / "subtask-003"
        write(missing_section / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("## Purpose\n", ""))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(missing_section), expect=1)

        empty_purpose = repo / ".xflow" / "issues" / "issue-1" / "subtask-008"
        write(empty_purpose / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("Split a large issue into a focused local task.", ""))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(empty_purpose), expect=1)

        empty_evidence = repo / ".xflow" / "issues" / "issue-1" / "subtask-009"
        write(empty_evidence / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("- [screenshot](evidence/screenshot.png)", ""))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(empty_evidence), expect=1)

        outside_source = repo / ".xflow" / "issues" / "issue-1" / "subtask-004"
        write(outside_source / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("walkthrough.md", "../issue-draft/issue-draft.md"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(outside_source), expect=1)

        subtask_source = repo / ".xflow" / "issues" / "issue-1" / "subtask-010"
        write(subtask_source / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("walkthrough.md", "subtask-001/README.md"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(subtask_source), expect=1)

        remote_evidence = repo / ".xflow" / "issues" / "issue-1" / "subtask-005"
        write(remote_evidence / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("evidence/screenshot.png", "https://img.example.test/xflow/evidence.png"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(remote_evidence), expect=1)

        cos_evidence = repo / ".xflow" / "issues" / "issue-1" / "subtask-006"
        write(cos_evidence / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("evidence/screenshot.png", "cos://bucket/evidence.png"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(cos_evidence), expect=1)

        outside_evidence = repo / ".xflow" / "issues" / "issue-1" / "subtask-007"
        write(outside_evidence / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("evidence/screenshot.png", "../walkthrough.md"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(outside_evidence), expect=1)

        root_evidence = repo / ".xflow" / "issues" / "issue-1" / "subtask-011"
        write(root_evidence / "notes.txt", "root evidence is not allowed")
        write(root_evidence / "README.md", (subtask / "README.md").read_text(encoding="utf-8").replace("evidence/screenshot.png", "notes.txt"))
        run_devctl(repo, "check", "subtask", "--issue", "1", "--path", str(root_evidence), expect=1)

        gap = repo / ".xflow" / "issues" / "issue-1" / "gap-analysis.md"
        write(gap.parent / "evidence" / "gap-note.txt", "gap evidence")
        write(
            gap,
            """# Problem/Gap Analysis

## User Original Statement
The user described a workflow gap.

## Clarified Problem Or Gap
AI may implement before the gap is recognized.

## Gap Analysis
- Missing analysis gate.

## Evidence
- [gap note](evidence/gap-note.txt)

## Evidence-Backed Findings

### Finding F-001: Analysis gate is missing

#### Finding Type
non-ui

#### Observation
The workflow permits implementation without a recorded human recognition.

#### User Impact
The user cannot review the proposed scope before changes begin.

#### Evidence
- [gap note](evidence/gap-note.txt)

#### Analysis
The existing workflow has no mechanical gate for this transition.

#### Proposed Change
Require a recognized gap analysis before implementation.

#### Acceptance
- [ ] A gap analysis without the required evidence bundle is rejected.

#### Human Review
- [ ] Confirm that the observed workflow gap and proposed gate are correct.

## Scope Boundaries
- Includes: local XFlow checks.
- Excludes: remote publishing.

## Proposed Modification Plan
- [ ] Add check commands.

## Acceptance Criteria
- [ ] Gap analysis must pass a mechanical check.

## Human Recognition
Recognized: yes
Reviewer: Test User
""",
        )
        run_devctl(repo, "check", "gap-analysis", "--issue", "1")

        incomplete_finding = gap.with_name("gap-analysis-incomplete-finding.md")
        write(
            incomplete_finding,
            gap.read_text(encoding="utf-8").replace(
                "#### Human Review\n- [ ] Confirm that the observed workflow gap and proposed gate are correct.\n\n",
                "",
            ),
        )
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(incomplete_finding), expect=1)

        root_gap_evidence = gap.with_name("gap-analysis-root-evidence.md")
        write(gap.parent / "root-gap-note.txt", "evidence outside evidence directory")
        write(
            root_gap_evidence,
            gap.read_text(encoding="utf-8").replace("evidence/gap-note.txt", "root-gap-note.txt"),
        )
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(root_gap_evidence), expect=1)

        write(gap.parent / "evidence" / "screenshots" / "f-001-before.png", "ui screenshot")
        write(gap.parent / "evidence" / "dom" / "f-001-before.html", "<main>observed UI</main>")
        ui_evidence = "- [screenshot](evidence/screenshots/f-001-before.png)\n- [DOM](evidence/dom/f-001-before.html)"
        ui_gap = gap.with_name("gap-analysis-ui.md")
        write(
            ui_gap,
            gap.read_text(encoding="utf-8")
            .replace("non-ui", "ui")
            .replace("- [gap note](evidence/gap-note.txt)", ui_evidence),
        )
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(ui_gap))

        ui_gap_without_dom = gap.with_name("gap-analysis-ui-without-dom.md")
        write(ui_gap_without_dom, ui_gap.read_text(encoding="utf-8").replace("\n- [DOM](evidence/dom/f-001-before.html)", ""))
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(ui_gap_without_dom), expect=1)

        unrecognized_gap = gap.with_name("gap-analysis-unrecognized.md")
        write(unrecognized_gap, gap.read_text(encoding="utf-8").replace("Recognized: yes", "Recognized: no"))
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(unrecognized_gap), expect=1)

        missing_gap_section = gap.with_name("gap-analysis-missing-section.md")
        write(missing_gap_section, gap.read_text(encoding="utf-8").replace("## Acceptance Criteria\n- [ ] Gap analysis must pass a mechanical check.\n\n", ""))
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(missing_gap_section), expect=1)

        remote_gap_evidence = gap.with_name("gap-analysis-remote-evidence.md")
        write(remote_gap_evidence, gap.read_text(encoding="utf-8").replace("evidence/gap-note.txt", "https://example.test/gap-note.txt"))
        run_devctl(repo, "check", "gap-analysis", "--issue", "1", "--file", str(remote_gap_evidence), expect=1)

        resolution = repo / ".xflow" / "issues" / "issue-1" / "resolution-report.md"
        write(resolution.parent / "evidence" / "resolution-note.txt", "resolution evidence")
        write(
            resolution,
            """# Resolution Report

## Source Problem Or Gap
- gap-analysis.md

## Actual Changes
- Added gap closure checks.

## Evidence Index
- [resolution note](evidence/resolution-note.txt)

## Completion Verification

### Criterion C-001: Gap analysis check rejects incomplete evidence

#### Verification Type
non-ui

#### Expected Result
The command rejects a gap analysis that omits a required finding field.

#### Evidence
- [resolution note](evidence/resolution-note.txt)

#### Actual Result
The focused verification command returned the expected rejection.

#### Human Review
- [ ] Confirm this evidence supports the reported closure.

## Closure Conclusion
Conclusion: resolved
Reason: The gap check now exists.

## AI Self-Review Result
- [x] Gap analysis is checked.
- [x] Resolution report is checked.

## Remaining Risks
- none

## Human Review Request
- Please review the evidence and conclusion.
""",
        )
        run_devctl(repo, "check", "resolution-report", "--issue", "1")

        incomplete_verification = resolution.with_name("resolution-report-incomplete-verification.md")
        write(
            incomplete_verification,
            resolution.read_text(encoding="utf-8").replace(
                "#### Human Review\n- [ ] Confirm this evidence supports the reported closure.\n\n",
                "",
            ),
        )
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(incomplete_verification), expect=1)

        ui_resolution = resolution.with_name("resolution-report-ui.md")
        write(
            ui_resolution,
            resolution.read_text(encoding="utf-8")
            .replace("non-ui", "ui")
            .replace("- [resolution note](evidence/resolution-note.txt)", ui_evidence),
        )
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(ui_resolution))

        ui_resolution_without_dom = resolution.with_name("resolution-report-ui-without-dom.md")
        write(
            ui_resolution_without_dom,
            ui_resolution.read_text(encoding="utf-8").replace("\n- [DOM](evidence/dom/f-001-before.html)", ""),
        )
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(ui_resolution_without_dom), expect=1)

        reduced_resolution = resolution.with_name("resolution-report-reduced.md")
        write(reduced_resolution, resolution.read_text(encoding="utf-8").replace("Conclusion: resolved\nReason: The gap check now exists.", "Conclusion: reduced\nReason: The main gap is smaller but follow-up remains."))
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(reduced_resolution))

        blocked_resolution = resolution.with_name("resolution-report-blocked.md")
        write(blocked_resolution, resolution.read_text(encoding="utf-8").replace("Conclusion: resolved\nReason: The gap check now exists.", "Conclusion: blocked\nReason: Human must choose the rollout path.").replace("- [x] Resolution report is checked.", "- [ ] Waiting for human rollout choice."))
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(blocked_resolution))

        unchecked_resolution = resolution.with_name("resolution-report-unchecked.md")
        write(unchecked_resolution, resolution.read_text(encoding="utf-8").replace("- [x] Resolution report is checked.", "- [ ] Resolution report is checked."))
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(unchecked_resolution), expect=1)

        bad_conclusion = resolution.with_name("resolution-report-bad-conclusion.md")
        write(bad_conclusion, resolution.read_text(encoding="utf-8").replace("Conclusion: resolved\nReason: The gap check now exists.", "Conclusion: done\nReason: Looks good."))
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(bad_conclusion), expect=1)

        remote_resolution_evidence = resolution.with_name("resolution-report-remote-evidence.md")
        write(remote_resolution_evidence, resolution.read_text(encoding="utf-8").replace("evidence/resolution-note.txt", "oss://bucket/resolution-note.txt"))
        run_devctl(repo, "check", "resolution-report", "--issue", "1", "--file", str(remote_resolution_evidence), expect=1)

        simple_issue = repo / ".xflow" / "issues" / "issue-2"
        write(simple_issue / "evidence" / "note.txt", "local issue evidence")
        write(simple_issue / "walkthrough.md", "# Walkthrough\n\n- [local note](evidence/note.txt)\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2")

        write(simple_issue / "walkthrough.md", "# Walkthrough\n\nhttps://example.test/reference\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2")

        write(simple_issue / "walkthrough.md", "# Walkthrough\n\n![remote](https://img.example.test/xflow/issues/issue-2/attachments/att-001.png)\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2", expect=1)

        write(simple_issue / "walkthrough.md", "# Walkthrough\n\noss://bucket/xflow/issues/issue-2/attachments/att-001.png\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2", expect=1)

        write(simple_issue / "walkthrough.md", "# Walkthrough\n\nbucket.r2.cloudflarestorage.com/evidence.json\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2", expect=1)

        write(simple_issue / "attachments" / "manifest.json", '{"items":[{"publishedUrl":"https://img.example.test/xflow/issues/issue-2/attachments/att-001.png"}]}\n')
        run_devctl(repo, "check", "issue-evidence", "--issue", "2", expect=1)

        write(simple_issue / "walkthrough.md", "# Walkthrough\n\n- [local note](evidence/note.txt)\n")
        write(simple_issue / "attachments" / "manifest.json", '{"items":[{"publishedUrl":null}]}\n')
        publish_body = repo / ".xflow" / "publish" / "issues" / "issue-2" / "issue.final.md"
        write(publish_body, "# Remote Body\n\n![remote](https://img.example.test/xflow/issues/issue-2/attachments/att-001.png)\n")
        run_devctl(repo, "check", "issue-evidence", "--issue", "2", "--publish-root", str(publish_body.parent))

        bad_issue = issue_file.with_name("bad-issue.md")
        shutil.copyfile(issue_file, bad_issue)
        bad_issue.write_text("# Issue Draft\n" + bad_issue.read_text(encoding="utf-8"), encoding="utf-8")
        run_devctl(repo, "check", "issue-draft", "--file", str(bad_issue), expect=1)

        run_devctl(repo, "approval", "prepare", "--issue", "draft", "--action", "issue-create", "--file", str(issue_file))
        approval = repo / ".xflow" / "issues" / "issue-draft" / "approvals" / "local-review.md"
        text = approval.read_text(encoding="utf-8")
        digest = hashlib.sha256(issue_file.read_bytes()).hexdigest()
        assert "Reviewer: Test User (test@example.com)" in text
        assert f"Approved SHA256: {digest}" in text
        assert "## Human Gate" in text
        assert "Prepared by AI or tooling does not mean approved." in text
        assert "Only the human reviewer may change Approved: no to Approved: yes." in text
        assert "If this file was approved by the AI, the approval is invalid." in text
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(issue_file), expect=1)
        approval.write_text(text.replace("Approved: no", "Approved: yes").replace(digest, digest.upper()), encoding="utf-8")
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(issue_file))
        write(current_task, current_task_text("draft"))
        run_devctl(repo, "issue", "create", "Review gate", "--body-file", str(issue_file))

        pasted_image = repo / "pasted-image.png"
        pasted_image.write_bytes(b"\x89PNG\r\n\x1a\nxflow-test-image")
        added = run_devctl(repo, "attachment", "add", "--issue", "draft", "--file", str(pasted_image), "--as", "image")
        assert "xflow-attachment://att-001" in added.stdout
        manifest = repo / ".xflow" / "issues" / "issue-draft" / "attachments" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["issue"] == "draft"
        assert data["items"][0]["id"] == "att-001"
        assert data["items"][0]["mime"] == "image/png"
        assert data["items"][0]["markdown"] == "![pasted-image.png](xflow-attachment://att-001)"

        attachment_body = repo / ".xflow" / "issues" / "issue-draft" / "issue-with-attachment.md"
        write_text_lf(
            attachment_body,
            issue_file.read_text(encoding="utf-8")
            + "\n## Attachments\n- ![pasted-image.png](xflow-attachment://att-001)\n",
        )
        run_devctl(repo, "attachment", "check", "--issue", "draft", "--manifest", str(manifest), "--body-file", str(attachment_body))
        run_devctl(repo, "issue", "create", "Attachment gate", "--body-file", str(attachment_body), "--attachments", str(manifest), expect=1)
        github_publish_result = run_devctl(
            repo,
            "attachment",
            "publish",
            "--issue",
            "draft",
            "--manifest",
            str(manifest),
            "--backend",
            "github",
            expect=1,
        )
        assert "issue/comment image attachments are disabled" in github_publish_result.stderr
        run_devctl(repo, "attachment", "publish", "--issue", "draft", "--manifest", str(manifest), "--url", "att-001=https://example.test/pasted-image.png")
        published_manifest = repo / ".xflow" / "publish" / "issues" / "issue-draft" / "attachments" / "manifest.json"
        assert published_manifest.is_file()
        assert "publishedUrl" in published_manifest.read_text(encoding="utf-8")
        assert '"publishedUrl": null' in manifest.read_text(encoding="utf-8")
        final_body = repo / ".xflow" / "publish" / "issues" / "issue-draft" / "issue-with-attachment.final.md"
        run_devctl(repo, "attachment", "render", "--issue", "draft", "--manifest", str(published_manifest), "--input", str(attachment_body), "--output", str(final_body))
        final_text = final_body.read_text(encoding="utf-8")
        assert "xflow-attachment://" not in final_text
        assert "https://example.test/pasted-image.png" in final_text
        run_devctl(repo, "attachment", "check", "--issue", "draft", "--manifest", str(published_manifest), "--body-file", str(final_body), "--final")
        issue_dir_final_body = repo / ".xflow" / "issues" / "issue-draft" / "issue-with-attachment.final.md"
        run_devctl(repo, "attachment", "render", "--issue", "draft", "--manifest", str(published_manifest), "--input", str(attachment_body), "--output", str(issue_dir_final_body), expect=1)

        local_path_body = final_body.with_name("issue-with-local-path.md")
        write_text_lf(local_path_body, final_text + "\n![bad](C:\\temp\\bad.png)\n")
        run_devctl(repo, "attachment", "check", "--issue", "draft", "--manifest", str(published_manifest), "--body-file", str(local_path_body), "--final", expect=1)

        run_devctl(
            repo,
            "approval",
            "prepare",
            "--issue",
            "draft",
            "--action",
            "issue-create",
            "--file",
            str(final_body),
            "--attachments",
            str(published_manifest),
            "--force",
        )
        approval_text = approval.read_text(encoding="utf-8")
        manifest_digest = hashlib.sha256(published_manifest.read_bytes()).hexdigest()
        assert f"Attachment Manifest SHA256: {manifest_digest}" in approval_text
        approval.write_text(approval_text.replace("Approved: no", "Approved: yes"), encoding="utf-8")
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(final_body), "--action", "issue-create", "--attachments", str(published_manifest))
        run_devctl(repo, "issue", "create", "Attachment gate", "--body-file", str(final_body), "--attachments", str(published_manifest), expect=1)
        comment_body = repo / ".xflow" / "issues" / "issue-1" / "comment-with-image.md"
        write(
            comment_body,
            """<!-- xflow: issue-comment -->

Image evidence is attached locally.
""",
        )
        write(current_task, current_task_text("1").replace("G5_APPROVE_MR_CREATE", "S9_REMOTE_REVIEW_AND_CI"))
        run_devctl(repo, "attachment", "add", "--issue", "1", "--file", str(pasted_image), "--as", "image")
        comment_manifest = repo / ".xflow" / "issues" / "issue-1" / "attachments" / "manifest.json"
        comment_result = run_devctl(
            repo,
            "issue",
            "comment",
            "1",
            "--body-file",
            str(comment_body),
            "--attachments",
            str(comment_manifest),
            expect=1,
        )
        assert "issue/comment image attachments are disabled" in comment_result.stderr

        oss_body = repo / ".xflow" / "issues" / "issue-draft" / "issue-with-oss-image.md"
        write(
            oss_body,
            issue_file.read_text(encoding="utf-8")
            + "\n## Attachments\n- ![pasted-image.png](xflow-attachment://att-001)\n",
        )
        with RecordingOssServer() as oss_server:
            oss_env = {
                "ALIYUN_OSS_ACCESS_KEY_ID": "test-access-key-id",
                "ALIYUN_OSS_ACCESS_KEY_SECRET": "test-access-key-secret",
                "ALIYUN_OSS_BUCKET": "pictbed",
                "ALIYUN_OSS_REGION": "oss-cn-chengdu",
                "ALIYUN_OSS_ENDPOINT": oss_server.base_url,
                "ALIYUN_OSS_PUBLIC_BASE_URL": "https://img.example.test",
                "ALIYUN_OSS_PREFIX": "xflow/issues",
            }
            run_devctl_with_env(
                repo,
                oss_env,
                "attachment",
                "publish",
                "--issue",
                "draft",
                "--manifest",
                str(manifest),
                "--backend",
                "aliyun-oss",
            )
            assert len(oss_server.requests) == 1
            oss_request = oss_server.requests[0]
            assert oss_request["method"] == "PUT"
            assert str(oss_request["path"]).startswith("/pictbed/xflow/issues/issue-draft/attachments/")
            assert str(oss_request["path"]).endswith("pasted-image.png")
            assert str(oss_request["authorization"]).startswith("OSS test-access-key-id:")
            assert "test-access-key-secret" not in str(oss_request["authorization"])
            assert oss_request["content_type"] == "image/png"
            assert oss_request["body"] == b"\x89PNG\r\n\x1a\nxflow-test-image"

        oss_published_manifest = repo / ".xflow" / "publish" / "issues" / "issue-draft" / "attachments" / "manifest.json"
        oss_manifest_data = json.loads(oss_published_manifest.read_text(encoding="utf-8"))
        oss_item = oss_manifest_data["items"][0]
        assert oss_item["backend"] == "aliyun-oss"
        assert oss_item["provider"] == "aliyun-oss"
        assert oss_item["bucket"] == "pictbed"
        assert oss_item["objectKey"].startswith("xflow/issues/issue-draft/attachments/")
        assert oss_item["publishedUrl"].startswith("https://img.example.test/xflow/issues/issue-draft/attachments/")
        manifest_text = oss_published_manifest.read_text(encoding="utf-8")
        assert "test-access-key-id" not in manifest_text
        assert "test-access-key-secret" not in manifest_text
        assert '"publishedUrl": null' in manifest.read_text(encoding="utf-8")
        run_devctl(repo, "check", "issue-evidence", "--issue", "draft")

        oss_final_body = repo / ".xflow" / "publish" / "issues" / "issue-draft" / "issue-with-oss-image.final.md"
        run_devctl(repo, "attachment", "render", "--issue", "draft", "--manifest", str(oss_published_manifest), "--input", str(oss_body), "--output", str(oss_final_body))
        oss_final_text = oss_final_body.read_text(encoding="utf-8")
        assert "xflow-attachment://" not in oss_final_text
        assert "https://img.example.test/xflow/issues/issue-draft/attachments/" in oss_final_text

        run_devctl(
            repo,
            "approval",
            "prepare",
            "--issue",
            "draft",
            "--action",
            "issue-create",
            "--file",
            str(oss_final_body),
            "--attachments",
            str(oss_published_manifest),
            "--force",
        )
        approval_text = approval.read_text(encoding="utf-8")
        approval.write_text(approval_text.replace("Approved: no", "Approved: yes"), encoding="utf-8")
        stale_draft_review = run_devctl(
            repo,
            "check",
            "local-review",
            "--issue",
            "draft",
            "--file",
            str(oss_final_body),
            "--action",
            "issue-create",
            "--attachments",
            str(oss_published_manifest),
            expect=1,
        )
        assert "current task Issue mismatch: expected draft, found 1" in stale_draft_review.stderr
        write(current_task, current_task_text("draft"))
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(oss_final_body), "--action", "issue-create", "--attachments", str(oss_published_manifest))
        run_devctl(repo, "issue", "create", "OSS image gate", "--body-file", str(oss_final_body), "--attachments", str(oss_published_manifest))

        run_devctl(repo, "issue", "create", "Review required", "--body-file", str(auto_issue_body := issue_file.with_name("plain-issue.md")), expect=1)

        write(
            auto_issue_body,
            """<!-- xflow: issue-draft -->

## Background
Need unattended plain issue creation.

## Problem
Some issues have no attachments.

## Goal
Create a plain issue without manual approval when explicitly requested.

## Scope
- Includes: no attachments.

## Acceptance Criteria
- [ ] Issue body is sent without attachment upload.

## Verification Plan
- python tests/python-core.py
""",
        )
        with RecordingApiServer() as plain_server:
            plain_env = {
                "GITHUB_API_BASE": plain_server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            }
            rejected_plain = run_devctl_with_env(
                repo,
                plain_env,
                "issue",
                "create",
                "Restricted unattended issue",
                "--body-file",
                str(auto_issue_body),
                "--no-local-review",
                expect=1,
            )
            assert "--no-local-review requires active task-scoped unattended mode" in rejected_plain.stderr
            assert not plain_server.requests
            enable(repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
            plain_result = run_devctl_with_env(
                repo,
                plain_env,
                "issue",
                "create",
                "Restricted unattended issue",
                "--body-file",
                str(auto_issue_body),
                "--no-local-review",
            )
            assert "Issue #42 created" in plain_result.stdout
            created_history = repo / ".xflow" / "issues" / "issue-42" / "approvals" / "history"
            created_payloads = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in created_history.glob("*.yaml")]
            assert len(created_payloads) == 1
            assert created_payloads[0]["source"] == "unattended"
            assert created_payloads[0]["issue"] == "42"
            assert created_payloads[0]["approvalIssue"] == "draft"
            draft_history = repo / ".xflow" / "issues" / "issue-draft" / "approvals" / "history"
            assert not any("action: issue-create" in path.read_text(encoding="utf-8") for path in draft_history.glob("*.yaml"))
            plain_issue_requests = [item for item in plain_server.requests if item["method"] == "POST" and item["path"].endswith("/issues")]
            assert plain_issue_requests
            assert "Need unattended plain issue creation." in str(plain_issue_requests[-1]["json"])
            assert not [item for item in plain_server.requests if item["path"].endswith("/releases/77/assets")]

        disable(repo)
        write(current_task, current_task_text("1").replace("G5_APPROVE_MR_CREATE", "S9_REMOTE_REVIEW_AND_CI"))
        merge_base = git_text(repo, "branch", "--show-current")
        merge_branch = "feature/1-recorded-merge"
        git(repo, "checkout", "-b", merge_branch, "-q")
        git(repo, "config", "extensions.worktreeConfig", "true")
        git(repo, "config", "--worktree", "devctl.issue", "1")
        git(repo, "config", "--worktree", "devctl.base", merge_base)
        git(repo, "config", "--worktree", "devctl.pr", "42")
        merge_identity = json.dumps(
            {
                "number": 42,
                "state": "open",
                "head": {"ref": merge_branch},
                "base": {"ref": merge_base},
            }
        )
        with RecordingApiServer(pull_request_payload=merge_identity) as merge_server:
            merge_env = {
                "GITHUB_API_BASE": merge_server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            }
            assert not active_task_pointer_file(repo, resolve_bindings(repo).worktree).exists()
            run_devctl(
                repo,
                "approval",
                "prepare",
                "--issue",
                "1",
                "--action",
                "git-pr-merge",
                "--file",
                str(mr_file),
                "--force",
            )
            approval = repo / ".xflow" / "issues" / "issue-1" / "approvals" / "local-review.md"
            approval.write_text(approval.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8")
            merge_result = run_devctl_with_env(
                repo,
                merge_env,
                "git",
                "pr-merge",
                "42",
                "--method",
                "squash",
                "--issue",
                "1",
                "--file",
                str(mr_file),
            )
            assert "PR #42 merged" in merge_result.stdout
            merge_requests = [item for item in merge_server.requests if item["method"] == "PUT"]
            assert merge_requests
            assert merge_requests[-1]["path"] == "/repos/Linkk2000/paper-demo/pulls/42/merge"
            assert '"merge_method": "squash"' in str(merge_requests[-1]["json"])

        write(current_task, current_task_text("draft"))
        auto_issue_body = repo / ".xflow" / "issues" / "issue-draft" / "auto-issue.md"
        write(
            auto_issue_body,
            """<!-- xflow: issue-draft -->

## Background
Need to prevent unsupported issue image upload.

## Problem
GitHub release assets are not approved as an issue image store.

## Goal
Fail before remote writes when an issue body includes an image attachment.

## Scope
- Includes: one pasted image and one generic file.

## Acceptance Criteria
- [ ] Issue creation stops before GitHub issue or release upload requests.

## Verification Plan
- python tests/python-core.py
""",
        )
        auto_image = repo / "auto-image.png"
        auto_image.write_bytes(b"\x89PNG\r\n\x1a\nauto-github-image")
        auto_file = repo / "notes.txt"
        write_text_lf(auto_file, "generic attachment notes\n")
        git(repo, "config", "--worktree", "devctl.issue", "draft")
        enable(repo, "draft", "XFLOW_HUMAN_UNATTENDED_ALL")
        auto_manifest = repo / ".xflow" / "issues" / "issue-draft" / "attachments" / "manifest.json"
        manifest_before = auto_manifest.read_bytes()
        with RecordingApiServer() as github_server:
            github_env = {
                "GITHUB_API_BASE": github_server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            }
            auto_result = run_devctl_with_env(
                repo,
                github_env,
                "issue",
                "create",
                "Auto attachment issue",
                "--body-file",
                str(auto_issue_body),
                "--attach-file",
                str(auto_image),
                "--attach-file",
                str(auto_file),
                "--upload-attachments",
                "github",
                "--no-local-review",
                expect=1,
            )
            assert "inline issue attachments are disabled" in auto_result.stderr
            assert auto_manifest.read_bytes() == manifest_before
            github_issue_requests = [item for item in github_server.requests if item["method"] == "POST" and item["path"].endswith("/issues")]
            assert not github_issue_requests
            upload_requests = [item for item in github_server.requests if item["path"].endswith("/releases/77/assets")]
            assert not upload_requests

        templates = repo / ".xflow" / "ops" / "workflow" / "templates"
        templates.mkdir(parents=True, exist_ok=True)
        (templates / "ai-rules.json").write_bytes(
            b"\xef\xbb\xbf"
            + """{
  "rules": [
    {
      "id": "codex",
      "target": "AGENTS.md",
      "template": "codex-agents.md",
      "description": "Codex project rules"
    }
  ]
}
""".encode("utf-8"),
        )
        write(templates / "codex-agents.md", "# Project Rules\n\n- Human review is required before remote writes.\n")
        run_devctl(repo, "rules", "list")
        run_devctl(repo, "rules", "sync", "codex")
        assert (repo / "AGENTS.md").read_text(encoding="utf-8").startswith("# Project Rules")

        write(
            repo / ".gitmodules",
            """[submodule ".xflow/ops/devctl"]
\tpath = .xflow/ops/devctl
\turl = git@github.com:Linkk2000/xflow-devctl.git
\tbranch = main
\tignore = untracked
[submodule ".xflow/ops/workflow"]
\tpath = .xflow/ops/workflow
\turl = git@github.com:Linkk2000/xflow-skills.git
\tbranch = main
\tignore = untracked
""",
        )
        write(repo / ".xflow" / "ops" / "devctl" / "__pycache__" / "x.pyc", "bytecode")
        run_devctl(repo, "check", "submodule-hygiene", expect=1)
        shutil.rmtree(repo / ".xflow" / "ops" / "devctl" / "__pycache__")
        run_devctl(repo, "check", "submodule-hygiene")

        run_devctl(repo, "migrate", "inspect")

    print("python core ok")


if __name__ == "__main__":
    main()
