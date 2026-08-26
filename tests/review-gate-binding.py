from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
GATE = OPS_ROOT / "tests" / "review-gate.sh"


def run_gate(
    wrapper: Path,
    *,
    test_python: bool,
    devctl_python: bool,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"PATH": "/usr/bin:/bin"})
    environment.pop("TEST_PYTHON", None)
    environment.pop("DEVCTL_PYTHON", None)
    if test_python:
        environment["TEST_PYTHON"] = str(wrapper)
    if devctl_python:
        environment["DEVCTL_PYTHON"] = str(wrapper)
    return subprocess.run(
        ["/bin/bash", str(GATE)],
        cwd=OPS_ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def assert_gate_passes(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout
    output = result.stdout + result.stderr
    assert "review gate ok" in output
    assert "python: command not found" not in output


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        wrapper = Path(raw) / "python override with spaces"
        wrapper.write_text(
            "#!/bin/sh\n"
            "exec "
            + shlex.quote(sys.executable)
            + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        for test_python, devctl_python in ((True, True), (True, False), (False, True)):
            assert_gate_passes(
                run_gate(
                    wrapper,
                    test_python=test_python,
                    devctl_python=devctl_python,
                )
            )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": "/usr/bin:/bin",
                "TEST_PYTHON": str(Path(raw) / "missing helper"),
                "DEVCTL_PYTHON": str(Path(raw) / "missing launcher"),
            }
        )
        result = subprocess.run(
            ["/bin/bash", str(GATE)],
            cwd=OPS_ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode != 0
        assert "not executable or not on PATH" in result.stderr

    print("review-gate binding ok")


if __name__ == "__main__":
    main()
