from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import yaml


OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(OPS_ROOT))

from xflow.cockpit import (  # noqa: E402
    CheckSpec,
    CockpitProfile,
    CommandSpec,
    ComposeDependency,
    DockerSpec,
    PlaygroundSpec,
    ScenarioSpec,
    ServiceSpec,
    expand_profile_path,
    load_cockpit_profile,
)


def assert_value_error(expected: str, action: object) -> None:
    try:
        action()
    except ValueError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


def load_mutated_profile(mutate: object) -> object:
    payload = yaml.safe_load((FIXTURES / "cockpit-profile.yaml").read_text(encoding="utf-8"))
    mutate(payload)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "cockpit.yaml"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return load_cockpit_profile(path)


def load_raw_profile(text: str) -> object:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "cockpit.yaml"
        path.write_text(text, encoding="utf-8")
        return load_cockpit_profile(path)


def test_valid_profile() -> None:
    profile = load_cockpit_profile(FIXTURES / "cockpit-profile.yaml")

    assert isinstance(profile, CockpitProfile)
    assert profile.version == 1
    assert isinstance(profile.state_command, CommandSpec)
    assert profile.state_command.argv[0] == sys.executable
    assert isinstance(profile.checks[0], CheckSpec)
    assert isinstance(profile.docker, DockerSpec)
    assert isinstance(profile.dependencies["postgres"], ComposeDependency)
    assert isinstance(profile.services["server"], ServiceSpec)
    assert profile.scenarios["run"].services == ("server", "web")
    assert isinstance(profile.scenarios["run"], ScenarioSpec)
    assert profile.playgrounds["flowable"].aliases == ("f", "bpmn")
    assert isinstance(profile.playgrounds["flowable"], PlaygroundSpec)
    assert profile.state_command.env["PROFILE_MODE"] == "{PROFILE_MODE}"
    assert profile.repositories == ("xflow-web", "xflow-server", "xflow-devctl")
    assert profile.default_playground == "flowable"
    assert "raw" not in profile.__dict__


def test_frozen_dataclasses() -> None:
    profile = load_cockpit_profile(FIXTURES / "cockpit-profile.yaml")
    try:
        profile.version = 2
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("CockpitProfile must be frozen")


def test_rejects_unknown_top_level_key() -> None:
    assert_value_error(
        "unknown field",
        lambda: load_mutated_profile(lambda payload: payload.update({"unexpected": True})),
    )


def test_rejects_missing_repository_allowlist() -> None:
    def mutate(payload: dict[str, object]) -> None:
        del payload["repositories"]

    assert_value_error("requires field: repositories", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_repository_allowlist_entry() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["repositories"] = ["xflow-web", "xflow-web"]

    assert_value_error("duplicate value", lambda: load_mutated_profile(mutate))


def test_rejects_invalid_repository_allowlist_entry() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["repositories"] = ["xflow-web", "../outside"]

    assert_value_error("repository", lambda: load_mutated_profile(mutate))


def test_rejects_unknown_or_blank_default_playground() -> None:
    def missing(mutate_value: str) -> object:
        return load_mutated_profile(
            lambda payload: payload.update({"defaultPlayground": mutate_value})
        )

    assert_value_error("defaultPlayground", lambda: missing(""))
    assert_value_error("defaultPlayground", lambda: missing("missing"))


def test_empty_playgrounds_may_omit_default_playground() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["playgrounds"] = []
        payload.pop("defaultPlayground", None)

    profile = load_mutated_profile(mutate)
    assert profile.playgrounds == {}
    assert profile.default_playground is None


def test_rejects_shell_string() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"] = "python state.py"

    assert_value_error("command must be a mapping", lambda: load_mutated_profile(mutate))


def test_rejects_empty_argv() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = []

    assert_value_error("argv must not be empty", lambda: load_mutated_profile(mutate))


def test_rejects_blank_first_argv() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = ["", "show"]

    assert_value_error("argv[0] must not be blank", lambda: load_mutated_profile(mutate))


def test_rejects_whitespace_first_argv() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = [" \t", "show"]

    assert_value_error("argv[0] must not be blank", lambda: load_mutated_profile(mutate))


def test_allows_blank_later_argv() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = ["{python}", ""]

    profile = load_mutated_profile(mutate)
    assert profile.state_command.argv[1] == ""


def test_rejects_embedded_python_template() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = ["prefix-{python}"]

    assert_value_error(
        "{python} must be the complete argument",
        lambda: load_mutated_profile(mutate),
    )


def test_rejects_path_escape() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["cwd"] = "{cockpit}/../../outside"

    assert_value_error("escapes declared roots", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_service_ids() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"].append(copy.deepcopy(payload["services"][0]))

    assert_value_error("duplicate services id", lambda: load_mutated_profile(mutate))


def test_rejects_unknown_dependency_id() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"][1]["dependencies"] = ["missing"]

    assert_value_error("unknown dependency id", lambda: load_mutated_profile(mutate))


def test_accepts_compose_dependency_outside_application_services() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["dependencies"][0]["service"] = "external-postgres"

    profile = load_mutated_profile(mutate)
    assert profile.dependencies["postgres"].service == "external-postgres"


def test_accepts_compose_service_name_starting_with_underscore() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["dependencies"][0]["service"] = "_service"

    profile = load_mutated_profile(mutate)
    assert profile.dependencies["postgres"].service == "_service"


def test_rejects_empty_compose_service_name() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["dependencies"][0]["service"] = ""

    assert_value_error("must not be empty", lambda: load_mutated_profile(mutate))


def test_rejects_illegal_compose_service_name() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["dependencies"][0]["service"] = "service name"

    assert_value_error("valid non-empty Compose service name", lambda: load_mutated_profile(mutate))


def test_rejects_unknown_scenario_service_id() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["scenarios"][0]["services"] = ["missing"]

    assert_value_error("unknown service id", lambda: load_mutated_profile(mutate))


def test_rejects_nonpositive_timeouts() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["docker"]["startupTimeoutSeconds"] = 0

    assert_value_error("must be positive", lambda: load_mutated_profile(mutate))


def test_rejects_non_http_health_url() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"][1]["healthUrls"] = ["file:///tmp/health"]

    assert_value_error("valid HTTP URL", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_health_urls() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"][1]["healthUrls"] = [
            "http://127.0.0.1:5173/",
            "http://127.0.0.1:5173/",
        ]

    assert_value_error("duplicate", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_scenario_open_urls() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["scenarios"].append(
            {"id": "run-again", "services": ["server"], "openUrl": "http://127.0.0.1:5173/"}
        )

    assert_value_error("duplicate", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_playground_urls() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["playgrounds"][1]["url"] = payload["playgrounds"][0]["url"]

    assert_value_error("duplicate", lambda: load_mutated_profile(mutate))


def test_rejects_health_url_with_whitespace_hostname() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"][1]["healthUrls"] = ["http://bad host/health"]

    assert_value_error("valid HTTP URL", lambda: load_mutated_profile(mutate))


def test_rejects_health_url_with_c1_control_character() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["services"][1]["healthUrls"] = ["http://example.com/\x80health"]

    assert_value_error("valid HTTP URL", lambda: load_mutated_profile(mutate))


def test_rejects_scenario_url_with_out_of_range_port() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["scenarios"][0]["openUrl"] = "http://example.com:99999/"

    assert_value_error("valid HTTP URL", lambda: load_mutated_profile(mutate))


def test_rejects_playground_url_with_empty_hostname() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["playgrounds"][0]["url"] = "http://:8080/"

    assert_value_error("valid HTTP URL", lambda: load_mutated_profile(mutate))


def test_rejects_blank_playground_alias() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["playgrounds"][0]["aliases"] = [" "]

    assert_value_error("blank", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_playground_aliases_globally() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["playgrounds"][1]["aliases"] = ["f"]

    assert_value_error("duplicate", lambda: load_mutated_profile(mutate))


def test_rejects_unknown_command_field() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["shell"] = "echo unsafe"

    assert_value_error("unknown field", lambda: load_mutated_profile(mutate))


def test_rejects_duplicate_yaml_mapping_key() -> None:
    duplicate = "version: 1\nversion: 1\n"
    assert_value_error("invalid cockpit profile YAML", lambda: load_raw_profile(duplicate))


def test_rejects_unknown_template_token() -> None:
    def mutate(payload: dict[str, object]) -> None:
        payload["state"]["command"]["argv"] = ["{unknown}"]

    assert_value_error("unknown command template token", lambda: load_mutated_profile(mutate))


def test_expand_profile_path_confines_to_declared_roots() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        cockpit = root / "cockpit"
        workspace = root / "workspace"
        repo = root / "repo"
        assert expand_profile_path("{cockpit}/.xflow", cockpit, workspace, repo) == cockpit / ".xflow"
        assert_value_error(
            "escapes declared roots",
            lambda: expand_profile_path("{cockpit}/../../outside", cockpit, workspace, repo),
        )


def test_plan2_profile_example_loads_with_v1_loader() -> None:
    plan = (OPS_ROOT / "docs/superpowers/plans/2026-08-26-xflow-spec-runtime-integration.md").read_text(
        encoding="utf-8"
    )
    marker = "### Task 4: Add the minimal XFlow cockpit profile and state adapter"
    start = plan.index("```yaml", plan.index(marker)) + len("```yaml\n")
    end = plan.index("```", start)
    with tempfile.TemporaryDirectory() as raw:
        profile_path = Path(raw) / ".xflow" / "cockpit.yaml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(plan[start:end], encoding="utf-8")
        profile = load_cockpit_profile(profile_path)
    assert profile.repositories
    assert profile.default_playground


def main() -> None:
    test_valid_profile()
    test_frozen_dataclasses()
    test_rejects_unknown_top_level_key()
    test_rejects_missing_repository_allowlist()
    test_rejects_duplicate_repository_allowlist_entry()
    test_rejects_invalid_repository_allowlist_entry()
    test_rejects_unknown_or_blank_default_playground()
    test_empty_playgrounds_may_omit_default_playground()
    test_rejects_shell_string()
    test_rejects_empty_argv()
    test_rejects_blank_first_argv()
    test_rejects_whitespace_first_argv()
    test_allows_blank_later_argv()
    test_rejects_embedded_python_template()
    test_rejects_path_escape()
    test_rejects_duplicate_service_ids()
    test_rejects_unknown_dependency_id()
    test_accepts_compose_dependency_outside_application_services()
    test_accepts_compose_service_name_starting_with_underscore()
    test_rejects_empty_compose_service_name()
    test_rejects_illegal_compose_service_name()
    test_rejects_unknown_scenario_service_id()
    test_rejects_nonpositive_timeouts()
    test_rejects_non_http_health_url()
    test_rejects_duplicate_health_urls()
    test_rejects_duplicate_scenario_open_urls()
    test_rejects_duplicate_playground_urls()
    test_rejects_health_url_with_whitespace_hostname()
    test_rejects_health_url_with_c1_control_character()
    test_rejects_scenario_url_with_out_of_range_port()
    test_rejects_playground_url_with_empty_hostname()
    test_rejects_blank_playground_alias()
    test_rejects_duplicate_playground_aliases_globally()
    test_rejects_unknown_command_field()
    test_rejects_duplicate_yaml_mapping_key()
    test_rejects_unknown_template_token()
    test_expand_profile_path_confines_to_declared_roots()
    test_plan2_profile_example_loads_with_v1_loader()
    print("cockpit profile ok")


if __name__ == "__main__":
    main()
