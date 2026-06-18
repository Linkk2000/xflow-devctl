from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .io import read_text_strict
from .paths import default_approval_file as approval_file_for_issue


UMBRELLA_ACTIONS = {"remote-write", "remote write", "all-remote-writes"}
LOCAL_REVIEW_REQUIRED_TEXT = [
    "# Local Review Approval",
    "Issue:",
    "Reviewer:",
    "Approved At:",
    "Approved Action:",
    "Approved File:",
    "Approved SHA256:",
    "## Decision",
]
PLACEHOLDER_MARKERS = ("<", "TODO", "TBD")


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"missing approved artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_field(text: str, field: str) -> str:
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise ValueError(f"missing required text '{prefix}'")


def reject_placeholder_field(text: str, field: str) -> None:
    value = parse_field(text, field)
    upper = value.upper()
    if any(marker in value for marker in ("<", ">")) or "TODO" in upper or "TBD" in upper:
        raise ValueError(f"placeholder remains in approval field: {field}")


def default_approval_file(repo_root: Path, issue: str) -> Path:
    return approval_file_for_issue(repo_root, issue)


def read_approval_text(repo_root: Path, issue: str, approved_file: Path) -> str:
    review_file = default_approval_file(repo_root, issue)
    if not review_file.is_file():
        raise ValueError(f"academic local approval required: {review_file}")
    return read_text_strict(review_file)


def require_local_review_shape(text: str) -> None:
    for needle in LOCAL_REVIEW_REQUIRED_TEXT:
        if needle not in text:
            raise ValueError(f"missing required text '{needle}'")


def resolve_approved_path(repo_root: Path, approved_file: Path) -> Path:
    if approved_file.is_absolute():
        return approved_file.resolve()
    return (repo_root / approved_file).resolve()


def normalize_approved_path(repo_root: Path, approved_file: Path) -> str:
    resolved = resolve_approved_path(repo_root, approved_file)
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def suggested_command_for(action: str, approved_file: Path, issue: str) -> str:
    path = approved_file.as_posix()
    if action == "issue-create":
        return f'devctl issue create "<title>" --body-file {path}'
    if action == "issue-comment":
        return f"devctl issue comment {issue} --body-file {path}"
    if action == "issue-close":
        return f"devctl issue close {issue}"
    if action == "git-mr":
        return f'devctl git mr --title "<title>" --body-file {path} --issue {issue}'
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


def prepare_local_review_file(
    repo_root: Path,
    issue: str,
    action: str,
    approved_file: Path,
    command: str | None = None,
    reviewer: str | None = None,
    force: bool = False,
) -> Path:
    repo_root = repo_root.resolve()
    approved_path = resolve_approved_path(repo_root, approved_file)
    if not approved_path.is_file():
        raise ValueError(f"missing approved artifact: {approved_path}")

    review_file = default_approval_file(repo_root, issue)
    if review_file.exists() and not force:
        existing = read_text_strict(review_file)
        if "Approved: yes" in existing:
            raise ValueError(f"refusing to overwrite approved local review: {review_file}")

    relative_file = normalize_approved_path(repo_root, approved_path)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    digest = sha256_file(approved_path).lower()
    suggested = command or suggested_command_for(action, Path(relative_file), issue)
    reviewer = reviewer or default_reviewer(repo_root)
    text = (
        "# Local Review Approval\n\n"
        f"Issue: {issue}\n"
        f"Reviewer: {reviewer}\n"
        f"Approved At: {now}\n"
        f"Approved Action: {action}\n"
        f"Approved File: {relative_file}\n"
        f"Approved SHA256: {digest}\n\n"
        "## Decision\n"
        "Approved: no\n\n"
        "## Suggested Command\n"
        f"Suggested Command: {suggested}\n\n"
        "## Expected Effect\n"
        f"- Authorizes exactly one `{action}` action for `{relative_file}` after the human changes `Approved: no` to `Approved: yes`.\n"
        "- If the approved file changes, run `devctl approval prepare` again before any remote write.\n\n"
        "## Notes\n"
        "- Human reviewer must inspect the approved file, the action, and the suggested command before approving.\n"
    )
    review_file.parent.mkdir(parents=True, exist_ok=True)
    review_file.write_text(text, encoding="utf-8")
    return review_file


def approved_file_matches(repo_root: Path, actual_file: Path, approved_file_text: str) -> bool:
    actual = resolve_approved_path(repo_root, actual_file)
    declared = resolve_approved_path(repo_root, Path(approved_file_text))
    return declared == actual


def check_local_review_file(repo_root: Path, issue: str, approved_file: Path) -> None:
    text = read_approval_text(repo_root, issue, approved_file)
    require_local_review_shape(text)
    for field in ("Reviewer", "Approved At", "Approved Action", "Approved File", "Approved SHA256", "Approved"):
        reject_placeholder_field(text, field)
    if parse_field(text, "Approved").lower() != "yes":
        raise ValueError("academic local approval required: Approved: yes")

    approved_file_text = parse_field(text, "Approved File")
    if not approved_file_matches(repo_root, approved_file, approved_file_text):
        raise ValueError(f"approved file mismatch: expected {normalize_approved_path(repo_root, approved_file)}")

    expected = parse_field(text, "Approved SHA256").lower()
    actual = sha256_file(resolve_approved_path(repo_root, approved_file))
    if expected != actual:
        raise ValueError(f"hash mismatch for {approved_file}: expected {expected}, actual {actual}")


def require_remote_approval(repo_root: Path, action: str, approved_file: Path, issue: str) -> None:
    text = read_approval_text(repo_root, issue, approved_file)
    approved_action = parse_field(text, "Approved Action")
    if approved_action != action and approved_action.lower() not in UMBRELLA_ACTIONS:
        raise ValueError(f"action mismatch: expected {action}, got {approved_action}")
    check_local_review_file(repo_root, issue, approved_file)
