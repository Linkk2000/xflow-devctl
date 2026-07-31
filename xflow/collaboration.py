from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, TypeVar, cast

from .bindings import fingerprint, git_path
from .json_safety import loads_unique_json


@dataclass
class _LockState:
    thread_lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    stream: BinaryIO | None = None


_STATES_GUARD = threading.Lock()
_STATES: dict[Path, _LockState] = {}
_MUTATION_LOCAL = threading.local()
_INHERITED_LOCAL = threading.local()
_F = TypeVar("_F", bound=Callable[..., object])
_LEASE_ENV = "XFLOW_DEVCTL_MUTATION_LEASE"
_LEASE_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_LEASE_FIELDS = {"version", "token", "repository", "worktree", "ownerPid"}
_SAFE_INHERITED_COMMANDS = frozenset({("task", "status")})


def _lock_path(repo_root: Path) -> Path:
    try:
        owner = git_path(repo_root, "--git-common-dir") / "xflow"
    except ValueError:
        owner = repo_root.resolve() / ".xflow" / "local"
    return owner / "locks" / "devctl-repository.lock"


def _lease_identity(repo_root: Path) -> tuple[Path, str, str]:
    common_dir = git_path(repo_root, "--git-common-dir")
    worktree = git_path(repo_root, "--show-toplevel")
    return (
        common_dir,
        fingerprint("repository", common_dir),
        fingerprint("worktree", worktree),
    )


def _lease_path(common_dir: Path, token: str) -> Path:
    return common_dir / "xflow" / "locks" / "mutations" / f"{token}.json"


def _validate_inherited_lease(repo_root: Path, token: str) -> None:
    try:
        from .local_artifacts import (
            MAX_SMALL_ARTIFACT_BYTES,
            capture_stable_file,
            revalidate_snapshots,
        )

        if not _LEASE_TOKEN_RE.fullmatch(token):
            raise ValueError("invalid mutation lease token")
        common_dir, repository, worktree = _lease_identity(repo_root)
        path = _lease_path(common_dir, token)
        snapshot = capture_stable_file(
            common_dir,
            path,
            common_dir,
            "repository mutation lease",
            max_bytes=MAX_SMALL_ARTIFACT_BYTES,
        )
        assert snapshot.content is not None
        payload = loads_unique_json(snapshot.content.decode("utf-8", errors="strict"))
        if (
            not isinstance(payload, dict)
            or set(payload) != _LEASE_FIELDS
            or payload.get("version") != 1
            or payload.get("token") != token
            or payload.get("repository") != repository
            or payload.get("worktree") != worktree
            or type(payload.get("ownerPid")) is not int
            or payload["ownerPid"] <= 0
        ):
            raise ValueError("mutation lease identity mismatch")
        revalidate_snapshots(common_dir, (snapshot,), "repository mutation lease")
    except (UnicodeError, ValueError):
        raise ValueError("inherited repository mutation lease is invalid or inactive") from None


def inherited_lease_present() -> bool:
    return bool(os.environ.get(_LEASE_ENV, ""))


@contextmanager
def inherited_lease_command(repo_root: Path, command: tuple[str, ...]) -> Iterator[None]:
    token = os.environ.get(_LEASE_ENV, "")
    if not token:
        yield
        return
    if command not in _SAFE_INHERITED_COMMANDS:
        raise ValueError(
            "inherited repository mutation lease does not allow command: " + " ".join(command)
        )
    _validate_inherited_lease(repo_root, token)
    previous = getattr(_INHERITED_LOCAL, "authorization", None)
    _INHERITED_LOCAL.authorization = (token, command)
    try:
        yield
        _validate_inherited_lease(repo_root, token)
    finally:
        if previous is None:
            delattr(_INHERITED_LOCAL, "authorization")
        else:
            _INHERITED_LOCAL.authorization = previous


def _authorized_inherited_lease(repo_root: Path) -> str | None:
    token = os.environ.get(_LEASE_ENV, "")
    if not token:
        return None
    authorization = getattr(_INHERITED_LOCAL, "authorization", None)
    if (
        not isinstance(authorization, tuple)
        or len(authorization) != 2
        or authorization[0] != token
        or authorization[1] not in _SAFE_INHERITED_COMMANDS
    ):
        raise ValueError("inherited repository mutation lease was not authorized at CLI dispatch")
    _validate_inherited_lease(repo_root, token)
    return token


def git_child_environment(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    token = getattr(_MUTATION_LOCAL, "token", None)
    if isinstance(token, str):
        env[_LEASE_ENV] = token
    elif _authorized_inherited_lease(repo_root) is None:
        env.pop(_LEASE_ENV, None)
    return env


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
    inherited = _authorized_inherited_lease(repo_root)
    if inherited is not None:
        try:
            yield
        finally:
            _validate_inherited_lease(repo_root, inherited)
        return
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


@contextmanager
def repository_mutation(repo_root: Path) -> Iterator[None]:
    if inherited_lease_present():
        _authorized_inherited_lease(repo_root)
        raise ValueError("inherited repository mutation lease cannot authorize repository mutation")
    with repository_lock(repo_root):
        common_dir, repository, worktree = _lease_identity(repo_root)
        token = secrets.token_hex(32)
        path = _lease_path(common_dir, token)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "token": token,
            "repository": repository,
            "worktree": worktree,
            "ownerPid": os.getpid(),
        }
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        previous = getattr(_MUTATION_LOCAL, "token", None)
        _MUTATION_LOCAL.token = token
        try:
            yield
        finally:
            if previous is None:
                delattr(_MUTATION_LOCAL, "token")
            else:
                _MUTATION_LOCAL.token = previous
            path.unlink(missing_ok=True)


def repository_locked(function: _F) -> _F:
    @wraps(function)
    def locked(repo_root: Path, *args: object, **kwargs: object) -> object:
        with repository_lock(repo_root):
            return function(repo_root, *args, **kwargs)

    return cast(_F, locked)
