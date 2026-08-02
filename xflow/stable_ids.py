from __future__ import annotations

import re


STABLE_ID_PATTERN = (
    r"^(?!(?:na|none|placeholder|tbd|todo|unknown)(?![\s\S]))"
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?![\s\S])"
)
STABLE_ID_RE = re.compile(STABLE_ID_PATTERN)


def is_stable_id(value: object) -> bool:
    return isinstance(value, str) and STABLE_ID_RE.fullmatch(value) is not None


def require_stable_id(value: object, label: str) -> str:
    if not is_stable_id(value):
        raise ValueError(
            f"{label} must use stable ID syntax: lowercase ASCII letters/digits "
            "separated by single dots or hyphens"
        )
    return value
