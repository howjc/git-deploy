"""Load and validate the intentionally small v1-lite TOML configuration."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from git_deploy.errors import ConfigError

Protocol = Literal["sftp", "ftp"]

DEFAULT_EXCLUDE = (
    ".git/**",
    ".env",
    ".env.*",
    "node_modules/**",
    "runtime/**",
    "uploads/**",
    "storage/logs/**",
)
DEFAULT_PROTECT = (
    ".env",
    ".env.*",
    "uploads/**",
    "runtime/**",
    "storage/cert/**",
    "**/*.key",
    "**/*.pem",
)


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Describe committed source paths managed by Git."""

    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    protect: tuple[str, ...] = DEFAULT_PROTECT
    require_clean_worktree: bool = False


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Describe trusted shell commands run serially in the project root."""

    steps: tuple[str, ...] = ()
    timeout: float | None = 900.0


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """Map one local build-output path to a remote relative directory."""

    local: Path
    remote: PurePosixPath
    delete_removed: bool = True


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Contain the connection settings for one independent environment."""

    name: str
    protocol: Protocol
    host: str | None
    username: str | None
    remote_root: PurePosixPath
    port: int
    port_explicit: bool = False
    password_env: str | None = None
    ssh_host_alias: str | None = None
    ssh_config_file: Path = Path("~/.ssh/config")
    ssh_config_explicit: bool = False
    known_hosts_file: Path | None = None
    key_file: Path | None = None
    use_ssh_agent: bool = True
    strict_host_key_checking: bool = True
    passive: bool = True
    timeout: float = 15.0
    ssh_resolved: bool = False
    resolved_key_files: tuple[str, ...] = ()
    runtime_dir: Path | None = None

    @property
    def fingerprint(self) -> str:
        """Return a stable non-secret identity for state-target binding."""

        if self.protocol == "sftp" and not self.ssh_resolved:
            resolved = resolve_ssh_target(self)
            return (
                f"sftp:{resolved.username}@{resolved.host}:{resolved.port}:"
                f"{self.remote_root}"
            )
        if self.protocol == "sftp":
            return f"sftp:{self.username or ''}@{self.host or ''}:{self.port}:{self.remote_root}"
        return f"ftp:{self.username or ''}@{self.host or ''}:{self.port}:{self.remote_root}"


@dataclass(frozen=True, slots=True)
class ResolvedSSHConfig:
    """Contain effective non-secret OpenSSH connection settings."""

    host: str
    username: str
    port: int
    key_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeployConfig:
    """Configure per-file retry behavior."""

    retries: int = 3
    retry_delay: float = 2.0


@dataclass(frozen=True, slots=True)
class Config:
    """Represent a fully resolved v1-lite project configuration."""

    path: Path
    project_root: Path
    default_target: str | None
    source: SourceConfig
    build: BuildConfig
    outputs: tuple[OutputConfig, ...]
    targets: dict[str, TargetConfig]
    deploy: DeployConfig = field(default_factory=DeployConfig)

    def target(self, requested: str | None) -> TargetConfig:
        """Resolve a requested or default target and return its settings.

        Args:
            requested: Explicit target name, or ``None`` to use ``default_target``.

        Returns:
            The selected validated target configuration.
        """

        name = requested or self.default_target
        if name is None:
            if len(self.targets) == 1:
                return next(iter(self.targets.values()))
            raise ConfigError("target is required because default_target is not set")
        try:
            return self.targets[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.targets)) or "<none>"
            raise ConfigError(f"unknown target {name!r}; configured targets: {choices}") from exc


def discover_config(explicit: Path | None = None) -> Path:
    """Find the v1-lite config without walking into parent directories.

    Args:
        explicit: Optional path supplied through ``--config``.

    Returns:
        An absolute path to the selected configuration file.
    """

    if explicit is not None:
        return explicit.expanduser().resolve()
    local = Path.cwd() / "deploy.toml"
    if local.is_file():
        return local.resolve()
    configured = os.environ.get("GIT_DEPLOY_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".config/git-deploy/deploy.toml").resolve()


def resolve_ssh_target(target: TargetConfig) -> ResolvedSSHConfig:
    """Resolve effective SFTP host, user, port, and identity files with ``ssh -G``.

    Args:
        target: Validated SFTP target whose explicit values override OpenSSH output.

    Returns:
        Effective non-secret settings used by both target binding and Paramiko.
    """

    if target.ssh_resolved:
        if not target.host or not target.username:
            raise ConfigError(f"resolved SFTP target {target.name} lacks host or username")
        return ResolvedSSHConfig(
            target.host,
            target.username,
            target.port,
            target.resolved_key_files,
        )
    query = target.ssh_host_alias or target.host
    if not query:
        raise ConfigError(f"SFTP target {target.name} has no host or ssh_host_alias")
    command = ["ssh", "-G"]
    if target.ssh_config_explicit or target.ssh_config_file.is_file():
        command.extend(["-F", str(target.ssh_config_file)])
    command.append(query)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ConfigError(f"cannot execute ssh -G for {query}: {exc}") from exc
    if result.returncode != 0:
        raise ConfigError(f"cannot resolve SSH target {query}: {result.stderr.strip()}")
    resolved: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(" ")
        if separator:
            resolved.setdefault(key.lower(), []).append(value.strip())
    proxy_jump = _first_ssh_value(resolved, "proxyjump")
    proxy_command = _first_ssh_value(resolved, "proxycommand")
    if not target.ssh_host_alias and (
        (proxy_jump and proxy_jump.lower() != "none")
        or (proxy_command and proxy_command.lower() != "none")
    ):
        raise ConfigError("ProxyJump/ProxyCommand is not supported by the v1-lite SFTP transport")
    host = target.host or _first_ssh_value(resolved, "hostname") or query
    username = target.username or _first_ssh_value(resolved, "user")
    if not username:
        raise ConfigError(f"SFTP target {target.name} did not resolve a username")
    port = target.port
    resolved_port = _first_ssh_value(resolved, "port")
    if not target.port_explicit and resolved_port:
        try:
            port = int(resolved_port)
        except ValueError as exc:
            raise ConfigError(f"SSH target {query} resolved an invalid port: {resolved_port}") from exc
    if target.key_file is not None:
        key_files = (str(target.key_file),)
    else:
        candidates = (
            os.path.expanduser(item)
            for item in resolved.get("identityfile", [])
            if item.lower() != "none"
        )
        # OpenSSH prints default IdentityFile paths even when absent. Passing those
        # paths to Paramiko can prevent a configured SSH Agent from being tried.
        key_files = tuple(item for item in candidates if Path(item).is_file())
    return ResolvedSSHConfig(host, username, port, key_files)


def resolve_target_for_plan(
    target: TargetConfig,
    *,
    runtime_dir: Path | None = None,
) -> TargetConfig:
    """Freeze effective non-secret connection identity into a deployment target.

    Args:
        target: Selected validated target from the loaded configuration.
        runtime_dir: Shared Git state root for locks and private SSH sockets.

    Returns:
        A target whose SFTP alias/config values can no longer drift after review.
    """

    if target.protocol != "sftp" or target.ssh_resolved:
        return replace(target, runtime_dir=runtime_dir or target.runtime_dir)
    resolved = resolve_ssh_target(target)
    return replace(
        target,
        host=resolved.host,
        username=resolved.username,
        port=resolved.port,
        port_explicit=True,
        # Preserve the alias for the Native OpenSSH backend. The resolved host,
        # user and port still freeze identity and prevent post-review drift.
        ssh_resolved=True,
        resolved_key_files=resolved.key_files,
        runtime_dir=runtime_dir,
    )


def _first_ssh_value(values: dict[str, list[str]], key: str) -> str | None:
    """Return the first effective OpenSSH value for one lowercase keyword."""

    items = values.get(key, [])
    return items[0] if items else None


def load_config(path: Path) -> Config:
    """Parse and validate one v1-lite TOML file.

    Args:
        path: Configuration path selected by discovery or ``--config``.

    Returns:
        A fully validated immutable configuration model.
    """

    path = path.expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc

    _reject_legacy_config(raw)
    _reject_unknown(raw, {"default_target", "source", "build", "outputs", "targets", "deploy"}, "top level")
    root = path.parent.resolve()
    source = _parse_source(raw.get("source", {}))
    build = _parse_build(raw.get("build", {}))
    outputs = _parse_outputs(raw.get("outputs", []), root)
    targets = _parse_targets(raw.get("targets", {}), root)
    deploy = _parse_deploy(raw.get("deploy", {}))
    default_target = _optional_string(raw.get("default_target"), "default_target")
    if default_target is not None and default_target not in targets:
        raise ConfigError(f"default_target {default_target!r} is not present in [targets]")
    return Config(path, root, default_target, source, build, outputs, targets, deploy)


def path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a normalized relative POSIX path matches any glob.

    Args:
        path: Relative POSIX path to test.
        patterns: Git-deploy glob patterns.

    Returns:
        ``True`` when at least one pattern owns the path.
    """

    from fnmatch import fnmatchcase

    normalized = path.strip("/")
    for pattern in patterns:
        pattern = pattern.strip("/")
        if pattern == "**" or fnmatchcase(normalized, pattern):
            return True
        # A recursive prefix owns both top-level and nested matches.
        if pattern.startswith("**/") and fnmatchcase(normalized, pattern[3:]):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
    return False


def is_protected(path: str, config: SourceConfig) -> bool:
    """Return whether a local or remote relative path is protected.

    Args:
        path: Relative POSIX path to check.
        config: Source safety policy containing configured and mandatory patterns.

    Returns:
        ``True`` when deployment must not touch the path.
    """

    candidate = PurePosixPath(path.strip("/"))
    return any(
        path_matches(ancestor.as_posix(), config.protect)
        for ancestor in (candidate, *candidate.parents)
        if ancestor.as_posix() != "."
    )


def is_source_managed(path: str, config: SourceConfig) -> bool:
    """Return whether a committed source path belongs to the managed set.

    Args:
        path: Relative POSIX Git path.
        config: Source include, exclude, and protect policy.

    Returns:
        ``True`` only for included, non-excluded, non-protected paths.
    """

    return (
        path_matches(path, config.include)
        and not path_matches(path, config.exclude)
        and not is_protected(path, config)
    )


def _reject_legacy_config(raw: dict[str, Any]) -> None:
    """Reject v0.x tables explicitly instead of guessing migration semantics."""

    legacy = sorted({"server", "remotes", "projects"}.intersection(raw))
    if legacy:
        raise ConfigError(
            "v0.x configuration is not compatible with v1-lite; found legacy tables: "
            + ", ".join(legacy)
        )


def _parse_source(raw: Any) -> SourceConfig:
    """Validate the source table and merge mandatory safety defaults."""

    table = _table(raw, "source")
    _reject_unknown(table, {"include", "exclude", "protect", "require_clean_worktree"}, "source")
    include = _string_tuple(table.get("include", ["**"]), "source.include", allow_empty=False)
    configured_exclude = _string_tuple(table.get("exclude", []), "source.exclude")
    configured_protect = _string_tuple(table.get("protect", []), "source.protect")
    exclude = tuple(dict.fromkeys((*DEFAULT_EXCLUDE, *configured_exclude)))
    protect = tuple(dict.fromkeys((*DEFAULT_PROTECT, *configured_protect)))
    clean = table.get("require_clean_worktree", False)
    if not isinstance(clean, bool):
        raise ConfigError("source.require_clean_worktree must be a boolean")
    _validate_patterns((*include, *exclude, *protect))
    return SourceConfig(include, exclude, protect, clean)


def _parse_build(raw: Any) -> BuildConfig:
    """Validate build steps and their optional aggregate timeout."""

    table = _table(raw, "build")
    _reject_unknown(table, {"steps", "timeout"}, "build")
    steps = _string_tuple(table.get("steps", []), "build.steps")
    timeout_raw = table.get("timeout", 900)
    if timeout_raw is None:
        timeout = None
    elif isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool) and timeout_raw > 0:
        timeout = float(timeout_raw)
    else:
        raise ConfigError("build.timeout must be a positive number or null")
    return BuildConfig(steps, timeout)


def _parse_outputs(raw: Any, root: Path) -> tuple[OutputConfig, ...]:
    """Validate output mappings and keep every local path inside the project."""

    if not isinstance(raw, list):
        raise ConfigError("outputs must be an array of tables")
    outputs: list[OutputConfig] = []
    for index, value in enumerate(raw):
        table = _table(value, f"outputs[{index}]")
        _reject_unknown(table, {"local", "remote", "delete_removed"}, f"outputs[{index}]")
        local_text = _required_string(table.get("local"), f"outputs[{index}].local")
        local_rel = Path(local_text)
        if local_rel.is_absolute() or ".." in local_rel.parts:
            raise ConfigError(f"outputs[{index}].local must stay inside the project root")
        local = (root / local_rel).resolve()
        if not local.is_relative_to(root):
            raise ConfigError(f"outputs[{index}].local resolves outside the project root")
        remote = _relative_remote(table.get("remote"), f"outputs[{index}].remote")
        delete_removed = table.get("delete_removed", True)
        if not isinstance(delete_removed, bool):
            raise ConfigError(f"outputs[{index}].delete_removed must be a boolean")
        outputs.append(OutputConfig(local, remote, delete_removed))
    _validate_output_roots(outputs)
    return tuple(outputs)


def _validate_output_roots(outputs: list[OutputConfig]) -> None:
    """Reject equal or nested remote roots whose deletion ownership is ambiguous."""

    for index, left in enumerate(outputs):
        for right in outputs[index + 1 :]:
            if (
                left.remote == right.remote
                or left.remote in right.remote.parents
                or right.remote in left.remote.parents
            ):
                raise ConfigError(
                    "output remote mappings must not be equal or nested: "
                    f"{left.remote} and {right.remote}"
                )


def _parse_targets(raw: Any, root: Path) -> dict[str, TargetConfig]:
    """Validate named SFTP/FTP targets and protocol-specific settings."""

    table = _table(raw, "targets")
    if not table:
        raise ConfigError("at least one [targets.NAME] table is required")
    targets: dict[str, TargetConfig] = {}
    for name, value in table.items():
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ConfigError(f"invalid target name: {name!r}")
        item = _table(value, f"targets.{name}")
        _reject_unknown(
            item,
            {
                "protocol",
                "host",
                "username",
                "remote_root",
                "port",
                "password_env",
                "ssh_host_alias",
                "ssh_config_file",
                "known_hosts_file",
                "key_file",
                "use_ssh_agent",
                "strict_host_key_checking",
                "passive",
                "timeout",
            },
            f"targets.{name}",
        )
        protocol_value = _required_string(item.get("protocol"), f"targets.{name}.protocol").lower()
        if protocol_value not in {"sftp", "ftp"}:
            raise ConfigError(f"targets.{name}.protocol must be 'sftp' or 'ftp'")
        protocol = cast(Protocol, protocol_value)
        alias = _optional_string(item.get("ssh_host_alias"), f"targets.{name}.ssh_host_alias")
        host = _optional_string(item.get("host"), f"targets.{name}.host")
        if not host and not (protocol == "sftp" and alias):
            raise ConfigError(f"targets.{name} requires host or an SFTP ssh_host_alias")
        username = _optional_string(item.get("username"), f"targets.{name}.username")
        if protocol == "ftp" and not username:
            raise ConfigError(f"targets.{name}.username is required for FTP")
        remote_root = _absolute_remote(item.get("remote_root"), f"targets.{name}.remote_root")
        port_default = 22 if protocol == "sftp" else 21
        port = item.get("port", port_default)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"targets.{name}.port must be between 1 and 65535")
        timeout = item.get("timeout", 15)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ConfigError(f"targets.{name}.timeout must be positive")
        password_env = _optional_string(item.get("password_env"), f"targets.{name}.password_env")
        if protocol == "ftp" and not password_env:
            raise ConfigError(f"targets.{name}.password_env is required for FTP")
        key_raw = _optional_string(item.get("key_file"), f"targets.{name}.key_file")
        key_file = _resolve_user_path(key_raw, root) if key_raw else None
        config_raw = _optional_string(item.get("ssh_config_file"), f"targets.{name}.ssh_config_file")
        ssh_config = _resolve_user_path(config_raw or "~/.ssh/config", root)
        known_hosts_raw = _optional_string(
            item.get("known_hosts_file"), f"targets.{name}.known_hosts_file"
        )
        known_hosts_file = _resolve_user_path(known_hosts_raw, root) if known_hosts_raw else None
        use_agent = item.get("use_ssh_agent", True)
        strict = item.get("strict_host_key_checking", True)
        passive = item.get("passive", True)
        for field_name, setting in (
            ("use_ssh_agent", use_agent),
            ("strict_host_key_checking", strict),
            ("passive", passive),
        ):
            if not isinstance(setting, bool):
                raise ConfigError(f"targets.{name}.{field_name} must be a boolean")
        if protocol == "sftp" and alias:
            conflicting = sorted(
                key
                for key in (
                    "host",
                    "username",
                    "port",
                    "password_env",
                    "known_hosts_file",
                    "key_file",
                    "use_ssh_agent",
                    "strict_host_key_checking",
                )
                if key in item
            )
            if conflicting:
                raise ConfigError(
                    f"targets.{name} ssh_host_alias uses Native OpenSSH and conflicts with: "
                    + ", ".join(conflicting)
                )
        if protocol == "ftp" and any(
            key in item
            for key in (
                "ssh_host_alias",
                "ssh_config_file",
                "known_hosts_file",
                "key_file",
                "use_ssh_agent",
                "strict_host_key_checking",
            )
        ):
            raise ConfigError(f"targets.{name} contains SFTP-only settings for an FTP target")
        targets[name] = TargetConfig(
            name=name,
            protocol=protocol,
            host=host,
            username=username,
            remote_root=remote_root,
            port=port,
            port_explicit="port" in item,
            password_env=password_env,
            ssh_host_alias=alias,
            ssh_config_file=ssh_config,
            ssh_config_explicit="ssh_config_file" in item,
            known_hosts_file=known_hosts_file,
            key_file=key_file,
            use_ssh_agent=use_agent,
            strict_host_key_checking=strict,
            passive=passive,
            timeout=float(timeout),
        )
    return targets


def _parse_deploy(raw: Any) -> DeployConfig:
    """Validate retry count and delay."""

    table = _table(raw, "deploy")
    _reject_unknown(table, {"retries", "retry_delay"}, "deploy")
    retries = table.get("retries", 3)
    delay = table.get("retry_delay", 2)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
        raise ConfigError("deploy.retries must be a positive integer")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError("deploy.retry_delay must be a non-negative number")
    return DeployConfig(retries, float(delay))


def _table(value: Any, name: str) -> dict[str, Any]:
    """Require and return a TOML table."""

    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _reject_unknown(table: dict[str, Any], allowed: set[str], name: str) -> None:
    """Reject misspelled or legacy fields instead of silently ignoring them."""

    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown {name} field(s): {', '.join(unknown)}")


def _string_tuple(value: Any, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    """Require a list of non-empty strings and return an immutable tuple."""

    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f"{name} must be an array of non-empty strings")
    result = tuple(item.strip() for item in value)
    if not allow_empty and not result:
        raise ConfigError(f"{name} must not be empty")
    return result


def _required_string(value: Any, name: str) -> str:
    """Require and normalize one non-empty string."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, name: str) -> str | None:
    """Validate an optional non-empty string."""

    if value is None:
        return None
    return _required_string(value, name)


def _relative_remote(value: Any, name: str) -> PurePosixPath:
    """Require a safe relative POSIX remote path."""

    text = _required_string(value, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{name} must be a relative POSIX path")
    return path


def _absolute_remote(value: Any, name: str) -> PurePosixPath:
    """Require an absolute normalized POSIX remote root."""

    text = _required_string(value, name).replace("\\", "/")
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{name} must be an absolute POSIX path")
    return path


def _resolve_user_path(value: str, root: Path) -> Path:
    """Resolve a user/config-relative local path."""

    expanded = Path(value).expanduser()
    return expanded.resolve() if expanded.is_absolute() else (root / expanded).resolve()


def _validate_patterns(patterns: tuple[str, ...]) -> None:
    """Reject absolute and parent-traversing path patterns."""

    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
            raise ConfigError(f"unsafe path pattern: {pattern!r}")
