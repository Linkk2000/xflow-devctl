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


@dataclass(frozen=True)
class _ProcessRecord:
    """Supervisor-owned process identity used for tree cleanup."""

    handle: ServiceHandle
    pid: int
    group_id: Optional[int]


class _HealthDeadlineExceeded(Exception):
    pass


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
        self._process_records: Dict[str, _ProcessRecord] = {}
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

    def _dependency_failure(
        self, dependency_id: str, stage: str, reason: str
    ) -> ValueError:
        return ValueError(f"dependency {dependency_id} {stage} failed: {reason}")

    def _ensure_dependency(self, dependency: ComposeDependency) -> None:
        deadline = time.monotonic() + float(dependency.timeout_seconds)
        up = self._command_for_dependency(dependency, dependency.up)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._dependency_failure(dependency.id, "up", "timed out")
        try:
            outcome = execute_command(
                up,
                self.context,
                capture=True,
                timeout=remaining,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            raise self._dependency_failure(
                dependency.id, "up", f"execution error: {type(exc).__name__}"
            ) from exc
        if time.monotonic() >= deadline:
            raise self._dependency_failure(dependency.id, "up", "timed out")
        if outcome.returncode != 0:
            reason = (
                "timed out"
                if outcome.returncode == 124
                else f"exit {int(outcome.returncode)}"
            )
            raise self._dependency_failure(dependency.id, "up", reason)

        ready = self._command_for_dependency(dependency, dependency.ready)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._dependency_failure(dependency.id, "ready", "timed out")
            try:
                outcome = execute_command(
                    ready,
                    self.context,
                    capture=True,
                    timeout=remaining,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                raise self._dependency_failure(
                    dependency.id, "ready", f"execution error: {type(exc).__name__}"
                ) from exc
            if time.monotonic() >= deadline:
                raise self._dependency_failure(dependency.id, "ready", "timed out")
            if outcome.returncode == 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = (
                    "timed out"
                    if outcome.returncode == 124
                    else f"exit {int(outcome.returncode)}"
                )
                raise self._dependency_failure(dependency.id, "ready", reason)
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

    def _process_record(self, handle: ServiceHandle) -> _ProcessRecord:
        group_id: Optional[int] = None
        if os.name != "nt":
            try:
                group_id = os.getpgid(handle.process.pid)
            except OSError:
                # ``start_new_session=True`` makes the child PID the group ID;
                # retain that identity even when the leader exits immediately.
                group_id = handle.process.pid
            try:
                if group_id == os.getpgrp():
                    group_id = None
            except OSError:
                pass
        return _ProcessRecord(handle, int(handle.process.pid), group_id)

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
            record = self._process_record(handle)
            self._write_pid_atomically(service_id, process.pid)
            self._process_records[service_id] = record
            self._log_streams[service_id] = stream
            return handle
        except FileNotFoundError as exc:
            try:
                if process is not None:
                    self._terminate(ServiceHandle(service_id, process, log_path))
            finally:
                if stream is not None:
                    stream.close()
            raise ValueError(f"executable for service {service_id} was not found") from exc
        except PermissionError as exc:
            try:
                if process is not None:
                    self._terminate(ServiceHandle(service_id, process, log_path))
            finally:
                if stream is not None:
                    stream.close()
            raise ValueError(f"executable for service {service_id} is not permitted") from exc
        except BaseException:
            try:
                if process is not None:
                    record = self._process_records.get(service_id)
                    self._terminate(record or ServiceHandle(service_id, process, log_path))
            finally:
                if stream is not None:
                    stream.close()
            raise

    def _check_early_exit(self, handle: ServiceHandle) -> None:
        # Give an immediately failing child a scheduling opportunity before
        # the next service is spawned.  This is short enough not to delay a
        # normal startup while making startup failures deterministic.
        deadline = time.monotonic() + 0.05
        while handle.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
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

    def _check_all_handles_alive(self) -> None:
        for handle in self._handles:
            returncode = handle.process.poll()
            if returncode is not None:
                raise ValueError(
                    f"service {handle.id} exited during health checks "
                    f"(exit {int(returncode)})"
                )

    def _url_is_healthy(self, url: str, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _HealthDeadlineExceeded()
        try:
            response = urlopen(url, timeout=remaining)
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except _HealthDeadlineExceeded:
            raise
        except Exception:
            return False
        if time.monotonic() >= deadline:
            raise _HealthDeadlineExceeded()
        return True

    def _wait_urls(
        self,
        handle: ServiceHandle,
        urls: Sequence[str],
        deadline: float,
    ) -> None:
        self._check_all_handles_alive()
        if not urls:
            if handle.process.poll() is not None:
                raise ValueError(
                    f"service {handle.id} exited before readiness "
                    f"(exit {int(handle.process.returncode or 0)})"
                )
            return

        while True:
            self._check_all_handles_alive()
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"service {handle.id} health check deadline expired: {', '.join(urls)}"
                )
            for url in urls:
                self._check_all_handles_alive()
                try:
                    healthy = self._url_is_healthy(url, deadline)
                except _HealthDeadlineExceeded:
                    raise ValueError(
                        f"service {handle.id} health check deadline expired: {', '.join(urls)}"
                    )
                if healthy:
                    self._check_all_handles_alive()
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            f"service {handle.id} health check deadline expired: {', '.join(urls)}"
                        )
                    return
            self._check_all_handles_alive()
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"service {handle.id} health check deadline expired: {', '.join(urls)}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f"service {handle.id} health check deadline expired: {', '.join(urls)}"
                )
            time.sleep(min(0.05, remaining))

    def wait_healthy(self, handles: Sequence[ServiceHandle]) -> None:
        """Wait for every declared service health URL, trying URL fallbacks."""

        deadline = time.monotonic() + float(self.profile.docker.startup_timeout_seconds)
        try:
            for handle in handles:
                service = self.profile.services.get(handle.id)
                if service is None:
                    raise ValueError(f"unknown service id: {handle.id}")
                self._wait_urls(handle, service.health_urls, deadline)
            self._check_all_handles_alive()
        except BaseException:
            self.stop_all()
            raise

    def _coerce_record(self, value: object) -> _ProcessRecord:
        if isinstance(value, _ProcessRecord):
            return value
        if isinstance(value, ServiceHandle):
            return self._process_records.get(value.id) or self._process_record(value)
        raise TypeError("process cleanup requires a supervisor process record")

    def _signal_posix(self, record: _ProcessRecord, signum: int) -> bool:
        group_id = record.group_id
        if group_id is not None:
            try:
                if group_id != os.getpgrp():
                    os.killpg(group_id, signum)
                    return True
            except (OSError, ProcessLookupError):
                pass
        try:
            record.handle.process.send_signal(signum)
            return True
        except (OSError, AttributeError):
            return False

    def _group_alive(self, record: _ProcessRecord) -> bool:
        if record.group_id is None:
            return False
        try:
            if record.group_id == os.getpgrp():
                return False
            os.killpg(record.group_id, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _windows_tree_signal(self, record: _ProcessRecord, force: bool) -> bool:
        command = ["taskkill", "/PID", str(record.pid), "/T"]
        if force:
            command.append("/F")
        try:
            subprocess.run(
                command,
                shell=False,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, TypeError):
            return False

    def _terminate(self, value: object) -> None:
        record = self._coerce_record(value)
        process = record.handle.process
        if os.name == "nt":
            sent = self._windows_tree_signal(record, force=False)
            if not sent:
                try:
                    process.terminate()
                except OSError:
                    pass
        else:
            sent = self._signal_posix(record, signal.SIGTERM)
            if not sent:
                try:
                    process.terminate()
                except OSError:
                    pass

        try:
            process.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass

        if os.name == "nt":
            self._windows_tree_signal(record, force=True)
        elif self._group_alive(record):
            try:
                os.killpg(record.group_id, signal.SIGKILL)  # type: ignore[arg-type]
            except (OSError, ProcessLookupError):
                pass
        elif process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

        try:
            process.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def stop_all(self) -> None:
        """Terminate all owned process groups in reverse startup order."""

        handles = list(reversed(self._handles))
        errors: List[str] = []
        for handle in handles:
            try:
                record = self._process_records.get(handle.id) or self._process_record(handle)
                self._terminate(record)
            except Exception as exc:
                errors.append(f"{handle.id} terminate: {type(exc).__name__}")
            finally:
                try:
                    self._remove_pid(handle)
                except Exception as exc:
                    errors.append(f"{handle.id} pid cleanup: {type(exc).__name__}")
                finally:
                    stream = self._log_streams.pop(handle.id, None)
                    if stream is not None:
                        try:
                            stream.close()  # type: ignore[union-attr]
                        except Exception as exc:
                            errors.append(f"{handle.id} log close: {type(exc).__name__}")
        self._handles.clear()
        self._handles_by_id.clear()
        self._process_records.clear()
        if errors:
            raise ValueError("service cleanup failed: " + "; ".join(errors))

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
            deadline = time.monotonic() + float(self.profile.docker.startup_timeout_seconds)
            self._wait_urls(handle, (url,), deadline)
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


def _stop_after_failure(supervisor: ServiceSupervisor) -> None:
    try:
        supervisor.stop_all()
    except Exception as exc:
        _report_failure("service cleanup", exc)


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
        supervisor._check_all_handles_alive()
        if scenario.open_url:
            supervisor.open_browser(scenario.open_url)
        return 0
    except KeyboardInterrupt:
        _stop_after_failure(supervisor)
        return 130
    except Exception as exc:
        _stop_after_failure(supervisor)
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
        supervisor._check_all_handles_alive()
        if open_browser:
            supervisor.open_browser(playground.url)
        return 0
    except KeyboardInterrupt:
        _stop_after_failure(supervisor)
        return 130
    except Exception as exc:
        _stop_after_failure(supervisor)
        _report_failure(f"playground {playground.id}", exc)
        return 1
