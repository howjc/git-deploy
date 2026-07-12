"""Typed deployment plans and persisted manifest records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    """Connection settings for one named remote environment."""

    values: dict[str, Any]


@dataclass(frozen=True)
class ProjectRemoteConfig:
    """Optional project policy overrides for one named remote."""

    remote_root: str | None = None
    post_commands: tuple[str, ...] | None = None
    health_urls: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ProjectConfig:
    """Resolved paths and policies for one deployable Git project."""

    name: str
    repository: Path
    remote_root: str
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    post_commands: tuple[str, ...] = ()
    health_urls: tuple[str, ...] = ()
    local_state_dir: Path | None = None
    remote: str = "default"
    remotes: dict[str, ProjectRemoteConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """Fully resolved deployment configuration and its source path."""

    path: Path
    remotes: dict[str, ServerConfig]
    projects: dict[str, ProjectConfig]
    default_remote: str | None = None


@dataclass(frozen=True)
class GitChange:
    """One normalized Git name-status entry between two commits."""

    status: str
    path: str
    old_path: str | None = None
    score: int | None = None


@dataclass(frozen=True)
class PlannedFile:
    """One remote path mutation derived from a Git change."""

    action: str
    path: str
    remote_path: str
    source_path: str | None
    expected_before_sha256: str | None
    target_sha256: str | None
    target_size: int = 0
    executable: bool = False
    expected_before_executable: bool | None = None


@dataclass(frozen=True)
class DeploymentPlan:
    """Immutable deployment plan for a single configured project."""

    project: str
    repository: Path
    remote_root: str
    from_commit: str
    to_commit: str
    files: tuple[PlannedFile, ...]
    excluded: tuple[GitChange, ...] = ()
    revision_specs: tuple[str, ...] = ()


@dataclass
class FileSnapshot:
    """Persisted before/after state for one touched remote path."""

    path: str
    remote_path: str
    before_exists: bool
    before_sha256: str | None
    backup_file: str | None
    after_exists: bool
    after_sha256: str | None
    before_executable: bool | None = None
    after_executable: bool | None = None


@dataclass
class DeploymentManifest:
    """Serializable record used to verify and reverse one deployment."""

    deployment_id: str
    project: str
    repository: str
    remote_root: str
    from_commit: str
    to_commit: str
    created_at: str
    status: str
    snapshots: list[FileSnapshot] = field(default_factory=list)
    error: str | None = None
    revision_specs: list[str] = field(default_factory=list)
    remote: str = "default"

    def to_dict(self) -> dict[str, Any]:
        """Return the complete manifest as JSON-compatible primitives."""

        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeploymentManifest":
        """Build a manifest from parsed JSON content.

        Args:
            payload: JSON-compatible manifest mapping.

        Returns:
            Rehydrated deployment manifest.
        """

        values = dict(payload)
        values["snapshots"] = [FileSnapshot(**row) for row in payload.get("snapshots", [])]
        return cls(**values)
