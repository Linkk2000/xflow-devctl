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

    raise ValueError("cannot resolve repository owner/repo; set DEVCTL_OWNER and DEVCTL_REPO or configure origin")


def resolve_platform(repo_root: Path, env: Mapping[str, str]) -> str:
    configured = env.get("XFLOW_PLATFORM", "").strip().lower()
    if configured:
        return configured
    remote = git_remote_url(repo_root)
    if "gitee.com" in remote:
        return "gitee"
    if "github.com" in remote:
        return "github"
    return "github"


def resolve_github_token(env: Mapping[str, str]) -> str:
    for name in ("GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_PRIVATE_TOKEN", "access_token"):
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing GitHub token: set GITHUB_TOKEN")


def resolve_gitee_token(env: Mapping[str, str]) -> str:
    for name in ("GITEE_TOKEN", "GITEE_ACCESS_TOKEN", "GITEE_PRIVATE_TOKEN", "access_token"):
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing Gitee token: set GITEE_TOKEN")


def github_api_base(env: Mapping[str, str]) -> str:
    return env.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def gitee_api_base(env: Mapping[str, str]) -> str:
    return env.get("GITEE_API_BASE", "https://gitee.com/api/v5").rstrip("/")


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xflow-devctl",
    }


def gitee_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "xflow-devctl",
    }


def github_repo_api_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, repo = resolve_owner_repo(repo_root, env)
    return f"{github_api_base(env)}/repos/{owner}/{repo}/{suffix.lstrip('/')}"


def gitee_repo_api_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, repo = resolve_owner_repo(repo_root, env)
    return f"{gitee_api_base(env)}/repos/{owner}/{repo}/{suffix.lstrip('/')}"


def gitee_owner_api_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, _repo = resolve_owner_repo(repo_root, env)
    return f"{gitee_api_base(env)}/repos/{owner}/{suffix.lstrip('/')}"


def request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
    api_name: str = "GitHub",
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
        raise ValueError(f"{api_name} API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"{api_name} API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def request_form_json(
    method: str,
    url: str,
    payload: Mapping[str, object],
    api_name: str = "Gitee",
) -> dict[str, object] | list[dict[str, object]]:
    body = urlencode({key: str(value) for key, value in payload.items() if value is not None}).encode("utf-8")
    request = Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", "xflow-devctl")
    try:
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"{api_name} API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"{api_name} API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def require_object(response: object, message: str) -> dict[str, object]:
    if not isinstance(response, dict):
        raise ValueError(message)
    return response


def post_json(url: str, headers: Mapping[str, str], payload: Mapping[str, object]) -> dict[str, object]:
    response = request_json("POST", url, headers, payload)
    return require_object(response, "GitHub API response must be a JSON object")


def gitee_query(env: Mapping[str, str], values: Mapping[str, object]) -> str:
    payload: dict[str, object] = {"access_token": resolve_gitee_token(env)}
    payload.update({key: value for key, value in values.items() if value not in ("", None)})
    return urlencode(payload)


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


def create_gitee_issue(
    repo_root: Path,
    title: str,
    body: str,
    labels: str | None,
    env: Mapping[str, str],
) -> IssueCreateResult:
    _owner, repo = resolve_owner_repo(repo_root, env)
    response = request_form_json(
        "POST",
        gitee_owner_api_url(repo_root, env, "issues"),
        {
            "access_token": resolve_gitee_token(env),
            "repo": repo,
            "title": title,
            "body": body,
            "labels": labels or "",
        },
    )
    item = require_object(response, "Gitee issue create response must be a JSON object")
    number = str(item.get("number", "")).strip()
    html_url = str(item.get("html_url", "")).strip()
    if not number:
        raise ValueError("Gitee issue create response missing number")
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


def create_gitee_pull_request(
    repo_root: Path,
    title: str,
    body: str,
    head: str,
    base: str,
    env: Mapping[str, str],
) -> PullRequestCreateResult:
    response = request_form_json(
        "POST",
        gitee_repo_api_url(repo_root, env, "pulls"),
        {
            "access_token": resolve_gitee_token(env),
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        },
    )
    item = require_object(response, "Gitee pull request create response must be a JSON object")
    number = str(item.get("number", "")).strip()
    html_url = str(item.get("html_url", "")).strip()
    if not number:
        raise ValueError("Gitee pull request create response missing number")
    return PullRequestCreateResult(number=number, html_url=html_url)


def list_github_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    token = resolve_github_token(env)
    query = urlencode({"state": state, "per_page": str(limit), "sort": "updated"})
    response = request_json("GET", f"{github_repo_api_url(repo_root, env, 'issues')}?{query}", github_headers(token))
    if not isinstance(response, list):
        raise ValueError("GitHub issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict) and not item.get("pull_request")]


def list_gitee_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    values: dict[str, object] = {"per_page": str(limit), "sort": "updated"}
    if state != "all":
        values["state"] = state
    response = request_json(
        "GET",
        f"{gitee_repo_api_url(repo_root, env, 'issues')}?{gitee_query(env, values)}",
        gitee_headers(),
        api_name="Gitee",
    )
    if not isinstance(response, list):
        raise ValueError("Gitee issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict)]


def show_github_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json("GET", github_repo_api_url(repo_root, env, f"issues/{number}"), github_headers(token))
    return require_object(response, "GitHub issue show response must be a JSON object")


def show_gitee_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_json(
        "GET",
        f"{gitee_repo_api_url(repo_root, env, f'issues/{number}')}?{gitee_query(env, {})}",
        gitee_headers(),
        api_name="Gitee",
    )
    return require_object(response, "Gitee issue show response must be a JSON object")


def comment_github_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = post_json(
        github_repo_api_url(repo_root, env, f"issues/{number}/comments"),
        github_headers(token),
        {"body": body},
    )
    return response


def comment_gitee_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_form_json(
        "POST",
        gitee_repo_api_url(repo_root, env, f"issues/{number}/comments"),
        {"access_token": resolve_gitee_token(env), "body": body},
    )
    return require_object(response, "Gitee issue comment response must be a JSON object")


def close_github_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json(
        "PATCH",
        github_repo_api_url(repo_root, env, f"issues/{number}"),
        github_headers(token),
        {"state": "closed"},
    )
    return require_object(response, "GitHub issue close response must be a JSON object")


def close_gitee_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    _owner, repo = resolve_owner_repo(repo_root, env)
    response = request_form_json(
        "PATCH",
        gitee_owner_api_url(repo_root, env, f"issues/{number}"),
        {"access_token": resolve_gitee_token(env), "repo": repo, "state": "closed"},
    )
    return require_object(response, "Gitee issue close response must be a JSON object")


def get_github_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    token = resolve_github_token(env)
    response = request_json("GET", github_repo_api_url(repo_root, env, f"pulls/{number}"), github_headers(token))
    return require_object(response, "GitHub pull request response must be a JSON object")


def get_gitee_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_json(
        "GET",
        f"{gitee_repo_api_url(repo_root, env, f'pulls/{number}')}?{gitee_query(env, {})}",
        gitee_headers(),
        api_name="Gitee",
    )
    return require_object(response, "Gitee pull request response must be a JSON object")


def create_issue(
    repo_root: Path,
    title: str,
    body: str,
    labels: str | None,
    env: Mapping[str, str],
) -> IssueCreateResult:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return create_gitee_issue(repo_root, title, body, labels, env)
    if platform == "github":
        return create_github_issue(repo_root, title, body, labels, env)
    raise ValueError(f"Python issue provider is not available for platform: {platform}")


def list_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return list_gitee_issues(repo_root, state, limit, env)
    if platform == "github":
        return list_github_issues(repo_root, state, limit, env)
    raise ValueError(f"Python issue provider is not available for platform: {platform}")


def show_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return show_gitee_issue(repo_root, number, env)
    if platform == "github":
        return show_github_issue(repo_root, number, env)
    raise ValueError(f"Python issue provider is not available for platform: {platform}")


def comment_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return comment_gitee_issue(repo_root, number, body, env)
    if platform == "github":
        return comment_github_issue(repo_root, number, body, env)
    raise ValueError(f"Python issue provider is not available for platform: {platform}")


def close_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return close_gitee_issue(repo_root, number, env)
    if platform == "github":
        return close_github_issue(repo_root, number, env)
    raise ValueError(f"Python issue provider is not available for platform: {platform}")


def create_pull_request(
    repo_root: Path,
    title: str,
    body: str,
    head: str,
    base: str,
    env: Mapping[str, str],
) -> PullRequestCreateResult:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return create_gitee_pull_request(repo_root, title, body, head, base, env)
    if platform == "github":
        return create_github_pull_request(repo_root, title, body, head, base, env)
    raise ValueError(f"Python pull request provider is not available for platform: {platform}")


def get_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    platform = resolve_platform(repo_root, env)
    if platform == "gitee":
        return get_gitee_pull_request(repo_root, number, env)
    if platform == "github":
        return get_github_pull_request(repo_root, number, env)
    raise ValueError(f"Python pull request provider is not available for platform: {platform}")
