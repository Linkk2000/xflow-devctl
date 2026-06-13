from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class IssueCreateResult:
    number: str
    html_url: str


def split_labels(labels: str | None) -> list[str]:
    if not labels:
        return []
    return [label.strip() for label in labels.split(",") if label.strip()]


def parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    patterns = (
        r"^git@[^:]+:([^/]+)/(.+?)(?:\.git)?$",
        r"^https?://[^/]+/([^/]+)/(.+?)(?:\.git)?$",
        r"^ssh://git@[^/]+/([^/]+)/(.+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url.strip())
        if match:
            return match.group(1), match.group(2)
    return None


def git_remote_url(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def resolve_owner_repo(repo_root: Path, env: Mapping[str, str]) -> tuple[str, str]:
    owner = env.get("DEVCTL_OWNER", "").strip()
    repo = env.get("DEVCTL_REPO", "").strip()
    if owner and repo:
        return owner, repo

    remote = git_remote_url(repo_root)
    parsed = parse_owner_repo(remote)
    if parsed:
        return parsed

    raise ValueError("cannot resolve GitHub owner/repo; set DEVCTL_OWNER and DEVCTL_REPO or configure origin")


def resolve_platform(repo_root: Path, env: Mapping[str, str]) -> str:
    configured = env.get("XFLOW_PLATFORM", "").strip().lower()
    if configured:
        return configured
    remote = git_remote_url(repo_root)
    if "github.com" in remote:
        return "github"
    if "gitee.com" in remote:
        return "gitee"
    return "github"


def resolve_github_token(env: Mapping[str, str]) -> str:
    for name in ("GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_TOKEN", "access_token"):
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing GitHub token: set GITHUB_TOKEN")


def github_api_base(env: Mapping[str, str]) -> str:
    return env.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def post_json(url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"GitHub API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def create_github_issue(
    repo_root: Path,
    title: str,
    body: str,
    labels: str | None,
    env: Mapping[str, str],
) -> IssueCreateResult:
    owner, repo = resolve_owner_repo(repo_root, env)
    token = resolve_github_token(env)
    url = f"{github_api_base(env)}/repos/{owner}/{repo}/issues"
    response = post_json(
        url,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "xflow-devctl",
        },
        {"title": title, "body": body, "labels": split_labels(labels)},
    )
    number = str(response.get("number", "")).strip()
    html_url = str(response.get("html_url", "")).strip()
    if not number:
        raise ValueError("GitHub issue create response missing number")
    return IssueCreateResult(number=number, html_url=html_url)


def create_issue(
    repo_root: Path,
    title: str,
    body: str,
    labels: str | None,
    env: Mapping[str, str],
) -> IssueCreateResult:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python issue provider is not available for platform: {platform}")
    return create_github_issue(repo_root, title, body, labels, env)
