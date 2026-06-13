import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xflow.env import RuntimeContext, detect_python_runtime
from xflow.checks import check_academic_issue, check_tdd_result
from xflow.cli import build_parser, main, resolve_check_file


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


if __name__ == "__main__":
    unittest.main()
