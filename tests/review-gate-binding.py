from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


OPS_ROOT = Path(__file__).resolve().parents[1]
GATE = OPS_ROOT / "tests" / "review-gate.sh"


def run_gate(
    *,
    test_python: Optional[str],
    devctl_python: Optional[str],
    path: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"PATH": path})
    environment.pop("TEST_PYTHON", None)
    environment.pop("DEVCTL_PYTHON", None)
    if test_python is not None:
        environment["TEST_PYTHON"] = test_python
    if devctl_python is not None:
        environment["DEVCTL_PYTHON"] = devctl_python
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
    assert "REVIEW_GATE_PREFLIGHT label=TEST_PYTHON" in output
    assert "REVIEW_GATE_PREFLIGHT label=DEVCTL_PYTHON" in output


def assert_gate_rejects_explicit_interpreter(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0
    assert "review gate ok" not in result.stdout + result.stderr
    assert "not executable or not on PATH" in result.stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        wrapper = Path(raw) / "python override with spaces"
        wrapper_contents = (
            "#!/bin/sh\n"
            "exec "
            + shlex.quote(sys.executable)
            + ' "$@"\n'
        )
        wrapper.write_text(wrapper_contents, encoding="utf-8")
        wrapper.chmod(0o755)
        fallback = Path(raw) / "python3"
        fallback.write_text(wrapper_contents, encoding="utf-8")
        fallback.chmod(0o755)
        restricted_path = "/usr/bin:/bin"

        for test_python, devctl_python in (
            (str(wrapper), str(wrapper)),
            (str(wrapper), None),
            (None, str(wrapper)),
        ):
            assert_gate_passes(
                run_gate(
                    test_python=test_python,
                    devctl_python=devctl_python,
                    path=restricted_path,
                )
            )

        assert_gate_passes(
            run_gate(
                test_python=None,
                devctl_python=None,
                path=str(Path(raw)) + os.pathsep + restricted_path,
            )
        )

        assert_gate_rejects_explicit_interpreter(
            run_gate(
                test_python=str(wrapper),
                devctl_python=str(Path(raw) / "missing launcher"),
                path=str(Path(raw)) + os.pathsep + restricted_path,
            )
        )

        assert_gate_rejects_explicit_interpreter(
            run_gate(
                test_python=str(Path(raw) / "missing helper"),
                devctl_python=str(wrapper),
                path=str(Path(raw)) + os.pathsep + restricted_path,
            )
        )

    print("review-gate binding ok")


if __name__ == "__main__":
    main()
