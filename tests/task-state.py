from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace as dataclass_replace
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import yaml


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import approval, unattended
from xflow import cli as cli_module
from xflow import local_artifacts as local_artifacts_module
from xflow.bindings import resolve_bindings
from xflow.checks import check_current_task
from xflow.cli import current_task_issue
from xflow.bindings import git_path
from xflow.collaboration import (
    git_child_environment,
    inherited_lease_command,
    repository_lock,
    repository_mutation,
)
from xflow.contracts import load_contract, validate_contract_acceptance
from xflow.env import RuntimeContext
from xflow.paths import active_task_pointer_file, legacy_active_task_pointer_file
from xflow.task_state import (
    TaskState,
    activate_task,
    check_task_binding,
    list_task_states,
    load_active_task,
    migrate_legacy_current_task,
    parse_task_state,
    render_task_state,
)


def git(repo_root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr or result.stdout


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


def state(issue: str, branch: str, *, phase: str = "classified", approval: str = "none") -> TaskState:
    return TaskState(
        issue=issue,
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase=phase,
        classification="capability-change",
        contract="example.contract.capability-name@0.1.0",
        contract_file="docs/requirements/example/contract.yaml",
        contract_change_required=True,
        branch=branch,
        base="main",
        allowed_actions=("clarify-contract", "prepare-verification"),
        forbidden_actions=("edit-implementation", "push", "create-mr"),
        human_gate="capability design acceptance required",
        human_approval_ref=approval,
    )


def write_state(repo_root: Path, value: TaskState) -> Path:
    path = repo_root / ".xflow" / "issues" / f"issue-{value.issue}" / "task-state.md"
    write(path, render_task_state(value))
    return path


def run_devctl_result(
    repo_root: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEVCTL_REPO_ROOT": str(repo_root),
        "DEVCTL_SKIP_PROVIDER_LOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
        "XFLOW_COLLABORATION_LOCK_TIMEOUT": "0.2",
        **(env_overrides or {}),
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


def run_devctl(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = run_devctl_result(repo_root, *args)
    assert result.returncode == 0, result.stderr
    return result


def authority_file(repo_root: Path, issue: str) -> Path:
    bindings = resolve_bindings(repo_root)
    return (
        git_path(repo_root, "--git-common-dir")
        / "xflow"
        / "local"
        / "worktrees"
        / bindings.worktree
        / "issues"
        / f"issue-{issue}"
        / "authority.json"
    )


def v1_pointer(payload: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "repository": payload["repository"],
        "worktree": payload["worktree"],
        "branch": payload["branch"],
        "issue": payload["issue"],
        "activatedAt": payload["activatedAt"],
    }


def task_artifact_bytes(repo_root: Path, issue: str) -> dict[Path, bytes | None]:
    bindings = resolve_bindings(repo_root)
    paths = (
        active_task_pointer_file(repo_root, bindings.worktree),
        legacy_active_task_pointer_file(repo_root, bindings.worktree),
        authority_file(repo_root, issue),
        repo_root / ".xflow" / "issues" / f"issue-{issue}" / "task-state.md",
    )
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def start_mutation_owner(repo_root: Path) -> tuple[subprocess.Popen[str], str]:
    script = """
import sys
import time
from pathlib import Path
from xflow.collaboration import git_child_environment, repository_mutation

repo = Path(sys.argv[1])
with repository_mutation(repo):
    print(git_child_environment(repo)["XFLOW_DEVCTL_MUTATION_LEASE"], flush=True)
    time.sleep(60)
"""
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(repo_root)],
        cwd=repo_root,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    token = process.stdout.readline().strip()
    if len(token) != 64:
        assert process.stderr is not None
        raise AssertionError(process.stderr.read())
    return process, token


def test_git_hook_devctl_reentry(root: Path) -> None:
    repo = root / "hook-reentry"
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "feature/77-hook", "-q")
    write(repo / "README.md", "# Hook reentry\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize hook fixture", "-q")
    write_state(
        repo,
        dataclass_replace(
            state("77", "feature/77-hook"),
            classification="ui-defect",
            contract_change_required=False,
        ),
    )
    activate_task(repo, "77")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "77")

    hook_result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks/pre-commit"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hook = Path(hook_result.stdout.strip())
    if not hook.is_absolute():
        hook = repo / hook
    python_executable = Path(sys.executable).as_posix()
    write(hook, f'#!/bin/sh\n"{python_executable}" -m xflow hook task-status >/dev/null\n')
    hook.chmod(0o755)
    write(repo / "README.md", "# Hook reentry\n\nchanged\n")
    run_devctl(repo, "git", "commit-msg", "-a", "-c", "验证钩子重入")


def test_git_hook_requires_retained_contract_acceptance(root: Path) -> None:
    repo = initialized_repo(root, "hook-contract-acceptance", "feature/507-hook-acceptance")
    issue = "507"
    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"contracts"}}\n')
    contract_path = repo / "contracts" / "contract.yaml"
    fixture = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
    write(contract_path, fixture.read_text(encoding="utf-8"))
    contract = load_contract(repo, contract_path)
    classified = dataclass_replace(
        state(issue, "feature/507-hook-acceptance"),
        contract_file="contracts/contract.yaml",
    )
    write_state(repo, classified)
    activate_task(repo, issue)

    accepted_objects = (str(contract.raw["id"]),)
    review = approval.prepare(
        repo,
        issue,
        "contract-acceptance",
        contract.path,
        reviewer="human reviewer",
        force=True,
        accepted_objects=accepted_objects,
    )
    write(review, review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    history = validate_contract_acceptance(repo, issue, contract, accepted_objects)
    issue_root = repo / ".xflow" / "issues" / f"issue-{issue}"
    history_reference = history.relative_to(issue_root).as_posix()
    write_state(
        repo,
        dataclass_replace(
            classified,
            semantic_phase="accepted-design",
            human_approval_ref=history_reference,
        ),
    )
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", issue)

    hook_result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks/pre-commit"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hook = Path(hook_result.stdout.strip())
    if not hook.is_absolute():
        hook = repo / hook
    python_executable = Path(sys.executable).as_posix()
    write(hook, f'#!/bin/sh\n"{python_executable}" -m xflow hook task-status >/dev/null\n')
    hook.chmod(0o755)

    write(repo / "README.md", "# hook-contract-acceptance\n\nvalid acceptance\n")
    valid_commit = run_devctl_result(repo, "git", "commit-msg", "-a", "-c", "验证合同验收钩子")
    assert valid_commit.returncode == 0, valid_commit.stderr
    assert "[INFO] committed" in valid_commit.stdout

    record = approval.parse_contract_acceptance_history(repo, history)
    archived_review = issue_root / str(record["approvedReviewFile"])
    claim = issue_root / str(record["approvalClaimFile"])
    bindings = resolve_bindings(repo)
    protected_paths = (
        repo / ".xflow" / "xflow.json",
        active_task_pointer_file(repo, bindings.worktree),
        legacy_active_task_pointer_file(repo, bindings.worktree),
        authority_file(repo, issue),
        issue_root / "task-state.md",
        contract_path,
        history,
        archived_review,
        claim,
    )

    def protected_bytes() -> dict[Path, bytes | None]:
        return {path: path.read_bytes() if path.exists() else None for path in protected_paths}

    def assert_invalid_acceptance_rejects(label: str) -> None:
        before = protected_bytes()
        ordinary = run_devctl_result(repo, "task", "status")
        assert ordinary.returncode == 1, ordinary.stdout
        assert "missing matching human contract acceptance" in ordinary.stderr

        with repository_mutation(repo):
            token = git_child_environment(repo)["XFLOW_DEVCTL_MUTATION_LEASE"]
            hook_status = run_devctl_result(
                repo,
                "hook",
                "task-status",
                env_overrides={"XFLOW_DEVCTL_MUTATION_LEASE": token},
            )
        assert hook_status.returncode == 1, hook_status.stdout
        assert "missing matching human contract acceptance" in hook_status.stderr

        head_before = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
        ).stdout.strip()
        write(repo / "README.md", f"# hook-contract-acceptance\n\n{label}\n")
        commit = run_devctl_result(repo, "git", "commit-msg", "-a", "-c", f"拒绝{label}验收")
        assert commit.returncode == 1, commit.stdout
        assert "missing matching human contract acceptance" in commit.stderr
        head_after = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
        ).stdout.strip()
        assert head_after == head_before
        assert protected_bytes() == before

    history_bytes = history.read_bytes()
    history.unlink()
    assert_invalid_acceptance_rejects("删除")
    history.write_bytes(history_bytes)
    history.write_bytes(history_bytes + b"forged: true\n")
    assert_invalid_acceptance_rejects("篡改")


def test_official_git_start_respects_closure_lock(root: Path) -> None:
    repo = root / "git-start-lock"
    origin = root / "git-start-origin.git"
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# Git mutation lock\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize mutation fixture", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")

    with repository_lock(repo):
        result = run_devctl_result(
            repo,
            "git",
            "start",
            "parallel-mutation",
            "--issue",
            "77",
            "--base",
            "main",
        )
    assert result.returncode == 1, result.stdout
    assert "another devctl process holds the repository collaboration lock" in result.stderr
    assert resolve_bindings(repo).branch == "main"

    with repository_lock(repo):
        result = run_devctl_result(repo, "git", "done", "--force", "--issue", "77")
    assert result.returncode == 1, result.stdout
    assert "another devctl process holds the repository collaboration lock" in result.stderr
    assert resolve_bindings(repo).branch == "main"

    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "devctl.issue", "77")
    write(repo / "README.md", "# Git mutation lock\n\nstaging contention\n")
    with repository_lock(repo):
        result = run_devctl_result(repo, "git", "commit-msg", "-a", "验证暂存互斥")
    assert result.returncode == 1, result.stdout
    assert "another devctl process holds the repository collaboration lock" in result.stderr
    assert subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
        check=False,
    ).returncode == 0


def test_first_capability_task_establishes_final_branch_before_acceptance(root: Path) -> None:
    origin = root / "first-capability-origin.git"
    repo = root / "first-capability"
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", "# First capability\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize first capability fixture", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")

    issue = "701"
    final_branch = "feat/701-first-capability"
    candidate = dataclass_replace(
        state(issue, final_branch),
        semantic_phase="declaring",
        human_gate="final task branch identity approval required",
    )
    state_path = write_state(repo, candidate)
    write(
        state_path.with_name("classification.yaml"),
        """version: 0.1.0
request:
  originalStatement: Add one participant-visible capability.
contractSearch:
  status: not-found
  refs: []
classification: capability-change
contractChangeRequired: true
reason: The request adds a participant-visible result.
nextArtifact: contract-change-proposal.md
decisionSource: ai-proposed
""",
    )
    review = approval.prepare(
        repo,
        issue,
        "task-branch-start",
        state_path,
        reviewer="human reviewer",
        force=True,
    )
    write(review, review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))

    started = run_devctl_result(
        repo,
        "git",
        "start",
        "first-capability",
        "--issue",
        issue,
        "--base",
        "main",
        "--file",
        str(state_path),
    )
    assert started.returncode == 0, started.stderr
    assert resolve_bindings(repo).branch == final_branch
    assert load_active_task(repo).issue == issue
    assert load_active_task(repo).semantic_phase == "declaring"
    remote_branch = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin", final_branch],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    assert remote_branch.stdout.strip() == ""

    write_state(repo, dataclass_replace(candidate, execution_state="S4_TDD_AND_IMPLEMENTATION"))
    assert_value_error("capability-change requires accepted-design", lambda: check_current_task(repo, issue))
    write_state(repo, candidate)

    write(repo / ".xflow" / "xflow.json", '{"contracts":{"root":"docs/requirements"}}\n')
    contract_path = repo / "docs" / "requirements" / "example" / "contract.yaml"
    fixture = Path(__file__).parent / "fixtures" / "contracts" / "valid.yaml"
    write(contract_path, fixture.read_text(encoding="utf-8"))
    contract = load_contract(repo, contract_path)
    accepted_objects = (str(contract.raw["id"]),)
    contract_review = approval.prepare(
        repo,
        issue,
        "contract-acceptance",
        contract_path,
        reviewer="human reviewer",
        force=True,
        accepted_objects=accepted_objects,
    )
    write(
        contract_review,
        contract_review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"),
    )
    acceptance = validate_contract_acceptance(repo, issue, contract, accepted_objects)
    acceptance_record = approval.parse_contract_acceptance_history(repo, acceptance)
    assert acceptance_record["branch"] == final_branch
    acceptance_ref = acceptance.relative_to(state_path.parent).as_posix()
    accepted = dataclass_replace(
        candidate,
        execution_state="S4_TDD_AND_IMPLEMENTATION",
        semantic_phase="accepted-design",
        human_gate="development start approval required",
        human_approval_ref=acceptance_ref,
    )
    write_state(repo, accepted)
    check_current_task(repo, issue)

    branch_records = tuple((state_path.parent / "approvals" / "history").glob("*-task-branch-start-*.yaml"))
    assert len(branch_records) == 1
    branch_record = yaml.safe_load(branch_records[0].read_text(encoding="utf-8"))
    assert branch_record["branch"] == "main"
    assert branch_record["targetBranch"] == final_branch


def capability_branch_start_fixture(
    root: Path,
    name: str,
    issue: str,
    slug: str,
) -> tuple[Path, Path, Path, str, RuntimeContext, SimpleNamespace]:
    origin = root / f"{name}-origin.git"
    repo = root / name
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", "main", "-q")
    write(repo / "README.md", f"# {name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", f"test: initialize {name}", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-u", "origin", "main", "-q")

    target = f"feat/{issue}-{slug}"
    state_path = write_state(
        repo,
        dataclass_replace(
            state(issue, target),
            semantic_phase="declaring",
            human_gate="final task branch identity approval required",
        ),
    )
    write(
        state_path.with_name("classification.yaml"),
        """version: 0.1.0
request:
  originalStatement: Add one participant-visible capability.
contractSearch:
  status: not-found
  refs: []
classification: capability-change
contractChangeRequired: true
reason: The request adds a participant-visible result.
nextArtifact: contract-change-proposal.md
decisionSource: ai-proposed
""",
    )
    review = approval.prepare(
        repo,
        issue,
        "task-branch-start",
        state_path,
        reviewer="human reviewer",
        force=True,
    )
    write(review, review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    ctx = RuntimeContext(tool_root=OPS_ROOT, repo_root=repo.resolve(), product_line="")
    args = SimpleNamespace(base="main", slug=slug, issue=issue, file=state_path)
    return repo, state_path, review, target, ctx, args


def task_branch_claim(repo_root: Path, issue: str) -> tuple[Path, dict[str, object]]:
    claims = tuple(
        (repo_root / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "history" / "claims").glob(
            "*-task-branch.yaml"
        )
    )
    assert len(claims) == 1, claims
    return claims[0], yaml.safe_load(claims[0].read_text(encoding="utf-8"))


def invoke_git_start_cli(repo_root: Path, args: SimpleNamespace) -> SimpleNamespace:
    argv = [
        "git",
        "start",
        args.slug,
        "--issue",
        args.issue,
        "--base",
        args.base,
        "--file",
        str(args.file),
    ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(
        os.environ,
        {
            "DEVCTL_REPO_ROOT": str(repo_root),
            "DEVCTL_SKIP_PROVIDER_LOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "XFLOW_COLLABORATION_LOCK_TIMEOUT": "0.2",
        },
        clear=False,
    ):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = cli_module.main(argv)
    return SimpleNamespace(returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def assert_cli_mutation_lease(repo_root: Path) -> None:
    child_env = git_child_environment(repo_root)
    assert len(child_env.get("XFLOW_DEVCTL_MUTATION_LEASE", "")) == 64


def advance_origin(root: Path, origin: Path, name: str, content: str) -> str:
    updater = root / name
    git(root, "clone", "-q", "--branch", "main", str(origin), str(updater))
    git(updater, "config", "user.email", "test@example.com")
    git(updater, "config", "user.name", "Test User")
    write(updater / "README.md", content)
    git(updater, "add", "README.md")
    git(updater, "commit", "-m", f"test: {name}", "-q")
    commit = subprocess.run(
        ["git", "-C", str(updater), "rev-parse", "HEAD"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    git(updater, "push", "origin", "main", "-q")
    return commit


def assert_task_branch_start_revalidates_exact_bytes_after_pull(
    root: Path,
    suffix: str,
    issue: str,
    selected: str,
) -> None:
    repo, state_path, review, target, ctx, args = capability_branch_start_fixture(
        root,
        f"branch-exact-{suffix}",
        issue,
        f"exact-{suffix}",
    )
    mutate_path = state_path if selected == "state" else review
    mutated = False
    original_revalidate = approval.revalidate_task_branch_start

    def mutate_before_branch(
        repo_root: Path,
        reservation: approval.TaskBranchStartReservation,
    ) -> approval.TaskBranchStartReservation:
        nonlocal mutated
        assert_cli_mutation_lease(repo_root)
        if not mutated:
            mutate_path.write_bytes(mutate_path.read_bytes() + b"\n")
            mutated = True
        return original_revalidate(repo_root, reservation)

    with patch.object(approval, "revalidate_task_branch_start", side_effect=mutate_before_branch):
        result = invoke_git_start_cli(repo, args)
    assert result.returncode == 1, result.stdout
    assert "exact approved" in result.stderr, result.stderr
    assert mutated
    assert resolve_bindings(repo).branch == "main"
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{target}"],
        check=False,
    ).returncode != 0


def test_task_branch_start_revalidates_exact_task_state_after_pull(root: Path) -> None:
    assert_task_branch_start_revalidates_exact_bytes_after_pull(root, "task-state", "711", "state")


def test_task_branch_start_revalidates_exact_local_review_after_pull(root: Path) -> None:
    assert_task_branch_start_revalidates_exact_bytes_after_pull(root, "local-review", "712", "review")


def test_task_branch_start_replays_the_first_sealed_remote_tip(root: Path) -> None:
    issue = "717"
    name = "branch-sealed-remote-tip"
    repo, _, _, target, _, args = capability_branch_start_fixture(
        root,
        name,
        issue,
        "sealed-remote-tip",
    )
    origin = root / f"{name}-origin.git"
    sealed_tip = advance_origin(root, origin, "branch-sealed-tip-b", "# sealed remote tip B\n")
    original_git_run = cli_module.git_run
    crashed = False

    def crash_after_base_sync(repo_root: Path, git_args: list[str]) -> str:
        nonlocal crashed
        output = original_git_run(repo_root, git_args)
        synchronized = git_args[:2] == ["pull", "--ff-only"] or git_args[:2] == ["merge", "--ff-only"]
        if synchronized and not crashed:
            assert_cli_mutation_lease(repo_root)
            crashed = True
            raise RuntimeError("injected failure after exact base synchronization")
        return output

    with patch.object(cli_module, "git_run", side_effect=crash_after_base_sync):
        try:
            invoke_git_start_cli(repo, args)
        except RuntimeError as exc:
            assert "injected failure" in str(exc)
        else:
            raise AssertionError("expected injected base synchronization failure")
    assert crashed
    advance_origin(root, origin, "branch-sealed-tip-c", "# later remote tip C\n")
    replay = invoke_git_start_cli(repo, args)
    assert replay.returncode == 0, replay.stderr
    target_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", target],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    ).stdout.strip()
    _, claim = task_branch_claim(repo, issue)
    assert claim["baseCommit"] == sealed_tip
    assert target_tip == sealed_tip


def test_task_branch_start_recovers_after_branch_creation(root: Path) -> None:
    issue = "713"
    repo, _, _, target, ctx, args = capability_branch_start_fixture(
        root,
        "branch-created-recovery",
        issue,
        "created-recovery",
    )

    def fail_branch_transition(
        repo_root: Path,
        _reservation: approval.TaskBranchStartReservation,
    ) -> approval.TaskBranchStartReservation:
        assert_cli_mutation_lease(repo_root)
        raise RuntimeError("injected failure before branch-created transition")

    with patch.object(approval, "mark_task_branch_created", side_effect=fail_branch_transition):
        try:
            invoke_git_start_cli(repo, args)
        except RuntimeError as exc:
            assert "injected failure" in str(exc)
        else:
            raise AssertionError("expected injected branch creation failure")

    _, pending = task_branch_claim(repo, issue)
    assert pending["state"] == "reserved"
    assert resolve_bindings(repo).branch == target
    replay = invoke_git_start_cli(repo, args)
    assert replay.returncode == 0, replay.stderr
    _, completed = task_branch_claim(repo, issue)
    assert completed["state"] == "completed"
    assert resolve_bindings(repo).branch == target
    records = tuple((repo / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "history").glob("*-task-branch-start-*.yaml"))
    assert len(records) == 1
    assert not tuple(
        (git_path(repo, "--git-common-dir") / "xflow" / "runtime" / "task-branch-start").rglob("*.lock")
    )


def test_task_branch_start_recovers_after_activation(root: Path) -> None:
    issue = "714"
    repo, _, _, target, ctx, args = capability_branch_start_fixture(
        root,
        "branch-activation-recovery",
        issue,
        "activation-recovery",
    )

    def fail_history(path: Path, content: str) -> None:
        assert_cli_mutation_lease(repo)
        raise RuntimeError("injected failure after activation")

    with patch.object(approval, "_write_history_atomic", side_effect=fail_history):
        try:
            invoke_git_start_cli(repo, args)
        except RuntimeError as exc:
            assert "injected failure" in str(exc)
        else:
            raise AssertionError("expected injected activation failure")

    _, pending = task_branch_claim(repo, issue)
    assert pending["state"] == "activated"
    bindings = resolve_bindings(repo)
    pointer = active_task_pointer_file(repo, bindings.worktree)
    authority = authority_file(repo, issue)
    pointer_bytes = pointer.read_bytes()
    authority_bytes = authority.read_bytes()
    replay = invoke_git_start_cli(repo, args)
    assert replay.returncode == 0, replay.stderr
    _, completed = task_branch_claim(repo, issue)
    assert completed["state"] == "completed"
    assert resolve_bindings(repo).branch == target
    assert pointer.read_bytes() == pointer_bytes
    assert authority.read_bytes() == authority_bytes
    records = tuple((repo / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "history").glob("*-task-branch-start-*.yaml"))
    assert len(records) == 1


def test_task_branch_start_rejects_unclaimed_or_wrong_start_point(root: Path) -> None:
    unclaimed, _, _, target, unclaimed_ctx, unclaimed_args = capability_branch_start_fixture(
        root,
        "branch-unclaimed",
        "715",
        "unclaimed",
    )
    git(unclaimed, "branch", target, "main")
    unclaimed_result = invoke_git_start_cli(unclaimed, unclaimed_args)
    assert unclaimed_result.returncode == 1, unclaimed_result.stdout
    assert "branch already exists" in unclaimed_result.stderr
    claims_root = unclaimed / ".xflow" / "issues" / "issue-715" / "approvals" / "history" / "claims"
    assert not claims_root.exists() or not tuple(claims_root.glob("*-task-branch.yaml"))

    issue = "716"
    repo, _, _, _, ctx, args = capability_branch_start_fixture(
        root,
        "branch-wrong-start",
        issue,
        "wrong-start",
    )
    def fail_branch_transition(
        repo_root: Path,
        _reservation: approval.TaskBranchStartReservation,
    ) -> approval.TaskBranchStartReservation:
        assert_cli_mutation_lease(repo_root)
        raise RuntimeError("injected failure before branch-created transition")

    with patch.object(approval, "mark_task_branch_created", side_effect=fail_branch_transition):
        try:
            invoke_git_start_cli(repo, args)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected injected branch creation failure")
    write(repo / "unexpected.txt", "unexpected branch advance\n")
    git(repo, "add", "unexpected.txt")
    git(repo, "commit", "-m", "test: advance claimed branch", "-q")
    wrong_start = invoke_git_start_cli(repo, args)
    assert wrong_start.returncode == 1, wrong_start.stdout
    assert "start point mismatch" in wrong_start.stderr


def test_inherited_mutation_lease_is_scoped_and_live(root: Path) -> None:
    repo = initialized_repo(root, "lease-scope", "feature/504-lease")
    write_state(
        repo,
        dataclass_replace(
            state("504", "feature/504-lease"),
            classification="ui-defect",
            contract_change_required=False,
        ),
    )
    activate_task(repo, "504")
    other = initialized_repo(root, "lease-other", "feature/505-other")
    write_state(other, state("505", "feature/505-other"))
    activate_task(other, "505")
    sibling = root / "lease-sibling"
    git(repo, "worktree", "add", "-b", "feature/505-sibling", str(sibling), "-q")

    with repository_mutation(repo):
        token = git_child_environment(repo)["XFLOW_DEVCTL_MUTATION_LEASE"]
        child_env = {"XFLOW_DEVCTL_MUTATION_LEASE": token}
        pointer = active_task_pointer_file(repo, resolve_bindings(repo).worktree)
        old_pointer = legacy_active_task_pointer_file(repo, resolve_bindings(repo).worktree)
        write(old_pointer, pointer.read_text(encoding="utf-8"))
        before = task_artifact_bytes(repo, "504")
        hook_status = run_devctl_result(repo, "hook", "task-status", env_overrides=child_env)
        assert hook_status.returncode == 0, hook_status.stderr
        assert "Issue: 504" in hook_status.stdout
        assert task_artifact_bytes(repo, "504") == before

        ordinary_status = run_devctl_result(repo, "task", "status", env_overrides=child_env)
        assert ordinary_status.returncode == 1, ordinary_status.stdout
        assert "does not allow command" in ordinary_status.stderr
        assert task_artifact_bytes(repo, "504") == before

        forbidden = (
            ("task", "activate", "--issue", "504"),
            ("task", "migrate-current"),
            ("git", "start", "nested", "--issue", "504", "--base", "main"),
            ("git", "done", "--force", "--issue", "504"),
            ("trace", "check", "--issue", "504", "--contract", "missing", "--matrix", "missing"),
            ("check", "resolution-report", "--issue", "504"),
            ("git", "push", "--issue", "504"),
        )
        for command in forbidden:
            result = run_devctl_result(repo, *command, env_overrides=child_env)
            assert result.returncode == 1, (command, result.stdout, result.stderr)
            assert "does not allow command" in result.stderr, (command, result.stderr)

        wrong_repo = run_devctl_result(other, "hook", "task-status", env_overrides=child_env)
        assert wrong_repo.returncode == 1, wrong_repo.stdout
        assert "invalid or inactive" in wrong_repo.stderr
        wrong_worktree = run_devctl_result(sibling, "hook", "task-status", env_overrides=child_env)
        assert wrong_worktree.returncode == 1, wrong_worktree.stdout
        assert "invalid or inactive" in wrong_worktree.stderr

        forged_env = {"XFLOW_DEVCTL_MUTATION_LEASE": "0" * 64}
        forged = run_devctl_result(repo, "hook", "task-status", env_overrides=forged_env)
        assert forged.returncode == 1, forged.stdout
        assert "invalid or inactive" in forged.stderr

        authority = authority_file(repo, "504")
        authority_bytes = authority.read_bytes()
        authority.unlink()
        missing_authority_before = task_artifact_bytes(repo, "504")
        missing_authority = run_devctl_result(repo, "hook", "task-status", env_overrides=child_env)
        assert missing_authority.returncode == 1, missing_authority.stdout
        assert "missing task authority" in missing_authority.stderr
        assert task_artifact_bytes(repo, "504") == missing_authority_before
        write(authority, authority_bytes.decode("utf-8"))

        lease_path = (
            git_path(repo, "--git-common-dir")
            / "xflow"
            / "locks"
            / "mutations"
            / f"{token}.json"
        )

        def remove_live_lease() -> None:
            with patch.dict(os.environ, child_env):
                with inherited_lease_command(repo, ("hook", "task-status")):
                    lease_path.unlink()
                    raise ValueError("snapshot operation failed")

        assert_value_error("invalid or inactive", remove_live_lease)

    stale = run_devctl_result(repo, "hook", "task-status", env_overrides=child_env)
    assert stale.returncode == 1, stale.stdout
    assert "invalid or inactive" in stale.stderr

    with repository_lock(repo):
        locked_status = run_devctl_result(repo, "task", "status")
    assert locked_status.returncode == 1, locked_status.stdout
    assert "another devctl process holds" in locked_status.stderr
    assert run_devctl_result(repo, "task", "status").returncode == 0
    assert not old_pointer.exists()


def test_inherited_mutation_lease_owner_identity_fails_closed(root: Path) -> None:
    repo = initialized_repo(root, "lease-owner", "feature/506-owner")
    write_state(repo, state("506", "feature/506-owner"))
    activate_task(repo, "506")
    before = task_artifact_bytes(repo, "506")

    with repository_mutation(repo):
        token = git_child_environment(repo)["XFLOW_DEVCTL_MUTATION_LEASE"]
        lease_path = git_path(repo, "--git-common-dir") / "xflow" / "locks" / "mutations" / f"{token}.json"
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        assert isinstance(payload.get("ownerStartIdentity"), str)
        payload["ownerStartIdentity"] = "mismatched-process-start"
        write(lease_path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
        mismatch = run_devctl_result(
            repo,
            "hook",
            "task-status",
            env_overrides={"XFLOW_DEVCTL_MUTATION_LEASE": token},
        )
        assert mismatch.returncode == 1, mismatch.stdout
        assert "invalid or inactive" in mismatch.stderr
        assert task_artifact_bytes(repo, "506") == before

    owner, token = start_mutation_owner(repo)
    lease_path = git_path(repo, "--git-common-dir") / "xflow" / "locks" / "mutations" / f"{token}.json"
    try:
        owner.terminate()
        owner.wait(timeout=10)
        dead = run_devctl_result(
            repo,
            "hook",
            "task-status",
            env_overrides={"XFLOW_DEVCTL_MUTATION_LEASE": token},
        )
        assert dead.returncode == 1, dead.stdout
        assert "invalid or inactive" in dead.stderr
        assert task_artifact_bytes(repo, "506") == before
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        lease_path.unlink(missing_ok=True)


def test_owner_death_between_hook_entry_and_exit_is_read_only(root: Path) -> None:
    repo = initialized_repo(root, "lease-owner-exit", "feature/507-owner-exit")
    write_state(repo, state("507", "feature/507-owner-exit"))
    activate_task(repo, "507")
    pointer = active_task_pointer_file(repo, resolve_bindings(repo).worktree)
    old_pointer = legacy_active_task_pointer_file(repo, resolve_bindings(repo).worktree)
    write(old_pointer, pointer.read_text(encoding="utf-8"))
    before = task_artifact_bytes(repo, "507")
    owner, token = start_mutation_owner(repo)
    lease_path = git_path(repo, "--git-common-dir") / "xflow" / "locks" / "mutations" / f"{token}.json"

    def die_during_read() -> None:
        with patch.dict(os.environ, {"XFLOW_DEVCTL_MUTATION_LEASE": token}):
            with inherited_lease_command(repo, ("hook", "task-status")):
                owner.terminate()
                owner.wait(timeout=10)
                from xflow.task_state import load_active_task_snapshot

                bindings, active = load_active_task_snapshot(repo)
                assert bindings.branch == "feature/507-owner-exit"
                assert active.issue == "507"

    try:
        assert_value_error("invalid or inactive", die_during_read)
        assert task_artifact_bytes(repo, "507") == before
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        lease_path.unlink(missing_ok=True)


def initialized_repo(root: Path, name: str, branch: str) -> Path:
    repo = root / name
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-b", branch, "-q")
    write(repo / "README.md", f"# {name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "test: initialize fixture", "-q")
    return repo


def gap_analysis_text() -> str:
    return """# Gap Analysis

## User Original Statement
The accepted contract is not implemented.

## Clarified Problem Or Gap
The implementation violates one existing contract result.

## Gap Analysis
The contract remains stable while implementation evidence is repaired.

## Evidence
- evidence/before.txt

## Evidence-Backed Findings
### Finding F-001: Existing result is missing

#### Finding Type
non-ui

#### Observation
The implementation does not produce the contracted result.

#### User Impact
The participant cannot rely on the existing contract.

#### Evidence
- evidence/before.txt

#### Analysis
The implementation must be corrected without changing the contract.

#### Proposed Change
Restore the contracted result.

#### Acceptance
Fresh evidence proves the existing result.

#### Human Review
- [x] Review the finding.

## Scope Boundaries
Only the existing implementation gap is in scope.

## Proposed Modification Plan
Add a regression and restore the existing result.

## Acceptance Criteria
- [ ] C-001: The existing result is restored.

## Human Recognition
Recognized: yes
"""


def test_route_semantics_fail_closed_for_capability_actions(root: Path) -> None:
    repo = initialized_repo(root, "capability-route-gate", "feature/601-route-gate")
    issue = "601"
    initial = state(issue, "feature/601-route-gate")
    write_state(repo, initial)
    activate_task(repo, issue)

    for execution_state in (
        "S4_TDD_AND_IMPLEMENTATION",
        "S7_PUSH_BRANCH",
        "S10_DONE",
    ):
        write_state(repo, dataclass_replace(initial, execution_state=execution_state))
        assert_value_error(
            "capability-change requires accepted-design",
            lambda: check_current_task(repo, issue),
        )

    write_state(repo, initial)
    approved_file = repo / ".xflow" / "issues" / f"issue-{issue}" / "walkthrough.md"
    write(approved_file, "# Walkthrough\n")
    unattended.enable(repo, issue, unattended.CONFIRMATION)
    assert_value_error(
        "capability-change requires accepted-design",
        lambda: approval.require_remote_or_unattended(
            repo,
            "git-push",
            approved_file,
            issue,
            request_unattended=True,
        ),
    )

    hook = run_devctl_result(repo, "hook", "task-status")
    assert hook.returncode == 1, hook.stdout
    assert "capability-change requires accepted-design" in hook.stderr


def test_implementation_gap_uses_immutable_gap_recognition(root: Path) -> None:
    repo = initialized_repo(root, "gap-recognition", "fix/602-existing-gap")
    issue = "602"
    candidate = dataclass_replace(
        state(issue, "fix/602-existing-gap"),
        classification="implementation-gap",
        contract_change_required=False,
        semantic_phase="gap-analysis",
        human_gate="human gap recognition required",
    )
    write_state(repo, candidate)
    activate_task(repo, issue)
    gap_file = repo / ".xflow" / "issues" / f"issue-{issue}" / "gap-analysis.md"
    write(gap_file.parent / "evidence" / "before.txt", "captured before evidence\n")
    write(gap_file, gap_analysis_text())

    review = approval.prepare(
        repo,
        issue,
        "gap-recognition",
        gap_file,
        reviewer="human reviewer",
        force=True,
    )
    write(review, review.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"))
    recognized = run_devctl_result(repo, "gap", "recognize", "--issue", issue, "--file", str(gap_file))
    assert recognized.returncode == 0, recognized.stderr
    records = tuple((gap_file.parent / "approvals" / "history").glob("*-gap-recognition-*.yaml"))
    assert len(records) == 1
    recognition_ref = records[0].relative_to(gap_file.parent).as_posix()
    recognized_state = dataclass_replace(
        candidate,
        execution_state="S4_TDD_AND_IMPLEMENTATION",
        semantic_phase="gap-recognized",
        human_approval_ref=recognition_ref,
    )
    write_state(repo, recognized_state)
    check_current_task(repo, issue)

    replay = run_devctl_result(repo, "gap", "recognize", "--issue", issue, "--file", str(gap_file))
    assert replay.returncode == 1, replay.stdout
    assert "approval already consumed" in replay.stderr
    write(gap_file, gap_analysis_text().replace("Restore the contracted result.", "Change the recognized scope."))
    assert_value_error("missing matching human gap recognition", lambda: check_current_task(repo, issue))


def legacy_source(issue: str, *, action: str = "clarify-contract") -> str:
    return (
        f"# XFlow Current Task\n\nIssue: {issue}\nState: S2_REMOTE_ISSUE_CREATED\n\n"
        f"## Allowed Actions\n- {action}\n\n## Forbidden Actions\n- push\n"
    )


def test_modern_pointer_blocks_legacy_migration(root: Path) -> None:
    repo = initialized_repo(root, "modern-migration", "feature/501-modern")
    write_state(repo, state("501", "feature/501-modern"))
    activate_task(repo, "501")
    bindings = resolve_bindings(repo)
    pointer = active_task_pointer_file(repo, bindings.worktree)
    original_pointer = pointer.read_bytes()
    authority = authority_file(repo, "501")
    authority.unlink()
    current_task = repo / ".xflow" / "current-task.md"
    write(current_task, legacy_source("501"))
    migrated_state = TaskState(
        issue="501",
        execution_state="S2_REMOTE_ISSUE_CREATED",
        semantic_phase="none",
        classification="implementation-gap",
        contract="legacy.current-task@0.1.0",
        contract_file=".xflow/current-task.md",
        contract_change_required=False,
        branch="feature/501-modern",
        base="main",
        allowed_actions=("clarify-contract",),
        forbidden_actions=("push",),
        human_gate="legacy task state requires human gate confirmation",
        human_approval_ref="none",
    )
    write_state(repo, migrated_state)

    assert_value_error("modern task authority cannot downgrade to legacy", lambda: migrate_legacy_current_task(repo))
    assert pointer.read_bytes() == original_pointer
    assert not authority.exists()

    mismatching = {
        **json.loads(original_pointer.decode("utf-8")),
        "issue": "999",
        "taskMode": "legacy",
        "contractId": "legacy.current-task",
        "contractVersion": "0.1.0",
        "contractFile": ".xflow/current-task.md",
    }
    write(pointer, json.dumps(mismatching, ensure_ascii=True, indent=2) + "\n")
    assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(repo))

    write_state(repo, state("501", "feature/501-modern"))
    write(pointer, original_pointer.decode("utf-8"))
    assert load_active_task(repo).issue == "501"
    old_pointer = legacy_active_task_pointer_file(repo, bindings.worktree)
    write(old_pointer, pointer.read_text(encoding="utf-8"))
    assert load_active_task(repo).issue == "501"
    assert not old_pointer.exists()


def test_legacy_authority_is_live_provenance(root: Path) -> None:
    repo = initialized_repo(root, "legacy-provenance", "feature/502-legacy")
    current_task = repo / ".xflow" / "current-task.md"
    original_source = legacy_source("502")
    write(current_task, original_source)
    migrated = migrate_legacy_current_task(repo)
    task_path = repo / ".xflow" / "issues" / "issue-502" / "task-state.md"
    original_state = task_path.read_text(encoding="utf-8")
    assert load_active_task(repo) == migrated

    current_task.unlink()
    assert_value_error("missing current task state file", lambda: load_active_task(repo))
    assert_value_error("missing current task state file", lambda: check_current_task(repo, "502"))
    write(current_task, original_source)
    write(current_task, legacy_source("502", action="verify"))
    assert_value_error("legacy task authority source provenance mismatch", lambda: load_active_task(repo))
    assert_value_error("legacy task authority source provenance mismatch", lambda: check_current_task(repo, "502"))
    assert_value_error("legacy task authority source provenance mismatch", lambda: current_task_issue(repo))
    write(current_task, original_source)

    write(task_path, original_state.replace("Base: main", "Base: develop"))
    assert_value_error("canonical migrated task-state", lambda: load_active_task(repo))
    assert_value_error("canonical migrated task-state", lambda: check_current_task(repo, "502"))
    assert_value_error("canonical migrated task-state", lambda: current_task_issue(repo))
    write(task_path, original_state)

    original_reader = local_artifacts_module._read_stable_bytes

    def mutate_source(*args: object, **kwargs: object) -> bytes:
        content = original_reader(*args, **kwargs)
        target = Path(args[1])
        if target == current_task.resolve():
            write(current_task, legacy_source("502", action="race"))
        return content

    try:
        with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_source):
            assert_value_error("changed while reading", lambda: load_active_task(repo))
    finally:
        write(current_task, original_source)


def test_legacy_fallback_is_stable_and_authority_aware(root: Path) -> None:
    repo = initialized_repo(root, "legacy-fallback", "feature/503-fallback")
    current_task = repo / ".xflow" / "current-task.md"
    write(current_task, legacy_source("STALE"))
    write_state(repo, state("503", "feature/503-fallback"))
    activate_task(repo, "503")
    bindings = resolve_bindings(repo)
    active_task_pointer_file(repo, bindings.worktree).unlink()
    (repo / ".xflow" / "issues" / "issue-503" / "task-state.md").unlink()

    assert_value_error("missing active task pointer", lambda: check_current_task(repo))
    assert_value_error("missing active task pointer", lambda: current_task_issue(repo))

    authority_file(repo, "503").unlink()
    hardlink = current_task.with_name("current-task-hardlink.md")
    os.link(current_task, hardlink)
    assert_value_error("exactly one filesystem link", lambda: check_current_task(repo))
    assert_value_error("exactly one filesystem link", lambda: current_task_issue(repo))
    hardlink.unlink()

    real_source = current_task.with_name("current-task-real.md")
    current_task.replace(real_source)
    try:
        os.symlink(real_source.name, current_task)
    except OSError:
        real_source.replace(current_task)
        with patch.object(local_artifacts_module, "_is_reparse_point", return_value=True):
            assert_value_error("symlink, junction, or reparse point", lambda: check_current_task(repo))
            assert_value_error("symlink, junction, or reparse point", lambda: current_task_issue(repo))
    else:
        assert_value_error("symlink, junction, or reparse point", lambda: check_current_task(repo))
        assert_value_error("symlink, junction, or reparse point", lambda: current_task_issue(repo))
        current_task.unlink()
        real_source.replace(current_task)

    original_reader = local_artifacts_module._read_stable_bytes

    def mutate_fallback(*args: object, **kwargs: object) -> bytes:
        content = original_reader(*args, **kwargs)
        target = Path(args[1])
        if target == current_task.resolve():
            write(current_task, legacy_source("RACE"))
        return content

    with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_fallback):
        assert_value_error("changed while reading", lambda: check_current_task(repo))
    write(current_task, legacy_source("STALE"))
    with patch.object(local_artifacts_module, "_read_stable_bytes", side_effect=mutate_fallback):
        assert_value_error("changed while reading", lambda: current_task_issue(repo))


def test_modern_unattended_ignores_preserved_legacy_task(root: Path) -> None:
    repo = initialized_repo(root, "modern-unattended", "feature/509-modern")
    legacy = repo / ".xflow" / "current-task.md"
    original_legacy = legacy_source("MIGRATEDA")
    write(legacy, original_legacy)
    migrate_legacy_current_task(repo)

    modern = dataclass_replace(
        state("MODERNB", "feature/509-modern"),
        classification="ui-defect",
        contract_change_required=False,
    )
    write_state(repo, modern)
    activate_task(repo, modern.issue)
    unattended.enable(repo, modern.issue, unattended.CONFIRMATION)

    loaded = unattended.load(repo)
    assert loaded is not None and loaded.issue == modern.issue
    assert legacy.read_text(encoding="utf-8") == original_legacy
    assert unattended.state_path(repo).is_file()

    write_state(repo, dataclass_replace(modern, execution_state="S10_DONE"))
    assert_value_error("current task is completed", lambda: unattended.load(repo))
    assert not unattended.state_path(repo).exists()
    assert legacy.read_text(encoding="utf-8") == original_legacy

def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worktree_a = root / "worktree-a"
        worktree_b = root / "worktree-b"
        git(root, "init", "-q", str(worktree_a))
        git(worktree_a, "config", "user.email", "test@example.com")
        git(worktree_a, "config", "user.name", "Test User")
        git(worktree_a, "checkout", "-b", "main", "-q")
        write(worktree_a / "README.md", "# Demo\n")
        git(worktree_a, "add", "README.md")
        git(worktree_a, "commit", "-m", "init", "-q")
        git(worktree_a, "checkout", "-b", "feature/101-a", "-q")
        git(worktree_a, "worktree", "add", "-b", "feature/IK3RR6-b", str(worktree_b), "main")

        write_state(worktree_a, state("101", "feature/101-a"))
        write_state(worktree_a, state("102", "feature/101-a"))
        write_state(worktree_a, state("IK3RR6", "feature/IK3RR6-b"))
        write_state(worktree_a, state("202", "feature/202-dependency"))
        write_state(worktree_b, state("IK3RR6", "feature/IK3RR6-b"))
        write(worktree_a / ".xflow" / "issues" / "issue-101" / "subtask-001" / "task-state.md", render_task_state(state("101", "feature/101-a")))

        activate_task(worktree_a, "101")
        activate_task(worktree_b, "IK3RR6")
        assert load_active_task(worktree_a).issue == "101"
        assert load_active_task(worktree_b).issue == "IK3RR6"
        check_current_task(worktree_a, "101")
        assert_value_error("active task Issue mismatch", lambda: check_task_binding(worktree_b, "101"))
        assert [item.issue for item in list_task_states(worktree_a)] == ["101", "102", "202", "IK3RR6"]

        pointer = active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert set(payload) == {
            "version", "repository", "worktree", "branch", "issue", "taskMode",
            "contractId", "contractVersion", "contractFile", "activatedAt",
        }
        assert payload["version"] == 2
        assert payload["issue"] == "101"
        assert payload["taskMode"] == "modern-contract"
        assert payload["contractId"] == "example.contract.capability-name"
        assert payload["contractVersion"] == "0.1.0"
        assert payload["contractFile"] == "docs/requirements/example/contract.yaml"
        pointer_bytes = pointer.read_text(encoding="utf-8")
        for field, replacement in (
            (
                '  "branch": "feature/101-a",',
                '  "branch": "feature/stale",\n  "branch": "feature/101-a",',
            ),
            (
                '  "taskMode": "modern-contract",',
                '  "taskMode": "legacy",\n  "taskMode": "modern-contract",',
            ),
        ):
            write(pointer, pointer_bytes.replace(field, replacement))
            assert_value_error("duplicate JSON key", lambda: load_active_task(worktree_a))
        write(pointer, pointer_bytes)
        authority = authority_file(worktree_a, "101")
        authority_payload = json.loads(authority.read_text(encoding="utf-8"))
        assert authority_payload["taskMode"] == "modern-contract"
        assert authority_payload["contractId"] == "example.contract.capability-name"
        authority_bytes = authority.read_text(encoding="utf-8")
        authority_link = authority.with_name("authority-hardlink.json")
        os.link(authority, authority_link)
        assert_value_error("exactly one filesystem link", lambda: activate_task(worktree_a, "101"))
        authority_link.unlink()
        write(authority, authority_bytes.replace("\n}", ',\n  "unexpected": true\n}'))
        assert_value_error("unexpected JSON fields", lambda: activate_task(worktree_a, "101"))
        write(authority, authority_bytes)
        write(
            authority,
            authority_bytes.replace(
                '  "taskMode": "modern-contract",',
                '  "taskMode": "legacy",\n  "taskMode": "modern-contract",',
            ),
        )
        assert_value_error("duplicate JSON key: taskMode", lambda: activate_task(worktree_a, "101"))
        write(authority, authority_bytes)

        sentinel = TaskState(
            **{
                **state("SENTINEL", "feature/101-a").__dict__,
                "contract": "legacy.current-task@0.1.0",
                "contract_file": ".xflow/current-task.md",
            }
        )
        write_state(worktree_a, sentinel)
        assert_value_error(
            "legacy task mode requires validated current-task migration",
            lambda: activate_task(worktree_a, "SENTINEL"),
        )

        activate_task(worktree_a, "102")
        downgraded = state("101", "feature/101-a")
        downgraded = TaskState(
            **{
                **downgraded.__dict__,
                "contract": "legacy.current-task@0.1.0",
                "contract_file": ".xflow/current-task.md",
            }
        )
        write_state(worktree_a, downgraded)
        assert_value_error("modern task authority cannot downgrade to legacy", lambda: activate_task(worktree_a, "101"))
        write_state(worktree_a, state("101", "feature/101-a"))
        activate_task(worktree_a, "101")

        pointer.unlink()
        state_101 = worktree_a / ".xflow" / "issues" / "issue-101" / "task-state.md"
        write_state(worktree_a, downgraded)
        assert_value_error("modern task authority cannot downgrade to legacy", lambda: activate_task(worktree_a, "101"))
        write_state(worktree_a, state("101", "feature/101-a"))
        assert_value_error("missing active task pointer", lambda: check_current_task(worktree_a, "101"))
        activate_task(worktree_a, "101")

        pointer_link = pointer.with_name("active-task-hardlink.json")
        os.link(pointer, pointer_link)
        assert_value_error("exactly one filesystem link", lambda: load_active_task(worktree_a))
        pointer_link.unlink()

        state_link = state_101.with_name("task-state-hardlink.md")
        os.link(state_101, state_link)
        assert_value_error("exactly one filesystem link", lambda: parse_task_state(state_101))
        state_link.unlink()

        old_payload = v1_pointer(payload)
        write(pointer, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        assert load_active_task(worktree_a).issue == "101"
        migrated_payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert migrated_payload["version"] == 2
        assert migrated_payload["taskMode"] == "modern-contract"

        old_location = legacy_active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        pointer.unlink()
        legacy_pointer_text = json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n"
        write(
            old_location,
            legacy_pointer_text.replace(
                '  "branch": "feature/101-a",',
                '  "branch": "feature/stale",\n  "branch": "feature/101-a",',
            ),
        )
        assert_value_error("duplicate JSON key: branch", lambda: load_active_task(worktree_a))
        write(old_location, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        check_current_task(worktree_a, "101")
        assert pointer.is_file()
        assert not old_location.exists()
        assert json.loads(pointer.read_text(encoding="utf-8"))["version"] == 2

        write(old_location, json.dumps(old_payload, ensure_ascii=True, indent=2) + "\n")
        assert load_active_task(worktree_a).issue == "101"
        assert not old_location.exists()

        conflicting_old_payload = {**old_payload, "activatedAt": "2000-01-01T00:00:00+00:00"}
        write(old_location, json.dumps(conflicting_old_payload, ensure_ascii=True, indent=2) + "\n")
        assert_value_error("conflicting active task pointers", lambda: load_active_task(worktree_a))
        old_location.unlink()

        pointer.unlink()
        stale_payload = {**old_payload, "branch": "feature/stale"}
        write(old_location, json.dumps(stale_payload, ensure_ascii=True, indent=2) + "\n")
        write(
            worktree_a / ".xflow" / "current-task.md",
            "# XFlow Current Task\n\nIssue: 101\nState: S2_REMOTE_ISSUE_CREATED\n\n"
            "## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n",
        )
        assert_value_error("active task branch mismatch", lambda: check_current_task(worktree_a, "101"))
        old_location.unlink()
        assert_value_error("active task pointer", lambda: check_current_task(worktree_a, "101"))
        activate_task(worktree_a, "101")

        git(worktree_a, "checkout", "-b", "feature/303-c", "-q")
        assert_value_error("active task branch mismatch", lambda: load_active_task(worktree_a))
        write_state(worktree_a, state("303", "feature/303-c"))
        run_devctl(worktree_a, "task", "activate", "--issue", "303")
        assert load_active_task(worktree_a).issue == "303"

        legacy_compat = worktree_a / ".xflow" / "current-task.md"
        write(legacy_compat, "# XFlow Current Task\n\nIssue: 303\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        git(worktree_a, "checkout", "--detach", "-q")
        assert_value_error("cannot bind XFlow task to detached HEAD", lambda: check_current_task(worktree_a, "303"))
        git(worktree_a, "checkout", "feature/303-c", "-q")

        accepted = state("404", "feature/404-d", phase="accepted-design", approval="approvals/history/design.md")
        accepted_path = write_state(worktree_a, accepted)
        assert accepted_path.is_file()
        malformed = render_task_state(accepted).replace("Human Approval Ref: approvals/history/design.md", "Human Approval Ref: none")
        write(accepted_path, malformed)
        assert_value_error("Human Approval Ref", lambda: list_task_states(worktree_a))
        write(accepted_path, render_task_state(accepted))
        assert_value_error("missing matching human contract acceptance", lambda: list_task_states(worktree_a))
        accepted_path.unlink()

        invalid_path = worktree_a / ".xflow" / "issues" / "issue-505" / "task-state.md"
        invalid_cases = (
            ("Execution State: S2_REMOTE_ISSUE_CREATED", "Execution State: INVALID", "unknown task-state Execution State"),
            ("Semantic Phase: classified", "Semantic Phase: INVALID", "unknown task-state Semantic Phase"),
            ("Classification: capability-change", "Classification: INVALID", "unknown task-state Classification"),
            ("Contract Change Required: yes", "Contract Change Required: maybe", "Contract Change Required must be yes or no"),
            ("Human Gate: capability design acceptance required", "Human Gate: ", "missing required task-state field: Human Gate"),
            ("- clarify-contract\n- prepare-verification", "", "Allowed Actions must not be empty"),
            ("- edit-implementation\n- push\n- create-mr", "", "Forbidden Actions must not be empty"),
        )
        template = render_task_state(state("505", "feature/505-e"))
        for invalid_issue in ("ABC-1", "A_B", "A.B"):
            assert_value_error("letters and numbers", lambda invalid_issue=invalid_issue: render_task_state(state(invalid_issue, "feature/505-e")))
            invalid_issue_path = worktree_a / ".xflow" / "issues" / f"issue-{invalid_issue}" / "task-state.md"
            write(invalid_issue_path, template.replace("Issue: 505", f"Issue: {invalid_issue}"))
            assert_value_error("letters and numbers", lambda invalid_issue_path=invalid_issue_path: parse_task_state(invalid_issue_path))
            invalid_issue_path.unlink()
        for old, new, expected in invalid_cases:
            write(invalid_path, template.replace(old, new))
            assert_value_error(expected, lambda: parse_task_state(invalid_path))
        ambiguous_cases = (
            (template.replace("Issue: 505", "Issue: 505\nIssue: 606"), "duplicate task-state field: Issue"),
            (
                template.replace("Human Approval Ref: none", "Human Approval Ref: none\nHuman Approval Ref: approvals/history/design.md"),
                "duplicate task-state field: Human Approval Ref",
            ),
            (
                template.replace("Contract: example.contract.capability-name@0.1.0\n", "").replace(
                    "## Allowed Actions\n", "## Allowed Actions\nContract: supplied-in-action-section\n"
                ),
                "unexpected non-list line in ## Allowed Actions",
            ),
            (template.replace("## Allowed Actions", "## Allowed Actions\n## Allowed Actions", 1), "duplicate task-state heading: ## Allowed Actions"),
            (template.replace("# XFlow Task State", "# XFlow Task State\n# XFlow Task State", 1), "duplicate task-state heading: # XFlow Task State"),
            (template.replace("- clarify-contract", "not-a-list-item\n- clarify-contract"), "unexpected non-list line in ## Allowed Actions"),
        )
        for malformed, expected in ambiguous_cases:
            write(invalid_path, malformed)
            assert_value_error(expected, lambda: parse_task_state(invalid_path))
        wrong_path = worktree_a / ".xflow" / "issues" / "issue-OTHER" / "task-state.md"
        write(wrong_path, template)
        assert_value_error("matching Issue directory", lambda: parse_task_state(wrong_path))
        outside_path = worktree_a / "issue-505" / "task-state.md"
        write(outside_path, template)
        assert_value_error("matching Issue directory", lambda: parse_task_state(outside_path))
        invalid_path.unlink()
        wrong_path.unlink()
        outside_path.unlink()

        legacy = worktree_a / ".xflow" / "current-task.md"
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        legacy_link = legacy.with_name("current-task-hardlink.md")
        os.link(legacy, legacy_link)
        assert_value_error("exactly one filesystem link", lambda: migrate_legacy_current_task(worktree_a))
        legacy_link.unlink()
        assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(worktree_a))
        active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).unlink()
        migrated = migrate_legacy_current_task(worktree_a)
        assert migrated.issue == "LEGACY7"
        assert load_active_task(worktree_a).issue == "LEGACY7"
        legacy_payload = json.loads(active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).read_text(encoding="utf-8"))
        assert legacy_payload["taskMode"] == "legacy"
        assert legacy_payload["contractId"] == "legacy.current-task"
        assert legacy_payload["contractVersion"] == "0.1.0"
        assert legacy_payload["contractFile"] == ".xflow/current-task.md"
        legacy_authority = authority_file(worktree_a, "LEGACY7")
        legacy_authority_payload = json.loads(legacy_authority.read_text(encoding="utf-8"))
        assert legacy_authority_payload["legacySourceDigest"]
        assert legacy_authority_payload["legacySourceFile"] == ".xflow/current-task.md"

        legacy_pointer_path = active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree)
        legacy_pointer_path.unlink()
        legacy_authority.unlink()
        recovered = migrate_legacy_current_task(worktree_a)
        assert recovered == migrated
        recovered_pointer = legacy_pointer_path.read_bytes()
        recovered_authority = legacy_authority.read_bytes()
        assert migrate_legacy_current_task(worktree_a) == migrated
        assert legacy_pointer_path.read_bytes() == recovered_pointer
        assert legacy_authority.read_bytes() == recovered_authority
        write(
            active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree),
            json.dumps(v1_pointer(legacy_payload), ensure_ascii=True, indent=2) + "\n",
        )
        assert_value_error("cannot be migrated safely", lambda: load_active_task(worktree_a))
        assert_value_error("legacy task mode requires validated current-task migration", lambda: activate_task(worktree_a, "LEGACY7"))
        migrate_legacy_current_task(worktree_a)
        assert legacy.is_file()
        migrated_path = worktree_a / ".xflow" / "issues" / "issue-LEGACY7" / "task-state.md"
        assert migrated_path.is_file()
        migrated_bytes = migrated_path.read_bytes()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S5_LOCAL_VERIFICATION\n\n## Allowed Actions\n- verify\n\n## Forbidden Actions\n- push\n")
        assert_value_error("legacy task authority source provenance mismatch", lambda: migrate_legacy_current_task(worktree_a))
        assert migrated_path.read_bytes() == migrated_bytes
        legacy.write_text("# XFlow Current Task\n\nIssue: ../invalid\n", encoding="utf-8", newline="\n")
        assert_value_error("current task Issue", lambda: migrate_legacy_current_task(worktree_a))
        assert not (worktree_a / ".xflow" / "issues" / "issue-invalid" / "task-state.md").exists()
        write(legacy, "# XFlow Current Task\n\nIssue: LEGACY7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")

        status = run_devctl(worktree_a, "task", "status")
        for field in ("repository", "worktree", "branch", "Issue", "Execution State", "Semantic Phase", "Classification", "Contract"):
            assert field in status.stdout
        listed = run_devctl(worktree_a, "task", "list")
        assert "#101" in listed.stdout
        write(legacy, "# XFlow Current Task\n\nIssue: CLI7\nState: S2_REMOTE_ISSUE_CREATED\n\n## Allowed Actions\n- clarify-contract\n\n## Forbidden Actions\n- push\n")
        assert_value_error("active task pointer Issue mismatch", lambda: migrate_legacy_current_task(worktree_a))
        active_task_pointer_file(worktree_a, resolve_bindings(worktree_a).worktree).unlink()
        migrated_output = run_devctl(worktree_a, "task", "migrate-current")
        assert "CLI7" in migrated_output.stdout
        assert load_active_task(worktree_a).issue == "CLI7"

        test_official_git_start_respects_closure_lock(root)
        test_first_capability_task_establishes_final_branch_before_acceptance(root)
        test_task_branch_start_revalidates_exact_task_state_after_pull(root)
        test_task_branch_start_revalidates_exact_local_review_after_pull(root)
        test_task_branch_start_replays_the_first_sealed_remote_tip(root)
        test_task_branch_start_recovers_after_branch_creation(root)
        test_task_branch_start_recovers_after_activation(root)
        test_task_branch_start_rejects_unclaimed_or_wrong_start_point(root)
        test_git_hook_devctl_reentry(root)
        test_git_hook_requires_retained_contract_acceptance(root)
        test_inherited_mutation_lease_is_scoped_and_live(root)
        test_inherited_mutation_lease_owner_identity_fails_closed(root)
        test_owner_death_between_hook_entry_and_exit_is_read_only(root)
        test_route_semantics_fail_closed_for_capability_actions(root)
        test_implementation_gap_uses_immutable_gap_recognition(root)
        test_modern_pointer_blocks_legacy_migration(root)
        test_legacy_authority_is_live_provenance(root)
        test_legacy_fallback_is_stable_and_authority_aware(root)
        test_modern_unattended_ignores_preserved_legacy_task(root)

    print("task state ok")


if __name__ == "__main__":
    main()
