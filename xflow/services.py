"""Process supervision for cockpit services, scenarios, and playgrounds."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .cockpit import (
    CockpitContext,
    CockpitProfile,
    CommandSpec,
    ComposeDependency,
    PlaygroundSpec,
    ServiceSpec,
)
from .commands import _expanded_command, _expand, execute_command
from .io import write_text_lf


@dataclass(frozen=True)
class ServiceHandle:
    """The process and log location for one supervised child."""

    id: str
    process: subprocess.Popen[str]
    log_path: Path


def _unique_ids(service_ids: Sequence[str]) -> Tuple[str, ...]:
    result: List[str] = []
    seen = set()
    for service_id in service_ids:
        value = str(service_id)
        if value not in seen:
            result.append(value)
            seen.add(value)
    return tuple(result)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def urlopen(url: str, timeout: Optional[float] = None) -> object:
    """Keep the health probe seam injectable while using urllib by default."""

    return urllib.request.urlopen(url, timeout=timeout)


class ServiceSupervisor:
    """Start, health-check, and clean up profile-declared processes.

    The profile remains the source of truth for commands.  Commands are
    expanded by the shell-free command module, then passed to ``Popen`` as an
    argv list with a dedicated process group/session.
    """

    def __init__(
        self,
        profile: CockpitProfile,
        context: CockpitContext,
        *,
        opener: Optional[Callable[..., object]] = None,
    ) -> None:
        self.profile = profile
        self.context = context
        self.opener = webbrowser.open if opener is None else opener
        self._handles: List[ServiceHandle] = []
        self._handles_by_id: Dict[str, ServiceHandle] = {}
        self._log_streams: Dict[str, object] = {}
        self._dependencies_started = set()

    def _run_dir(self) -> Path:
        run_dir = Path(self.context.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _pid_path(self, service_id: str) -> Path:
        return self._run_dir() / (service_id + ".pid")

    def _assert_pid_available(self, service_id: str) -> None:
        path = self._pid_path(service_id)
        if not path.exists():
            return
        try:
            metadata = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"service {service_id} has unreadable stale pid metadata: {path}"
            ) from exc
        detail = metadata or "empty"
        raise ValueError(
            f"service {service_id} has stale pid metadata ({detail}) at {path}; "
            "remove it only after confirming the previous process is stopped"
        )

    def _write_pid_atomically(self, service_id: str, pid: int) -> Path:
        target = self._pid_path(service_id)
        temporary = target.with_name(
            ".{0}.{1}.{2}.tmp".format(service_id, os.getpid(), time.monotonic_ns())
        )
        try:
            write_text_lf(temporary, str(pid) + "\n")
            os.replace(str(temporary), str(target))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return target

    def _remove_pid(self, handle: ServiceHandle) -> None:
        path = self._pid_path(handle.id)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return
        if value != str(handle.process.pid):
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _log_path(self, service: ServiceSpec) -> Path:
        try:
            rendered = Path(_expand(service.log_file, self.context, "service log path"))
        except ValueError as exc:
            raise ValueError(f"invalid log path for service {service.id}") from exc
        path = rendered.resolve(strict=False)
        run_dir = self._run_dir().resolve(strict=False)
        if not _inside(path, run_dir):
            raise ValueError(f"service {service.id} log path must stay under run directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _command_for_dependency(
        self, dependency: ComposeDependency, command: CommandSpec
    ) -> CommandSpec:
        # ComposeDependency.cwd is the single working-directory declaration
        # for both commands.  Keeping it authoritative also makes profiles
        # resilient if a command's generic cwd is edited independently.
        return replace(command, cwd=dependency.cwd)

    def _dependency_ids(self, service_ids: Sequence[str]) -> Tuple[str, ...]:
        ids = _unique_ids(service_ids)
        for service_id in ids:
            if service_id not in self.profile.services:
                raise ValueError(f"unknown service id: {service_id}")

        dependencies: List[str] = []
        seen = set()
        for service_id in ids:
            service = self.profile.services[service_id]
            for dependency_id in service.dependencies:
                if dependency_id not in self.profile.dependencies:
                    raise ValueError(
                        f"service {service_id} references unknown dependency: {dependency_id}"
                    )
                if dependency_id not in seen:
                    dependencies.append(dependency_id)
                    seen.add(dependency_id)
        return tuple(dependencies)

    def _dependency_failure(self, dependency_id: str, stage: str, code: int) -> ValueError:
        return ValueError(
            f"dependency {dependency_id} {stage} failed (exit {int(code)})"
        )

    def _ensure_dependency(self, dependency: ComposeDependency) -> None:
        up = self._command_for_dependency(dependency, dependency.up)
        try:
            outcome = execute_command(up, self.context, capture=True)
        except BaseException:
            raise
        if outcome.returncode != 0:
            raise self._dependency_failure(dependency.id, "startup", outcome.returncode)

        ready = self._command_for_dependency(dependency, dependency.ready)
        deadline = time.monotonic() + float(dependency.timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._dependency_failure(dependency.id, "readiness", 124)
            try:
                outcome = execute_command(
                    ready,
                    self.context,
                    capture=True,
                    timeout=remaining,
                )
            except BaseException:
                raise
            if outcome.returncode == 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._dependency_failure(dependency.id, "readiness", outcome.returncode)
            time.sleep(min(0.05, remaining))

    def ensure_dependencies(self, service_ids: Sequence[str]) -> None:
        """Start each referenced Compose dependency once and await readiness."""

        dependency_ids = self._dependency_ids(service_ids)
        for dependency_id in dependency_ids:
            if dependency_id in self._dependencies_started:
                continue
            dependency = self.profile.dependencies[dependency_id]
            self._ensure_dependency(dependency)
            self._dependencies_started.add(dependency_id)

    def _session_kwargs(self) -> Dict[str, object]:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return {"creationflags": flags}
        return {"start_new_session": True}

    def _spawn(
        self,
        service_id: str,
        command: CommandSpec,
        log_path: Path,
    ) -> ServiceHandle:
        try:
            argv, cwd, child_env = _expanded_command(command, self.context)
        except ValueError as exc:
            raise ValueError(f"invalid command for service {service_id}: {exc}") from exc

        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise ValueError(f"working directory for service {service_id} does not exist: {cwd}")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = None
        process = None
        try:
            stream = log_path.open("a", encoding="utf-8")
            kwargs = {
                "cwd": str(cwd_path),
                "env": dict(child_env),
                "stdout": stream,
                "stderr": subprocess.STDOUT,
                "shell": False,
                "text": True,
                "encoding": "utf-8",
            }
            kwargs.update(self._session_kwargs())
            process = subprocess.Popen(list(argv), **kwargs)
            handle = ServiceHandle(service_id, process, log_path)
            self._write_pid_atomically(service_id, process.pid)
            self._log_streams[service_id] = stream
            return handle
        except FileNotFoundError as exc:
            if process is not None:
                self._terminate(process)
            if stream is not None:
                stream.close()
            raise ValueError(f"executable for service {service_id} was not found") from exc
        except PermissionError as exc:
            if process is not None:
                self._terminate(process)
            if stream is not None:
                stream.close()
            raise ValueError(f"executable for service {service_id} is not permitted") from exc
        except BaseException:
            if process is not None:
                self._terminate(process)
            if stream is not None:
                stream.close()
            raise

    def _check_early_exit(self, handle: ServiceHandle) -> None:
        # Give an immediately failing child a scheduling opportunity before
        # the next service is spawned.  This is short enough not to delay a
        # normal startup while making startup failures deterministic.
        if handle.process.poll() is None:
            time.sleep(0.01)
        returncode = handle.process.poll()
        if returncode is not None:
            raise ValueError(
                f"service {handle.id} exited during startup (exit {int(returncode)})"
            )

    def start(self, service_ids: Sequence[str]) -> Tuple[ServiceHandle, ...]:
        """Start unique services in declared order after dependencies are ready."""

        ids = _unique_ids(service_ids)
        if not ids:
            return ()
        # Resolve and validate every service before any dependency or process
        # side effect, so a typo cannot leave a partial startup behind.
        services = []
        for service_id in ids:
            service = self.profile.services.get(service_id)
            if service is None:
                raise ValueError(f"unknown service id: {service_id}")
            services.append(service)

        for service_id in ids:
            existing = self._handles_by_id.get(service_id)
            if existing is not None:
                if existing.process.poll() is None:
                    continue
                raise ValueError(
                    f"service {service_id} exited before it could be reused "
                    f"(exit {int(existing.process.returncode or 0)})"
                )
            self._assert_pid_available(service_id)

        # Reject stale ownership metadata before touching any Compose
        # dependency.  A failed startup must not perform even prerequisite
        # side effects for a service that cannot be safely owned.
        self.ensure_dependencies(ids)

        started: List[ServiceHandle] = []
        try:
            for service in services:
                existing = self._handles_by_id.get(service.id)
                if existing is not None:
                    started.append(existing)
                    self._check_early_exit(existing)
                    continue
                handle = self._spawn(service.id, service.command, self._log_path(service))
                self._handles.append(handle)
                self._handles_by_id[service.id] = handle
                started.append(handle)
                self._check_early_exit(handle)
        except BaseException:
            self.stop_all()
            raise
        return tuple(started)

    def _url_is_healthy(self, url: str, timeout: float) -> bool:
        try:
            response = urlopen(url, timeout=max(0.01, timeout))
            close = getattr(response, "close", None)
            if callable(close):
                close()
            return True
        except Exception:
            return False

    def _wait_urls(
        self,
        handle: ServiceHandle,
        urls: Sequence[str],
        timeout_seconds: float,
    ) -> None:
        if not urls:
            if handle.process.poll() is not None:
                raise ValueError(
                    f"service {handle.id} exited before readiness "
                    f"(exit {int(handle.process.returncode or 0)})"
                )
            return

        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        while True:
            returncode = handle.process.poll()
            if returncode is not None:
                raise ValueError(
                    f"service {handle.id} exited before readiness (exit {int(returncode)})"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f"service {handle.id} health check timed out: {', '.join(urls)}"
                )
            for url in urls:
                if self._url_is_healthy(url, remaining):
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f"service {handle.id} health check timed out: {', '.join(urls)}"
                )
            time.sleep(min(0.05, remaining))

    def wait_healthy(self, handles: Sequence[ServiceHandle]) -> None:
        """Wait for every declared service health URL, trying URL fallbacks."""

        timeout_seconds = float(self.profile.docker.startup_timeout_seconds)
        try:
            for handle in handles:
                service = self.profile.services.get(handle.id)
                if service is None:
                    raise ValueError(f"unknown service id: {handle.id}")
                self._wait_urls(handle, service.health_urls, timeout_seconds)
        except BaseException:
            self.stop_all()
            raise

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            try:
                process.wait(timeout=0)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return

        if os.name == "nt":
            try:
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
                if ctrl_break is not None:
                    process.send_signal(ctrl_break)
                else:
                    process.terminate()
            except (OSError, AttributeError):
                try:
                    process.terminate()
                except OSError:
                    pass
        else:
            try:
                group_id = os.getpgid(process.pid)
                if group_id == os.getpid():
                    process.terminate()
                else:
                    os.killpg(group_id, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    pass

        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                try:
                    process.kill()
                except OSError:
                    pass
            else:
                try:
                    group_id = os.getpgid(process.pid)
                    if group_id == os.getpid():
                        process.kill()
                    else:
                        os.killpg(group_id, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except OSError:
            pass

    def stop_all(self) -> None:
        """Terminate all owned process groups in reverse startup order."""

        handles = list(reversed(self._handles))
        for handle in handles:
            try:
                self._terminate(handle.process)
            finally:
                self._remove_pid(handle)
                stream = self._log_streams.pop(handle.id, None)
                if stream is not None:
                    try:
                        stream.close()  # type: ignore[union-attr]
                    except OSError:
                        pass
        self._handles.clear()
        self._handles_by_id.clear()

    def _start_playground(self, playground: PlaygroundSpec) -> ServiceHandle:
        self._assert_pid_available(playground.id)
        log_path = self._run_dir() / (playground.id + ".log")
        handle = self._spawn(playground.id, playground.command, log_path)
        self._handles.append(handle)
        self._handles_by_id[playground.id] = handle
        try:
            self._check_early_exit(handle)
        except BaseException:
            self.stop_all()
            raise
        return handle

    def _wait_playground(self, handle: ServiceHandle, url: str) -> None:
        try:
            self._wait_urls(handle, (url,), float(self.profile.docker.startup_timeout_seconds))
        except BaseException:
            self.stop_all()
            raise

    def open_browser(self, url: str) -> object:
        """Open one URL with the injected opener after readiness succeeds."""

        try:
            return self.opener(url, new=2)
        except TypeError:
            # A list.append-style test seam intentionally accepts only the URL;
            # the production webbrowser opener still receives ``new=2``.
            return self.opener(url)


def _playground_for(profile: CockpitProfile, target: str) -> PlaygroundSpec:
    if target in profile.playgrounds:
        return profile.playgrounds[target]
    for playground in profile.playgrounds.values():
        if target in playground.aliases:
            return playground
    raise ValueError(f"unknown playground target: {target}")


def _report_failure(label: str, error: BaseException) -> None:
    print(f"[ERROR] {label}: {error}", file=sys.stderr)


def run_scenario(
    profile: CockpitProfile, context: CockpitContext, scenario_id: str
) -> int:
    """Run a named scenario, opening its URL only after all services are healthy."""

    scenario = profile.scenarios.get(scenario_id)
    if scenario is None:
        raise ValueError(f"unknown scenario id: {scenario_id}")
    supervisor = ServiceSupervisor(profile, context)
    try:
        supervisor.ensure_dependencies(scenario.services)
        handles = supervisor.start(scenario.services)
        supervisor.wait_healthy(handles)
        if scenario.open_url:
            supervisor.open_browser(scenario.open_url)
        return 0
    except KeyboardInterrupt:
        supervisor.stop_all()
        return 130
    except Exception as exc:
        supervisor.stop_all()
        _report_failure(f"scenario {scenario_id}", exc)
        return 1


def run_playground(
    profile: CockpitProfile,
    context: CockpitContext,
    target: str,
    open_browser: bool,
) -> int:
    """Build and run a playground target or alias."""

    playground = _playground_for(profile, target)
    supervisor = ServiceSupervisor(profile, context)
    try:
        if playground.build is not None:
            outcome = execute_command(playground.build, context, capture=True)
            if outcome.returncode != 0:
                _report_failure(
                    f"playground {playground.id} build",
                    ValueError(f"command exited with {int(outcome.returncode)}"),
                )
                return int(outcome.returncode) or 1
        handle = supervisor._start_playground(playground)
        supervisor._wait_playground(handle, playground.url)
        if open_browser:
            supervisor.open_browser(playground.url)
        return 0
    except KeyboardInterrupt:
        supervisor.stop_all()
        return 130
    except Exception as exc:
        supervisor.stop_all()
        _report_failure(f"playground {playground.id}", exc)
        return 1
