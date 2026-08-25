from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf

from xflow.bindings import resolve_bindings


def git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_repo(repo_root: Path) -> None:
    repo_root.mkdir()
    git(repo_root, "init", "-q")
    git(repo_root, "config", "user.email", "test@example.com")
    git(repo_root, "config", "user.name", "Test User")
    git(repo_root, "checkout", "-b", "feature/101-a", "-q")
    write_text_lf(repo_root / "README.md", "# Demo\n")
    git(repo_root, "add", "README.md")
    git(repo_root, "commit", "-m", "init", "-q")


def test_worktree_bindings_and_detached_head() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        main_worktree = root / "main-worktree"
        sibling_worktree = root / "sibling-worktree"
        init_repo(main_worktree)
        git(main_worktree, "worktree", "add", "-b", "feature/202-b", str(sibling_worktree), "HEAD")

        first = resolve_bindings(main_worktree)
        second = resolve_bindings(sibling_worktree)

        assert first.repository == second.repository
        assert first.worktree != second.worktree
        assert first.branch == "feature/101-a"
        assert second.branch == "feature/202-b"

        git(main_worktree, "checkout", "--detach", "-q")
        try:
            resolve_bindings(main_worktree)
        except ValueError as exc:
            assert str(exc) == "cannot bind XFlow task to detached HEAD"
        else:
            raise AssertionError("expected detached HEAD binding to fail")


if __name__ == "__main__":
    test_worktree_bindings_and_detached_head()
    print("bindings core tests passed")
