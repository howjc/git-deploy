"""Local and read-only remote deployment verification service."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from git_deploy.errors import ConfigurationError, PolicyError
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.models import DeploymentManifest, FileSnapshot
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base

from .config_service import ApplicationConfigService, ProjectSelection
from .models import VerifyRequest
from .plan_token import StalePlanError


class VerifyMode(StrEnum):
    """Explicit verification side-effect modes."""

    LOCAL = "local"
    REMOTE_READ = "remote_read"


@dataclass(frozen=True, slots=True)
class VerifyPathResult:
    """Renderer-neutral comparison for one deployment path."""

    path: str
    remote_path: str
    status: str
    expected_sha256: str | None
    actual_sha256: str | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Structured local or remote-read deployment verification result."""

    selection: ProjectSelection
    deployment_id: str
    mode: VerifyMode
    paths: tuple[VerifyPathResult, ...]
    ok: bool
    remote_read_calls: int
    remote_write_calls: int


class VerifyTransport(Protocol):
    """Minimal read-only transport surface required by verification."""

    def read_file(self, remote_path: str) -> bytes | None:
        """Return remote bytes or None when the path is absent."""


TransportFactory = Callable[[dict[str, object]], VerifyTransport]


class VerifyService:
    """Verify manifests locally or compare remote bytes without mutations."""

    def __init__(
        self,
        config: ApplicationConfigService,
        *,
        transport_factory: TransportFactory | None = None,
    ):
        """Bind configuration and an injectable read-only transport factory.

        Args:
            config: Shared application configuration service.
            transport_factory: Optional fake/real transport constructor.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        self._config = config
        self._transport_factory = transport_factory

    def verify(self, request: VerifyRequest) -> VerifyResult:
        """Verify one selected deployment in explicit local or remote-read mode.

        Args:
            request: Immutable verification request.

        Returns:
            Structured path classifications and transport counters.
        """

        if not isinstance(request, VerifyRequest):
            raise TypeError("request must be a VerifyRequest")
        _alias, server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            request.expected_target_id != selection.target_id
            or request.expected_physical_fingerprint
            != selection.physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before verify")
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        current = ExpectedStateStore(target_root, identity).read_current()
        generation = current.generation if current is not None else None
        if generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before verify: "
                f"expected {request.expected_generation}, actual {generation}"
            )
        manifest = _selected_manifest(
            project,
            target_root,
            request.deployment_id,
            request.latest,
        )
        if not request.remote_check:
            paths = tuple(_local_path(item, manifest) for item in manifest.snapshots)
            return VerifyResult(
                selection=selection,
                deployment_id=manifest.deployment_id,
                mode=VerifyMode.LOCAL,
                paths=paths,
                ok=True,
                remote_read_calls=0,
                remote_write_calls=0,
            )

        transport = self._open_transport(dict(server.values))
        writes_before = int(getattr(transport, "write_calls", 0))
        reads_before = int(getattr(transport, "read_calls", 0))
        try:
            paths = tuple(_remote_path(transport, item, manifest) for item in manifest.snapshots)
            writes_after = int(getattr(transport, "write_calls", writes_before))
            reads_after = int(getattr(transport, "read_calls", reads_before + len(paths)))
            writes = writes_after - writes_before
            if writes != 0:
                raise PolicyError("read-only verify performed a remote write")
            return VerifyResult(
                selection=selection,
                deployment_id=manifest.deployment_id,
                mode=VerifyMode.REMOTE_READ,
                paths=paths,
                ok=all(item.status == "match" for item in paths),
                remote_read_calls=reads_after - reads_before,
                remote_write_calls=writes,
            )
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    def _open_transport(self, values: dict[str, object]) -> VerifyTransport:
        """Open the configured transport only for remote-read mode."""

        if self._transport_factory is not None:
            return self._transport_factory(values)
        from git_deploy.remote_verify import open_cli_transport

        return open_cli_transport(values)


def _selected_manifest(
    project,
    target_root,
    deployment_id: str | None,
    latest: bool,
) -> DeploymentManifest:
    """Select one manifest from merged target-scoped and legacy history."""

    by_id = {
        item.deployment_id: item
        for item in DeploymentStore(project).list_manifests()
    }
    for item in DeploymentStore(project, root=target_root).list_manifests():
        by_id[item.deployment_id] = item
    manifests = sorted(by_id.values(), key=lambda item: item.deployment_id, reverse=True)
    if latest:
        for item in manifests:
            if item.status == "succeeded":
                return item
        raise ConfigurationError(f"no successful deployment found for project {project.name}")
    assert deployment_id is not None
    exact = [item for item in manifests if item.deployment_id == deployment_id]
    matches = exact or [
        item for item in manifests if item.deployment_id.startswith(deployment_id)
    ]
    if not matches:
        raise ConfigurationError(f"deployment not found: {deployment_id}")
    if len(matches) > 1:
        raise ConfigurationError(f"deployment prefix is ambiguous: {deployment_id}")
    return matches[0]


def _expected_after(manifest: DeploymentManifest) -> bool:
    """Return which snapshot side represents the manifest's current outcome."""

    return manifest.status not in {"rolled_back", "auto_rolled_back"}


def _local_path(snapshot: FileSnapshot, manifest: DeploymentManifest) -> VerifyPathResult:
    """Return a local-only path expectation without reading remote bytes."""

    after = _expected_after(manifest)
    return VerifyPathResult(
        path=snapshot.path,
        remote_path=snapshot.remote_path,
        status="unverified",
        expected_sha256=(snapshot.after_sha256 if after else snapshot.before_sha256),
        actual_sha256=None,
    )


def _remote_path(
    transport: VerifyTransport,
    snapshot: FileSnapshot,
    manifest: DeploymentManifest,
) -> VerifyPathResult:
    """Read and classify one remote deployment snapshot path."""

    after = _expected_after(manifest)
    expected_exists = snapshot.after_exists if after else snapshot.before_exists
    expected_hash = snapshot.after_sha256 if after else snapshot.before_sha256
    actual = transport.read_file(snapshot.remote_path)
    actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None
    if not expected_exists:
        status = "match" if actual is None else "drift"
    elif actual is None:
        status = "absent"
    elif actual_hash == expected_hash:
        status = "match"
    else:
        status = "drift"
    return VerifyPathResult(
        path=snapshot.path,
        remote_path=snapshot.remote_path,
        status=status,
        expected_sha256=expected_hash,
        actual_sha256=actual_hash,
    )
