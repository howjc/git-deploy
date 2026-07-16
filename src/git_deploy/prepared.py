"""Prepare and freeze one independent project before any remote connection."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from git_deploy.builder import run_build
from git_deploy.config import Config, load_config, resolve_target_for_plan
from git_deploy.deployer import TransportFactory, execute_frozen_plan, freeze_uploads
from git_deploy.errors import PlanError, StateError
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.planner import DeploymentPlan, create_plan
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
    _temporary: tempfile.TemporaryDirectory[str]
    _lock: TargetLock
    _closed: bool = False

    def close(self) -> None:
        """Release frozen files and target lock idempotently."""

        if self._closed:
            return
        self._closed = True
        try:
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
) -> PreparedDeployment:
    """Preflight, build, plan, and freeze one project with zero remote connection.

    Args:
        name: Human-readable repository label for warnings and summaries.
        config_path: Per-repository ``deploy.toml`` path.
        requested_target: Unified explicit/default target name.
        full: Force full current ownership upload and State rebuild.
        skip_build: Skip configured trusted build steps.

    Returns:
        A locked deployment whose upload bytes cannot change after confirmation.
    """

    config = load_config(config_path)
    target = config.target(requested_target)
    repository = GitRepository(config.project_root)
    repository.validate()
    state_store = StateStore(repository.common_dir())
    lock = TargetLock(state_store.base, target.name)
    lock.acquire()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        legacy_store = StateStore(repository.git_dir())
        if state_store.migrate_from(legacy_store, target.name):
            print(f"[{name}] Migrated target state to Git common dir.")
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
        resolved_target = resolve_target_for_plan(target, runtime_dir=state_store.base)
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
        temporary = tempfile.TemporaryDirectory(prefix=f"git-deploy-{name}-")
        frozen = freeze_uploads(plan, repository, Path(temporary.name))
        return PreparedDeployment(
            name,
            config,
            repository,
            state_store,
            plan,
            frozen,
            temporary,
            lock,
        )
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        lock.release()
        raise


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
        )
    finally:
        prepared.close()
