"""Prepare and freeze one independent project before any remote connection."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from git_deploy.builder import run_build
from git_deploy.config import Config, TargetConfig, load_config, resolve_target_for_plan
from git_deploy.deployer import (
    TransportFactory,
    estimate_frozen_bytes,
    execute_frozen_plan,
    freeze_uploads,
)
from git_deploy.errors import PlanError, StateError
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.planner import DeploymentPlan, complete_remote_plan, create_plan
from git_deploy.transports import create_transport
from git_deploy.transports.base import Transport
from git_deploy.transports.openssh_sftp import SSHConnectionPool


@dataclass(slots=True)
class PreparedDeployment:
    """Own one frozen project plan, target lock, and cleanup scope."""

    name: str
    config: Config
    repository: GitRepository
    state_store: StateStore
    plan: DeploymentPlan
    frozen: dict[str, Path]
    frozen_bytes: int
    _temporary: tempfile.TemporaryDirectory[str]
    _lock: TargetLock
    transport: Transport | None = None
    _closed: bool = False

    def close(self) -> None:
        """Release frozen files and target lock idempotently."""

        if self._closed:
            return
        self._closed = True
        try:
            if self.transport is not None:
                self.transport.close()
                self.transport = None
            self._temporary.cleanup()
        finally:
            self._lock.release()


def prepare_project(
    name: str,
    config_path: Path,
    requested_target: str | None,
    *,
    full: bool,
    skip_build: bool,
    prepared_config: Config | None = None,
    prepared_target: TargetConfig | None = None,
    prepared_lock: TargetLock | None = None,
) -> PreparedDeployment:
    """Preflight, build, plan, and freeze one project with zero remote connection.

    Args:
        name: Human-readable repository label for warnings and summaries.
        config_path: Per-repository ``deploy.toml`` path.
        requested_target: Unified explicit/default target name.
        full: Force full current ownership upload and State rebuild.
        skip_build: Skip configured trusted build steps.
        prepared_config: Workspace-preloaded immutable project configuration.
        prepared_target: Workspace-pre-resolved physical target for this project.
        prepared_lock: Workspace lock already acquired before any project Build.

    Returns:
        A locked deployment whose upload bytes cannot change after confirmation.
    """

    config = prepared_config or load_config(config_path)
    target = config.target(requested_target)
    repository = GitRepository(config.project_root)
    repository.validate()
    state_store = StateStore(repository.common_dir())
    lock = prepared_lock or TargetLock(state_store.base, target.name)
    if prepared_lock is None:
        lock.acquire()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        legacy_store = StateStore(repository.git_dir())
        if state_store.migrate_from(legacy_store, target.name):
            print(f"[{name}] Migrated target state to Git common dir.")
        _check_hybrid_ignore(config, repository, name)
        before_build_status = repository.status_porcelain()
        if before_build_status and config.source.require_clean_worktree:
            raise PlanError(
                f"[{name}] worktree has uncommitted changes and "
                "source.require_clean_worktree is true"
            )
        if before_build_status:
            print(
                f"[{name}] WARNING: uncommitted changes are not included; "
                "committed HEAD content will be deployed."
            )
        try:
            state = state_store.load(target.name)
        except StateError:
            if not full:
                raise
            print(f"[{name}] WARNING: unreadable State will be rebuilt after full success.")
            state = None
        resolved_target = prepared_target or resolve_target_for_plan(
            target, runtime_dir=state_store.base
        )
        if (
            state is not None
            and state.target_fingerprint != resolved_target.fingerprint
            and not full
        ):
            raise PlanError(
                f"[{name}] target identity changed since the last success; "
                "review it and rerun with --full"
            )
        if not skip_build:
            run_build(config.build, config.project_root)
            after_build_status = repository.status_porcelain()
            if after_build_status and config.source.require_clean_worktree:
                raise PlanError(
                    f"[{name}] build left uncommitted changes and "
                    "source.require_clean_worktree is true"
                )
            if after_build_status != before_build_status:
                print(
                    f"[{name}] WARNING: build changed the worktree; these changes "
                    "are not included in source deployment."
                )
        plan = create_plan(
            config,
            target,
            repository,
            state,
            full=full,
            resolved_target=resolved_target,
        )
        frozen_bytes = estimate_frozen_bytes(plan, repository)
        available = shutil.disk_usage(tempfile.gettempdir()).free
        if frozen_bytes > available:
            raise PlanError(
                f"[{name}] insufficient temporary disk space to freeze uploads: "
                f"need {frozen_bytes} byte(s), available {available}"
            )
        temporary = tempfile.TemporaryDirectory(prefix=f"git-deploy-{name}-")
        frozen = freeze_uploads(plan, repository, Path(temporary.name))
        return PreparedDeployment(
            name,
            config,
            repository,
            state_store,
            plan,
            frozen,
            frozen_bytes,
            temporary,
            lock,
        )
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        lock.release()
        raise


def prepare_remote_plan(
    prepared: PreparedDeployment,
    *,
    allow_recovery: bool,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
) -> None:
    """Connect read-only and complete Hybrid ownership planning before writes.

    Args:
        prepared: Locally frozen project plan.
        allow_recovery: Whether normal deployment may reconcile prior recovery.
        transport_factory: Optional fake adapter factory.
        connection_pool: Optional command-scoped Native OpenSSH pool.

    Returns:
        ``None`` after storing a connected transport and remote-complete plan.
    """

    if prepared.transport is not None:
        raise PlanError("remote plan has already been prepared")
    transport = (
        transport_factory(prepared.plan.target)
        if transport_factory is not None
        else create_transport(prepared.plan.target, connection_pool)
    )
    try:
        transport.connect()
        if prepared.plan.hybrid is not None:
            prepared.plan = complete_remote_plan(
                prepared.plan,
                prepared.config,
                transport,
                allow_recovery=allow_recovery,
            )
        else:
            transport.root_exists()
        prepared.transport = transport
    except BaseException:
        transport.close()
        raise


def _check_hybrid_ignore(config: Config, repository: GitRepository, name: str) -> None:
    """Warn or fail when a Local Aggregation Root is not ignored by Git.

    Args:
        config: Loaded project and clean-worktree policy.
        repository: Validated Git reader.
        name: Human-readable repository label.

    Returns:
        ``None`` after every Hybrid local root is proven ignored or warned.
    """

    for output in config.outputs:
        if output.mode != "hybrid" or repository.is_ignored(output.local):
            continue
        relative = output.local.relative_to(config.project_root).as_posix()
        detail = (
            f"hybrid output directory {relative!r} is not ignored by Git; "
            "add '.deploy/' to .gitignore"
        )
        if config.source.require_clean_worktree:
            raise PlanError(f"[{name}] {detail}")
        print(f"[{name}] WARNING: {detail}")


def execute_prepared(
    prepared: PreparedDeployment,
    *,
    verbose: bool = False,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
) -> None:
    """Execute one prepared project and always release its local resources."""

    try:
        execute_frozen_plan(
            prepared.plan,
            prepared.config,
            prepared.state_store,
            prepared.frozen,
            verbose=verbose,
            transport_factory=transport_factory,
            connection_pool=connection_pool,
            prepared_transport=prepared.transport,
        )
        prepared.transport = None
    finally:
        prepared.close()
