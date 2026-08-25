from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional, Sequence
from unittest import mock
from urllib.error import URLError


OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf  # noqa: E402
from xflow.cockpit import CockpitContext, load_cockpit_profile  # noqa: E402
from xflow.services import (  # noqa: E402
    ServiceSupervisor,
    run_playground,
    run_scenario,
)


FIXTURE_PROCESS = """\
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path


event_file = Path(os.environ.get("EVENT_FILE", "events.log"))


def event(value: str) -> None:
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")


mode = sys.argv[1]
if mode == "dependency-up":
    event("dependency-up")
    raise SystemExit(0 if not os.environ.get("FAIL_DEPENDENCY") else 9)
if mode == "dependency-ready":
    event("dependency-ready")
    if os.environ.get("FAIL_DEPENDENCY"):
        raise SystemExit(9)
    attempts = Path(os.environ["DEP_ATTEMPTS_FILE"])
    try:
        count = int(attempts.read_text(encoding="utf-8"))
    except FileNotFoundError:
        count = 0
    attempts.write_text(str(count + 1), encoding="utf-8")
    raise SystemExit(0 if count >= 1 else 1)
if mode == "build":
    event("build")
    marker = os.environ.get("PLAYGROUND_BUILD_FILE")
    if marker:
        Path(marker).write_text("built", encoding="utf-8")
    raise SystemExit(0)
if mode == "fail":
    event("fail")
    raise SystemExit(17)

name = sys.argv[-1] if mode == "service" else "playground"
event("start:" + name)


def stop(_signum: int, _frame: object) -> None:
    event("stop:" + name)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    time.sleep(0.02)
"""


class _Response:
    def close(self) -> None:
        return None


def _urlopen(url: str, timeout: Optional[float] = None) -> _Response:
    if url.endswith("/unavailable"):
        raise URLError("not ready")
    return _Response()


def _context(root: Path, *, fail_dependency: bool = False) -> CockpitContext:
    run_dir = root / ".xflow" / "run"
    env = dict(os.environ)
    env.update(
        {
            "EVENT_FILE": str(root / "events.log"),
            "DEP_ATTEMPTS_FILE": str(root / "dep-attempts"),
            "FAIL_DEPENDENCY": "1" if fail_dependency else "",
            "PLAYGROUND_BUILD_FILE": str(root / "build.marker"),
        }
    )
    return CockpitContext(root, root.parent, root, Path(sys.executable), run_dir, env)


def _profile(root: Path):
    profile_path = root / ".xflow" / "supervisor.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "supervisor-profile.yaml", profile_path)
    write_text_lf(root / "fixture-process.py", FIXTURE_PROCESS)
    return load_cockpit_profile(profile_path)


def _events(root: Path) -> list[str]:
    path = root / "events.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _cleanup_run(run_dir: Path) -> None:
    """Clean successful wrapper sessions which intentionally remain running."""

    for pid_path in run_dir.glob("*.pid"):
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid == os.getpid():
            continue
        try:
            group_id = os.getpgid(pid)
            if group_id == os.getpid():
                os.kill(pid, signal.SIGTERM)
            else:
                os.killpg(group_id, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
    time.sleep(0.05)


def test_dependency_readiness_is_deduplicated_before_services() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        with mock.patch("xflow.services.urlopen", side_effect=_urlopen):
            supervisor.ensure_dependencies(("server", "web", "server"))
            handles = supervisor.start(("server", "web", "server"))
            supervisor.wait_healthy(handles)

        assert [handle.id for handle in handles] == ["server", "web"]
        assert _events(root)[:3] == ["dependency-up", "dependency-ready", "dependency-ready"]
        assert _events(root).index("dependency-ready") < _events(root).index("start:server")
        assert handles[0].log_path == root / ".xflow" / "run" / "server.log"
        assert handles[1].log_path == root / ".xflow" / "run" / "web.log"
        supervisor.stop_all()
        assert all(handle.process.poll() is not None for handle in handles)
        assert not (root / ".xflow" / "run" / "server.pid").exists()
        assert not (root / ".xflow" / "run" / "web.pid").exists()


def test_dependency_failure_identifies_dependency_and_starts_no_service() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root, fail_dependency=True)
        supervisor = ServiceSupervisor(profile, context)

        try:
            supervisor.start(("server", "web"))
        except ValueError as exc:
            assert "database" in str(exc)
        else:
            raise AssertionError("dependency failure must be reported")

        assert not any(item.startswith("start:") for item in _events(root))
        supervisor.stop_all()


def test_early_child_failure_cleans_started_processes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        failed = replace(
            profile.services["server"],
            command=replace(
                profile.services["server"].command,
                argv=(sys.executable, str(root / "fixture-process.py"), "fail"),
            ),
        )
        profile = replace(profile, services={**profile.services, "server": failed})
        supervisor = ServiceSupervisor(profile, _context(root))
        try:
            supervisor.start(("server", "web"))
        except ValueError as exc:
            assert "server" in str(exc)
        else:
            raise AssertionError("early child failure must be reported")
        assert not (root / ".xflow" / "run" / "server.pid").exists()
        assert not (root / ".xflow" / "run" / "web.pid").exists()
        supervisor.stop_all()


def test_stale_pid_metadata_is_rejected_without_spawning() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        pid_path = context.run_dir / "server.pid"
        write_text_lf(pid_path, str(os.getpid()) + "\n")
        supervisor = ServiceSupervisor(profile, context)

        try:
            supervisor.start(("server",))
        except ValueError as exc:
            assert "pid" in str(exc).lower()
        else:
            raise AssertionError("stale pid metadata must not be accepted")
        assert not _events(root)
        supervisor.stop_all()


def test_run_scenario_opens_browser_only_after_health() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        opened: list[str] = []

        with mock.patch("xflow.services.urlopen", side_effect=_urlopen):
            with mock.patch("xflow.services.webbrowser.open", side_effect=lambda url, new=0: opened.append(url)):
                assert run_scenario(profile, context, "run") == 0
        assert opened == ["http://127.0.0.1:1/ready"]
        # run_scenario intentionally leaves a successful development session up;
        # clean its process groups through the generated pid metadata.
        _cleanup_run(context.run_dir)


def test_playground_alias_builds_before_spawn_and_opens_after_ready() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        opened: list[str] = []

        with mock.patch("xflow.services.urlopen", side_effect=_urlopen):
            with mock.patch("xflow.services.webbrowser.open", side_effect=lambda url, new=0: opened.append(url)):
                assert run_playground(profile, context, "f", True) == 0
        assert _events(root)[:2] == ["build", "start:playground"]
        assert opened == ["http://127.0.0.1:1/playground"]
        _cleanup_run(context.run_dir)


def test_playground_build_failure_does_not_start_process() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        build = replace(
            profile.playgrounds["flowable"].build,
            argv=(sys.executable, str(root / "fixture-process.py"), "fail"),
        )
        playground = replace(profile.playgrounds["flowable"], build=build)
        profile = replace(profile, playgrounds={**profile.playgrounds, "flowable": playground})

        assert run_playground(profile, context, "flowable", True) != 0
        assert not any(item.startswith("start:") for item in _events(root))
        assert not (context.run_dir / "flowable.pid").exists()


def main() -> None:
    tests: Sequence[Callable[[], None]] = (
        test_dependency_readiness_is_deduplicated_before_services,
        test_dependency_failure_identifies_dependency_and_starts_no_service,
        test_early_child_failure_cleans_started_processes,
        test_stale_pid_metadata_is_rejected_without_spawning,
        test_run_scenario_opens_browser_only_after_health,
        test_playground_alias_builds_before_spawn_and_opens_after_ready,
        test_playground_build_failure_does_not_start_process,
    )
    for test in tests:
        test()
    print("cockpit services ok")


if __name__ == "__main__":
    main()
