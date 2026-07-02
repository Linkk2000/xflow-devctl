from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import approval, attachment, providers, rules
from .checks import (
    check_current_task,
    check_issue_draft,
    check_mr_draft,
    check_subtask,
    check_submodule_hygiene,
    write_pr_state_update_suggestion,
)
from .env import RuntimeContext, load_env_files, python_version, token_status_lines
from .migration import inspect, write_wrappers
from .paths import default_issue_file


ISSUE_CREATE_EPILOG = """AI call recipes:
  Plain unattended issue:
    devctl issue create "<title>" --body-file issue.md --no-local-review

  Reviewed non-image attachment issue:
    devctl attachment add --issue draft --file notes.txt --as file
    devctl attachment publish --issue draft --backend manual --url att-001=https://public.example/notes.txt --body-file issue.md --output issue.final.md
    devctl approval prepare --issue draft --action issue-create --file issue.final.md --attachments .xflow/issues/issue-draft/attachments/manifest.json
    devctl check local-review --issue draft --file issue.final.md --action issue-create --attachments .xflow/issues/issue-draft/attachments/manifest.json
    devctl issue create "<title>" --body-file issue.final.md --attachments .xflow/issues/issue-draft/attachments/manifest.json

Notes:
  Issue/comment image attachments are disabled. Do not use GitHub release assets as an issue image store.
  --attach-file with an image MIME type or Markdown image attachment fails before remote writes.
  Aliyun OSS image attachments must be published first with attachment publish --backend aliyun-oss.
  For non-image files, use a reviewed manifest and an approved URL backend.
  GITHUB_TOKEN is required for issue creation.
  --no-local-review is only valid when the current user explicitly authorized that exact unattended command.
"""


ISSUE_COMMENT_EPILOG = """AI call recipes:
  Plain unattended comment:
    devctl issue comment <number> --body-file comment.md --no-local-review

  Reviewed non-image attachment comment:
    devctl attachment add --issue <number> --file notes.txt --as file
    devctl attachment publish --issue <number> --backend manual --url att-001=https://public.example/notes.txt --body-file comment.md --output comment.final.md
    devctl approval prepare --issue <number> --action issue-comment --file comment.final.md --attachments .xflow/issues/issue-<number>/attachments/manifest.json
    devctl check local-review --issue <number> --file comment.final.md --action issue-comment --attachments .xflow/issues/issue-<number>/attachments/manifest.json
    devctl issue comment <number> --body-file comment.final.md --attachments .xflow/issues/issue-<number>/attachments/manifest.json

Notes:
  Issue/comment image attachments are disabled. Do not use GitHub release assets as an issue image store.
  --attach-file with an image MIME type or Markdown image attachment fails before remote writes.
  Aliyun OSS image attachments must be published first with attachment publish --backend aliyun-oss.
  For non-image files, use a reviewed manifest and an approved URL backend.
  GITHUB_TOKEN is required for issue comments.
  --no-local-review is only valid when the current user explicitly authorized that exact unattended command.
"""


ATTACHMENT_PUBLISH_EPILOG = """Attachment publishing:
  devctl attachment publish --issue draft --backend github --body-file issue.md --output issue.final.md

This creates or reuses the xflow-attachments release, uploads manifest files as
release assets, writes publishedUrl values into the manifest, and optionally
renders the final body file with public GitHub URLs.
Do not use this backend as issue/comment image storage. GitHub publishing and
issue/comment commands reject image attachments before remote writes.

Manual URL mode:
  devctl attachment publish --issue draft --backend manual --url att-001=https://public.example/file.png

Aliyun OSS mode:
  devctl attachment publish --issue draft --backend aliyun-oss

Reads ALIYUN_OSS_BUCKET, ALIYUN_OSS_REGION, ALIYUN_OSS_ACCESS_KEY_ID,
ALIYUN_OSS_ACCESS_KEY_SECRET, optional ALIYUN_OSS_ENDPOINT,
ALIYUN_OSS_PUBLIC_BASE_URL, and ALIYUN_OSS_PREFIX from loaded env files.
Secrets must live in ~/.xflow/env.local or .xflow/local/env.local and are not
written to attachment manifests.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devctl")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight")

    check = sub.add_parser("check")
    check_sub = check.add_subparsers(dest="check_command")
    issue_draft = check_sub.add_parser("issue-draft")
    issue_draft.add_argument("--issue", default="draft")
    issue_draft.add_argument("--file", type=Path)
    mr_draft = check_sub.add_parser("mr-draft")
    mr_draft.add_argument("--issue")
    mr_draft.add_argument("--file", type=Path)
    local_review = check_sub.add_parser("local-review")
    local_review.add_argument("--issue", required=True)
    local_review.add_argument("--file", required=True, type=Path)
    local_review.add_argument("--action")
    local_review.add_argument("--attachments", type=Path)
    check_sub.add_parser("submodule-hygiene")
    current_task = check_sub.add_parser("current-task")
    current_task.add_argument("--issue")
    subtask = check_sub.add_parser("subtask")
    subtask.add_argument("--issue", required=True)
    subtask.add_argument("--path", type=Path)

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command")
    issue_create = issue_sub.add_parser(
        "create",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ISSUE_CREATE_EPILOG,
    )
    issue_create.add_argument("title")
    issue_create.add_argument("--body")
    issue_create.add_argument("--body-file", type=Path)
    issue_create.add_argument("--labels")
    issue_create.add_argument("--attachments", type=Path)
    issue_create.add_argument("--attach-file", action="append", default=[], type=Path)
    issue_create.add_argument("--attach-as", choices=("auto", "image", "file"), default="auto")
    issue_create.add_argument("--upload-attachments", choices=("github", "github-release"), nargs="?", const="github")
    issue_create.add_argument("--release-tag", default=os.environ.get("XFLOW_GITHUB_ATTACHMENT_RELEASE_TAG", "xflow-attachments"))
    issue_create.add_argument("--rendered-body-file", type=Path)
    issue_create.add_argument("--no-local-review", action="store_true")
    issue_list = issue_sub.add_parser("list")
    issue_list.add_argument("--state", choices=("open", "closed", "all"), default="open")
    issue_list.add_argument("--limit", type=int, default=20)
    issue_show = issue_sub.add_parser("show")
    issue_show.add_argument("number")
    issue_comment = issue_sub.add_parser(
        "comment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ISSUE_COMMENT_EPILOG,
    )
    issue_comment.add_argument("number")
    issue_comment.add_argument("--body")
    issue_comment.add_argument("--body-file", type=Path)
    issue_comment.add_argument("--attachments", type=Path)
    issue_comment.add_argument("--attach-file", action="append", default=[], type=Path)
    issue_comment.add_argument("--attach-as", choices=("auto", "image", "file"), default="auto")
    issue_comment.add_argument("--upload-attachments", choices=("github", "github-release"), nargs="?", const="github")
    issue_comment.add_argument("--release-tag", default=os.environ.get("XFLOW_GITHUB_ATTACHMENT_RELEASE_TAG", "xflow-attachments"))
    issue_comment.add_argument("--rendered-body-file", type=Path)
    issue_comment.add_argument("--no-local-review", action="store_true")
    issue_close = issue_sub.add_parser("close")
    issue_close.add_argument("number")

    git = sub.add_parser("git")
    git_sub = git.add_subparsers(dest="git_command")
    git_start = git_sub.add_parser("start")
    git_start.add_argument("slug")
    git_start.add_argument("--issue")
    git_start.add_argument("--base")
    git_commit_msg = git_sub.add_parser("commit-msg")
    git_commit_msg.add_argument("-a", "--all", action="store_true")
    git_commit_msg.add_argument("-c", "--commit", action="store_true")
    git_commit_msg.add_argument("-m", "--message")
    git_commit_msg.add_argument("summary", nargs="?")
    git_push = git_sub.add_parser("push")
    git_push.add_argument("--issue")
    git_push.add_argument("--file", type=Path)
    git_mr = git_sub.add_parser("mr")
    git_mr.add_argument("--title")
    git_mr.add_argument("--body")
    git_mr.add_argument("--body-file", type=Path)
    git_mr.add_argument("--base")
    git_mr.add_argument("--issue")
    git_mr.add_argument("--attachments", type=Path)
    pr_get = git_sub.add_parser("pr-get")
    pr_get.add_argument("number")
    pr_merge = git_sub.add_parser("pr-merge")
    pr_merge.add_argument("number")
    pr_merge.add_argument("--method", choices=("merge", "squash", "rebase"), default="squash")
    pr_merge.add_argument("--commit-title")
    pr_merge.add_argument("--commit-message")
    pr_merge.add_argument("--issue")
    pr_merge.add_argument("--file", type=Path)
    git_sub.add_parser("status")
    git_done = git_sub.add_parser("done")
    git_done.add_argument("--base")
    git_done.add_argument("--force", action="store_true")

    app = sub.add_parser("app")
    app_sub = app.add_subparsers(dest="app_command")
    app_start = app_sub.add_parser("start-frontend")
    app_start.add_argument("--port", type=int, default=int(os.environ.get("XFLOW_FRONTEND_PORT", "5173")))
    app_start.add_argument("--foreground", action="store_true")
    app_stop = app_sub.add_parser("stop-frontend")
    app_stop.add_argument("--port", type=int, default=int(os.environ.get("XFLOW_FRONTEND_PORT", "5173")))
    app_status = app_sub.add_parser("status")
    app_status.add_argument("--port", type=int, default=int(os.environ.get("XFLOW_FRONTEND_PORT", "5173")))

    approval_parser = sub.add_parser("approval")
    approval_sub = approval_parser.add_subparsers(dest="approval_command")
    approval_prepare = approval_sub.add_parser("prepare")
    approval_prepare.add_argument("--issue", required=True)
    approval_prepare.add_argument("--action", required=True)
    approval_prepare.add_argument("--file", required=True, type=Path)
    approval_prepare.add_argument("--command", dest="suggested_command")
    approval_prepare.add_argument("--reviewer")
    approval_prepare.add_argument("--force", action="store_true")
    approval_prepare.add_argument("--attachments", type=Path)

    attachment_parser = sub.add_parser("attachment")
    attachment_sub = attachment_parser.add_subparsers(dest="attachment_command")
    attachment_add = attachment_sub.add_parser("add")
    attachment_add.add_argument("--issue", default="draft")
    attachment_add.add_argument("--file", required=True, type=Path)
    attachment_add.add_argument("--as", dest="attachment_kind", choices=("auto", "image", "file"), default="auto")
    attachment_add.add_argument("--id")
    attachment_add.add_argument("--manifest", type=Path)
    attachment_check = attachment_sub.add_parser("check")
    attachment_check.add_argument("--issue", default="draft")
    attachment_check.add_argument("--manifest", type=Path)
    attachment_check.add_argument("--body-file", type=Path)
    attachment_check.add_argument("--final", action="store_true")
    attachment_publish = attachment_sub.add_parser(
        "publish",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ATTACHMENT_PUBLISH_EPILOG,
    )
    attachment_publish.add_argument("--issue", default="draft")
    attachment_publish.add_argument("--manifest", type=Path)
    attachment_publish.add_argument("--backend", choices=("manual", "github", "github-release", "aliyun-oss", "object"))
    attachment_publish.add_argument("--release-tag", default=os.environ.get("XFLOW_GITHUB_ATTACHMENT_RELEASE_TAG", "xflow-attachments"))
    attachment_publish.add_argument("--url", action="append", default=[])
    attachment_publish.add_argument("--body-file", type=Path)
    attachment_publish.add_argument("--output", type=Path)
    attachment_render = attachment_sub.add_parser("render")
    attachment_render.add_argument("--issue", default="draft")
    attachment_render.add_argument("--manifest", type=Path)
    attachment_render.add_argument("--input", required=True, type=Path)
    attachment_render.add_argument("--output", required=True, type=Path)

    rules_parser = sub.add_parser("rules")
    rules_sub = rules_parser.add_subparsers(dest="rules_command")
    rules_sub.add_parser("list")
    rules_sync = rules_sub.add_parser("sync")
    rules_sync.add_argument("rule_id", nargs="?")
    rules_sync.add_argument("--all", action="store_true")
    rules_sync.add_argument("--force", action="store_true")

    migrate = sub.add_parser("migrate")
    migrate_sub = migrate.add_subparsers(dest="migrate_command")
    migrate_sub.add_parser("inspect")
    migrate_sub.add_parser("wrappers")
    return parser


def context() -> RuntimeContext:
    return RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)


def resolve_check_file(repo_root: Path, issue: str | None, file: Path | None, filename: str) -> Path:
    if file is not None:
        return file
    if not issue:
        raise ValueError("--issue is required")
    return default_issue_file(repo_root, issue, filename)


def run_preflight() -> int:
    ctx = context()
    print(f"python: {sys.executable}")
    print(f"version: {python_version()}")
    print(f"tool_root: {ctx.tool_root}")
    print(f"repo_root: {ctx.repo_root}")
    print(f"product_line: {ctx.product_line or 'unset'}")
    loaded = os.environ.get("XFLOW_LOADED_ENV_FILES", "")
    if loaded:
        for path in loaded.split(os.pathsep):
            print(f"env_file: {path}")
    else:
        print("env_file: <none>")
    for line in token_status_lines(os.environ):
        print(line)
    return 0


def run_check(args: argparse.Namespace) -> int:
    ctx = context()
    if args.check_command == "issue-draft":
        path = resolve_check_file(ctx.repo_root, args.issue, args.file, "issue-draft.md")
        check_issue_draft(path)
    elif args.check_command == "mr-draft":
        path = resolve_check_file(ctx.repo_root, args.issue, args.file, "mr-draft.md")
        check_mr_draft(path)
    elif args.check_command == "local-review":
        path = args.file
        if args.action:
            approval.require_remote(ctx.repo_root, args.action, path, args.issue, args.attachments)
        else:
            approval.check(ctx.repo_root, args.issue, path, args.attachments)
    elif args.check_command == "submodule-hygiene":
        path = ctx.repo_root
        check_submodule_hygiene(path)
    elif args.check_command == "subtask":
        path = check_subtask(ctx.repo_root, args.issue, args.path)
    elif args.check_command == "current-task":
        path = ctx.repo_root / ".xflow" / "current-task.md"
        check_current_task(ctx.repo_root, args.issue)
    else:
        raise ValueError(f"unknown check subcommand: {args.check_command}")
    print(f"[INFO] {args.check_command} check passed: {path}")
    return 0


def body_from_file(path: Path | None, inline: str | None, required_message: str) -> tuple[str, Path]:
    if inline:
        raise ValueError(required_message)
    if path is None:
        raise ValueError(required_message)
    if not path.is_file():
        raise ValueError(f"body file does not exist: {path}")
    return path.read_text(encoding="utf-8"), path


def generated_body_path(path: Path, suffix: str) -> Path:
    extension = path.suffix or ".md"
    return path.with_name(f"{path.stem}{suffix}{extension}")


def release_tag(args: argparse.Namespace) -> str:
    return getattr(args, "release_tag", None) or os.environ.get("XFLOW_GITHUB_ATTACHMENT_RELEASE_TAG", "xflow-attachments")


def prepare_attachment_body(
    repo_root: Path,
    issue: str,
    body_file: Path,
    args: argparse.Namespace,
) -> tuple[str, Path, Path | None]:
    manifest_path = getattr(args, "attachments", None)
    attach_files = list(getattr(args, "attach_file", []) or [])
    upload_backend = getattr(args, "upload_attachments", None)
    if attach_files and manifest_path is None:
        manifest_path = attachment.default_manifest(repo_root, issue)
    if attach_files and upload_backend is None:
        upload_backend = "github"

    original_body = body_file
    current_body = body_file
    if attach_files:
        markdown_items: list[str] = []
        for file_path in attach_files:
            item, manifest_path = attachment.add_attachment(
                repo_root,
                issue,
                file_path,
                getattr(args, "attach_as", "auto"),
                None,
                manifest_path,
            )
            markdown_items.append(str(item["markdown"]))
        current_body = attachment.append_markdown_to_body(
            repo_root,
            current_body,
            markdown_items,
            generated_body_path(current_body, ".attachments"),
        )

    if manifest_path is not None:
        attachment.reject_issue_image_attachments(repo_root, manifest_path, issue)

    if upload_backend:
        if manifest_path is None:
            raise ValueError("--upload-attachments requires --attachments or --attach-file")
        if upload_backend not in {"github", "github-release"}:
            raise ValueError(f"unsupported attachment upload backend: {upload_backend}")
        attachment.publish_github_release(repo_root, issue, manifest_path, os.environ, release_tag(args))
        output = getattr(args, "rendered_body_file", None) or generated_body_path(original_body, ".final")
        current_body = attachment.render_body(repo_root, issue, manifest_path, current_body, output)
    else:
        attachment.ensure_publishable(repo_root, current_body, manifest_path, issue if manifest_path else None)

    return current_body.read_text(encoding="utf-8"), current_body, manifest_path


def run_issue(args: argparse.Namespace) -> int:
    ctx = context()
    if args.issue_command == "list":
        if args.limit < 1 or args.limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        rows = providers.list_issues(ctx.repo_root, args.state, args.limit, os.environ)
        for row in rows:
            print(f"#{row.get('number', '')}\t[{row.get('state', '')}]\t{row.get('title', '')}")
        return 0
    if args.issue_command == "show":
        item = providers.show_issue(ctx.repo_root, args.number, os.environ)
        print(f"#{item.get('number', args.number)} [{item.get('state', '')}] {item.get('title', '')}")
        if item.get("body"):
            print()
            print(item["body"])
        if item.get("html_url"):
            print()
            print(item["html_url"])
        return 0
    if args.issue_command == "comment":
        _body, file_path = body_from_file(args.body_file, args.body, "remote issue comments require --body-file for local review")
        body, file_path, manifest_path = prepare_attachment_body(ctx.repo_root, args.number, file_path, args)
        if not args.no_local_review:
            approval.require_remote(ctx.repo_root, "issue-comment", file_path, args.number, manifest_path)
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] issue-comment gate passed; provider skipped")
            return 0
        result = providers.comment_issue(ctx.repo_root, args.number, body, os.environ)
        print(f"[INFO] Comment posted on Issue #{args.number}")
        if result.get("html_url"):
            print(f"[INFO] {result['html_url']}")
        return 0
    if args.issue_command == "close":
        file_path = Path(os.environ.get("DEVCTL_APPROVED_FILE", default_issue_file(ctx.repo_root, args.number, "walkthrough.md")))
        approval.require_remote(ctx.repo_root, "issue-close", file_path, args.number)
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] issue-close gate passed; provider skipped")
            return 0
        result = providers.close_issue(ctx.repo_root, args.number, os.environ)
        print(f"[INFO] Issue #{result.get('number', args.number)} closed")
        return 0
    if args.issue_command != "create":
        raise ValueError(f"unknown issue subcommand: {args.issue_command}")
    _body, file_path = body_from_file(args.body_file, args.body, "remote issue creation requires --body-file for local review")
    body, file_path, manifest_path = prepare_attachment_body(ctx.repo_root, "draft", file_path, args)
    if not args.no_local_review:
        approval.require_remote(ctx.repo_root, "issue-create", file_path, "draft", manifest_path)
    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] issue-create gate passed; provider skipped")
        return 0
    result = providers.create_issue(ctx.repo_root, args.title, body, args.labels, os.environ)
    print(f"[INFO] Issue #{result.number} created")
    if result.html_url:
        print(f"[INFO] {result.html_url}")
    print(result.number)
    return 0


def git_output(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_run(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def current_branch(repo_root: Path) -> str:
    branch = git_output(repo_root, ["branch", "--show-current"])
    if not branch:
        raise ValueError("cannot determine current git branch")
    return branch


def set_branch_meta(repo_root: Path, key: str, value: str) -> None:
    git_run(repo_root, ["config", "--local", f"devctl.{key}", value])


def branch_meta(repo_root: Path, key: str) -> str:
    return git_output(repo_root, ["config", "--local", "--get", f"devctl.{key}"])


def unset_branch_meta(repo_root: Path, key: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), "config", "--local", "--unset-all", f"devctl.{key}"], check=False)


def default_base(repo_root: Path) -> str:
    env_base = os.environ.get("DEVCTL_BASE_BRANCH")
    if env_base:
        return env_base
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/master"]).returncode == 0:
        return "master"
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/main"]).returncode == 0:
        return "main"
    origin_head = git_output(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if origin_head.startswith("origin/"):
        return origin_head[len("origin/") :]
    return "main"


def require_clean_worktree(repo_root: Path) -> None:
    if subprocess.run(["git", "-C", str(repo_root), "diff", "--quiet"], check=False).returncode != 0:
        raise ValueError("worktree has unstaged changes; commit or stash them first")
    if subprocess.run(["git", "-C", str(repo_root), "diff", "--cached", "--quiet"], check=False).returncode != 0:
        raise ValueError("index has staged changes; commit or reset them first")
    if git_output(repo_root, ["ls-files", "--others", "--exclude-standard"]):
        raise ValueError("worktree has untracked files; add, ignore, or remove them first")


def branch_slugify(value: str) -> str:
    slug = []
    last_dash = False
    for char in value.lower():
        if char.isascii() and char.isalnum():
            slug.append(char)
            last_dash = False
        elif not last_dash:
            slug.append("-")
            last_dash = True
    result = "".join(slug).strip("-")
    while "--" in result:
        result = result.replace("--", "-")
    if not result:
        raise ValueError("branch slug is empty after normalization")
    return result


def branch_name_from_slug(slug: str, issue: str | None) -> str:
    prefix = os.environ.get("DEVCTL_BRANCH_PREFIX", "feat")
    normalized = branch_slugify(slug)
    return f"{prefix}/{issue}-{normalized}" if issue else f"{prefix}/{normalized}"


def changed_paths(repo_root: Path) -> list[str]:
    paths = set()
    for args in (["diff", "--cached", "--name-only"], ["diff", "--name-only"]):
        output = git_output(repo_root, args)
        for line in output.splitlines():
            if line.strip():
                paths.add(line.strip())
    return sorted(paths)


def guess_commit_type(paths: list[str]) -> str:
    for path in paths:
        if path.endswith(".md") or path.startswith("docs/") or path in {"AGENTS.md"} or Path(path).name.startswith("README"):
            return "docs"
        if "_test." in path or "/test/" in path or path.startswith("tests/"):
            return "test"
        if path.startswith("_ops/") or path == "devctl" or path.startswith("scripts/"):
            return "chore"
        if path.endswith((".vue", ".tsx", ".jsx", ".java")):
            return "feat"
    return "chore"


def guess_commit_scope(paths: list[str]) -> str:
    joined = "\n".join(paths[:5])
    if "warmflow-designer" in joined:
        return "warmflow-designer"
    if "xflow-server" in joined or "xflow-app" in joined:
        return "server"
    if "apps/xflow" in joined:
        return "xflow"
    if "_ops/" in joined:
        return "devctl"
    if paths:
        return "/".join(Path(paths[0]).parts[:2])
    return "dev"


def summarize_commit_message(repo_root: Path, override: str | None) -> str:
    if override:
        return override
    paths = changed_paths(repo_root)
    if not paths:
        raise ValueError("no changes to summarize")
    names = [Path(path).name for path in paths[:3]]
    summary = ", ".join(names)
    if len(paths) > 3:
        summary = f"{summary} 等 {len(paths)} 个文件"
    return f"{guess_commit_type(paths)}({guess_commit_scope(paths)}): 更新 {summary}"


def push_branch(repo_root: Path, branch: str) -> None:
    if os.environ.get("DEVCTL_SKIP_PUSH") == "1":
        return
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    command = ["push", "origin", branch] if upstream else ["push", "-u", "origin", branch]
    result = subprocess.run(
        ["git", "-C", str(repo_root), *command],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"git push failed: {result.stderr.strip() or result.stdout.strip()}")


def require_branch_ready_for_mr(repo_root: Path, branch: str) -> None:
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    if not upstream:
        raise ValueError("task branch has no upstream; run devctl git push before devctl git mr")
    ahead = int(git_output(repo_root, ["rev-list", "--count", f"{upstream}..HEAD"]) or "0")
    if ahead:
        raise ValueError(f"task branch has {ahead} unpushed commit(s); run devctl git push before devctl git mr")


def update_current_task_for_pr(repo_root: Path, issue: str, pr_number: str, pr_url: str | None) -> list[Path]:
    path = repo_root / ".xflow" / "current-task.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = []
    state_updated = False
    for line in text.splitlines():
        if line.startswith("State:"):
            lines.append("State: S9_REMOTE_REVIEW_AND_CI")
            state_updated = True
        else:
            lines.append(line)
    if not state_updated:
        lines.insert(0, "State: S9_REMOTE_REVIEW_AND_CI")
    updated = "\n".join(lines).rstrip() + "\n"
    if "## Remote Review" not in updated:
        url_line = f"PR URL: {pr_url}\n" if pr_url else ""
        updated += f"\n## Remote Review\nPR: {pr_number}\n{url_line}"
    if updated != text:
        path.write_text(updated, encoding="utf-8", newline="\n")
        return [path]
    return []


def commit_and_push_pr_backfill(repo_root: Path, branch: str, paths: list[Path], pr_number: str) -> bool:
    if not paths:
        return False
    for path in paths:
        if path.exists():
            git_run(repo_root, ["add", "--", str(path.relative_to(repo_root))])
    if subprocess.run(["git", "-C", str(repo_root), "diff", "--cached", "--quiet"], check=False).returncode == 0:
        return False
    git_run(repo_root, ["commit", "-m", f"chore(xflow): 回填 PR #{pr_number} 状态"])
    push_branch(repo_root, branch)
    return True


def run_git_push(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    issue = args.issue or branch_meta(ctx.repo_root, "issue")
    if not issue:
        raise ValueError("devctl git push requires --issue or branch issue metadata")
    approved_file = args.file or default_issue_file(ctx.repo_root, issue, "walkthrough.md")
    if not approved_file.is_file():
        raise ValueError(f"approved file does not exist: {approved_file}")
    approval.require_remote(ctx.repo_root, "git-push", approved_file, issue)
    check_current_task(ctx.repo_root, issue)
    branch = current_branch(ctx.repo_root)
    base = branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is {base}; start a task branch before pushing")
    push_branch(ctx.repo_root, branch)
    print(f"[INFO] pushed {branch}")
    return 0


def run_git_start(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    base = args.base or default_base(ctx.repo_root)
    branch = branch_name_from_slug(args.slug, args.issue)
    require_clean_worktree(ctx.repo_root)
    current = current_branch(ctx.repo_root)
    if current != base:
        print(f"[INFO] checkout {base}")
        git_run(ctx.repo_root, ["checkout", base])
    print(f"[INFO] pull origin/{base}")
    git_run(ctx.repo_root, ["pull", "--ff-only", "origin", base])
    if subprocess.run(["git", "-C", str(ctx.repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0:
        raise ValueError(f"branch already exists: {branch}")
    print(f"[INFO] create branch {branch}")
    git_run(ctx.repo_root, ["checkout", "-b", branch])
    set_branch_meta(ctx.repo_root, "slug", args.slug)
    if args.issue:
        set_branch_meta(ctx.repo_root, "issue", args.issue)
    set_branch_meta(ctx.repo_root, "base", base)
    print("[INFO] ready")
    return 0


def run_git_status(ctx: RuntimeContext) -> int:
    branch = current_branch(ctx.repo_root)
    base = branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    issue = branch_meta(ctx.repo_root, "issue")
    slug = branch_meta(ctx.repo_root, "slug")
    pr = branch_meta(ctx.repo_root, "pr")
    print(f"branch:  {branch}")
    print(f"base:    {base}")
    if slug:
        print(f"slug:    {slug}")
    if issue:
        print(f"issue:   #{issue}")
    if pr:
        print(f"pr:      #{pr}")
    upstream = git_output(ctx.repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"]) or "(none)"
    print(f"upstream: {upstream}")
    if upstream != "(none)":
        ahead = git_output(ctx.repo_root, ["rev-list", "--count", f"{upstream}..HEAD"]) or "0"
        behind = git_output(ctx.repo_root, ["rev-list", "--count", f"HEAD..{upstream}"]) or "0"
        print(f"ahead:   {ahead}  behind: {behind}")
    dirty = (
        subprocess.run(["git", "-C", str(ctx.repo_root), "diff", "--quiet"], check=False).returncode != 0
        or subprocess.run(["git", "-C", str(ctx.repo_root), "diff", "--cached", "--quiet"], check=False).returncode != 0
        or bool(git_output(ctx.repo_root, ["ls-files", "--others", "--exclude-standard"]))
    )
    print(f"worktree: {'dirty' if dirty else 'clean'}")
    return 0


def run_git_commit_msg(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    if args.all:
        git_run(ctx.repo_root, ["add", "-A"])
    message = summarize_commit_message(ctx.repo_root, args.message or args.summary)
    print("[INFO] suggested commit message:")
    print()
    print(f"  {message}")
    print()
    if args.commit:
        git_run(ctx.repo_root, ["commit", "-m", message])
        print("[INFO] committed")
    else:
        print("[INFO] confirm commit: devctl git commit-msg -c")
    return 0


def run_git_done(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    branch = current_branch(ctx.repo_root)
    base = args.base or branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is already {base}; nothing to clean up")
    pr_number = branch_meta(ctx.repo_root, "pr")
    if not args.force and pr_number and os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") != "1":
        item = providers.get_pull_request(ctx.repo_root, pr_number, os.environ)
        if item and not item.get("merged") and item.get("state") != "closed":
            raise ValueError(f"PR #{pr_number} is not merged or closed; use --force only with explicit approval")
    elif not args.force and not pr_number:
        print("[WARN] no PR number recorded; skipping merge-state check")
    require_clean_worktree(ctx.repo_root)
    print(f"[INFO] checkout {base}")
    git_run(ctx.repo_root, ["checkout", base])
    print(f"[INFO] pull origin/{base}")
    git_run(ctx.repo_root, ["pull", "--ff-only", "origin", base])
    if subprocess.run(["git", "-C", str(ctx.repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0:
        result = subprocess.run(["git", "-C", str(ctx.repo_root), "branch", "-d", branch], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            git_run(ctx.repo_root, ["branch", "-D", branch])
        print(f"[INFO] deleted local branch {branch}")
    for key in ("slug", "issue", "base", "pr", "pr-url"):
        unset_branch_meta(ctx.repo_root, key)
    print("[INFO] done")
    return 0


def run_git(args: argparse.Namespace) -> int:
    ctx = context()
    if args.git_command == "start":
        return run_git_start(ctx, args)
    if args.git_command == "status":
        return run_git_status(ctx)
    if args.git_command == "commit-msg":
        return run_git_commit_msg(ctx, args)
    if args.git_command == "push":
        return run_git_push(ctx, args)
    if args.git_command == "done":
        return run_git_done(ctx, args)
    if args.git_command == "pr-get":
        pr = providers.get_pull_request(ctx.repo_root, args.number, os.environ)
        print(f"#{pr.get('number', args.number)} [{pr.get('state', '')}] {pr.get('title', '')}")
        if pr.get("html_url"):
            print()
            print(pr["html_url"])
        return 0
    if args.git_command == "pr-merge":
        issue = args.issue or branch_meta(ctx.repo_root, "issue")
        if not issue:
            raise ValueError("devctl git pr-merge requires --issue or branch issue metadata")
        approved_file = args.file or default_issue_file(ctx.repo_root, issue, "mr-draft.md")
        if not approved_file.is_file():
            raise ValueError(f"approved file does not exist: {approved_file}")
        approval.require_remote(ctx.repo_root, "git-pr-merge", approved_file, issue)
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] git-pr-merge gate passed; provider skipped")
            return 0
        result = providers.merge_pull_request(
            ctx.repo_root,
            args.number,
            args.method,
            args.commit_title,
            args.commit_message,
            os.environ,
        )
        print(f"[INFO] PR #{args.number} merged")
        if result.get("sha"):
            print(f"[INFO] merge sha: {result['sha']}")
        if result.get("message"):
            print(f"[INFO] {result['message']}")
        return 0
    if args.git_command != "mr":
        raise ValueError(f"unknown git subcommand: {args.git_command}")
    if args.body:
        raise ValueError("remote MR/PR creation requires --body-file for local review")
    issue = args.issue or branch_meta(ctx.repo_root, "issue")
    if not issue:
        raise ValueError("devctl git mr requires --issue or branch issue metadata")
    body_file = args.body_file or default_issue_file(ctx.repo_root, issue, "mr-draft.md")
    if not body_file.is_file():
        raise ValueError(f"body file does not exist: {body_file}")
    attachment.ensure_publishable(ctx.repo_root, body_file, args.attachments, issue if args.attachments else None)
    approval.require_remote(ctx.repo_root, "git-mr", body_file, issue, args.attachments)
    branch = current_branch(ctx.repo_root)
    base = args.base or branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is {base}; start a task branch before creating an MR")
    require_branch_ready_for_mr(ctx.repo_root, branch)
    title = args.title or f"[#{issue}] {branch.replace('-', ' ')}"
    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] git-mr gate passed; provider skipped")
        return 0
    result = providers.create_pull_request(ctx.repo_root, title, body_file.read_text(encoding="utf-8"), branch, base, os.environ)
    subprocess.run(["git", "-C", str(ctx.repo_root), "config", "--local", "devctl.pr", result.number], check=False)
    if result.html_url:
        subprocess.run(["git", "-C", str(ctx.repo_root), "config", "--local", "devctl.pr-url", result.html_url], check=False)
    suggestion = write_pr_state_update_suggestion(ctx.repo_root, issue, result.number, result.html_url)
    backfill_paths = [suggestion, *update_current_task_for_pr(ctx.repo_root, issue, result.number, result.html_url)]
    backfill_pushed = commit_and_push_pr_backfill(ctx.repo_root, branch, backfill_paths, result.number)
    print(f"[INFO] PR #{result.number} created")
    if result.html_url:
        print(f"[INFO] {result.html_url}")
    print(f"[INFO] state update suggestion: {suggestion}")
    if backfill_pushed:
        print("[INFO] state backfill pushed")
    print(result.number)
    return 0


def run_approval(args: argparse.Namespace) -> int:
    ctx = context()
    if args.approval_command != "prepare":
        raise ValueError(f"unknown approval subcommand: {args.approval_command}")
    path = approval.prepare(
        ctx.repo_root,
        args.issue,
        args.action,
        args.file,
        args.suggested_command,
        args.reviewer,
        args.force,
        args.attachments,
    )
    print(f"[INFO] local review prepared: {path}")
    return 0


def attachment_manifest_path(repo_root: Path, issue: str, manifest: Path | None) -> Path:
    return manifest or attachment.default_manifest(repo_root, issue)


def run_attachment(args: argparse.Namespace) -> int:
    ctx = context()
    issue = args.issue
    manifest = attachment_manifest_path(ctx.repo_root, issue, getattr(args, "manifest", None))
    if args.attachment_command == "add":
        item, path = attachment.add_attachment(ctx.repo_root, issue, args.file, args.attachment_kind, args.id, manifest)
        print(f"[INFO] attachment added: {item['id']}")
        print(item["markdown"])
        print(f"[INFO] manifest: {path}")
        return 0
    if args.attachment_command == "check":
        attachment.check_attachment(ctx.repo_root, issue, manifest, args.body_file, args.final)
        print(f"[INFO] attachment check passed: {manifest}")
        return 0
    if args.attachment_command == "publish":
        backend = args.backend or ("manual" if args.url else "github")
        if backend == "object":
            backend = os.environ.get("XFLOW_ATTACHMENT_BACKEND", "").strip()
            if not backend:
                raise ValueError("attachment publish --backend object requires XFLOW_ATTACHMENT_BACKEND")
        if backend == "manual":
            path = attachment.publish_urls(ctx.repo_root, issue, manifest, args.url)
            print(f"[INFO] attachment URLs recorded: {path}")
        elif backend in {"github", "github-release"}:
            attachment.reject_issue_image_attachments(ctx.repo_root, manifest, issue)
            path = attachment.publish_github_release(ctx.repo_root, issue, manifest, os.environ, args.release_tag)
            print(f"[INFO] GitHub attachment assets uploaded: {path}")
        elif backend == "aliyun-oss":
            path = attachment.publish_aliyun_oss(ctx.repo_root, issue, manifest, os.environ)
            print(f"[INFO] Aliyun OSS attachments uploaded: {path}")
        else:
            raise ValueError(f"unsupported attachment backend: {backend}")
        if args.body_file:
            output = args.output or generated_body_path(args.body_file, ".final")
            rendered = attachment.render_body(ctx.repo_root, issue, manifest, args.body_file, output)
            print(f"[INFO] rendered attachment body: {rendered}")
        return 0
    if args.attachment_command == "render":
        output = attachment.render_body(ctx.repo_root, issue, manifest, args.input, args.output)
        print(f"[INFO] rendered attachment body: {output}")
        return 0
    raise ValueError(f"unknown attachment subcommand: {args.attachment_command}")


def run_rules(args: argparse.Namespace) -> int:
    ctx = context()
    if args.rules_command == "list":
        for entry in rules.load_entries(ctx.repo_root):
            print(f"{entry.rule_id}\t{entry.target.as_posix()}\t{entry.description}")
        return 0
    if args.rules_command == "sync":
        if args.all and args.rule_id:
            raise ValueError("use either a rule id or --all, not both")
        if not args.all and not args.rule_id:
            raise ValueError("rules sync requires a rule id or --all")
        entries = rules.load_entries(ctx.repo_root) if args.all else [rules.find_entry(ctx.repo_root, args.rule_id)]
        for entry in entries:
            target = rules.sync(ctx.repo_root, entry, force=args.force)
            print(f"[INFO] synced {entry.rule_id} -> {target.relative_to(ctx.repo_root).as_posix()}")
        return 0
    raise ValueError(f"unknown rules subcommand: {args.rules_command}")


def run_migrate(args: argparse.Namespace) -> int:
    ctx = context()
    if args.migrate_command == "inspect":
        report = inspect(ctx.repo_root)
        print(f"legacy_ops_present: {'yes' if report.legacy_ops_present else 'no'}")
        print(f"v2_ops_present: {'yes' if report.v2_ops_present else 'no'}")
        for message in report.messages:
            print(f"- {message}")
        return 0
    if args.migrate_command == "wrappers":
        written = write_wrappers(ctx.repo_root)
        print("[INFO] wrote devctl wrappers: " + ", ".join(path.name for path in written))
        return 0
    raise ValueError(f"unknown migrate subcommand: {args.migrate_command}")


VITE_CONFIG_TEMPLATE = """import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const projectRoot = process.env.DEVCTL_REPO_ROOT || process.cwd()
const require = createRequire(resolve(projectRoot, 'package.json'))
const viteConfigRequire = createRequire(resolve(projectRoot, 'internal/vite-config/package.json'))

const { defineConfig, loadEnv } = await import(require.resolve('vite'))
const vue = (await import(require.resolve('@vitejs/plugin-vue'))).default
const vueJsx = (await import(require.resolve('@vitejs/plugin-vue-jsx'))).default
const tailwindcss = (await import(viteConfigRequire.resolve('@tailwindcss/vite'))).default

const TAILWIND_REFERENCE_LINE = '@reference "@vben/tailwind-config/theme";\\n'

function tailwindReferencePlugin() {
  return {
    enforce: 'pre',
    name: 'devctl:tailwind-reference',
    transform(code, id) {
      if (!id.includes('.vue') || !id.includes('type=style')) {
        return null
      }
      if (code.includes('@reference') || !code.includes('@apply')) {
        return null
      }
      return {
        code: TAILWIND_REFERENCE_LINE + code,
        map: null,
      }
    },
  }
}

export default defineConfig(({ mode }) => {
  const appRoot = resolve(projectRoot, process.env.XFLOW_FRONTEND_APP_ROOT || 'apps/xflow')
  const env = loadEnv(mode, appRoot)
  const port = Number(process.env.XFLOW_FRONTEND_PORT || env.VITE_PORT) || 5173

  return {
    root: appRoot,
    plugins: [
      vue({
        script: {
          defineModel: true,
        },
      }),
      vueJsx(),
      tailwindReferencePlugin(),
      tailwindcss(),
    ],
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(process.env.XFLOW_APP_VERSION || '0.1.0'),
    },
    resolve: {
      alias: {
        '#': resolve(appRoot, 'src'),
        '@warm-flow/designer-vueflow': resolve(projectRoot, 'packages/warmflow-designer/src/index.ts'),
      },
      dedupe: ['vue', '@vue-flow/core'],
    },
    optimizeDeps: {
      include: [
        '@vue-flow/core',
        '@vue-flow/background',
        '@vue-flow/controls',
        '@vue-flow/minimap',
      ],
    },
    server: {
      host: '0.0.0.0',
      port,
      proxy: {
        '/api': {
          changeOrigin: true,
          target: process.env.XFLOW_BACKEND_URL || 'http://localhost:8080',
          ws: true,
        },
      },
    },
  }
})
"""


def ensure_project_local_dir(repo_root: Path) -> Path:
    local_dir = repo_root / ".xflow-local"
    local_dir.mkdir(parents=True, exist_ok=True)
    git_dir_raw = git_output(repo_root, ["rev-parse", "--git-dir"])
    if git_dir_raw:
        git_dir = Path(git_dir_raw)
        if not git_dir.is_absolute():
            git_dir = repo_root / git_dir
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".xflow-local/" not in existing.splitlines():
            with exclude.open("a", encoding="utf-8", newline="\n") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(".xflow-local/\n")
    return local_dir


def frontend_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    run_dir = ensure_project_local_dir(repo_root) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "frontend.pid", run_dir / "frontend.log", ensure_project_local_dir(repo_root) / "vite.xflow.config.mjs"


def read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def tail(path: Path, lines: int = 120) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def vite_binary(repo_root: Path) -> Path:
    candidates = [
        repo_root / "node_modules" / ".bin" / "vite.cmd",
        repo_root / "node_modules" / ".bin" / "vite.ps1",
        repo_root / "node_modules" / ".bin" / "vite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ValueError(f"missing Vite binary: run pnpm install in {repo_root}")


def run_app_start(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    if not (ctx.repo_root / "package.json").is_file():
        raise ValueError(f"not a frontend repo: missing package.json in {ctx.repo_root}")
    vite = vite_binary(ctx.repo_root)
    pid_file, log_file, config_file = frontend_paths(ctx.repo_root)
    url = f"http://localhost:{args.port}"
    config_file.write_text(VITE_CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    old_pid = read_pid(pid_file)
    if process_running(old_pid):
        print(f"[INFO] frontend already running: pid {old_pid}, {url}")
        return 0
    if pid_file.exists():
        pid_file.unlink()
    if http_ok(url):
        raise ValueError(f"port {args.port} already responds with HTTP; stop the old service first")
    command = [
        str(vite),
        "--config",
        str(config_file),
        "--mode",
        "development",
        "--host",
        "0.0.0.0",
        "--port",
        str(args.port),
    ]
    env = os.environ.copy()
    env["DEVCTL_REPO_ROOT"] = str(ctx.repo_root)
    env["XFLOW_FRONTEND_PORT"] = str(args.port)
    print(f"[INFO] starting frontend: {url}")
    print(f"[INFO] log: {log_file}")
    if args.foreground:
        return subprocess.call(command, cwd=ctx.repo_root, env=env)
    with log_file.open("w", encoding="utf-8", newline="\n") as log:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            command,
            cwd=ctx.repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    pid_file.write_text(str(process.pid), encoding="utf-8", newline="\n")
    ready = False
    for _ in range(90):
        if http_ok(url):
            ready = True
            break
        if process.poll() is not None:
            if pid_file.exists():
                pid_file.unlink()
            raise ValueError(f"frontend process exited; recent log:\n{tail(log_file)}")
        time.sleep(1)
    if not ready:
        try:
            process.terminate()
        finally:
            if pid_file.exists():
                pid_file.unlink()
        raise ValueError(f"frontend startup timed out; recent log:\n{tail(log_file)}")
    print(f"[INFO] frontend started: {url} (pid {process.pid})")
    return 0


def run_app_status(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    pid_file, log_file, _config_file = frontend_paths(ctx.repo_root)
    url = f"http://localhost:{args.port}"
    pid = read_pid(pid_file)
    if process_running(pid):
        print(f"[INFO] frontend process: running pid {pid}")
    else:
        print("[WARN] frontend process: not running")
    if http_ok(url):
        print(f"[INFO] frontend HTTP: ok {url}")
    else:
        print(f"[WARN] frontend HTTP: unavailable {url}")
    if log_file.is_file():
        print(f"[INFO] log: {log_file}")
    return 0


def run_app_stop(ctx: RuntimeContext, _args: argparse.Namespace) -> int:
    pid_file, _log_file, _config_file = frontend_paths(ctx.repo_root)
    pid = read_pid(pid_file)
    if not pid:
        print("[INFO] no recorded frontend process")
        return 0
    if process_running(pid):
        print(f"[INFO] stopping frontend: pid {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(10):
            if not process_running(pid):
                break
            time.sleep(1)
        if process_running(pid):
            try:
                os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            except OSError:
                pass
    else:
        print(f"[WARN] recorded frontend process is gone: pid {pid}")
    if pid_file.exists():
        pid_file.unlink()
    return 0


def run_app(args: argparse.Namespace) -> int:
    ctx = context()
    if args.app_command == "start-frontend":
        return run_app_start(ctx, args)
    if args.app_command == "status":
        return run_app_status(ctx, args)
    if args.app_command == "stop-frontend":
        return run_app_stop(ctx, args)
    raise ValueError(f"unknown app subcommand: {args.app_command}")


def main(argv: list[str] | None = None) -> int:
    load_env_files(os.environ)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return run_preflight()
        if args.command == "check":
            return run_check(args)
        if args.command == "issue":
            return run_issue(args)
        if args.command == "git":
            return run_git(args)
        if args.command == "approval":
            return run_approval(args)
        if args.command == "attachment":
            return run_attachment(args)
        if args.command == "rules":
            return run_rules(args)
        if args.command == "migrate":
            return run_migrate(args)
        if args.command == "app":
            return run_app(args)
        parser.print_help()
        return 0
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
