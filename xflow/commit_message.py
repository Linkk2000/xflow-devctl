from __future__ import annotations

import re

from .paths import normalized_issue


COMMIT_TYPES = (
    "feat",
    "fix",
    "refactor",
    "perf",
    "test",
    "docs",
    "build",
    "ci",
    "chore",
    "revert",
    "style",
    "merge",
)
ISSUE_ID_PATTERN = r"[A-Za-z0-9._-]+"
SUBJECT_RE = re.compile(
    rf"^(?P<type>{'|'.join(COMMIT_TYPES)})"
    rf"\((?P<scope>[^()\r\n]+)\): "
    rf"(?P<summary>.+?)(?P<tags>(?:\[#{ISSUE_ID_PATTERN}\]){{1,2}})$"
)
ISSUE_TAG_RE = re.compile(rf"\[#({ISSUE_ID_PATTERN})\]")
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z]+")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\s]+")
URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>()]+")
POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![:/])/(?!/)[^\s`'\"<>]+")
UNC_OR_DEVICE_PATH_RE = re.compile(r"\\\\(?:[.?]\\|[^\\\s]+\\)[^\s`'\"<>]+")
AI_TRAILER_RE = re.compile(
    r"(?im)(?:^Co-authored-by:\s*(?:Cursor|Claude|Gemini)\b|^Generated-by:|OpenAI-Codex)"
)
PROVIDER_METADATA_RE = re.compile(r"(?im)^(?:GitHub|Gitee)-(?:Issue|PR|MR|Pull-Request)\s*:")


def _is_chinese_dominant(text: str) -> bool:
    chinese = len(HAN_RE.findall(text))
    latin_tokens = len(LATIN_TOKEN_RE.findall(text))
    return chinese > 0 and chinese >= latin_tokens


def check_commit_message(
    message: str,
    branch_issue: str | None = None,
) -> tuple[str, ...]:
    if AI_TRAILER_RE.search(message):
        raise ValueError("commit message must not contain an AI-client trailer")
    path_scan = URL_RE.sub("", message)
    if (
        WINDOWS_ABSOLUTE_PATH_RE.search(path_scan)
        or POSIX_ABSOLUTE_PATH_RE.search(path_scan)
        or UNC_OR_DEVICE_PATH_RE.search(path_scan)
    ):
        raise ValueError("commit message must not contain a local absolute path (absolute Windows path included)")
    if PROVIDER_METADATA_RE.search(message):
        raise ValueError("commit message must not contain provider-only metadata")

    lines = message.splitlines()
    subject = lines[0] if lines else ""
    match = SUBJECT_RE.fullmatch(subject)
    if not match:
        raise ValueError(
            "commit subject must use type(scope): Chinese-dominant summary[#Issue] with a non-empty scope and Issue tag"
        )
    if not match.group("scope").strip():
        raise ValueError("commit subject scope must be non-empty")

    summary = match.group("summary").strip()
    if "[#" in summary:
        raise ValueError("commit subject must contain one or two Issue IDs only in the suffix")
    if not _is_chinese_dominant(summary):
        raise ValueError("commit subject summary must be Chinese-dominant")
    raw_issue_ids = tuple(ISSUE_TAG_RE.findall(match.group("tags")))
    try:
        issue_ids = tuple(normalized_issue(issue_id) for issue_id in raw_issue_ids)
    except ValueError as exc:
        raise ValueError(f"commit subject Issue identifier is invalid: {exc}") from exc
    if len(issue_ids) == 2 and match.group("type") != "merge":
        raise ValueError("only merge commit subjects may contain two Issue IDs")
    if len(issue_ids) == 2 and issue_ids[0] == issue_ids[1]:
        raise ValueError("merge commit subjects require two distinct Issue IDs")
    if branch_issue is not None and issue_ids[0] != normalized_issue(branch_issue):
        raise ValueError(
            f"commit subject first Issue #{issue_ids[0]} does not match branch Issue #{normalized_issue(branch_issue)}"
        )

    if len(lines) < 2 or lines[1].strip():
        raise ValueError("commit message requires a blank separator after the subject")
    body_lines = [line.strip() for line in lines[2:] if line.strip()]
    if len(body_lines) < 2:
        raise ValueError("commit message body requires at least two non-empty Chinese-dominant lines")
    for line in body_lines:
        if not _is_chinese_dominant(line):
            raise ValueError(f"commit message body line must be Chinese-dominant: {line}")
    return issue_ids
