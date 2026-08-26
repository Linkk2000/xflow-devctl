from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from . import approval, attachment, providers, rules, unattended
from .contracts import contract_diff_exit_code, diff_contracts, load_contract, render_contract_diff, validate_contract_acceptance
from .traceability import check_traceability
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
from .bindings import GitBindings, resolve_bindings
from .classification import check_classification
from .collaboration import (
    git_child_environment,
    inherited_lease_command,
    inherited_lease_present,
    repository_locked,
    repository_mutation,
)
from .cockpit import CockpitContext, CockpitProfile, load_cockpit_profile
from .commands import (
    execute_state,
    run_docker as run_cockpit_docker,
    run_preflight as run_cockpit_preflight,
)
from .env import (
    RuntimeContext,
    cockpit_root_from_env,
    load_env_files,
    load_target_env_files,
    parse_env_file,
    python_version,
    token_status_lines,
)
from .io import canonical_path, write_text_lf
from .dependencies import check_dependencies
from .migration import apply_issue_workspace_migration, inspect, inspect_issue_workspace_migration, write_wrappers
from .paths import default_issue_file, normalized_issue, task_state_file
from .task_state import (
    TaskState,
    _capture_file,
    _legacy_field,
    _pointer_snapshots,
    _snapshot_text,
    activate_task,
    activate_task_from_snapshot,
    list_task_states,
    load_active_task,
    load_active_task_snapshot,
    migrate_legacy_current_task,
    parse_task_state,
    task_authority_issues,
)
from .services import run_playground, run_scenario


SUPPORTED_COCKPIT_COMMANDS = (
    "state",
    "state show",
    "dev preflight",
    "dev docker setup",
    "dev docker status",
    "dev all",
    "run",
    "dev playground",
    "pg",
    "playground",
)

_ACTIVE_REPO_ROOT: Path | None = None
_ACTIVE_COCKPIT_PROFILE: CockpitProfile | None = None
_ACTIVE_COCKPIT_CONTEXT: CockpitContext | None = None


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
    parser.add_argument(
        "--profile",
        type=Path,
        help="cockpit profile path for the platform runtime",
    )
    parser.add_argument(
        "--cockpit-root",
        type=Path,
        help="cockpit root containing the profile and runtime files",
    )
    parser.add_argument(
        "--repo",
        help="declared sibling repository name in the cockpit workspace",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight")

    state = sub.add_parser(
        "state",
        help="read state through the configured cockpit provider",
    )
    state.add_argument(
        "state_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to the configured state provider (for example: show --json)",
    )

    dev = sub.add_parser("dev", help="run platform runtime development commands")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    dev_preflight = dev_sub.add_parser("preflight", help="check declared runtime capabilities")
    dev_preflight.add_argument("--warn-only", action="store_true")
    docker = dev_sub.add_parser("docker", help="inspect or prepare the Docker capability")
    docker_sub = docker.add_subparsers(dest="docker_command", required=True)
    docker_sub.add_parser("setup")
    docker_sub.add_parser("status")
    dev_sub.add_parser("all", help="run the profile's default development scenario")
    dev_playground = dev_sub.add_parser("playground", help="run a configured playground")
    dev_playground.add_argument("target", nargs="?", default="flowable")
    dev_playground.add_argument("--no-browser", dest="open_browser", action="store_false")

    sub.add_parser("run", help="run the profile's default development scenario")
    for alias in ("pg", "playground"):
        alias_parser = sub.add_parser(alias, help="run a configured playground")
        alias_parser.add_argument("target", nargs="?", default="flowable")
        alias_parser.add_argument("--no-browser", dest="open_browser", action="store_false")

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

    hook = sub.add_parser("hook")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("task-status")

    contract = sub.add_parser("contract")
    contract_sub = contract.add_subparsers(dest="contract_command", required=True)
    contract_lint = contract_sub.add_parser("lint")
    contract_lint.add_argument("--file", required=True, type=Path)
    contract_accept = contract_sub.add_parser("accept")
    contract_accept.add_argument("--issue", required=True)
    contract_accept.add_argument("--file", required=True, type=Path)
    contract_accept.add_argument("--objects", required=True)
    contract_diff = contract_sub.add_parser("diff")
    contract_diff.add_argument("--old", required=True, type=Path)
    contract_diff.add_argument("--new", required=True, type=Path)

    gap = sub.add_parser("gap")
    gap_sub = gap.add_subparsers(dest="gap_command", required=True)
    gap_recognize = gap_sub.add_parser("recognize")
    gap_recognize.add_argument("--issue", required=True)
    gap_recognize.add_argument("--file", required=True, type=Path)

    trace = sub.add_parser("trace")
    trace_sub = trace.add_subparsers(dest="trace_command", required=True)
    trace_check = trace_sub.add_parser("check")
    trace_check.add_argument("--issue", required=True)
    trace_check.add_argument(
        "--contract",
        type=Path,
        help=(
            "non-authoritative fail-closed evolution input; omit only when immutable "
            "sealed contract-acceptance history is authoritative"
        ),
    )
    trace_check.add_argument("--matrix", required=True, type=Path)

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
    git_start = git_sub.add_parser(
        "start",
        help="create and activate an approved final task branch without implementation or remote write",
    )
    git_start.add_argument("slug")
    git_start.add_argument("--issue")
    git_start.add_argument("--base")
    git_start.add_argument(
        "--file",
        type=Path,
        help="canonical task-state.md approved for task-branch-start",
    )
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
    approval_prepare.add_argument("--objects")
    approval_reconcile = approval_sub.add_parser("reconcile")
    approval_reconcile.add_argument("--issue", required=True)
    approval_reconcile.add_argument("--approval-id", required=True)
    approval_reconcile.add_argument("--outcome", choices=("no-effect", "success"), required=True)
    approval_reconcile.add_argument("--confirm", required=True)
    approval_reconcile.add_argument("--target-issue")
    approval_reconcile.add_argument("--provider-receipt")
    approval_supersede_branch = approval_sub.add_parser(
        "supersede-branch-start",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Human-only retirement of a reserved task-branch-start claim with no branch, "
            "metadata, activation, or history effect."
        ),
        epilog=(
            "Human Approval Is Non-Delegable. AI must never run this command or supply "
            "XFLOW_HUMAN_SUPERSEDE_TASK_BRANCH_START."
        ),
    )
    approval_supersede_branch.add_argument("--issue", required=True)
    approval_supersede_branch.add_argument("--approval-id", required=True)
    approval_supersede_branch.add_argument("--reason", required=True)
    approval_supersede_branch.add_argument("--confirm", required=True)

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
    env = os.environ
    if _ACTIVE_REPO_ROOT is not None:
        env = dict(os.environ)
        env["DEVCTL_REPO_ROOT"] = str(_ACTIVE_REPO_ROOT)
    return RuntimeContext.from_env(Path(__file__).resolve().parents[1], env)


_GLOBAL_OPTIONS = ("--profile", "--cockpit-root", "--repo")


def _normalize_global_argv(argv: list[str]) -> list[str]:
    """Allow the three global options before or after a command.

    argparse only accepts parent options before subcommands.  Pulling the
    recognized options into a prefix keeps provider/state arguments intact and
    leaves every unrecognized option for the command-specific parser.
    """

    globals_found: list[str] = []
    command_args: list[str] = []
    index = 0
    while index < len(argv):
        argument = str(argv[index])
        if argument == "--":
            command_args.extend(str(value) for value in argv[index:])
            break
        if argument in _GLOBAL_OPTIONS:
            globals_found.append(argument)
            if index + 1 < len(argv) and not str(argv[index + 1]).startswith("-"):
                globals_found.append(str(argv[index + 1]))
                index += 2
                continue
            index += 1
            continue
        if any(argument.startswith(option + "=") for option in _GLOBAL_OPTIONS):
            globals_found.append(argument)
        else:
            command_args.append(argument)
        index += 1
    if command_args and command_args[0] == "state":
        tail = command_args[1:]
        if tail and tail[0] not in {"-h", "--help", "--"}:
            return globals_found + ["state", "--", *tail]
    return globals_found + command_args


def _lexical_path(value: Path) -> Path:
    """Make an absolute path without resolving symlinks."""

    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(str(expanded)))


def _profile_env_path(env: Mapping[str, str], names: tuple[str, ...]) -> Path | None:
    values = env
    for name in names:
        value = str(values.get(name, "")).strip()
        if value:
            return _lexical_path(Path(value))
    return None


def _profile_inferred_root(profile_path: Path) -> Path:
    profile_dir = profile_path.parent
    return profile_dir.parent if profile_dir.name == ".xflow" else profile_dir


def _cockpit_command_requested(args: argparse.Namespace) -> bool:
    return str(getattr(args, "command", "")) in {
        "state",
        "dev",
        "run",
        "pg",
        "playground",
    }


def _profile_candidates(args: argparse.Namespace) -> tuple[Path, ...]:
    return tuple(
        canonical_path(candidate)
        for candidate, _root in _profile_candidates_with_roots(args)
    )


def _profile_candidates_with_roots(
    args: argparse.Namespace,
) -> tuple[tuple[Path, Path], ...]:
    candidates: list[tuple[Path, Path]] = []
    explicit = getattr(args, "profile", None)
    if explicit is not None:
        profile_path = _lexical_path(Path(explicit))
        configured_root = getattr(args, "cockpit_root", None)
        root = (
            _lexical_path(Path(configured_root))
            if configured_root is not None
            else _profile_inferred_root(profile_path)
        )
        candidates.append((profile_path, root))

    configured = _profile_env_path(
        os.environ,
        ("XFLOW_PROFILE", "XFLOW_COCKPIT_PROFILE", "DEVCTL_PROFILE", "DEVCTL_COCKPIT_PROFILE"),
    )
    if configured is not None:
        env_root = cockpit_root_from_env(os.environ)
        candidates.append((configured, env_root or _profile_inferred_root(configured)))

    root = getattr(args, "cockpit_root", None)
    if root is None:
        root = cockpit_root_from_env(os.environ)
    if root is not None:
        root_path = _lexical_path(Path(root))
        candidates.extend(
            (
                (root_path / ".xflow" / "cockpit.yaml", root_path),
                (root_path / "cockpit.yaml", root_path),
            )
        )

    if (
        _cockpit_command_requested(args)
        or getattr(args, "profile", None) is not None
        or getattr(args, "cockpit_root", None) is not None
        or getattr(args, "repo", None) is not None
    ):
        current = _lexical_path(Path.cwd())
        candidates.extend(
            (
                (current / ".xflow" / "cockpit.yaml", current),
                (current / "cockpit.yaml", current),
            )
        )

    unique: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for candidate, root_path in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append((candidate, root_path))
    return tuple(unique)


def _validate_profile_binding(profile_path: Path, discovery_root: Path) -> Path:
    lexical_path = _lexical_path(profile_path)
    root = canonical_path(Path(discovery_root))
    canonical_profile = canonical_path(lexical_path)
    if not (canonical_profile == root or root in canonical_profile.parents):
        raise ValueError(f"cockpit profile escapes declared root: {lexical_path}")
    canonical_profile_root = canonical_path(_profile_inferred_root(canonical_profile))
    if canonical_profile_root != root:
        raise ValueError(
            f"cockpit profile root binding mismatch: {lexical_path} (root {root})"
        )
    return canonical_profile


def _load_runtime_profile(args: argparse.Namespace) -> tuple[Path, CockpitProfile]:
    candidates_with_roots = _profile_candidates_with_roots(args)
    candidates = tuple(candidate for candidate, _root in candidates_with_roots)
    if not candidates:
        raise ValueError(
            "cockpit profile is required for platform runtime commands; use --profile PATH "
            "or --cockpit-root PATH"
        )
    explicit = getattr(args, "profile", None)
    if explicit is not None:
        profile_path, discovery_root = candidates_with_roots[0]
        if not profile_path.is_file():
            raise ValueError(f"cockpit profile does not exist: {profile_path}")
    else:
        selected = next(
            (
                (candidate, discovery_root)
                for candidate, discovery_root in candidates_with_roots
                if candidate.is_file()
            ),
            None,
        )
        if selected is None:
            searched = ", ".join(str(candidate) for candidate in candidates)
            raise ValueError(f"cockpit profile not found; searched: {searched}")
        profile_path, discovery_root = selected
    canonical_profile = _validate_profile_binding(profile_path, discovery_root)
    return canonical_profile, load_cockpit_profile(canonical_profile)


def _cockpit_root_for(args: argparse.Namespace, profile_path: Path) -> Path:
    configured = getattr(args, "cockpit_root", None)
    if configured is None:
        configured_root = cockpit_root_from_env(os.environ)
    else:
        configured_root = Path(configured)
    if configured_root is not None:
        root = canonical_path(Path(configured_root))
    elif profile_path.parent.name == ".xflow":
        root = canonical_path(profile_path.parent.parent)
    else:
        root = canonical_path(profile_path.parent)
    if profile_path != root and root not in profile_path.parents:
        raise ValueError(f"cockpit profile must stay under cockpit root: {profile_path}")
    return root


def _profile_command_values(profile: CockpitProfile) -> tuple[str, ...]:
    values: list[str] = []

    def add(command: object) -> None:
        if command is None:
            return
        values.extend(str(value) for value in getattr(command, "argv", ()))
        cwd = getattr(command, "cwd", None)
        if cwd is not None:
            values.append(str(cwd))
        values.extend(str(value) for value in getattr(command, "env", {}).values())

    add(profile.state_command)
    for check in profile.checks:
        add(check.command)
    for command in (
        profile.docker.cli_check,
        profile.docker.compose_check,
        profile.docker.engine_probe,
        profile.docker.image_probe,
    ):
        add(command)
    for dependency in profile.dependencies.values():
        add(dependency.up)
        add(dependency.ready)
        values.append(dependency.cwd)
    for service in profile.services.values():
        add(service.command)
        values.append(service.log_file)
    for playground in profile.playgrounds.values():
        add(playground.command)
        add(playground.build)
    return tuple(values)


def _declared_repo_names(profile: CockpitProfile) -> frozenset[str]:
    """Infer literal sibling names from profile paths without adding product knowledge."""

    names: set[str] = set()
    marker = "{workspace}/"
    for value in _profile_command_values(profile):
        start = 0
        while True:
            index = value.find(marker, start)
            if index < 0:
                break
            suffix = value[index + len(marker) :].split("/", 1)[0]
            if suffix and "{" not in suffix and "}" not in suffix:
                names.add(suffix)
            start = index + len(marker)
    return frozenset(names)


def _resolve_repo_name(
    name: str,
    *,
    workspace_root: Path,
    cockpit_root: Path,
    profile: CockpitProfile,
) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"repository must be a declared sibling name: {name}")
    candidate = canonical_path(workspace_root / name)
    if candidate.parent != canonical_path(workspace_root):
        raise ValueError(f"repository must stay under workspace root: {name}")
    if not candidate.is_dir():
        raise ValueError(f"repository sibling does not exist: {name}")
    declared = _declared_repo_names(profile)
    if declared and name not in declared and candidate != canonical_path(cockpit_root):
        raise ValueError(f"repository is not declared by cockpit profile: {name}")
    return candidate


def _runtime_for(args: argparse.Namespace) -> tuple[CockpitProfile, CockpitContext]:
    profile_path, profile = _load_runtime_profile(args)
    cockpit_root = _cockpit_root_for(args, profile_path)
    workspace_root = canonical_path(cockpit_root.parent)
    selected_name = getattr(args, "repo", None)
    if selected_name:
        repo_root = _resolve_repo_name(
            str(selected_name),
            workspace_root=workspace_root,
            cockpit_root=cockpit_root,
            profile=profile,
        )
    else:
        repo_root = canonical_path(Path(os.environ.get("DEVCTL_REPO_ROOT", Path.cwd())))
        allowed_roots = (cockpit_root, workspace_root)
        if not any(repo_root == root or root in repo_root.parents for root in allowed_roots):
            raise ValueError("repository root must be the cockpit root or a workspace child")
    runtime_env = dict(os.environ)
    runtime_env["DEVCTL_REPO_ROOT"] = str(repo_root)
    context_value = CockpitContext(
        cockpit_root=cockpit_root,
        workspace_root=workspace_root,
        repo_root=repo_root,
        python_executable=canonical_path(Path(sys.executable)),
        run_dir=canonical_path(cockpit_root / ".xflow" / "run"),
        env=runtime_env,
    )
    return profile, context_value


def _prepare_runtime(args: argparse.Namespace) -> tuple[CockpitProfile | None, CockpitContext | None]:
    needs_runtime = (
        _cockpit_command_requested(args)
        or getattr(args, "profile", None) is not None
        or getattr(args, "cockpit_root", None) is not None
        or getattr(args, "repo", None) is not None
    )
    if not needs_runtime:
        return None, None
    return _runtime_for(args)


def _state_provider_args(profile: CockpitProfile, args: argparse.Namespace) -> tuple[str, ...]:
    requested = tuple(str(value) for value in getattr(args, "state_args", ()))
    if requested and requested[0] == "--":
        requested = requested[1:]
    if requested:
        if profile.state_command.argv and profile.state_command.argv[-1] == "show" and requested[0] == "show":
            return requested[1:]
        return requested
    if profile.state_command.argv and profile.state_command.argv[-1] == "show":
        return ()
    return ("show",)


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


def _print_task_status(bindings: GitBindings, state: TaskState) -> None:
    print(f"repository: {bindings.repository[:12]}")
    print(f"worktree: {bindings.worktree[:12]}")
    print(f"branch: {bindings.branch}")
    print(f"Issue: {state.issue}")
    print(f"Execution State: {state.execution_state}")
    print(f"Semantic Phase: {state.semantic_phase}")
    print(f"Classification: {state.classification}")
    print(f"Contract: {state.contract}")


def run_hook(args: argparse.Namespace) -> int:
    if args.hook_command != "task-status":
        raise ValueError(f"unknown hook subcommand: {args.hook_command}")
    bindings, state = load_active_task_snapshot(context().repo_root)
    from .semantic_routes import require_route_semantics

    require_route_semantics(state, "commit")
    _print_task_status(bindings, state)
    return 0


def run_contract(args: argparse.Namespace) -> int:
    ctx = context()
    if args.contract_command == "diff":
        old = load_contract(ctx.repo_root, args.old)
        new = load_contract(ctx.repo_root, args.new)
        diff = diff_contracts(old, new)
        print(render_contract_diff(diff))
        return contract_diff_exit_code(diff)
    contract = load_contract(ctx.repo_root, args.file)
    if args.contract_command == "lint":
        print(f"[INFO] contract lint passed: {contract.path}")
        return 0
    if args.contract_command == "accept":
        object_ids = tuple(args.objects.split(","))
        record = validate_contract_acceptance(ctx.repo_root, args.issue, contract, object_ids)
        print(f"[INFO] contract acceptance recorded: {record}")
        return 0
    raise ValueError(f"unknown contract subcommand: {args.contract_command}")


def run_gap(args: argparse.Namespace) -> int:
    if args.gap_command != "recognize":
        raise ValueError(f"unknown gap subcommand: {args.gap_command}")
    ctx = context()
    record = approval.consume_gap_recognition(ctx.repo_root, args.issue, args.file)
    print(f"[INFO] gap recognition recorded: {record}")
    return 0


def run_trace(args: argparse.Namespace) -> int:
    ctx = context()
    if args.trace_command != "check":
        raise ValueError(f"unknown trace subcommand: {args.trace_command}")
    contract = load_contract(ctx.repo_root, args.contract) if args.contract is not None else None
    result = check_traceability(ctx.repo_root, args.issue, contract, args.matrix)
    print(f"[INFO] trace check passed: {result.path}")
    return 0


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


@repository_locked
def current_task_issue(repo_root: Path) -> str:
    from .local_artifacts import revalidate_snapshots

    bindings = resolve_bindings(repo_root)
    pointer_snapshot, legacy_pointer_snapshot = _pointer_snapshots(repo_root, bindings)
    if pointer_snapshot.exists or legacy_pointer_snapshot.exists:
        return load_active_task(repo_root).issue
    if task_authority_issues(repo_root):
        raise ValueError("missing active task pointer for retained task authority")
    root = repo_root.resolve()
    source_snapshot = _capture_file(
        root,
        root / ".xflow" / "current-task.md",
        root,
        "current task state file",
        required=False,
    )
    if not source_snapshot.exists:
        revalidate_snapshots(root, (source_snapshot,), "current task state file")
        return ""
    text = _snapshot_text(source_snapshot, "current task state file")
    value = _legacy_field(text, "Issue")
    issue = normalized_issue(value) if value else ""
    revalidate_snapshots(root, (source_snapshot,), "current task state file")
    return issue


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


def begin_remote_action(
    repo_root: Path,
    grant: approval.ApprovalGrant,
) -> tuple[approval.RemoteActionReservation | None, dict[str, object] | None]:
    if grant.source != "local-review":
        return None, None
    reservation = approval.reserve_remote_action(repo_root, grant)
    if reservation.provider_required:
        return reservation, None
    return reservation, provider_receipt_from_reservation(reservation)


def provider_receipt_from_reservation(
    reservation: approval.RemoteActionReservation,
) -> dict[str, object]:
    receipt = json.loads(reservation.provider_receipt)
    if not isinstance(receipt, dict):
        raise ValueError("confirmed provider receipt must be a mapping")
    return receipt


def begin_remote_action_with_immediate_completion(
    repo_root: Path,
    grant: approval.ApprovalGrant,
) -> tuple[approval.RemoteActionReservation | None, dict[str, object] | None]:
    reservation, receipt = begin_remote_action(repo_root, grant)
    if receipt is not None and reservation is not None:
        approval.complete_remote_action(repo_root, reservation)
    return reservation, receipt


def finish_remote_action(
    repo_root: Path,
    grant: approval.ApprovalGrant,
    reservation: approval.RemoteActionReservation | None,
    *,
    target_issue: str | None,
    provider_receipt: dict[str, object],
) -> None:
    if reservation is None:
        approval.record_consumed_approval(repo_root, grant, "success", target_issue=target_issue)
        return
    confirmed = approval.confirm_remote_action(
        repo_root,
        reservation,
        target_issue=target_issue,
        provider_receipt=provider_receipt,
    )
    approval.complete_remote_action(repo_root, confirmed)


def mark_remote_outcome_unknown(
    repo_root: Path,
    reservation: approval.RemoteActionReservation | None,
    exc: BaseException,
) -> None:
    if reservation is None:
        return
    try:
        approval.mark_remote_action_unknown(repo_root, reservation, str(exc))
    except ValueError:
        pass


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
        reservation, recovered = begin_remote_action_with_immediate_completion(ctx.repo_root, grant)
        if recovered is None:
            provider_body = reservation.approved_text() if reservation is not None else body
            try:
                result = providers.comment_issue(ctx.repo_root, issue_id, provider_body, os.environ)
            except BaseException as exc:
                mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
                raise
            finish_remote_action(
                ctx.repo_root,
                grant,
                reservation,
                target_issue=None,
                provider_receipt={"html_url": str(result.get("html_url", "")), "issue": issue_id},
            )
        else:
            result = recovered
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
        reservation, recovered = begin_remote_action_with_immediate_completion(ctx.repo_root, grant)
        if recovered is None:
            try:
                result = providers.close_issue(ctx.repo_root, issue_id, os.environ)
            except BaseException as exc:
                mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
                raise
            finish_remote_action(
                ctx.repo_root,
                grant,
                reservation,
                target_issue=None,
                provider_receipt={"issue": issue_id, "state": str(result.get("state", "closed"))},
            )
        else:
            result = recovered
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
    reservation, recovered = begin_remote_action_with_immediate_completion(ctx.repo_root, grant)
    if recovered is None:
        provider_body = reservation.approved_text() if reservation is not None else body
        try:
            result = providers.create_issue(ctx.repo_root, args.title, provider_body, args.labels, os.environ)
        except BaseException as exc:
            mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
            raise
        created_issue = normalized_issue(result.number)
        finish_remote_action(
            ctx.repo_root,
            grant,
            reservation,
            target_issue=created_issue,
            provider_receipt={"html_url": result.html_url, "number": created_issue},
        )
    else:
        created_issue = normalized_issue(str(recovered["number"]))
        result = providers.IssueResult(created_issue, str(recovered.get("html_url", "")))
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
        env=git_child_environment(repo_root),
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
        env=git_child_environment(repo_root),
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_succeeds(repo_root: Path, args: list[str]) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=git_child_environment(repo_root),
    ).returncode == 0


def current_branch(repo_root: Path) -> str:
    branch = git_output(repo_root, ["branch", "--show-current"])
    if not branch:
        raise ValueError("cannot determine current git branch")
    return branch


def remote_branch_tip(repo_root: Path, remote: str, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    lines = [line.split() for line in git_run(repo_root, ["ls-remote", "--heads", remote, ref]).splitlines()]
    matches = [fields for fields in lines if len(fields) == 2 and fields[1] == ref]
    if len(matches) != 1:
        raise ValueError(f"cannot resolve exactly one remote base branch: {remote}/{branch}")
    commit = matches[0][0].lower()
    if len(commit) not in {40, 64} or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError(f"invalid remote base commit for {remote}/{branch}")
    return commit


def validate_task_branch_remote_base(
    repo_root: Path,
    base: str,
    sealed_commit: str,
) -> str:
    current_tip = remote_branch_tip(repo_root, "origin", base)
    try:
        git_run(repo_root, ["fetch", "--no-tags", "origin", f"refs/heads/{base}"])
    except ValueError as exc:
        raise ValueError(
            "cannot fetch the current remote base to validate the sealed remote base"
        ) from exc
    fetched_tip = git_output(repo_root, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"])
    if fetched_tip != current_tip:
        raise ValueError("fetched current remote base does not match its advertised tip")
    if not git_succeeds(repo_root, ["rev-parse", "--verify", f"{sealed_commit}^{{commit}}"]):
        raise ValueError("sealed remote base is unreachable from the current remote base")
    if not git_succeeds(repo_root, ["merge-base", "--is-ancestor", sealed_commit, current_tip]):
        raise ValueError("sealed remote base is no longer contained by the current remote base")
    return current_tip


def synchronize_base_to_commit(repo_root: Path, base: str, sealed_commit: str) -> None:
    if current_branch(repo_root) != base:
        raise ValueError(f"exact base synchronization requires active base branch {base}")
    current_commit = git_output(repo_root, ["rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"])
    if current_commit == sealed_commit:
        return
    if not current_commit:
        raise ValueError(f"cannot resolve local base branch {base}")
    git_run(repo_root, ["fetch", "--no-tags", "origin", sealed_commit])
    fetched_commit = git_output(repo_root, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"])
    if fetched_commit != sealed_commit:
        raise ValueError("fetched base commit does not match the sealed remote tip")
    if not git_succeeds(repo_root, ["merge-base", "--is-ancestor", current_commit, sealed_commit]):
        raise ValueError("local base cannot fast-forward to the sealed remote tip")
    git_run(repo_root, ["merge", "--ff-only", sealed_commit])
    synchronized = git_output(repo_root, ["rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"])
    if synchronized != sealed_commit:
        raise ValueError("local base did not synchronize to the sealed remote tip")


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


def set_branch_meta_exact(repo_root: Path, key: str, value: str) -> None:
    expected = normalized_issue(value) if key == "issue" else value
    existing = branch_meta(repo_root, key)
    if existing and existing != expected:
        raise ValueError(f"conflicting task branch metadata for {key}: expected {expected}, found {existing}")
    if not existing:
        set_branch_meta(repo_root, key, expected)


def _document_pr_identity(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", ""
    numbers: list[str] = []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("PR:"):
            numbers.append(line.partition(":")[2].strip())
        elif line.startswith("PR URL:"):
            urls.append(line.partition(":")[2].strip())
    if len(numbers) > 1 or len(urls) > 1:
        raise ValueError(f"duplicate PR identity fields in {path}")
    return (numbers[0] if numbers else "", urls[0] if urls else "")


def _require_matching_pr_identity(label: str, actual_number: str, actual_url: str, number: str, url: str) -> None:
    if actual_number and normalized_issue(actual_number) != normalized_issue(number):
        raise ValueError(f"conflicting {label} PR identity: expected {number}, got {actual_number}")
    if actual_url and actual_url != url:
        raise ValueError(f"conflicting {label} PR URL: expected {url or 'none'}, got {actual_url}")


def apply_pr_local_metadata(
    repo_root: Path,
    issue: str,
    number: str,
    url: str,
) -> tuple[Path, list[Path]]:
    _require_matching_pr_identity(
        "branch metadata",
        branch_meta(repo_root, "pr"),
        branch_meta(repo_root, "pr-url"),
        number,
        url,
    )
    suggestion = repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}" / "state-update-suggestion.md"
    suggestion_number, suggestion_url = _document_pr_identity(suggestion)
    _require_matching_pr_identity("state suggestion", suggestion_number, suggestion_url, number, url)
    current_task = repo_root / ".xflow" / "current-task.md"
    task_number, task_url = _document_pr_identity(current_task)
    _require_matching_pr_identity("current task", task_number, task_url, number, url)

    set_branch_meta(repo_root, "pr", number)
    if url:
        set_branch_meta(repo_root, "pr-url", url)
    written_suggestion = write_pr_state_update_suggestion(repo_root, issue, number, url)
    update_current_task_for_pr(repo_root, issue, number, url)
    current_task_paths = [current_task] if current_task.is_file() else []
    return written_suggestion, current_task_paths


def branch_meta(repo_root: Path, key: str) -> str:
    value = git_output(repo_root, ["config", "--worktree", "--get", f"devctl.{key}"])
    return normalized_issue(value) if key == "issue" and value else value


def unset_branch_meta(repo_root: Path, key: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "--worktree", "--unset-all", f"devctl.{key}"],
        check=False,
        env=git_child_environment(repo_root),
    )


def default_base(repo_root: Path) -> str:
    env_base = os.environ.get("DEVCTL_BASE_BRANCH")
    if env_base:
        return env_base
    if git_succeeds(repo_root, ["show-ref", "--verify", "--quiet", "refs/heads/master"]):
        return "master"
    if git_succeeds(repo_root, ["show-ref", "--verify", "--quiet", "refs/heads/main"]):
        return "main"
    origin_head = git_output(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if origin_head.startswith("origin/"):
        return origin_head[len("origin/") :]
    return "main"


def require_clean_worktree(repo_root: Path) -> None:
    if not git_succeeds(repo_root, ["diff", "--quiet"]):
        raise ValueError("worktree has unstaged changes; commit or stash them first")
    if not git_succeeds(repo_root, ["diff", "--cached", "--quiet"]):
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
        env=git_child_environment(repo_root),
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
        env=git_child_environment(repo_root),
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
    expected_url = pr_url or ""
    actual_number, actual_url = _document_pr_identity(path)
    _require_matching_pr_identity("current task", actual_number, actual_url, pr_number, expected_url)
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
    headings = [index for index, line in enumerate(lines) if line.strip() == "## Remote Review"]
    if len(headings) > 1:
        raise ValueError("current task contains duplicate Remote Review sections")
    if not headings:
        url_line = f"PR URL: {pr_url}\n" if pr_url else ""
        lines.extend(["", "## Remote Review", f"PR: {pr_number}"])
        if url_line:
            lines.append(url_line.rstrip("\n"))
    else:
        section_start = headings[0]
        section_end = next(
            (index for index in range(section_start + 1, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
        section = lines[section_start + 1 : section_end]
        pr_offset = next((index for index, line in enumerate(section) if line.startswith("PR:")), None)
        if pr_offset is None:
            section.insert(0, f"PR: {pr_number}")
            pr_offset = 0
        if pr_url and not any(line.startswith("PR URL:") for line in section):
            section.insert(pr_offset + 1, f"PR URL: {pr_url}")
        lines[section_start + 1 : section_end] = section
    updated = "\n".join(lines).rstrip() + "\n"
    if updated != text:
        write_text_lf(path, updated)
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
    if not staged_after.issubset(expected_paths):
        raise ValueError(
            "PR state backfill staged paths must stay within metadata paths; "
            f"allowed {sorted(expected_paths)}, found {sorted(staged_after)}"
        )
    message = (
        f"chore(xflow): 回填合并请求状态[#{normalized_issue(issue)}]\n\n"
        "- 记录合并请求编号与远端链接\n"
        "- 同步当前任务状态文件"
    )
    check_commit_message(message, branch_issue=issue)
    if staged_after:
        git_run(repo_root, ["commit", "-m", message])
        return push_branch(repo_root, branch)

    tracked = all(git_succeeds(repo_root, ["ls-files", "--error-unmatch", relative]) for relative in expected_paths)
    clean_paths = git_succeeds(repo_root, ["diff", "--quiet", "HEAD", "--", *sorted(expected_paths)])
    committed_paths = {
        line
        for line in git_run(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]).splitlines()
        if line
    }
    committed_message = git_run(repo_root, ["log", "-1", "--pretty=%B"]).strip()
    if (
        not expected_paths
        or not tracked
        or not clean_paths
        or not committed_paths
        or not committed_paths.issubset(expected_paths)
        or committed_message != message
    ):
        raise ValueError("PR state backfill has no verifiable committed metadata effect")
    upstream = git_output(repo_root, ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    if not upstream:
        raise ValueError("PR state backfill recovery requires an existing upstream")
    ahead = int(git_output(repo_root, ["rev-list", "--count", f"{upstream}..HEAD"]) or "0")
    behind = int(git_output(repo_root, ["rev-list", "--count", f"HEAD..{upstream}"]) or "0")
    if behind or ahead > 1:
        raise ValueError("PR state backfill recovery found an unexpected upstream history")
    if ahead == 1:
        return push_branch(repo_root, branch)
    if git_output(repo_root, ["rev-parse", "HEAD"]) != git_output(repo_root, ["rev-parse", upstream]):
        raise ValueError("PR state backfill recovery requires HEAD to match its upstream")
    return PushResult(performed=True, success=True)


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
    reservation, recovered = begin_remote_action_with_immediate_completion(ctx.repo_root, grant)
    if recovered is not None:
        push_result = PushResult(performed=True, success=True)
    else:
        try:
            push_result = push_branch(ctx.repo_root, branch)
        except BaseException as exc:
            mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
            raise
    if push_result.performed and push_result.success:
        if recovered is None:
            finish_remote_action(
                ctx.repo_root,
                grant,
                reservation,
                target_issue=None,
                provider_receipt={"branch": branch, "result": "pushed"},
            )
        print(f"[INFO] pushed {branch}")
    else:
        if reservation is not None:
            approval.mark_remote_action_retryable(ctx.repo_root, reservation, "git push was skipped before remote effect")
        print(f"[INFO] push skipped for {branch}")
    return 0


def run_git_start(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    base = args.base or default_base(ctx.repo_root)
    branch = branch_name_from_slug(args.slug, args.issue)
    branch_reservation: approval.TaskBranchStartReservation | None = None
    issue = normalized_issue(args.issue) if args.issue else None
    state_path = task_state_file(ctx.repo_root, issue) if issue else None
    if state_path is not None and state_path.is_file():
        state = parse_task_state(state_path, binding_mode="recorded")
        classification = check_classification(ctx.repo_root, issue)
        if state.classification != classification.classification:
            raise ValueError("task-state Classification does not match canonical classification")
        if state.classification == "capability-change":
            if args.file is None:
                raise ValueError("first capability task branch requires --file with canonical task-state.md")
            allowed_prefix = f".xflow/issues/issue-{issue}/"
            unexpected = [path for path in changed_paths(ctx.repo_root) if not path.startswith(allowed_prefix)]
            if unexpected:
                raise ValueError(
                    "task branch identity step may only change the matching Issue workspace; "
                    f"found {unexpected}"
                )
            branch_reservation = approval.resume_task_branch_start(
                ctx.repo_root, issue, args.file, branch, base
            )
            if branch_reservation is None:
                branch_grant = approval.require_task_branch_start(
                    ctx.repo_root,
                    issue,
                    args.file,
                    branch,
                    base,
                )
                branch_reservation = approval.reserve_task_branch_start(
                    ctx.repo_root,
                    branch_grant,
                    base,
                )
        else:
            require_clean_worktree(ctx.repo_root)
    else:
        require_clean_worktree(ctx.repo_root)
    current = current_branch(ctx.repo_root)
    if branch_reservation is not None:
        if branch_reservation.base_commit == "pending":
            if current != base:
                raise ValueError(f"task branch reservation requires active base branch {base}")
            remote_tip = remote_branch_tip(ctx.repo_root, "origin", base)
            branch_reservation = approval.bind_task_branch_base(
                ctx.repo_root,
                branch_reservation,
                remote_tip,
            )
        target_commit = git_output(ctx.repo_root, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"])
        if not target_commit:
            validate_task_branch_remote_base(
                ctx.repo_root,
                base,
                branch_reservation.base_commit,
            )
        if current == base:
            print(f"[INFO] synchronize {base} to {branch_reservation.base_commit}")
            synchronize_base_to_commit(ctx.repo_root, base, branch_reservation.base_commit)
        if target_commit:
            if target_commit != branch_reservation.base_commit:
                raise ValueError("task branch exact start point mismatch")
            branch_reservation = approval.revalidate_task_branch_start(ctx.repo_root, branch_reservation)
            if current != branch:
                print(f"[INFO] checkout {branch}")
                git_run(ctx.repo_root, ["checkout", branch])
            branch_reservation = approval.mark_task_branch_created(ctx.repo_root, branch_reservation)
        else:
            if branch_reservation.state != "reserved":
                raise ValueError("task branch claim requires an existing exact target branch")
            if current != base:
                raise ValueError(f"task branch creation requires active base branch {base}")
            base_commit = git_output(ctx.repo_root, ["rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"])
            if base_commit != branch_reservation.base_commit:
                raise ValueError("task branch exact base commit changed before creation")
            branch_reservation = approval.revalidate_task_branch_start(ctx.repo_root, branch_reservation)
            validate_task_branch_remote_base(
                ctx.repo_root,
                base,
                branch_reservation.base_commit,
            )
            print(f"[INFO] create branch {branch}")
            git_run(ctx.repo_root, ["checkout", "-b", branch, branch_reservation.base_commit])
            branch_reservation = approval.mark_task_branch_created(ctx.repo_root, branch_reservation)
    else:
        if current != base:
            print(f"[INFO] checkout {base}")
            git_run(ctx.repo_root, ["checkout", base])
        print(f"[INFO] pull origin/{base}")
        git_run(ctx.repo_root, ["pull", "--ff-only", "origin", base])
        if git_succeeds(ctx.repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]):
            raise ValueError(f"branch already exists: {branch}")
        print(f"[INFO] create branch {branch}")
        git_run(ctx.repo_root, ["checkout", "-b", branch])
    set_meta = set_branch_meta_exact if branch_reservation is not None else set_branch_meta
    set_meta(ctx.repo_root, "slug", args.slug)
    if args.issue:
        set_meta(ctx.repo_root, "issue", args.issue)
    set_meta(ctx.repo_root, "base", base)
    unattended.disable(ctx.repo_root)
    if branch_reservation is not None:
        assert state_path is not None
        assert issue is not None
        activate_task_from_snapshot(ctx.repo_root, issue, branch_reservation.task_state_bytes)
        branch_reservation = approval.mark_task_branch_activated(ctx.repo_root, branch_reservation)
        approval.complete_task_branch_start(ctx.repo_root, branch_reservation)
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
        not git_succeeds(ctx.repo_root, ["diff", "--quiet"])
        or not git_succeeds(ctx.repo_root, ["diff", "--cached", "--quiet"])
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
        issue = branch_meta(ctx.repo_root, "issue")
        if approval.task_binding_evidence_exists(ctx.repo_root):
            approval.check_reviewed_task_binding(ctx.repo_root, issue, "commit")
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
    if git_succeeds(ctx.repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]):
        delete_flag = "-D" if args.force else "-d"
        git_run(ctx.repo_root, ["branch", delete_flag, branch])
        print(f"[INFO] deleted local branch {branch}")
    for key in ("slug", "issue", "base", "pr", "pr-url"):
        unset_branch_meta(ctx.repo_root, key)
    unattended.disable(ctx.repo_root)
    print("[INFO] done")
    return 0


def _run_git(ctx: RuntimeContext, args: argparse.Namespace) -> int:
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
        reservation, recovered = begin_remote_action_with_immediate_completion(ctx.repo_root, grant)
        if recovered is None:
            try:
                result = providers.merge_pull_request(
                    ctx.repo_root,
                    requested_pr,
                    args.method,
                    args.commit_title,
                    args.commit_message,
                    os.environ,
                )
            except BaseException as exc:
                mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
                raise
            finish_remote_action(
                ctx.repo_root,
                grant,
                reservation,
                target_issue=None,
                provider_receipt={
                    "message": str(result.get("message", "")),
                    "number": requested_pr,
                    "sha": str(result.get("sha", "")),
                },
            )
        else:
            result = recovered
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
    branch = current_branch(ctx.repo_root)
    reservation = approval.resume_pending_remote_action(ctx.repo_root, "git-mr", body_file, issue)
    if reservation is not None:
        grant = reservation.grant
        recovered = provider_receipt_from_reservation(reservation)
    else:
        if not body_file.is_file():
            raise ValueError(f"body file does not exist: {body_file}")
        check_current_task(ctx.repo_root, issue)
        check_mr_draft(body_file)
        attachment.ensure_publishable(ctx.repo_root, body_file, args.attachments, issue if args.attachments else None)
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
        if os.environ.get("DEVCTL_SKIP_PROVIDER_LOAD") == "1":
            print("[INFO] git-mr gate passed; provider skipped")
            return 0
        reservation, recovered = begin_remote_action(ctx.repo_root, grant)
    if recovered is None:
        base = args.base or branch_meta(ctx.repo_root, "base") or default_base(ctx.repo_root)
        title = args.title or f"[#{issue}] {branch.replace('-', ' ')}"
        provider_body = reservation.approved_text() if reservation is not None else body_file.read_text(encoding="utf-8")
        try:
            result = providers.create_pull_request(ctx.repo_root, title, provider_body, branch, base, os.environ)
        except BaseException as exc:
            mark_remote_outcome_unknown(ctx.repo_root, reservation, exc)
            raise
        if reservation is None:
            confirmed = None
        else:
            confirmed = approval.confirm_remote_action(
                ctx.repo_root,
                reservation,
                target_issue=None,
                provider_receipt={"html_url": result.html_url, "number": result.number},
            )
    else:
        result = providers.PullRequestResult(str(recovered["number"]), str(recovered.get("html_url", "")))
        confirmed = reservation
    suggestion, current_task_paths = apply_pr_local_metadata(
        ctx.repo_root,
        issue,
        result.number,
        result.html_url,
    )
    backfill_paths = [suggestion, *current_task_paths]
    backfill_pushed = commit_and_push_pr_backfill(ctx.repo_root, branch, backfill_paths, result.number, issue)
    if backfill_pushed is None or not backfill_pushed.performed or not backfill_pushed.success:
        raise ValueError("PR state backfill is not confirmed; remote approval remains pending")
    if confirmed is None:
        approval.record_consumed_approval(ctx.repo_root, grant, "success")
    else:
        approval.publish_remote_action_history(ctx.repo_root, confirmed)
    approval.record_subordinate_effect(
        ctx.repo_root,
        grant,
        "git-state-backfill",
        "success",
        idempotent=True,
    )
    if confirmed is not None:
        ready = approval.mark_remote_post_effects_complete(ctx.repo_root, confirmed)
        approval.complete_remote_action(ctx.repo_root, ready)
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


def run_git(args: argparse.Namespace) -> int:
    ctx = context()
    mutates_local_git = (
        args.git_command in {"start", "done", "mr"}
        or (args.git_command == "commit-msg" and (args.all or args.commit))
    )
    if mutates_local_git:
        with repository_mutation(ctx.repo_root):
            return _run_git(ctx, args)
    return _run_git(ctx, args)


def run_approval(args: argparse.Namespace) -> int:
    ctx = context()
    if args.approval_command == "supersede-branch-start":
        with repository_mutation(ctx.repo_root):
            path = approval.supersede_task_branch_start_by_id(
                ctx.repo_root,
                args.issue,
                args.approval_id,
                reason=args.reason,
                confirmation=args.confirm,
            )
        print(f"[INFO] task branch approval claim superseded: {path}")
        return 0
    if args.approval_command == "reconcile":
        provider_receipt: dict[str, object] | None = None
        if args.outcome == "no-effect":
            if args.target_issue or args.provider_receipt:
                raise ValueError("no-effect reconciliation does not accept provider outcome fields")
        else:
            if not args.provider_receipt:
                raise ValueError("success reconciliation requires --provider-receipt JSON")
            try:
                decoded_receipt = json.loads(args.provider_receipt)
            except json.JSONDecodeError as exc:
                raise ValueError("--provider-receipt must be valid JSON") from exc
            if not isinstance(decoded_receipt, dict) or not decoded_receipt:
                raise ValueError("--provider-receipt must be a non-empty JSON object")
            provider_receipt = decoded_receipt
        result = approval.reconcile_remote_action_by_id(
            ctx.repo_root,
            args.issue,
            args.approval_id,
            outcome=args.outcome,
            confirmation=args.confirm,
            target_issue=args.target_issue,
            provider_receipt=provider_receipt,
        )
        print(f"[INFO] remote approval reconciled: {result}")
        return 0
    if args.approval_command != "prepare":
        raise ValueError(f"unknown approval subcommand: {args.approval_command}")
    accepted_objects: tuple[str, ...] | None = None
    if args.action == "contract-acceptance":
        if not args.objects:
            raise ValueError("--objects is required for contract-acceptance")
        accepted_objects = approval.normalize_accepted_objects(tuple(args.objects.split(",")))
        contract = load_contract(ctx.repo_root, args.file)
        missing = [identifier for identifier in accepted_objects if identifier not in contract.objects_by_id]
        if missing:
            raise ValueError(f"accepted contract object does not exist: {missing[0]}")
    elif args.objects:
        raise ValueError("--objects is only valid for contract-acceptance")
    path = approval.prepare(
        ctx.repo_root,
        args.issue,
        args.action,
        args.file,
        args.suggested_command,
        args.reviewer,
        args.force,
        args.attachments,
        accepted_objects,
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


def _command_scope(args: argparse.Namespace) -> tuple[str, ...]:
    command = str(args.command or "")
    subcommand = getattr(args, f"{command.replace('-', '_')}_command", None)
    return (command, str(subcommand)) if subcommand else (command,)


def _cockpit_runtime() -> tuple[CockpitProfile, CockpitContext]:
    if _ACTIVE_COCKPIT_PROFILE is None or _ACTIVE_COCKPIT_CONTEXT is None:
        raise ValueError("cockpit runtime was not initialized")
    return _ACTIVE_COCKPIT_PROFILE, _ACTIVE_COCKPIT_CONTEXT


def _run_cockpit(args: argparse.Namespace) -> int:
    profile, cockpit_context = _cockpit_runtime()
    if args.command == "state":
        return execute_state(profile, cockpit_context, _state_provider_args(profile, args))
    if args.command == "dev":
        if args.dev_command == "preflight":
            return run_cockpit_preflight(profile, cockpit_context, args.warn_only)
        if args.dev_command == "docker":
            return run_cockpit_docker(profile, cockpit_context, args.docker_command)
        if args.dev_command == "all":
            preflight_result = run_cockpit_preflight(profile, cockpit_context, False)
            if preflight_result != 0:
                return preflight_result
            return run_scenario(profile, cockpit_context, "run")
        if args.dev_command == "playground":
            return run_playground(profile, cockpit_context, args.target, args.open_browser)
    if args.command == "run":
        return run_scenario(profile, cockpit_context, "run")
    if args.command in {"pg", "playground"}:
        return run_playground(profile, cockpit_context, args.target, args.open_browser)
    supported = ", ".join(SUPPORTED_COCKPIT_COMMANDS)
    raise ValueError(f"unsupported cockpit command: {args.command}; supported: {supported}")


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "preflight":
        return run_preflight()
    if args.command in {"state", "dev", "run", "pg", "playground"}:
        return _run_cockpit(args)
    if args.command == "check":
        return run_check(args)
    if args.command == "task":
        return run_task(args)
    if args.command == "hook":
        return run_hook(args)
    if args.command == "contract":
        return run_contract(args)
    if args.command == "gap":
        return run_gap(args)
    if args.command == "trace":
        return run_trace(args)
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


def main(argv: list[str] | None = None) -> int:
    global _ACTIVE_REPO_ROOT, _ACTIVE_COCKPIT_PROFILE, _ACTIVE_COCKPIT_CONTEXT
    _ACTIVE_REPO_ROOT = None
    _ACTIVE_COCKPIT_PROFILE = None
    _ACTIVE_COCKPIT_CONTEXT = None
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_global_argv(raw_argv))
    host_env_keys = {key for key, value in os.environ.items() if value}
    runtime_requested = (
        _cockpit_command_requested(args)
        or getattr(args, "profile", None) is not None
        or getattr(args, "cockpit_root", None) is not None
        or getattr(args, "repo", None) is not None
    )
    target_preserve_keys = set(host_env_keys)
    if runtime_requested:
        load_env_files(
            os.environ,
            include_project=False,
        )
        explicit_env_file = os.environ.get("XFLOW_ENV_FILE", "").strip()
        if explicit_env_file:
            explicit_path = canonical_path(Path(explicit_env_file))
            if explicit_path.is_file():
                target_preserve_keys.update(parse_env_file(explicit_path))
    else:
        load_env_files(os.environ)
    try:
        profile, cockpit_context = _prepare_runtime(args)
        if cockpit_context is not None:
            load_target_env_files(
                os.environ,
                cockpit_context.repo_root,
                preserve_keys=target_preserve_keys,
            )
            runtime_env = dict(os.environ)
            runtime_env["DEVCTL_REPO_ROOT"] = str(cockpit_context.repo_root)
            cockpit_context = replace(cockpit_context, env=runtime_env)
        _ACTIVE_COCKPIT_PROFILE = profile
        _ACTIVE_COCKPIT_CONTEXT = cockpit_context
        if cockpit_context is not None:
            _ACTIVE_REPO_ROOT = cockpit_context.repo_root
        if inherited_lease_present():
            with inherited_lease_command(context().repo_root, _command_scope(args)):
                return _dispatch(args, parser)
        return _dispatch(args, parser)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        _ACTIVE_REPO_ROOT = None
        _ACTIVE_COCKPIT_PROFILE = None
        _ACTIVE_COCKPIT_CONTEXT = None
