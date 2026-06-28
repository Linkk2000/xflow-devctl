from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .io import read_text


PLACEHOLDER_RE = re.compile(r"xflow-attachment://([A-Za-z0-9._-]+)")
DRIVE_PATH_RE = re.compile(r"(?i)(^|[\s\(\[\"'])[A-Z]:[\\/]")
POSIX_LOCAL_RE = re.compile(r"(^|[\s\(\[\"'])(/tmp/|/mnt/|/home/)")
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def normalized_issue(issue: str) -> str:
    issue = issue.strip()
    return issue[1:] if issue.startswith("#") else issue


def issue_dir(repo_root: Path, issue: str) -> Path:
    return repo_root / ".xflow" / "issues" / f"issue-{normalized_issue(issue)}"


def attachment_dir(repo_root: Path, issue: str) -> Path:
    return issue_dir(repo_root, issue) / "attachments"


def default_manifest(repo_root: Path, issue: str) -> Path:
    return attachment_dir(repo_root, issue) / "manifest.json"


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
    write_manifest(manifest_path, data)
    return manifest_path


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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    return output


def ensure_publishable(repo_root: Path, body_file: Path, manifest_path: Path | None = None, issue: str | None = None) -> None:
    text = read_text(resolve_repo_path(repo_root, body_file))
    reject_publication_body(text)
    if manifest_path is not None:
        manifest = validate_manifest(repo_root, manifest_path, issue=issue, require_published=True)
        check_body(repo_root, manifest, body_file, final=True)
