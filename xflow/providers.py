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
class IssueResult:
    number: str
    html_url: str


@dataclass(frozen=True)
class PullRequestResult:
    number: str
    html_url: str


def split_labels(labels: str | None) -> list[str]:
    return [label.strip() for label in (labels or "").split(",") if label.strip()]


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


def remote_url(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def owner_repo(repo_root: Path, env: Mapping[str, str]) -> tuple[str, str]:
    owner = env.get("DEVCTL_OWNER", "").strip()
    repo = env.get("DEVCTL_REPO", "").strip()
    if owner and repo:
        return owner, repo
    parsed = parse_owner_repo(remote_url(repo_root))
    if parsed:
        return parsed
    raise ValueError("cannot resolve GitHub owner/repo; set DEVCTL_OWNER and DEVCTL_REPO or configure origin")


def platform(repo_root: Path, env: Mapping[str, str]) -> str:
    configured = env.get("XFLOW_PLATFORM", "").strip().lower()
    if configured:
        return configured
    remote = remote_url(repo_root)
    if "gitee.com" in remote:
        return "gitee"
    return "github"


def token(env: Mapping[str, str]) -> str:
    for name in ("GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_TOKEN", "access_token"):
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing GitHub token: set GITHUB_TOKEN")


def headers(value: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {value}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xflow-devctl",
    }


def api_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, repo = owner_repo(repo_root, env)
    base = env.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    return f"{base}/repos/{owner}/{repo}/{suffix.lstrip('/')}"


def request_json(method: str, url: str, request_headers: Mapping[str, str], payload: Mapping[str, object] | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method)
    for key, value in request_headers.items():
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


def ensure_github(repo_root: Path, env: Mapping[str, str]) -> None:
    if platform(repo_root, env) != "github":
        raise ValueError("Python provider currently supports GitHub only")


def create_issue(repo_root: Path, title: str, body: str, labels: str | None, env: Mapping[str, str]) -> IssueResult:
    ensure_github(repo_root, env)
    response = request_json("POST", api_url(repo_root, env, "issues"), headers(token(env)), {
        "title": title,
        "body": body,
        "labels": split_labels(labels),
    })
    if not isinstance(response, dict) or not response.get("number"):
        raise ValueError("GitHub issue create response missing number")
    return IssueResult(str(response.get("number")), str(response.get("html_url", "")))


def list_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    ensure_github(repo_root, env)
    query = urlencode({"state": state, "per_page": str(limit), "sort": "updated"})
    response = request_json("GET", f"{api_url(repo_root, env, 'issues')}?{query}", headers(token(env)))
    if not isinstance(response, list):
        raise ValueError("GitHub issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict) and not item.get("pull_request")]


def show_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    ensure_github(repo_root, env)
    response = request_json("GET", api_url(repo_root, env, f"issues/{number}"), headers(token(env)))
    if not isinstance(response, dict):
        raise ValueError("GitHub issue show response must be a JSON object")
    return response


def comment_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    ensure_github(repo_root, env)
    response = request_json("POST", api_url(repo_root, env, f"issues/{number}/comments"), headers(token(env)), {"body": body})
    if not isinstance(response, dict):
        raise ValueError("GitHub issue comment response must be a JSON object")
    return response


def close_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    ensure_github(repo_root, env)
    response = request_json("PATCH", api_url(repo_root, env, f"issues/{number}"), headers(token(env)), {"state": "closed"})
    if not isinstance(response, dict):
        raise ValueError("GitHub issue close response must be a JSON object")
    return response


def create_pull_request(repo_root: Path, title: str, body: str, head: str, base: str, env: Mapping[str, str]) -> PullRequestResult:
    ensure_github(repo_root, env)
    response = request_json("POST", api_url(repo_root, env, "pulls"), headers(token(env)), {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    })
    if not isinstance(response, dict) or not response.get("number"):
        raise ValueError("GitHub pull request create response missing number")
    return PullRequestResult(str(response.get("number")), str(response.get("html_url", "")))


def get_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    ensure_github(repo_root, env)
    response = request_json("GET", api_url(repo_root, env, f"pulls/{number}"), headers(token(env)))
    if not isinstance(response, dict):
        raise ValueError("GitHub pull request response must be a JSON object")
    return response
