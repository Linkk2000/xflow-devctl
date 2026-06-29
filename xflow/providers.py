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
    raise ValueError("cannot resolve repository owner/repo; set DEVCTL_OWNER and DEVCTL_REPO or configure origin")


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


def gitee_token(env: Mapping[str, str]) -> str:
    for name in ("GITEE_TOKEN", "GITEE_ACCESS_TOKEN", "access_token"):
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing Gitee token: set GITEE_TOKEN")


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


def gitee_api_base(env: Mapping[str, str]) -> str:
    return env.get("GITEE_API_BASE", "https://gitee.com/api/v5").rstrip("/")


def gitee_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "xflow-devctl",
    }


def gitee_repo_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, repo = owner_repo(repo_root, env)
    return f"{gitee_api_base(env)}/repos/{owner}/{repo}/{suffix.lstrip('/')}"


def gitee_owner_url(repo_root: Path, env: Mapping[str, str], suffix: str) -> str:
    owner, _repo = owner_repo(repo_root, env)
    return f"{gitee_api_base(env)}/repos/{owner}/{suffix.lstrip('/')}"


def request_json(
    method: str,
    url: str,
    request_headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
    api_name: str = "GitHub",
):
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
        raise ValueError(f"{api_name} API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"{api_name} API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def request_json_or_none_on_404(
    method: str,
    url: str,
    request_headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
    api_name: str = "GitHub",
):
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
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"{api_name} API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"{api_name} API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def request_binary_json(method: str, url: str, request_headers: Mapping[str, str], body: bytes, content_type: str):
    request = Request(url, data=body, method=method)
    for key, value in request_headers.items():
        request.add_header(key, value)
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    try:
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub upload API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError(f"GitHub upload API request failed: {exc.reason}") from exc
    return json.loads(text) if text else {}


def request_form_json(method: str, url: str, payload: Mapping[str, object], api_name: str = "Gitee"):
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


def ensure_github(repo_root: Path, env: Mapping[str, str]) -> None:
    if platform(repo_root, env) != "github":
        raise ValueError("Python provider currently supports GitHub only")


def github_release_by_tag(repo_root: Path, tag: str, env: Mapping[str, str]) -> dict[str, object] | None:
    ensure_github(repo_root, env)
    response = request_json_or_none_on_404("GET", api_url(repo_root, env, f"releases/tags/{tag}"), headers(token(env)))
    if response is None:
        return None
    return require_object(response, "GitHub release response must be a JSON object")


def create_github_release(repo_root: Path, tag: str, env: Mapping[str, str]) -> dict[str, object]:
    ensure_github(repo_root, env)
    response = request_json(
        "POST",
        api_url(repo_root, env, "releases"),
        headers(token(env)),
        {
            "tag_name": tag,
            "name": tag,
            "body": "XFlow uploaded issue and review attachments.",
            "draft": False,
            "prerelease": False,
        },
    )
    return require_object(response, "GitHub release create response must be a JSON object")


def ensure_github_release(repo_root: Path, tag: str, env: Mapping[str, str]) -> dict[str, object]:
    release = github_release_by_tag(repo_root, tag, env)
    return release if release is not None else create_github_release(repo_root, tag, env)


def upload_github_release_asset(
    repo_root: Path,
    upload_url: str,
    name: str,
    label: str,
    content_type: str,
    body: bytes,
    env: Mapping[str, str],
) -> dict[str, object]:
    base = upload_url.split("{", 1)[0]
    separator = "&" if "?" in base else "?"
    url = f"{base}{separator}{urlencode({'name': name, 'label': label})}"
    response = request_binary_json("POST", url, headers(token(env)), body, content_type)
    return require_object(response, "GitHub release asset upload response must be a JSON object")


def gitee_query(env: Mapping[str, str], values: Mapping[str, object]) -> str:
    payload = {"access_token": gitee_token(env)}
    payload.update({key: value for key, value in values.items() if value not in ("", None)})
    return urlencode(payload)


def require_object(response: object, message: str) -> dict[str, object]:
    if not isinstance(response, dict):
        raise ValueError(message)
    return response


def create_gitee_issue(repo_root: Path, title: str, body: str, labels: str | None, env: Mapping[str, str]) -> IssueResult:
    _owner, repo = owner_repo(repo_root, env)
    response = request_form_json(
        "POST",
        gitee_owner_url(repo_root, env, "issues"),
        {
            "access_token": gitee_token(env),
            "repo": repo,
            "title": title,
            "body": body,
            "labels": labels or "",
        },
    )
    item = require_object(response, "Gitee issue create response must be a JSON object")
    if not item.get("number"):
        raise ValueError("Gitee issue create response missing number")
    return IssueResult(str(item.get("number")), str(item.get("html_url", "")))


def list_gitee_issues(repo_root: Path, state: str, limit: int, env: Mapping[str, str]) -> list[dict[str, object]]:
    values: dict[str, object] = {"per_page": str(limit), "sort": "updated"}
    if state != "all":
        values["state"] = state
    response = request_json(
        "GET",
        f"{gitee_repo_url(repo_root, env, 'issues')}?{gitee_query(env, values)}",
        gitee_headers(),
        api_name="Gitee",
    )
    if not isinstance(response, list):
        raise ValueError("Gitee issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict)]


def show_gitee_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_json(
        "GET",
        f"{gitee_repo_url(repo_root, env, f'issues/{number}')}?{gitee_query(env, {})}",
        gitee_headers(),
        api_name="Gitee",
    )
    return require_object(response, "Gitee issue show response must be a JSON object")


def comment_gitee_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_form_json(
        "POST",
        gitee_repo_url(repo_root, env, f"issues/{number}/comments"),
        {"access_token": gitee_token(env), "body": body},
    )
    return require_object(response, "Gitee issue comment response must be a JSON object")


def close_gitee_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    _owner, repo = owner_repo(repo_root, env)
    response = request_form_json(
        "PATCH",
        gitee_owner_url(repo_root, env, f"issues/{number}"),
        {"access_token": gitee_token(env), "repo": repo, "state": "closed"},
    )
    return require_object(response, "Gitee issue close response must be a JSON object")


def create_gitee_pull_request(
    repo_root: Path,
    title: str,
    body: str,
    head: str,
    base: str,
    env: Mapping[str, str],
) -> PullRequestResult:
    response = request_form_json(
        "POST",
        gitee_repo_url(repo_root, env, "pulls"),
        {
            "access_token": gitee_token(env),
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        },
    )
    item = require_object(response, "Gitee pull request create response must be a JSON object")
    if not item.get("number"):
        raise ValueError("Gitee pull request create response missing number")
    return PullRequestResult(str(item.get("number")), str(item.get("html_url", "")))


def get_gitee_pull_request(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    response = request_json(
        "GET",
        f"{gitee_repo_url(repo_root, env, f'pulls/{number}')}?{gitee_query(env, {})}",
        gitee_headers(),
        api_name="Gitee",
    )
    return require_object(response, "Gitee pull request response must be a JSON object")


def create_issue(repo_root: Path, title: str, body: str, labels: str | None, env: Mapping[str, str]) -> IssueResult:
    if platform(repo_root, env) == "gitee":
        return create_gitee_issue(repo_root, title, body, labels, env)
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
    if platform(repo_root, env) == "gitee":
        return list_gitee_issues(repo_root, state, limit, env)
    ensure_github(repo_root, env)
    query = urlencode({"state": state, "per_page": str(limit), "sort": "updated"})
    response = request_json("GET", f"{api_url(repo_root, env, 'issues')}?{query}", headers(token(env)))
    if not isinstance(response, list):
        raise ValueError("GitHub issue list response must be a JSON array")
    return [item for item in response if isinstance(item, dict) and not item.get("pull_request")]


def show_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    if platform(repo_root, env) == "gitee":
        return show_gitee_issue(repo_root, number, env)
    ensure_github(repo_root, env)
    response = request_json("GET", api_url(repo_root, env, f"issues/{number}"), headers(token(env)))
    if not isinstance(response, dict):
        raise ValueError("GitHub issue show response must be a JSON object")
    return response


def comment_issue(repo_root: Path, number: str, body: str, env: Mapping[str, str]) -> dict[str, object]:
    if platform(repo_root, env) == "gitee":
        return comment_gitee_issue(repo_root, number, body, env)
    ensure_github(repo_root, env)
    response = request_json("POST", api_url(repo_root, env, f"issues/{number}/comments"), headers(token(env)), {"body": body})
    if not isinstance(response, dict):
        raise ValueError("GitHub issue comment response must be a JSON object")
    return response


def close_issue(repo_root: Path, number: str, env: Mapping[str, str]) -> dict[str, object]:
    if platform(repo_root, env) == "gitee":
        return close_gitee_issue(repo_root, number, env)
    ensure_github(repo_root, env)
    response = request_json("PATCH", api_url(repo_root, env, f"issues/{number}"), headers(token(env)), {"state": "closed"})
    if not isinstance(response, dict):
        raise ValueError("GitHub issue close response must be a JSON object")
    return response


def create_pull_request(repo_root: Path, title: str, body: str, head: str, base: str, env: Mapping[str, str]) -> PullRequestResult:
    if platform(repo_root, env) == "gitee":
        return create_gitee_pull_request(repo_root, title, body, head, base, env)
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
    if platform(repo_root, env) == "gitee":
        return get_gitee_pull_request(repo_root, number, env)
    ensure_github(repo_root, env)
    response = request_json("GET", api_url(repo_root, env, f"pulls/{number}"), headers(token(env)))
    if not isinstance(response, dict):
        raise ValueError("GitHub pull request response must be a JSON object")
    return response
