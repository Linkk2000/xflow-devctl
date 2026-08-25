from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.cli import build_parser


def powershell_executable() -> Optional[str]:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_posix_suite_does_not_require_powershell() -> None:
    source = (OPS_ROOT / "tests" / "entrypoint-routing.py").read_text(encoding="utf-8")
    assert '"powershell"' not in source
    assert '"pwsh"' not in source


def test_env() -> dict[str, str]:
    return {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(OPS_ROOT),
    }


def run_powershell(
    executable: str,
    cwd: Path,
    *args: str,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=cwd,
        env=test_env() if env is None else env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def assert_powershell_help_alias(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "help")
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError("devctl.ps1 help must succeed")
    assert "AI call recipes" in result.stdout


def assert_check_commands_are_discoverable(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "check", "--help")
    assert result.returncode == 0, result.stderr
    assert "dependencies" in result.stdout, result.stdout
    assert "commit-msg" in result.stdout, result.stdout
    assert "classification" in result.stdout, result.stdout


def assert_unattended_commands_are_discoverable(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "unattended", "--help")
    assert result.returncode == 0, result.stderr
    for name in ("enable", "status", "disable"):
        assert name in result.stdout, (name, result.stdout)


def assert_task_commands_are_discoverable(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "task", "--help")
    assert result.returncode == 0, result.stderr
    for name in ("activate", "status", "list", "migrate-current"):
        assert name in result.stdout, (name, result.stdout)


def assert_contract_commands_are_discoverable(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "contract", "--help")
    assert result.returncode == 0, result.stderr
    for name in ("lint", "accept"):
        assert name in result.stdout, (name, result.stdout)


def assert_trace_commands_are_discoverable(executable: str) -> None:
    result = run_powershell(executable, OPS_ROOT, "-File", str(OPS_ROOT / "devctl.ps1"), "trace", "--help")
    assert result.returncode == 0, result.stderr
    assert "check" in result.stdout, result.stdout


def assert_issue_workspace_migration_is_discoverable(executable: str) -> None:
    result = run_powershell(
        executable,
        OPS_ROOT,
        "-File",
        str(OPS_ROOT / "devctl.ps1"),
        "migrate",
        "issue-workspace",
        "--help",
    )
    assert result.returncode == 0, result.stderr
    assert "issue-workspace" in result.stdout, result.stdout
    assert "--mode {tracked,local}" in result.stdout, result.stdout


def assert_contract_acceptance_recipes_match_parser() -> None:
    parser = build_parser()
    prefixes = ("devctl ", ".\\devctl.ps1 ")
    for path in (OPS_ROOT / "README.md", OPS_ROOT / "help.txt"):
        pending = None
        pairs = 0
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            prefix = next((item for item in prefixes if line.startswith(item)), None)
            if prefix is None:
                continue
            command_text = line[len(prefix) :]
            is_prepare = command_text.startswith("approval prepare ") and "--action contract-acceptance" in command_text
            is_accept = pending is not None and command_text.startswith("contract accept ")
            if not is_prepare and not is_accept:
                continue
            args = parser.parse_args(shlex.split(command_text))
            if is_prepare:
                assert pending is None, (path, line)
                assert args.objects, (path, line)
                pending = (args, line)
                continue
            prepare, prepare_line = pending
            assert args.command == "contract" and args.contract_command == "accept", (path, line)
            assert prepare.issue == args.issue, (path, prepare_line, line)
            assert prepare.file == args.file, (path, prepare_line, line)
            assert prepare.objects == args.objects, (path, prepare_line, line)
            pending = None
            pairs += 1
        assert pending is None, (path, pending)
        assert pairs == 3, (path, pairs)


def run_powershell_devctl(executable: str, repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **test_env(),
        "DEVCTL_REPO_ROOT": str(repo_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = run_powershell(executable, repo_root, "-File", str(OPS_ROOT / "devctl.ps1"), *args, env=env)
    assert result.returncode == 0, result.stderr
    return result


def assert_unattended_lifecycle_routing(executable: str, root: Path) -> None:
    repo = root / "unattended-routing"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    inactive = run_powershell_devctl(executable, repo, "unattended", "status")
    assert "inactive" in inactive.stdout.lower()

    enabled = run_powershell_devctl(
        executable,
        repo,
        "unattended",
        "enable",
        "--issue",
        "IK152D",
        "--confirm",
        "XFLOW_HUMAN_UNATTENDED_ALL",
    )
    assert "enabled" in enabled.stdout.lower()
    status = run_powershell_devctl(executable, repo, "unattended", "status")
    assert "active" in status.stdout.lower()
    assert "IK152D" in status.stdout
    disabled = run_powershell_devctl(executable, repo, "unattended", "disable")
    assert "disabled" in disabled.stdout.lower()


def run_all_powershell_assertions(executable: str) -> None:
    assert_powershell_help_alias(executable)
    assert_check_commands_are_discoverable(executable)
    assert_unattended_commands_are_discoverable(executable)
    assert_task_commands_are_discoverable(executable)
    assert_contract_commands_are_discoverable(executable)
    assert_trace_commands_are_discoverable(executable)
    assert_issue_workspace_migration_is_discoverable(executable)
    assert_contract_acceptance_recipes_match_parser()

    with tempfile.TemporaryDirectory() as raw:
        assert_unattended_lifecycle_routing(executable, Path(raw))


def main() -> int:
    test_posix_suite_does_not_require_powershell()
    executable = powershell_executable()
    if executable is None:
        print("SKIP: PowerShell capability unavailable")
        return 77
    run_all_powershell_assertions(executable)
    print("PowerShell entrypoint routing ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
