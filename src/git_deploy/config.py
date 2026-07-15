"""Configuration discovery and TOML parsing."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigurationError
from .models import (
    AppConfig,
    ArtifactConfig,
    BuildConfig,
    DockerBuildConfig,
    OnePasswordConfig,
    ProjectConfig,
    ProjectRemoteConfig,
    ServerConfig,
)
from .remote_permissions import load_sftp_permission_policy


_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def discover_config(explicit: str | None = None) -> Path:
    """Resolve the deployment configuration without searching parent folders.

    Args:
        explicit: Optional path supplied by ``--config``.

    Returns:
        Absolute path to an existing TOML configuration file.

    Raises:
        ConfigurationError: When no candidate exists.
    """

    candidates: list[tuple[str, Path]] = []
    if explicit:
        candidates.append(("--config", Path(explicit).expanduser()))
    else:
        candidates.append(("current directory", Path.cwd() / "deploy.toml"))
        env_path = os.environ.get("GIT_DEPLOY_CONFIG")
        if env_path:
            candidates.append(("GIT_DEPLOY_CONFIG", Path(env_path).expanduser()))
        candidates.append(("user config", Path("~/.config/git-deploy/deploy.toml").expanduser()))

    for source, candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
        if explicit:
            raise ConfigurationError(f"configuration from {source} does not exist: {resolved}")

    raise ConfigurationError(
        "deploy.toml not found in the current directory; use --config or GIT_DEPLOY_CONFIG"
    )


def load_config(path: Path) -> AppConfig:
    """Parse and resolve one deployment TOML file.

    Args:
        path: Existing TOML file selected by configuration discovery.

    Returns:
        Validated application configuration.
    """

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc

    server = raw.get("server")
    raw_remotes = raw.get("remotes")
    if server is not None and raw_remotes is not None:
        raise ConfigurationError("configure either [server] or [remotes.NAME], not both")
    if server is not None:
        if not isinstance(server, dict):
            raise ConfigurationError("server must be a table")
        load_sftp_permission_policy(server, location="server")
        values = dict(server)
        _resolve_ftps_tls_paths(values, base=path.parent, location="server")
        remotes = {"default": ServerConfig(values)}
        default_remote: str | None = "default"
    else:
        if not isinstance(raw_remotes, dict) or not raw_remotes:
            raise ConfigurationError("configuration requires [server] or at least one [remotes.NAME]")
        remotes = {}
        for name, raw_values in raw_remotes.items():
            _validate_remote_name(name)
            if not isinstance(raw_values, dict):
                raise ConfigurationError(f"remotes.{name} must be a table")
            load_sftp_permission_policy(raw_values, location=f"remotes.{name}")
            values = dict(raw_values)
            _resolve_ftps_tls_paths(values, base=path.parent, location=f"remotes.{name}")
            remotes[name] = ServerConfig(values)
        configured_default = raw.get("default_remote")
        default_remote = str(configured_default).strip() if configured_default is not None else None
        if default_remote is not None and default_remote not in remotes:
            raise ConfigurationError(f"unknown default_remote {default_remote!r}")

    raw_projects = raw.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise ConfigurationError("configuration requires at least one [projects.NAME] table")

    projects: dict[str, ProjectConfig] = {}
    for name, values in raw_projects.items():
        if not isinstance(values, dict):
            raise ConfigurationError(f"projects.{name} must be a table")
        projects[name] = _load_project(name, values, path.parent, set(remotes))

    # Reject explicit target_id reuse across distinct physical payloads before
    # any CLI remote connect or state-dir access.
    _validate_explicit_target_id_bindings(remotes, projects)

    return AppConfig(
        path=path,
        remotes=remotes,
        projects=projects,
        default_remote=default_remote,
    )


def _validate_explicit_target_id_bindings(
    remotes: dict[str, ServerConfig],
    projects: dict[str, ProjectConfig],
) -> None:
    """Ensure each explicit ``target_id`` binds to one canonical physical payload.

    Args:
        remotes: Configured remotes.
        projects: Configured projects (including per-remote root overrides).

    Returns:
        None.

    Raises:
        ConfigurationError: When the same explicit id names different payloads.
    """

    from .target_identity import PhysicalTargetPayload, build_physical_payload

    bound: dict[str, PhysicalTargetPayload] = {}
    for remote_name, server in remotes.items():
        for project in projects.values():
            if not project.target_id:
                continue
            override = project.remotes.get(remote_name)
            root = (
                override.remote_root
                if override is not None and override.remote_root is not None
                else project.remote_root
            )
            if not root:
                continue
            payload = build_physical_payload(
                protocol=str(server.values.get("protocol", "sftp")),
                host=str(server.values.get("host", "")),
                port=server.values.get("port"),
                project=project.name,
                remote_root=root,
            )
            existing = bound.get(project.target_id)
            if existing is not None and existing.canonical_dict() != payload.canonical_dict():
                raise ConfigurationError(
                    f"explicit target_id {project.target_id!r} cannot merge distinct "
                    f"physical payloads (conflict across remote {remote_name!r} / "
                    f"project {project.name!r})"
                )
            bound[project.target_id] = payload


def _load_project(
    name: str,
    values: dict[str, Any],
    base: Path,
    remote_names: set[str],
) -> ProjectConfig:
    """Resolve one project table relative to the configuration directory.

    Args:
        name: Project key used by the CLI.
        values: Raw TOML project mapping.
        base: Directory containing the TOML file.
        remote_names: Configured remote names available to the project.

    Returns:
        Validated project configuration.
    """

    raw_repository = values.get("repository", values.get("source"))
    remote_root = str(values.get("remote_root", "")).strip()
    if not raw_repository:
        raise ConfigurationError(f"projects.{name}.repository is required")
    if remote_root:
        remote_root = _validate_remote_root(remote_root, f"projects.{name}.remote_root")

    raw_project_remotes = values.get("remotes", {})
    if not isinstance(raw_project_remotes, dict):
        raise ConfigurationError(f"projects.{name}.remotes must be a table")
    project_remotes: dict[str, ProjectRemoteConfig] = {}
    for remote_name, overrides in raw_project_remotes.items():
        _validate_remote_name(remote_name)
        if remote_name not in remote_names:
            raise ConfigurationError(
                f"projects.{name}.remotes.{remote_name} has no matching [remotes.{remote_name}]"
            )
        if not isinstance(overrides, dict):
            raise ConfigurationError(f"projects.{name}.remotes.{remote_name} must be a table")
        remote_prefix = f"projects.{name}.remotes.{remote_name}"
        override_root = str(overrides.get("remote_root", "")).strip() or None
        if override_root is not None:
            override_root = _validate_remote_root(
                override_root,
                f"projects.{name}.remotes.{remote_name}.remote_root",
            )
        project_remotes[remote_name] = ProjectRemoteConfig(
            remote_root=override_root,
            post_commands=(
                _string_tuple(overrides.get("post_commands"), ())
                if "post_commands" in overrides
                else None
            ),
            health_urls=(
                _string_tuple(overrides.get("health_urls"), ())
                if "health_urls" in overrides
                else None
            ),
            build=(
                _load_build(overrides["build"], f"{remote_prefix}.build")
                if "build" in overrides
                else None
            ),
            build_configured="build" in overrides,
            artifacts=(
                _load_artifacts(overrides["artifacts"], f"{remote_prefix}.artifacts")
                if "artifacts" in overrides
                else None
            ),
        )

    missing_roots = sorted(
        remote_name
        for remote_name in remote_names
        if not remote_root
        and (
            remote_name not in project_remotes
            or project_remotes[remote_name].remote_root is None
        )
    )
    if missing_roots:
        raise ConfigurationError(
            f"projects.{name} requires remote_root for remote(s): {', '.join(missing_roots)}"
        )

    repository = Path(str(raw_repository)).expanduser()
    if not repository.is_absolute():
        repository = base / repository
    repository = repository.resolve()
    if not repository.is_dir():
        raise ConfigurationError(f"projects.{name}.repository does not exist: {repository}")

    state_value = values.get("local_state_dir")
    state_dir = _resolve_optional_path(state_value, base)
    explicit_target = values.get("target_id")
    target_id = str(explicit_target).strip() if explicit_target is not None else None
    if target_id == "":
        raise ConfigurationError(f"projects.{name}.target_id must not be empty")
    build = (
        _load_build(values["build"], f"projects.{name}.build")
        if "build" in values
        else None
    )
    artifacts = _load_artifacts(
        values.get("artifacts", []), f"projects.{name}.artifacts"
    )

    return ProjectConfig(
        name=name,
        repository=repository,
        remote_root=remote_root,
        include=_string_tuple(values.get("include"), ("**",)),
        exclude=_string_tuple(values.get("exclude"), ()),
        protected=_string_tuple(values.get("protected"), ()),
        post_commands=_string_tuple(values.get("post_commands"), ()),
        health_urls=_string_tuple(values.get("health_urls"), ()),
        local_state_dir=state_dir,
        remotes=project_remotes,
        target_id=target_id,
        build=build,
        artifacts=artifacts,
        build_origin="project" if build is not None else "none",
        artifacts_origin="project",
    )


def select_remote(
    config: AppConfig,
    requested: str | None,
) -> tuple[str, ServerConfig, dict[str, ProjectConfig]]:
    """Resolve one remote and materialize its project-specific overrides.

    Args:
        config: Loaded application configuration.
        requested: CLI remote name, or ``None`` to use a safe configured default.

    Returns:
        Remote name, server settings, and resolved projects.
    """

    remote_name = requested or config.default_remote
    if remote_name is None:
        if len(config.remotes) == 1:
            remote_name = next(iter(config.remotes))
        else:
            available = ", ".join(config.remotes)
            raise ConfigurationError(
                f"--remote is required; available remotes: {available}"
            )
    try:
        server = config.remotes[remote_name]
    except KeyError as exc:
        available = ", ".join(config.remotes)
        raise ConfigurationError(
            f"unknown remote {remote_name!r}; available: {available}"
        ) from exc

    projects: dict[str, ProjectConfig] = {}
    for name, project in config.projects.items():
        override = project.remotes.get(remote_name)
        projects[name] = replace(
            project,
            remote_root=(override.remote_root if override and override.remote_root else project.remote_root),
            post_commands=(
                override.post_commands
                if override and override.post_commands is not None
                else project.post_commands
            ),
            health_urls=(
                override.health_urls
                if override and override.health_urls is not None
                else project.health_urls
            ),
            build=(
                override.build
                if override and override.build_configured
                else project.build
            ),
            artifacts=(
                override.artifacts
                if override and override.artifacts is not None
                else project.artifacts
            ),
            build_origin=(
                f"remote:{remote_name}"
                if override and override.build_configured
                else project.build_origin
            ),
            artifacts_origin=(
                f"remote:{remote_name}"
                if override and override.artifacts is not None
                else project.artifacts_origin
            ),
            remote=remote_name,
            remotes={},
        )
    return remote_name, server, projects


def resolve_project_target(
    server: ServerConfig,
    project: ProjectConfig,
    *,
    bound_payloads: dict[str, Any] | None = None,
    config: AppConfig | None = None,
) -> Any:
    """Resolve physical target identity for one project/remote pair.

    When ``config`` is provided, rebuilds the global explicit-id binding table
    from all remotes/projects so accidental cross-payload merges fail closed
    even if callers omit ``bound_payloads``.

    Args:
        server: Selected remote server settings.
        project: Resolved project (with remote_root already applied).
        bound_payloads: Optional explicit-id → payload map used to reject merges.
        config: Optional full app config for global binding enforcement.

    Returns:
        ``TargetIdentity`` for the project/remote combination.
    """

    from .target_identity import PhysicalTargetPayload, build_physical_payload, resolve_target_identity

    bound = None
    table = dict(bound_payloads or {})
    if config is not None and project.target_id:
        # Collect payloads already bound to this explicit id across the config.
        for remote_name, remote_server in config.remotes.items():
            other = config.projects.get(project.name)
            if other is None or not other.target_id:
                continue
            if other.target_id != project.target_id:
                continue
            override = other.remotes.get(remote_name)
            root = (
                override.remote_root
                if override is not None and override.remote_root is not None
                else other.remote_root
            )
            if not root:
                continue
            payload = build_physical_payload(
                protocol=str(remote_server.values.get("protocol", "sftp")),
                host=str(remote_server.values.get("host", "")),
                port=remote_server.values.get("port"),
                project=other.name,
                remote_root=root,
            )
            existing = table.get(project.target_id)
            if existing is not None:
                existing_payload = (
                    existing
                    if isinstance(existing, PhysicalTargetPayload)
                    else None
                )
                if (
                    existing_payload is not None
                    and existing_payload.canonical_dict() != payload.canonical_dict()
                ):
                    raise ConfigurationError(
                        f"explicit target_id {project.target_id!r} cannot merge "
                        "distinct physical payloads"
                    )
            else:
                table[project.target_id] = payload
        # Also scan every project sharing the same explicit id.
        for other_name, other in config.projects.items():
            if other.target_id != project.target_id:
                continue
            for remote_name, remote_server in config.remotes.items():
                override = other.remotes.get(remote_name)
                root = (
                    override.remote_root
                    if override is not None and override.remote_root is not None
                    else other.remote_root
                )
                if not root:
                    # Resolved project already has remote_root applied.
                    root = project.remote_root if other_name == project.name else other.remote_root
                if not root:
                    continue
                payload = build_physical_payload(
                    protocol=str(remote_server.values.get("protocol", "sftp")),
                    host=str(remote_server.values.get("host", "")),
                    port=remote_server.values.get("port"),
                    project=other.name,
                    remote_root=root,
                )
                existing = table.get(project.target_id)
                if isinstance(existing, PhysicalTargetPayload):
                    if existing.canonical_dict() != payload.canonical_dict():
                        raise ConfigurationError(
                            f"explicit target_id {project.target_id!r} cannot merge "
                            "distinct physical payloads"
                        )
                else:
                    table[project.target_id] = payload

    if project.target_id and project.target_id in table:
        bound = table[project.target_id]
        if not isinstance(bound, PhysicalTargetPayload):
            bound = None
    return resolve_target_identity(
        server,
        project,
        explicit_target_id=project.target_id,
        bound_payload=bound,
    )


def build_config_summary(project: ProjectConfig) -> dict[str, Any]:
    """Return a secret-safe summary for plan/dry-run rendering.

    The summary intentionally includes only declared environment names and the
    provider label. It never includes ``op://`` references or resolved values.

    Args:
        project: Resolved project configuration.

    Returns:
        JSON-compatible summary without secret references or values.
    """

    build = project.build
    if build is None:
        return {
            "enabled": False,
            "build_origin": project.build_origin,
            "artifacts_origin": project.artifacts_origin,
            "artifact_destinations": [item.destination for item in project.artifacts],
        }
    summary: dict[str, Any] = {
        "enabled": True,
        "runner": build.runner,
        "build_origin": project.build_origin,
        "artifacts_origin": project.artifacts_origin,
        "commands": [list(command) for command in build.commands],
        "cwd": build.cwd,
        "timeout": build.timeout,
        "env_names": list(build.env_allowlist),
        "artifact_destinations": [item.destination for item in project.artifacts],
    }
    if build.docker is not None:
        summary["docker"] = {
            "image": build.docker.image,
            "network": build.docker.network,
            "platform": build.docker.platform,
            "pull_policy": build.docker.pull_policy,
        }
    if build.onepassword is not None:
        summary["secret_provider"] = "1password"
        summary["secret_env_names"] = [name for name, _reference in build.onepassword.env]
    return summary


def _validate_remote_name(name: str) -> None:
    """Reject remote names that are unsafe in CLI output or state paths.

    Args:
        name: Candidate remote identifier.

    Returns:
        None.
    """

    if not _REMOTE_NAME.fullmatch(name):
        raise ConfigurationError(f"invalid remote name: {name!r}")


def _validate_remote_root(value: str, field: str) -> str:
    """Validate and normalize one absolute POSIX deployment root.

    Args:
        value: Candidate remote directory.
        field: Configuration field name used in validation errors.

    Returns:
        Normalized absolute remote directory.
    """

    if not value.startswith("/") or "\\" in value:
        raise ConfigurationError(f"{field} must be an absolute POSIX path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{field} cannot contain control characters")
    raw_parts = value.split("/")
    if ".." in raw_parts or "." in raw_parts:
        raise ConfigurationError(f"{field} cannot contain traversal segments")
    return PurePosixPath(value).as_posix().rstrip("/") or "/"


def _load_build(value: Any, field: str) -> BuildConfig:
    """Validate a host/Docker build configuration table.

    Args:
        value: Parsed TOML value.
        field: Fully qualified field used in errors.

    Returns:
        Immutable build configuration.
    """

    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be a table")
    allowed = {
        "runner",
        "commands",
        "timeout",
        "cwd",
        "env_allowlist",
        "docker",
        "onepassword",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unsupported keys: {', '.join(unknown)}")
    runner = str(value.get("runner", "host")).strip().lower()
    if runner not in {"host", "docker"}:
        raise ConfigurationError(f"{field}.runner must be 'host' or 'docker'")
    commands = _command_matrix(value.get("commands"), f"{field}.commands")
    timeout_value = value.get("timeout", 900)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, int) or timeout_value <= 0:
        raise ConfigurationError(f"{field}.timeout must be a positive integer")
    cwd = _relative_config_path(value.get("cwd", "."), f"{field}.cwd", allow_dot=True)
    env_allowlist = _env_name_tuple(
        value.get("env_allowlist", []), f"{field}.env_allowlist"
    )
    docker = None
    if "docker" in value:
        docker = _load_docker(value["docker"], f"{field}.docker")
    if runner == "docker" and docker is None:
        raise ConfigurationError(f"{field}.docker is required for runner='docker'")
    if runner == "host" and docker is not None:
        raise ConfigurationError(f"{field}.docker requires runner='docker'")
    onepassword = None
    if "onepassword" in value:
        onepassword = _load_onepassword(
            value["onepassword"],
            f"{field}.onepassword",
            env_allowlist,
        )
    return BuildConfig(
        runner=runner,
        commands=commands,
        timeout=timeout_value,
        cwd=cwd,
        env_allowlist=env_allowlist,
        docker=docker,
        onepassword=onepassword,
    )


def _load_docker(value: Any, field: str) -> DockerBuildConfig:
    """Validate the restricted Docker runner table."""

    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be a table")
    allowed = {"image", "platform", "network", "pull_policy"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unsupported keys: {', '.join(unknown)}")
    image = str(value.get("image", "")).strip()
    if not image:
        raise ConfigurationError(f"{field}.image is required")
    platform = str(value.get("platform", "linux/amd64")).strip()
    if not platform or any(char.isspace() for char in platform):
        raise ConfigurationError(f"{field}.platform must be a non-empty platform")
    network = str(value.get("network", "none")).strip().lower()
    if network not in {"none", "bridge"}:
        raise ConfigurationError(f"{field}.network must be 'none' or 'bridge'")
    pull_policy = str(value.get("pull_policy", "never")).strip().lower()
    if pull_policy not in {"never", "missing"}:
        raise ConfigurationError(f"{field}.pull_policy must be 'never' or 'missing'")
    return DockerBuildConfig(
        image=image,
        platform=platform,
        network=network,
        pull_policy=pull_policy,
    )


def _load_onepassword(
    value: Any,
    field: str,
    env_allowlist: tuple[str, ...],
) -> OnePasswordConfig:
    """Validate opaque ``op://`` environment references without resolving them."""

    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be a table")
    unknown = sorted(set(value) - {"env"})
    if unknown:
        raise ConfigurationError(f"{field} contains unsupported keys: {', '.join(unknown)}")
    raw_env = value.get("env")
    if not isinstance(raw_env, dict) or not raw_env:
        raise ConfigurationError(f"{field}.env must be a non-empty table")
    pairs: list[tuple[str, str]] = []
    for name, reference_value in raw_env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or name.startswith("OP_"):
            raise ConfigurationError(f"{field}.env has invalid or reserved name {name!r}")
        if name not in env_allowlist:
            raise ConfigurationError(f"{field}.env.{name} must also appear in env_allowlist")
        if not isinstance(reference_value, str) or not reference_value.startswith("op://"):
            raise ConfigurationError(f"{field}.env.{name} must be an op:// reference")
        if len(reference_value.split("/")) < 5:
            raise ConfigurationError(f"{field}.env.{name} is not a complete op:// reference")
        pairs.append((name, reference_value))
    return OnePasswordConfig(env=tuple(sorted(pairs)))


def _load_artifacts(value: Any, field: str) -> tuple[ArtifactConfig, ...]:
    """Validate artifact file/tree mappings and destination uniqueness."""

    if not isinstance(value, list):
        raise ConfigurationError(f"{field} must be an array of tables")
    artifacts: list[ArtifactConfig] = []
    destinations: set[str] = set()
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise ConfigurationError(f"{item_field} must be a table")
        unknown = sorted(set(item) - {"source", "destination", "kind"})
        if unknown:
            raise ConfigurationError(
                f"{item_field} contains unsupported keys: {', '.join(unknown)}"
            )
        source = _relative_config_path(item.get("source"), f"{item_field}.source")
        destination = _relative_config_path(
            item.get("destination"), f"{item_field}.destination"
        )
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in {"file", "tree"}:
            raise ConfigurationError(f"{item_field}.kind must be 'file' or 'tree'")
        if destination in destinations:
            raise ConfigurationError(f"{field} has duplicate destination {destination!r}")
        destinations.add(destination)
        artifacts.append(ArtifactConfig(source=source, destination=destination, kind=kind))
    return tuple(artifacts)


def _relative_config_path(value: Any, field: str, *, allow_dot: bool = False) -> str:
    """Normalize a safe POSIX-relative configuration path."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty relative path")
    candidate = value.strip()
    if "\\" in candidate or candidate.startswith("/"):
        raise ConfigurationError(f"{field} must be a POSIX-relative path")
    raw_parts = candidate.split("/")
    if any(part in {"", ".."} for part in raw_parts):
        raise ConfigurationError(f"{field} cannot contain empty or traversal segments")
    if candidate == ".":
        if allow_dot:
            return candidate
        raise ConfigurationError(f"{field} cannot be '.'")
    if any(part == "." for part in raw_parts):
        raise ConfigurationError(f"{field} cannot contain '.' segments")
    return PurePosixPath(candidate).as_posix()


def _command_matrix(value: Any, field: str) -> tuple[tuple[str, ...], ...]:
    """Normalize a non-empty list of argv arrays without shell strings."""

    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{field} must be a non-empty array of argv arrays")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if not isinstance(command, list) or not command:
            raise ConfigurationError(f"{field}[{index}] must be a non-empty argv array")
        argv: list[str] = []
        for arg in command:
            if not isinstance(arg, str) or "\x00" in arg:
                raise ConfigurationError(f"{field}[{index}] must contain only strings")
            argv.append(arg)
        if not argv[0].strip():
            raise ConfigurationError(f"{field}[{index}][0] must not be empty")
        commands.append(tuple(argv))
    return tuple(commands)


def _env_name_tuple(value: Any, field: str) -> tuple[str, ...]:
    """Validate unique, non-reserved environment variable names."""

    names = _string_tuple(value, ())
    if len(names) != len(set(names)):
        raise ConfigurationError(f"{field} contains duplicate names")
    for name in names:
        if not _ENV_NAME.fullmatch(name):
            raise ConfigurationError(f"{field} contains invalid name {name!r}")
    return names


_FTPS_TLS_PATH_FIELDS = ("tls_ca_file", "tls_cert_file", "tls_key_file")
# transport.py's build_ftps_ssl_context/ftps_tls_trust_digest also accept these
# legacy, undocumented aliases (`server.get("tls_ca_file") or server.get("ca_file")`);
# resolve/validate whichever key is actually present so neither name silently
# skips the config-relative rule or the load-time existence check.
_FTPS_TLS_PATH_ALIASES = {
    "tls_ca_file": "ca_file",
    "tls_cert_file": "cert_file",
    "tls_key_file": "key_file",
}


def _resolve_ftps_tls_paths(values: dict[str, Any], *, base: Path, location: str) -> None:
    """Resolve FTPS certificate paths config-relative and reject unreadable files.

    P1-08: ``tls_ca_file``/``tls_cert_file``/``tls_key_file`` (and their
    ``ca_file``/``cert_file``/``key_file`` aliases, scoped to ``protocol =
    "ftps"`` entries only so they never touch SFTP's unrelated ``key_file``)
    must follow the same "relative to deploy.toml's directory" rule as every
    other configured path (README ``配置文件发现顺序``), instead of being
    interpreted against the process's current working directory only at
    connect time. Resolved values are written back into ``values`` in place
    so downstream transport code needs no change. Existence/readability is
    only enforced for entries actually configured as ``protocol = "ftps"``:
    these fields are meaningless for other protocols and must not block an
    otherwise-valid config.

    Args:
        values: Mutable parsed ``[server]``/``[remotes.NAME]`` table.
        base: Directory containing the TOML file.
        location: Human-readable table path used in error messages.

    Returns:
        None.

    Raises:
        ConfigurationError: When an ftps entry names a missing or unreadable file.
    """

    is_ftps = str(values.get("protocol", "sftp")).lower() == "ftps"
    fields = list(_FTPS_TLS_PATH_FIELDS)
    if is_ftps:
        fields += list(_FTPS_TLS_PATH_ALIASES.values())
    for field in fields:
        raw_value = values.get(field)
        if raw_value is None or not str(raw_value).strip():
            continue
        resolved = _resolve_optional_path(raw_value, base)
        assert resolved is not None
        if is_ftps:
            if not resolved.is_file():
                raise ConfigurationError(
                    f"{location}.{field} does not exist: {resolved}"
                )
            try:
                resolved.open("rb").close()
            except OSError as exc:
                raise ConfigurationError(
                    f"{location}.{field} is not readable: {resolved}: {exc}"
                ) from exc
        values[field] = str(resolved)


def _resolve_optional_path(value: Any, base: Path) -> Path | None:
    """Resolve an optional path value relative to a configuration directory.

    Args:
        value: TOML value or ``None``.
        base: Directory containing the TOML file.

    Returns:
        Absolute path, or ``None`` when no value was configured.
    """

    if value is None:
        return None
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a TOML string array.

    Args:
        value: Parsed TOML value.
        default: Value returned when the key is absent.

    Returns:
        Tuple of non-empty strings.
    """

    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("expected an array of strings")
    return tuple(item for item in value if item)
