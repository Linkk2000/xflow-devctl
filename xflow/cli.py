from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .approval import check_local_review_file, require_remote_approval
from .checks import check_academic_issue, check_tdd_result
from .env import RuntimeContext, detect_python_runtime
from .paths import default_issue_file


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
    local_review = check_sub.add_parser("local-review")
    local_review.add_argument("--issue")
    local_review.add_argument("--file", type=Path)

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command")
    issue_create = issue_sub.add_parser("create")
    issue_create.add_argument("title")
    issue_create.add_argument("--body")
    issue_create.add_argument("--body-file", type=Path)
    issue_create.add_argument("--labels")
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
        elif args.check_command == "local-review":
            path = resolve_check_file(context, args.issue, args.file, "issue-draft.md")
            issue = args.issue or "draft"
            check_local_review_file(context.repo_root, issue, path)
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
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
        print("[INFO] issue-create gate passed; provider skipped")
        return 0

    print("[ERROR] provider not ported to Python yet", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return run_preflight()
    if args.command == "check":
        return run_check(args)
    if args.command == "issue":
        return run_issue(args)
    parser.print_help()
    return 0
