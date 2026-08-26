from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
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
    _ProcessRecord,
    ServiceHandle,
    ServiceSupervisor,
    run_playground,
    run_scenario,
)


FIXTURE_PROCESS = """\
from __future__ import annotations

import os
import signal
import subprocess
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
if mode == "leader-exits":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")
    raise SystemExit(0)
if mode == "tree-ignore-term":
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ]
    )
    Path(os.environ["CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")

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
            "CHILD_PID_FILE": str(root / "child.pid"),
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


def _run_sigterm_case(root: Path, mode: str) -> tuple[subprocess.Popen[str], Optional[int]]:
    """Run one real parent CLI process so SIGTERM exercises the public wrapper."""

    runner = root / "signal-runner.py"
    write_text_lf(
        runner,
        "from __future__ import annotations\n"
        "import os, signal, sys, time\n"
        "from dataclasses import replace\n"
        f"sys.path.insert(0, {str(OPS_ROOT)!r})\n"
        "import xflow.services as services\n"
        "from xflow.cockpit import CockpitContext, load_cockpit_profile\n"
        "from xflow.services import run_playground, run_scenario\n"
        f"root = __import__('pathlib').Path({str(root)!r})\n"
        "profile = load_cockpit_profile(root / '.xflow' / 'supervisor.yaml')\n"
        "if sys.argv[1] == 'scenario':\n"
        "    service = replace(profile.services['server'], command=replace(profile.services['server'].command, argv=(sys.executable, str(root / 'fixture-process.py'), 'tree-ignore-term')))\n"
        "    profile = replace(profile, services={**profile.services, 'server': service}, scenarios={'run': replace(profile.scenarios['run'], services=('server',), open_url=None)})\n"
        "else:\n"
        "    playground = replace(profile.playgrounds['flowable'], command=replace(profile.playgrounds['flowable'].command, argv=(sys.executable, str(root / 'fixture-process.py'), 'tree-ignore-term')), build=None)\n"
        "    profile = replace(profile, playgrounds={**profile.playgrounds, 'flowable': playground})\n"
        "env = dict(os.environ)\n"
        "env.update({'EVENT_FILE': str(root / 'events.log'), 'DEP_ATTEMPTS_FILE': str(root / 'dep-attempts'), 'FAIL_DEPENDENCY': '', 'CHILD_PID_FILE': str(root / 'child.pid'), 'PLAYGROUND_BUILD_FILE': str(root / 'build.marker')})\n"
        "context = CockpitContext(root, root.parent, root, __import__('pathlib').Path(sys.executable), root / '.xflow' / 'run', env)\n"
        "def blocked(_url: str, timeout: object = None) -> object:\n"
        "    while True:\n"
        "        time.sleep(10)\n"
        "services.urlopen = blocked\n"
        "old_int = signal.getsignal(signal.SIGINT)\n"
        "old_term = signal.getsignal(signal.SIGTERM)\n"
        "if sys.argv[1] == 'scenario':\n"
        "    result = run_scenario(profile, context, 'run')\n"
        "else:\n"
        "    result = run_playground(profile, context, 'flowable', False)\n"
        "if signal.getsignal(signal.SIGINT) is not old_int or signal.getsignal(signal.SIGTERM) is not old_term:\n"
        "    print('handlers-not-restored', file=sys.stderr)\n"
        "    raise SystemExit(91)\n"
        "print('result=' + str(result), flush=True)\n"
        "raise SystemExit(result)\n",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(OPS_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "EVENT_FILE": str(root / "events.log"),
            "DEP_ATTEMPTS_FILE": str(root / "dep-attempts"),
            "CHILD_PID_FILE": str(root / "child.pid"),
            "PLAYGROUND_BUILD_FILE": str(root / "build.marker"),
            "FAIL_DEPENDENCY": "",
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(runner), mode],
        cwd=root,
        env=environment,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: Optional[int] = None
    for _ in range(100):
        child_pid = _read_pid(root / "child.pid")
        if child_pid is not None:
            break
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert child_pid is not None, process.stderr.read() if process.stderr else ""
    os.kill(process.pid, signal.SIGTERM)
    return process, child_pid


def test_run_scenario_sigterm_cleans_tree_and_restores_handlers() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        _profile(root)
        process, child_pid = _run_sigterm_case(root, "scenario")
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 143, (stdout, stderr)
        assert "result=143" in stdout
        assert not _pid_alive(child_pid)
        assert not (root / ".xflow" / "run" / "server.pid").exists()


def test_run_playground_sigterm_cleans_tree_and_restores_handlers() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        _profile(root)
        process, child_pid = _run_sigterm_case(root, "playground")
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 143, (stdout, stderr)
        assert "result=143" in stdout
        assert not _pid_alive(child_pid)
        assert not (root / ".xflow" / "run" / "flowable.pid").exists()


def test_dependency_up_timeout_uses_absolute_budget_and_controlled_error() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        calls = []

        def timed_out(_spec: object, _context: CockpitContext, *, capture: bool = False, timeout: Optional[float] = None) -> object:
            calls.append(timeout)
            raise subprocess.TimeoutExpired(["dependency-up"], timeout or 0)

        supervisor = ServiceSupervisor(profile, context)
        with mock.patch("xflow.services.execute_command", side_effect=timed_out):
            try:
                supervisor.ensure_dependencies(("server",))
            except ValueError as exc:
                assert "database" in str(exc)
                assert "up" in str(exc)
            else:
                raise AssertionError("dependency timeout must be controlled")
        assert calls and calls[0] is not None and calls[0] <= 2


def test_dependency_up_success_after_deadline_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        dependency = replace(profile.dependencies["database"], timeout_seconds=0.01)
        profile = replace(
            profile,
            dependencies={**profile.dependencies, "database": dependency},
        )
        context = _context(root)

        def late_success(
            _spec: object,
            _context: CockpitContext,
            *,
            capture: bool = False,
            timeout: Optional[float] = None,
        ) -> object:
            time.sleep(0.03)
            return type("Outcome", (), {"returncode": 0})()

        supervisor = ServiceSupervisor(profile, context)
        with mock.patch("xflow.services.execute_command", side_effect=late_success):
            try:
                supervisor.ensure_dependencies(("server",))
            except ValueError as exc:
                assert "database" in str(exc)
                assert "up" in str(exc)
                assert "timed out" in str(exc)
            else:
                raise AssertionError("late dependency success must not pass the deadline")


def test_dependency_ready_execution_error_identifies_ready_stage() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        calls = []

        def ready_error(_spec: object, _context: CockpitContext, *, capture: bool = False, timeout: Optional[float] = None) -> object:
            calls.append(timeout)
            if len(calls) == 1:
                return type("Outcome", (), {"returncode": 0})()
            raise OSError("fixture readiness failure")

        supervisor = ServiceSupervisor(profile, context)
        with mock.patch("xflow.services.execute_command", side_effect=ready_error):
            try:
                supervisor.ensure_dependencies(("server",))
            except ValueError as exc:
                assert "database" in str(exc)
                assert "ready" in str(exc)
            else:
                raise AssertionError("dependency readiness errors must be controlled")
        assert len(calls) == 2 and all(timeout is not None for timeout in calls)


def test_health_url_probe_rechecks_deadline_after_probe_returns() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        profile = replace(profile, docker=replace(profile.docker, startup_timeout_seconds=0.01))
        context = _context(root)

        class Alive:
            pid = os.getpid()
            returncode = None

            def poll(self) -> None:
                return None

        handle = type("Handle", (), {"id": "server", "process": Alive(), "log_path": root / "server.log"})()
        supervisor = ServiceSupervisor(profile, context)
        supervisor._handles = [handle]
        supervisor._handles_by_id["server"] = handle
        service = replace(profile.services["server"], health_urls=("http://ready",))
        supervisor.profile = replace(profile, services={**profile.services, "server": service})

        def late_success(_url: str, timeout: Optional[float] = None) -> object:
            time.sleep(0.03)
            return object()

        with mock.patch("xflow.services.urlopen", side_effect=late_success):
            with mock.patch.object(supervisor, "stop_all"):
                try:
                    supervisor.wait_healthy((handle,))
                except ValueError as exc:
                    assert "health" in str(exc)
                else:
                    raise AssertionError("late URL success must not pass the deadline")


def test_health_wait_requires_all_supervisor_handles_alive_before_return() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)

        class Process:
            def __init__(self) -> None:
                self.pid = os.getpid()
                self.returncode = None
                self.dead = False

            def poll(self) -> Optional[int]:
                return 1 if self.dead else None

        first_process = Process()
        second_process = Process()
        first = type("Handle", (), {"id": "server", "process": first_process, "log_path": root / "server.log"})()
        second = type("Handle", (), {"id": "web", "process": second_process, "log_path": root / "web.log"})()
        supervisor = ServiceSupervisor(profile, context)
        supervisor._handles = [first, second]
        supervisor._handles_by_id.update({"server": first, "web": second})

        def ready(url: str, timeout: Optional[float] = None) -> object:
            if url.endswith("/ready"):
                first_process.dead = True
            raise URLError("not ready") if url.endswith("/unavailable") else URLError("race")

        with mock.patch("xflow.services.urlopen", side_effect=ready):
            with mock.patch.object(supervisor, "stop_all") as stop:
                try:
                    supervisor.wait_healthy((first, second))
                except ValueError as exc:
                    assert "server" in str(exc)
                    assert stop.called
                else:
                    raise AssertionError("all handles must be checked after each readiness")


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: Optional[int]) -> bool:
    if pid is None or pid == os.getpid():
        return False
    try:
        os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return False
    return True


def _kill_pid(pid: Optional[int]) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def test_leader_exit_still_cleans_owned_descendant_group() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        server = replace(
            profile.services["server"],
            command=replace(
                profile.services["server"].command,
                argv=(sys.executable, str(root / "fixture-process.py"), "leader-exits"),
            ),
        )
        profile = replace(profile, services={**profile.services, "server": server})
        supervisor = ServiceSupervisor(profile, context)
        child_pid = None
        try:
            try:
                supervisor.start(("server",))
            except ValueError as exc:
                assert "server" in str(exc)
            else:
                raise AssertionError("leader exit must fail startup")
            child_pid = _read_pid(root / "child.pid")
            for _ in range(20):
                if not _pid_alive(child_pid):
                    break
                time.sleep(0.02)
            assert not _pid_alive(child_pid)
        finally:
            _kill_pid(child_pid)


def test_tree_cleanup_forces_child_which_ignores_term() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        server = replace(
            profile.services["server"],
            command=replace(
                profile.services["server"].command,
                argv=(sys.executable, str(root / "fixture-process.py"), "tree-ignore-term"),
            ),
        )
        profile = replace(profile, services={**profile.services, "server": server})
        supervisor = ServiceSupervisor(profile, context)
        child_pid = None
        try:
            handles = supervisor.start(("server",))
            for _ in range(20):
                child_pid = _read_pid(root / "child.pid")
                if child_pid is not None:
                    break
                time.sleep(0.02)
            streams = list(supervisor._log_streams.values())
            supervisor.stop_all()
            assert handles[0].process.poll() is not None
            assert not _pid_alive(child_pid)
            assert all(getattr(stream, "closed", False) for stream in streams)
        finally:
            _kill_pid(child_pid)


def test_stop_all_cleans_process_record_before_handle_registration() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)
        handle = supervisor._spawn(
            "server", profile.services["server"].command, root / ".xflow" / "run" / "server.log"
        )
        # A signal can arrive after _spawn records ownership but before the
        # caller appends the handle to its ordered list.
        assert supervisor._handles == []
        supervisor.stop_all()
        assert handle.process.poll() is not None
        assert not (context.run_dir / "server.pid").exists()
        assert supervisor._process_records == {}
        assert supervisor._log_streams == {}


def test_stop_all_collects_pid_cleanup_errors_and_closes_every_stream() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)
        handles = supervisor.start(("server", "web"))
        streams = list(supervisor._log_streams.values())
        with mock.patch.object(supervisor, "_remove_pid", side_effect=PermissionError("denied")):
            try:
                supervisor.stop_all()
            except ValueError as exc:
                assert "cleanup" in str(exc).lower()
            else:
                raise AssertionError("cleanup failures must be reported after all handles")
        assert all(handle.process.poll() is not None for handle in handles)
        assert all(getattr(stream, "closed", False) for stream in streams)
        assert supervisor._handles == []
        assert supervisor._log_streams == {}


def test_windows_tree_cleanup_uses_injectable_argv_capability() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)

        class Process:
            pid = 4242
            returncode = 0
            waits = 0

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired(["fixture"], timeout or 0)
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

        supervisor = ServiceSupervisor(profile, context)
        handle = ServiceHandle("server", Process(), root / "server.log")
        job = object()
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch(
                "xflow.services.subprocess.run",
                return_value=type("Outcome", (), {"returncode": 0})(),
            ) as run:
                with mock.patch.object(supervisor, "_windows_job_terminate", return_value=False):
                    with mock.patch.object(
                        supervisor, "_windows_job_active_processes", return_value=0
                    ):
                        with mock.patch.object(supervisor, "_windows_job_close", return_value=True):
                            record = _ProcessRecord(handle, 4242, None, job, True, True)
                            supervisor._terminate(record)
        assert run.call_count == 1
        for call in run.call_args_list:
            argv = call.args[0]
            assert argv[:4] == ["taskkill", "/PID", "4242", "/T"]
            assert call.kwargs["shell"] is False


def test_windows_taskkill_nonzero_is_not_success() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))

        class Process:
            pid = 4242
            returncode = 0

            def poll(self) -> int:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return self.returncode

            def terminate(self) -> None:
                return None

        handle = ServiceHandle("server", Process(), root / "server.log")
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch(
                "xflow.services.subprocess.run",
                return_value=type("Outcome", (), {"returncode": 1})(),
            ) as run:
                record = _ProcessRecord(handle, 4242, None, None, False, True)
                assert supervisor._windows_tree_signal(record, force=False) is False
                try:
                    supervisor._terminate(record)
                except ValueError as exc:
                    assert "termination" in str(exc).lower()
                else:
                    raise AssertionError("non-zero taskkill must not be success")
        assert run.call_count == 3


def test_windows_fallback_without_job_never_claims_tree_clean() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))

        class Process:
            pid = 4242
            returncode = 0

            def poll(self) -> int:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return self.returncode

        handle = ServiceHandle("server", Process(), root / "server.log")
        record = _ProcessRecord(handle, 4242, None, None, False, True)
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch(
                "xflow.services.subprocess.run",
                return_value=type("Outcome", (), {"returncode": 0})(),
            ):
                try:
                    supervisor._terminate(record)
                except ValueError as exc:
                    assert "owned tree unknown" in str(exc)
                else:
                    raise AssertionError("fallback must not claim unknown tree cleanup")


def test_windows_spawn_record_attaches_injectable_job() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))

        class Process:
            pid = 4242

        handle = ServiceHandle("server", Process(), root / "server.log")
        job = object()
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_windows_create_job", return_value=job) as create:
                record = supervisor._process_record(handle, attach_job=True)
        assert create.call_args.args == (handle.process,)
        assert record.windows_job is job
        assert record.windows_tree_known is True
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_windows_create_job", return_value=None):
                degraded = supervisor._process_record(handle, attach_job=True)
        assert degraded.windows_job is None
        assert degraded.windows_tree_known is False


def test_windows_trampoline_waits_before_business_execution() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))
        neutral = root / "neutral"
        neutral.mkdir()
        cwd = root / "business-cwd"
        cwd.mkdir()
        marker = root / "business.marker"
        local_module_marker = root / "local-module.marker"
        local_module = (
            "from pathlib import Path; "
            f"Path({str(local_module_marker)!r}).write_text('imported', encoding='utf-8')"
        )
        (neutral / "subprocess.py").write_text(local_module, encoding="utf-8")
        (neutral / "sitecustomize.py").write_text(local_module, encoding="utf-8")
        business_code = (
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')"
        )
        command = [sys.executable, "-c", business_code]
        ack = root / "business.ack"
        payload = json.dumps(
            {"argv": command, "cwd": str(cwd), "ack": str(ack)},
            separators=(",", ":"),
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(neutral)
        process = subprocess.Popen(
            supervisor._windows_trampoline_argv(),
            cwd=str(neutral),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            shell=False,
        )
        try:
            time.sleep(0.05)
            assert not local_module_marker.exists()
            assert not marker.exists()
            assert process.stdin is not None
            process.stdin.write("1\n" + payload + "\n")
            process.stdin.flush()
            process.stdin.close()
            assert process.wait(timeout=5) == 0
            assert marker.read_text(encoding="utf-8") == "executed"
            assert ack.read_text(encoding="utf-8") == "ready"
            assert "executed" not in ack.read_text(encoding="utf-8")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if ack.exists():
                ack.unlink()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def test_windows_spawn_assigns_before_release_and_business_state() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)
        events = []
        state = {"business_executed": False}

        class Pipe:
            def __init__(self) -> None:
                self.closed = False
                self.payload = None

            def write(self, value: str) -> int:
                assert value.startswith("1\n")
                self.payload = json.loads(value.splitlines()[1])
                events.append("payload_release")
                return len(value)

            def flush(self) -> None:
                assert self.payload is not None
                events.append("business_popen")
                type(root)(self.payload["ack"]).write_text("ready", encoding="ascii")
                events.append("ack_written")
                state["business_executed"] = True

            def close(self) -> None:
                self.closed = True
                events.append("release_closed")

        class Process:
            pid = 4242
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return self.returncode

        process = Process()
        command = profile.services["server"].command

        def popen(argv: Sequence[str], **kwargs: object) -> Process:
            events.append("trampoline_started")
            assert list(argv[:2]) == [sys.executable, "-I"]
            assert argv[2] == "-S"
            assert "--" not in argv
            assert kwargs["stdin"] == subprocess.PIPE
            assert kwargs["shell"] is False
            assert kwargs["cwd"] == str(context.run_dir)
            return process

        def assign(_process: object) -> object:
            assert events == ["trampoline_started"]
            events.append("job_assigned")
            return object()

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch("xflow.services.Path", type(root)):
                with mock.patch("xflow.services.subprocess.Popen", side_effect=popen):
                    with mock.patch.object(supervisor, "_windows_create_job", side_effect=assign):
                        with mock.patch.object(
                            supervisor,
                            "_write_pid_atomically",
                            side_effect=lambda _service_id, _pid: events.append("pid_written"),
                        ) as write_pid:
                            handle = supervisor._spawn("server", command, root / "server.log")
                            assert handle.process is process
                            assert write_pid.called
        assert events == [
            "trampoline_started",
            "job_assigned",
            "payload_release",
            "business_popen",
            "ack_written",
            "release_closed",
            "pid_written",
        ]
        assert state["business_executed"] is True
        assert supervisor._process_records["server"].business_released is True
        assert not (context.run_dir / "server.ack").exists()
        supervisor._log_streams["server"].close()


def test_windows_spawn_assignment_failure_never_releases_business() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)
        state = {"business_executed": False}

        class Pipe:
            def __init__(self) -> None:
                self.closed = False

            def write(self, _value: str) -> int:
                state["business_executed"] = True
                return 1

            def flush(self) -> None:
                state["business_executed"] = True

            def close(self) -> None:
                self.closed = True

        class Process:
            pid = 4242
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return int(self.returncode or 0)

            def terminate(self) -> None:
                self.returncode = 143

            def kill(self) -> None:
                self.returncode = 137

        process = Process()

        def popen(_argv: Sequence[str], **_kwargs: object) -> Process:
            return process

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch("xflow.services.Path", type(root)):
                with mock.patch("xflow.services.subprocess.Popen", side_effect=popen):
                    with mock.patch.object(supervisor, "_windows_create_job", return_value=None):
                        with mock.patch.object(supervisor, "_write_pid_atomically") as write_pid:
                            try:
                                supervisor._spawn(
                                    "server",
                                    profile.services["server"].command,
                                    root / "server.log",
                                )
                            except ValueError as exc:
                                assert "ownership" in str(exc).lower()
                            else:
                                raise AssertionError("assignment failure must reject startup")
        assert state["business_executed"] is False
        assert process.stdin.closed is True
        assert process.returncode is not None
        assert not write_pid.called


def test_windows_trampoline_missing_executable_reports_failure_ack() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))
        ack = root / "missing.ack"
        process = subprocess.Popen(
            supervisor._windows_trampoline_argv(),
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        payload = json.dumps(
            {
                "argv": [str(root / "does-not-exist")],
                "cwd": str(root),
                "ack": str(ack),
            },
            separators=(",", ":"),
        )
        try:
            assert process.stdin is not None
            process.stdin.write("1\n" + payload + "\n")
            process.stdin.flush()
            process.stdin.close()
            assert process.wait(timeout=5) == 126
            assert ack.read_text(encoding="ascii") == "error"
            assert "does-not-exist" not in ack.read_text(encoding="ascii")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if ack.exists():
                ack.unlink()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def test_windows_spawn_ack_timeout_is_controlled_and_cleans_process() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def write(self, _value: str) -> int:
                return 1

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        class Process:
            pid = 4242
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return int(self.returncode or 0)

            def terminate(self) -> None:
                self.returncode = 143

            def kill(self) -> None:
                self.returncode = 137

        process = Process()
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch("xflow.services.Path", type(root)):
                with mock.patch("xflow.services.subprocess.Popen", return_value=process):
                    with mock.patch.object(
                        supervisor, "_windows_create_job", return_value=object()
                    ):
                        with mock.patch.object(
                            supervisor, "_windows_ack_timeout", return_value=0.01
                        ):
                            with mock.patch.object(
                                supervisor, "_terminate", return_value=True
                            ) as terminate:
                                with mock.patch.object(
                                    supervisor, "_write_pid_atomically"
                                ) as write_pid:
                                    try:
                                        supervisor._spawn(
                                            "server",
                                            profile.services["server"].command,
                                            root / "server.log",
                                        )
                                    except ValueError as exc:
                                        assert "ack" in str(exc).lower()
                                    else:
                                        raise AssertionError("ACK timeout must fail startup")
        assert terminate.called
        assert not write_pid.called
        assert not (context.run_dir / "server.ack").exists()
        assert supervisor._log_streams == {}


def test_windows_spawn_error_ack_is_synchronous_start_failure() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def __init__(self) -> None:
                self.payload = None

            def write(self, value: str) -> int:
                self.payload = json.loads(value.splitlines()[1])
                return len(value)

            def flush(self) -> None:
                assert self.payload is not None
                type(root)(self.payload["ack"]).write_text("error", encoding="ascii")

            def close(self) -> None:
                return None

        class Process:
            pid = 4343
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return self.returncode

        process = Process()
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch("xflow.services.Path", type(root)):
                with mock.patch("xflow.services.subprocess.Popen", return_value=process):
                    with mock.patch.object(
                        supervisor, "_windows_create_job", return_value=object()
                    ):
                        with mock.patch.object(
                            supervisor, "_terminate", return_value=True
                        ) as terminate:
                            with mock.patch.object(
                                supervisor, "_write_pid_atomically"
                            ) as write_pid:
                                try:
                                    supervisor._spawn(
                                        "server",
                                        profile.services["server"].command,
                                        root / "server.log",
                                    )
                                except ValueError as exc:
                                    assert "executable failed before ack" in str(exc).lower()
                                else:
                                    raise AssertionError("error ACK must fail synchronously")
        assert terminate.called
        assert not write_pid.called
        assert not (context.run_dir / "server.ack").exists()


def test_windows_abort_failure_retains_live_trampoline_ownership() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def close(self) -> None:
                raise OSError("pipe close failed")

        class Process:
            pid = 5151
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return None

            def terminate(self) -> None:
                raise OSError("terminate failed")

            def kill(self) -> None:
                raise OSError("kill failed")

            def wait(self, timeout: Optional[float] = None) -> int:
                raise subprocess.TimeoutExpired("trampoline", timeout)

        process = Process()

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch("xflow.services.Path", type(root)):
                with mock.patch("xflow.services.subprocess.Popen", return_value=process):
                    with mock.patch.object(supervisor, "_windows_create_job", return_value=None):
                        with mock.patch.object(supervisor, "_write_pid_atomically") as write_pid:
                            try:
                                supervisor._spawn(
                                    "server",
                                    profile.services["server"].command,
                                    root / "server.log",
                                )
                            except ValueError as exc:
                                assert "cleanup" in str(exc).lower()
                            else:
                                raise AssertionError("live abort must report cleanup failure")
        assert "server" in supervisor._process_records
        assert supervisor._process_records["server"].pid == process.pid
        assert supervisor._process_records["server"].business_released is False
        assert supervisor._handles_by_id["server"].process is process
        assert write_pid.called
        assert supervisor._log_streams["server"].closed


def test_windows_prerelease_retry_clears_after_trampoline_exits() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class Process:
            pid = 5252
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return self.returncode

            def terminate(self) -> None:
                raise OSError("abort unavailable")

            def kill(self) -> None:
                raise OSError("kill unavailable")

            def wait(self, timeout: Optional[float] = None) -> int:
                raise subprocess.TimeoutExpired("trampoline", timeout)

        process = Process()
        handle = ServiceHandle("server", process, root / "server.log")
        record = _ProcessRecord(handle, process.pid, None, None, False, False)
        supervisor._handles = [handle]
        supervisor._handles_by_id[handle.id] = handle
        supervisor._process_records[handle.id] = record
        stream = (root / "server.log").open("w", encoding="utf-8")
        supervisor._log_streams[handle.id] = stream
        supervisor._write_pid_atomically(handle.id, process.pid)
        pid_path = context.run_dir / "server.pid"

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_pid_path", return_value=pid_path):
                try:
                    supervisor.stop_all()
                except ValueError as exc:
                    assert "server" in str(exc)
                else:
                    raise AssertionError("failed pre-release abort must be retained")
                assert supervisor._process_records[handle.id].business_released is False
                assert supervisor._handles == [handle]
                assert pid_path.exists()
                assert stream.closed

                process.returncode = 0
                supervisor.stop_all()
        assert supervisor._handles == []
        assert supervisor._process_records == {}
        assert not pid_path.exists()


def test_windows_prerelease_retry_clears_when_abort_later_succeeds() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def close(self) -> None:
                return None

        class Process:
            pid = 5353
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()
                self.attempts = 0

            def poll(self) -> Optional[int]:
                return self.returncode

            def terminate(self) -> None:
                self.attempts += 1
                if self.attempts < 2:
                    raise OSError("first abort unavailable")
                self.returncode = 0

            def kill(self) -> None:
                raise OSError("kill not expected")

            def wait(self, timeout: Optional[float] = None) -> int:
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("trampoline", timeout)
                return self.returncode

        process = Process()
        handle = ServiceHandle("server", process, root / "server.log")
        record = _ProcessRecord(handle, process.pid, None, None, False, False)
        supervisor._handles = [handle]
        supervisor._handles_by_id[handle.id] = handle
        supervisor._process_records[handle.id] = record
        stream = (root / "server.log").open("w", encoding="utf-8")
        supervisor._log_streams[handle.id] = stream
        supervisor._write_pid_atomically(handle.id, process.pid)
        pid_path = context.run_dir / "server.pid"

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_pid_path", return_value=pid_path):
                try:
                    supervisor.stop_all()
                except ValueError:
                    pass
                else:
                    raise AssertionError("first pre-release abort must be retained")
                assert supervisor._handles == [handle]
                supervisor.stop_all()
        assert supervisor._handles == []
        assert supervisor._process_records == {}
        assert not pid_path.exists()
        assert stream.closed


def test_windows_prerelease_retry_still_retains_when_abort_fails() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Pipe:
            def close(self) -> None:
                return None

        class Process:
            pid = 5454
            returncode = None

            def __init__(self) -> None:
                self.stdin = Pipe()

            def poll(self) -> Optional[int]:
                return None

            def terminate(self) -> None:
                raise OSError("terminate unavailable")

            def kill(self) -> None:
                raise OSError("kill unavailable")

            def wait(self, timeout: Optional[float] = None) -> int:
                raise subprocess.TimeoutExpired("trampoline", timeout)

        process = Process()
        handle = ServiceHandle("server", process, root / "server.log")
        record = _ProcessRecord(handle, process.pid, None, None, False, False)
        supervisor._handles = [handle]
        supervisor._handles_by_id[handle.id] = handle
        supervisor._process_records[handle.id] = record
        stream = (root / "server.log").open("w", encoding="utf-8")
        supervisor._log_streams[handle.id] = stream
        supervisor._write_pid_atomically(handle.id, process.pid)
        pid_path = context.run_dir / "server.pid"

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_pid_path", return_value=pid_path):
                for _ in range(2):
                    try:
                        supervisor.stop_all()
                    except ValueError as exc:
                        assert "server" in str(exc)
                    else:
                        raise AssertionError("failed pre-release abort must remain owned")
                assert supervisor._handles == [handle]
                assert supervisor._process_records[handle.id].business_released is False
                assert pid_path.exists()
        assert stream.closed


def test_windows_released_unknown_tree_still_rejects_cleanup() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))

        class Process:
            pid = 5555
            returncode = 0

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return self.returncode

        handle = ServiceHandle("server", Process(), root / "server.log")
        record = _ProcessRecord(handle, handle.process.pid, None, None, False, True)
        with mock.patch("xflow.services.os.name", "nt"):
            try:
                supervisor._terminate(record)
            except ValueError as exc:
                assert "owned tree unknown" in str(exc)
            else:
                raise AssertionError("released unknown tree must not be weakened")


def test_windows_failed_tree_cleanup_retains_ownership_for_retry() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Process:
            pid = 4242
            returncode = 0

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return self.returncode

        class Job:
            active = 1
            closed = False

        process = Process()
        job = Job()
        handle = ServiceHandle("server", process, root / "server.log")
        record = _ProcessRecord(handle, 4242, None, job, True, True)
        supervisor._handles = [handle]
        supervisor._handles_by_id[handle.id] = handle
        supervisor._process_records[handle.id] = record
        stream = (root / "server.log").open("w", encoding="utf-8")
        supervisor._log_streams[handle.id] = stream
        supervisor._write_pid_atomically(handle.id, process.pid)
        pid_path = context.run_dir / "server.pid"

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(
                supervisor, "_pid_path", return_value=pid_path
            ):
                with mock.patch.object(supervisor, "_windows_job_terminate", return_value=True):
                    with mock.patch.object(
                        supervisor,
                        "_windows_job_active_processes",
                        side_effect=lambda _job: job.active,
                    ):
                        with mock.patch.object(supervisor, "_windows_job_close") as close:
                            try:
                                supervisor.stop_all()
                            except ValueError as exc:
                                assert "terminate" in str(exc)
                            else:
                                raise AssertionError("live owned tree must remain owned")
                            assert supervisor._handles == [handle]
                            assert supervisor._handles_by_id[handle.id] is handle
                            assert handle.id in supervisor._process_records
                            assert pid_path.exists()
                            assert stream.closed
                            assert not close.called

                            job.active = 0
                            process.returncode = 0
                            supervisor.stop_all()
                            assert supervisor._handles == []
                            assert supervisor._process_records == {}
                            assert not pid_path.exists()
                            assert close.called


def test_windows_final_live_leader_is_controlled_failure() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        supervisor = ServiceSupervisor(profile, _context(root))

        class Process:
            pid = 4242
            returncode = None

            def poll(self) -> Optional[int]:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                raise subprocess.TimeoutExpired(["fixture"], timeout or 0)

        handle = ServiceHandle("server", Process(), root / "server.log")
        record = _ProcessRecord(handle, 4242, None, object(), True, True)
        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(supervisor, "_windows_job_terminate", return_value=True):
                with mock.patch.object(
                    supervisor, "_windows_job_active_processes", return_value=0
                ):
                    try:
                        supervisor._terminate(record)
                    except ValueError as exc:
                        assert "alive" in str(exc).lower()
                    else:
                        raise AssertionError("live leader must fail controlled cleanup")


def test_windows_failed_handle_does_not_block_other_cleanup() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        profile = _profile(root)
        context = _context(root)
        supervisor = ServiceSupervisor(profile, context)

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.returncode = 0

            def poll(self) -> int:
                return self.returncode

            def wait(self, timeout: Optional[float] = None) -> int:
                return self.returncode

        class Job:
            def __init__(self, active: int) -> None:
                self.active = active

        server = ServiceHandle("server", Process(4242), root / "server.log")
        web = ServiceHandle("web", Process(4243), root / "web.log")
        server_job = Job(1)
        web_job = Job(0)
        supervisor._handles = [server, web]
        supervisor._handles_by_id.update({"server": server, "web": web})
        supervisor._process_records.update(
            {
                "server": _ProcessRecord(server, 4242, None, server_job, True, True),
                "web": _ProcessRecord(web, 4243, None, web_job, True, True),
            }
        )
        server_stream = (root / "server.log").open("w", encoding="utf-8")
        web_stream = (root / "web.log").open("w", encoding="utf-8")
        supervisor._log_streams.update({"server": server_stream, "web": web_stream})
        supervisor._write_pid_atomically("server", 4242)
        supervisor._write_pid_atomically("web", 4243)
        pid_paths = {
            "server": context.run_dir / "server.pid",
            "web": context.run_dir / "web.pid",
        }

        with mock.patch("xflow.services.os.name", "nt"):
            with mock.patch.object(
                supervisor, "_pid_path", side_effect=lambda service_id: pid_paths[service_id]
            ):
                with mock.patch.object(supervisor, "_windows_job_terminate", return_value=True):
                    with mock.patch.object(
                        supervisor,
                        "_windows_job_active_processes",
                        side_effect=lambda job: job.active,
                    ):
                        with mock.patch.object(supervisor, "_windows_job_close", return_value=True):
                            try:
                                supervisor.stop_all()
                            except ValueError as exc:
                                assert "server" in str(exc)
                            else:
                                raise AssertionError("live handle cleanup must be reported")
        assert supervisor._handles == [server]
        assert supervisor._handles_by_id == {"server": server}
        assert "server" in supervisor._process_records
        assert "web" not in supervisor._process_records
        assert pid_paths["server"].exists()
        assert not pid_paths["web"].exists()
        assert server_stream.closed and web_stream.closed


def main() -> None:
    tests: Sequence[Callable[[], None]] = (
        test_dependency_readiness_is_deduplicated_before_services,
        test_dependency_failure_identifies_dependency_and_starts_no_service,
        test_early_child_failure_cleans_started_processes,
        test_stale_pid_metadata_is_rejected_without_spawning,
        test_run_scenario_opens_browser_only_after_health,
        test_playground_alias_builds_before_spawn_and_opens_after_ready,
        test_playground_build_failure_does_not_start_process,
        test_run_scenario_sigterm_cleans_tree_and_restores_handlers,
        test_run_playground_sigterm_cleans_tree_and_restores_handlers,
        test_dependency_up_timeout_uses_absolute_budget_and_controlled_error,
        test_dependency_up_success_after_deadline_is_rejected,
        test_dependency_ready_execution_error_identifies_ready_stage,
        test_health_url_probe_rechecks_deadline_after_probe_returns,
        test_health_wait_requires_all_supervisor_handles_alive_before_return,
        test_leader_exit_still_cleans_owned_descendant_group,
        test_tree_cleanup_forces_child_which_ignores_term,
        test_stop_all_cleans_process_record_before_handle_registration,
        test_stop_all_collects_pid_cleanup_errors_and_closes_every_stream,
        test_windows_tree_cleanup_uses_injectable_argv_capability,
        test_windows_taskkill_nonzero_is_not_success,
        test_windows_fallback_without_job_never_claims_tree_clean,
        test_windows_spawn_record_attaches_injectable_job,
        test_windows_trampoline_waits_before_business_execution,
        test_windows_spawn_assigns_before_release_and_business_state,
        test_windows_spawn_assignment_failure_never_releases_business,
        test_windows_trampoline_missing_executable_reports_failure_ack,
        test_windows_spawn_ack_timeout_is_controlled_and_cleans_process,
        test_windows_spawn_error_ack_is_synchronous_start_failure,
        test_windows_abort_failure_retains_live_trampoline_ownership,
        test_windows_prerelease_retry_clears_after_trampoline_exits,
        test_windows_prerelease_retry_clears_when_abort_later_succeeds,
        test_windows_prerelease_retry_still_retains_when_abort_fails,
        test_windows_released_unknown_tree_still_rejects_cleanup,
        test_windows_failed_tree_cleanup_retains_ownership_for_retry,
        test_windows_final_live_leader_is_controlled_failure,
        test_windows_failed_handle_does_not_block_other_cleanup,
    )
    for test in tests:
        test()
    print("cockpit services ok")


if __name__ == "__main__":
    main()
