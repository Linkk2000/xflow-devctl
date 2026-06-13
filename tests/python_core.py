import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    check_tdd_result,
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
    review = Path(repo_root) / ".xflow" / f"issue-{issue}" / "approvals" / "local-review.md"
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
                Path(tmp).resolve() / ".xflow" / "issue-draft" / "issue-draft.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "tdd-result.md"),
                Path(tmp).resolve() / ".xflow" / "issue-1" / "tdd-result.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "claude-task.md"),
                Path(tmp).resolve() / ".xflow" / "issue-1" / "claude-task.md",
            )
            self.assertEqual(
                resolve_check_file(context, "1", None, "mr-draft.md"),
                Path(tmp).resolve() / ".xflow" / "issue-1" / "mr-draft.md",
            )

    def test_academic_issue_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Academic Issue Draft\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Task Type:"):
                check_academic_issue(path)

    def test_academic_issue_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-draft" / "issue-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Academic Issue Draft\n\n"
                "Task Type: workflow-test\n"
                "Target Branch: academic\n"
                "Target Artifacts:\n- README.md\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            check_academic_issue(path)

    def test_academic_issue_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "issue-draft.md"
            path.write_text(
                "# Academic Issue Draft\n\n"
                "Task Type: workflow-test\n"
                "Target Branch: academic\n"
                "Target Artifacts:\n- README.md\n\n"
                "## Background\nx\n\n"
                "## Goal\nx\n\n"
                "## Scope\nx\n\n"
                "## Acceptance Criteria\nx\n\n"
                "## Verification Plan\nx\n\n"
                "## Human Review Gate\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "academic-issue", "--file", str(path)]), 0)

    def test_tdd_result_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "tdd-result.md"
            path.parent.mkdir(parents=True)
            path.write_text("# TDD Result\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Issue:"):
                check_tdd_result(path)

    def test_tdd_result_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "tdd-result.md"
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
            path = Path(tmp) / ".xflow" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Claude Task Package\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Issue:"):
                check_claude_package(path)

    def test_claude_package_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "claude-task.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "AcademicForge Skill: paper-polish-workflow-skill@unknown\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            check_claude_package(path)

    def test_claude_package_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-task.md"
            path.write_text(
                "# Claude Task Package\n\n"
                "Issue: 1\n"
                "AcademicForge Skill: paper-polish-workflow-skill@unknown\n"
                "Input Files:\n"
                "- draft.md: sha256-placeholder\n"
                "Output File: .xflow/issue-1/claude-result.md\n\n"
                "## Objective\nx\n\n"
                "## Constraints\nx\n\n"
                "## Required Output Format\nx\n\n"
                "## Human Review Requirement\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "claude-package", "--file", str(path)]), 0)

    def test_academic_mr_rejects_missing_sections(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text("# MR Draft\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Issue:"):
                check_academic_mr(path)

    def test_academic_mr_accepts_valid_template(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# MR Draft\n\n"
                "Issue: 1\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issue-1/approvals/local-review.md\n\n"
                "## Remote Actions Requested\nx\n",
                encoding="utf-8",
            )
            check_academic_mr(path)

    def test_academic_mr_rejects_missing_tdd_result_evidence(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# MR Draft\n\n"
                "Issue: 1\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- Local Review: .xflow/issue-1/approvals/local-review.md\n\n"
                "## Remote Actions Requested\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "TDD Result:"):
                check_academic_mr(path)

    def test_academic_mr_rejects_missing_local_review_evidence(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xflow" / "issue-1" / "mr-draft.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# MR Draft\n\n"
                "Issue: 1\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issue-1/tdd-result.md\n\n"
                "## Remote Actions Requested\nx\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Local Review:"):
                check_academic_mr(path)

    def test_academic_mr_accepts_explicit_file_without_issue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mr-draft.md"
            path.write_text(
                "# MR Draft\n\n"
                "Issue: 1\n"
                "Target Branch: academic\n\n"
                "## Summary\nx\n\n"
                "## Evidence\n"
                "- TDD Result: .xflow/issue-1/tdd-result.md\n"
                "- Local Review: .xflow/issue-1/approvals/local-review.md\n\n"
                "## Remote Actions Requested\nx\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["check", "academic-mr", "--file", str(path)]), 0)


class ClaudeRunTests(unittest.TestCase):
    def write_claude_task(self, repo, issue="1"):
        path = Path(repo) / ".xflow" / f"issue-{issue}" / "claude-task.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# Claude Task Package\n\n"
            f"Issue: {issue}\n"
            "AcademicForge Skill: paper-polish-workflow-skill@unknown\n"
            "Input Files:\n"
            "- draft.md: sha256-placeholder\n"
            f"Output File: .xflow/issue-{issue}/claude-result.md\n\n"
            "## Objective\n"
            "Polish the academic paragraph.\n\n"
            "## Constraints\n"
            "Do not change citations.\n\n"
            "## Required Output Format\n"
            "Return Markdown only.\n\n"
            "## Human Review Requirement\n"
            "Human review is required before using this output.\n",
            encoding="utf-8",
        )
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
                self.assertFalse((repo / ".xflow" / "issue-1" / "claude-result.md").exists())
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_run_executes_configured_command_and_writes_output(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.write_claude_task(repo)
            fake = repo / "fake_claude.py"
            fake.write_text(
                "import sys\n"
                "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
                "print('CLAUDE_RESULT')\n"
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
                output = repo / ".xflow" / "issue-1" / "claude-result.md"
                self.assertTrue(output.exists())
                self.assertIn("CLAUDE_RESULT", output.read_text(encoding="utf-8"))
                self.assertIn("HAS_OBJECTIVE=True", output.read_text(encoding="utf-8"))
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
                self.assertIn("claude mcp add academicforge npx @hughyau/academicforge@latest", text)
            finally:
                os.environ.clear()
                os.environ.update(original)

    def test_claude_doctor_accepts_registered_academicforge(self):
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
                self.assertEqual(result, 0)
                text = out.getvalue()
                self.assertIn("claude_cli: ok", text)
                self.assertIn("academicforge: ok", text)
            finally:
                os.environ.clear()
                os.environ.update(original)


class ApprovalTests(unittest.TestCase):
    def test_require_remote_approval_accepts_matching_local_review(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
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
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            relative_artifact = Path(".xflow") / "issue-draft" / "issue-draft.md"
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
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, approved_file_text=str(artifact))

            require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_approved_file_mismatch(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(
                repo,
                "draft",
                "issue-create",
                artifact,
                approved_file_text=".xflow/issue-draft/other.md",
            )

            with self.assertRaisesRegex(ValueError, "approved file mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_local_review_rejects_missing_title(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_title=False)

            with self.assertRaisesRegex(ValueError, "# Local Review Approval"):
                check_local_review_file(repo, "draft", artifact)

    def test_require_remote_approval_rejects_missing_title(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_title=False)

            with self.assertRaisesRegex(ValueError, "# Local Review Approval"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_missing_approved_file(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, include_approved_file=False)

            with self.assertRaisesRegex(ValueError, "Approved File:"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_missing_artifact_raises_value_error(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            write_local_review(repo, "draft", "issue-create", artifact, sha="0" * 64)

            with self.assertRaisesRegex(ValueError, "missing approved artifact"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_issue_create_missing_artifact_returns_controlled_error(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
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
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "wrong-action", artifact)

            with self.assertRaisesRegex(ValueError, "action mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_requires_local_review_file(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")

            with self.assertRaisesRegex(ValueError, "academic local approval required"):
                require_remote_approval(repo, "issue-create", artifact, "draft")

    def test_require_remote_approval_rejects_hash_mismatch(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            artifact = repo / ".xflow" / "issue-draft" / "issue-draft.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"academic draft\n")
            write_local_review(repo, "draft", "issue-create", artifact, sha="0" * 64)

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                require_remote_approval(repo, "issue-create", artifact, "draft")


if __name__ == "__main__":
    unittest.main()
