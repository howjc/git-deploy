"""Read-only deployment history application service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from git_deploy.errors import ConfigurationError
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.models import DeploymentManifest
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base

from .config_service import ApplicationConfigService, ProjectSelection
from .models import HistoryRequest
from .plan_token import StalePlanError


class HistoryLineage(StrEnum):
    """Stable distinction between v0.2 stateful and legacy history."""

    STATEFUL = "stateful"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """Renderer-neutral deployment history row."""

    deployment_id: str
    created_at: str
    status: str
    revision_specs: tuple[str, ...]
    from_commit: str
    to_commit: str
    file_count: int
    lineage: HistoryLineage
    before_generation: int | None
    after_generation: int | None
    transaction_id: str | None


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """One local history page or selected deployment detail."""

    selection: ProjectSelection
    entries: tuple[HistoryEntry, ...]
    total: int
    offset: int
    next_offset: int | None


class HistoryService:
    """Merge target-scoped and legacy manifests without remote or state writes."""

    def __init__(self, config: ApplicationConfigService):
        """Bind the shared application configuration service.

        Args:
            config: Validated application configuration selector.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        self._config = config

    def history(self, request: HistoryRequest) -> HistoryResult:
        """Return a paged or selected local deployment history result.

        Args:
            request: Immutable local-read history request.

        Returns:
            Structured history rows with explicit lineage classification.
        """

        if not isinstance(request, HistoryRequest):
            raise TypeError("request must be a HistoryRequest")
        _alias, _server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            request.expected_target_id != selection.target_id
            or request.expected_physical_fingerprint
            != selection.physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before history read")
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        current = ExpectedStateStore(target_root, identity).read_current()
        generation = current.generation if current is not None else None
        if generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before history read: "
                f"expected {request.expected_generation}, actual {generation}"
            )

        manifests = _merged_manifests(project, target_root)
        if request.deployment_id is not None:
            selected = _select_manifest(manifests, request.deployment_id)
            page = (selected,)
            offset = 0
            next_offset = None
        else:
            offset = request.offset
            page = tuple(manifests[offset : offset + request.limit])
            following = offset + len(page)
            next_offset = following if following < len(manifests) else None
        return HistoryResult(
            selection=selection,
            entries=tuple(_history_entry(item) for item in page),
            total=len(manifests),
            offset=offset,
            next_offset=next_offset,
        )


def _merged_manifests(project, target_root) -> list[DeploymentManifest]:
    """Merge legacy and target-scoped history, preferring stateful records."""

    by_id = {
        item.deployment_id: item
        for item in DeploymentStore(project).list_manifests()
    }
    for item in DeploymentStore(project, root=target_root).list_manifests():
        by_id[item.deployment_id] = item
    return sorted(by_id.values(), key=lambda item: item.deployment_id, reverse=True)


def _select_manifest(
    manifests: list[DeploymentManifest],
    deployment_id: str,
) -> DeploymentManifest:
    """Resolve an exact or unique deployment prefix from merged history."""

    exact = [item for item in manifests if item.deployment_id == deployment_id]
    if exact:
        return exact[0]
    matches = [item for item in manifests if item.deployment_id.startswith(deployment_id)]
    if not matches:
        raise ConfigurationError(f"deployment not found: {deployment_id}")
    if len(matches) > 1:
        raise ConfigurationError(f"deployment prefix is ambiguous: {deployment_id}")
    return matches[0]


def _history_entry(manifest: DeploymentManifest) -> HistoryEntry:
    """Convert a persisted manifest to a renderer-neutral history row."""

    stateful = bool(
        manifest.state == "v1"
        or manifest.after_state_id
        or manifest.after_generation is not None
    )
    return HistoryEntry(
        deployment_id=manifest.deployment_id,
        created_at=manifest.created_at,
        status=manifest.status,
        revision_specs=tuple(manifest.revision_specs),
        from_commit=manifest.from_commit,
        to_commit=manifest.to_commit,
        file_count=len(manifest.snapshots),
        lineage=(HistoryLineage.STATEFUL if stateful else HistoryLineage.LEGACY),
        before_generation=manifest.before_generation,
        after_generation=manifest.after_generation,
        transaction_id=manifest.transaction_id,
    )
