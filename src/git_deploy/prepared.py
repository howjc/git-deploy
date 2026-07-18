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
    execute_recovery_plan,
    execute_frozen_plan,
    freeze_uploads,
)
from git_deploy.errors import PlanError, StateError
from git_deploy.ftp_hybrid import (
    FTPPendingPhase,
    load_capability_profile,
    read_pending,
    validate_remote_root_aliases,
)
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.planner import (
    DeploymentPlan,
    ExplicitRecoveryPlan,
    complete_remote_plan,
    create_recovery_plan,
    create_plan,
    validate_recovery_freshness,
    validate_remote_freshness,
)
from git_deploy.transports import create_transport
from git_deploy.transports.base import Transport
from git_deploy.transports.ftp import FTPTransport
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


@dataclass(slots=True)
class PreparedRecovery:
    """Own a Recovery-only plan, connected transport, State path, and lock."""

    name: str
    config: Config
    state_store: StateStore
    plan: ExplicitRecoveryPlan
    transport: Transport
    _lock: TargetLock
    _closed: bool = False

    def close(self) -> None:
        """Close the remote connection and release the local target lock."""

        if self._closed:
            return
        self._closed = True
        try:
            self.transport.close()
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
    check_post_commit_pending: bool = False,
) -> PreparedDeployment:
    """Preflight, build, plan, and freeze one project under its target lock.

    Args:
        name: Human-readable repository label for warnings and summaries.
        config_path: Per-repository ``deploy.toml`` path.
        requested_target: Unified explicit/default target name.
        full: Force full current ownership upload and State rebuild.
        skip_build: Skip configured trusted build steps.
        prepared_config: Workspace-preloaded immutable project configuration.
        prepared_target: Workspace-pre-resolved physical target for this project.
        prepared_lock: Workspace lock already acquired before any project Build.
        check_post_commit_pending: Read FTP metadata before Build and require
            explicit recovery when remote ownership is already committed.

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
        resolved_target = prepared_target or resolve_target_for_plan(
            target, runtime_dir=state_store.base
        )
        if check_post_commit_pending:
            _reject_post_commit_ftp_pending(config, resolved_target)
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


def _reject_post_commit_ftp_pending(config: Config, target: TargetConfig) -> None:
    """Stop a normal FTP deployment before Build when frozen recovery is authoritative.

    Args:
        config: Loaded project configuration containing the Hybrid mapping.
        target: Resolved physical target used to read protected metadata.

    Returns:
        ``None`` when no post-commit FTP Pending marker exists.
    """

    hybrid_outputs = tuple(item for item in config.outputs if item.mode == "hybrid")
    if target.protocol != "ftp" or not hybrid_outputs:
        return
    if len(hybrid_outputs) != 1 or config.project_id is None:
        raise PlanError(
            "FTP Hybrid pending preflight requires one mapping and project_id"
        )
    output = hybrid_outputs[0]
    if output.name is None or target.runtime_dir is None:
        raise PlanError(
            "FTP Hybrid pending preflight lacks mapping or runtime identity"
        )
    transport = create_transport(target)
    try:
        transport.connect()
        if not isinstance(transport, FTPTransport):
            raise PlanError(
                "FTP Hybrid pending preflight requires FTPTransport semantics"
            )
        transport.enable_utf8()
        validate_remote_root_aliases(transport, (("internal", ".git-deploy"),))
        load_capability_profile(
            target.runtime_dir,
            target,
            server_banner_hash=transport.server_banner_hash(),
        )
        pending = read_pending(
            transport,
            project_id=config.project_id,
            mapping=output.name,
            remote=output.remote.as_posix(),
            target=target,
        )
        if pending is not None and pending.phase in {
            FTPPendingPhase.OWNERSHIP_COMMITTED,
            FTPPendingPhase.STATE_COMPLETE,
        }:
            raise PlanError(
                "FTP Hybrid remote Ownership is already committed for a frozen Pending "
                "deployment; run this target with --recover before starting a new deployment"
            )
    finally:
        transport.close()


def prepare_remote_plan(
    prepared: PreparedDeployment,
    *,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
) -> None:
    """Connect read-only and complete Hybrid ownership planning before writes.

    Args:
        prepared: Locally frozen project plan.
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
            )
        else:
            transport.root_exists()
        prepared.transport = transport
    except BaseException:
        transport.close()
        raise


def prepare_recovery(
    name: str,
    config: Config,
    requested_target: str | None,
    *,
    prepared_target: TargetConfig | None = None,
    prepared_lock: TargetLock | None = None,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
) -> PreparedRecovery | None:
    """Prepare only remote Recovery facts without Build, State read, or scans.

    Args:
        name: Human-readable project label.
        config: Loaded project configuration.
        requested_target: Explicit/default target name.
        prepared_target: Workspace-resolved target, when available.
        prepared_lock: Workspace-acquired lock transferred on success.
        transport_factory: Optional fake/custom transport factory.
        connection_pool: Optional Native OpenSSH connection pool.

    Returns:
        Locked connected Recovery, or ``None`` when no record exists.
    """

    target = config.target(requested_target)
    repository = GitRepository(config.project_root)
    state_store = StateStore(repository.common_dir())
    lock = prepared_lock or TargetLock(state_store.base, target.name)
    if prepared_lock is None:
        lock.acquire()
    transport: Transport | None = None
    try:
        resolved_target = prepared_target or resolve_target_for_plan(
            target,
            runtime_dir=state_store.base,
        )
        transport = (
            transport_factory(resolved_target)
            if transport_factory is not None
            else create_transport(resolved_target, connection_pool)
        )
        transport.connect()
        plan = create_recovery_plan(config, resolved_target, transport)
        if plan is None:
            transport.close()
            lock.release()
            return None
        return PreparedRecovery(name, config, state_store, plan, transport, lock)
    except BaseException:
        if transport is not None:
            transport.close()
        lock.release()
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


def execute_prepared_recovery(
    prepared: PreparedRecovery,
    *,
    verbose: bool = False,
) -> None:
    """Execute one reviewed Recovery and always release connection and lock.

    Args:
        prepared: Recovery-only plan retaining its connected transport.
        verbose: Whether to print remote command context.

    Returns:
        ``None`` after the pending recovery phases finish or remain recorded.
    """

    try:
        execute_recovery_plan(
            prepared.plan,
            prepared.state_store,
            prepared.transport,
            verbose=verbose,
        )
    finally:
        prepared.close()


def validate_prepared_freshness(prepared: PreparedDeployment) -> None:
    """Revalidate one connected Hybrid plan without mutating remote state.

    Args:
        prepared: Remote-complete prepared deployment retaining its transport.

    Returns:
        ``None`` when the reviewed remote snapshot remains current.
    """

    if prepared.plan.hybrid is None:
        return
    if prepared.transport is None:
        raise PlanError("hybrid freshness validation requires a prepared transport")
    validate_remote_freshness(prepared.plan, prepared.config, prepared.transport)


def validate_prepared_recovery(prepared: PreparedRecovery) -> None:
    """Revalidate one connected Recovery without mutating remote state.

    Args:
        prepared: Recovery-only plan retaining its transport and target lock.

    Returns:
        ``None`` when record, Ownership, and phase facts remain unchanged.
    """

    validate_recovery_freshness(prepared.plan, prepared.transport)
