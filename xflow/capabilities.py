"""Optional host capabilities used by cockpit command orchestration.

The command runtime deliberately keeps engine-provider discovery separate from
profile command execution.  A provider is started only when a caller opts into
the Docker setup flow and its engine probe has failed.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Mapping, Optional, Tuple


# Keep discovery as a module-level callable so tests and embedders can inject a
# deterministic command lookup without requiring a desktop application.
def which(name: str) -> Optional[str]:
    return shutil.which(name)


_PROVIDERS = {
    "colima": (("colima",), ("start",)),
    "podman": (("podman",), ("machine", "start")),
    "rancher": (("rdctl", "rancher-desktop"), ("start",)),
    "rancher-desktop": (("rdctl", "rancher-desktop"), ("start",)),
    "docker": (("docker",), ("desktop", "start")),
    "desktop": (("docker",), ("desktop", "start")),
    "docker-desktop": (("docker-desktop", "docker"), ("start",)),
}
_DISCOVERY_ORDER = ("colima", "podman", "rancher-desktop", "docker-desktop")


def _provider_command(
    provider: str, env: Mapping[str, str]
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    explicit_command = str(env.get("XFLOW_ENGINE_PROVIDER_COMMAND", "")).strip()
    explicit_args = str(env.get("XFLOW_ENGINE_PROVIDER_ARGS", "")).strip()
    if explicit_command:
        # The command is one executable token.  Optional arguments are parsed
        # into argv with shlex; no value is ever handed to a shell.
        try:
            args = tuple(shlex.split(explicit_args, posix=True)) if explicit_args else ()
        except ValueError:
            return None
        return explicit_command, args

    # An explicitly supplied executable path is also a valid injected
    # provider.  It is still one argv token and is never interpreted by a
    # shell; this is useful for tests and portable host adapters.
    raw_provider = str(
        env.get("XFLOW_ENGINE_PROVIDER")
        or env.get("XFLOW_DOCKER_PROVIDER")
        or env.get("DOCKER_PROVIDER")
        or ""
    ).strip()
    if "/" in raw_provider or "\\" in raw_provider:
        return raw_provider, ()

    definition = _PROVIDERS.get(provider)
    if definition is None:
        return None
    candidates, args = definition
    for candidate in candidates:
        executable = which(candidate)
        if executable:
            if provider == "docker-desktop" and candidate == "docker":
                return executable, ("desktop", "start")
            return executable, args
    return None


def _discover_provider(env: Mapping[str, str]) -> Optional[Tuple[str, Tuple[str, ...]]]:
    requested = str(
        env.get("XFLOW_ENGINE_PROVIDER")
        or env.get("XFLOW_DOCKER_PROVIDER")
        or env.get("DOCKER_PROVIDER")
        or ""
    ).strip().lower()
    if requested and requested not in {"auto", "detect"}:
        return _provider_command(requested, env)
    for provider in _DISCOVERY_ORDER:
        command = _provider_command(provider, env)
        if command is not None:
            return command
    return None


def start_engine_provider(env: Mapping[str, str]) -> bool:
    """Start a detected engine provider, returning whether it accepted start.

    Provider startup is intentionally best-effort and has no effect when no
    provider was explicitly selected or discovered.  The caller owns readiness
    polling; this function only issues one shell-free argv command.
    """

    command = _discover_provider(env)
    if command is None:
        return False
    executable, args = command
    argv = [executable, *args]
    try:
        completed = subprocess.run(
            argv,
            env=dict(env),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0
