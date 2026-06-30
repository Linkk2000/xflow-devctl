from __future__ import annotations

import hashlib
import json
import os
import re
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


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


def test_python_core_rejects_inline_remote_bodies(repo: Path) -> None:
    run_devctl(repo, "issue", "create", "Inline body", "--body", "simple status update", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "line1\nline2", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", r"line1\nline2", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "uses `code`", "--no-local-review", expect=1)
    run_devctl(repo, "issue", "create", "Inline body", "--body", "uses $(cmd)", "--no-local-review", expect=1)


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

    status = run_devctl(work, "git", "status").stdout
    assert "branch:  main" in status
    assert "worktree: clean" in status

    run_devctl(work, "git", "start", "wsl-free", "--issue", "9", "--base", "main")
    assert git_text(work, "branch", "--show-current") == "feat/9-wsl-free"

    status = run_devctl(work, "git", "status").stdout
    assert "branch:  feat/9-wsl-free" in status
    assert "issue:   #9" in status

    write(work / "feature.txt", "python core git command\n")
    message = "Add Python core git commands"
    msg = run_devctl(work, "git", "commit-msg", "-a", "-m", message).stdout
    assert message in msg
    run_devctl(work, "git", "commit-msg", "-a", "-c", "-m", message)
    assert message in git_text(work, "log", "-1", "--pretty=%B")

    run_devctl(work, "git", "done", "--force", "--base", "main")
    assert git_text(work, "branch", "--show-current") == "main"
    assert "feat/9-wsl-free" not in git_text(work, "branch", "--format=%(refname:short)")

    app_status = run_devctl(work, "app", "status", "--port", "65534").stdout
    assert "frontend process: not running" in app_status
    assert "frontend HTTP: unavailable" in app_status
    app_stop = run_devctl(work, "app", "stop-frontend", "--port", "65534").stdout
    assert "no recorded frontend process" in app_stop


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
    assert "devctl git push" in mr_before_push.stderr
    assert branch not in git_text(origin, "branch", "--format=%(refname:short)")

    run_devctl(work, "approval", "prepare", "--issue", "8", "--action", "git-push", "--file", str(walkthrough), "--force")
    approval.write_text(approval.read_text(encoding="utf-8").replace("Approved: no", "Approved: yes"), encoding="utf-8")
    push_result = run_devctl(work, "git", "push", "--issue", "8", "--file", str(walkthrough))
    assert f"pushed {branch}" in push_result.stdout
    assert branch in git_text(origin, "branch", "--format=%(refname:short)")

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

    latest_remote_subject = git_text(origin, "log", f"refs/heads/{branch}", "-1", "--pretty=%s")
    assert latest_remote_subject == "chore(xflow): 回填 PR #42 状态"
    remote_task = git_text(origin, "show", f"refs/heads/{branch}:.xflow/current-task.md")
    assert "State: S9_REMOTE_REVIEW_AND_CI" in remote_task
    assert "PR: 42" in remote_task
    assert "PR URL: https://github.test/pulls/42" in remote_task
    remote_suggestion = git_text(origin, "show", f"refs/heads/{branch}:.xflow/issues/issue-8/state-update-suggestion.md")
    assert "PR: 42" in remote_suggestion


def test_ai_call_guidance_is_visible(repo: Path) -> None:
    issue_help = run_devctl(repo, "issue", "create", "--help").stdout
    assert "AI call recipes" in issue_help
    assert "Plain unattended issue" in issue_help
    assert "Issue/comment image attachments are disabled" in issue_help
    assert "Do not use GitHub release assets as an issue image store" in issue_help
    assert "For non-image files, use a reviewed manifest" in issue_help

    publish_help = run_devctl(repo, "attachment", "publish", "--help").stdout
    assert "Attachment publishing" in publish_help
    assert "writes publishedUrl" in publish_help
    assert "Do not use this backend as issue/comment image storage" in publish_help

    git_help = run_devctl(repo, "git", "--help").stdout
    assert "push" in git_help
    assert "mr" in git_help

    help_text = (OPS_ROOT / "help.txt").read_text(encoding="utf-8")
    assert "AI call recipes" in help_text
    assert "Plain unattended issue" in help_text
    assert "Issue/comment image attachments are disabled" in help_text
    assert "Do not use GitHub release assets as an issue image store" in help_text
    assert "devctl git push --issue" in help_text
    assert "state backfill commit" in help_text
    assert "Do not run bare bash/Git-Bash/WSL for normal XFlow validation on Windows" in help_text

    readme_text = (OPS_ROOT / "README.md").read_text(encoding="utf-8")
    assert "AI Call Recipes" in readme_text
    assert "Plain unattended issue" in readme_text
    assert "Issue/comment image attachments are disabled" in readme_text
    assert "GitHub release assets" in readme_text
    assert "issue image store" in readme_text
    assert "devctl git push --issue" in readme_text
    assert "state backfill commit" in readme_text
    assert "Normal Git, Issue, Attachment, Approval, Rules, Migration, and App commands route" in readme_text
    assert "repository-local `devctl.ps1`" in readme_text
    assert "do not run bare `bash`, Git Bash, or WSL for normal XFlow validation" in readme_text


class RecordingApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    release_created: bool = False

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
        elif parsed.path.endswith("/issues/12"):
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
            self.send_json('{"number":"12","html_url":"https://gitee.test/issue/12"}')
        elif parsed.path.endswith("/issues/12/comments"):
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
            self.send_json('{"number":42,"html_url":"https://github.test/issue/42"}')
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
        if parsed.path.endswith("/Linkk2000/issues/12"):
            self.send_json('{"number":"12","state":"closed","title":"Gitee Issue"}')
        else:
            self.send_response(404)
            self.end_headers()


class RecordingApiServer:
    def __enter__(self) -> "RecordingApiServer":
        RecordingApiHandler.requests = []
        RecordingApiHandler.release_created = False
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
        test_python_core_rejects_inline_remote_bodies(repo)
        test_python_core_git_and_app_commands(repo / "core-routing")
        test_git_push_and_mr_are_separate_with_state_backfill(repo / "push-mr-state")
        test_ai_call_guidance_is_visible(repo)

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
        attachment_body.write_text(
            issue_file.read_text(encoding="utf-8")
            + "\n## Attachments\n- ![pasted-image.png](xflow-attachment://att-001)\n",
            encoding="utf-8",
            newline="\n",
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
        final_body = repo / ".xflow" / "issues" / "issue-draft" / "issue-with-attachment.final.md"
        run_devctl(repo, "attachment", "render", "--issue", "draft", "--manifest", str(manifest), "--input", str(attachment_body), "--output", str(final_body))
        final_text = final_body.read_text(encoding="utf-8")
        assert "xflow-attachment://" not in final_text
        assert "https://example.test/pasted-image.png" in final_text
        run_devctl(repo, "attachment", "check", "--issue", "draft", "--manifest", str(manifest), "--body-file", str(final_body), "--final")

        local_path_body = final_body.with_name("issue-with-local-path.md")
        local_path_body.write_text(final_text + "\n![bad](C:\\temp\\bad.png)\n", encoding="utf-8", newline="\n")
        run_devctl(repo, "attachment", "check", "--issue", "draft", "--manifest", str(manifest), "--body-file", str(local_path_body), "--final", expect=1)

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
            str(manifest),
            "--force",
        )
        approval_text = approval.read_text(encoding="utf-8")
        manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        assert f"Attachment Manifest SHA256: {manifest_digest}" in approval_text
        approval.write_text(approval_text.replace("Approved: no", "Approved: yes"), encoding="utf-8")
        run_devctl(repo, "check", "local-review", "--issue", "draft", "--file", str(final_body), "--action", "issue-create", "--attachments", str(manifest))
        run_devctl(repo, "issue", "create", "Attachment gate", "--body-file", str(final_body), "--attachments", str(manifest), expect=1)
        comment_body = repo / ".xflow" / "issues" / "issue-1" / "comment-with-image.md"
        write(
            comment_body,
            """<!-- xflow: issue-comment -->

Image evidence is attached locally.
""",
        )
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
            plain_result = run_devctl_with_env(
                repo,
                plain_env,
                "issue",
                "create",
                "Plain unattended issue",
                "--body-file",
                str(auto_issue_body),
                "--no-local-review",
            )
            assert "Issue #42 created" in plain_result.stdout
            plain_issue_requests = [item for item in plain_server.requests if item["method"] == "POST" and item["path"].endswith("/issues")]
            assert plain_issue_requests
            assert "Need unattended plain issue creation." in str(plain_issue_requests[-1]["json"])
            assert not [item for item in plain_server.requests if item["path"].endswith("/releases/77/assets")]

        with RecordingApiServer() as merge_server:
            merge_env = {
                "GITHUB_API_BASE": merge_server.base_url,
                "GITHUB_TOKEN": "github-token",
                "XFLOW_PLATFORM": "github",
                "DEVCTL_SKIP_PROVIDER_LOAD": "0",
            }
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
        auto_file.write_text("generic attachment notes\n", encoding="utf-8", newline="\n")
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
            assert "issue/comment image attachments are disabled" in auto_result.stderr
            auto_manifest = repo / ".xflow" / "issues" / "issue-draft" / "attachments" / "manifest.json"
            auto_manifest_data = json.loads(auto_manifest.read_text(encoding="utf-8"))
            assert any(item["mime"] == "image/png" for item in auto_manifest_data["items"])
            github_issue_requests = [item for item in github_server.requests if item["method"] == "POST" and item["path"].endswith("/issues")]
            assert not github_issue_requests
            upload_requests = [item for item in github_server.requests if item["path"].endswith("/releases/77/assets")]
            assert not upload_requests

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
