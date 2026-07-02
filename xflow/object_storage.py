from __future__ import annotations

import base64
import hashlib
import hmac
import re
from email.utils import formatdate
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def require_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"missing Aliyun OSS config: set {name}")
    return value


def optional_env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, "").strip() or default


def safe_object_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "attachment"


def object_key(prefix: str, issue: str, item: dict[str, object]) -> str:
    clean_prefix = prefix.strip().strip("/") or "xflow/issues"
    item_id = safe_object_component(str(item["id"]))
    digest = str(item["sha256"])[:12]
    filename = safe_object_component(str(item["filename"]))
    return f"{clean_prefix}/issue-{safe_object_component(issue)}/attachments/{item_id}-{digest}-{filename}"


def endpoint_host(bucket: str, endpoint: str) -> str:
    parsed = urlparse(endpoint)
    host = parsed.netloc or parsed.path
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        return host
    if host.startswith(f"{bucket}."):
        return host
    return f"{bucket}.{host}"


def upload_url(bucket: str, endpoint: str, key: str) -> str:
    parsed = urlparse(endpoint)
    scheme = parsed.scheme or "https"
    host = endpoint_host(bucket, endpoint)
    quoted_key = quote(key, safe="/")
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        return f"{scheme}://{host}/{bucket}/{quoted_key}"
    return f"{scheme}://{host}/{quoted_key}"


def public_url(env: Mapping[str, str], bucket: str, region: str, key: str) -> str:
    base = optional_env(env, "ALIYUN_OSS_PUBLIC_BASE_URL")
    if not base:
        custom_domain = optional_env(env, "ALIYUN_OSS_CUSTOM_DOMAIN")
        base = custom_domain or f"https://{bucket}.{region}.aliyuncs.com"
    return f"{base.rstrip('/')}/{quote(key, safe='/')}"


def canonical_resource(bucket: str, key: str) -> str:
    return f"/{bucket}/{key}"


def authorization(access_key_id: str, access_key_secret: str, method: str, content_md5: str, content_type: str, date: str, bucket: str, key: str) -> str:
    string_to_sign = "\n".join([method, content_md5, content_type, date, canonical_resource(bucket, key)])
    digest = hmac.new(access_key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return f"OSS {access_key_id}:{signature}"


def upload_aliyun_oss(local_path: Path, key: str, mime: str, env: Mapping[str, str]) -> tuple[str, str]:
    bucket = require_env(env, "ALIYUN_OSS_BUCKET")
    region = require_env(env, "ALIYUN_OSS_REGION")
    endpoint = optional_env(env, "ALIYUN_OSS_ENDPOINT", f"https://{region}.aliyuncs.com")
    access_key_id = require_env(env, "ALIYUN_OSS_ACCESS_KEY_ID")
    access_key_secret = require_env(env, "ALIYUN_OSS_ACCESS_KEY_SECRET")

    body = local_path.read_bytes()
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
    date = formatdate(usegmt=True)
    target_url = upload_url(bucket, endpoint, key)
    headers = {
        "Authorization": authorization(access_key_id, access_key_secret, "PUT", content_md5, mime, date, bucket, key),
        "Content-MD5": content_md5,
        "Content-Type": mime,
        "Date": date,
    }
    request = Request(target_url, data=body, headers=headers, method="PUT")
    try:
        with urlopen(request, timeout=30) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Aliyun OSS upload failed with HTTP {response.status}")
    except OSError as exc:
        raise ValueError(f"Aliyun OSS upload failed: {exc}") from exc
    return public_url(env, bucket, region, key), bucket
