from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .io import read_text
from .paths import default_approval_file
from .unattended import require_active


UMBRELLA_ACTIONS = {"remote-write", "remote write", "all-remote-writes"}
UNATTENDED_ACTIONS = {
    "issue-create",
    "issue-comment",
    "issue-close",
    "git-push",
    "git-mr",
    "git-pr-merge",
    "git-state-backfill",
}
REQUIRED_TEXT = (
    "# Local Review Approval",
    "Issue:",
    "Reviewer:",
    "Approved At:",
    "Approved Action:",
    "Approved File:",
    "Approved SHA256:",
    "## Decision",
)


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"missing approved artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def field(text: str, name: str) -> str:
    prefix = f"{name}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise ValueError(f"missing required text '{prefix}'")


def reject_placeholder(text: str, name: str) -> None:
    value = field(text, name)
    upper = value.upper()
    if "<" in value or ">" in value or "TODO" in upper or "TBD" in upper:
        raise ValueError(f"placeholder remains in approval field: {name}")


def resolve_path(repo_root: Path, path: Path) -> Path:
    msys_path = resolve_msys_path(path)
    if msys_path is not None:
        return msys_path
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def resolve_msys_path(path: Path) -> Path | None:
    raw = path.as_posix()
    if os.name != "nt" or not raw.startswith("/") or raw.startswith("//"):
        return None
    result = subprocess.run(
        ["cygpath", "-w", raw],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    converted = result.stdout.strip()
    return Path(converted).resolve() if converted else None


def display_path(repo_root: Path, path: Path) -> str:
    resolved = resolve_path(repo_root, path)
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def suggested_command(action: str, approved_file: Path, issue: str, attachment_manifest: Path | None = None) -> str:
    path = approved_file.as_posix()
    attachments = f" --attachments {attachment_manifest.as_posix()}" if attachment_manifest else ""
    if action == "issue-create":
        return f'devctl issue create "<title>" --body-file {path}{attachments}'
    if action == "issue-comment":
        return f"devctl issue comment {issue} --body-file {path}{attachments}"
    if action == "issue-close":
        return f"devctl issue close {issue}"
    if action == "git-push":
        return f"devctl git push --issue {issue} --file {path}"
    if action == "git-mr":
        return f'devctl git mr --title "<title>" --body-file {path} --issue {issue}{attachments}'
    if action == "git-pr-merge":
        return f"devctl git pr-merge <number> --issue {issue} --file {path}"
    return f"devctl <remote-write-command> --body-file {path}"


def git_config(repo_root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", key],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def default_reviewer(repo_root: Path) -> str:
    name = git_config(repo_root, "user.name")
    email = git_config(repo_root, "user.email")
    if name and email:
        return f"{name} ({email})"
    if name:
        return name
    if email:
        return email
    return "human reviewer"


def prepare(
    repo_root: Path,
    issue: str,
    action: str,
    approved_file: Path,
    command: str | None = None,
    reviewer: str | None = None,
    force: bool = False,
    attachment_manifest: Path | None = None,
) -> Path:
    repo_root = repo_root.resolve()
    approved_path = resolve_path(repo_root, approved_file)
    if not approved_path.is_file():
        raise ValueError(f"missing approved artifact: {approved_path}")
    review_file = default_approval_file(repo_root, issue)
    if review_file.exists() and not force:
        existing = read_text(review_file)
        if "Approved: yes" in existing:
            raise ValueError(f"refusing to overwrite approved local review: {review_file}")

    relative_file = display_path(repo_root, approved_path)
    digest = sha256_file(approved_path).lower()
    attachment_text = ""
    relative_manifest: str | None = None
    if attachment_manifest is not None:
        manifest_path = resolve_path(repo_root, attachment_manifest)
        if not manifest_path.is_file():
            raise ValueError(f"missing attachment manifest: {manifest_path}")
        relative_manifest = display_path(repo_root, manifest_path)
        manifest_digest = sha256_file(manifest_path).lower()
        attachment_text = (
            f"Attachment Manifest: {relative_manifest}\n"
            f"Attachment Manifest SHA256: {manifest_digest}\n"
        )
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    command = command or suggested_command(action, Path(relative_file), issue, Path(relative_manifest) if relative_manifest else None)
    reviewer = reviewer or default_reviewer(repo_root)
    text = (
        "# Local Review Approval\n\n"
        f"Issue: {issue}\n"
        f"Reviewer: {reviewer}\n"
        f"Approved At: {now}\n"
        f"Approved Action: {action}\n"
        f"Approved File: {relative_file}\n"
        f"Approved SHA256: {digest}\n"
        f"{attachment_text}\n"
        "## Decision\n"
        "Approved: no\n\n"
        "## Human Gate\n"
        "Prepared by AI or tooling does not mean approved.\n"
        "Only the human reviewer may change Approved: no to Approved: yes.\n"
        "If this file was approved by the AI, the approval is invalid.\n\n"
        "## Suggested Command\n"
        f"Suggested Command: {command}\n\n"
        "## Expected Effect\n"
        f"- Authorizes exactly one `{action}` action for `{relative_file}` after human approval.\n"
        "- If the approved file changes, run `devctl approval prepare` again before remote write.\n"
    )
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(text, encoding="utf-8", newline="\n")
    return review_file


def check(repo_root: Path, issue: str, approved_file: Path, attachment_manifest: Path | None = None) -> None:
    repo_root = repo_root.resolve()
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    text = read_text(review_file)
    for needle in REQUIRED_TEXT:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}'")
    for name in ("Reviewer", "Approved At", "Approved Action", "Approved File", "Approved SHA256", "Approved"):
        reject_placeholder(text, name)
    if field(text, "Approved").lower() != "yes":
        raise ValueError("local approval required: Approved: yes")

    declared = resolve_path(repo_root, Path(field(text, "Approved File")))
    actual = resolve_path(repo_root, approved_file)
    if declared != actual:
        raise ValueError(f"approved file mismatch: expected {display_path(repo_root, actual)}")

    expected = field(text, "Approved SHA256").lower()
    actual_hash = sha256_file(actual).lower()
    if expected != actual_hash:
        raise ValueError(f"hash mismatch for {approved_file}: expected {expected}, actual {actual_hash}")

    if attachment_manifest is not None:
        declared_manifest = resolve_path(repo_root, Path(field(text, "Attachment Manifest")))
        actual_manifest = resolve_path(repo_root, attachment_manifest)
        if declared_manifest != actual_manifest:
            raise ValueError(f"attachment manifest mismatch: expected {display_path(repo_root, actual_manifest)}")
        expected_manifest = field(text, "Attachment Manifest SHA256").lower()
        actual_manifest_hash = sha256_file(actual_manifest).lower()
        if expected_manifest != actual_manifest_hash:
            raise ValueError(
                f"hash mismatch for attachment manifest {attachment_manifest}: "
                f"expected {expected_manifest}, actual {actual_manifest_hash}"
            )


def require_remote(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
) -> None:
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"local review approval required: {review_file}")
    text = read_text(review_file)
    approved_action = field(text, "Approved Action")
    if approved_action != action and approved_action.lower() not in UMBRELLA_ACTIONS:
        raise ValueError(f"action mismatch: expected {action}, got {approved_action}")
    check(repo_root, issue, approved_file, attachment_manifest)


def require_remote_or_unattended(
    repo_root: Path,
    action: str,
    approved_file: Path,
    issue: str,
    attachment_manifest: Path | None = None,
    request_unattended: bool = False,
) -> str:
    if action not in UNATTENDED_ACTIONS:
        raise ValueError(f"remote action {action} is not eligible for unattended mode")

    sha256_file(resolve_path(repo_root, approved_file))
    if attachment_manifest is not None:
        sha256_file(resolve_path(repo_root, attachment_manifest))

    try:
        state = require_active(repo_root, issue)
    except ValueError:
        if request_unattended:
            raise ValueError("--no-local-review requires active task-scoped unattended mode") from None
        require_remote(repo_root, action, approved_file, issue, attachment_manifest)
        return "local-review"

    print(f"[UNATTENDED] Human approval gate bypassed for current task {state.issue}.")
    return "unattended"
