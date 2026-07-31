from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, TypeVar, cast

from .bindings import git_path


@dataclass
class _LockState:
    thread_lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    stream: BinaryIO | None = None


_STATES_GUARD = threading.Lock()
_STATES: dict[Path, _LockState] = {}
_F = TypeVar("_F", bound=Callable[..., object])


def _lock_path(repo_root: Path) -> Path:
    try:
        owner = git_path(repo_root, "--git-common-dir") / "xflow"
    except ValueError:
        owner = repo_root.resolve() / ".xflow" / "local"
    return owner / "locks" / "devctl-repository.lock"


def _try_lock(stream: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def repository_lock(repo_root: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    path = _lock_path(repo_root)
    with _STATES_GUARD:
        state = _STATES.setdefault(path, _LockState())
    state.thread_lock.acquire()
    try:
        if state.depth == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a+b")
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            configured = os.environ.get("XFLOW_COLLABORATION_LOCK_TIMEOUT", "30")
            timeout = float(configured) if timeout_seconds is None else timeout_seconds
            deadline = time.monotonic() + max(timeout, 0.0)
            while not _try_lock(stream):
                if time.monotonic() >= deadline:
                    stream.close()
                    raise ValueError(
                        "another devctl process holds the repository collaboration lock"
                    )
                time.sleep(0.05)
            state.stream = stream
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                assert state.stream is not None
                _unlock(state.stream)
                state.stream.close()
                state.stream = None
    finally:
        state.thread_lock.release()


def repository_locked(function: _F) -> _F:
    @wraps(function)
    def locked(repo_root: Path, *args: object, **kwargs: object) -> object:
        with repository_lock(repo_root):
            return function(repo_root, *args, **kwargs)

    return cast(_F, locked)
