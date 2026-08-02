from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval
from xflow import cli, providers
from xflow.bindings import git_path, resolve_bindings
from xflow.paths import active_task_pointer_file
from xflow.task_state import TaskState, activate_task, render_task_state
from xflow.unattended import enable


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_devctl(repo_root: Path, extra_env: dict[str, str], *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        **extra_env,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "DEVCTL_TOOL_ROOT": str(OPS_ROOT),
        "DEVCTL_OPS_ROOT": str(OPS_ROOT),
        "PYTHONPATH": str(OPS_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
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
    assert result.returncode == expect, result.stderr
    return result


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def assert_value_error(expected: str, action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except ValueError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def approval_scope_lock_path(repo_root: Path, runtime_scope: str) -> Path:
    return (
        git_path(repo_root, "--git-common-dir")
        / "xflow"
        / "runtime"
        / runtime_scope
        / resolve_bindings(repo_root).worktree
        / "claims.lock"
    )


def assert_claim_lock_released_and_reacquirable(
    repo_root: Path,
    runtime_scope: str,
    approval_id: str,
) -> None:
    lock_path = approval_scope_lock_path(repo_root, runtime_scope)
    assert lock_path.is_file()
    assert {path.name for path in lock_path.parent.iterdir()} == {"claims.lock"}
    script = """
import sys
from pathlib import Path
from xflow import approval

repo = Path(sys.argv[1])
with approval._approval_claim_lock(repo, sys.argv[2], sys.argv[3], "claim lock is busy"):
    print("acquired")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo_root), approval_id, runtime_scope],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": str(OPS_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"
    assert lock_path.is_file()
    assert {path.name for path in lock_path.parent.iterdir()} == {"claims.lock"}


def wait_for_path(path: Path, label: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {label}: {path}")


def task_state(issue: str, branch: str) -> TaskState:
    return TaskState(
        issue=issue,
        execution_state="S6_PREPARE_COMMIT_AND_MR_DRAFT",
        semantic_phase="classified",
        classification="ui-defect",
        contract="example.contract.approval-binding@0.1.0",
        contract_file="docs/requirements/example/contract.yaml",
        contract_change_required=False,
        branch=branch,
        base="main",
        allowed_actions=("prepare-verification",),
        forbidden_actions=("edit-implementation",),
        human_gate="local human approval required",
        human_approval_ref="none",
    )


def activate(repo_root: Path, issue: str) -> None:
    branch = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    write(
        repo_root / ".xflow" / "issues" / f"issue-{issue}" / "task-state.md",
        render_task_state(task_state(issue, branch)),
    )
    activate_task(repo_root, issue)


def approve(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8", newline="\n")


def init_active_repo(root: Path, name: str, issue: str = "202") -> tuple[Path, Path]:
    repo = root / name
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", f"# {name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init", "-q")
    git(repo, "checkout", "-b", f"feature/{issue}-{name}", "-q")
    approved_file = repo / ".xflow" / "issues" / f"issue-{issue}" / "walkthrough.md"
    write(approved_file, "# Walkthrough\n\nApproved artifact.\n")
    activate(repo, issue)
    return repo, approved_file


def init_remote_mr_repo(root: Path, name: str) -> tuple[Path, Path, Path]:
    origin = root / f"{name}-origin.git"
    repo = root / name
    git(root, "init", "--bare", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", f"# {name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")
    branch = f"feature/202-{name}"
    git(repo, "checkout", "-b", branch, "-q")
    write(repo / "feature.txt", "approved feature content\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat(xflow): 添加远端恢复场景", "-q")
    git(repo, "push", "-u", "origin", branch, "-q")
    approved_file = repo / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
    activate(repo, "202")
    return repo, approved_file, origin


def test_consumed_record(repo_root: Path, approved_file: Path) -> None:
    original_artifact = approved_file.read_text(encoding="utf-8")
    review = approval.prepare(repo_root, "202", "git-push", approved_file, reviewer="trusted local reviewer", force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-push", approved_file, "202")
    assert grant.source == "local-review"
    assert grant.action == "git-push"
    assert grant.approved_sha256 == approval.sha256_file(approved_file)
    approved_file.write_text("mutated after gate\n", encoding="utf-8", newline="\n")
    review.write_text(review.read_text(encoding="utf-8").replace("trusted local reviewer", "changed reviewer"), encoding="utf-8")
    record = approval.record_consumed_approval(repo_root, grant, "success")
    text = record.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    assert record.parent == repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    assert payload["version"] == "0.1.0"
    assert payload["reusable"] is False
    assert payload["source"] == "local-review"
    assert payload["issue"] == "202"
    assert payload["approvalIssue"] == "202"
    assert payload["action"] == "git-push"
    assert payload["approvedSha256"] == grant.approved_sha256
    assert payload["reviewerSummary"] == "trusted local reviewer"
    assert "Approved: yes" not in text
    assert "GITHUB_TOKEN" not in text
    approved_file.write_text(original_artifact, encoding="utf-8", newline="\n")
    assert_value_error(
        "approval already consumed",
        lambda: approval.require_remote(repo_root, "git-push", approved_file, "202"),
    )
    assert_value_error(
        "approval already consumed",
        lambda: approval.record_consumed_approval(repo_root, replace(grant, action="git-mr"), "success"),
    )

    history = record.parent
    before = tuple(history.glob("*.yaml"))
    assert_value_error(
        "confirmed success",
        lambda: approval.record_consumed_approval(repo_root, grant, "failure"),  # type: ignore[arg-type]
    )
    assert tuple(history.glob("*.yaml")) == before

    assert_value_error(
        "invalid approval action",
        lambda: approval.prepare(repo_root, "202", "remote-write", approved_file, force=True),
    )
    review = approval.prepare(repo_root, "202", "git-push", approved_file, force=True)
    approve(review)
    assert_value_error(
        "action mismatch: expected git-mr, got git-push",
        lambda: approval.require_remote(repo_root, "git-mr", approved_file, "202"),
    )


def test_history_integrity(repo_root: Path, approved_file: Path) -> None:
    review = approval.prepare(repo_root, "202", "issue-comment", approved_file, force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "issue-comment", approved_file, "202")

    class FrozenDatetime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            return datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)

        @classmethod
        def fromisoformat(cls, value: str) -> datetime:
            return datetime.fromisoformat(value)

    with patch.object(approval, "datetime", FrozenDatetime):
        record = approval.record_consumed_approval(repo_root, grant, "success")
        review = approval.prepare(repo_root, "202", "issue-comment", approved_file, force=True)
        approve(review)
        second_grant = approval.require_remote(repo_root, "issue-comment", approved_file, "202")
        assert_value_error(
            "history collision",
            lambda: approval.record_consumed_approval(repo_root, second_grant, "success"),
        )

    payload = yaml.safe_load(record.read_text(encoding="utf-8"))
    payload.pop("approvedSha256")
    record.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    review = approval.prepare(repo_root, "202", "git-mr", approved_file, force=True)
    approve(review)
    assert_value_error(
        "approval history integrity error",
        lambda: approval.require_remote(repo_root, "git-mr", approved_file, "202"),
    )
    record.write_text("version: [unterminated\n", encoding="utf-8")
    assert_value_error(
        "invalid YAML",
        lambda: approval.require_remote(repo_root, "git-mr", approved_file, "202"),
    )


def test_effect_masquerade_fails_closed(repo_root: Path, approved_file: Path) -> None:
    review = approval.prepare(repo_root, "202", "git-push", approved_file, force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-push", approved_file, "202")
    record = approval.record_consumed_approval(repo_root, grant, "success")
    payload = yaml.safe_load(record.read_text(encoding="utf-8"))
    payload["source"] = "effect"
    payload["parentAction"] = "git-mr"
    payload["parentApprovalId"] = "a" * 32
    record.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    assert_value_error(
        "approval history integrity error",
        lambda: approval.require_remote(repo_root, "git-push", approved_file, "202"),
    )
    assert_value_error(
        "approval history integrity error",
        lambda: approval.record_consumed_approval(
            repo_root,
            replace(grant, approval_id="b" * 32, action="git-mr"),
            "success",
        ),
    )


def test_history_path_validation(repo_root: Path, approved_file: Path) -> None:
    review = approval.prepare(repo_root, "202", "git-push", approved_file, force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-push", approved_file, "202")
    for invalid_issue in ("../escape", "..\\escape", ".", "..", "nested/escape", "nested\\escape"):
        assert_value_error(
            "issue identifier",
            lambda invalid_issue=invalid_issue: approval.record_consumed_approval(
                repo_root, grant, "success", target_issue=invalid_issue
            ),
        )
    for invalid_action in ("../escape", "..\\escape", ".", "unknown-action"):
        assert_value_error(
            "invalid approval action",
            lambda invalid_action=invalid_action: approval.record_consumed_approval(
                repo_root, replace(grant, action=invalid_action), "success"
            ),
        )
    assert_value_error(
        "target Issue mismatch",
        lambda: approval.record_consumed_approval(repo_root, grant, "success", target_issue="303"),
    )
    for action in ("issue-comment", "git-mr"):
        review = approval.prepare(repo_root, "202", action, approved_file, force=True)
        approve(review)
        action_grant = approval.require_remote(repo_root, action, approved_file, "202")
        assert_value_error(
            "target Issue mismatch",
            lambda action_grant=action_grant: approval.record_consumed_approval(
                repo_root,
                action_grant,
                "success",
                target_issue="303",
            ),
        )
    assert not (repo_root / ".xflow" / "escape").exists()
    assert not (repo_root / ".xflow" / "issues" / "issue-303" / "approvals" / "history").exists()


def test_credential_safety(repo_root: Path, approved_file: Path) -> None:
    review = approval.prepare(repo_root, "202", "git-push", approved_file, reviewer="Bearer top-secret", force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-push", approved_file, "202")
    assert grant.reviewer_summary == "human reviewer"
    record = approval.record_consumed_approval(repo_root, grant, "success")
    assert "Bearer" not in record.read_text(encoding="utf-8")

    for unsafe_grant in (
        replace(grant, approved_file="Bearer top-secret"),
        replace(grant, approved_file="api_key=hidden.md"),
        replace(grant, branch="feature/secret=hidden"),
        replace(grant, reviewer_summary="token=hidden"),
    ):
        assert_value_error(
            "credential-like text",
            lambda unsafe_grant=unsafe_grant: approval.record_consumed_approval(repo_root, unsafe_grant, "success"),
        )

    for name in ("ghp_12345678.md", "api_key=secret.md", "token=secret.md", "secret=hidden.md"):
        unsafe_file = approved_file.with_name(name)
        write(unsafe_file, "credential path test\n")
        review = approval.prepare(repo_root, "202", "git-push", unsafe_file, force=True)
        approve(review)
        assert_value_error(
            "credential-like text",
            lambda unsafe_file=unsafe_file: approval.require_remote(repo_root, "git-push", unsafe_file, "202"),
        )


def test_skipped_git_push_has_no_history(repo_root: Path, approved_file: Path) -> None:
    git(repo_root, "config", "extensions.worktreeConfig", "true")
    git(repo_root, "config", "--worktree", "devctl.issue", "202")
    git(repo_root, "config", "--worktree", "devctl.base", "main")
    review = approval.prepare(repo_root, "202", "git-push", approved_file, force=True)
    approve(review)
    result = run_devctl(
        repo_root,
        {"DEVCTL_SKIP_PUSH": "1", "DEVCTL_SKIP_PROVIDER_LOAD": "1"},
        "git",
        "push",
        "--issue",
        "202",
        "--file",
        str(approved_file),
    )
    assert "push skipped" in result.stdout
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    assert not tuple(history.glob("*.yaml"))


def test_unattended_grants_are_per_execution(repo_root: Path, approved_file: Path) -> None:
    enable(repo_root, "202", "XFLOW_HUMAN_UNATTENDED_ALL")
    first = approval.require_remote_or_unattended(repo_root, "issue-comment", approved_file, "202")
    approval.record_consumed_approval(repo_root, first, "success")
    second = approval.require_remote_or_unattended(repo_root, "issue-comment", approved_file, "202")
    assert second.approval_id != first.approval_id
    approval.record_consumed_approval(repo_root, second, "success")
    assert_value_error(
        "approval already consumed",
        lambda: approval.record_consumed_approval(repo_root, first, "success"),
    )


def test_local_remote_reservation_snapshot_and_recovery(repo_root: Path, approved_file: Path) -> None:
    original = approved_file.read_bytes()
    review = approval.prepare(
        repo_root,
        "202",
        "issue-comment",
        approved_file,
        reviewer="human reviewer",
        force=True,
    )
    approve(review)
    grant = approval.require_remote(repo_root, "issue-comment", approved_file, "202")

    def reserve() -> object:
        try:
            return approval.reserve_remote_action(repo_root, grant)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: reserve(), range(2)))
    reservations = [item for item in results if isinstance(item, approval.RemoteActionReservation)]
    failures = [item for item in results if isinstance(item, ValueError)]
    assert len(reservations) == len(failures) == 1, results
    assert "remote approval outcome unresolved" in str(failures[0])
    reservation = reservations[0]
    assert reservation.approved_bytes == original

    approved_file.write_bytes(b"mutated after reservation\n")
    assert reservation.approved_text() == original.decode("utf-8")
    approval.mark_remote_action_unknown(repo_root, reservation, "simulated provider timeout")
    assert_value_error(
        "remote approval outcome unresolved",
        lambda: approval.reserve_remote_action(repo_root, grant),
    )
    approval.reconcile_remote_action(
        repo_root,
        grant,
        outcome="no-effect",
        confirmation=approval.REMOTE_RECONCILIATION_CONFIRMATION,
    )

    approved_file.write_bytes(original)
    retry = approval.reserve_remote_action(repo_root, grant)
    assert retry.provider_required is True
    assert retry.attempt == 2
    approval.confirm_remote_action(
        repo_root,
        retry,
        target_issue="202",
        provider_receipt={"comment": "confirmed"},
    )

    recovered = approval.reserve_remote_action(repo_root, grant)
    assert recovered.provider_required is False
    record = approval.complete_remote_action(repo_root, recovered)
    payload = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert payload["approvedSnapshotSha256"] == grant.approved_sha256
    assert (repo_root / payload["approvedSnapshotFile"]).read_bytes() == original
    assert payload["providerReceipt"] == '{"comment":"confirmed"}'
    assert len(tuple(record.parent.glob("*-issue-comment-*.yaml"))) == 1
    assert_value_error("approval already consumed", lambda: approval.reserve_remote_action(repo_root, grant))
    assert_claim_lock_released_and_reacquirable(
        repo_root,
        "remote-approvals",
        grant.approval_id,
    )


def test_posix_claim_lock_keeps_third_process_behind_waiter(repo_root: Path) -> None:
    if os.name == "nt":
        return
    worker = """
import sys
import time
from pathlib import Path
import fcntl
from xflow import approval

repo = Path(sys.argv[1])
approval_id = sys.argv[2]
scope = sys.argv[3]
attempt = None if sys.argv[4] == "-" else Path(sys.argv[4])
entered = Path(sys.argv[5])
release = Path(sys.argv[6])
original_flock = fcntl.flock
if attempt is not None:
    def observed_flock(fd, operation):
        if operation & fcntl.LOCK_EX:
            attempt.write_text("attempted\\n", encoding="utf-8")
        return original_flock(fd, operation)
    fcntl.flock = observed_flock
with approval._approval_claim_lock(repo, approval_id, scope, "claim lock is busy"):
    entered.write_text("entered\\n", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""
    root = repo_root.parent / "posix-lock-signals"
    root.mkdir()
    approval_id = "a" * 64
    env = {
        **os.environ,
        "PYTHONPATH": str(OPS_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    markers = {
        name: root / name
        for name in (
            "a-entered",
            "a-release",
            "b-attempt",
            "b-entered",
            "b-release",
            "c-attempt",
            "c-entered",
            "c-release",
        )
    }

    def launch(attempt: str | None, entered: str, release: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(repo_root),
                approval_id,
                "remote-approvals",
                "-" if attempt is None else str(markers[attempt]),
                str(markers[entered]),
                str(markers[release]),
            ],
            cwd=repo_root,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    processes: list[subprocess.Popen[str]] = []
    try:
        first = launch(None, "a-entered", "a-release")
        processes.append(first)
        wait_for_path(markers["a-entered"], "first lock owner")
        waiter = launch("b-attempt", "b-entered", "b-release")
        processes.append(waiter)
        wait_for_path(markers["b-attempt"], "waiting process lock attempt")
        assert not markers["b-entered"].exists()
        markers["a-release"].write_text("release\n", encoding="utf-8")
        assert first.wait(timeout=10) == 0, first.stderr.read() if first.stderr else ""
        wait_for_path(markers["b-entered"], "waiting process entry")

        third = launch("c-attempt", "c-entered", "c-release")
        processes.append(third)
        wait_for_path(markers["c-attempt"], "third process lock attempt")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not markers["c-entered"].exists():
            time.sleep(0.01)
        assert not markers["c-entered"].exists(), "third process entered while waiter held the stable lock"

        markers["b-release"].write_text("release\n", encoding="utf-8")
        assert waiter.wait(timeout=10) == 0, waiter.stderr.read() if waiter.stderr else ""
        wait_for_path(markers["c-entered"], "third process entry after waiter release")
        markers["c-release"].write_text("release\n", encoding="utf-8")
        assert third.wait(timeout=10) == 0, third.stderr.read() if third.stderr else ""
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_issue_comment_provider_consumes_reserved_bytes(repo_root: Path, approved_file: Path) -> None:
    original = b"# Exact approved comment\n\nProvider must receive these bytes.\n"
    approved_file.write_bytes(original)
    review = approval.prepare(
        repo_root,
        "202",
        "issue-comment",
        approved_file,
        reviewer="human reviewer",
        force=True,
    )
    approve(review)
    captured: dict[str, str] = {}
    original_reserve = approval.reserve_remote_action

    def reserve_then_mutate(root: Path, grant: approval.ApprovalGrant) -> approval.RemoteActionReservation:
        reservation = original_reserve(root, grant)
        approved_file.write_bytes(b"unapproved mutation after reservation\n")
        return reservation

    def comment_provider(
        root: Path,
        issue: str,
        body: str,
        env: object,
    ) -> dict[str, object]:
        del root, env
        captured[issue] = body
        return {"html_url": "https://example.invalid/comment/1"}

    with (
        patch.dict(
            os.environ,
            {
                "DEVCTL_REPO_ROOT": str(repo_root),
                "DEVCTL_TOOL_ROOT": str(OPS_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        ),
        patch.object(approval, "reserve_remote_action", side_effect=reserve_then_mutate),
        patch.object(providers, "comment_issue", side_effect=comment_provider),
    ):
        os.environ.pop("DEVCTL_SKIP_PROVIDER_LOAD", None)
        assert cli.main(["issue", "comment", "202", "--body-file", str(approved_file)]) == 0

    assert captured["202"].encode("utf-8") == original
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    record = next(history.glob("*-issue-comment-*.yaml"))
    payload = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert (repo_root / payload["approvedSnapshotFile"]).read_bytes() == original


def test_remote_reconciliation_cli_requires_exact_human_confirmation(
    repo_root: Path,
    approved_file: Path,
) -> None:
    review = approval.prepare(
        repo_root,
        "202",
        "issue-comment",
        approved_file,
        reviewer="human reviewer",
        force=True,
    )
    approve(review)
    grant = approval.require_remote(repo_root, "issue-comment", approved_file, "202")
    reservation = approval.reserve_remote_action(repo_root, grant)
    approval.mark_remote_action_unknown(repo_root, reservation, "simulated provider timeout")

    rejected = run_devctl(
        repo_root,
        {},
        "approval",
        "reconcile",
        "--issue",
        "202",
        "--approval-id",
        grant.approval_id,
        "--outcome",
        "no-effect",
        "--confirm",
        "wrong",
        expect=1,
    )
    assert approval.REMOTE_RECONCILIATION_CONFIRMATION in rejected.stderr

    run_devctl(
        repo_root,
        {},
        "approval",
        "reconcile",
        "--issue",
        "202",
        "--approval-id",
        grant.approval_id,
        "--outcome",
        "no-effect",
        "--confirm",
        approval.REMOTE_RECONCILIATION_CONFIRMATION,
    )
    retry = approval.reserve_remote_action(repo_root, grant)
    assert retry.provider_required is True
    assert retry.attempt == 2


def test_reserved_mr_history_can_parent_confirmed_backfill(
    repo_root: Path,
    approved_file: Path,
) -> None:
    original = b"# Approved MR body\n\nExact parent snapshot.\n"
    approved_file.write_bytes(original)
    review = approval.prepare(repo_root, "202", "git-mr", approved_file, reviewer="human reviewer", force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-mr", approved_file, "202")
    reservation = approval.reserve_remote_action(repo_root, grant)
    approved_file.write_bytes(b"changed after provider reservation\n")
    confirmed = approval.confirm_remote_action(
        repo_root,
        reservation,
        target_issue=None,
        provider_receipt={"number": "42", "html_url": "https://example.invalid/pulls/42"},
    )
    assert_value_error(
        "post-effects are not complete",
        lambda: approval.complete_remote_action(repo_root, confirmed),
    )
    parent = approval.publish_remote_action_history(repo_root, confirmed)
    effect = approval.record_subordinate_effect(
        repo_root,
        grant,
        "git-state-backfill",
        "success",
        idempotent=True,
    )
    assert approval.record_subordinate_effect(
        repo_root,
        grant,
        "git-state-backfill",
        "success",
        idempotent=True,
    ) == effect
    ready = approval.mark_remote_post_effects_complete(repo_root, confirmed)
    assert approval.complete_remote_action(repo_root, ready) == parent

    parent_payload = yaml.safe_load(parent.read_text(encoding="utf-8"))
    effect_payload = yaml.safe_load(effect.read_text(encoding="utf-8"))
    assert (repo_root / parent_payload["approvedSnapshotFile"]).read_bytes() == original
    assert effect_payload["parentApprovalId"] == grant.approval_id
    assert effect_payload["parentAction"] == "git-mr"


def prepare_replayable_mr(repo_root: Path, approved_file: Path) -> approval.ApprovalGrant:
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 202
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Create the approved MR.

## Forbidden Actions
- Change implementation after approval.
""",
    )
    write(
        approved_file,
        """<!-- xflow: mr-draft -->

Closes #202

## Summary
- Exercise replay after provider confirmation.

## Test Plan
- python tests/approval-binding.py

## Risk
- Low.

## Review Request
- Review the recovered local metadata.
""",
    )
    review = approval.prepare(repo_root, "202", "git-mr", approved_file, reviewer="human reviewer", force=True)
    approve(review)
    return approval.require_remote(repo_root, "git-mr", approved_file, "202")


def run_replayable_mr(
    repo_root: Path,
    provider_calls: list[str],
    *,
    body_file: Path | None = None,
    set_meta: object | None = None,
    backfill_result: cli.PushResult | None = cli.PushResult(performed=True, success=True),
    update_task: object | None = None,
) -> int:
    selected_body = body_file or repo_root / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
    def create_pull_request(*_args: object, **_kwargs: object) -> providers.PullRequestResult:
        provider_calls.append("create")
        return providers.PullRequestResult("42", "https://example.invalid/pulls/42")

    patches = [
        patch.dict(
            os.environ,
            {
                "DEVCTL_REPO_ROOT": str(repo_root),
                "DEVCTL_TOOL_ROOT": str(OPS_ROOT),
                "DEVCTL_OPS_ROOT": str(OPS_ROOT),
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        ),
        patch.object(cli, "require_branch_ready_for_mr"),
        patch.object(cli, "require_branch_contains_remote_base"),
        patch.object(providers, "create_pull_request", side_effect=create_pull_request),
        patch.object(
            cli,
            "commit_and_push_pr_backfill",
            return_value=backfill_result,
        ),
    ]
    if set_meta is not None:
        patches.append(patch.object(cli, "set_branch_meta", side_effect=set_meta))
    if update_task is not None:
        patches.append(patch.object(cli, "update_current_task_for_pr", side_effect=update_task))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        if set_meta is not None:
            with patches[5]:
                return cli.main(
                    ["git", "mr", "--title", "Replay MR", "--body-file", str(selected_body), "--issue", "202"]
                )
        if update_task is not None:
            with patches[5]:
                return cli.main(
                    ["git", "mr", "--title", "Replay MR", "--body-file", str(selected_body), "--issue", "202"]
                )
        return cli.main(
            ["git", "mr", "--title", "Replay MR", "--body-file", str(selected_body), "--issue", "202"]
        )


def run_real_replayable_mr(
    repo_root: Path,
    provider_calls: list[str],
    *,
    push_effect: object | None = None,
    history_effect: object | None = None,
) -> int:
    body_file = repo_root / ".xflow" / "issues" / "issue-202" / "walkthrough.md"

    def create_pull_request(*_args: object, **_kwargs: object) -> providers.PullRequestResult:
        provider_calls.append("create")
        return providers.PullRequestResult("42", "https://example.invalid/pulls/42")

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "DEVCTL_REPO_ROOT": str(repo_root),
                    "DEVCTL_TOOL_ROOT": str(OPS_ROOT),
                    "DEVCTL_OPS_ROOT": str(OPS_ROOT),
                    "DEVCTL_SKIP_PROVIDER_LOAD": "0",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                clear=False,
            )
        )
        stack.enter_context(patch.object(providers, "create_pull_request", side_effect=create_pull_request))
        if push_effect is not None:
            stack.enter_context(patch.object(cli, "push_branch", side_effect=push_effect))
        if history_effect is not None:
            stack.enter_context(
                patch.object(
                    approval,
                    "publish_remote_action_history",
                    side_effect=history_effect,
                )
            )
        return cli.main(
            ["git", "mr", "--title", "Replay MR", "--body-file", str(body_file), "--issue", "202"]
        )


def test_mr_replay_after_provider_confirmation_finishes_local_effects(
    repo_root: Path,
    approved_file: Path,
) -> None:
    grant = prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def fail_before_pr_meta(_repo_root: Path, key: str, _value: str) -> None:
        if key == "pr":
            raise RuntimeError("injected failure before PR metadata")
        raise AssertionError(f"unexpected metadata key before injected failure: {key}")

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_before_pr_meta)
    except RuntimeError as exc:
        assert "injected failure" in str(exc)
    else:
        raise AssertionError("expected post-provider metadata failure")

    claim_path = next(
        (repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history" / "claims").glob("*-remote.yaml")
    )
    claim = yaml.safe_load(claim_path.read_text(encoding="utf-8"))
    assert claim["approvalId"] == grant.approval_id
    assert claim["state"] == "post-effects-pending"
    assert provider_calls == ["create"]

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]
    assert cli.branch_meta(repo_root, "pr") == "42"
    assert cli.branch_meta(repo_root, "pr-url") == "https://example.invalid/pulls/42"
    suggestion = repo_root / ".xflow" / "issues" / "issue-202" / "state-update-suggestion.md"
    assert "PR: 42" in suggestion.read_text(encoding="utf-8")
    assert "PR URL: https://example.invalid/pulls/42" in suggestion.read_text(encoding="utf-8")
    current_task = (repo_root / ".xflow" / "current-task.md").read_text(encoding="utf-8")
    assert "State: S9_REMOTE_REVIEW_AND_CI" in current_task
    assert "PR: 42" in current_task

    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    records = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in history.glob("*.yaml")]
    assert len([record for record in records if record["action"] == "git-mr"]) == 1
    assert len([record for record in records if record["action"] == "git-state-backfill"]) == 1
    assert yaml.safe_load(claim_path.read_text(encoding="utf-8"))["state"] == "completed"


def test_mr_replay_converges_after_partial_metadata(repo_root: Path, approved_file: Path) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []
    original_set_branch_meta = cli.set_branch_meta

    def fail_after_pr_meta(root: Path, key: str, value: str) -> None:
        if key == "pr-url":
            raise RuntimeError("injected failure after PR number metadata")
        original_set_branch_meta(root, key, value)

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_after_pr_meta)
    except RuntimeError as exc:
        assert "injected failure" in str(exc)
    else:
        raise AssertionError("expected partial metadata failure")
    assert cli.branch_meta(repo_root, "pr") == "42"
    assert not cli.branch_meta(repo_root, "pr-url")

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    actions = [yaml.safe_load(path.read_text(encoding="utf-8"))["action"] for path in history.glob("*.yaml")]
    assert actions.count("git-mr") == 1
    assert actions.count("git-state-backfill") == 1


def assert_real_mr_replay_completed(repo_root: Path, origin: Path, provider_calls: list[str]) -> None:
    branch = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    assert provider_calls == ["create"]
    assert subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip() == subprocess.run(
        ["git", "-C", str(origin), "rev-parse", f"refs/heads/{branch}"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    actions = [yaml.safe_load(path.read_text(encoding="utf-8"))["action"] for path in history.glob("*.yaml")]
    assert actions.count("git-mr") == 1
    assert actions.count("git-state-backfill") == 1
    claim_path = next((history / "claims").glob("*-remote.yaml"))
    assert yaml.safe_load(claim_path.read_text(encoding="utf-8"))["state"] == "completed"


def test_mr_replay_pushes_existing_local_backfill_commit(
    repo_root: Path,
    approved_file: Path,
    origin: Path,
) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []
    try:
        run_real_replayable_mr(
            repo_root,
            provider_calls,
            push_effect=RuntimeError("injected failure after local backfill commit"),
        )
    except RuntimeError as exc:
        assert "injected failure" in str(exc)
    else:
        raise AssertionError("expected failure after local backfill commit")
    branch = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    assert subprocess.run(
        ["git", "-C", str(repo_root), "rev-list", "--count", f"origin/{branch}..HEAD"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip() == "1"

    assert run_real_replayable_mr(repo_root, provider_calls) == 0
    assert_real_mr_replay_completed(repo_root, origin, provider_calls)


def test_mr_replay_verifies_already_pushed_backfill_before_history(
    repo_root: Path,
    approved_file: Path,
    origin: Path,
) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []
    try:
        run_real_replayable_mr(
            repo_root,
            provider_calls,
            history_effect=RuntimeError("injected failure after backfill push"),
        )
    except RuntimeError as exc:
        assert "injected failure" in str(exc)
    else:
        raise AssertionError("expected failure after backfill push")

    assert run_real_replayable_mr(repo_root, provider_calls) == 0
    assert_real_mr_replay_completed(repo_root, origin, provider_calls)


def test_mr_replay_rejects_conflicting_pr_identity(repo_root: Path, approved_file: Path) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def fail_before_pr_meta(_repo_root: Path, key: str, _value: str) -> None:
        if key == "pr":
            raise RuntimeError("injected failure before PR metadata")

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_before_pr_meta)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected post-provider metadata failure")
    cli.set_branch_meta(repo_root, "pr", "99")

    assert run_replayable_mr(repo_root, provider_calls) == 1
    assert provider_calls == ["create"]
    assert cli.branch_meta(repo_root, "pr") == "99"
    claim_path = next(
        (repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history" / "claims").glob("*-remote.yaml")
    )
    assert yaml.safe_load(claim_path.read_text(encoding="utf-8"))["state"] == "post-effects-pending"


def test_mr_without_successful_backfill_stays_nonterminal(
    repo_root: Path,
    approved_file: Path,
    result: cli.PushResult | None,
) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []
    assert run_replayable_mr(repo_root, provider_calls, backfill_result=result) == 1
    assert provider_calls == ["create"]
    claim_path = next(
        (repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history" / "claims").glob(
            "*-remote.yaml"
        )
    )
    assert yaml.safe_load(claim_path.read_text(encoding="utf-8"))["state"] == "post-effects-pending"
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    assert not tuple(history.glob("*.yaml"))


def test_mr_terminal_gate_requires_unique_backfill_effect(repo_root: Path, approved_file: Path) -> None:
    grant = prepare_replayable_mr(repo_root, approved_file)
    reservation = approval.reserve_remote_action(repo_root, grant)
    confirmed = approval.confirm_remote_action(
        repo_root,
        reservation,
        target_issue=None,
        provider_receipt={"number": "42", "html_url": "https://example.invalid/pulls/42"},
    )
    approval.publish_remote_action_history(repo_root, confirmed)
    assert_value_error(
        "git-state-backfill effect",
        lambda: approval.mark_remote_post_effects_complete(repo_root, confirmed),
    )


def test_mr_replay_uses_sealed_body_when_mutable_body_changes(repo_root: Path, approved_file: Path) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def fail_before_pr_meta(_repo_root: Path, key: str, _value: str) -> None:
        if key == "pr":
            raise RuntimeError("injected failure before PR metadata")

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_before_pr_meta)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected post-provider metadata failure")
    approved_file.write_text("mutated body without MR markers\n", encoding="utf-8")

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]


def test_mr_replay_uses_sealed_body_when_mutable_body_is_deleted(repo_root: Path, approved_file: Path) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def fail_before_pr_meta(_repo_root: Path, key: str, _value: str) -> None:
        if key == "pr":
            raise RuntimeError("injected failure before PR metadata")

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_before_pr_meta)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected post-provider metadata failure")
    approved_file.unlink()

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]


def test_pending_mr_claim_discovery_rejects_conflicting_body_path(
    repo_root: Path,
    approved_file: Path,
) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def fail_before_pr_meta(_repo_root: Path, key: str, _value: str) -> None:
        if key == "pr":
            raise RuntimeError("injected failure before PR metadata")

    try:
        run_replayable_mr(repo_root, provider_calls, set_meta=fail_before_pr_meta)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected post-provider metadata failure")
    conflicting_body = approved_file.with_name("other-mr-draft.md")
    shutil.copyfile(approved_file, conflicting_body)

    assert run_replayable_mr(repo_root, provider_calls, body_file=conflicting_body) == 1
    assert provider_calls == ["create"]


def test_pending_mr_claim_discovery_rejects_multiple_matches(repo_root: Path, approved_file: Path) -> None:
    first = prepare_replayable_mr(repo_root, approved_file)
    first_reservation = approval.reserve_remote_action(repo_root, first)
    approval.confirm_remote_action(
        repo_root,
        first_reservation,
        target_issue=None,
        provider_receipt={"number": "42", "html_url": "https://example.invalid/pulls/42"},
    )
    second_review = approval.prepare(
        repo_root,
        "202",
        "git-mr",
        approved_file,
        reviewer="second human reviewer",
        force=True,
    )
    approve(second_review)
    second = approval.require_remote(repo_root, "git-mr", approved_file, "202")
    assert second.approval_id != first.approval_id
    with patch.object(approval, "_arbitrate_remote_claim_scope", return_value=None):
        second_reservation = approval.reserve_remote_action(repo_root, second)
    approval.confirm_remote_action(
        repo_root,
        second_reservation,
        target_issue=None,
        provider_receipt={"number": "43", "html_url": "https://example.invalid/pulls/43"},
    )

    assert_value_error(
        "multiple matching pending git-mr claims",
        lambda: approval.resume_pending_remote_action(repo_root, "git-mr", approved_file, "202"),
    )


def _prepare_replacement_mr_approval(
    repo_root: Path,
    approved_file: Path,
    previous: approval.ApprovalGrant,
) -> approval.ApprovalGrant:
    review = approval.prepare(
        repo_root,
        "202",
        "git-mr",
        approved_file,
        reviewer="replacement human reviewer",
        force=True,
    )
    approve(review)
    grant = approval.require_remote(repo_root, "git-mr", approved_file, "202")
    assert grant.approval_id != previous.approval_id
    return grant


def test_reserved_mr_claim_blocks_replacement_approval_provider_call(
    repo_root: Path,
    approved_file: Path,
) -> None:
    first = prepare_replayable_mr(repo_root, approved_file)
    first_reservation = approval.reserve_remote_action(repo_root, first)
    replacement = _prepare_replacement_mr_approval(repo_root, approved_file, first)
    provider_calls: list[str] = []

    assert run_replayable_mr(repo_root, provider_calls) == 1
    assert provider_calls == []
    assert yaml.safe_load(first_reservation.claim_path.read_text(encoding="utf-8"))["state"] == "reserved"
    replacement_claim = approval._remote_claim_path(repo_root, "202", replacement.approval_id)
    assert not replacement_claim.exists()


def test_unknown_mr_claim_blocks_replacement_approval_provider_call(
    repo_root: Path,
    approved_file: Path,
) -> None:
    first = prepare_replayable_mr(repo_root, approved_file)
    first_reservation = approval.reserve_remote_action(repo_root, first)
    approval.mark_remote_action_unknown(repo_root, first_reservation, "provider result was not observed")
    replacement = _prepare_replacement_mr_approval(repo_root, approved_file, first)
    provider_calls: list[str] = []

    assert run_replayable_mr(repo_root, provider_calls) == 1
    assert provider_calls == []
    assert yaml.safe_load(first_reservation.claim_path.read_text(encoding="utf-8"))["state"] == "outcome-unknown"
    replacement_claim = approval._remote_claim_path(repo_root, "202", replacement.approval_id)
    assert not replacement_claim.exists()


def test_human_confirmed_no_effect_allows_replacement_mr_approval(
    repo_root: Path,
    approved_file: Path,
) -> None:
    first = prepare_replayable_mr(repo_root, approved_file)
    first_reservation = approval.reserve_remote_action(repo_root, first)
    approval.reconcile_remote_action(
        repo_root,
        first,
        outcome="no-effect",
        confirmation=approval.REMOTE_RECONCILIATION_CONFIRMATION,
    )
    _prepare_replacement_mr_approval(repo_root, approved_file, first)
    provider_calls: list[str] = []

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]
    assert yaml.safe_load(first_reservation.claim_path.read_text(encoding="utf-8"))["state"] == "retryable"


def test_confirmed_mr_claim_is_not_selected_while_an_unresolved_claim_exists(
    repo_root: Path,
    approved_file: Path,
) -> None:
    grant = prepare_replayable_mr(repo_root, approved_file)
    reservation = approval.reserve_remote_action(repo_root, grant)
    confirmed = approval.confirm_remote_action(
        repo_root,
        reservation,
        target_issue=None,
        provider_receipt={"number": "42", "html_url": "https://example.invalid/pulls/42"},
    )
    unresolved_id = "f" * 64
    unresolved_path = approval._remote_claim_path(repo_root, "202", unresolved_id)
    unresolved = yaml.safe_load(confirmed.claim_path.read_text(encoding="utf-8"))
    unresolved.update(
        {
            "approvalId": unresolved_id,
            "remoteClaimFile": unresolved_path.relative_to(repo_root).as_posix(),
            "state": "reserved",
            "targetIssue": "none",
            "providerReceipt": "none",
            "failureReason": "none",
            "recordedAt": "none",
            "historyFile": "none",
            "historySha256": "none",
        }
    )
    unresolved_path.write_bytes(approval._remote_claim_bytes(unresolved))
    provider_calls: list[str] = []

    assert run_replayable_mr(repo_root, provider_calls) == 1
    assert provider_calls == []
    assert not cli.branch_meta(repo_root, "pr")
    assert yaml.safe_load(confirmed.claim_path.read_text(encoding="utf-8"))["state"] == "post-effects-pending"
    assert yaml.safe_load(unresolved_path.read_text(encoding="utf-8"))["state"] == "reserved"


def test_mr_replay_completes_partial_current_task_fields(repo_root: Path, approved_file: Path) -> None:
    prepare_replayable_mr(repo_root, approved_file)
    provider_calls: list[str] = []

    def write_partial_task(root: Path, issue: str, number: str, _url: str) -> list[Path]:
        path = root / ".xflow" / "current-task.md"
        path.write_text(
            f"# XFlow Current Task\n\nIssue: {issue}\nState: S9_REMOTE_REVIEW_AND_CI\n\n"
            f"## Remote Review\nPR: {number}\n",
            encoding="utf-8",
            newline="\n",
        )
        raise RuntimeError("injected failure after partial current-task metadata")

    try:
        run_replayable_mr(repo_root, provider_calls, update_task=write_partial_task)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected partial current-task failure")

    assert run_replayable_mr(repo_root, provider_calls) == 0
    assert provider_calls == ["create"]
    current_task = (repo_root / ".xflow" / "current-task.md").read_text(encoding="utf-8")
    assert current_task.count("PR: 42") == 1
    assert current_task.count("PR URL: https://example.invalid/pulls/42") == 1


def test_current_task_pr_fields_merge_and_conflict(repo_root: Path) -> None:
    current_task = repo_root / ".xflow" / "current-task.md"
    write(
        current_task,
        "# XFlow Current Task\n\nIssue: 202\nState: S9_REMOTE_REVIEW_AND_CI\n\n## Remote Review\nPR: 42\n",
    )
    assert cli.update_current_task_for_pr(
        repo_root,
        "202",
        "42",
        "https://example.invalid/pulls/42",
    ) == [current_task]
    merged = current_task.read_text(encoding="utf-8")
    assert merged.count("PR: 42") == 1
    assert merged.count("PR URL: https://example.invalid/pulls/42") == 1
    write(
        current_task,
        merged.replace("https://example.invalid/pulls/42", "https://example.invalid/pulls/99"),
    )
    assert_value_error(
        "conflicting current task PR URL",
        lambda: cli.update_current_task_for_pr(
            repo_root,
            "202",
            "42",
            "https://example.invalid/pulls/42",
        ),
    )


def test_skipped_backfill_has_no_effect(repo_root: Path, approved_file: Path) -> None:
    from xflow.cli import commit_and_push_pr_backfill, record_backfill_effect_if_confirmed

    review = approval.prepare(repo_root, "202", "git-mr", approved_file, force=True)
    approve(review)
    grant = approval.require_remote(repo_root, "git-mr", approved_file, "202")
    approval.record_consumed_approval(repo_root, grant, "success")
    suggestion = repo_root / ".xflow" / "issues" / "issue-202" / "pr-state-update.md"
    write(suggestion, "# PR State Update\n\nPR: 42\n")
    previous = os.environ.get("DEVCTL_SKIP_PUSH")
    os.environ["DEVCTL_SKIP_PUSH"] = "1"
    try:
        push_result = commit_and_push_pr_backfill(
            repo_root,
            "feature/202-skipped-backfill",
            [suggestion],
            "42",
            "202",
        )
    finally:
        if previous is None:
            os.environ.pop("DEVCTL_SKIP_PUSH", None)
        else:
            os.environ["DEVCTL_SKIP_PUSH"] = previous
    assert push_result is not None
    assert not push_result.performed
    assert not push_result.success
    recorded = record_backfill_effect_if_confirmed(repo_root, grant, push_result)
    assert recorded is None
    history = repo_root / ".xflow" / "issues" / "issue-202" / "approvals" / "history"
    payloads = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in history.glob("*.yaml")]
    assert len(payloads) == 1
    assert payloads[0]["action"] == "git-mr"


def test_final_issue_and_effect_records(repo_root: Path, approved_file: Path) -> None:
    activate(repo_root, "draft")
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: draft
State: G1_APPROVE_ISSUE_CREATE

## Allowed Actions
- Create the approved issue.

## Forbidden Actions
- Push unreviewed changes.
""",
    )
    draft_file = repo_root / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(draft_file, "<!-- xflow: issue-draft -->\n\nApproved draft.\n")
    review = approval.prepare(repo_root, "draft", "issue-create", draft_file, force=True)
    approve(review)
    create_grant = approval.require_remote(repo_root, "issue-create", draft_file, "draft")
    assert_value_error(
        "provider-confirmed non-draft target",
        lambda: approval.record_consumed_approval(repo_root, create_grant, "success"),
    )
    assert_value_error(
        "provider-confirmed non-draft target",
        lambda: approval.record_consumed_approval(repo_root, create_grant, "success", target_issue="draft"),
    )
    record = approval.record_consumed_approval(repo_root, create_grant, "success", target_issue="#42")
    payload = yaml.safe_load(record.read_text(encoding="utf-8"))
    assert record.parent == repo_root / ".xflow" / "issues" / "issue-42" / "approvals" / "history"
    assert payload["issue"] == "42"
    assert payload["approvalIssue"] == "draft"
    assert not (repo_root / ".xflow" / "issues" / "issue-draft" / "approvals" / "history").exists()

    activate(repo_root, "202")
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 202
State: S9_REMOTE_REVIEW_AND_CI

## Allowed Actions
- Create the approved MR.

## Forbidden Actions
- Push unreviewed changes.
""",
    )
    review = approval.prepare(repo_root, "202", "git-mr", approved_file, force=True)
    approve(review)
    mr_grant = approval.require_remote(repo_root, "git-mr", approved_file, "202")
    assert_value_error(
        "consumed git-mr parent approval",
        lambda: approval.record_subordinate_effect(repo_root, mr_grant, "git-state-backfill", "success"),
    )
    approval.record_consumed_approval(repo_root, mr_grant, "success")
    effect = approval.record_subordinate_effect(repo_root, mr_grant, "git-state-backfill", "success")
    effect_payload = yaml.safe_load(effect.read_text(encoding="utf-8"))
    assert effect_payload["source"] == "effect"
    assert effect_payload["parentAction"] == "git-mr"
    assert effect_payload["parentApprovalId"] == mr_grant.approval_id
    assert effect_payload["reviewerSummary"] == "subordinate-effect"
    assert_value_error(
        "effect already recorded",
        lambda: approval.record_subordinate_effect(repo_root, mr_grant, "git-state-backfill", "success"),
    )
    for field_name, invalid_value in (
        ("action", "git-push"),
        ("parentAction", "git-push"),
        ("reviewerSummary", "human reviewer"),
        ("approvalId", "c" * 64),
        ("approvedFile", "different-parent-artifact.md"),
    ):
        invalid_effect = dict(effect_payload)
        invalid_effect[field_name] = invalid_value
        effect.write_text(yaml.safe_dump(invalid_effect, sort_keys=False), encoding="utf-8")
        assert_value_error(
            "approval history integrity error",
            lambda: approval.require_remote(repo_root, "git-mr", approved_file, "202"),
        )
    effect.write_text(yaml.safe_dump(effect_payload, sort_keys=False), encoding="utf-8")
    assert_value_error(
        "subordinate effect of git-mr",
        lambda: approval.record_subordinate_effect(repo_root, replace(mr_grant, action="git-push"), "git-state-backfill", "success"),
    )


def test_credential_branch_is_rejected(repo_root: Path, approved_file: Path) -> None:
    git(repo_root, "checkout", "-b", "feature/ghp_12345678", "-q")
    activate(repo_root, "202")
    review = approval.prepare(repo_root, "202", "git-push", approved_file, force=True)
    approve(review)
    assert_value_error(
        "credential-like text",
        lambda: approval.require_remote(repo_root, "git-push", approved_file, "202"),
    )


def test_legacy_pr_merge_review(repo_root: Path) -> None:
    git(repo_root, "config", "extensions.worktreeConfig", "true")
    git(repo_root, "config", "--worktree", "devctl.issue", "1")
    git(repo_root, "config", "--worktree", "devctl.base", "main")
    git(repo_root, "config", "--worktree", "devctl.pr", "42")
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 1
State: S9_REMOTE_REVIEW_AND_CI

## Allowed Actions
- Merge the approved PR.

## Forbidden Actions
- Push unreviewed changes.
""",
    )
    mr_file = repo_root / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
    write(mr_file, "<!-- xflow: mr-draft -->\n\nLegacy PR merge evidence.\n")
    pointer = active_task_pointer_file(repo_root, resolve_bindings(repo_root).worktree)
    assert not pointer.exists()
    review = approval.prepare(repo_root, "1", "git-pr-merge", mr_file, reviewer="human reviewer")
    approve(review)
    assert approval.require_remote_or_unattended(repo_root, "git-pr-merge", mr_file, "1").source == "local-review"


def test_legacy_draft_ignores_unrelated_pr_metadata(repo_root: Path) -> None:
    git(repo_root, "config", "extensions.worktreeConfig", "true")
    git(repo_root, "config", "--worktree", "devctl.pr", "9")
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 1
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Push the approved branch.

## Forbidden Actions
- Push unreviewed changes.
""",
    )
    draft_file = repo_root / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
    write(draft_file, "<!-- xflow: issue-draft -->\n\nLegacy draft evidence.\n")
    review = approval.prepare(repo_root, "draft", "issue-create", draft_file, reviewer="human reviewer")
    approve(review)
    assert_value_error(
        "current task Issue mismatch",
        lambda: approval.require_remote(repo_root, "issue-create", draft_file, "draft"),
    )
    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: draft
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Create the approved issue.

## Forbidden Actions
- Push unreviewed changes.
""",
    )
    approval.require_remote(repo_root, "issue-create", draft_file, "draft")

    write(
        repo_root / ".xflow" / "current-task.md",
        """# XFlow Current Task

Issue: 1
State: G5_APPROVE_MR_CREATE

## Allowed Actions
- Push the approved branch.

## Forbidden Actions
- Create unreviewed remote writes.
""",
    )
    git_file = repo_root / ".xflow" / "issues" / "issue-1" / "walkthrough.md"
    write(git_file, "# Walkthrough\n\nGit approval evidence.\n")
    review = approval.prepare(repo_root, "1", "git-push", git_file, reviewer="human reviewer")
    approve(review)
    assert_value_error(
        "stale current task state",
        lambda: approval.require_remote(repo_root, "git-push", git_file, "1"),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree_a = root / "worktree-a"
        worktree_b = root / "worktree-b"
        repository_c = root / "repository-c"
        legacy_repo = root / "legacy-pr-merge"
        legacy_draft_repo = root / "legacy-draft"
        git(root, "init", "-q", str(worktree_a))
        git(worktree_a, "config", "user.email", "test@example.com")
        git(worktree_a, "config", "user.name", "Test User")
        git(worktree_a, "checkout", "-b", "main", "-q")
        write(worktree_a / "README.md", "# Demo\n")
        git(worktree_a, "add", "README.md")
        git(worktree_a, "commit", "-m", "init", "-q")
        git(worktree_a, "checkout", "-b", "feature/202-a", "-q")
        git(worktree_a, "worktree", "add", "-b", "feature/202-b", str(worktree_b), "main")

        a_file = worktree_a / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        b_file = worktree_b / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        write(a_file, "# Walkthrough\n\nApproved artifact.\n")
        write(b_file, a_file.read_text(encoding="utf-8"))
        activate(worktree_a, "202")
        activate(worktree_b, "202")
        review_a = approval.prepare(worktree_a, "202", "git-push", a_file, reviewer="human reviewer")
        approve(review_a)
        review_b = worktree_b / ".xflow" / "issues" / "issue-202" / "approvals" / "local-review.md"
        review_b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_b)

        assert_value_error(
            "approval worktree mismatch",
            lambda: approval.require_remote(worktree_b, "git-push", b_file, "202"),
        )

        git(worktree_a, "checkout", "-b", "feature/202-switched", "-q")
        assert_value_error(
            "approval branch mismatch",
            lambda: approval.require_remote(worktree_a, "git-push", a_file, "202"),
        )
        git(worktree_a, "checkout", "feature/202-a", "-q")

        activate(worktree_a, "303")
        review_303 = worktree_a / ".xflow" / "issues" / "issue-303" / "approvals" / "local-review.md"
        review_303.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_303)
        assert_value_error(
            "approval Issue mismatch",
            lambda: approval.require_remote(worktree_a, "git-push", a_file, "303"),
        )

        git(root, "init", "-q", str(repository_c))
        git(repository_c, "config", "user.email", "test@example.com")
        git(repository_c, "config", "user.name", "Test User")
        git(repository_c, "checkout", "-b", "feature/202-a", "-q")
        write(repository_c / "README.md", "# Demo\n")
        git(repository_c, "add", "README.md")
        git(repository_c, "commit", "-m", "init", "-q")
        c_file = repository_c / ".xflow" / "issues" / "issue-202" / "walkthrough.md"
        write(c_file, a_file.read_text(encoding="utf-8"))
        activate(repository_c, "202")
        review_c = repository_c / ".xflow" / "issues" / "issue-202" / "approvals" / "local-review.md"
        review_c.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(review_a, review_c)
        assert_value_error(
            "approval repository mismatch",
            lambda: approval.require_remote(repository_c, "git-push", c_file, "202"),
        )

        activate(worktree_a, "202")
        assert_value_error(
            "action mismatch: expected git-mr, got git-push",
            lambda: approval.require_remote(worktree_a, "git-mr", a_file, "202"),
        )
        assert_value_error(
            "contract-acceptance is not eligible for unattended mode",
            lambda: approval.require_remote_or_unattended(
                worktree_a, "contract-acceptance", a_file, "202", request_unattended=True
            ),
        )
        test_consumed_record(worktree_a, a_file)

        integrity_repo, integrity_file = init_active_repo(root, "history-integrity")
        test_history_integrity(integrity_repo, integrity_file)
        masquerade_repo, masquerade_file = init_active_repo(root, "effect-masquerade")
        test_effect_masquerade_fails_closed(masquerade_repo, masquerade_file)
        path_repo, path_file = init_active_repo(root, "history-paths")
        test_history_path_validation(path_repo, path_file)
        credential_repo, credential_file = init_active_repo(root, "credential-safety")
        test_credential_safety(credential_repo, credential_file)
        skipped_repo, skipped_file = init_active_repo(root, "skipped-push")
        test_skipped_git_push_has_no_history(skipped_repo, skipped_file)
        unattended_repo, unattended_file = init_active_repo(root, "unattended-grants")
        test_unattended_grants_are_per_execution(unattended_repo, unattended_file)
        reservation_repo, reservation_file = init_active_repo(root, "remote-reservation")
        test_local_remote_reservation_snapshot_and_recovery(reservation_repo, reservation_file)
        posix_lock_repo, _ = init_active_repo(root, "posix-claim-lock")
        test_posix_claim_lock_keeps_third_process_behind_waiter(posix_lock_repo)
        provider_repo, provider_file = init_active_repo(root, "remote-provider-snapshot")
        test_issue_comment_provider_consumes_reserved_bytes(provider_repo, provider_file)
        reconcile_repo, reconcile_file = init_active_repo(root, "remote-reconcile-cli")
        test_remote_reconciliation_cli_requires_exact_human_confirmation(reconcile_repo, reconcile_file)
        backfill_repo, backfill_file = init_active_repo(root, "remote-mr-backfill")
        test_reserved_mr_history_can_parent_confirmed_backfill(backfill_repo, backfill_file)
        replay_repo, replay_file = init_active_repo(root, "remote-mr-replay")
        test_mr_replay_after_provider_confirmation_finishes_local_effects(replay_repo, replay_file)
        partial_repo, partial_file = init_active_repo(root, "remote-mr-partial")
        test_mr_replay_converges_after_partial_metadata(partial_repo, partial_file)
        local_commit_repo, local_commit_file, local_commit_origin = init_remote_mr_repo(root, "remote-mr-local-commit")
        test_mr_replay_pushes_existing_local_backfill_commit(
            local_commit_repo,
            local_commit_file,
            local_commit_origin,
        )
        pushed_repo, pushed_file, pushed_origin = init_remote_mr_repo(root, "remote-mr-pushed")
        test_mr_replay_verifies_already_pushed_backfill_before_history(pushed_repo, pushed_file, pushed_origin)
        conflict_repo, conflict_file = init_active_repo(root, "remote-mr-conflict")
        test_mr_replay_rejects_conflicting_pr_identity(conflict_repo, conflict_file)
        incomplete_repo, incomplete_file = init_active_repo(root, "remote-mr-incomplete")
        test_mr_without_successful_backfill_stays_nonterminal(incomplete_repo, incomplete_file, None)
        skipped_repo, skipped_file = init_active_repo(root, "remote-mr-skipped")
        test_mr_without_successful_backfill_stays_nonterminal(
            skipped_repo,
            skipped_file,
            cli.PushResult(performed=False, success=False),
        )
        gate_repo, gate_file = init_active_repo(root, "remote-mr-effect-gate")
        test_mr_terminal_gate_requires_unique_backfill_effect(gate_repo, gate_file)
        mutated_repo, mutated_file = init_active_repo(root, "remote-mr-mutated-body")
        test_mr_replay_uses_sealed_body_when_mutable_body_changes(mutated_repo, mutated_file)
        deleted_repo, deleted_file = init_active_repo(root, "remote-mr-deleted-body")
        test_mr_replay_uses_sealed_body_when_mutable_body_is_deleted(deleted_repo, deleted_file)
        claim_conflict_repo, claim_conflict_file = init_active_repo(root, "remote-mr-claim-conflict")
        test_pending_mr_claim_discovery_rejects_conflicting_body_path(claim_conflict_repo, claim_conflict_file)
        multiple_repo, multiple_file = init_active_repo(root, "remote-mr-multiple-claims")
        test_pending_mr_claim_discovery_rejects_multiple_matches(multiple_repo, multiple_file)
        reserved_scope_repo, reserved_scope_file = init_active_repo(root, "remote-mr-reserved-scope")
        test_reserved_mr_claim_blocks_replacement_approval_provider_call(
            reserved_scope_repo,
            reserved_scope_file,
        )
        unknown_scope_repo, unknown_scope_file = init_active_repo(root, "remote-mr-unknown-scope")
        test_unknown_mr_claim_blocks_replacement_approval_provider_call(
            unknown_scope_repo,
            unknown_scope_file,
        )
        retryable_scope_repo, retryable_scope_file = init_active_repo(root, "remote-mr-retryable-scope")
        test_human_confirmed_no_effect_allows_replacement_mr_approval(
            retryable_scope_repo,
            retryable_scope_file,
        )
        mixed_scope_repo, mixed_scope_file = init_active_repo(root, "remote-mr-mixed-scope")
        test_confirmed_mr_claim_is_not_selected_while_an_unresolved_claim_exists(
            mixed_scope_repo,
            mixed_scope_file,
        )
        partial_task_repo, partial_task_file = init_active_repo(root, "remote-mr-partial-task")
        test_mr_replay_completes_partial_current_task_fields(partial_task_repo, partial_task_file)
        task_merge_repo, _task_merge_file = init_active_repo(root, "remote-mr-task-merge")
        test_current_task_pr_fields_merge_and_conflict(task_merge_repo)
        skipped_backfill_repo, skipped_backfill_file = init_active_repo(root, "skipped-backfill")
        test_skipped_backfill_has_no_effect(skipped_backfill_repo, skipped_backfill_file)
        final_repo, final_file = init_active_repo(root, "final-issue-effect")
        test_final_issue_and_effect_records(final_repo, final_file)
        branch_repo, branch_file = init_active_repo(root, "credential-branch")
        test_credential_branch_is_rejected(branch_repo, branch_file)

        git(root, "init", "-q", str(legacy_repo))
        git(legacy_repo, "config", "user.email", "test@example.com")
        git(legacy_repo, "config", "user.name", "Test User")
        git(legacy_repo, "checkout", "-b", "main", "-q")
        write(legacy_repo / "README.md", "# Legacy\n")
        git(legacy_repo, "add", "README.md")
        git(legacy_repo, "commit", "-m", "init", "-q")
        git(legacy_repo, "checkout", "-b", "feature/1-recorded-merge", "-q")
        test_legacy_pr_merge_review(legacy_repo)

        git(root, "init", "-q", str(legacy_draft_repo))
        git(legacy_draft_repo, "config", "user.email", "test@example.com")
        git(legacy_draft_repo, "config", "user.name", "Test User")
        git(legacy_draft_repo, "checkout", "-b", "main", "-q")
        write(legacy_draft_repo / "README.md", "# Legacy draft\n")
        git(legacy_draft_repo, "add", "README.md")
        git(legacy_draft_repo, "commit", "-m", "init", "-q")
        test_legacy_draft_ignores_unrelated_pr_metadata(legacy_draft_repo)

    print("approval binding ok")


if __name__ == "__main__":
    main()
