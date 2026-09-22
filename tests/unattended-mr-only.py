"""Real local Git/contract/gap transitions; no provider mutation or human file edits."""
from pathlib import Path
from dataclasses import replace
import json
import runpy
import sys
import tempfile
import yaml
import os
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
h = runpy.run_path(str(ROOT / "tests/task-state.py"))
from xflow import unattended, approval
from xflow import cli, providers
from xflow.contracts import load_contract, validate_contract_acceptance
from xflow.checks import check_current_task
from xflow.task_state import activate_task

git, write, state, write_state = (h[x] for x in ("git", "write", "state", "write_state"))
run = h["run_devctl_result"]
fail = h["assert_value_error"]

def invoke(repo, argv):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {"DEVCTL_REPO_ROOT": str(repo), "DEVCTL_SKIP_PROVIDER_LOAD": "0"}):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    repo = root / "repo"
    origin = root / "origin.git"
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "checkout", "-b", "main")
    write(repo / ".gitignore", ".xflow/local/\n")
    write(repo / "README.md", "fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: fixture")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-qu", "origin", "main")
    # Explicit opt-in is versioned; a legacy state does not gain semantic authority.
    legacy = unattended.enable(repo, "draft", unattended.CONFIRMATION)
    assert legacy.version == 1 and unattended.mr_only(repo, "draft") is None
    result = run(repo, "unattended", "enable", "--issue", "draft", "--confirm", unattended.CONFIRMATION)
    assert result.returncode == 0, result.stderr
    first = unattended.require_active(repo, "draft")
    assert first.approvalPolicy == "mr-only-v1"
    capabilities = run(repo, "unattended", "capabilities")
    assert capabilities.returncode == 0, capabilities.stderr
    assert "mr-only-v1" in json.loads(capabilities.stdout)["policies"]
    fail("Issue mismatch", lambda: unattended.require_active(repo, "another-task"))
    token_path = unattended.state_path(repo)
    token_bytes = token_path.read_bytes()
    malformed = json.loads(token_bytes)
    malformed["approvalPolicy"] = "unknown-policy"
    write(token_path, json.dumps(malformed))
    fail("invalid unattended policy", lambda: unattended.load(repo))
    token_path.write_bytes(token_bytes)
    migrated = unattended.migrate_issue(repo, "draft", "901")
    assert migrated.authorizationId == first.authorizationId
    candidate = replace(state("901", "feat/901-summary"), semantic_phase="declaring")
    task = write_state(repo, candidate)
    write(task.with_name("classification.yaml"), """version: 0.1.0
request:
  originalStatement: A new test capability.
contractSearch:
  status: not-found
  refs: []
classification: capability-change
contractChangeRequired: true
reason: New capability.
nextArtifact: contract-change-proposal.md
decisionSource: ai-proposed
""")
    result = run(repo, "git", "start", "summary", "--issue", "901", "--base", "main", "--file", str(task))
    assert result.returncode == 0, result.stderr
    current = unattended.require_active(repo, "901")
    assert current.authorizationId == first.authorizationId
    assert current.branch == candidate.branch
    assert not approval.default_approval_file(repo, "901").exists()
    branch_record = next((task.parent / "approvals/history").glob("*-task-branch-start-*.yaml"))
    assert yaml.safe_load(branch_record.read_text())["source"] == "unattended"
    git(repo, "add", ".xflow/issues")
    git(repo, "commit", "-qm", "docs: branch evidence")

    write(repo / ".xflow/xflow.json", '{"contracts":{"root":"docs/requirements"}}')
    path = repo / candidate.contract_file
    write(path, (ROOT / "tests/fixtures/contracts/valid.yaml").read_text())
    contract = load_contract(repo, path)
    history = validate_contract_acceptance(repo, "901", contract, (str(contract.raw["id"]),))
    record = approval.parse_contract_acceptance_history(repo, history)
    assert record["source"] == "unattended"
    receipt = task.parent / record["approvedReviewFile"]
    assert "Approved: yes" not in receipt.read_text()
    assert "Approved: delegated" in receipt.read_text()
    write_state(repo, replace(candidate, execution_state="S4_TDD_AND_IMPLEMENTATION",
        semantic_phase="accepted-design", human_gate="MR review only",
        human_approval_ref=history.relative_to(task.parent).as_posix()))
    check_current_task(repo, "901")
    # Even at active MR-only state, the user must review/merge through the provider.
    for action in ("git-push", "git-mr", "issue-comment"):
        grant = approval.require_remote_or_unattended(repo, action, path, "901")
        assert grant.source == "unattended"
    fail("MR-only review barrier", lambda: approval.require_remote_or_unattended(
        repo, "git-pr-merge", path, "901"))
    original = receipt.read_bytes()
    write(receipt, receipt.read_text().replace("Approved: delegated", "Approved: yes"))
    fail("SHA256 mismatch", lambda: check_current_task(repo, "901"))
    receipt.write_bytes(original)
    check_current_task(repo, "901")
    # Same token cannot be carried to an unrelated branch.
    git(repo, "checkout", "-b", "unrelated")
    fail("branch mismatch", lambda: unattended.load(repo))
    git(repo, "checkout", candidate.branch)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: accepted semantics")
    git(repo, "config", "--worktree", "devctl.pr", "123")
    argv = ["git", "done", "--issue", "901", "--base", "main", "--file", str(path)]
    result = invoke(repo, argv + ["--force"])
    assert result[0] == 1 and "never authorizes forced cleanup" in result[2], result
    remote = {"number": 123, "state": "closed", "merged": False,
              "head": {"ref": candidate.branch}, "base": {"ref": "main"}}
    with patch.object(providers, "get_pull_request", return_value=remote):
        result = invoke(repo, argv)
        assert result[0] == 1 and "requires a merged PR" in result[2], result
    remote["merged"] = True
    with patch.object(providers, "get_pull_request", return_value=remote):
        result = invoke(repo, argv)
        assert result[0] == 1 and "reachable from base" in result[2], result
        write(repo / "uncommitted.txt", "must remain")
        result = invoke(repo, argv)
        assert result[0] == 1 and (repo / "uncommitted.txt").read_text() == "must remain"
        git(repo, "add", "uncommitted.txt")
        git(repo, "commit", "-qm", "test: retain local data")
        # Simulate a provider merge in a local bare remote, never production.
        git(repo, "push", "-q", "origin", "HEAD:main")
        result = invoke(repo, argv)
        assert result[0] == 0, result
    assert unattended.load(repo) is None

    gap_repo = h["initialized_repo"](root, "gap", "fix/902-gap")
    gap_candidate = replace(state("902", "fix/902-gap"),
        classification="implementation-gap", contract_change_required=False,
        semantic_phase="gap-analysis")
    gap_state = write_state(gap_repo, gap_candidate)
    activate_task(gap_repo, "902")
    gap = gap_state.with_name("gap-analysis.md")
    write(gap.parent / "evidence/before.txt", "before evidence\n")
    write(gap, h["gap_analysis_text"]())
    unattended.enable(gap_repo, "902", unattended.CONFIRMATION, policy=unattended.MR_ONLY_POLICY)
    history = approval.consume_gap_recognition(gap_repo, "902", gap)
    assert yaml.safe_load(history.read_text())["source"] == "unattended"
    write_state(gap_repo, replace(gap_candidate, semantic_phase="gap-recognized",
        execution_state="S4_TDD_AND_IMPLEMENTATION",
        human_approval_ref=history.relative_to(gap.parent).as_posix()))
    check_current_task(gap_repo, "902")
    assert not approval.default_approval_file(gap_repo, "902").exists()

    for index, hook in enumerate(("mark_task_branch_created", "_write_history_atomic")):
        issue = str(910 + index)
        fixture = h["capability_branch_start_fixture"](root, "resume-" + issue, issue, "summary")
        recovery, _, manual, target, _, args = fixture
        manual.unlink()  # Test fixture only: prove retry never needs a human file.
        write(recovery / ".gitignore", ".xflow/local/\n")
        git(recovery, "add", ".gitignore")
        git(recovery, "commit", "-qm", "test: local runtime ignore")
        git(recovery, "push", "-q", "origin", "main")
        token = unattended.enable(recovery, issue, unattended.CONFIRMATION, policy=unattended.MR_ONLY_POLICY)
        with patch.object(approval, hook, side_effect=RuntimeError("injected interruption")):
            try:
                h["invoke_git_start_cli"](recovery, args)
            except RuntimeError as exc:
                assert "injected interruption" in str(exc)
            else:
                raise AssertionError("expected injected interruption")
        result = h["invoke_git_start_cli"](recovery, args)
        assert result.returncode == 0, result.stderr
        resumed = unattended.require_active(recovery, issue)
        assert resumed.authorizationId == token.authorizationId and resumed.branch == target

print("MR-only runtime: continuity/recovery, delegated contract/gap, integrity, isolation, merge barrier, safe cleanup passed")
