from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import approval, attachment, providers, rules, unattended
from .checks import (
    check_current_task,
    check_gap_analysis,
    check_issue_evidence,
    check_issue_draft,
    check_mr_draft,
    check_resolution_report,
    check_subtask,
    check_submodule_hygiene,
    markdown_field,
    write_pr_state_update_suggestion,
)
from .commit_message import check_commit_message
from .bindings import resolve_bindings
from .classification import check_classification
from .env import RuntimeContext, load_env_files, python_version, token_status_lines
from .dependencies import check_dependencies
from .migration import apply_issue_workspace_migration, inspect, inspect_issue_workspace_migration, write_wrappers
from .paths import default_issue_file, normalized_issue
from .task_state import activate_task, list_task_states, load_active_task, migrate_legacy_current_task


ISSUE_CREATE_EPILOG = """AI call recipes:
  Reviewed non-image attachment issue:
    devctl attachment add --issue draft --file notes.txt --as file
    devctl attachment publish --issue draft --backend manual --url att-001=https://public.example/notes.txt --body-file issue.md --output .xflow/publish/issues/issue-draft/issue.final.md
    devctl approval prepare --issue draft --action issue-create --file .xflow/publish/issues/issue-draft/issue.final.md --attachments .xflow/publish/issues/issue-draft/attachments/manifest.json
    devctl check local-review --issue draft --file .xflow/publish/issues/issue-draft/issue.final.md --action issue-create --attachments .xflow/publish/issues/issue-draft/attachments/manifest.json
    devctl issue create "<title>" --body-file .xflow/publish/issues/issue-draft/issue.final.md --attachments .xflow/publish/issues/issue-draft/attachments/manifest.json

Notes:
  Issue/comment image attachments are disabled. Do not use GitHub release assets as an issue image store.
  Inline --attach-file and --upload-attachments are disabled for issue create/comment.
  Use attachment add/publish/render before the final issue command.
  Aliyun OSS image attachments must be published first with attachment publish --backend aliyun-oss.
  For non-image files, use a reviewed manifest and an approved URL backend.
  GITHUB_TOKEN or GITEE_TOKEN is required for issue creation, depending on platform.
"""


ISSUE_COMMENT_EPILOG = """AI call recipes:
  Reviewed non-image attachment comment:
    devctl attachment add --issue <id> --file notes.txt --as file
    devctl attachment publish --issue <id> --backend manual --url att-001=https://public.example/notes.txt --body-file comment.md --output .xflow/publish/issues/issue-<id>/comment.final.md
    devctl approval prepare --issue <id> --action issue-comment --file .xflow/publish/issues/issue-<id>/comment.final.md --attachments .xflow/publish/issues/issue-<id>/attachments/manifest.json
    devctl check local-review --issue <id> --file .xflow/publish/issues/issue-<id>/comment.final.md --action issue-comment --attachments .xflow/publish/issues/issue-<id>/attachments/manifest.json
    devctl issue comment <id> --body-file .xflow/publish/issues/issue-<id>/comment.final.md --attachments .xflow/publish/issues/issue-<id>/attachments/manifest.json

Notes:
  Issue/comment image attachments are disabled. Do not use GitHub release assets as an issue image store.
  Inline --attach-file and --upload-attachments are disabled for issue create/comment.
  Use attachment add/publish/render before the final issue command.
  Aliyun OSS image attachments must be published first with attachment publish --backend aliyun-oss.
  For non-image files, use a reviewed manifest and an approved URL backend.
  GITHUB_TOKEN or GITEE_TOKEN is required for issue comments, depending on platform.
"""


ATTACHMENT_PUBLISH_EPILOG = """Attachment publishing:
  devctl attachment publish --issue draft --backend github --body-file issue.md --output .xflow/publish/issues/issue-draft/issue.final.md

This creates or reuses the xflow-attachments release, uploads manifest files as
release assets, writes publishedUrl values into a publish manifest under
.xflow/publish/issues, and optionally renders the final body file with public
GitHub URLs.
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
    parser = argparse.ArgumentParser(
        prog="devctl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Focused checks:
  devctl check dependencies --issue IK152D
  devctl check commit-msg --file .xflow/local/commit-message.txt --issue IK152D
""",
    )
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
    issue_evidence = check_sub.add_parser("issue-evidence")
    issue_evidence.add_argument("--issue", required=True)
    issue_evidence.add_argument("--publish-root", type=Path)
    subtask = check_sub.add_parser("subtask")
    subtask.add_argument("--issue", required=True)
    subtask.add_argument("--path", type=Path)
    gap_analysis = check_sub.add_parser("gap-analysis")
    gap_analysis.add_argument("--issue", required=True)
    gap_analysis.add_argument("--file", type=Path)
    resolution_report = check_sub.add_parser("resolution-report")
    resolution_report.add_argument("--issue", required=True)
    resolution_report.add_argument("--file", type=Path)
    dependencies = check_sub.add_parser("dependencies")
    dependencies.add_argument("--issue", required=True)
    dependencies.add_argument("--file", type=Path)
    classification = check_sub.add_parser("classification")
    classification.add_argument("--issue", required=True)
    classification.add_argument("--file", type=Path)
    commit_message = check_sub.add_parser("commit-msg")
    commit_message.add_argument("--file", required=True, type=Path)
    commit_message.add_argument("--issue")

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command")
    task_activate = task_sub.add_parser("activate")
    task_activate.add_argument("--issue", required=True)
    task_sub.add_parser("status")
    task_sub.add_parser("list")
    task_sub.add_parser("migrate-current")

    unattended_parser = sub.add_parser("unattended")
    unattended_sub = unattended_parser.add_subparsers(dest="unattended_command")
    unattended_enable = unattended_sub.add_parser("enable")
    unattended_enable.add_argument("--issue", required=True)
    unattended_enable.add_argument("--confirm", required=True)
    unattended_sub.add_parser("status")
    unattended_sub.add_parser("disable")

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
    git_done.add_argument("--issue")
    git_done.add_argument("--file", type=Path)

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
    issue_workspace = migrate_sub.add_parser("issue-workspace")
    issue_workspace.add_argument("--mode", choices=("tracked", "local"), required=True)
    issue_workspace_action = issue_workspace.add_mutually_exclusive_group(required=True)
    issue_workspace_action.add_argument("--check", action="store_true")
    issue_workspace_action.add_argument("--apply", action="store_true")
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
    elif args.check_command == "gap-analysis":
        path = check_gap_analysis(ctx.repo_root, args.issue, args.file)
    elif args.check_command == "resolution-report":
        path = check_resolution_report(ctx.repo_root, args.issue, args.file)
    elif args.check_command == "dependencies":
        result = check_dependencies(ctx.repo_root, args.issue, args.file)
        path = result.path
        for warning in result.warnings:
            print(f"[WARN] {warning}")
    elif args.check_command == "classification":
        result = check_classification(ctx.repo_root, args.issue, args.file)
        path = result.path
    elif args.check_command == "commit-msg":
        path = resolve_check_file(ctx.repo_root, args.issue, args.file, "commit-message.txt")
        if not path.is_file():
            raise ValueError(f"commit message file does not exist: {path}")
        issue_ids = check_commit_message(path.read_text(encoding="utf-8-sig"), branch_issue=args.issue)
        print("[INFO] associated Issues: " + " ".join(f"#{issue_id}" for issue_id in issue_ids))
    elif args.check_command == "issue-evidence":
        path = check_issue_evidence(ctx.repo_root, args.issue, args.publish_root)
    elif args.check_command == "current-task":
        path = ctx.repo_root / ".xflow" / "current-task.md"
        check_current_task(ctx.repo_root, args.issue)
    else:
        raise ValueError(f"unknown check subcommand: {args.check_command}")
    print(f"[INFO] {args.check_command} check passed: {path}")
    return 0


def run_task(args: argparse.Namespace) -> int:
    ctx = context()
    if args.task_command == "activate":
        state = activate_task(ctx.repo_root, args.issue)
        print(f"[INFO] active task: #{state.issue}")
        return 0
    if args.task_command == "status":
        bindings = resolve_bindings(ctx.repo_root)
        state = load_active_task(ctx.repo_root)
        print(f"repository: {bindings.repository[:12]}")
        print(f"worktree: {bindings.worktree[:12]}")
        print(f"branch: {bindings.branch}")
        print(f"Issue: {state.issue}")
        print(f"Execution State: {state.execution_state}")
        print(f"Semantic Phase: {state.semantic_phase}")
        print(f"Classification: {state.classification}")
        print(f"Contract: {state.contract}")
        return 0
    if args.task_command == "list":
        for state in list_task_states(ctx.repo_root):
            print(f"#{state.issue}\t{state.execution_state}\t{state.semantic_phase}\t{state.classification}\t{state.contract}")
        return 0
    if args.task_command == "migrate-current":
        state = migrate_legacy_current_task(ctx.repo_root)
        print(f"[INFO] migrated current task: #{state.issue}")
        return 0
    raise ValueError(f"unknown task subcommand: {args.task_command}")


def body_from_file(path: Path | None, inline: str | None, required_message: str) -> tuple[str, Path]:
    if inline:
        raise ValueError(required_message)
    if path is None:
        raise ValueError(required_message)
    if not path.is_file():
        raise ValueError(f"body file does not exist: {path}")
    return path.read_text(encoding="utf-8"), path


def prepare_attachment_body(
    repo_root: Path,
    issue: str,
    body_file: Path,
    args: argparse.Namespace,
) -> tuple[str, Path, Path | None]:
    manifest_path = getattr(args, "attachments", None)
    attach_files = list(getattr(args, "attach_file", []) or [])
    upload_backend = getattr(args, "upload_attachments", None)
    if attach_files or upload_backend:
        raise ValueError(
            "inline issue attachments are disabled; use attachment add/publish/render "
            "before issue create or comment"
        )

    if manifest_path is not None:
        attachment.reject_issue_image_attachments(repo_root, manifest_path, issue)
    attachment.ensure_publishable(repo_root, body_file, manifest_path, issue if manifest_path else None)
    return body_file.read_text(encoding="utf-8"), body_file, manifest_path


def require_requested_unattended(repo_root: Path, issue: str, requested: bool) -> None:
    if not requested:
        return
    try:
        unattended.require_active(repo_root, issue)
    except ValueError:
        raise ValueError("--no-local-review requires active task-scoped unattended mode") from None


def current_task_issue(repo_root: Path) -> str:
    path = repo_root / ".xflow" / "current-task.md"
    if not path.is_file():
        return ""
    value = markdown_field(path.read_text(encoding="utf-8-sig"), "Issue")
    return normalized_issue(value) if value else ""


def resolve_action_issue(
    ctx: RuntimeContext,
    explicit_issue: str | None,
    *,
    strict_state: bool = False,
) -> str:
    sources: list[tuple[str, str]] = []
    for name, value in (
        ("explicit", explicit_issue or ""),
        ("branch", branch_meta(ctx.repo_root, "issue")),
        ("current-task", current_task_issue(ctx.repo_root)),
    ):
        if value:
            sources.append((name, normalized_issue(value)))
    try:
        state = unattended.load(ctx.repo_root)
    except ValueError:
        if strict_state:
            raise
        state = None
    if state is not None:
        sources.append(("unattended", state.issue))

    identities = {value for _name, value in sources}
    if len(identities) > 1:
        details = ", ".join(f"{name}={value}" for name, value in sources)
        raise ValueError(f"Issue identity mismatch: {details}")
    return sources[0][1] if sources else ""


def check_action_current_task(repo_root: Path, issue: str) -> None:
    if issue != "draft":
        check_current_task(repo_root, issue)


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
        issue_id = normalized_issue(args.number)
        item = providers.show_issue(ctx.repo_root, issue_id, os.environ)
        print(f"#{item.get('number', issue_id)} [{item.get('state', '')}] {item.get('title', '')}")
        if item.get("body"):
            print()
            print(item["body"])
        if item.get("html_url"):
            print()
            print(item["html_url"])
        return 0
    if args.issue_command == "comment":
        issue_id = resolve_action_issue(ctx, args.number)
        _body, file_path = body_from_file(args.body_file, args.body, "remote issue comments require --body-file for local review")
        check_action_current_task(ctx.repo_root, issue_id)
        require_requested_unattended(ctx.repo_root, issue_id, args.no_local_review)
        body, file_path, manifest_path = prepare_attachment_body(ctx.repo_root, issue_id, file_path, args)
        grant = approval.require_remote_or_unattended(
            ctx.repo_root,
            "issue-comment",
            file_path,
            issue_id,
            manifest_path,
            request_unattended=args.no_local_review,
        )
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] issue-comment gate passed; provider skipped")
            return 0
        result = providers.comment_issue(ctx.repo_root, issue_id, body, os.environ)
        approval.record_consumed_approval(ctx.repo_root, grant, "success")
        print(f"[INFO] Comment posted on Issue #{issue_id}")
        if result.get("html_url"):
            print(f"[INFO] {result['html_url']}")
        return 0
    if args.issue_command == "close":
        issue_id = resolve_action_issue(ctx, args.number)
        file_path = Path(os.environ.get("DEVCTL_APPROVED_FILE", default_issue_file(ctx.repo_root, issue_id, "walkthrough.md")))
        check_action_current_task(ctx.repo_root, issue_id)
        grant = approval.require_remote_or_unattended(ctx.repo_root, "issue-close", file_path, issue_id)
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] issue-close gate passed; provider skipped")
            return 0
        result = providers.close_issue(ctx.repo_root, issue_id, os.environ)
        approval.record_consumed_approval(ctx.repo_root, grant, "success")
        unattended.disable(ctx.repo_root)
        print(f"[INFO] Issue #{result.get('number', issue_id)} closed")
        return 0
    if args.issue_command != "create":
        raise ValueError(f"unknown issue subcommand: {args.issue_command}")
    issue_id = resolve_action_issue(ctx, "draft")
    _body, file_path = body_from_file(args.body_file, args.body, "remote issue creation requires --body-file for local review")
    check_issue_draft(file_path)
    require_requested_unattended(ctx.repo_root, issue_id, args.no_local_review)
    body, file_path, manifest_path = prepare_attachment_body(ctx.repo_root, issue_id, file_path, args)
    grant = approval.require_remote_or_unattended(
        ctx.repo_root,
        "issue-create",
        file_path,
        issue_id,
        manifest_path,
        request_unattended=args.no_local_review,
    )
    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] issue-create gate passed; provider skipped")
        return 0
    result = providers.create_issue(ctx.repo_root, args.title, body, args.labels, os.environ)
    created_issue = normalized_issue(result.number)
    approval.record_consumed_approval(ctx.repo_root, grant, "success", target_issue=created_issue)
    if grant.source == "unattended":
        unattended.migrate_issue(ctx.repo_root, issue_id, result.number)
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


def enable_worktree_config(repo_root: Path) -> None:
    enabled = git_output(
        repo_root,
        ["config", "--local", "--get", "extensions.worktreeConfig"],
    ).lower()
    if enabled == "true":
        return
    git_run(repo_root, ["config", "--local", "extensions.worktreeConfig", "true"])


def set_branch_meta(repo_root: Path, key: str, value: str) -> None:
    if key == "issue":
        value = normalized_issue(value)
    enable_worktree_config(repo_root)
    git_run(repo_root, ["config", "--worktree", f"devctl.{key}", value])


def branch_meta(repo_root: Path, key: str) -> str:
    value = git_output(repo_root, ["config", "--worktree", "--get", f"devctl.{key}"])
    return normalized_issue(value) if key == "issue" and value else value


def unset_branch_meta(repo_root: Path, key: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "--worktree", "--unset-all", f"devctl.{key}"],
        check=False,
    )


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
    return f"{prefix}/{normalized_issue(issue)}-{normalized}" if issue else f"{prefix}/{normalized}"


def changed_paths(repo_root: Path) -> list[str]:
    paths = set()
    for args in (
        ["diff", "--cached", "--name-only"],
        ["diff", "--name-only"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
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


def summarize_commit_message(
    repo_root: Path,
    full_message: str | None,
    summary_override: str | None,
) -> str:
    issue = branch_meta(repo_root, "issue")
    if not issue:
        raise ValueError("commit message requires branch Issue identity metadata")
    if full_message is not None and summary_override is not None:
        raise ValueError("use either -m/--message or positional summary, not both")
    if full_message is not None:
        check_commit_message(full_message, branch_issue=issue)
        return full_message

    paths = changed_paths(repo_root)
    if not paths:
        raise ValueError("no changes to summarize")
    names = [Path(path).name for path in paths[:3]]
    changed_summary = names[0] if len(paths) == 1 else f"{len(paths)} 个任务文件"
    summary = summary_override.strip() if summary_override is not None else f"更新 {changed_summary}"
    if not summary:
        raise ValueError("positional summary must be a non-empty Chinese core summary")
    message = (
        f"{guess_commit_type(paths)}({guess_commit_scope(paths)}): {summary}[#{issue}]\n\n"
        f"- 修改范围包含 {changed_summary}\n"
        "- 验证结果由提交前检查确认"
    )
    check_commit_message(message, branch_issue=issue)
    return message


@dataclass(frozen=True)
class PushResult:
    performed: bool
    success: bool


def record_backfill_effect_if_confirmed(
    repo_root: Path,
    grant: approval.ApprovalGrant,
    push_result: PushResult | None,
) -> Path | None:
    if push_result is None or not push_result.performed or not push_result.success:
        return None
    return approval.record_subordinate_effect(repo_root, grant, "git-state-backfill", "success")


def push_branch(repo_root: Path, branch: str) -> PushResult:
    if os.environ.get("DEVCTL_SKIP_PUSH") == "1":
        return PushResult(performed=False, success=False)
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
    return PushResult(performed=True, success=True)


def require_branch_ready_for_mr(repo_root: Path, branch: str) -> None:
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    if not upstream:
        raise ValueError("task branch has no upstream; run devctl git push before devctl git mr")
    ahead = int(git_output(repo_root, ["rev-list", "--count", f"{upstream}..HEAD"]) or "0")
    if ahead:
        raise ValueError(f"task branch has {ahead} unpushed commit(s); run devctl git push before devctl git mr")


def require_branch_contains_remote_base(repo_root: Path, base: str) -> None:
    git_run(repo_root, ["fetch", "origin", base])
    remote_base = f"origin/{base}"
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", remote_base, "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        raise ValueError(
            f"current HEAD does not contain {remote_base}; synchronize the task branch and rerun checks before MR"
        )
    if result.returncode != 0:
        raise ValueError(f"cannot verify target baseline {remote_base}")


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


def commit_and_push_pr_backfill(
    repo_root: Path,
    branch: str,
    paths: list[Path],
    pr_number: str,
    issue: str,
) -> PushResult | None:
    staged_before = {
        line
        for line in git_run(repo_root, ["diff", "--cached", "--name-only"]).splitlines()
        if line
    }
    if staged_before:
        raise ValueError(
            "PR state backfill requires an empty index; found staged paths: "
            + ", ".join(sorted(staged_before))
        )
    if not paths:
        return None
    expected_paths = {
        path.relative_to(repo_root).as_posix()
        for path in paths
        if path.exists()
    }
    for relative_path in sorted(expected_paths):
        git_run(repo_root, ["add", "--", relative_path])
    staged_after = {
        line
        for line in git_run(repo_root, ["diff", "--cached", "--name-only"]).splitlines()
        if line
    }
    if staged_after != expected_paths:
        raise ValueError(
            "PR state backfill staged paths must exactly match metadata paths; "
            f"expected {sorted(expected_paths)}, found {sorted(staged_after)}"
        )
    if not staged_after:
        return None
    message = (
        f"chore(xflow): 回填合并请求状态[#{normalized_issue(issue)}]\n\n"
        "- 记录合并请求编号与远端链接\n"
        "- 同步当前任务状态文件"
    )
    check_commit_message(message, branch_issue=issue)
    git_run(repo_root, ["commit", "-m", message])
    return push_branch(repo_root, branch)


def run_git_push(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    issue = resolve_action_issue(ctx, args.issue)
    if not issue:
        raise ValueError("devctl git push requires --issue or branch issue metadata")
    approved_file = args.file or default_issue_file(ctx.repo_root, issue, "walkthrough.md")
    if not approved_file.is_file():
        raise ValueError(f"approved file does not exist: {approved_file}")
    check_current_task(ctx.repo_root, issue)
    branch = current_branch(ctx.repo_root)
    base = branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is {base}; start a task branch before pushing")
    grant = approval.require_remote_or_unattended(ctx.repo_root, "git-push", approved_file, issue)
    push_result = push_branch(ctx.repo_root, branch)
    if push_result.performed and push_result.success:
        approval.record_consumed_approval(ctx.repo_root, grant, "success")
        print(f"[INFO] pushed {branch}")
    else:
        print(f"[INFO] push skipped for {branch}")
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
    unattended.disable(ctx.repo_root)
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
    message = summarize_commit_message(ctx.repo_root, args.message, args.summary)
    if args.all:
        git_run(ctx.repo_root, ["add", "-A"])
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
    issue = resolve_action_issue(ctx, args.issue)
    if not issue:
        raise ValueError("devctl git done requires --issue or branch issue metadata")
    approved_file = args.file or default_issue_file(ctx.repo_root, issue, "resolution-report.md")
    action = "git-cleanup-force" if args.force else "git-cleanup"
    approval.require_exact_remote(ctx.repo_root, action, approved_file, issue)
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
        delete_flag = "-D" if args.force else "-d"
        git_run(ctx.repo_root, ["branch", delete_flag, branch])
        print(f"[INFO] deleted local branch {branch}")
    for key in ("slug", "issue", "base", "pr", "pr-url"):
        unset_branch_meta(ctx.repo_root, key)
    unattended.disable(ctx.repo_root)
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
        issue = resolve_action_issue(ctx, args.issue)
        if not issue:
            raise ValueError("devctl git pr-merge requires --issue or branch issue metadata")
        approved_file = args.file or default_issue_file(ctx.repo_root, issue, "mr-draft.md")
        if not approved_file.is_file():
            raise ValueError(f"approved file does not exist: {approved_file}")
        check_current_task(ctx.repo_root, issue)
        check_mr_draft(approved_file)
        branch = current_branch(ctx.repo_root)
        base = branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
        if branch == base:
            raise ValueError(f"current branch is {base}; checkout the recorded task branch before merging its PR")
        recorded_pr = branch_meta(ctx.repo_root, "pr")
        if not recorded_pr:
            raise ValueError("devctl git pr-merge requires branch PR metadata")
        requested_pr = normalized_issue(args.number)
        if normalized_issue(recorded_pr) != requested_pr:
            raise ValueError(f"recorded PR mismatch: expected {recorded_pr}, got {requested_pr}")
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            raise ValueError("git pr-merge requires provider PR identity verification")
        remote_pr = providers.normalize_pull_request_identity(
            providers.get_pull_request(ctx.repo_root, requested_pr, os.environ)
        )
        if remote_pr.number != requested_pr:
            raise ValueError(f"pull request number mismatch: expected {requested_pr}, got {remote_pr.number}")
        if remote_pr.state != "open":
            raise ValueError(f"pull request state mismatch: expected open, got {remote_pr.state}")
        if remote_pr.head != branch:
            raise ValueError(f"pull request head branch mismatch: expected {branch}, got {remote_pr.head}")
        if remote_pr.base != base:
            raise ValueError(f"pull request base branch mismatch: expected {base}, got {remote_pr.base}")
        grant = approval.require_remote_or_unattended(ctx.repo_root, "git-pr-merge", approved_file, issue)
        result = providers.merge_pull_request(
            ctx.repo_root,
            requested_pr,
            args.method,
            args.commit_title,
            args.commit_message,
            os.environ,
        )
        approval.record_consumed_approval(ctx.repo_root, grant, "success")
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
    issue = resolve_action_issue(ctx, args.issue)
    if not issue:
        raise ValueError("devctl git mr requires --issue or branch issue metadata")
    body_file = args.body_file or default_issue_file(ctx.repo_root, issue, "mr-draft.md")
    if not body_file.is_file():
        raise ValueError(f"body file does not exist: {body_file}")
    check_current_task(ctx.repo_root, issue)
    check_mr_draft(body_file)
    attachment.ensure_publishable(ctx.repo_root, body_file, args.attachments, issue if args.attachments else None)
    branch = current_branch(ctx.repo_root)
    base = args.base or branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is {base}; start a task branch before creating an MR")
    require_branch_ready_for_mr(ctx.repo_root, branch)
    require_branch_contains_remote_base(ctx.repo_root, base)
    grant = approval.require_remote_or_unattended(
        ctx.repo_root,
        "git-mr",
        body_file,
        issue,
        args.attachments,
    )
    title = args.title or f"[#{issue}] {branch.replace('-', ' ')}"
    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] git-mr gate passed; provider skipped")
        return 0
    result = providers.create_pull_request(ctx.repo_root, title, body_file.read_text(encoding="utf-8"), branch, base, os.environ)
    approval.record_consumed_approval(ctx.repo_root, grant, "success")
    set_branch_meta(ctx.repo_root, "pr", result.number)
    if result.html_url:
        set_branch_meta(ctx.repo_root, "pr-url", result.html_url)
    suggestion = write_pr_state_update_suggestion(ctx.repo_root, issue, result.number, result.html_url)
    backfill_paths = [suggestion, *update_current_task_for_pr(ctx.repo_root, issue, result.number, result.html_url)]
    backfill_pushed = commit_and_push_pr_backfill(ctx.repo_root, branch, backfill_paths, result.number, issue)
    record_backfill_effect_if_confirmed(ctx.repo_root, grant, backfill_pushed)
    print(f"[INFO] PR #{result.number} created")
    if result.html_url:
        print(f"[INFO] {result.html_url}")
    print(f"[INFO] state update suggestion: {suggestion}")
    if backfill_pushed is not None and backfill_pushed.performed and backfill_pushed.success:
        print("[INFO] state backfill pushed")
    elif backfill_pushed is not None:
        print("[INFO] state backfill push skipped")
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


def run_unattended(args: argparse.Namespace) -> int:
    ctx = context()
    if args.unattended_command == "enable":
        issue = resolve_action_issue(ctx, args.issue)
        state = unattended.enable(ctx.repo_root, issue, args.confirm)
        print(f"[INFO] task-scoped unattended mode enabled for current task {state.issue}")
        return 0
    if args.unattended_command == "status":
        try:
            resolve_action_issue(ctx, None, strict_state=True)
            state = unattended.load(ctx.repo_root)
        except ValueError as exc:
            print(f"[WARN] unattended mode invalid: {exc}")
            return 0
        if state is None:
            print("[INFO] task-scoped unattended mode inactive")
        else:
            print(f"[INFO] task-scoped unattended mode active for current task {state.issue}")
        return 0
    if args.unattended_command == "disable":
        removed = unattended.disable(ctx.repo_root)
        status = "disabled" if removed else "already inactive"
        print(f"[INFO] task-scoped unattended mode {status}")
        return 0
    raise ValueError(f"unknown unattended subcommand: {args.unattended_command}")


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
            output = args.output or attachment.default_rendered_body(ctx.repo_root, issue, args.body_file)
            rendered = attachment.render_body(ctx.repo_root, issue, path, args.body_file, output)
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
    if args.migrate_command == "issue-workspace":
        report = (
            apply_issue_workspace_migration(ctx.repo_root, args.mode)
            if args.apply
            else inspect_issue_workspace_migration(ctx.repo_root, args.mode)
        )
        print(f"mode: {report.mode}")
        print(f"contract root: {report.contract_root.as_posix()}")
        print(f"git ignore source: {report.git_ignore_source or '<none>'}")
        for line in report.exact_ignore_lines:
            print(f"exact issue workspace ignore line: {line}")
        for path in report.active_approvals:
            print(f"active approval: {path}")
        for path in report.oversized_files:
            print(f"file over 10 MiB: {path}")
        for path in report.absolute_path_files:
            print(f"local absolute path: {path}")
        for path in report.credential_files:
            print(f"credential-like text: {path}")
        for error in report.scan_errors:
            print(f"scan error: {error}")
        for action in report.manual_actions:
            print(action)
        if args.apply:
            if args.mode == "tracked" and report.exact_ignore_lines:
                print("[INFO] removed exact .gitignore lines: " + ", ".join(report.exact_ignore_lines))
            print(f"[INFO] issue workspace migration applied: {report.mode}")
        return 0
    raise ValueError(f"unknown migrate subcommand: {args.migrate_command}")


def main(argv: list[str] | None = None) -> int:
    load_env_files(os.environ)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return run_preflight()
        if args.command == "check":
            return run_check(args)
        if args.command == "task":
            return run_task(args)
        if args.command == "issue":
            return run_issue(args)
        if args.command == "git":
            return run_git(args)
        if args.command == "approval":
            return run_approval(args)
        if args.command == "unattended":
            return run_unattended(args)
        if args.command == "attachment":
            return run_attachment(args)
        if args.command == "rules":
            return run_rules(args)
        if args.command == "migrate":
            return run_migrate(args)
        parser.print_help()
        return 0
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
