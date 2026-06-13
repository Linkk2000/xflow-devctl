from __future__ import annotations

import hashlib
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


def approved_file_matches(repo_root: Path, actual_file: Path, approved_file_text: str) -> bool:
    actual = resolve_approved_path(repo_root, actual_file)
    declared = resolve_approved_path(repo_root, Path(approved_file_text))
    return declared == actual


def check_local_review_file(repo_root: Path, issue: str, approved_file: Path) -> None:
    text = read_approval_text(repo_root, issue, approved_file)
    require_local_review_shape(text)
    if parse_field(text, "Approved").lower() != "yes":
        raise ValueError("academic local approval required: Approved: yes")

    approved_file_text = parse_field(text, "Approved File")
    if not approved_file_matches(repo_root, approved_file, approved_file_text):
        raise ValueError(f"approved file mismatch: expected {normalize_approved_path(repo_root, approved_file)}")

    expected = parse_field(text, "Approved SHA256")
    actual = sha256_file(resolve_approved_path(repo_root, approved_file))
    if expected != actual:
        raise ValueError(f"hash mismatch for {approved_file}")


def require_remote_approval(repo_root: Path, action: str, approved_file: Path, issue: str) -> None:
    text = read_approval_text(repo_root, issue, approved_file)
    approved_action = parse_field(text, "Approved Action")
    if approved_action != action and approved_action.lower() not in UMBRELLA_ACTIONS:
        raise ValueError(f"action mismatch: expected {action}, got {approved_action}")
    check_local_review_file(repo_root, issue, approved_file)
