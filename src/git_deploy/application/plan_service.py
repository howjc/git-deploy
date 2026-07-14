"""Local-only revision planning service with signed plan credentials."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from git_deploy.errors import PolicyError
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.gitrepo import GitDeploymentPlanner
from git_deploy.models import PlannedFile, ProjectConfig
from git_deploy.state_guards import StateGuards
from git_deploy.state_planner import StatePlanner
from git_deploy.target_identity import default_state_base

from .config_service import ApplicationConfigService, ProjectSelection
from .models import DeployRequest, PlanRequest
from .plan_token import OperationPlanToken, PlanTokenSigner, StalePlanError


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """Renderer-neutral source file mutation."""

    action: str
    path: str
    target_size: int
    executable: bool


@dataclass(frozen=True, slots=True)
class ArtifactMappingPlan:
    """Configured artifact mapping deferred until an authorized build."""

    source: str
    destination: str
    kind: str
    status: str = "deferred_until_build"


@dataclass(frozen=True, slots=True)
class BuildPlanSummary:
    """Secret-safe build inputs displayed during local planning."""

    enabled: bool
    runner: str | None
    commands: tuple[tuple[str, ...], ...]
    build_origin: str
    artifacts_origin: str
    uses_secret: bool


@dataclass(frozen=True, slots=True)
class RevisionPlanResult:
    """Structured local plan shared by CLI and future TUI adapters."""

    selection: ProjectSelection
    generation: int | None
    before_tree_id: str
    after_tree_id: str
    source_changes: tuple[PlannedChange, ...]
    excluded_paths: tuple[str, ...]
    introduced_transition_ids: tuple[str, ...]
    artifacts: tuple[ArtifactMappingPlan, ...]
    build: BuildPlanSummary
    warnings: tuple[str, ...]
    static_noop: bool
    remote_verified: bool
    plan_digest: str
    plan_token: OperationPlanToken


class RevisionPlanService:
    """Plan source/build intent without remote, state, cache, or worktree writes."""

    def __init__(
        self,
        config: ApplicationConfigService,
        signer: PlanTokenSigner,
    ):
        """Bind configuration selection and caller-owned token signer.

        Args:
            config: Shared application configuration service.
            signer: Plan credential signer used again during execution.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        if not isinstance(signer, PlanTokenSigner):
            raise TypeError("signer must be a PlanTokenSigner")
        self._config = config
        self._signer = signer

    def plan(self, request: PlanRequest | DeployRequest) -> RevisionPlanResult:
        """Build a deterministic local-only revision plan.

        Args:
            request: Plan request or eventual deploy request being reviewed.

        Returns:
            Structured source/artifact/build/warning result and signed token.

        Raises:
            PolicyError: If a remote read was requested through this local service.
            StalePlanError: If expected target or generation differs from state.
        """

        if not isinstance(request, (PlanRequest, DeployRequest)):
            raise TypeError("request must be a PlanRequest or DeployRequest")
        if request.check_remote:
            raise PolicyError("local revision plan does not perform remote checks")

        _alias, _server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            selection.target_id != request.expected_target_id
            or selection.physical_fingerprint
            != request.expected_physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before planning")

        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        store = ExpectedStateStore(target_root, identity)
        loaded = store.load_current_state()
        actual_generation = loaded[0].generation if loaded is not None else None
        if actual_generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before planning: "
                f"expected {request.expected_generation}, actual {actual_generation}"
            )

        if loaded is None:
            source = GitDeploymentPlanner(project).build_revisions(request.revisions)
            before_tree = source.from_commit
            after_tree = source.to_commit
            files = source.files
            excluded_paths = tuple(item.path for item in source.excluded)
            introduced: tuple[str, ...] = ()
            static_noop = not files
        else:
            _pointer, state = loaded
            StateGuards(
                target_root,
                identity,
                expected_policy=selection.policy_fingerprint,
            ).require_clear(force=request.force)
            alternate_dirs = _existing_object_dirs(target_root)
            planner = StatePlanner(
                project.repository,
                include=project.include,
                exclude=project.exclude,
                protected=project.protected,
                remote_root=project.remote_root,
                git_store=None,
                alternate_object_dirs=alternate_dirs or None,
            )
            if alternate_dirs:
                from git_deploy.git_store import PersistentGitStore

                planner._object_env = PersistentGitStore(
                    target_root,
                    project.repository,
                ).object_environment()
            source = planner.plan_selectors(
                request.revisions,
                current_tree_id=state.source_tree_id,
                applied_transition_ids=state.applied_transition_ids,
                static_only=True,
            )
            before_tree = source.before_tree_id
            after_tree = source.after_tree_id
            files = source.files
            excluded_paths = tuple(item.path for item in source.excluded)
            introduced = source.introduced_transition_ids
            static_noop = source.static_noop

        changes = tuple(_planned_change(item) for item in files)
        artifacts = tuple(
            ArtifactMappingPlan(item.source, item.destination, item.kind)
            for item in project.artifacts
        )
        build, warnings = _build_summary(project)
        digest = _plan_digest(
            selection,
            actual_generation,
            before_tree,
            after_tree,
            changes,
            artifacts,
            build,
            warnings,
            introduced,
        )
        token = self._signer.issue(
            request,
            policy_fingerprint=selection.policy_fingerprint,
            plan_digest=digest,
        )
        return RevisionPlanResult(
            selection=selection,
            generation=actual_generation,
            before_tree_id=before_tree,
            after_tree_id=after_tree,
            source_changes=changes,
            excluded_paths=excluded_paths,
            introduced_transition_ids=introduced,
            artifacts=artifacts,
            build=build,
            warnings=warnings,
            static_noop=static_noop,
            remote_verified=False,
            plan_digest=digest,
            plan_token=token,
        )


def _existing_object_dirs(target_root: Path) -> list[str]:
    """Return existing persistent object directories without creating them."""

    objects = target_root / "git" / "objects"
    return [str(objects)] if objects.is_dir() else []


def _planned_change(item: PlannedFile) -> PlannedChange:
    """Convert a domain file plan to a renderer-neutral summary."""

    return PlannedChange(
        action=item.action,
        path=item.path,
        target_size=item.target_size,
        executable=item.executable,
    )


def _build_summary(
    project: ProjectConfig,
) -> tuple[BuildPlanSummary, tuple[str, ...]]:
    """Return build intent and safe warnings without invoking a runner."""

    build = project.build
    if build is None:
        return (
            BuildPlanSummary(
                enabled=False,
                runner=None,
                commands=(),
                build_origin=project.build_origin,
                artifacts_origin=project.artifacts_origin,
                uses_secret=False,
            ),
            (),
        )
    warnings: list[str] = []
    if build.runner == "host":
        from git_deploy.build_runner import HostBuildRunner

        warnings.append(HostBuildRunner.permission_warning)
    elif build.runner == "docker":
        from git_deploy.docker_runner import DockerBuildRunner

        warnings.append(DockerBuildRunner.daemon_warning)
    if build.onepassword is not None:
        warnings.append("secret-enabled builds bypass reusable build cache")
    return (
        BuildPlanSummary(
            enabled=True,
            runner=build.runner,
            commands=build.commands,
            build_origin=project.build_origin,
            artifacts_origin=project.artifacts_origin,
            uses_secret=build.onepassword is not None,
        ),
        tuple(warnings),
    )


def _plan_digest(
    selection: ProjectSelection,
    generation: int | None,
    before_tree: str,
    after_tree: str,
    changes: tuple[PlannedChange, ...],
    artifacts: tuple[ArtifactMappingPlan, ...],
    build: BuildPlanSummary,
    warnings: tuple[str, ...],
    introduced: tuple[str, ...],
) -> str:
    """Hash the exact renderer-neutral plan reviewed by an operator."""

    payload = {
        "target_id": selection.target_id,
        "physical_fingerprint": selection.physical_fingerprint,
        "policy_fingerprint": selection.policy_fingerprint,
        "generation": generation,
        "before_tree": before_tree,
        "after_tree": after_tree,
        "source_changes": [
            {
                "action": item.action,
                "path": item.path,
                "target_size": item.target_size,
                "executable": item.executable,
            }
            for item in changes
        ],
        "artifacts": [
            {
                "source": item.source,
                "destination": item.destination,
                "kind": item.kind,
                "status": item.status,
            }
            for item in artifacts
        ],
        "build": {
            "enabled": build.enabled,
            "runner": build.runner,
            "commands": [list(item) for item in build.commands],
            "build_origin": build.build_origin,
            "artifacts_origin": build.artifacts_origin,
            "uses_secret": build.uses_secret,
        },
        "warnings": list(warnings),
        "introduced_transition_ids": list(introduced),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
