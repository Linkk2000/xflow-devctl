from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
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
    CommandOutcome,
    execute_command,
    execute_state,
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
    state: Optional[CommandSpec] = None,
    checks: Sequence[CheckSpec] = (),
    docker: Optional[DockerSpec] = None,
) -> CockpitProfile:
    empty = command((sys.executable, "-c", "pass"))
    return CockpitProfile(
        version=1,
        state_command=state or empty,
        checks=tuple(checks),
        docker=docker or DockerSpec(empty, empty, empty, None, 1),
        dependencies={},
        services={},
        scenarios={},
        playgrounds={},
    )


def test_execute_state_expands_profile_only_and_preserves_runtime_args() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        helper = write_executable(
            root / "state.py",
            "import sys\n"
            "print(repr(sys.argv[1:]))\n"
            "sys.exit(7)\n",
        )
        runner = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(OPS_ROOT)!r})",
                "from xflow.cockpit import CockpitContext",
                "from xflow.commands import execute_state",
                f"from xflow.cockpit import CockpitProfile, CommandSpec, DockerSpec",
                f"root = Path({str(root)!r})",
                f"state = CommandSpec(({sys.executable!r}, {str(helper)!r}), '{{repo}}', {{}})",
                "empty = state",
                "profile = CockpitProfile(1, state, (), DockerSpec(empty, empty, empty, None, 1), {}, {}, {}, {})",
                "context = CockpitContext(root, root.parent, root, Path(sys.executable), root / 'run', dict(os.environ))",
                "raise SystemExit(execute_state(profile, context, ('literal {repo}', '{repo}')))",
            )
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(OPS_ROOT)
        result = subprocess.run(
            [sys.executable, "-c", runner],
            cwd=str(root),
            env=environment,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        assert result.returncode == 7
        assert "['literal {repo}', '{repo}']" in result.stdout
        assert result.stderr == ""


def test_execute_state_prints_local_spawn_diagnostic_and_preserves_code() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        missing = root / "missing state executable"
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            outcome = execute_state(
                profile_for(state=command((str(missing),), env={"API_TOKEN": "secret-value"})),
                make_context(root, {"API_TOKEN": "secret-value"}),
                (),
            )

        assert outcome == 127
        assert "executable not found" in diagnostics.getvalue().lower()
        assert "secret-value" not in diagnostics.getvalue()


def test_execute_command_distinguishes_missing_cwd_from_missing_executable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        missing_cwd = root / "missing cwd"
        outcome = execute_command(
            command((str(root / "missing executable"),), cwd=str(missing_cwd)),
            make_context(root),
            capture=True,
        )

        assert outcome.returncode == 126
        assert "working directory" in outcome.stderr.lower()
        assert "executable not found" not in outcome.stderr.lower()


def test_execute_command_converts_timeout_to_deterministic_outcome() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        helper = write_executable(
            root / "slow.py",
            "import sys, time\n"
            "print('partial', flush=True)\n"
            "time.sleep(10)\n",
        )

        outcome = execute_command(
            command((sys.executable, str(helper))),
            make_context(root),
            capture=True,
            timeout=0.05,
        )

        assert outcome.returncode == 124
        assert outcome.stdout == "partial\n"
        assert "timed out" in outcome.stderr.lower()


def test_execute_command_redacts_timeout_partial_output_and_diagnostic() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        token = "synthetic-api-token"
        helper = write_executable(
            root / "slow-secret.py",
            "import sys, time\n"
            "print(sys.argv[1], flush=True)\n"
            "time.sleep(10)\n",
        )

        outcome = execute_command(
            command((sys.executable, str(helper), "{API_TOKEN}")),
            make_context(root, {"API_TOKEN": token}),
            capture=True,
            timeout=0.05,
        )

        assert outcome.returncode == 124
        assert token not in outcome.stdout
        assert token not in outcome.stderr
        assert "<redacted>" in outcome.stdout
        assert "<redacted>" in outcome.stderr
        assert "timed out" in outcome.stderr.lower()


def test_run_docker_passes_remaining_timeout_to_engine_probe_and_provider() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        calls = []
        cli = command(("cli",), cwd="{repo}")
        compose = command(("compose",), cwd="{repo}")
        engine = command(("engine",), cwd="{repo}")
        docker = DockerSpec(cli, compose, engine, None, 0.5)
        profile = profile_for(docker=docker)
        context = make_context(root)

        def fake_execute(
            spec: CommandSpec,
            _context: CockpitContext,
            *,
            capture: bool = False,
            timeout: Optional[float] = None,
        ) -> CommandOutcome:
            calls.append((spec.argv[0], capture, timeout))
            return CommandOutcome(tuple(spec.argv), 1 if spec is engine else 0, "", "")

        provider_timeouts = []

        def fake_provider(_env: Mapping[str, str], *, timeout: Optional[float] = None) -> bool:
            provider_timeouts.append(timeout)
            return False

        with mock.patch("xflow.commands.execute_command", fake_execute):
            with mock.patch("xflow.commands.start_engine_provider", fake_provider):
                result = run_docker(profile, context, "setup")

        assert result == 1
        assert calls[0][2] is not None
        assert calls[1][2] is not None
        assert calls[2][2] is not None
        assert provider_timeouts and provider_timeouts[0] is not None


def test_start_engine_provider_skips_implicit_discovery_and_handles_timeout() -> None:
    with mock.patch.object(capabilities, "which", side_effect=AssertionError("which called")) as which:
        with mock.patch.object(capabilities.subprocess, "run", side_effect=AssertionError("run called")) as run:
            assert capabilities.start_engine_provider({}) is False
            assert capabilities.start_engine_provider({"XFLOW_ENGINE_PROVIDER": ""}) is False
            assert not which.called
            assert not run.called

    timeout = subprocess.TimeoutExpired(["/fake/provider"], 0.1, output=b"partial", stderr=b"secret")
    with mock.patch.object(capabilities.subprocess, "run", side_effect=timeout):
        assert (
            capabilities.start_engine_provider(
                {"XFLOW_ENGINE_PROVIDER_COMMAND": "/fake/provider"}, timeout=0.1
            )
            is False
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
            "elif mode == 'mismatch': print('wrong but successful')\n"
            "elif mode == 'stderr': print('stderr failure', file=sys.stderr)\n"
            "else: print('ok')\n"
            "sys.exit(0 if mode in ('ok', 'mismatch') else 3)\n",
        )
        checks = (
            CheckSpec("ok", command((str(helper), "ok")), None),
            CheckSpec("stderr", command((str(helper), "stderr")), None),
            CheckSpec("regex", command((str(helper), "regex")), "required-marker"),
            CheckSpec("mismatch", command((str(helper), "mismatch")), "required-marker"),
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
        assert "mismatch" in text

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_preflight(profile, context, warn_only=True)
        assert result == 0
        assert "WARN" in output.getvalue()
        assert "mismatch" in output.getvalue()


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

        def start_provider(
            _env: Mapping[str, str], *, timeout: Optional[float] = None
        ) -> bool:
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
        with mock.patch(
            "xflow.commands.start_engine_provider",
            lambda _env, *, timeout=None: False,
        ):
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
        test_execute_state_expands_profile_only_and_preserves_runtime_args,
        test_execute_state_prints_local_spawn_diagnostic_and_preserves_code,
        test_execute_command_distinguishes_missing_cwd_from_missing_executable,
        test_execute_command_converts_timeout_to_deterministic_outcome,
        test_execute_command_redacts_timeout_partial_output_and_diagnostic,
        test_run_preflight_aggregates_failures_and_warn_only_changes_exit_code,
        test_run_docker_status_probes_cli_compose_and_engine_in_order,
        test_run_docker_setup_starts_provider_then_polls_before_image_probe,
        test_run_docker_setup_stops_before_optional_image_when_engine_not_ready,
        test_run_docker_passes_remaining_timeout_to_engine_probe_and_provider,
        test_start_engine_provider_uses_injected_command_discovery,
        test_start_engine_provider_skips_implicit_discovery_and_handles_timeout,
    )
    for test in tests:
        test()
    print("cockpit commands ok")


if __name__ == "__main__":
    main()
