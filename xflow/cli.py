from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from .approval import check_local_review_file, require_remote_approval
from .claude_runner import run_claude_doctor, run_claude_task
from .checks import (
    check_academic_issue,
    check_academic_mr,
    check_claude_package,
    check_submodule_hygiene,
    check_tdd_result,
)
from .env import RuntimeContext, detect_python_runtime
from .paths import default_issue_file
from .providers import create_issue, create_pull_request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devctl")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight")

    check = sub.add_parser("check")
    check_sub = check.add_subparsers(dest="check_command")
    academic_issue = check_sub.add_parser("academic-issue")
    academic_issue.add_argument("--issue")
    academic_issue.add_argument("--file", type=Path)
    tdd_result = check_sub.add_parser("tdd-result")
    tdd_result.add_argument("--issue")
    tdd_result.add_argument("--file", type=Path)
    claude_package = check_sub.add_parser("claude-package")
    claude_package.add_argument("--issue")
    claude_package.add_argument("--file", type=Path)
    academic_mr = check_sub.add_parser("academic-mr")
    academic_mr.add_argument("--issue")
    academic_mr.add_argument("--file", type=Path)
    local_review = check_sub.add_parser("local-review")
    local_review.add_argument("--issue")
    local_review.add_argument("--file", type=Path)
    check_sub.add_parser("submodule-hygiene")

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command")
    issue_create = issue_sub.add_parser("create")
    issue_create.add_argument("title")
    issue_create.add_argument("--body")
    issue_create.add_argument("--body-file", type=Path)
    issue_create.add_argument("--labels")

    git = sub.add_parser("git")
    git_sub = git.add_subparsers(dest="git_command")
    git_mr = git_sub.add_parser("mr")
    git_mr.add_argument("--title")
    git_mr.add_argument("--body")
    git_mr.add_argument("--body-file", type=Path)
    git_mr.add_argument("--base")
    git_mr.add_argument("--issue")

    claude = sub.add_parser("claude")
    claude_sub = claude.add_subparsers(dest="claude_command")
    claude_sub.add_parser("doctor")
    claude_run = claude_sub.add_parser("run")
    claude_run.add_argument("--issue", required=True)
    claude_run.add_argument("--file", type=Path)
    claude_run.add_argument("--output", type=Path)
    claude_run.add_argument("--dry-run", action="store_true")
    return parser


def run_preflight() -> int:
    runtime = detect_python_runtime()
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    print(f"python: {runtime.executable}")
    print(f"version: {runtime.version_info[0]}.{runtime.version_info[1]}.{runtime.version_info[2]}")
    print(f"tool_root: {context.tool_root}")
    print(f"repo_root: {context.repo_root}")
    print(f"product_line: {context.product_line or 'unset'}")
    return 0


def resolve_check_file(context: RuntimeContext, issue: str | None, file: Path | None, filename: str) -> Path:
    if file is not None:
        return file
    if not issue:
        raise ValueError("--issue is required")
    return default_issue_file(context.repo_root, issue, filename)


def run_check(args: argparse.Namespace) -> int:
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    try:
        if args.check_command == "academic-issue":
            path = resolve_check_file(context, args.issue, args.file, "issue-draft.md")
            check_academic_issue(path)
        elif args.check_command == "tdd-result":
            path = resolve_check_file(context, args.issue, args.file, "tdd-result.md")
            check_tdd_result(path)
        elif args.check_command == "claude-package":
            path = resolve_check_file(context, args.issue, args.file, "claude-task.md")
            check_claude_package(path)
        elif args.check_command == "academic-mr":
            path = resolve_check_file(context, args.issue, args.file, "mr-draft.md")
            check_academic_mr(path)
        elif args.check_command == "local-review":
            path = resolve_check_file(context, args.issue, args.file, "issue-draft.md")
            issue = args.issue or "draft"
            check_local_review_file(context.repo_root, issue, path)
        elif args.check_command == "submodule-hygiene":
            path = context.repo_root
            check_submodule_hygiene(path)
        else:
            raise ValueError(f"unknown check subcommand: {args.check_command}")
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[INFO] {args.check_command} check passed: {path}")
    return 0


def run_issue(args: argparse.Namespace) -> int:
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    try:
        if args.issue_command != "create":
            raise ValueError(f"unknown issue subcommand: {args.issue_command}")
        if args.body_file is None:
            raise ValueError("academic issue create requires --body-file")
        require_remote_approval(context.repo_root, "issue-create", args.body_file, "draft")
        body = args.body_file.read_text(encoding="utf-8")
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] issue-create gate passed; provider skipped")
        return 0

    try:
        result = create_issue(context.repo_root, args.title, body, args.labels, os.environ)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_set_config(repo_root: Path, key: str, value: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--local", f"devctl.{key}", value],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot set git config devctl.{key}: {result.stderr.strip()}")


def current_branch(repo_root: Path) -> str:
    branch = git_output(repo_root, ["branch", "--show-current"])
    if not branch:
        raise ValueError("cannot determine current git branch")
    return branch


def branch_meta(repo_root: Path, key: str) -> str:
    return git_output(repo_root, ["config", "--local", "--get", f"devctl.{key}"])


def default_base_branch(repo_root: Path) -> str:
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/master"]).returncode == 0:
        return "master"
    if subprocess.run(["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", "refs/heads/main"]).returncode == 0:
        return "main"
    remote_head = git_output(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if remote_head.startswith("origin/"):
        return remote_head.removeprefix("origin/")
    return "master"


def push_current_branch(repo_root: Path, branch: str, env: Mapping[str, str]) -> None:
    if env.get("DEVCTL_SKIP_PUSH") == "1":
        return
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    command = ["push", "origin", branch] if upstream else ["push", "-u", "origin", branch]
    result = subprocess.run(
        ["git", "-C", str(repo_root), *command],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(f"git push failed: {result.stderr.strip() or result.stdout.strip()}")


def derive_mr_title(branch: str, issue: str) -> str:
    raw = branch
    for prefix in ("feature/", "fix/", "feat/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    title = raw.replace("-", " ")
    return f"[#{issue}] {title}" if issue else title


def run_git(args: argparse.Namespace) -> int:
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    try:
        if args.git_command != "mr":
            raise ValueError(f"unknown git subcommand: {args.git_command}")
        if args.body and args.body_file:
            raise ValueError("use only one of --body or --body-file")
        if args.body:
            raise ValueError("academic git mr requires --body-file or the default issue MR draft")

        issue = args.issue or branch_meta(context.repo_root, "issue")
        if not issue:
            raise ValueError("academic git mr requires --issue or devctl.issue branch metadata")

        body_file = args.body_file or default_issue_file(context.repo_root, issue, "mr-draft.md")
        require_remote_approval(context.repo_root, "git-mr", body_file, issue)
        body = body_file.read_text(encoding="utf-8")

        branch = current_branch(context.repo_root)
        base = args.base or branch_meta(context.repo_root, "base") or default_base_branch(context.repo_root)
        if branch == base:
            raise ValueError(f"current branch is {base}; start a task branch before creating an MR")

        push_current_branch(context.repo_root, branch, os.environ)
        title = args.title or derive_mr_title(branch, issue)
        result = create_pull_request(context.repo_root, title, body, branch, base, os.environ)
        git_set_config(context.repo_root, "pr", result.number)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(f"[INFO] PR #{result.number} created")
    if result.html_url:
        print(f"[INFO] {result.html_url}")
    print(result.number)
    return 0


def run_claude(args: argparse.Namespace) -> int:
    context = RuntimeContext.from_env(Path(__file__).resolve().parents[1], os.environ)
    try:
        if args.claude_command == "doctor":
            doctor = run_claude_doctor(os.environ)
            print(f"claude_cli: {'ok' if doctor.claude_cli_ok else 'missing'}")
            print(f"academicforge: {'ok' if doctor.academicforge_ok else 'missing'}")
            print(f"claude_config: {doctor.config_file}")
            if not doctor.academicforge_ok:
                print("No installation was performed.")
                print("After human review, run:")
                print(f"  {doctor.install_command}")
            return 0 if doctor.claude_cli_ok and doctor.academicforge_ok else 1
        if args.claude_command != "run":
            raise ValueError(f"unknown claude subcommand: {args.claude_command}")
        task_file = args.file or resolve_check_file(context, args.issue, None, "claude-task.md")
        result = run_claude_task(context, task_file, args.output, args.dry_run, os.environ)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if result.dry_run:
        print(f"[INFO] claude task package ready: {result.task_file}")
        print(f"[INFO] claude output target: {result.output_file}")
    else:
        print(f"[INFO] claude result written: {result.output_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return run_preflight()
    if args.command == "check":
        return run_check(args)
    if args.command == "issue":
        return run_issue(args)
    if args.command == "git":
        return run_git(args)
    if args.command == "claude":
        return run_claude(args)
    parser.print_help()
    return 0
