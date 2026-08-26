from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable


OPS_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = OPS_ROOT / "devctl"


def write_fake_python(directory: Path, name: str, *, version_ok: bool) -> tuple[Path, Path]:
    executable = directory / name
    log = directory / f"{name}.log"
    status = "0" if version_ok else "1"
    log.write_text("", encoding="utf-8")
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> {shlex.quote(str(log))}\n"
        f"printf '\\n' >> {shlex.quote(str(log))}\n"
        f"if [ \"${{1:-}}\" = '-c' ]; then exit {status}; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def run_launcher(env: dict[str, str], args: Iterable[str]) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.pop("DEVCTL_PYTHON", None)
    process_env.update(env)
    return subprocess.run(
        [str(LAUNCHER), *args],
        cwd=OPS_ROOT,
        env=process_env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def invocations(log: Path) -> list[tuple[str, ...]]:
    if not log.exists():
        return []
    return [tuple(block.splitlines()) for block in log.read_text(encoding="utf-8").strip().split("\n\n") if block]


def test_explicit_interpreter_override_wins(root: Path) -> None:
    override, override_log = write_fake_python(root, "override", version_ok=True)
    _, python3_log = write_fake_python(root, "python3", version_ok=True)
    _, python_log = write_fake_python(root, "python", version_ok=True)
    result = run_launcher(
        {
            "DEVCTL_PYTHON": str(override),
            "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
        },
        ("issue", "show", "IK3RR6"),
    )
    assert result.returncode == 0, result.stderr
    override_invocations = invocations(override_log)
    assert override_invocations and override_invocations[-1] == ("-m", "xflow", "issue", "show", "IK3RR6")
    assert invocations(python3_log) == []
    assert invocations(python_log) == []


def test_python3_precedes_python(root: Path) -> None:
    _, python3_log = write_fake_python(root, "python3", version_ok=True)
    _, python_log = write_fake_python(root, "python", version_ok=True)
    result = run_launcher(
        {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"},
        ("issue", "show", "IK3RR6"),
    )
    assert result.returncode == 0, result.stderr
    python3_invocations = invocations(python3_log)
    assert python3_invocations and python3_invocations[-1] == ("-m", "xflow", "issue", "show", "IK3RR6")
    assert invocations(python_log) == []


def test_old_python3_is_rejected_before_falling_back_to_python(root: Path) -> None:
    _, python3_log = write_fake_python(root, "python3", version_ok=False)
    _, python_log = write_fake_python(root, "python", version_ok=True)
    result = run_launcher(
        {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"},
        ("issue", "show", "IK3RR6"),
    )
    assert result.returncode == 0, result.stderr
    assert invocations(python3_log) == [("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")]
    python_invocations = invocations(python_log)
    assert python_invocations and python_invocations[-1] == ("-m", "xflow", "issue", "show", "IK3RR6")


def test_invalid_explicit_override_falls_back_to_python3(root: Path) -> None:
    missing_override = root / "missing interpreter"
    _, python3_log = write_fake_python(root, "python3", version_ok=True)
    _, python_log = write_fake_python(root, "python", version_ok=True)
    args = ("issue", "show", "IK3RR6", "--format", "json")
    result = run_launcher(
        {
            "DEVCTL_PYTHON": str(missing_override),
            "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
        },
        args,
    )
    assert result.returncode == 0, result.stderr
    assert invocations(python3_log) == [
        ("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"),
        ("-m", "xflow", *args),
    ]
    assert invocations(python_log) == []


def test_rejects_candidates_below_python_39(root: Path) -> None:
    _, python3_log = write_fake_python(root, "python3", version_ok=False)
    _, python_log = write_fake_python(root, "python", version_ok=False)
    result = run_launcher(
        {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"},
        ("issue", "show", "IK3RR6"),
    )
    assert result.returncode == 1
    assert "Python 3.9+" in result.stderr
    assert invocations(python3_log) == [("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")]
    assert invocations(python_log) == [("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")]


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        test_explicit_interpreter_override_wins(root)
        test_python3_precedes_python(root)
        test_old_python3_is_rejected_before_falling_back_to_python(root)
        test_invalid_explicit_override_falls_back_to_python3(root)
        test_rejects_candidates_below_python_39(root)
    print("POSIX launcher ok")


if __name__ == "__main__":
    main()
