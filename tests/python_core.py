import os
import json
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xflow.env import RuntimeContext, detect_python_runtime
from xflow.checks import (
    check_academic_issue,
    check_academic_mr,
    check_claude_package,
    check_submodule_hygiene,
    check_tdd_result,
    load_academicforge_skill_names,
)
from xflow.cli import build_parser, main, resolve_check_file
from xflow.approval import check_local_review_file, require_remote_approval


def write_local_review(
    repo_root,
    issue,
    action,
    approved_file,
    approved="yes",
    sha=None,
    include_title=True,
    include_approved_file=True,
    approved_file_text=None,
):
    if sha is None:
        import hashlib

        sha = hashlib.sha256(approved_file.read_bytes()).hexdigest()
    review = Path(repo_root) / ".xflow" / "issues" / f"issue-{issue}" / "approvals" / "local-review.md"
    review.parent.mkdir(parents=True)
    text = (
        f"Issue: {issue}\n"
        "Reviewer: user\n"
        "Approved At: 2026-06-13T00:00:00+08:00\n"
        f"Approved Action: {action}\n"
        f"Approved SHA256: {sha}\n\n"
        "## Decision\n"
        f"Approved: {approved}\n"
    )
    if include_approved_file:
        approved_file_text = approved_file if approved_file_text is None else approved_file_text
        text = text.replace(
            f"Approved SHA256: {sha}",
            f"Approved File: {approved_file_text}\nApproved SHA256: {sha}",
        )
    if include_title:
        text = "# Local Review Approval\n\n" + text
    review.write_text(text, encoding="utf-8")
    return review


class RuntimeTests(unittest.TestCase):
    def test_detect_python_runtime_requires_modern_python(self):
        runtime = detect_python_runtime()
        self.assertGreaterEqual(runtime.version_info[:2], (3, 10))
        self.assertTrue(runtime.executable)

    def test_runtime_context_uses_explicit_repo_root(self):
        with TemporaryDirectory() as tmp:
            env = {"DEVCTL_REPO_ROOT": tmp, "DEVCTL_PRODUCT_LINE": "academic"}
            context = RuntimeContext.from_env(ROOT, env)
            self.assertEqual(context.repo_root, Path(tmp).resolve())
            self.assertEqual(context.tool_root, ROOT)
            self.assertEqual(context.product_line, "academic")


class CheckTests(unittest.TestCase):
    def test_check_file_without_issue_is_accepted_by_argparse(self):
        parser = build_parser()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "issue-draft.md"
            args = parser.parse_args(["check", "academic-issue", "--file", str(path)])
            self.assertEqual(args.file, path)
            self.assertIsNone(args.issue)

    def test_check_without_issue_or_file_fails(self):
        self.assertEqual(main(["check", "academic-issue"]), 1)
        self.assertEqual(main(["check", "tdd-result"]), 1)
        self.assertEqual(main(["check", "claude-package"]), 1)
        self.assertEqual(main(["check", "academic-mr"]), 1)

    def test_check_defaults_resolve_issue_files(self):
        with TemporaryDirectory() as tmp:
            context = RuntimeContext.from_env(ROOT, {"DEVCTL_REPO_ROOT": tmp})
            self.assertEqual(
                resolve_check_file(context, "draft", None, "issue-draft.md"),
                Path(tmp).resolve() / ".xflow" / "issues" / "issue-draft" / "issue-draft.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "tdd-result.md"),
                Path(tmp).resolve() / ".xflow" / "issues" / "issue-1" / "tdd-result.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "claude-task.md"),
                Path(tmp).resolve() / ".xflow" / "issues" / "issue-1" / "claude-task.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "mr-draft.md"),
                Path(tmp).resolve() / ".xflow" / "issues" / "issue-1" / "mr-draft.md",
            )

    def test_academic_issue_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text("<!-- xflow: academic-issue-draft -->\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "<!-- task-type:"):
                check_academic_issue(path)

    def test_academic_issue_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-issue-draft -->\n"
                "<!-- task-type: workflow-test -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Background 🧩\nx\n\n"
                "## Goal 🎯\nx\n\n"
                "## Scope 📌\nx\n\n"
                "## Target Artifacts 📁\n- README.md\n\n"
                "## Acceptance Criteria ✅\nx\n\n"
                "## Verification Plan 🧪\nx\n\n"
                "## Human Review Gate 🧑‍⚖️\nx\n",
                encoding="utf-8",
            )
            check_academic_issue(path)

    def test_academic_issue_rejects_internal_draft_heading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-issue-draft -->\n"
                "# Academic Issue Draft\n\n"
                "<!-- task-type: workflow-test -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Target Artifacts\n- README.md\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "internal draft heading"):
                check_academic_issue(path)

    def test_academic_issue_rejects_generic_internal_draft_heading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-issue-draft -->\n"
                "# Issue Draft\n\n"
                "<!-- task-type: workflow-test -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Target Artifacts\n- README.md\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "internal draft heading"):
                check_academic_issue(path)

    def test_academic_issue_rejects_academic_as_target_branch(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-issue-draft -->\n"
                "<!-- task-type: workflow-test -->\n"
                "Target Branch: academic\n"
                "## Target Artifacts\n- README.md\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Workflow Product Line"):
                check_academic_issue(path)

    def test_academic_issue_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "issue-draft.md"
            path.write_text(
                "<!-- xflow: academic-issue-draft -->\n"
                "<!-- task-type: workflow-test -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Target Artifacts\n- README.md\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "academic-issue", "--file", str(path)]), 0)

    def test_tdd_result_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "tdd-result.md"
            path.parent.mkdir(parents=True)
            path.write_text("# TDD Result\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Issue:"):
                check_tdd_result(path)

    def test_tdd_result_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "tdd-result.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# TDD Result\n\n"
                "Issue: 1\n"
                "Branch: feature/1-test\n"
                "Verified At: 2026-06-13T00:00:00+08:00\n"
                "Executor: Codex\n\n"
                "## Verification Scope\nx\n\n"
                "## Commands\nx\n\n"
                "## Results\nx\n\n"
                "## Risks\nx\n\n"
                "## Human Review Entry\nx\n",
                encoding="utf-8",
            )
            check_tdd_result(path)

    def test_claude_package_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Claude Task Package\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Issue:"):
                check_claude_package(path)

    def test_claude_package_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "Claude Skill: peer-review\n"
                "Skill Source: AcademicForge\n"
                "Invocation: /peer-review\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            check_claude_package(path)

    def test_academicforge_catalog_is_a_verified_command_set(self):
        names = load_academicforge_skill_names()
        self.assertEqual(len(names), 271)
        self.assertFalse(any(name.startswith("#") for name in names))
        self.assertIn("peer-review", names)
        self.assertIn("ppw-reviewer-simulation", names)
        self.assertIn("paper-polish-workflow", names)
        self.assertNotIn("paper-review", names)

    def test_claude_package_accepts_another_verified_forge_skill(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "Claude Skill: ppw-reviewer-simulation\n"
                "Skill Source: AcademicForge\n"
                "Invocation: /ppw-reviewer-simulation\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            check_claude_package(path)

    def test_claude_package_rejects_any_unknown_forge_skill_invocation(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "Claude Skill: definitely-not-a-forge-skill\n"
                "Skill Source: AcademicForge\n"
                "Invocation: /definitely-not-a-forge-skill\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown AcademicForge skill"):
                check_claude_package(path)

    def test_claude_package_rejects_skill_and_invocation_mismatch(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "Claude Skill: peer-review\n"
                "Skill Source: AcademicForge\n"
                "Invocation: /ppw-reviewer-simulation\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match Invocation"):
                check_claude_package(path)

    def test_claude_package_rejects_obsolete_academicforge_skill_field(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "AcademicForge Skill: paper-polish-workflow-skill@unknown\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "obsolete Claude skill field"):
                check_claude_package(path)

    def test_claude_package_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-task.md"
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "Claude Skill: peer-review\n"
                "Skill Source: AcademicForge\n"
                "Invocation: /peer-review\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issues/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "claude-package", "--file", str(path)]), 0)

    def test_academic_mr_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text("<!-- xflow: academic-mr-draft -->\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "<!-- issue:"):
                check_academic_mr(path)

    def test_academic_mr_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "Closes #1\n\n"
                "## Summary 🧭\nx\n\n"
                "## Evidence 🔎\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification ✅\nx\n\n"
                "## Risks ⚠️\nx\n\n"
                "## Review Request 🚀\nx\n",
                encoding="utf-8",
            )
            check_academic_mr(path)

    def test_academic_mr_rejects_internal_draft_heading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "# MR Draft\n\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "internal draft heading"):
                check_academic_mr(path)

    def test_academic_mr_rejects_pr_draft_heading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "# PR Draft\n\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "internal draft heading"):
                check_academic_mr(path)

    def test_academic_mr_rejects_academic_as_target_branch(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "<!-- issue: 1 -->\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Workflow Product Line"):
                check_academic_mr(path)

    def test_academic_mr_rejects_missing_tdd_result_evidence(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "TDD Result:"):
                check_academic_mr(path)

    def test_academic_mr_rejects_missing_local_review_evidence(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Local Review:"):
                check_academic_mr(path)

    def test_academic_mr_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mr-draft.md"
            path.write_text(
                "<!-- xflow: academic-mr-draft -->\n"
                "<!-- issue: 1 -->\n"
                "<!-- workflow-product-line: academic -->\n"
                "<!-- paper-base-branch: main -->\n"
                "<!-- task-branch: feature/1-test -->\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issues/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issues/issue-1/approvals/local-review.md\n\n"
                "## Verification\nx\n\n"
                "## Risks\nx\n\n"
                "## Review Request\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "academic-mr", "--file", str(path)]), 0)


class SubmoduleHygieneTests(unittest.TestCase):
    def init_git_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
        (path / "README.md").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "init", "-q"], check=True)

    def write_gitmodules(self, repo: Path, include_ignore: bool = True) -> None:
        ignore_line = "\n\tignore = untracked" if include_ignore else ""
        (repo / ".gitmodules").write_text(
            "[submodule \".xflow/ops/devctl\"]\n"
            "\tpath = .xflow/ops/devctl\n"
            "\turl = git@github.com:Linkk2000/xflow-devctl.git\n"
            "\tbranch = academic"
            f"{ignore_line}\n"
            "[submodule \".xflow/ops/workflow\"]\n"
            "\tpath = .xflow/ops/workflow\n"
            "\turl = git@github.com:Linkk2000/xflow-skills.git\n"
            "\tbranch = academic"
            f"{ignore_line}\n",
            encoding="utf-8",
        )

    def prepare_parent_with_ops(self, tmp: str, include_ignore: bool = True) -> Path:
        repo = Path(tmp)
        self.write_gitmodules(repo, include_ignore=include_ignore)
        self.init_git_repo(repo / ".xflow" / "ops" / "devctl")
        self.init_git_repo(repo / ".xflow" / "ops" / "workflow")
        return repo

    def test_submodule_hygiene_accepts_clean_ops_repositories(self):
        with TemporaryDirectory() as tmp:
            repo = self.prepare_parent_with_ops(tmp)
            check_submodule_hygiene(repo)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "PATH": original.get("PATH", ""),
                    }
                )
                self.assertEqual(main(["check", "submodule-hygiene"]), 0)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_submodule_hygiene_rejects_tracked_changes_inside_ops(self):
        with TemporaryDirectory() as tmp:
            repo = self.prepare_parent_with_ops(tmp)
            (repo / ".xflow" / "ops" / "devctl" / "README.md").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tracked changes in .xflow/ops/devctl"):
                check_submodule_hygiene(repo)

    def test_submodule_hygiene_rejects_byproducts_inside_ops(self):
        with TemporaryDirectory() as tmp:
            repo = self.prepare_parent_with_ops(tmp)
            cache_dir = repo / ".xflow" / "ops" / "workflow" / "xflow" / "__pycache__"
            cache_dir.mkdir(parents=True)
            (cache_dir / "checks.cpython-312.pyc").write_bytes(b"cache")
            with self.assertRaisesRegex(ValueError, "byproduct in .xflow/ops/workflow"):
                check_submodule_hygiene(repo)

    def test_submodule_hygiene_rejects_missing_ignore_untracked(self):
        with TemporaryDirectory() as tmp:
            repo = self.prepare_parent_with_ops(tmp, include_ignore=False)
            with self.assertRaisesRegex(ValueError, "missing ignore = untracked"):
                check_submodule_hygiene(repo)


class ClaudeRunTests(unittest.TestCase):
    def write_claude_task(self, repo, issue="1"):
        path = Path(repo) / ".xflow" / "issues" / f"issue-{issue}" / "claude-task.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# Claude Task Package\n\n"
            f"Issue: {issue}\n"
            "Claude Skill: peer-review\n"
            "Skill Source: AcademicForge\n"
            "Invocation: /peer-review\n"
            "Input Files:\n"
            "- draft.md: sha256-placeholder\n"
            f"Output File: .xflow/issues/issue-{issue}/claude-result.md\n\n"
            "## Objective\n"
            "Polish the academic paragraph.\n\n"
            "## Constraints\n"
            "Do not change citations.\n\n"
            "## Required Output Format\n"
            "## Summary\n"
            "## Proposed Changes\n"
            "## Risks\n"
            "## Questions\n\n"
            "## Human Review Requirement\n"
            "Human review is required before using this output.\n",
            encoding="utf-8",
        )
        return path

    def install_forge_skill(self, repo, skill="peer-review"):
        path = Path(repo) / ".claude" / "skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# {skill}\n", encoding="utf-8")
        return path

    def install_official_layout_forge_skill(self, repo, skill="peer-review"):
        path = (
            Path(repo)
            / ".claude"
            / "skills"
            / "academic-forge"
            / "skills"
            / "scientific-agent-skills"
            / "skills"
            / skill
            / "SKILL.md"
        )
        path.parent.mkdir(parents=True)
        path.write_text(f"# {skill}\n", encoding="utf-8")
        return path

    def test_claude_run_dry_run_validates_task_without_writing_output(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update({"DEVCTL_REPO_ROOT": str(repo), "DEVCTL_PRODUCT_LINE": "academic"})
                out = StringIO()
                with redirect_stdout(out):
                    result = main(["claude", "run", "--issue", "1", "--dry-run"])
                self.assertEqual(result, 0)
                self.assertIn("claude task package ready", out.getvalue())
                self.assertFalse((repo / ".xflow" / "issues" / "issue-1" / "claude-result.md").exists())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_skills_lists_verified_catalog(self):
        out = StringIO()
        with redirect_stdout(out):
            result = main(["claude", "skills"])
        self.assertEqual(result, 0)
        text = out.getvalue()
        self.assertIn("peer-review", text)
        self.assertIn("ppw-reviewer-simulation", text)
        self.assertNotIn("paper-review", text)

    def test_claude_run_executes_configured_command_and_writes_output(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            self.install_forge_skill(repo)
            fake = repo / "fake_claude.py"
            fake.write_text(
                "import sys\n"
                "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
                "print('## Summary')\n"
                "print('CLAUDE_RESULT')\n"
                "print('## Proposed Changes')\n"
                "print('- none')\n"
                "print('## Risks')\n"
                "print('- none')\n"
                "print('## Questions')\n"
                "print('- none')\n"
                "print('HAS_INVOCATION=' + str(prompt.startswith('/peer-review')))\n"
                "print('HAS_OBJECTIVE=' + str('## Objective' in prompt))\n",
                encoding="utf-8",
            )
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 0)
                output = repo / ".xflow" / "issues" / "issue-1" / "claude-result.md"
                self.assertTrue(output.exists())
                self.assertIn("CLAUDE_RESULT", output.read_text(encoding="utf-8"))
                self.assertIn("HAS_INVOCATION=True", output.read_text(encoding="utf-8"))
                self.assertIn("HAS_OBJECTIVE=True", output.read_text(encoding="utf-8"))
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_rejects_official_nested_layout_without_flat_skill(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            self.install_official_layout_forge_skill(repo)
            fake = repo / "fake_claude.py"
            fake.write_text(
                "print('## Summary')\n"
                "print('ok')\n"
                "print('## Proposed Changes')\n"
                "print('- none')\n"
                "print('## Risks')\n"
                "print('- none')\n"
                "print('## Questions')\n"
                "print('- none')\n",
                encoding="utf-8",
            )
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 1)
                self.assertIn("not Claude-resolvable", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_preserves_skill_invocation_arguments(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            task = self.write_claude_task(repo)
            text = task.read_text(encoding="utf-8").replace(
                "Invocation: /peer-review",
                "Invocation: /peer-review focus=methodology severity=major",
            )
            task.write_text(text, encoding="utf-8")
            self.install_forge_skill(repo)
            fake = repo / "fake_claude.py"
            fake.write_text(
                "import sys\n"
                "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
                "print('## Summary')\n"
                "print(prompt.splitlines()[0])\n"
                "print('## Proposed Changes')\n"
                "print('- none')\n"
                "print('## Risks')\n"
                "print('- none')\n"
                "print('## Questions')\n"
                "print('- none')\n",
                encoding="utf-8",
            )
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 0)
                output = repo / ".xflow" / "issues" / "issue-1" / "claude-result.md"
                self.assertIn("/peer-review focus=methodology severity=major", output.read_text(encoding="utf-8"))
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_appends_configured_cli_arguments_before_prompt(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            self.install_forge_skill(repo)
            seen = repo / "seen-args.txt"
            fake = repo / "fake_claude.py"
            fake.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({str(seen)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
                "print('## Summary')\n"
                "print('ok')\n"
                "print('## Proposed Changes')\n"
                "print('- none')\n"
                "print('## Risks')\n"
                "print('- none')\n"
                "print('## Questions')\n"
                "print('- none')\n",
                encoding="utf-8",
            )
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                        "DEVCTL_CLAUDE_ARGS": "--model sonnet --permission-mode dontAsk",
                    }
                )
                result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 0)
                args = seen.read_text(encoding="utf-8").splitlines()
                self.assertEqual(args[:4], ["--model", "sonnet", "--permission-mode", "dontAsk"])
                self.assertIn("-p", args)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_rejects_generic_success_output(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            self.install_forge_skill(repo)
            fake = repo / "fake_claude.py"
            fake.write_text(
                "print('I can help with academic writing. What would you like me to do?')\n",
                encoding="utf-8",
            )
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 1)
                self.assertIn("Claude output appears non-actionable", err.getvalue())
                self.assertFalse((repo / ".xflow" / "issues" / "issue-1" / "claude-result.md").exists())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_rejects_missing_academicforge_install(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            fake = repo / "fake_claude.py"
            fake.write_text("print('## Summary')\n", encoding="utf-8")
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 1)
                self.assertIn("AcademicForge skill is not installed", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_rejects_missing_installed_child_skill(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            root = repo / ".claude" / "skills" / "academic-forge" / "SKILL.md"
            root.parent.mkdir(parents=True)
            root.write_text("# AcademicForge\n", encoding="utf-8")
            fake = repo / "fake_claude.py"
            fake.write_text("print('## Summary')\n", encoding="utf-8")
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["claude", "run", "--issue", "1"])
                self.assertEqual(result, 1)
                self.assertIn("not Claude-resolvable", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_doctor_reports_missing_academicforge_without_installing(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake = repo / "fake_claude.py"
            fake.write_text("print('fake')\n", encoding="utf-8")
            config = repo / ".claude.json"
            config.write_text("{}", encoding="utf-8")
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                        "DEVCTL_CLAUDE_CONFIG": str(config),
                    }
                )
                out = StringIO()
                with redirect_stdout(out):
                    result = main(["claude", "doctor"])
                self.assertEqual(result, 1)
                text = out.getvalue()
                self.assertIn("claude_cli: ok", text)
                self.assertIn("academicforge: missing", text)
                self.assertIn("No installation was performed.", text)
                self.assertIn(".claude", text)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_doctor_rejects_mcp_config_without_skill_directory(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake = repo / "fake_claude.py"
            fake.write_text("print('fake')\n", encoding="utf-8")
            config = repo / ".claude.json"
            config.write_text('{"mcpServers": {"academicforge": {}}}', encoding="utf-8")
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                        "DEVCTL_CLAUDE_CONFIG": str(config),
                    }
                )
                out = StringIO()
                with redirect_stdout(out):
                    result = main(["claude", "doctor"])
                self.assertEqual(result, 1)
                text = out.getvalue()
                self.assertIn("claude_cli: ok", text)
                self.assertIn("academicforge: missing", text)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_doctor_accepts_project_flat_academicforge_skill(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake = repo / "fake_claude.py"
            fake.write_text("print('fake')\n", encoding="utf-8")
            skill = self.install_forge_skill(repo)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                out = StringIO()
                with redirect_stdout(out):
                    result = main(["claude", "doctor"])
                self.assertEqual(result, 0)
                text = out.getvalue()
                self.assertIn("claude_cli: ok", text)
                self.assertIn("academicforge: ok", text)
                self.assertIn(str(skill), text)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_doctor_rejects_official_nested_layout_without_flat_skill(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            fake = repo / "fake_claude.py"
            fake.write_text("print('fake')\n", encoding="utf-8")
            child = self.install_official_layout_forge_skill(repo)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_CLAUDE_COMMAND": f"{sys.executable} {fake}",
                    }
                )
                out = StringIO()
                with redirect_stdout(out):
                    result = main(["claude", "doctor"])
                self.assertEqual(result, 1)
                text = out.getvalue()
                self.assertIn("academicforge: missing", text)
                self.assertIn("academicforge_source_root", text)
                self.assertIn(str(child.parents[4]), text)
            finally:
                os.environ.clear()
                os.environ.update(original)


class ApprovalTests(unittest.TestCase):
    def test_require_remote_approval_accepts_matching_local_review(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact)

            require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_accepts_repo_relative_paths_from_outside_cwd(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            outside = root / "outside"
            outside.mkdir(parents=True)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            relative_artifact = Path(".xflow") / "issues" / "issue-draft" / "issue-draft.md"
            write_local_review(
                repo,
                "draft",
                "issue-create",
                artifact,
                approved_file_text=relative_artifact.as_posix(),
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(outside)
                require_remote_approval(repo, "issue-create", relative_artifact, "draft")
            finally:
                os.chdir(old_cwd)

    def test_require_remote_approval_accepts_absolute_approved_file(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, approved_file_text=str(artifact))

            require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_approved_file_mismatch(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(
                repo,
                "draft",
                "issue-create",
                artifact,
                approved_file_text=".xflow/issues/issue-draft/other.md",
            )

            with self.assertRaisesRegex(ValueError, "approved file mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_local_review_rejects_missing_title(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_title=False)

            with self.assertRaisesRegex(ValueError, "# Local Review Approval"):
                check_local_review_file(repo, "draft", artifact)

    def test_require_remote_approval_rejects_missing_title(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_title=False)

            with self.assertRaisesRegex(ValueError, "# Local Review Approval"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_missing_approved_file(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_approved_file=False)

            with self.assertRaisesRegex(ValueError, "Approved File:"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_missing_artifact_raises_value_error(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            write_local_review(repo, "draft", "issue-create", artifact, sha="0" * 64)

            with self.assertRaisesRegex(ValueError, "missing approved artifact"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_issue_create_missing_artifact_returns_controlled_error(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            write_local_review(repo, "draft", "issue-create", artifact, sha="0" * 64)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "DEVCTL_SKIP_PROVIDER_LOAD": "1",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["issue", "create", "missing artifact", "--body-file", str(artifact)])
                self.assertEqual(result, 1)
                self.assertIn("[ERROR] missing approved artifact", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_require_remote_approval_rejects_wrong_action(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "wrong-action", artifact)

            with self.assertRaisesRegex(ValueError, "action mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_requires_local_review_file(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")

            with self.assertRaisesRegex(ValueError, "academic local approval required"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_hash_mismatch(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, sha="0" * 64)

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")


class IssueProviderTests(unittest.TestCase):
    def test_issue_create_requires_github_token_after_local_approval(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("<!-- xflow: academic-issue-draft -->\n\n## Background\nBody text\n", encoding="utf-8")
            write_local_review(repo, "draft", "issue-create", artifact)

            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "XFLOW_PLATFORM": "github",
                        "DEVCTL_OWNER": "Linkk2000",
                        "DEVCTL_REPO": "paper-demo",
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["issue", "create", "Academic draft", "--body-file", str(artifact)])
                self.assertEqual(result, 1)
                self.assertIn("missing GitHub token", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_issue_create_posts_to_github_after_local_approval(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "accept": self.headers.get("Accept"),
                        "body": json.loads(body),
                    }
                )
                payload = json.dumps({"number": 7, "html_url": "https://github.example/issues/7"}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                repo = Path(tmp)
                artifact = repo / ".xflow" / "issues" / "issue-draft" / "issue-draft.md"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("<!-- xflow: academic-issue-draft -->\n\n## Background\nBody text\n", encoding="utf-8")
                write_local_review(repo, "draft", "issue-create", artifact)

                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": str(repo),
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "DEVCTL_OWNER": "Linkk2000",
                            "DEVCTL_REPO": "paper-demo",
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(
                            [
                                "issue",
                                "create",
                                "Academic draft",
                                "--body-file",
                                str(artifact),
                                "--labels",
                                "academic,tdd",
                            ]
                        )
                    self.assertEqual(result, 0)
                    self.assertIn("Issue #7 created", out.getvalue())
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]["path"], "/repos/Linkk2000/paper-demo/issues")
                    self.assertEqual(requests[0]["authorization"], "Bearer token-value")
                    self.assertEqual(requests[0]["accept"], "application/vnd.github+json")
                    self.assertEqual(
                        requests[0]["body"],
                        {
                            "title": "Academic draft",
                            "body": "<!-- xflow: academic-issue-draft -->\n\n## Background\nBody text\n",
                            "labels": ["academic", "tdd"],
                        },
                    )
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_issue_list_reads_github_and_filters_pull_request_items(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                payload = json.dumps(
                    [
                        {"number": 1, "state": "open", "title": "Draft polish"},
                        {"number": 2, "state": "open", "title": "PR mirror", "pull_request": {"url": "x"}},
                    ]
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": tmp,
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "DEVCTL_OWNER": "Linkk2000",
                            "DEVCTL_REPO": "paper-demo",
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(["issue", "list", "--state", "open", "--limit", "2"])
                    self.assertEqual(result, 0)
                    text = out.getvalue()
                    self.assertIn("#1\t[open]\tDraft polish", text)
                    self.assertNotIn("PR mirror", text)
                    self.assertEqual(len(requests), 1)
                    self.assertTrue(requests[0]["path"].startswith("/repos/Linkk2000/paper-demo/issues?"))
                    self.assertIn("state=open", requests[0]["path"])
                    self.assertIn("per_page=2", requests[0]["path"])
                    self.assertEqual(requests[0]["authorization"], "Bearer token-value")
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_issue_show_reads_github_details(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(
                    {
                        "number": 7,
                        "state": "open",
                        "title": "Academic draft",
                        "body": "Body text",
                        "html_url": "https://github.example/issues/7",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": tmp,
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "DEVCTL_OWNER": "Linkk2000",
                            "DEVCTL_REPO": "paper-demo",
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(["issue", "show", "7"])
                    self.assertEqual(result, 0)
                    text = out.getvalue()
                    self.assertIn("#7 [open] Academic draft", text)
                    self.assertIn("Body text", text)
                    self.assertIn("https://github.example/issues/7", text)
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_issue_comment_posts_after_local_approval(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                requests.append({"path": self.path, "body": json.loads(self.rfile.read(length).decode("utf-8"))})
                payload = json.dumps({"html_url": "https://github.example/issues/7#comment"}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                repo = Path(tmp)
                comment = repo / ".xflow" / "issues" / "issue-7" / "comment-draft.md"
                comment.parent.mkdir(parents=True)
                comment.write_text("Reviewed comment\n", encoding="utf-8")
                write_local_review(repo, "7", "issue-comment", comment)
                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": str(repo),
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "DEVCTL_OWNER": "Linkk2000",
                            "DEVCTL_REPO": "paper-demo",
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(["issue", "comment", "7", "--body-file", str(comment)])
                    self.assertEqual(result, 0)
                    self.assertIn("Comment posted on Issue #7", out.getvalue())
                    self.assertEqual(requests, [{"path": "/repos/Linkk2000/paper-demo/issues/7/comments", "body": {"body": "Reviewed comment\n"}}])
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_issue_comment_rejects_inline_body_in_academic_mode(self):
        with TemporaryDirectory() as tmp:
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update({"DEVCTL_REPO_ROOT": tmp, "DEVCTL_PRODUCT_LINE": "academic"})
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["issue", "comment", "7", "--body", "inline"])
                self.assertEqual(result, 1)
                self.assertIn("academic issue comment requires --body-file", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_issue_close_patches_after_local_approval(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_PATCH(self):
                length = int(self.headers.get("Content-Length", "0"))
                requests.append({"path": self.path, "body": json.loads(self.rfile.read(length).decode("utf-8"))})
                payload = json.dumps({"number": 7, "state": "closed", "html_url": "https://github.example/issues/7"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                repo = Path(tmp)
                walkthrough = repo / ".xflow" / "issues" / "issue-7" / "walkthrough.md"
                walkthrough.parent.mkdir(parents=True)
                walkthrough.write_text("Ready to close\n", encoding="utf-8")
                write_local_review(repo, "7", "issue-close", walkthrough)
                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": str(repo),
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "DEVCTL_OWNER": "Linkk2000",
                            "DEVCTL_REPO": "paper-demo",
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(["issue", "close", "7"])
                    self.assertEqual(result, 0)
                    self.assertIn("Issue #7 closed", out.getvalue())
                    self.assertEqual(requests, [{"path": "/repos/Linkk2000/paper-demo/issues/7", "body": {"state": "closed"}}])
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class PullRequestProviderTests(unittest.TestCase):
    def init_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
        (path / "README.md").write_text("paper\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "switch", "-c", "feature/1-polish"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", "git@github.com:Linkk2000/paper-demo.git"],
            check=True,
        )

    def test_git_mr_posts_to_github_after_local_approval(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                payload = json.dumps({"number": 9, "html_url": "https://github.example/pull/9"}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self.init_repo(repo)
                mr = repo / ".xflow" / "issues" / "issue-1" / "mr-draft.md"
                mr.parent.mkdir(parents=True)
                mr.write_text("<!-- xflow: academic-mr-draft -->\n\nCloses #1\n\n## Summary\n- Ready\n", encoding="utf-8")
                write_local_review(repo, "1", "git-mr", mr)

                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": str(repo),
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "DEVCTL_SKIP_PUSH": "1",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "PATH": original.get("PATH", ""),
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(
                            [
                                "git",
                                "mr",
                                "--title",
                                "论文润色",
                                "--body-file",
                                str(mr),
                                "--base",
                                "main",
                                "--issue",
                                "1",
                            ]
                        )
                    self.assertEqual(result, 0)
                    self.assertIn("PR #9 created", out.getvalue())
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]["path"], "/repos/Linkk2000/paper-demo/pulls")
                    self.assertEqual(requests[0]["authorization"], "Bearer token-value")
                    self.assertEqual(
                        requests[0]["body"],
                        {
                            "title": "论文润色",
                            "body": "<!-- xflow: academic-mr-draft -->\n\nCloses #1\n\n## Summary\n- Ready\n",
                            "head": "feature/1-polish",
                            "base": "main",
                        },
                    )
                    stored = subprocess.run(
                        ["git", "-C", str(repo), "config", "--local", "--get", "devctl.pr"],
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                    ).stdout.strip()
                    self.assertEqual(stored, "9")
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_git_mr_rejects_inline_body_in_academic_mode(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.init_repo(repo)
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "DEVCTL_REPO_ROOT": str(repo),
                        "DEVCTL_PRODUCT_LINE": "academic",
                        "XFLOW_PLATFORM": "github",
                        "GITHUB_TOKEN": "token-value",
                        "PATH": original.get("PATH", ""),
                    }
                )
                err = StringIO()
                with redirect_stderr(err):
                    result = main(["git", "mr", "--title", "t", "--body", "inline", "--issue", "1"])
                self.assertEqual(result, 1)
                self.assertIn("academic git mr requires --body-file", err.getvalue())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_git_pr_get_reads_github_details(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(
                    {
                        "number": 9,
                        "state": "open",
                        "title": "Paper polish",
                        "html_url": "https://github.example/pull/9",
                        "head": {"ref": "feature/1-polish"},
                        "base": {"ref": "main"},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self.init_repo(repo)
                original = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(
                        {
                            "DEVCTL_REPO_ROOT": str(repo),
                            "DEVCTL_PRODUCT_LINE": "academic",
                            "XFLOW_PLATFORM": "github",
                            "GITHUB_API_BASE": f"http://127.0.0.1:{server.server_port}",
                            "GITHUB_TOKEN": "token-value",
                            "PATH": original.get("PATH", ""),
                        }
                    )
                    out = StringIO()
                    with redirect_stdout(out):
                        result = main(["git", "pr-get", "9"])
                    self.assertEqual(result, 0)
                    text = out.getvalue()
                    self.assertIn("#9 [open] Paper polish", text)
                    self.assertIn("feature/1-polish -> main", text)
                    self.assertIn("https://github.example/pull/9", text)
                finally:
                    os.environ.clear()
                    os.environ.update(original)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
