from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
        else:
            raise ValueError(f"unknown check subcommand: {args.check_command}")
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[INFO] {args.check_command} check passed: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return run_preflight()
    if args.command == "check":
        return run_check(args)
    parser.print_help()
    return 0
