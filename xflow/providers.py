from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class IssueCreateResult:
    number: str
    html_url: str


@dataclass(frozen=True)
class PullRequestCreateResult:
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


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xflow-devctl",
    }


def github_repo_api_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, repo = resolve_owner_repo(repo_root, env)
    return f"{github_api_base(env)}/repos/{owner}/{repo}/{suffix.lstrip('/')}"


def request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
) -> dict[str, object] | list[dict[str, object]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    if payload is not None:
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


def post_json(url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> dict[str, object]:
    response = request_json("POST", url, headers, payload)
    if not isinstance(response, dict):
        raise ValueError("GitHub API response must be a JSON object")
    return response


def create_github_issue(
    repo_root: Path,
    title: str,
    body: str,
    labels: str | None,
    env: Mapping[str, str],
) -> IssueCreateResult:
    token = resolve_github_token(env)
    url = github_repo_api_url(repo_root, env, "issues")
    response = post_json(
        url,
        github_headers(token),
        {"title": title, "body": body, "labels": split_labels(labels)},
    )
    number = str(response.get("number", "")).strip()
    html_url = str(response.get("html_url", "")).strip()
    if not number:
        raise ValueError("GitHub issue create response missing number")
    return IssueCreateResult(number=number, html_url=html_url)


def create_github_pull_request(
    repo_root: Path,
    title: str,
    body: str,
    head: str,
    base: str,
    env: Mapping[str, str],
) -> PullRequestCreateResult:
    token = resolve_github_token(env)
    url = github_repo_api_url(repo_root, env, "pulls")
    response = post_json(
        url,
        github_headers(token),
        {"title": title, "body": body, "head": head, "base": base},
    )
    number = str(response.get("number", "")).strip()
    html_url = str(response.get("html_url", "")).strip()
    if not number:
        raise ValueError("GitHub pull request create response missing number")
    return PullRequestCreateResult(number=number, html_url=html_url)


def list_github_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    token = resolve_github_token(env)
    query = urlencode({"state": state, "per_page": str(limit), "sort": "updated"})
    response = request_json("GET", f"{github_repo_api_url(repo_root, env, 'issues')}?{query}", github_headers(token))
    if not isinstance(response, list):
        raise ValueError("GitHub issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict) and not item.get("pull_request")]


def show_github_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json("GET", github_repo_api_url(repo_root, env, f"issues/{number}"), github_headers(token))
    if not isinstance(response, dict):
        raise ValueError("GitHub issue show response must be a JSON object")
    return response


def comment_github_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = post_json(
        github_repo_api_url(repo_root, env, f"issues/{number}/comments"),
        github_headers(token),
        {"body": body},
    )
    return response


def close_github_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json(
        "PATCH",
        github_repo_api_url(repo_root, env, f"issues/{number}"),
        github_headers(token),
        {"state": "closed"},
    )
    if not isinstance(response, dict):
        raise ValueError("GitHub issue close response must be a JSON object")
    return response


def get_github_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json("GET", github_repo_api_url(repo_root, env, f"pulls/{number}"), github_headers(token))
    if not isinstance(response, dict):
        raise ValueError("GitHub pull request response must be a JSON object")
    return response


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


def list_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python issue provider is not available for platform: {platform}")
    return list_github_issues(repo_root, state, limit, env)


def show_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python issue provider is not available for platform: {platform}")
    return show_github_issue(repo_root, number, env)


def comment_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python issue provider is not available for platform: {platform}")
    return comment_github_issue(repo_root, number, body, env)


def close_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python issue provider is not available for platform: {platform}")
    return close_github_issue(repo_root, number, env)


def create_pull_request(
    repo_root: Path,
    title: str,
    body: str,
    head: str,
    base: str,
    env: Mapping[str, str],
) -> PullRequestCreateResult:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python pull request provider is not available for platform: {platform}")
    return create_github_pull_request(repo_root, title, body, head, base, env)


def get_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform != "github":
        raise ValueError(f"Python pull request provider is not available for platform: {platform}")
    return get_github_pull_request(repo_root, number, env)
