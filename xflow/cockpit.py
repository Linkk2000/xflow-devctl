from __future__ import annotations

import ipaddress
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar
from urllib.parse import urlparse

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from .io import canonical_path


ALLOWED_PATH_TOKENS = frozenset({"cockpit", "workspace", "repo"})
_COMMAND_BUILTIN_TOKENS = frozenset({"python"})
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_COMPOSE_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_MISSING = object()


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(None, None, "expected a mapping", node.start_mark)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class CockpitContext:
    cockpit_root: Path
    workspace_root: Path
    repo_root: Path
    python_executable: Path
    run_dir: Path
    env: Mapping[str, str]


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]


@dataclass(frozen=True)
class ComposeDependency:
    id: str
    cwd: str
    service: str
    up: CommandSpec
    ready: CommandSpec
    timeout_seconds: int


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    command: CommandSpec
    dependencies: tuple[str, ...]
    health_urls: tuple[str, ...]
    log_file: str


@dataclass(frozen=True)
class CheckSpec:
    id: str
    command: CommandSpec
    expect_regex: Optional[str]


@dataclass(frozen=True)
class DockerSpec:
    cli_check: CommandSpec
    compose_check: CommandSpec
    engine_probe: CommandSpec
    image_probe: Optional[CommandSpec]
    startup_timeout_seconds: int


@dataclass(frozen=True)
class ScenarioSpec:
    services: tuple[str, ...]
    open_url: Optional[str]


@dataclass(frozen=True)
class PlaygroundSpec:
    id: str
    aliases: tuple[str, ...]
    command: CommandSpec
    build: Optional[CommandSpec]
    url: str


@dataclass(frozen=True)
class CockpitProfile:
    version: int
    state_command: CommandSpec
    checks: tuple[CheckSpec, ...]
    docker: DockerSpec
    dependencies: Mapping[str, ComposeDependency]
    services: Mapping[str, ServiceSpec]
    scenarios: Mapping[str, ScenarioSpec]
    playgrounds: Mapping[str, PlaygroundSpec]
    # These fields are appended with defaults to keep the direct construction
    # contract used by command-level callers source compatible.  Profiles
    # loaded from YAML validate both values strictly below.
    repositories: tuple[str, ...] = ()
    default_playground: Optional[str] = None


T = TypeVar("T")


def expand_profile_path(
    value: str,
    cockpit_root: Path,
    workspace_root: Path,
    repo_root: Path,
) -> Path:
    """Expand a profile path and keep it under one of the declared roots."""

    if not isinstance(value, str) or not value:
        raise ValueError("profile path must be a non-empty string")
    tokens = _template_tokens(value, "profile path")
    unknown = sorted(set(tokens) - ALLOWED_PATH_TOKENS)
    if unknown:
        raise ValueError(f"unknown path template token: {unknown[0]}")

    try:
        rendered = value.format(
            cockpit=str(cockpit_root),
            workspace=str(workspace_root),
            repo=str(repo_root),
        )
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(f"invalid profile path template: {value}") from exc

    path = canonical_path(Path(rendered))
    roots = tuple(canonical_path(root) for root in (cockpit_root, workspace_root, repo_root))
    if not any(_is_within(path, root) for root in roots):
        raise ValueError(f"profile path escapes declared roots: {value}")
    return path


def load_cockpit_profile(path: Path) -> CockpitProfile:
    """Load and validate a cockpit profile without retaining YAML mappings."""

    profile_path = canonical_path(Path(path))
    try:
        raw = yaml.load(
            profile_path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader
        )
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid cockpit profile YAML: {profile_path}") from exc
    root = _mapping(raw, "profile")
    _fields(
        root,
        {
            "version",
            "repositories",
            "defaultPlayground",
            "allowedEnvironment",
            "state",
            "preflight",
            "docker",
            "dependencies",
            "services",
            "scenarios",
            "playgrounds",
        },
        "profile",
    )

    version = _int(root, "version", "profile", minimum=1)
    if version != 1:
        raise ValueError(f"unsupported cockpit profile version: {version}")
    repositories = _repository_names(
        _required(root, "repositories", "profile"), "profile.repositories"
    )
    allowed_environment = _environment_names(root.get("allowedEnvironment", []), "allowedEnvironment")
    roots = _profile_roots(profile_path)

    state = _mapping(_required(root, "state", "profile"), "state")
    _fields(state, {"command"}, "state")
    state_command = _command(state.get("command"), "state.command", roots, allowed_environment)

    preflight = _mapping(_required(root, "preflight", "profile"), "preflight")
    _fields(preflight, {"checks"}, "preflight")
    checks = _records(
        _required(preflight, "checks", "preflight"),
        "preflight.checks",
        lambda raw_check: _check(raw_check, roots, allowed_environment),
    )

    docker = _mapping(_required(root, "docker", "profile"), "docker")
    _fields(
        docker,
        {"cliCheck", "composeCheck", "engineProbe", "imageProbe", "startupTimeoutSeconds"},
        "docker",
    )
    docker_spec = DockerSpec(
        cli_check=_command(docker.get("cliCheck"), "docker.cliCheck", roots, allowed_environment),
        compose_check=_command(
            docker.get("composeCheck"), "docker.composeCheck", roots, allowed_environment
        ),
        engine_probe=_command(
            docker.get("engineProbe"), "docker.engineProbe", roots, allowed_environment
        ),
        image_probe=(
            None
            if docker.get("imageProbe") is None
            else _command(docker.get("imageProbe"), "docker.imageProbe", roots, allowed_environment)
        ),
        startup_timeout_seconds=_int(
            docker, "startupTimeoutSeconds", "docker", minimum=1
        ),
    )

    dependencies = _records(
        root.get("dependencies", []),
        "dependencies",
        lambda raw_dependency: _dependency(raw_dependency, roots, allowed_environment),
    )
    services = _records(
        root.get("services", []),
        "services",
        lambda raw_service: _service(raw_service, roots, allowed_environment),
    )
    scenarios = _records(
        root.get("scenarios", []),
        "scenarios",
        _scenario_record,
    )
    playgrounds = _records(
        root.get("playgrounds", []),
        "playgrounds",
        lambda raw_playground: _playground(raw_playground, roots, allowed_environment),
    )

    _validate_references(dependencies, services, scenarios)
    _validate_aliases(playgrounds)
    _validate_scenario_urls(scenarios)
    _validate_playground_urls(playgrounds)
    default_playground = _default_playground(root, playgrounds)

    return CockpitProfile(
        version=version,
        state_command=state_command,
        checks=tuple(checks.values()),
        docker=docker_spec,
        dependencies=_mapping_proxy(dependencies),
        services=_mapping_proxy(services),
        scenarios=_mapping_proxy(scenarios),
        playgrounds=_mapping_proxy(playgrounds),
        repositories=repositories,
        default_playground=default_playground,
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{label} field names must be strings")
    return value


def _fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} unknown field: {unknown[0]}")


def _required(value: Mapping[str, Any], name: str, label: str) -> Any:
    result = value.get(name, _MISSING)
    if result is _MISSING:
        raise ValueError(f"{label} requires field: {name}")
    return result


def _string(value: Any, label: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if non_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _id(value: Any, label: str) -> str:
    result = _string(value, label)
    if _ID.fullmatch(result) is None:
        raise ValueError(f"{label} must contain only letters, numbers, dots, underscores, or dashes")
    return result


def _int(
    value: Mapping[str, Any], name: str, label: str, *, minimum: Optional[int] = None
) -> int:
    result = _required(value, name, label)
    if type(result) is not int:
        raise ValueError(f"{label}.{name} must be an integer")
    if minimum is not None and result < minimum:
        if minimum == 1:
            raise ValueError(f"{label}.{name} must be positive")
        raise ValueError(f"{label}.{name} must be at least {minimum}")
    return result


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    return value


def _string_tuple(value: Any, label: str, *, ids: bool = False) -> tuple[str, ...]:
    values = _sequence(value, label)
    result = []
    seen = set()
    for index, item in enumerate(values):
        item_label = f"{label}[{index}]"
        parsed = _id(item, item_label) if ids else _string(item, item_label)
        if parsed in seen:
            raise ValueError(f"{label} contains duplicate value: {parsed}")
        seen.add(parsed)
        result.append(parsed)
    return tuple(result)


def _environment_names(value: Any, label: str) -> frozenset[str]:
    names = _string_tuple(value, label)
    for name in names:
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError(f"{label} contains invalid environment name: {name}")
    return frozenset(names)


def _repository_names(value: Any, label: str) -> tuple[str, ...]:
    values = _sequence(value, label)
    result = []
    seen = set()
    for index, item in enumerate(values):
        item_label = f"{label}[{index}]"
        name = _string(item, item_label)
        if (
            name in {".", ".."}
            or "/" in name
            or "\\" in name
            or _ID.fullmatch(name) is None
        ):
            raise ValueError(f"{item_label} must be a legal sibling repository name")
        if name in seen:
            raise ValueError(f"{label} contains duplicate value: {name}")
        seen.add(name)
        result.append(name)
    return tuple(result)


def _template_tokens(value: str, label: str) -> tuple[str, ...]:
    if value.count("{") != value.count("}"):
        raise ValueError(f"{label} has unmatched template braces")
    tokens = tuple(match.group(1) for match in _TEMPLATE_TOKEN.finditer(value))
    consumed = _TEMPLATE_TOKEN.sub("", value)
    if "{" in consumed or "}" in consumed:
        raise ValueError(f"{label} has invalid template braces")
    return tokens


def _profile_roots(profile_path: Path) -> tuple[Path, Path, Path]:
    profile_dir = profile_path.parent
    if profile_dir.name == ".xflow":
        cockpit_root = profile_dir.parent
    else:
        cockpit_root = profile_dir
    cockpit_root = canonical_path(cockpit_root)
    workspace_root = canonical_path(cockpit_root.parent)
    repo_root = cockpit_root
    return cockpit_root, workspace_root, repo_root


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _path_template(value: Any, label: str, roots: tuple[Path, Path, Path]) -> str:
    result = _string(value, label)
    expand_profile_path(result, *roots)
    return result


def _command(
    value: Any,
    label: str,
    roots: tuple[Path, Path, Path],
    allowed_environment: frozenset[str],
) -> CommandSpec:
    command = _mapping(value, label)
    _fields(command, {"argv", "cwd", "env"}, label)
    argv_values = _sequence(_required(command, "argv", label), f"{label}.argv")
    if not argv_values:
        raise ValueError(f"{label}.argv must not be empty")
    argv = []
    for index, item in enumerate(argv_values):
        argument = _string(item, f"{label}.argv[{index}]", non_empty=False)
        if index == 0 and not argument.strip():
            raise ValueError(f"{label}.argv[0] must not be blank")
        tokens = _template_tokens(argument, f"{label}.argv[{index}]")
        allowed = ALLOWED_PATH_TOKENS | _COMMAND_BUILTIN_TOKENS | allowed_environment
        unknown = sorted(set(tokens) - allowed)
        if unknown:
            raise ValueError(f"unknown command template token: {unknown[0]}")
        if "python" in tokens and argument != "{python}":
            raise ValueError(f"{label}.argv[{index}] {{python}} must be the complete argument")
        if any(token in ALLOWED_PATH_TOKENS for token in tokens):
            _path_template(argument, f"{label}.argv[{index}]", roots)
        argv.append(sys.executable if argument == "{python}" else argument)

    cwd = _path_template(_required(command, "cwd", label), f"{label}.cwd", roots)
    environment = _command_environment(command.get("env", {}), label, allowed_environment)
    return CommandSpec(argv=tuple(argv), cwd=cwd, env=_mapping_proxy(environment))


def _command_environment(
    value: Any, label: str, allowed_environment: frozenset[str]
) -> dict[str, str]:
    environment = _mapping(value, f"{label}.env")
    result = {}
    for key, item in environment.items():
        if _ENVIRONMENT_NAME.fullmatch(key) is None:
            raise ValueError(f"{label}.env has invalid environment name: {key}")
        parsed = _string(item, f"{label}.env.{key}", non_empty=False)
        tokens = _template_tokens(parsed, f"{label}.env.{key}")
        unknown = sorted(set(tokens) - allowed_environment)
        if unknown:
            raise ValueError(f"unknown environment template token: {unknown[0]}")
        result[key] = parsed
    return result


def _check(
    value: Any,
    roots: tuple[Path, Path, Path],
    allowed_environment: frozenset[str],
) -> CheckSpec:
    check = _mapping(value, "preflight.check")
    _fields(check, {"id", "command", "expectRegex"}, "preflight.check")
    expect_regex = check.get("expectRegex")
    if expect_regex is not None:
        expect_regex = _string(expect_regex, "preflight.check.expectRegex", non_empty=False)
        try:
            re.compile(expect_regex)
        except re.error as exc:
            raise ValueError("preflight.check.expectRegex must be valid regex") from exc
    return CheckSpec(
        id=_id(_required(check, "id", "preflight.check"), "preflight.check.id"),
        command=_command(check.get("command"), "preflight.check.command", roots, allowed_environment),
        expect_regex=expect_regex,
    )


def _dependency(
    value: Any,
    roots: tuple[Path, Path, Path],
    allowed_environment: frozenset[str],
) -> ComposeDependency:
    dependency = _mapping(value, "dependency")
    _fields(dependency, {"id", "cwd", "service", "up", "ready", "timeoutSeconds"}, "dependency")
    cwd = _path_template(_required(dependency, "cwd", "dependency"), "dependency.cwd", roots)
    return ComposeDependency(
        id=_id(_required(dependency, "id", "dependency"), "dependency.id"),
        cwd=cwd,
        service=_compose_service_name(
            _required(dependency, "service", "dependency"), "dependency.service"
        ),
        up=_command(dependency.get("up"), "dependency.up", roots, allowed_environment),
        ready=_command(dependency.get("ready"), "dependency.ready", roots, allowed_environment),
        timeout_seconds=_int(dependency, "timeoutSeconds", "dependency", minimum=1),
    )


def _service(
    value: Any,
    roots: tuple[Path, Path, Path],
    allowed_environment: frozenset[str],
) -> ServiceSpec:
    service = _mapping(value, "service")
    _fields(service, {"id", "command", "dependencies", "healthUrls", "logFile"}, "service")
    health_urls = _urls(service.get("healthUrls", []), "service.healthUrls")
    return ServiceSpec(
        id=_id(_required(service, "id", "service"), "service.id"),
        command=_command(service.get("command"), "service.command", roots, allowed_environment),
        dependencies=_string_tuple(
            _required(service, "dependencies", "service"), "service.dependencies", ids=True
        ),
        health_urls=health_urls,
        log_file=_path_template(_required(service, "logFile", "service"), "service.logFile", roots),
    )


def _urls(value: Any, label: str) -> tuple[str, ...]:
    values = _sequence(value, label)
    result = []
    seen = set()
    for index, item in enumerate(values):
        url = _string(item, f"{label}[{index}]")
        if not _is_http_url(url):
            raise ValueError(f"{label}[{index}] must be a valid HTTP URL")
        if url in seen:
            raise ValueError(f"{label} contains duplicate URL: {url}")
        seen.add(url)
        result.append(url)
    return tuple(result)


def _is_http_url(value: str) -> bool:
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        return False
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        hostname = parsed.hostname
        if not hostname or not _is_legal_hostname(hostname):
            return False
        parsed.port
    except ValueError:
        return False
    return True


def _is_legal_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return False
    if not ascii_hostname or len(ascii_hostname) > 253:
        return False
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    return re.fullmatch(rf"{label}(?:\.{label})*", ascii_hostname) is not None


def _compose_service_name(value: Any, label: str) -> str:
    result = _string(value, label)
    if _COMPOSE_SERVICE_NAME.fullmatch(result) is None:
        raise ValueError(f"{label} must be a valid non-empty Compose service name")
    return result


def _scenario(value: Any) -> ScenarioSpec:
    scenario = _mapping(value, "scenario")
    _fields(scenario, {"id", "services", "openUrl"}, "scenario")
    open_url = scenario.get("openUrl")
    if open_url is not None:
        open_url = _string(open_url, "scenario.openUrl")
        if not _is_http_url(open_url):
            raise ValueError("scenario.openUrl must be a valid HTTP URL")
    return ScenarioSpec(
        services=_string_tuple(
            _required(scenario, "services", "scenario"), "scenario.services", ids=True
        ),
        open_url=open_url,
    )


def _scenario_record(value: Any) -> "_NamedRecord[ScenarioSpec]":
    scenario = _mapping(value, "scenario")
    scenario_id = _id(_required(scenario, "id", "scenario"), "scenario.id")
    return _NamedRecord(scenario_id, _scenario(scenario))


def _playground(
    value: Any,
    roots: tuple[Path, Path, Path],
    allowed_environment: frozenset[str],
) -> PlaygroundSpec:
    playground = _mapping(value, "playground")
    _fields(playground, {"id", "aliases", "command", "build", "url"}, "playground")
    url = _string(_required(playground, "url", "playground"), "playground.url")
    if not _is_http_url(url):
        raise ValueError("playground.url must be a valid HTTP URL")
    build = playground.get("build")
    return PlaygroundSpec(
        id=_id(_required(playground, "id", "playground"), "playground.id"),
        aliases=_aliases(_required(playground, "aliases", "playground"), "playground.aliases"),
        command=_command(
            playground.get("command"), "playground.command", roots, allowed_environment
        ),
        build=(
            None
            if build is None
            else _command(build, "playground.build", roots, allowed_environment)
        ),
        url=url,
    )


@dataclass(frozen=True)
class _NamedRecord:
    id: str
    value: T


def _records(value: Any, label: str, reader: Callable[[Any], T]) -> dict[str, T]:
    values = _sequence(value, label)
    result = {}
    for index, item in enumerate(values):
        parsed = reader(item)
        if isinstance(parsed, _NamedRecord):
            record_id = parsed.id
            record = parsed.value
        else:
            record_id = getattr(parsed, "id", None)
            record = parsed
        if not isinstance(record_id, str):
            raise ValueError(f"{label}[{index}] must produce an id")
        if record_id in result:
            raise ValueError(f"duplicate {label} id: {record_id}")
        result[record_id] = record
    return result


def _validate_references(
    dependencies: Mapping[str, ComposeDependency],
    services: Mapping[str, ServiceSpec],
    scenarios: Mapping[str, ScenarioSpec],
) -> None:
    for service in services.values():
        for dependency_id in service.dependencies:
            if dependency_id not in dependencies:
                raise ValueError(f"unknown dependency id: {dependency_id}")
    for scenario_id, scenario in scenarios.items():
        for service_id in scenario.services:
            if service_id not in services:
                raise ValueError(f"unknown service id in scenario {scenario_id}: {service_id}")


def _validate_scenario_urls(scenarios: Mapping[str, ScenarioSpec]) -> None:
    seen = set()
    for scenario_id, scenario in scenarios.items():
        if scenario.open_url is None:
            continue
        if scenario.open_url in seen:
            raise ValueError(
                f"scenarios contains duplicate open URL: {scenario.open_url}"
            )
        seen.add(scenario.open_url)


def _validate_playground_urls(playgrounds: Mapping[str, PlaygroundSpec]) -> None:
    seen = set()
    for playground in playgrounds.values():
        if playground.url in seen:
            raise ValueError(
                f"playgrounds contains duplicate URL: {playground.url}"
            )
        seen.add(playground.url)


def _validate_aliases(playgrounds: Mapping[str, PlaygroundSpec]) -> None:
    aliases = set()
    for playground in playgrounds.values():
        for alias in playground.aliases:
            if alias in playgrounds or alias in aliases:
                raise ValueError(f"duplicate playground alias: {alias}")
            aliases.add(alias)


def _aliases(value: Any, label: str) -> tuple[str, ...]:
    values = _sequence(value, label)
    result = []
    seen = set()
    for index, item in enumerate(values):
        alias = _string(item, f"{label}[{index}]")
        if not alias.strip():
            raise ValueError(f"{label}[{index}] must not be blank")
        if alias in seen:
            raise ValueError(f"{label} contains duplicate value: {alias}")
        seen.add(alias)
        result.append(alias)
    return tuple(result)


def _default_playground(
    root: Mapping[str, Any], playgrounds: Mapping[str, PlaygroundSpec]
) -> Optional[str]:
    value = root.get("defaultPlayground", _MISSING)
    if value is _MISSING:
        if playgrounds:
            raise ValueError(
                "profile.defaultPlayground is required when playgrounds are configured"
            )
        return None
    default = _string(value, "profile.defaultPlayground")
    if not default.strip():
        raise ValueError("profile.defaultPlayground must not be blank")
    if default not in playgrounds:
        raise ValueError(
            f"profile.defaultPlayground references unknown playground: {default}"
        )
    return default


def _mapping_proxy(value: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(value))
