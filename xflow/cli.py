from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import approval, providers, rules
from .checks import (
    check_current_task,
    check_issue_draft,
    check_mr_draft,
    check_submodule_hygiene,
    write_pr_state_update_suggestion,
)
from .env import RuntimeContext, load_env_files, python_version, token_status_lines
from .migration import inspect, write_wrappers
from .paths import default_issue_file


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
    check_sub.add_parser("submodule-hygiene")
    current_task = check_sub.add_parser("current-task")
    current_task.add_argument("--issue")

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command")
    issue_create = issue_sub.add_parser("create")
    issue_create.add_argument("title")
    issue_create.add_argument("--body")
    issue_create.add_argument("--body-file", type=Path)
    issue_create.add_argument("--labels")
    issue_list = issue_sub.add_parser("list")
    issue_list.add_argument("--state", choices=("open", "closed", "all"), default="open")
    issue_list.add_argument("--limit", type=int, default=20)
    issue_show = issue_sub.add_parser("show")
    issue_show.add_argument("number")
    issue_comment = issue_sub.add_parser("comment")
    issue_comment.add_argument("number")
    issue_comment.add_argument("--body")
    issue_comment.add_argument("--body-file", type=Path)
    issue_close = issue_sub.add_parser("close")
    issue_close.add_argument("number")

    git = sub.add_parser("git")
    git_sub = git.add_subparsers(dest="git_command")
    git_mr = git_sub.add_parser("mr")
    git_mr.add_argument("--title")
    git_mr.add_argument("--body")
    git_mr.add_argument("--body-file", type=Path)
    git_mr.add_argument("--base")
    git_mr.add_argument("--issue")
    pr_get = git_sub.add_parser("pr-get")
    pr_get.add_argument("number")

    approval_parser = sub.add_parser("approval")
    approval_sub = approval_parser.add_subparsers(dest="approval_command")
    approval_prepare = approval_sub.add_parser("prepare")
    approval_prepare.add_argument("--issue", required=True)
    approval_prepare.add_argument("--action", required=True)
    approval_prepare.add_argument("--file", required=True, type=Path)
    approval_prepare.add_argument("--command", dest="suggested_command")
    approval_prepare.add_argument("--reviewer")
    approval_prepare.add_argument("--force", action="store_true")

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
            approval.require_remote(ctx.repo_root, args.action, path, args.issue)
        else:
            approval.check(ctx.repo_root, args.issue, path)
    elif args.check_command == "submodule-hygiene":
        path = ctx.repo_root
        check_submodule_hygiene(path)
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
        body, file_path = body_from_file(args.body_file, args.body, "remote issue comments require --body-file for local review")
        approval.require_remote(ctx.repo_root, "issue-comment", file_path, args.number)
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
    body, file_path = body_from_file(args.body_file, args.body, "remote issue creation requires --body-file for local review")
    approval.require_remote(ctx.repo_root, "issue-create", file_path, "draft")
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
    result = subprocess.run(["git", "-C", str(repo_root), *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip() if result.returncode == 0 else ""


def current_branch(repo_root: Path) -> str:
    branch = git_output(repo_root, ["branch", "--show-current"])
    if not branch:
        raise ValueError("cannot determine current git branch")
    return branch


def branch_meta(repo_root: Path, key: str) -> str:
    return git_output(repo_root, ["config", "--local", "--get", f"devctl.{key}"])


def default_base(repo_root: Path) -> str:
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/master"]).returncode == 0:
        return "master"
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/main"]).returncode == 0:
        return "main"
    return "main"


def push_branch(repo_root: Path, branch: str) -> None:
    if os.environ.get("DEVCTL_SKIP_PUSH") == "1":
        return
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    command = ["push", "origin", branch] if upstream else ["push", "-u", "origin", branch]
    result = subprocess.run(["git", "-C", str(repo_root), *command], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise ValueError(f"git push failed: {result.stderr.strip() or result.stdout.strip()}")


def run_git(args: argparse.Namespace) -> int:
    ctx = context()
    if args.git_command == "pr-get":
        pr = providers.get_pull_request(ctx.repo_root, args.number, os.environ)
        print(f"#{pr.get('number', args.number)} [{pr.get('state', '')}] {pr.get('title', '')}")
        if pr.get("html_url"):
            print()
            print(pr["html_url"])
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
    approval.require_remote(ctx.repo_root, "git-mr", body_file, issue)
    branch = current_branch(ctx.repo_root)
    base = args.base or branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
    if branch == base:
        raise ValueError(f"current branch is {base}; start a task branch before creating an MR")
    push_branch(ctx.repo_root, branch)
    title = args.title or f"[#{issue}] {branch.replace('-', ' ')}"
    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] git-mr gate passed; provider skipped")
        return 0
    result = providers.create_pull_request(ctx.repo_root, title, body_file.read_text(encoding="utf-8"), branch, base, os.environ)
    subprocess.run(["git", "-C", str(ctx.repo_root), "config", "--local", "devctl.pr", result.number], check=False)
    if result.html_url:
        subprocess.run(["git", "-C", str(ctx.repo_root), "config", "--local", "devctl.pr-url", result.html_url], check=False)
    suggestion = write_pr_state_update_suggestion(ctx.repo_root, issue, result.number, result.html_url)
    print(f"[INFO] PR #{result.number} created")
    if result.html_url:
        print(f"[INFO] {result.html_url}")
    print(f"[INFO] state update suggestion: {suggestion}")
    print(result.number)
    return 0


def run_approval(args: argparse.Namespace) -> int:
    ctx = context()
    if args.approval_command != "prepare":
        raise ValueError(f"unknown approval subcommand: {args.approval_command}")
    path = approval.prepare(ctx.repo_root, args.issue, args.action, args.file, args.suggested_command, args.reviewer, args.force)
    print(f"[INFO] local review prepared: {path}")
    return 0


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
        if args.command == "rules":
            return run_rules(args)
        if args.command == "migrate":
            return run_migrate(args)
        parser.print_help()
        return 0
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
