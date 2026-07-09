from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from . import object_storage, providers
from .io import read_text
from .paths import normalized_issue


PLACEHOLDER_RE = re.compile(r"xflow-attachment://([A-Za-z0-9._-]+)")
DRIVE_PATH_RE = re.compile(r"(?i)(^|[\s\(\[\"'])[A-Z]:[\\/]")
POSIX_LOCAL_RE = re.compile(r"(^|[\s\(\[\"'])(/tmp/|/mnt/|/home/)")
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"


def attachment_dir(repo_root: Path, issue: str) -> Path:
    return issue_dir(repo_root, issue) / "attachments"


def default_manifest(repo_root: Path, issue: str) -> Path:
    return attachment_dir(repo_root, issue) / "manifest.json"


def publish_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / "publish" / "issues" / f"issue-{normalized_issue(issue)}"


def publish_attachment_dir(repo_root: Path, issue: str) -> Path:
    return publish_dir(repo_root, issue) / "attachments"


def published_manifest_path(repo_root: Path, issue: str, manifest_path: Path) -> Path:
    return publish_attachment_dir(repo_root, issue) / manifest_path.name


def default_rendered_body(repo_root: Path, issue: str, body_file: Path) -> Path:
    extension = body_file.suffix or ".md"
    return publish_dir(repo_root, issue) / f"{body_file.stem}.final{extension}"


def resolve_repo_path(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def display_path(repo_root: Path, path: Path) -> str:
    resolved = resolve_repo_path(repo_root, path)
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"missing attachment file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path, issue: str | None = None) -> dict[str, object]:
    if not path.is_file():
        if issue is None:
            raise ValueError(f"missing attachment manifest: {path}")
        return {"version": 1, "issue": normalized_issue(issue), "items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid attachment manifest JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("attachment manifest must be a JSON object")
    return data


def write_manifest(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_published_manifest(repo_root: Path, issue: str, source_manifest: Path, data: dict[str, object]) -> Path:
    output = published_manifest_path(repo_root, issue, source_manifest)
    write_manifest(output, data)
    return output


def next_attachment_id(items: list[dict[str, object]]) -> str:
    highest = 0
    for item in items:
        raw = str(item.get("id", ""))
        match = re.fullmatch(r"att-(\d+)", raw)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"att-{highest + 1:03d}"


def unique_destination(files_dir: Path, filename: str, source_hash: str) -> Path:
    candidate = files_dir / filename
    if candidate.exists() and candidate.is_file() and sha256_file(candidate) == source_hash:
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while candidate.exists():
        candidate = files_dir / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def markdown_for(filename: str, placeholder: str, kind: str, mime: str) -> str:
    if kind == "image" or (kind == "auto" and mime.startswith("image/")):
        return f"![{filename}]({placeholder})"
    return f"[{filename}]({placeholder})"


def add_attachment(
    repo_root: Path,
    issue: str,
    file_path: Path,
    kind: str = "auto",
    attachment_id: str | None = None,
    manifest_path: Path | None = None,
) -> tuple[dict[str, object], Path]:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path or default_manifest(repo_root, issue))
    source = resolve_repo_path(repo_root, file_path)
    if not source.is_file():
        raise ValueError(f"attachment file does not exist: {source}")
    if kind not in {"auto", "image", "file"}:
        raise ValueError("--as must be one of: auto, image, file")

    data = load_manifest(manifest_path, issue)
    items = data.setdefault("items", [])
    if data.get("version") != 1:
        data["version"] = 1
    data["issue"] = normalized_issue(str(data.get("issue") or issue))
    if data["issue"] != normalized_issue(issue):
        raise ValueError(f"manifest issue mismatch: expected {issue}, found {data['issue']}")
    if not isinstance(items, list):
        raise ValueError("attachment manifest items must be a list")

    attachment_id = attachment_id or next_attachment_id([item for item in items if isinstance(item, dict)])
    if not ID_RE.fullmatch(attachment_id):
        raise ValueError(f"invalid attachment id: {attachment_id}")
    if any(isinstance(item, dict) and item.get("id") == attachment_id for item in items):
        raise ValueError(f"duplicate attachment id: {attachment_id}")

    digest = sha256_file(source)
    files_dir = manifest_path.parent / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(files_dir, source.name, digest)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)

    mime = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
    placeholder = f"xflow-attachment://{attachment_id}"
    item = {
        "id": attachment_id,
        "filename": destination.name,
        "localPath": display_path(repo_root, destination),
        "sha256": digest,
        "mime": mime,
        "size": destination.stat().st_size,
        "placeholder": placeholder,
        "markdown": markdown_for(destination.name, placeholder, kind, mime),
        "publishedUrl": None,
    }
    items.append(item)
    write_manifest(manifest_path, data)
    return item, manifest_path


def require_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"published attachment URL must be http(s): {url}")


def is_image_item(item: dict[str, object]) -> bool:
    mime = str(item.get("mime", "")).lower()
    markdown = str(item.get("markdown", "")).lstrip()
    return mime.startswith("image/") or markdown.startswith("![")


def is_issue_image_publishable(item: dict[str, object]) -> bool:
    return item.get("backend") == "aliyun-oss" and bool(item.get("publishedUrl"))


def validate_manifest(
    repo_root: Path,
    manifest_path: Path,
    issue: str | None = None,
    require_published: bool = False,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path)
    data = load_manifest(manifest_path)
    if data.get("version") != 1:
        raise ValueError("attachment manifest version must be 1")
    if issue is not None and str(data.get("issue", "")) != normalized_issue(issue):
        raise ValueError(f"manifest issue mismatch: expected {issue}, found {data.get('issue') or '<missing>'}")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("attachment manifest items must be a list")

    seen: set[str] = set()
    for index, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"attachment item #{index} must be an object")
        for key in ("id", "filename", "localPath", "sha256", "mime", "size", "placeholder", "markdown"):
            if key not in raw:
                raise ValueError(f"attachment item #{index} missing required field: {key}")
        item_id = str(raw["id"])
        if not ID_RE.fullmatch(item_id):
            raise ValueError(f"invalid attachment id: {item_id}")
        if item_id in seen:
            raise ValueError(f"duplicate attachment id: {item_id}")
        seen.add(item_id)
        expected_placeholder = f"xflow-attachment://{item_id}"
        if raw["placeholder"] != expected_placeholder:
            raise ValueError(f"placeholder mismatch for {item_id}")
        if expected_placeholder not in str(raw["markdown"]):
            raise ValueError(f"markdown for {item_id} must contain its placeholder")

        local_path = resolve_repo_path(repo_root, Path(str(raw["localPath"])))
        if not local_path.is_file():
            raise ValueError(f"attachment file does not exist: {raw['localPath']}")
        if str(raw["sha256"]).lower() != sha256_file(local_path).lower():
            raise ValueError(f"sha256 mismatch for attachment {item_id}")
        if int(raw["size"]) != local_path.stat().st_size:
            raise ValueError(f"size mismatch for attachment {item_id}")
        if not str(raw["mime"]):
            raise ValueError(f"missing MIME type for attachment {item_id}")

        published = raw.get("publishedUrl")
        if published:
            require_url(str(published))
        elif require_published:
            raise ValueError(f"attachment {item_id} has no publishedUrl")
    return data


def placeholder_ids(text: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(text))


def reject_publication_body(text: str) -> None:
    if "xflow-attachment://" in text:
        raise ValueError("remote body contains unresolved xflow-attachment:// placeholder")
    if "file://" in text:
        raise ValueError("remote body contains file:// local path")
    if DRIVE_PATH_RE.search(text):
        raise ValueError("remote body contains Windows local path")
    if POSIX_LOCAL_RE.search(text):
        raise ValueError("remote body contains POSIX local path")
    if ".xflow/" in text or ".xflow\\" in text:
        raise ValueError("remote body contains .xflow local path")


def check_body(repo_root: Path, manifest: dict[str, object], body_file: Path, final: bool = False) -> None:
    text = read_text(resolve_repo_path(repo_root, body_file))
    items = manifest.get("items")
    known = {str(item["id"]) for item in items if isinstance(item, dict)}
    unknown = sorted(placeholder_ids(text) - known)
    if unknown:
        raise ValueError(f"body references unknown attachment placeholders: {', '.join(unknown)}")
    if final:
        reject_publication_body(text)


def check_attachment(
    repo_root: Path,
    issue: str,
    manifest_path: Path,
    body_file: Path | None = None,
    final: bool = False,
) -> dict[str, object]:
    manifest = validate_manifest(repo_root, manifest_path, issue=issue, require_published=final)
    if body_file is not None:
        check_body(repo_root, manifest, body_file, final=final)
    return manifest


def reject_issue_image_attachments(repo_root: Path, manifest_path: Path, issue: str) -> None:
    manifest = validate_manifest(repo_root, manifest_path, issue=issue, require_published=False)
    images: list[str] = []
    for raw in manifest.get("items", []):
        if not isinstance(raw, dict):
            continue
        if is_image_item(raw) and not is_issue_image_publishable(raw):
            images.append(str(raw.get("filename") or raw.get("id") or "image"))
    if images:
        raise ValueError(
            "issue/comment image attachments are disabled; "
            "keep images as local evidence until a supported GitHub issue-native attachment API is available: "
            + ", ".join(images)
        )


def parse_url_mapping(raw: str, single_id: str | None = None) -> tuple[str, str]:
    if "=" in raw:
        item_id, url = raw.split("=", 1)
        item_id = item_id.strip()
        url = url.strip()
    elif single_id:
        item_id = single_id
        url = raw.strip()
    else:
        raise ValueError("--url must use id=https://... when manifest has multiple items")
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f"invalid attachment id in --url: {item_id}")
    require_url(url)
    return item_id, url


def publish_urls(repo_root: Path, issue: str, manifest_path: Path, urls: list[str]) -> Path:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path)
    data = validate_manifest(repo_root, manifest_path, issue=issue)
    items = data["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("attachment manifest has no items to publish")
    if not urls:
        raise ValueError("attachment publish requires at least one --url")

    by_id = {str(item["id"]): item for item in items if isinstance(item, dict)}
    single_id = next(iter(by_id)) if len(by_id) == 1 else None
    for raw in urls:
        item_id, url = parse_url_mapping(raw, single_id)
        if item_id not in by_id:
            raise ValueError(f"unknown attachment id in --url: {item_id}")
        by_id[item_id]["publishedUrl"] = url
    return write_published_manifest(repo_root, issue, manifest_path, data)


def safe_asset_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "attachment"


def github_asset_name(issue: str, item: dict[str, object]) -> str:
    item_id = safe_asset_component(str(item["id"]))
    digest = str(item["sha256"])[:12]
    filename = safe_asset_component(str(item["filename"]))
    return f"xflow-{safe_asset_component(normalized_issue(issue))}-{item_id}-{digest}-{filename}"


def publish_github_release(
    repo_root: Path,
    issue: str,
    manifest_path: Path,
    env: Mapping[str, str],
    release_tag: str = "xflow-attachments",
) -> Path:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path)
    data = validate_manifest(repo_root, manifest_path, issue=issue)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("attachment manifest has no items to publish")

    release = providers.ensure_github_release(repo_root, release_tag, env)
    upload_url = str(release.get("upload_url", ""))
    if not upload_url:
        raise ValueError("GitHub release response missing upload_url")

    for raw in items:
        if not isinstance(raw, dict):
            continue
        if raw.get("publishedUrl"):
            continue
        local_path = resolve_repo_path(repo_root, Path(str(raw["localPath"])))
        asset_name = github_asset_name(issue, raw)
        response = providers.upload_github_release_asset(
            repo_root,
            upload_url,
            asset_name,
            str(raw["filename"]),
            str(raw["mime"]),
            local_path.read_bytes(),
            env,
        )
        published_url = str(response.get("browser_download_url", ""))
        if not published_url:
            raise ValueError(f"GitHub release asset response missing browser_download_url for {raw['id']}")
        raw["publishedUrl"] = published_url
        raw["backend"] = "github-release"
        raw["githubReleaseTag"] = release_tag
        raw["githubAssetName"] = asset_name
        if response.get("id") is not None:
            raw["githubAssetId"] = str(response.get("id"))
    return write_published_manifest(repo_root, issue, manifest_path, data)


def publish_aliyun_oss(
    repo_root: Path,
    issue: str,
    manifest_path: Path,
    env: Mapping[str, str],
) -> Path:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path)
    data = validate_manifest(repo_root, manifest_path, issue=issue)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("attachment manifest has no items to publish")

    prefix = env.get("ALIYUN_OSS_PREFIX", "xflow/issues")
    for raw in items:
        if not isinstance(raw, dict):
            continue
        if raw.get("publishedUrl") and raw.get("backend") == "aliyun-oss":
            continue
        local_path = resolve_repo_path(repo_root, Path(str(raw["localPath"])))
        key = object_storage.object_key(prefix, normalized_issue(issue), raw)
        url, bucket = object_storage.upload_aliyun_oss(local_path, key, str(raw["mime"]), env)
        raw["publishedUrl"] = url
        raw["backend"] = "aliyun-oss"
        raw["provider"] = "aliyun-oss"
        raw["bucket"] = bucket
        raw["objectKey"] = key
    return write_published_manifest(repo_root, issue, manifest_path, data)


def append_markdown_to_body(repo_root: Path, body_file: Path, markdown_items: list[str], output_path: Path) -> Path:
    body_path = resolve_repo_path(repo_root, body_file)
    text = read_text(body_path).rstrip()
    additions = [item for item in markdown_items if item and item not in text]
    output = resolve_repo_path(repo_root, output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if additions:
        bullet_lines = "\n".join(f"- {item}" for item in additions)
        text = f"{text}\n\n## Attachments\n{bullet_lines}\n"
    else:
        text = f"{text}\n"
    output.write_text(text, encoding="utf-8", newline="\n")
    return output


def render_body(repo_root: Path, issue: str, manifest_path: Path, input_path: Path, output_path: Path) -> Path:
    repo_root = repo_root.resolve()
    manifest_path = resolve_repo_path(repo_root, manifest_path)
    data = validate_manifest(repo_root, manifest_path, issue=issue, require_published=True)
    text = read_text(resolve_repo_path(repo_root, input_path))
    for raw in data["items"]:
        if not isinstance(raw, dict):
            continue
        text = text.replace(str(raw["placeholder"]), str(raw["publishedUrl"]))
    reject_publication_body(text)
    output = resolve_repo_path(repo_root, output_path)
    try:
        output.relative_to(issue_dir(repo_root, issue).resolve())
    except ValueError:
        pass
    else:
        raise ValueError("rendered remote bodies must stay outside .xflow/issues; use .xflow/publish/issues/issue-<id>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    return output


def ensure_publishable(repo_root: Path, body_file: Path, manifest_path: Path | None = None, issue: str | None = None) -> None:
    text = read_text(resolve_repo_path(repo_root, body_file))
    reject_publication_body(text)
    if manifest_path is not None:
        manifest = validate_manifest(repo_root, manifest_path, issue=issue, require_published=True)
        check_body(repo_root, manifest, body_file, final=True)
