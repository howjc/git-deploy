"""Local-only revision planning service with signed plan credentials."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from git_deploy.errors import ConfigurationError, PolicyError
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.gitrepo import GitDeploymentPlanner, GitRepository
from git_deploy.models import PlannedFile, ProjectConfig
from git_deploy.state_composer import StateComposer, TransitionId
from git_deploy.state_guards import StateGuards
from git_deploy.state_planner import SourceDiffPlan, StatePlanner
from git_deploy.target_identity import default_state_base

from .config_service import ApplicationConfigService, ProjectSelection
from .errors import ApplicationError, ErrorCategory
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


class RevisionSelectionOrigin(StrEnum):
    """Stable origin of the effective revision selection."""

    EXPLICIT = "explicit"
    IMPLICIT_CURRENT_TO_HEAD = "implicit_current_to_head"


@dataclass(frozen=True, slots=True)
class FrozenDomainFile:
    """Exact domain PlannedFile fields bound into the reviewed plan digest."""

    action: str
    path: str
    remote_path: str
    source_path: str | None
    expected_before_sha256: str | None
    target_sha256: str | None
    target_size: int
    executable: bool
    expected_before_executable: bool | None


@dataclass(frozen=True, slots=True)
class RevisionPlanResult:
    """Structured local plan shared by CLI and future TUI adapters.

    Domain freeze fields (``before_state_id``, before applied transitions,
    exact ``domain_files``, after applied set) are part of the signed plan.
    Execution must reconstitute the same domain boundary and must not re-plan
    against a later current generation.
    """

    selection: ProjectSelection
    selection_origin: RevisionSelectionOrigin
    resolved_revisions: tuple[str, ...]
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
    before_state_id: str | None = None
    before_applied_transition_ids: tuple[str, ...] = ()
    after_applied_transition_ids: tuple[str, ...] = ()
    domain_files: tuple[FrozenDomainFile, ...] = ()


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

        before_state_id: str | None = None
        before_applied: tuple[str, ...] = ()
        after_applied: tuple[str, ...] = ()
        if loaded is None:
            if not request.revisions:
                revision_command = (
                    f"git-deploy state bootstrap {request.project} --revision COMMIT "
                    f"--remote {request.remote} --yes"
                )
                empty_command = (
                    f"git-deploy state bootstrap {request.project} --empty "
                    f"--remote {request.remote} --yes"
                )
                raise ApplicationError(
                    code="state.current-missing",
                    category=ErrorCategory.CONFIGURATION,
                    message=(
                        f"project {request.project!r} on remote {request.remote!r} has no "
                        "trusted current state. If the remote matches a known commit, run: "
                        f"{revision_command}. If every managed remote path is confirmed "
                        f"absent, run: {empty_command}. Explicit --revisions can only build "
                        "a legacy source plan and cannot bypass artifact state requirements."
                    ),
                    context={
                        "project": request.project,
                        "remote": request.remote,
                        "bootstrap_revision_command": revision_command,
                        "bootstrap_empty_command": empty_command,
                    },
                )
            source = GitDeploymentPlanner(project).build_revisions(request.revisions)
            selection_origin = RevisionSelectionOrigin.EXPLICIT
            resolved_revisions = source.revision_specs
            before_tree = source.from_commit
            after_tree = source.to_commit
            files = source.files
            excluded_paths = tuple(item.path for item in source.excluded)
            introduced: tuple[str, ...] = ()
            static_noop = not files
            domain_files = tuple(_freeze_domain_file(item) for item in files)
        else:
            _pointer, state = loaded
            before_state_id = state.state_id()
            before_applied = tuple(state.applied_transition_ids)
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
            selection_origin, effective_revisions = _effective_revisions(
                project,
                state.source_tree_id,
                state.applied_transition_ids,
                request.revisions,
            )
            source = planner.plan_selectors(
                effective_revisions,
                current_tree_id=state.source_tree_id,
                applied_transition_ids=state.applied_transition_ids,
                static_only=True,
            )
            resolved_revisions = source.revision_specs
            before_tree = source.before_tree_id
            after_tree = source.after_tree_id
            files = source.files
            excluded_paths = tuple(item.path for item in source.excluded)
            introduced = source.introduced_transition_ids
            static_noop = source.static_noop
            after_applied = tuple(source.applied_transition_ids)
            domain_files = tuple(_freeze_domain_file(item) for item in files)

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
            selection_origin,
            resolved_revisions,
            before_state_id=before_state_id,
            before_applied_transition_ids=before_applied,
            after_applied_transition_ids=after_applied,
            domain_files=domain_files,
        )
        token = self._signer.issue(
            request,
            policy_fingerprint=selection.policy_fingerprint,
            plan_digest=digest,
        )
        return RevisionPlanResult(
            selection=selection,
            selection_origin=selection_origin,
            resolved_revisions=resolved_revisions,
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
            before_state_id=before_state_id,
            before_applied_transition_ids=before_applied,
            after_applied_transition_ids=after_applied,
            domain_files=domain_files,
        )


def to_frozen_source_diff_plan(plan: RevisionPlanResult) -> SourceDiffPlan:
    """Rebuild the exact domain SourceDiffPlan bound by a reviewed application plan.

    Does not re-read live current or re-resolve revision selectors. Execution
    paths must use this (or an equivalent freeze) so TargetLock freshness
    compares against the operator-reviewed boundary.

    Args:
        plan: Signed application revision plan.

    Returns:
        Domain source plan with lock-held expected_* fields from the review.
    """

    files = tuple(
        PlannedFile(
            action=item.action,
            path=item.path,
            remote_path=item.remote_path,
            source_path=item.source_path,
            expected_before_sha256=item.expected_before_sha256,
            target_sha256=item.target_sha256,
            target_size=item.target_size,
            executable=item.executable,
            expected_before_executable=item.expected_before_executable,
        )
        for item in plan.domain_files
    )
    return SourceDiffPlan(
        before_tree_id=plan.before_tree_id,
        after_tree_id=plan.after_tree_id,
        files=files,
        excluded=(),
        introduced_transition_ids=plan.introduced_transition_ids,
        applied_transition_ids=plan.after_applied_transition_ids
        or plan.before_applied_transition_ids,
        remote_unverified=False,
        static_noop=plan.static_noop,
        revision_specs=plan.resolved_revisions,
        expected_before_state_id=plan.before_state_id,
        expected_generation=plan.generation,
        expected_before_tree_id=plan.before_tree_id,
        expected_before_applied_transition_ids=plan.before_applied_transition_ids
        if plan.before_state_id is not None or plan.generation is not None
        else None,
    )


def _effective_revisions(
    project: ProjectConfig,
    current_tree_id: str,
    applied_transition_ids: tuple[str, ...],
    requested: tuple[str, ...],
) -> tuple[RevisionSelectionOrigin, tuple[str, ...]]:
    """Resolve explicit selectors or derive missing first-parent transitions to HEAD.

    Args:
        project: Selected project containing the repository path.
        current_tree_id: Trusted source tree from current state.
        applied_transition_ids: Durable transitions already represented by current.
        requested: Explicit selectors, or empty for implicit current-to-HEAD.

    Returns:
        Selection origin and immutable full commit selectors to plan.
    """

    repository = GitRepository(project.repository)
    if requested:
        return (
            RevisionSelectionOrigin.EXPLICIT,
            repository.freeze_head_revision_specs(requested),
        )

    head = repository.resolve_commit("HEAD")
    commits = tuple(reversed(repository.first_parent_chain(head)))
    composer = StateComposer(repository)
    head_transitions = {
        composer.transition_id_for_commit(commit).as_str(): commit for commit in commits
    }
    applied = set(applied_transition_ids)
    for value in applied:
        TransitionId.parse(value)
    unknown = applied - set(head_transitions)
    if unknown:
        sample = sorted(unknown)[0]
        raise ConfigurationError(
            "trusted current transition is not reachable from HEAD; "
            f"cannot derive implicit plan ({sample[:24]})"
        )

    if current_tree_id == repository.empty_tree():
        missing = commits
    else:
        matching = tuple(
            commit
            for commit in commits
            if repository._run_text("rev-parse", f"{commit}^{{tree}}").strip()
            == current_tree_id
        )
        if len(matching) == 1:
            baseline = commits.index(matching[0])
            candidates = commits[baseline + 1 :]
            missing = tuple(
                commit
                for commit in candidates
                if composer.transition_id_for_commit(commit).as_str() not in applied
            )
        elif applied:
            # A composed/cherry-picked state may not equal one historical tree;
            # durable transition IDs then remain the authoritative replay filter.
            missing = tuple(
                commit
                for transition, commit in head_transitions.items()
                if transition not in applied
            )
        else:
            raise ConfigurationError(
                "trusted current tree cannot be mapped uniquely onto HEAD history; "
                "run state verify and restore required Git objects"
            )
    return RevisionSelectionOrigin.IMPLICIT_CURRENT_TO_HEAD, missing


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


def _freeze_domain_file(item: PlannedFile) -> FrozenDomainFile:
    """Capture exact domain file mutation fields for digest and execution."""

    return FrozenDomainFile(
        action=item.action,
        path=item.path,
        remote_path=item.remote_path,
        source_path=item.source_path,
        expected_before_sha256=item.expected_before_sha256,
        target_sha256=item.target_sha256,
        target_size=item.target_size,
        executable=item.executable,
        expected_before_executable=item.expected_before_executable,
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
    selection_origin: RevisionSelectionOrigin,
    resolved_revisions: tuple[str, ...],
    *,
    before_state_id: str | None = None,
    before_applied_transition_ids: tuple[str, ...] = (),
    after_applied_transition_ids: tuple[str, ...] = (),
    domain_files: tuple[FrozenDomainFile, ...] = (),
) -> str:
    """Hash the exact renderer-neutral plan reviewed by an operator."""

    payload = {
        "target_id": selection.target_id,
        "physical_fingerprint": selection.physical_fingerprint,
        "policy_fingerprint": selection.policy_fingerprint,
        "generation": generation,
        "before_state_id": before_state_id,
        "before_applied_transition_ids": list(before_applied_transition_ids),
        "after_applied_transition_ids": list(after_applied_transition_ids),
        "selection_origin": selection_origin.value,
        "resolved_revisions": list(resolved_revisions),
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
        "domain_files": [
            {
                "action": item.action,
                "path": item.path,
                "remote_path": item.remote_path,
                "source_path": item.source_path,
                "expected_before_sha256": item.expected_before_sha256,
                "target_sha256": item.target_sha256,
                "target_size": item.target_size,
                "executable": item.executable,
                "expected_before_executable": item.expected_before_executable,
            }
            for item in domain_files
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
