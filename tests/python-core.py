from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.checks import write_pr_state_update_suggestion
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


def run_devctl_with_env(repo_root: Path, extra_env: dict[str, str], *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env)
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


def test_env_loading_policy(repo: Path) -> None:
    fake_home = repo / "fake-home"
    global_env = fake_home / ".xflow" / "env.local"
    project_env = repo / ".xflow" / "local" / "env.local"
    write(global_env, "GITHUB_TOKEN=global-gh\nGITEE_TOKEN=global-ge\nXFLOW_PLATFORM=github\n")
    write(project_env, "XFLOW_PLATFORM=gitee\n")
    env = {"USERPROFILE": str(fake_home), "HOME": str(fake_home), "DEVCTL_REPO_ROOT": str(repo)}
    loaded = load_env_files(env)
    assert global_env in loaded
    assert project_env in loaded
    assert env["GITHUB_TOKEN"] == "global-gh"
    assert env["GITEE_TOKEN"] == "global-ge"
    assert env["XFLOW_PLATFORM"] == "gitee"

    project_env.unlink()
    env = {"USERPROFILE": str(fake_home), "HOME": str(fake_home), "DEVCTL_REPO_ROOT": str(repo)}
    load_env_files(env)
    assert env["GITHUB_TOKEN"] == "global-gh"
    assert env["GITEE_TOKEN"] == "global-ge"
    assert "XFLOW_PLATFORM" not in env


class RecordingApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.requests.append({"method": "GET", "path": parsed.path, "query": query})
        if parsed.path.endswith("/issues/12"):
            self.send_json('{"number":"12","state":"open","title":"Gitee Issue","body":"body","html_url":"https://gitee.test/issue/12"}')
        elif parsed.path.endswith("/issues"):
            self.send_json('[{"number":"12","state":"open","title":"Gitee Issue","body":"body","html_url":"https://gitee.test/issue/12"}]')
        elif parsed.path.endswith("/pulls/7"):
            self.send_json('{"number":"7","state":"open","title":"Gitee PR","html_url":"https://gitee.test/pulls/7"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        form = self.read_form()
        self.requests.append({"method": "POST", "path": parsed.path, "form": form})
        if parsed.path.endswith("/Linkk2000/issues"):
            self.send_json('{"number":"12","html_url":"https://gitee.test/issue/12"}')
        elif parsed.path.endswith("/issues/12/comments"):
            self.send_json('{"id":"99","body":"comment"}')
        elif parsed.path.endswith("/pulls"):
            self.send_json('{"number":"7","html_url":"https://gitee.test/pulls/7"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        form = self.read_form()
        self.requests.append({"method": "PATCH", "path": parsed.path, "form": form})
        if parsed.path.endswith("/Linkk2000/issues/12"):
            self.send_json('{"number":"12","state":"closed","title":"Gitee Issue"}')
        else:
            self.send_response(404)
            self.end_headers()


class RecordingApiServer:
    def __enter__(self) -> "RecordingApiServer":
        RecordingApiHandler.requests = []
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


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        git(repo, "remote", "add", "origin", "git@gitee.com:Linkk2000/paper-demo.git")
        test_env_loading_policy(repo)

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
        assert f"env_file: {env_file}" in preflight.stdout
        assert "GITHUB_TOKEN=SET" in preflight.stdout
        assert "GITEE_TOKEN=SET" in preflight.stdout
        assert "secret-token-value" not in preflight.stdout
        assert "other-secret" not in preflight.stdout

        with RecordingApiServer() as server:
            gitee_env = {"GITEE_API_BASE": server.base_url, "GITEE_TOKEN": "gitee-token"}
            created = create_issue(repo, "Gitee title", "Gitee body", "bug,docs", gitee_env)
            assert created.number == "12"
            rows = list_issues(repo, "open", 20, gitee_env)
            assert rows[0]["number"] == "12"
            shown = show_issue(repo, "12", gitee_env)
            assert shown["title"] == "Gitee Issue"
            comment = comment_issue(repo, "12", "Gitee comment", gitee_env)
            assert comment["id"] == "99"
            closed = close_issue(repo, "12", gitee_env)
            assert closed["state"] == "closed"
            pr = create_pull_request(repo, "Gitee PR", "PR body", "feature/demo", "main", gitee_env)
            assert pr.number == "7"
            fetched_pr = get_pull_request(repo, "7", gitee_env)
            assert fetched_pr["title"] == "Gitee PR"

            assert server.requests[0]["method"] == "POST"
            assert server.requests[0]["path"] == "/repos/Linkk2000/issues"
            assert server.requests[0]["form"]["repo"] == "paper-demo"
            assert server.requests[0]["form"]["access_token"] == "gitee-token"
            assert server.requests[3]["path"] == "/repos/Linkk2000/paper-demo/issues/12/comments"
            assert server.requests[4]["method"] == "PATCH"
            assert server.requests[4]["path"] == "/repos/Linkk2000/issues/12"
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
        assert "Suggested State: S9_REMOTE_REVIEW_AND_CI" in suggestion.read_text(encoding="utf-8")

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
