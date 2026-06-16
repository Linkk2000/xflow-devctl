from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]


def run_devctl(repo_root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEVCTL_REPO_ROOT"] = str(repo_root)
    env["DEVCTL_TOOL_ROOT"] = str(OPS_ROOT)
    env["DEVCTL_OPS_ROOT"] = str(OPS_ROOT)
    env["PYTHONPATH"] = str(OPS_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("DEVCTL_SKIP_PROVIDER_LOAD", "1")
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=repo_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected exit {expect}, got {result.returncode}: {' '.join(args)}")
    return result


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")

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
        run_devctl(repo, "check", "issue-draft", "--file", str(issue_file))
        run_devctl(repo, "check", "mr-draft", "--issue", "1")

        bad_issue = issue_file.with_name("bad-issue.md")
        shutil.copyfile(issue_file, bad_issue)
        bad_issue.write_text("# Issue Draft\n" + bad_issue.read_text(encoding="utf-8"), encoding="utf-8")
        run_devctl(repo, "check", "issue-draft", "--file", str(bad_issue), expect=1)

        run_devctl(repo, "approval", "prepare", "--issue", "draft", "--action", "issue-create", "--file", str(issue_file))
        approval = repo / ".xflow" / "issues" / "issue-draft" / "approvals" / "local-review.md"
        text = approval.read_text(encoding="utf-8")
        digest = hashlib.sha256(issue_file.read_bytes()).hexdigest()
        assert f"Approved SHA256: {digest}" in text
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(issue_file), expect=1)
        approval.write_text(text.replace("Approved: no", "Approved: yes").replace(digest, digest.upper()), encoding="utf-8")
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(issue_file))
        run_devctl(repo, "issue", "create", "Review gate", "--body-file", str(issue_file))

        templates = repo / ".xflow" / "ops" / "workflow" / "templates"
        write(
            templates / "ai-rules.json",
            """{
  "rules": [
    {
      "id": "codex",
      "target": "AGENTS.md",
      "template": "codex-agents.md",
      "description": "Codex project rules"
    }
  ]
}
""",
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
