"""Shell-free execution for validated cockpit profile commands."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from . import capabilities
from .cockpit import CheckSpec, CockpitContext, CockpitProfile, CommandSpec


@dataclass(frozen=True)
class CommandOutcome:
    argv: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


_SENSITIVE_ENV_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "COOKIE")


def start_engine_provider(
    env: Mapping[str, str], *, timeout: Optional[float] = None
) -> bool:
    """Delegate optional provider startup while keeping the capability seam injectable."""

    return capabilities.start_engine_provider(env, timeout=timeout)


def _template_values(context: CockpitContext) -> Dict[str, str]:
    values = {key: str(value) for key, value in context.env.items()}
    values.update(
        {
            "cockpit": str(context.cockpit_root),
            "workspace": str(context.workspace_root),
            "repo": str(context.repo_root),
            "python": str(context.python_executable),
        }
    )
    return values


def _expand(value: str, context: CockpitContext, label: str) -> str:
    try:
        return value.format_map(_template_values(context))
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"missing or invalid {label} template: {value}") from exc


def _expanded_command(
    spec: CommandSpec, context: CockpitContext
) -> Tuple[Tuple[str, ...], str, Dict[str, str]]:
    argv = tuple(_expand(str(argument), context, "command argument") for argument in spec.argv)
    if not argv or not argv[0].strip():
        raise ValueError("command argv must not be empty")
    cwd = _expand(spec.cwd, context, "command cwd")
    child_env = {str(key): str(value) for key, value in context.env.items()}
    values = _template_values(context)
    for key, value in spec.env.items():
        try:
            child_env[str(key)] = str(value).format_map(values)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"missing or invalid command environment template: {key}") from exc
    return argv, cwd, child_env


def _is_sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def _redact(text: str, env: Mapping[str, str]) -> str:
    redacted = text
    # Replace longer values first so a short value cannot expose a suffix of a
    # longer secret during the replacement pass.
    secrets = sorted(
        {
            str(value)
            for name, value in env.items()
            if _is_sensitive_name(str(name)) and str(value)
        },
        key=len,
        reverse=True,
    )
    for secret in secrets:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _display_argv(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(argument)) for argument in argv)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _append_diagnostic(stderr: str, diagnostic: str) -> str:
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return stderr + diagnostic + "\n"


def _cwd_diagnostic(cwd: str) -> Optional[str]:
    path = Path(cwd)
    try:
        if not path.exists():
            return f"working directory does not exist: {cwd}"
        if not path.is_dir():
            return f"working directory is not a directory: {cwd}"
    except OSError as exc:
        return f"working directory cannot be inspected: {cwd}: {exc}"
    return None


def _run_expanded_command(
    argv: Tuple[str, ...],
    cwd: str,
    child_env: Mapping[str, str],
    *,
    capture: bool,
    timeout: Optional[float] = None,
) -> CommandOutcome:
    cwd_error = _cwd_diagnostic(cwd)
    if cwd_error is not None:
        return CommandOutcome(
            argv,
            126,
            "",
            _redact(cwd_error, child_env),
        )

    run_kwargs = {
        "cwd": str(cwd),
        "env": dict(child_env),
        "text": True,
        "encoding": "utf-8",
        "stdout": subprocess.PIPE if capture else None,
        "stderr": subprocess.PIPE if capture else None,
        "shell": False,
    }
    if timeout is not None:
        run_kwargs["timeout"] = max(0.0, float(timeout))
    try:
        completed = subprocess.run(list(argv), **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = _as_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        stderr = _as_text(getattr(exc, "stderr", None))
        timeout_label = "unknown" if timeout is None else f"{float(timeout):g}s"
        diagnostic = (
            f"command timed out after {timeout_label}: {_display_argv(argv)} "
            f"(cwd={cwd})"
        )
        return CommandOutcome(
            argv,
            124,
            _redact(stdout, child_env),
            _redact(_append_diagnostic(stderr, diagnostic), child_env),
        )
    except FileNotFoundError as exc:
        diagnostic = (
            f"executable not found for command {_display_argv(argv)} "
            f"(cwd={cwd}): {exc}"
        )
        return CommandOutcome(argv, 127, "", _redact(diagnostic, child_env))
    except PermissionError as exc:
        diagnostic = (
            f"executable is not permitted for command {_display_argv(argv)} "
            f"(cwd={cwd}): {exc}"
        )
        return CommandOutcome(argv, 126, "", _redact(diagnostic, child_env))
    except OSError as exc:
        diagnostic = f"could not execute command {_display_argv(argv)} (cwd={cwd}): {exc}"
        return CommandOutcome(argv, 126, "", _redact(diagnostic, child_env))

    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    return CommandOutcome(argv, int(completed.returncode), stdout, stderr)


def execute_command(
    spec: CommandSpec,
    context: CockpitContext,
    *,
    capture: bool = False,
    timeout: Optional[float] = None,
) -> CommandOutcome:
    """Expand and execute one profile command without invoking a shell."""

    argv, cwd, child_env = _expanded_command(spec, context)
    return _run_expanded_command(
        argv,
        cwd,
        child_env,
        capture=capture,
        timeout=timeout,
    )


def execute_state(
    profile: CockpitProfile, context: CockpitContext, args: Sequence[str]
) -> int:
    argv, cwd, child_env = _expanded_command(profile.state_command, context)
    outcome = _run_expanded_command(
        argv + tuple(str(argument) for argument in args),
        cwd,
        child_env,
        capture=False,
    )
    if outcome.stdout:
        print(outcome.stdout, end="" if outcome.stdout.endswith("\n") else "\n")
    if outcome.stderr:
        print(outcome.stderr, file=sys.stderr, end="" if outcome.stderr.endswith("\n") else "\n")
    return outcome.returncode


def _emit_outcome(label: str, outcome: CommandOutcome, env: Mapping[str, str]) -> None:
    if outcome.stdout:
        print(_redact(outcome.stdout, env), end="" if outcome.stdout.endswith("\n") else "\n")
    if outcome.stderr:
        print(_redact(outcome.stderr, env), file=sys.stderr, end="" if outcome.stderr.endswith("\n") else "\n")
    status = "OK" if outcome.returncode == 0 else "FAIL"
    print(f"[{status}] {label} (exit {outcome.returncode})")


def _preflight_ok(check: CheckSpec, outcome: CommandOutcome) -> bool:
    if outcome.returncode != 0:
        return False
    if check.expect_regex is None:
        return True
    return re.search(check.expect_regex, outcome.stdout + outcome.stderr) is not None


def run_preflight(
    profile: CockpitProfile, context: CockpitContext, warn_only: bool
) -> int:
    failures = 0
    for check in profile.checks:
        outcome = execute_command(check.command, context, capture=True)
        try:
            _argv, _cwd, diagnostic_env = _expanded_command(check.command, context)
        except ValueError:
            diagnostic_env = dict(context.env)
        if _preflight_ok(check, outcome):
            print(f"[OK] {check.id}")
            continue
        failures += 1
        label = f"preflight {check.id}"
        if warn_only:
            if outcome.stdout:
                print(_redact(outcome.stdout, diagnostic_env), end="" if outcome.stdout.endswith("\n") else "\n")
            if outcome.stderr:
                print(_redact(outcome.stderr, diagnostic_env), file=sys.stderr, end="" if outcome.stderr.endswith("\n") else "\n")
            print(f"[WARN] {label} (exit {outcome.returncode})")
        else:
            _emit_outcome(label, outcome, diagnostic_env)
    return 0 if warn_only or failures == 0 else 1


def _docker_probe(
    label: str,
    spec: CommandSpec,
    context: CockpitContext,
    *,
    timeout: Optional[float] = None,
) -> CommandOutcome:
    if timeout is None:
        outcome = execute_command(spec, context, capture=True)
    else:
        outcome = execute_command(spec, context, capture=True, timeout=timeout)
    try:
        _argv, _cwd, diagnostic_env = _expanded_command(spec, context)
    except ValueError:
        diagnostic_env = dict(context.env)
    _emit_outcome(f"docker {label}", outcome, diagnostic_env)
    return outcome


def _setup_engine(
    profile: CockpitProfile, context: CockpitContext
) -> CommandOutcome:
    deadline = time.monotonic() + float(profile.docker.startup_timeout_seconds)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return CommandOutcome(
            tuple(profile.docker.engine_probe.argv),
            124,
            "",
            "engine readiness deadline expired before probe\n",
        )
    engine = _docker_probe(
        "engine",
        profile.docker.engine_probe,
        context,
        timeout=remaining,
    )
    if engine.returncode == 0:
        return engine
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return engine
    if not start_engine_provider(context.env, timeout=remaining):
        return engine

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return engine
        time.sleep(min(0.1, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return engine
        engine = _docker_probe(
            "engine",
            profile.docker.engine_probe,
            context,
            timeout=remaining,
        )
        if engine.returncode == 0:
            return engine


def run_docker(profile: CockpitProfile, context: CockpitContext, action: str) -> int:
    if action not in {"status", "setup"}:
        raise ValueError(f"unsupported Docker action: {action}")

    probe_timeout = float(profile.docker.startup_timeout_seconds)
    cli = _docker_probe("cli", profile.docker.cli_check, context, timeout=probe_timeout)
    compose = _docker_probe(
        "compose", profile.docker.compose_check, context, timeout=probe_timeout
    )
    if action == "status":
        engine = _docker_probe(
            "engine", profile.docker.engine_probe, context, timeout=probe_timeout
        )
        for outcome in (cli, compose, engine):
            if outcome.returncode != 0:
                return outcome.returncode or 1
        return 0

    if cli.returncode != 0:
        return cli.returncode or 1
    if compose.returncode != 0:
        return compose.returncode or 1
    engine = _setup_engine(profile, context)
    if engine.returncode != 0:
        return engine.returncode or 1
    if profile.docker.image_probe is None:
        return 0
    image = _docker_probe(
        "image", profile.docker.image_probe, context, timeout=probe_timeout
    )
    return image.returncode or (0 if image.returncode == 0 else 1)
