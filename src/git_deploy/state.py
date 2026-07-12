"""Local deployment backup and manifest persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import ConfigurationError
from .models import DeploymentManifest, ProjectConfig


class DeploymentStore:
    """Persist deployment metadata below a project-specific state directory."""

    def __init__(self, project: ProjectConfig):
        """Resolve the state location without creating it.

        Args:
            project: Project whose deployment history is stored.
        """

        self.project = project
        base = project.local_state_dir or _default_state_root(project.name)
        # Named remotes must never share deployment history or rollback backups.
        self.root = base if project.remote == "default" else base / "remotes" / project.remote

    def deployment_dir(self, deployment_id: str) -> Path:
        """Return a validated deployment record directory.

        Args:
            deployment_id: Exact local deployment identifier.

        Returns:
            Directory reserved for the deployment.
        """

        _validate_deployment_id(deployment_id)
        return self.root / "deployments" / deployment_id

    def write_backup(self, deployment_id: str, index: int, data: bytes) -> str:
        """Persist one pre-deployment file backup atomically.

        Args:
            deployment_id: Owning deployment identifier.
            index: Stable snapshot sequence number.
            data: Exact remote bytes captured before mutation.

        Returns:
            Deployment-directory-relative backup path.
        """

        relative = Path("backups") / f"{index:05d}.bin"
        destination = self.deployment_dir(deployment_id) / relative
        _atomic_write(destination, data)
        return relative.as_posix()

    def read_backup(self, deployment_id: str, relative_path: str) -> bytes:
        """Read one backup while preventing state-directory traversal.

        Args:
            deployment_id: Owning deployment identifier.
            relative_path: Manifest-relative backup path.

        Returns:
            Persisted backup bytes.
        """

        candidate = (self.deployment_dir(deployment_id) / relative_path).resolve()
        base = self.deployment_dir(deployment_id).resolve()
        if candidate != base and base not in candidate.parents:
            raise ConfigurationError(f"unsafe backup path in manifest: {relative_path}")
        try:
            return candidate.read_bytes()
        except OSError as exc:
            raise ConfigurationError(f"cannot read deployment backup {candidate}: {exc}") from exc

    def write_manifest(self, manifest: DeploymentManifest) -> None:
        """Persist a deployment manifest atomically.

        Args:
            manifest: Current deployment record.
        """

        payload = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _atomic_write(self.deployment_dir(manifest.deployment_id) / "manifest.json", payload)

    def load(self, deployment_id: str) -> DeploymentManifest:
        """Load one exact or unambiguous prefix deployment identifier.

        Args:
            deployment_id: Full deployment identifier or unique prefix.

        Returns:
            Parsed deployment manifest.
        """

        resolved = self.resolve_id(deployment_id)
        path = self.deployment_dir(resolved) / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("manifest root is not an object")
            return DeploymentManifest.from_dict(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read deployment manifest {path}: {exc}") from exc

    def resolve_id(self, deployment_id: str) -> str:
        """Resolve an exact deployment ID or a unique prefix.

        Args:
            deployment_id: Exact identifier or prefix.

        Returns:
            Exact deployment identifier.
        """

        _validate_deployment_id(deployment_id)
        manifests = self.list_manifests()
        exact = [item.deployment_id for item in manifests if item.deployment_id == deployment_id]
        if exact:
            return exact[0]
        matches = [item.deployment_id for item in manifests if item.deployment_id.startswith(deployment_id)]
        if not matches:
            raise ConfigurationError(f"deployment not found: {deployment_id}")
        if len(matches) > 1:
            raise ConfigurationError(f"deployment prefix is ambiguous: {deployment_id}")
        return matches[0]

    def latest_successful(self) -> DeploymentManifest:
        """Return the newest deployment still eligible for rollback.

        Returns:
            Newest manifest with ``succeeded`` status.
        """

        for manifest in self.list_manifests():
            if manifest.status == "succeeded":
                return manifest
        raise ConfigurationError(f"no successful deployment found for project {self.project.name}")

    def list_manifests(self) -> list[DeploymentManifest]:
        """Load local deployment manifests in newest-first order.

        Returns:
            Valid manifests sorted by deployment identifier descending.
        """

        deployments = self.root / "deployments"
        if not deployments.is_dir():
            return []
        manifests: list[DeploymentManifest] = []
        for path in sorted(deployments.glob("*/manifest.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    manifests.append(DeploymentManifest.from_dict(payload))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return sorted(manifests, key=lambda item: item.deployment_id, reverse=True)


def _default_state_root(project_name: str) -> Path:
    """Resolve the XDG-compatible default state directory.

    Args:
        project_name: Configured project key.

    Returns:
        Absolute project state directory.
    """

    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.local/state").expanduser()
    return (base / "git-deploy" / project_name).resolve()


def _validate_deployment_id(deployment_id: str) -> None:
    """Reject empty or path-like deployment identifiers.

    Args:
        deployment_id: Candidate full identifier or prefix.
    """

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not deployment_id or any(character not in allowed for character in deployment_id):
        raise ConfigurationError(f"invalid deployment identifier: {deployment_id!r}")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes through a sibling temporary file and atomic replace.

    Args:
        path: Final local path.
        data: Bytes to persist.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
