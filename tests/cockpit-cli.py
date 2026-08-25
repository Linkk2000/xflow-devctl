from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock


OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(OPS_ROOT))

from tests.support import write_text_lf  # noqa: E402
from xflow import cli  # noqa: E402


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repo(path: Path, name: str) -> Path:
    result = path / name
    result.mkdir(parents=True)
    _git(result, "init", "-q")
    _git(result, "config", "user.email", "test@example.com")
    _git(result, "config", "user.name", "Test User")
    write_text_lf(result / "README.md", "# fixture\n")
    _git(result, "add", "README.md")
    _git(result, "commit", "-m", "fixture", "-q")
    return result


def _run(cockpit: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DEVCTL_REPO_ROOT": str(cockpit),
            "DEVCTL_SKIP_PROVIDER_LOAD": "1",
            "PYTHONPATH": str(OPS_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PROFILE_MODE": "test",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "xflow", *args],
        cwd=cockpit,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != expect:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"expected exit {expect}: {' '.join(args)}")
    return result


def _fixture() -> tuple[Path, Path, Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="xflow-cockpit-cli-"))
    workspace = root / "workspace"
    cockpit = workspace / "xflow-spec"
    profile = cockpit / ".xflow" / "cockpit.yaml"
    cockpit.mkdir(parents=True)
    profile.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "cockpit-profile.yaml", profile)
    state = cockpit / "_ops" / "portable" / "state.py"
    state.parent.mkdir(parents=True)
    write_text_lf(
        state,
        "import sys\n"
        "print('fixture-state:' + '|'.join(sys.argv[1:]))\n",
    )
    web = _repo(workspace, "xflow-web")
    _repo(workspace, "xflow-server")
    return root, workspace, cockpit, profile


def test_profile_repo_state_and_alias_routes() -> None:
    root, workspace, cockpit, profile = _fixture()
    try:
        status = _run(
            cockpit,
            "--profile",
            str(profile),
            "--cockpit-root",
            str(cockpit),
            "--repo",
            "xflow-web",
            "git",
            "status",
        )
        assert "branch:" in status.stdout

        state = _run(
            cockpit,
            "--profile",
            str(profile),
            "--cockpit-root",
            str(cockpit),
            "--repo",
            "xflow-web",
            "state",
        )
        assert "fixture-state:" in state.stdout
        state_show = _run(
            cockpit,
            "--profile",
            str(profile),
            "--repo",
            "xflow-web",
            "state",
            "show",
            "--json",
        )
        assert "fixture-state:show|--json" in state_show.stdout

        selected: list[tuple[str, bool]] = []
        with mock.patch.object(cli, "run_playground", side_effect=lambda _p, _c, target, opened: selected.append((target, opened)) or 0):
            assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "pg", "f", "--no-browser"]) == 0
            assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "playground", "bpmn", "--no-browser"]) == 0
        assert selected == [("f", False), ("bpmn", False)]
    finally:
        shutil.rmtree(root)


def test_repo_resolution_rejects_non_sibling_and_missing_repositories() -> None:
    root, _workspace, cockpit, profile = _fixture()
    try:
        outside = root / "outside"
        outside.mkdir()
        result = _run(
            cockpit,
            "--profile",
            str(profile),
            "--repo",
            str(outside),
            "git",
            "status",
            expect=1,
        )
        assert "repository" in result.stderr.lower()
        missing = _run(
            cockpit,
            "--profile",
            str(profile),
            "--repo",
            "not-declared",
            "git",
            "status",
            expect=1,
        )
        assert "repository" in missing.stderr.lower()
    finally:
        shutil.rmtree(root)


def test_repo_resolution_honors_literal_profile_siblings() -> None:
    root, workspace, cockpit, profile = _fixture()
    try:
        _repo(workspace, "xflow-other")
        profile_text = profile.read_text(encoding="utf-8")
        profile_text = profile_text.replace('cwd: "{cockpit}"', 'cwd: "{workspace}/xflow-web"', 1)
        write_text_lf(profile, profile_text)
        result = _run(
            cockpit,
            "--profile",
            str(profile),
            "--repo",
            "xflow-other",
            "git",
            "status",
            expect=1,
        )
        assert "not declared" in result.stderr
    finally:
        shutil.rmtree(root)


def test_unsupported_command_fails_before_profile_or_process_side_effect() -> None:
    root, _workspace, cockpit, _profile = _fixture()
    try:
        marker = cockpit / "unsupported.marker"
        result = _run(
            cockpit,
            "--profile",
            str(_profile),
            "dev",
            "unsupported",
            expect=2,
        )
        assert "unsupported" in result.stderr
        assert not marker.exists()
    finally:
        shutil.rmtree(root)


def test_existing_issue_list_route_is_preserved() -> None:
    root, _workspace, cockpit, profile = _fixture()
    try:
        with mock.patch.object(cli.providers, "list_issues", return_value=[]) as listed:
            assert (
                cli.main(
                    [
                        "--profile",
                        str(profile),
                        "--repo",
                        "xflow-web",
                        "issue",
                        "list",
                        "--limit",
                        "1",
                    ]
                )
                == 0
            )
        listed.assert_called_once()
    finally:
        shutil.rmtree(root)


def main() -> None:
    test_profile_repo_state_and_alias_routes()
    test_repo_resolution_rejects_non_sibling_and_missing_repositories()
    test_repo_resolution_honors_literal_profile_siblings()
    test_unsupported_command_fails_before_profile_or_process_side_effect()
    test_existing_issue_list_route_is_preserved()
    print("cockpit CLI ok")


if __name__ == "__main__":
    main()
