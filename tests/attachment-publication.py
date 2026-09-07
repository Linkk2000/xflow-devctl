from __future__ import annotations

import sys
from pathlib import Path

OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.attachment import reject_publication_body


def assert_value_error(expected: str, action: object) -> None:
    try:
        action()
    except ValueError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def main() -> None:
    reject_publication_body("See `.xflow/issues/issue-1/issue-draft.md` for scope.")
    reject_publication_body("Rendered under `.xflow/publish/issues/issue-1/issue.final.md`.")
    reject_publication_body("Ops notes may mention `.xflow/ops/workflow/SKILL.md`.")

    assert_value_error(
        ".xflow/local path",
        lambda: reject_publication_body("Do not open `.xflow/local/env.local`."),
    )
    assert_value_error(
        ".xflow/local path",
        lambda: reject_publication_body("Pointer: .xflow\\local\\worktrees\\abc\\active-task.json"),
    )
    assert_value_error(
        "file:// local path",
        lambda: reject_publication_body("Bad link file:///tmp/secret"),
    )
    assert_value_error(
        "Windows local path",
        lambda: reject_publication_body("Bad path C:\\Users\\x\\notes.md"),
    )
    assert_value_error(
        "POSIX local path",
        lambda: reject_publication_body("Bad path /tmp/scratch.md"),
    )
    assert_value_error(
        "xflow-attachment://",
        lambda: reject_publication_body("![img](xflow-attachment://att-001)"),
    )

    # Adjacent names must not be treated as .xflow/local.
    reject_publication_body("Capability notes under `.xflow/locale-pack/README.md` are fine.")

    print("attachment publication ok")


if __name__ == "__main__":
    main()
