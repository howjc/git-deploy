"""Physical target identity and managed-state policy fingerprints.

Physical ``target_id`` is derived only from protocol/host/effective-port/project/
remote_root. Username, alias, credentials, and build/secret settings never enter
the physical payload so multiple connection identities can share state and locks.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError
from .models import ProjectConfig, ServerConfig

SCHEMA_VERSION = 1

# Shared defaults with transport.connect: explicit FTPS (FTP_TLS) uses 21, not 990.
_DEFAULT_PORTS: dict[str, int] = {
    "sftp": 22,
    "ftp": 21,
    "ftps": 21,
    "ssh": 22,
}

_TARGET_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class PhysicalTargetPayload:
    """Canonical physical identity fields for one deploy target.

    Attributes:
        protocol: Lowercased transport protocol.
        host: Normalized DNS host or IP text.
        port: Effective port including protocol defaults.
        project: Configured project key.
        remote_root: Normalized absolute POSIX remote root.
    """

    protocol: str
    host: str
    port: int
    project: str
    remote_root: str

    def canonical_dict(self) -> dict[str, Any]:
        """Return sorted-key-ready canonical mapping for hashing.

        Returns:
            JSON-serializable payload with fixed field names.
        """

        return {
            "host": self.host,
            "port": self.port,
            "project": self.project,
            "protocol": self.protocol,
            "remote_root": self.remote_root,
            "schema_version": SCHEMA_VERSION,
        }

    def fingerprint(self) -> str:
        """Return SHA-256 of the canonical JSON payload.

        Returns:
            Lowercase hexadecimal fingerprint.
        """

        return _sha256_canonical(self.canonical_dict())


@dataclass(frozen=True)
class TargetIdentity:
    """Resolved physical identity used for state directory layout.

    Attributes:
        target_id: Stable directory/id name (explicit or derived).
        payload: Canonical physical fields.
        physical_fingerprint: Hash of the physical payload.
        explicit: Whether ``target_id`` was configured explicitly.
    """

    target_id: str
    payload: PhysicalTargetPayload
    physical_fingerprint: str
    explicit: bool = False

    def state_root(self, base: Path) -> Path:
        """Return ``targets/<target-id>`` under a project state base.

        Args:
            base: Project-level state directory.

        Returns:
            Absolute target state root.
        """

        return (base / "targets" / self.target_id).resolve()


def normalize_protocol(protocol: str) -> str:
    """Lowercase and validate a transport protocol name.

    Args:
        protocol: Raw protocol string from configuration.

    Returns:
        Normalized protocol.
    """

    value = protocol.strip().lower()
    if not value:
        raise ConfigurationError("protocol is required for target identity")
    return value


def normalize_host(host: str) -> str:
    """Normalize DNS hosts (lowercase, strip trailing dots) and IP literals.

    Args:
        host: Raw host from configuration.

    Returns:
        Canonical host text for identity.
    """

    raw = host.strip()
    if not raw:
        raise ConfigurationError("host is required for target identity")
    # Strip surrounding brackets for IPv6 literals such as [::1].
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        # DNS: lowercase and remove a single trailing root zone dot.
        dns = raw.rstrip(".").lower()
        if not dns:
            raise ConfigurationError(f"invalid host for target identity: {host!r}")
        return dns


def default_port_for_protocol(protocol: str) -> int:
    """Return the transport default TCP port for a protocol (single source of truth).

    Explicit FTPS uses control port 21 (same as ``FtpTransport`` / ``FTP_TLS``).

    Args:
        protocol: Raw or normalized protocol name.

    Returns:
        Default TCP port for identity and connect when config omits ``port``.
    """

    normalized = normalize_protocol(protocol)
    try:
        return _DEFAULT_PORTS[normalized]
    except KeyError as exc:
        raise ConfigurationError(
            f"port is required for protocol without a default: {normalized}"
        ) from exc


def effective_port(protocol: str, port: int | str | None) -> int:
    """Resolve the effective port, filling protocol defaults when omitted.

    Args:
        protocol: Normalized protocol name.
        port: Explicit port or ``None``/empty to use the protocol default.

    Returns:
        Effective TCP port.
    """

    if port is None or port == "":
        return default_port_for_protocol(protocol)
    value = int(port)
    if value < 1 or value > 65535:
        raise ConfigurationError(f"invalid port for target identity: {port!r}")
    return value


def normalize_remote_root(remote_root: str) -> str:
    """Normalize an absolute POSIX remote root for identity comparison.

    Args:
        remote_root: Absolute remote directory.

    Returns:
        Canonical root with collapsed separators and no ``.``/``..``.
    """

    value = remote_root.strip()
    if not value.startswith("/"):
        raise ConfigurationError("remote_root must be an absolute POSIX path")
    pure = PurePosixPath(value)
    if ".." in pure.parts or "." in pure.parts:
        raise ConfigurationError("remote_root cannot contain traversal segments")
    # Collapse repeated separators via PurePosixPath reconstruction.
    normalized = pure.as_posix()
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized or "/"


def build_physical_payload(
    *,
    protocol: str,
    host: str,
    project: str,
    remote_root: str,
    port: int | str | None = None,
) -> PhysicalTargetPayload:
    """Build a normalized physical target payload.

    Args:
        protocol: Transport protocol.
        host: DNS host or IP.
        project: Project key.
        remote_root: Absolute remote root.
        port: Optional explicit port.

    Returns:
        Canonical physical payload.
    """

    normalized_protocol = normalize_protocol(protocol)
    return PhysicalTargetPayload(
        protocol=normalized_protocol,
        host=normalize_host(host),
        port=effective_port(normalized_protocol, port),
        project=project.strip(),
        remote_root=normalize_remote_root(remote_root),
    )


def derive_target_id(payload: PhysicalTargetPayload) -> str:
    """Derive a stable target directory id from a physical payload.

    Args:
        payload: Canonical physical fields.

    Returns:
        Short stable identifier based on the physical fingerprint.
    """

    return f"tgt-{payload.fingerprint()[:16]}"


def resolve_target_identity(
    server: ServerConfig | Mapping[str, Any],
    project: ProjectConfig | str,
    *,
    remote_root: str | None = None,
    explicit_target_id: str | None = None,
    bound_payload: PhysicalTargetPayload | None = None,
) -> TargetIdentity:
    """Resolve physical identity for a named remote + project combination.

    Args:
        server: Server/remote connection settings (username/password ignored).
        project: Project configuration or project key string.
        remote_root: Optional override root (defaults to project remote_root).
        explicit_target_id: Optional configured stable name for this payload only.
        bound_payload: When set with an explicit id, rejects cross-payload reuse.

    Returns:
        Resolved target identity.

    Raises:
        ConfigurationError: When an explicit id is bound to a different payload.
    """

    values = server.values if isinstance(server, ServerConfig) else dict(server)
    if isinstance(project, ProjectConfig):
        project_name = project.name
        root = remote_root if remote_root is not None else project.remote_root
        configured_explicit = getattr(project, "target_id", None)
    else:
        project_name = project
        if remote_root is None:
            raise ConfigurationError("remote_root is required when project is a name")
        root = remote_root
        configured_explicit = None

    explicit = explicit_target_id if explicit_target_id is not None else configured_explicit
    payload = build_physical_payload(
        protocol=str(values.get("protocol", "sftp")),
        host=str(values.get("host", "")),
        port=values.get("port"),
        project=project_name,
        remote_root=root,
    )
    fingerprint = payload.fingerprint()

    if explicit:
        _validate_explicit_target_id(explicit)
        if bound_payload is not None and bound_payload.canonical_dict() != payload.canonical_dict():
            raise ConfigurationError(
                f"explicit target_id {explicit!r} cannot merge distinct physical payloads"
            )
        return TargetIdentity(
            target_id=explicit,
            payload=payload,
            physical_fingerprint=fingerprint,
            explicit=True,
        )

    return TargetIdentity(
        target_id=derive_target_id(payload),
        payload=payload,
        physical_fingerprint=fingerprint,
        explicit=False,
    )


def assert_explicit_id_matches_payload(
    explicit_id: str,
    payload: PhysicalTargetPayload,
    existing_payload: PhysicalTargetPayload,
) -> None:
    """Reject an explicit id when it would force-merge distinct payloads.

    Args:
        explicit_id: Configured stable target name.
        payload: Newly resolved physical payload.
        existing_payload: Payload already bound to the explicit id.

    Returns:
        None.

    Raises:
        ConfigurationError: When payloads differ.
    """

    if payload.canonical_dict() != existing_payload.canonical_dict():
        raise ConfigurationError(
            f"explicit target_id {explicit_id!r} cannot merge distinct physical payloads"
        )


def managed_policy_fingerprint(
    *,
    repository_identity: str,
    include: Sequence[str] = ("**",),
    exclude: Sequence[str] = (),
    protected: Sequence[str] = (),
    artifact_destinations: Sequence[str] = (),
) -> str:
    """Hash managed-state policy fields that require explicit migration.

    Build commands, Docker images, and secret references intentionally do not
    participate so cache/build changes never look like target/policy drift.

    Args:
        repository_identity: Stable repository identity (for example origin URL or path id).
        include: Source include globs.
        exclude: Source exclude globs.
        protected: Protected path globs.
        artifact_destinations: Artifact destination roots under remote_root.

    Returns:
        Lowercase hexadecimal policy fingerprint.
    """

    payload = {
        "artifact_destinations": list(artifact_destinations),
        "exclude": list(exclude),
        "include": list(include),
        "protected": list(protected),
        "repository_identity": repository_identity,
        "schema_version": SCHEMA_VERSION,
    }
    return _sha256_canonical(payload)


def policy_fingerprint_for_project(
    project: ProjectConfig,
    *,
    repository_identity: str | None = None,
    artifact_destinations: Sequence[str] = (),
) -> str:
    """Compute managed policy fingerprint from a project configuration.

    Args:
        project: Resolved project configuration.
        repository_identity: Optional override identity; defaults to repository path.
        artifact_destinations: Artifact destinations (empty until Gate B).

    Returns:
        Policy fingerprint hex digest.
    """

    identity = repository_identity
    if identity is None:
        identity = str(project.repository.resolve())
    return managed_policy_fingerprint(
        repository_identity=identity,
        include=project.include,
        exclude=project.exclude,
        protected=project.protected,
        artifact_destinations=artifact_destinations,
    )


def default_state_base(project_name: str, local_state_dir: Path | None = None) -> Path:
    """Resolve the project-level state base directory.

    Args:
        project_name: Configured project key.
        local_state_dir: Optional explicit local state directory.

    Returns:
        Absolute project state base (targets live under ``targets/``).
    """

    if local_state_dir is not None:
        return local_state_dir.resolve()
    import os

    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.local/state").expanduser()
    return (base / "git-deploy" / project_name).resolve()


def _validate_explicit_target_id(target_id: str) -> None:
    """Validate a user-supplied target identifier.

    Args:
        target_id: Candidate explicit id.

    Returns:
        None.
    """

    if not _TARGET_ID_SAFE.fullmatch(target_id):
        raise ConfigurationError(f"invalid explicit target_id: {target_id!r}")


def _sha256_canonical(payload: Mapping[str, Any]) -> str:
    """Hash a mapping as canonical sorted JSON.

    Args:
        payload: JSON-compatible mapping.

    Returns:
        Hex digest.
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
