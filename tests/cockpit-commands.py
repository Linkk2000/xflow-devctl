from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence
from unittest import mock


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow import capabilities  # noqa: E402
from xflow.cockpit import (  # noqa: E402
    CheckSpec,
    CockpitContext,
    CockpitProfile,
    CommandSpec,
    DockerSpec,
)
from xflow.commands import (  # noqa: E402
    execute_command,
    run_docker,
    run_preflight,
)


def write_executable(path: Path, text: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_context(root: Path, env: Optional[Mapping[str, str]] = None) -> CockpitContext:
    values = dict(os.environ)
    if env:
        values.update(env)
    return CockpitContext(
        cockpit_root=root,
        workspace_root=root.parent,
        repo_root=root,
        python_executable=Path(sys.executable),
        run_dir=root / ".xflow" / "run",
        env=values,
    )


def command(argv: Sequence[str], *, cwd: str = "{repo}", env: Optional[Mapping[str, str]] = None) -> CommandSpec:
    return CommandSpec(tuple(argv), cwd, dict(env or {}))


def profile_for(
    *,
    checks: Sequence[CheckSpec] = (),
    docker: Optional[DockerSpec] = None,
) -> CockpitProfile:
    empty = command((sys.executable, "-c", "pass"))
    return CockpitProfile(
        version=1,
        state_command=empty,
        checks=tuple(checks),
        docker=docker or DockerSpec(empty, empty, empty, None, 1),
        dependencies={},
        services={},
        scenarios={},
        playgrounds={},
    )


def test_execute_command_preserves_arguments_cwd_and_allowed_environment() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "workspace with spaces"
        root.mkdir()
        root = root.resolve()
        helper = write_executable(
            root / "helper.py",
            "import os, sys\n"
            "print(sys.argv[1])\n"
            "print(os.getcwd())\n"
            "print(os.environ['VISIBLE'])\n",
        )
        spec = command(
            (str(helper), "value with spaces"),
            cwd="{repo}",
            env={"VISIBLE": "{VISIBLE}"},
        )

        outcome = execute_command(
            spec,
            make_context(root, {"VISIBLE": "forwarded value"}),
            capture=True,
        )

        assert outcome.argv == (str(helper), "value with spaces")
        assert outcome.returncode == 0
        assert outcome.stdout == "value with spaces\n" + str(root) + "\nforwarded value\n"
        assert outcome.stderr == ""


def test_execute_command_reports_missing_executable_without_shell() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        missing = root / "missing executable"
        outcome = execute_command(
            command((str(missing),), cwd="{repo}", env={"API_TOKEN": "do-not-print"}),
            make_context(root, {"API_TOKEN": "secret-token-value"}),
            capture=True,
        )

        assert outcome.returncode == 127
        assert "not found" in outcome.stderr.lower()
        assert "secret-token-value" not in outcome.stderr


def test_run_preflight_aggregates_failures_and_warn_only_changes_exit_code() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        helper = write_executable(
            root / "check.py",
            "import sys\n"
            "mode = sys.argv[1]\n"
            "if mode == 'regex': print('unexpected')\n"
            "elif mode == 'stderr': print('stderr failure', file=sys.stderr)\n"
            "else: print('ok')\n"
            "sys.exit(0 if mode == 'ok' else 3)\n",
        )
        checks = (
            CheckSpec("ok", command((str(helper), "ok")), None),
            CheckSpec("stderr", command((str(helper), "stderr")), None),
            CheckSpec("regex", command((str(helper), "regex")), "required-marker"),
        )
        profile = profile_for(checks=checks)
        context = make_context(root)

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_preflight(profile, context, warn_only=False)
        assert result == 1
        text = output.getvalue()
        assert "ok" in text
        assert "stderr" in text
        assert "regex" in text

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_preflight(profile, context, warn_only=True)
        assert result == 0
        assert "WARN" in output.getvalue()


def test_run_docker_status_probes_cli_compose_and_engine_in_order() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        log = root / "probes.log"
        helper = write_executable(
            root / "probe.py",
            "import os, sys\n"
            "with open(os.environ['PROBE_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(sys.argv[1] + '\\n')\n",
        )
        context = make_context(root, {"PROBE_LOG": str(log)})
        docker = DockerSpec(
            command((str(helper), "cli"), env={"PROBE_LOG": "{PROBE_LOG}"}),
            command((str(helper), "compose"), env={"PROBE_LOG": "{PROBE_LOG}"}),
            command((str(helper), "engine"), env={"PROBE_LOG": "{PROBE_LOG}"}),
            command((str(helper), "image"), env={"PROBE_LOG": "{PROBE_LOG}"}),
            1,
        )

        result = run_docker(profile_for(docker=docker), context, "status")

        assert result == 0
        assert log.read_text(encoding="utf-8").splitlines() == ["cli", "compose", "engine"]


def test_run_docker_setup_starts_provider_then_polls_before_image_probe() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        log = root / "probes.log"
        count = root / "engine.count"
        helper = write_executable(
            root / "probe.py",
            "import os, sys\n"
            "name = sys.argv[1]\n"
            "with open(os.environ['PROBE_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(name + '\\n')\n"
            "if name == 'engine':\n"
            "    count_path = os.environ['ENGINE_COUNT']\n"
            "    try: count = int(open(count_path, encoding='utf-8').read())\n"
            "    except FileNotFoundError: count = 0\n"
            "    open(count_path, 'w', encoding='utf-8').write(str(count + 1))\n"
            "    sys.exit(0 if count else 1)\n",
        )
        context = make_context(root, {"PROBE_LOG": str(log), "ENGINE_COUNT": str(count)})
        env = {"PROBE_LOG": "{PROBE_LOG}", "ENGINE_COUNT": "{ENGINE_COUNT}"}
        docker = DockerSpec(
            command((str(helper), "cli"), env=env),
            command((str(helper), "compose"), env=env),
            command((str(helper), "engine"), env=env),
            command((str(helper), "image"), env=env),
            1,
        )
        provider_calls = []

        def start_provider(_env: Mapping[str, str]) -> bool:
            provider_calls.append("provider")
            with log.open("a", encoding="utf-8") as stream:
                stream.write("provider\n")
            return True

        with mock.patch("xflow.commands.start_engine_provider", start_provider):
            result = run_docker(profile_for(docker=docker), context, "setup")

        assert result == 0
        assert provider_calls == ["provider"]
        assert log.read_text(encoding="utf-8").splitlines() == [
            "cli",
            "compose",
            "engine",
            "provider",
            "engine",
            "image",
        ]


def test_run_docker_setup_stops_before_optional_image_when_engine_not_ready() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        log = root / "probes.log"
        helper = write_executable(
            root / "probe.py",
            "import os, sys\n"
            "with open(os.environ['PROBE_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(sys.argv[1] + '\\n')\n"
            "sys.exit(1 if sys.argv[1] == 'engine' else 0)\n",
        )
        context = make_context(root, {"PROBE_LOG": str(log)})
        env = {"PROBE_LOG": "{PROBE_LOG}"}
        docker = DockerSpec(
            command((str(helper), "cli"), env=env),
            command((str(helper), "compose"), env=env),
            command((str(helper), "engine"), env=env),
            command((str(helper), "image"), env=env),
            1,
        )
        with mock.patch("xflow.commands.start_engine_provider", lambda _env: False):
            result = run_docker(profile_for(docker=docker), context, "setup")

        assert result != 0
        assert "image" not in log.read_text(encoding="utf-8").splitlines()


def test_start_engine_provider_uses_injected_command_discovery() -> None:
    completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with mock.patch.object(
        capabilities,
        "which",
        side_effect=lambda name: "/fake/colima" if name == "colima" else None,
    ):
        with mock.patch.object(capabilities.subprocess, "run", return_value=completed) as run:
            assert capabilities.start_engine_provider({"XFLOW_ENGINE_PROVIDER": "colima"}) is True

    args, kwargs = run.call_args
    assert args[0] == ["/fake/colima", "start"]
    assert kwargs["shell"] is False


def main() -> None:
    tests: Sequence[Callable[[], None]] = (
        test_execute_command_preserves_arguments_cwd_and_allowed_environment,
        test_execute_command_reports_missing_executable_without_shell,
        test_run_preflight_aggregates_failures_and_warn_only_changes_exit_code,
        test_run_docker_status_probes_cli_compose_and_engine_in_order,
        test_run_docker_setup_starts_provider_then_polls_before_image_probe,
        test_run_docker_setup_stops_before_optional_image_when_engine_not_ready,
        test_start_engine_provider_uses_injected_command_discovery,
    )
    for test in tests:
        test()
    print("cockpit commands ok")


if __name__ == "__main__":
    main()
