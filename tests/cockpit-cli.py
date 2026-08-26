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


def _run(
    cockpit: Path,
    *args: str,
    expect: int = 0,
    env_updates: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_updates:
        for key, value in env_updates.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
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
        state_after_globals = _run(
            cockpit,
            "state",
            "--profile",
            str(profile),
            "--repo",
            "xflow-web",
            "--json",
        )
        assert "fixture-state:show|--json" in state_after_globals.stdout
        state_equals_globals = _run(
            cockpit,
            "--profile=" + str(profile),
            "--cockpit-root=" + str(cockpit),
            "--repo=xflow-web",
            "state",
        )
        assert "fixture-state:show" in state_equals_globals.stdout

        cwd_discovered = _run(cockpit, "--repo", "xflow-web", "git", "status")
        assert "branch:" in cwd_discovered.stdout

        selected: list[tuple[str, bool]] = []
        with mock.patch.object(cli, "run_playground", side_effect=lambda _p, _c, target, opened: selected.append((target, opened)) or 0):
            assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "pg", "f", "--no-browser"]) == 0
            assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "playground", "bpmn", "--no-browser"]) == 0
        assert selected == [("f", False), ("bpmn", False)]
    finally:
        shutil.rmtree(root)


def test_omitted_playground_target_uses_profile_default() -> None:
    root, _workspace, cockpit, profile = _fixture()
    try:
        profile_text = profile.read_text(encoding="utf-8").replace(
            "defaultPlayground: flowable", "defaultPlayground: warmflow"
        )
        write_text_lf(profile, profile_text)
        selected: list[tuple[str | None, bool]] = []
        with mock.patch.object(
            cli,
            "run_playground",
            side_effect=lambda _p, _c, target, opened: selected.append((target, opened)) or 0,
        ):
            assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "pg", "--no-browser"]) == 0
        assert selected == [("warmflow", False)]
        assert cli._ACTIVE_COCKPIT_PROFILE is None
    finally:
        shutil.rmtree(root)


def test_legacy_command_without_globals_does_not_require_profile() -> None:
    root = Path(tempfile.mkdtemp(prefix="xflow-legacy-cli-"))
    try:
        repo = _repo(root, "plain")
        result = _run(repo, "git", "status")
        assert "branch:" in result.stdout
    finally:
        shutil.rmtree(root)


def test_unknown_option_fails_closed_for_legacy_command() -> None:
    root, _workspace, cockpit, _profile = _fixture()
    try:
        result = _run(cockpit, "git", "status", "--typo", expect=2)
        assert "unrecognized arguments" in result.stderr
    finally:
        shutil.rmtree(root)


def test_target_project_env_is_loaded_after_repo_resolution() -> None:
    root, workspace, cockpit, profile = _fixture()
    try:
        local = cockpit / ".xflow" / "local"
        local.mkdir(parents=True, exist_ok=True)
        write_text_lf(local / "env.local", "TARGET_MARKER=cockpit\nXFLOW_PLATFORM=cockpit\n")
        target = workspace / "xflow-web" / ".xflow" / "local"
        target.mkdir(parents=True, exist_ok=True)
        write_text_lf(target / "env.local", "TARGET_MARKER=target\nXFLOW_PLATFORM=target\n")

        profile_text = profile.read_text(encoding="utf-8")
        profile_text = profile_text.replace(
            "  - PROFILE_MODE\n",
            "  - PROFILE_MODE\n  - TARGET_MARKER\n  - XFLOW_PLATFORM\n",
        )
        profile_text = profile_text.replace(
            "      PROFILE_MODE: \"{PROFILE_MODE}\"\n",
            "      PROFILE_MODE: \"{PROFILE_MODE}\"\n"
            "      TARGET_MARKER: \"{TARGET_MARKER}\"\n"
            "      XFLOW_PLATFORM: \"{XFLOW_PLATFORM}\"\n",
        )
        write_text_lf(profile, profile_text)
        state = cockpit / "_ops" / "portable" / "state.py"
        write_text_lf(
            state,
            "import os\n"
            "import sys\n"
            "print('fixture-env:' + os.environ.get('TARGET_MARKER', '') + ':' + os.environ.get('XFLOW_PLATFORM', '') + '|' + '|'.join(sys.argv[1:]))\n",
        )

        target_result = _run(
            cockpit,
            "--profile",
            str(profile),
            "--cockpit-root",
            str(cockpit),
            "--repo",
            "xflow-web",
            "state",
            env_updates={"TARGET_MARKER": None, "XFLOW_PLATFORM": None},
        )
        assert "fixture-env:target:target|" in target_result.stdout

        host_result = _run(
            cockpit,
            "--profile",
            str(profile),
            "--cockpit-root",
            str(cockpit),
            "--repo",
            "xflow-web",
            "state",
            env_updates={"TARGET_MARKER": "host", "XFLOW_PLATFORM": "host"},
        )
        assert "fixture-env:host:host|" in host_result.stdout
    finally:
        shutil.rmtree(root)


def test_discovered_profile_symlink_cannot_escape_cockpit_root() -> None:
    root, _workspace, cockpit, _profile = _fixture()
    try:
        outside = root / "outside"
        outside.mkdir()
        outside_profile = outside / "cockpit.yaml"
        shutil.copyfile(FIXTURES / "cockpit-profile.yaml", outside_profile)
        profile = cockpit / ".xflow" / "cockpit.yaml"
        profile.unlink()
        profile.symlink_to(outside_profile)
        result = _run(cockpit, "--cockpit-root", str(cockpit), "state", expect=1)
        assert "escapes" in result.stderr.lower() or "under cockpit root" in result.stderr.lower()
        assert "fixture-state:" not in result.stdout
    finally:
        shutil.rmtree(root)


def test_explicit_profile_root_wins_over_environment_root() -> None:
    root, _workspace, cockpit, profile = _fixture()
    try:
        nested = cockpit / "nested"
        nested_profile = nested / ".xflow" / "cockpit.yaml"
        nested_profile.parent.mkdir(parents=True)
        shutil.copyfile(profile, nested_profile)
        nested_state = nested / "_ops" / "portable" / "state.py"
        nested_state.parent.mkdir(parents=True)
        write_text_lf(
            nested_state,
            "import sys\n"
            "print('nested-state:' + '|'.join(sys.argv[1:]))\n",
        )

        result = _run(
            cockpit,
            "--profile",
            str(nested_profile),
            "state",
            env_updates={"XFLOW_COCKPIT_ROOT": str(cockpit)},
        )
        assert "nested-state:show" in result.stdout
    finally:
        shutil.rmtree(root)


def test_explicit_empty_cockpit_root_does_not_fallback_to_cwd_profile() -> None:
    root, _workspace, cockpit, _profile = _fixture()
    try:
        empty = root / "empty-cockpit"
        empty.mkdir()
        result = _run(
            cockpit,
            "--cockpit-root",
            str(empty),
            "state",
            expect=1,
            env_updates={
                "XFLOW_PROFILE": None,
                "XFLOW_COCKPIT_ROOT": None,
                "DEVCTL_COCKPIT_ROOT": None,
                "XFLOW_COCKPIT": None,
            },
        )
        assert "profile" in result.stderr.lower()
        assert "fixture-state:" not in result.stdout
    finally:
        shutil.rmtree(root)


def test_explicit_missing_profile_does_not_fallback_to_cwd_profile() -> None:
    root, _workspace, cockpit, _profile = _fixture()
    try:
        missing = root / "missing-profile.yaml"
        result = _run(
            cockpit,
            "state",
            expect=1,
            env_updates={
                "XFLOW_PROFILE": str(missing),
                "XFLOW_COCKPIT_ROOT": None,
                "DEVCTL_COCKPIT_ROOT": None,
                "XFLOW_COCKPIT": None,
            },
        )
        assert "profile not found" in result.stderr
        assert "fixture-state:" not in result.stdout
    finally:
        shutil.rmtree(root)


def test_parser_project_env_defaults_are_resolved_after_loading() -> None:
    root, workspace, cockpit, profile = _fixture()
    release_tag_key = "XFLOW_GITHUB_ATTACHMENT_RELEASE_TAG"
    try:
        target_local = workspace / "xflow-web" / ".xflow" / "local"
        target_local.mkdir(parents=True, exist_ok=True)
        write_text_lf(target_local / "env.local", f"{release_tag_key}=target-tag\n")

        def capture(argv: list[str], host_tag: str | None = None) -> str:
            previous = os.environ.pop(release_tag_key, None)
            env = {
                "DEVCTL_REPO_ROOT": str(cockpit),
                "DEVCTL_SKIP_PROVIDER_LOAD": "1",
                "PYTHONPATH": str(OPS_ROOT),
            }
            if host_tag is not None:
                env[release_tag_key] = host_tag
            captured: list[str] = []
            try:
                with mock.patch.dict(os.environ, env, clear=False):
                    with mock.patch.object(
                        cli,
                        "run_attachment",
                        side_effect=lambda args: captured.append(args.release_tag) or 0,
                    ):
                        assert cli.main(argv) == 0
                    return captured[-1]
            finally:
                os.environ.pop(release_tag_key, None)
                if previous is not None:
                    os.environ[release_tag_key] = previous

        target_default = capture(
            [
                "--profile",
                str(profile),
                "--repo",
                "xflow-web",
                "attachment",
                "publish",
            ]
        )
        assert target_default == "target-tag"
        assert capture(
            [
                "--profile",
                str(profile),
                "--repo",
                "xflow-web",
                "attachment",
                "publish",
                "--release-tag",
                "explicit-tag",
            ]
        ) == "explicit-tag"
        assert capture(
            [
                "--profile",
                str(profile),
                "--repo",
                "xflow-web",
                "attachment",
                "publish",
            ],
            host_tag="host-tag",
        ) == "host-tag"

        plain = _repo(root, "plain")
        plain_local = plain / ".xflow" / "local"
        plain_local.mkdir(parents=True, exist_ok=True)
        write_text_lf(plain_local / "env.local", f"{release_tag_key}=legacy-tag\n")
        previous = os.environ.pop(release_tag_key, None)
        captured: list[str] = []
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "DEVCTL_REPO_ROOT": str(plain),
                    "DEVCTL_SKIP_PROVIDER_LOAD": "1",
                    "PYTHONPATH": str(OPS_ROOT),
                },
                clear=False,
            ):
                with mock.patch.object(
                    cli,
                    "run_attachment",
                    side_effect=lambda args: captured.append(args.release_tag) or 0,
                ):
                    assert cli.main(["attachment", "publish"]) == 0
            assert captured == ["legacy-tag"]
        finally:
            os.environ.pop(release_tag_key, None)
            if previous is not None:
                os.environ[release_tag_key] = previous
    finally:
        shutil.rmtree(root)


def test_cockpit_routes_and_all_preflight_order() -> None:
    root, _workspace, cockpit, profile = _fixture()
    try:
        env = {
            "DEVCTL_REPO_ROOT": str(cockpit),
            "DEVCTL_SKIP_PROVIDER_LOAD": "1",
            "PYTHONPATH": str(OPS_ROOT),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            calls: list[tuple[str, object]] = []
            with mock.patch.object(
                cli,
                "run_cockpit_preflight",
                side_effect=lambda _p, _c, warn: calls.append(("preflight", warn)) or 3,
            ):
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "dev", "preflight", "--warn-only"]) == 3
            assert calls == [("preflight", True)]

            calls.clear()
            with mock.patch.object(
                cli,
                "run_cockpit_docker",
                side_effect=lambda _p, _c, action: calls.append(("docker", action)) or 0,
            ):
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "dev", "docker", "setup"]) == 0
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "dev", "docker", "status"]) == 0
            assert calls == [("docker", "setup"), ("docker", "status")]

            calls.clear()
            with mock.patch.object(
                cli,
                "run_playground",
                side_effect=lambda _p, _c, target, opened: calls.append(("playground", (target, opened))) or 0,
            ):
                assert cli.main(
                    ["--profile", str(profile), "--repo", "xflow-web", "dev", "playground", "w", "--no-browser"]
                ) == 0
            assert calls == [("playground", ("w", False))]

            calls.clear()
            with mock.patch.object(
                cli,
                "run_cockpit_preflight",
                side_effect=lambda _p, _c, warn: calls.append(("preflight", warn)) or 1,
            ), mock.patch.object(
                cli,
                "run_scenario",
                side_effect=lambda _p, _c, scenario: calls.append(("scenario", scenario)) or 0,
            ):
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "dev", "all"]) == 1
            assert calls == [("preflight", False)]

            calls.clear()
            with mock.patch.object(
                cli,
                "run_cockpit_preflight",
                side_effect=lambda _p, _c, warn: calls.append(("preflight", warn)) or 0,
            ), mock.patch.object(
                cli,
                "run_scenario",
                side_effect=lambda _p, _c, scenario: calls.append(("scenario", scenario)) or 7,
            ):
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "dev", "all"]) == 7
            assert calls == [("preflight", False), ("scenario", "run")]

            calls.clear()
            with mock.patch.object(
                cli,
                "run_cockpit_preflight",
                side_effect=lambda _p, _c, warn: calls.append(("preflight", warn)) or 0,
            ), mock.patch.object(
                cli,
                "run_scenario",
                side_effect=lambda _p, _c, scenario: calls.append(("scenario", scenario)) or 0,
            ):
                assert cli.main(["--profile", str(profile), "--repo", "xflow-web", "run"]) == 0
            assert calls == [("scenario", "run")]
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
    test_omitted_playground_target_uses_profile_default()
    test_legacy_command_without_globals_does_not_require_profile()
    test_unknown_option_fails_closed_for_legacy_command()
    test_target_project_env_is_loaded_after_repo_resolution()
    test_discovered_profile_symlink_cannot_escape_cockpit_root()
    test_explicit_profile_root_wins_over_environment_root()
    test_explicit_empty_cockpit_root_does_not_fallback_to_cwd_profile()
    test_explicit_missing_profile_does_not_fallback_to_cwd_profile()
    test_parser_project_env_defaults_are_resolved_after_loading()
    test_cockpit_routes_and_all_preflight_order()
    test_repo_resolution_rejects_non_sibling_and_missing_repositories()
    test_repo_resolution_honors_literal_profile_siblings()
    test_unsupported_command_fails_before_profile_or_process_side_effect()
    test_existing_issue_list_route_is_preserved()
    print("cockpit CLI ok")


if __name__ == "__main__":
    main()
