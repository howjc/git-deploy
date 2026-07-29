"""Execute a frozen plan with per-file retries and delayed state commit."""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath

from git_deploy.config import Config, TargetConfig
from git_deploy.errors import DeployError, PlanError, StaleRemotePlanError
from git_deploy.ftp_hybrid import (
    FTP_PENDING_SCHEMA,
    FTPHybridPending,
    FTPPendingPhase,
    local_manifest_hash,
    pending_path,
    publish_verified_bytes,
    serialize_pending,
)
from git_deploy.git import GitRepository
from git_deploy.hybrid import (
    MAX_REMOTE_RECORD_BYTES,
    RECOVERY_SCHEMA,
    HybridBackend,
    HybridRecoveryRecord,
    RecoveryPhase,
    cleanup_committed_recovery,
    discard_staged_recovery,
    make_ownership,
    ownership_hash,
    ownership_path,
    reconcile_recovery,
    recovery_command_hash,
    serialize_ownership,
    write_recovery,
)
from git_deploy.manifest import ManifestEntry, StateStore, hash_file, new_state
from git_deploy.planner import (
    DeploymentPlan,
    ExplicitRecoveryPlan,
    FTPHybridFileUpload,
    FTPRecoveryPlan,
    HybridDirectoryDelete,
    HybridDirectoryMirror,
    HybridOwnershipUpdate,
    HybridRootFileDelete,
    HybridRootFileUpload,
    Operation,
    UploadOperation,
    complete_remote_plan,
    validate_recovery_freshness,
    validate_remote_freshness,
)
from git_deploy.progress import ProgressReporter
from git_deploy.transports import create_transport
from git_deploy.transports.base import RemotePathType, Transport
from git_deploy.transports.ftp import FTPTransport
from git_deploy.transports.openssh_sftp import SSHConnectionPool

TransportFactory = Callable[[TargetConfig], Transport]


def execute_plan(
    plan: DeploymentPlan,
    config: Config,
    repository: GitRepository,
    state_store: StateStore,
    *,
    verbose: bool = False,
    transport_factory: TransportFactory = create_transport,
) -> None:
    """Freeze upload bytes, mutate the remote, then atomically commit state.

    Args:
        plan: Conflict-free deployment operations.
        config: Retry and project settings.
        repository: Source of exact committed HEAD blobs.
        state_store: Per-target lightweight state store.
        verbose: Enable more frequent progress output.
        transport_factory: Injectable protocol adapter factory for tests.

    Returns:
        ``None`` only after remote operations and state commit succeed.
    """

    if not plan.operations and plan.hybrid is None:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return
    with tempfile.TemporaryDirectory(prefix="git-deploy-") as directory:
        frozen = freeze_uploads(plan, repository, Path(directory))
        execute_frozen_plan(
            plan,
            config,
            state_store,
            frozen,
            verbose=verbose,
            transport_factory=transport_factory,
        )


def execute_frozen_plan(
    plan: DeploymentPlan,
    config: Config,
    state_store: StateStore,
    frozen: dict[str, Path],
    *,
    verbose: bool = False,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
    prepared_transport: Transport | None = None,
    progress_label: str | None = None,
) -> None:
    """Execute already frozen bytes and commit only this project's successful state.

    Args:
        plan: Frozen deployment plan.
        config: Project retry settings.
        state_store: Independent project target state.
        frozen: Upload paths captured during the prepare phase.
        verbose: Enable more frequent progress output.
        transport_factory: Optional fake/custom transport factory.
        connection_pool: Command-scoped Native OpenSSH connection pool.
        prepared_transport: Optional already connected transport from remote planning.
        progress_label: Optional repository name prefixed to the transfer summary.

    Returns:
        ``None`` after this project's remote operations and state commit succeed.
    """

    if not plan.operations and plan.hybrid is None:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return
    transport = prepared_transport or (
        transport_factory(plan.target)
        if transport_factory is not None
        else create_transport(plan.target, connection_pool)
    )
    progress = ProgressReporter(
        verbose,
        label=progress_label,
        measurement_mode=transport.measurement_mode,
    )
    try:
        if prepared_transport is None:
            _connect_with_retry(
                transport,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
                ensure_root=plan.hybrid is None,
            )
        elif plan.hybrid is None:
            # Remote planning is read-only; root creation remains a confirmed
            # execution mutation and therefore happens only at this point.
            transport.ensure_root()
        if plan.hybrid is not None and not plan.hybrid.remote_complete:
            plan = complete_remote_plan(plan, config, transport)
        if plan.hybrid is not None:
            _execute_hybrid_plan(
                plan,
                config,
                state_store,
                frozen,
                transport,
                progress,
                verbose=verbose,
            )
            return
        if not plan.operations:
            state_store.save(
                new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
            )
            print("No file changes; deployment state advanced to HEAD.")
            return
        for operation in plan.operations:
            _execute_with_retry(
                operation,
                frozen,
                transport,
                progress,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
        _execute_after_deploy(plan, transport, verbose=verbose)
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError(f"deployment failed: {exc}") from exc
    finally:
        transport.close()
    state_store.save(
        new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
    )
    progress.render_summary()


def _execute_hybrid_plan(
    plan: DeploymentPlan,
    config: Config,
    state_store: StateStore,
    frozen: dict[str, Path],
    transport: Transport,
    progress: ProgressReporter,
    *,
    verbose: bool,
) -> None:
    """Execute one remote-complete Hybrid Plan with durable recovery phases.

    Args:
        plan: Frozen plan containing validated remote ownership facts.
        config: Project identity and retry policy.
        state_store: Local State committed only after commands succeed.
        frozen: Verified Source, Incremental, and Hybrid bytes.
        transport: Connected SFTP adapter reused for preflight and mutation.
        progress: Shared upload progress reporter.
        verbose: Whether to print command execution context.

    Returns:
        ``None`` after State commit and best-effort recovery cleanup.
    """

    hybrid = plan.hybrid
    if hybrid is None or not hybrid.remote_complete or config.project_id is None:
        raise PlanError("hybrid deployment requires a complete remote ownership plan")
    if hybrid.backend is HybridBackend.FTP_IN_PLACE:
        if not isinstance(transport, FTPTransport):
            raise PlanError("FTP Hybrid execution requires FTPTransport semantics")
        _execute_ftp_hybrid_plan(
            plan,
            config,
            state_store,
            frozen,
            transport,
            progress,
        )
        return
    validate_remote_freshness(plan, config, transport)
    if hybrid.recovery_records:
        raise PlanError(
            "pending Hybrid Recovery requires an explicit reviewed --recover run"
        )
    transport.ensure_root()
    if not plan.has_remote_work:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return
    for operation in plan.operations:
        _execute_with_retry(
            operation,
            frozen,
            transport,
            progress,
            attempts=config.deploy.retries,
            delay=config.deploy.retry_delay,
        )
    previous_updated_at = hybrid.ownership.updated_at if hybrid.ownership is not None else -1
    next_ownership = make_ownership(
        hybrid.local,
        config.project_id,
        plan.head,
        now=max(int(time.time()), previous_updated_at + 1),
    )
    deployment_id = uuid.uuid4().hex
    stage_root = f".git-deploy/stage/{deployment_id}"
    backup_root = f".git-deploy/backup/{deployment_id}"
    mutation_names = tuple(
        sorted(
            {
                (
                    operation.path
                    if isinstance(operation, (HybridRootFileUpload, HybridRootFileDelete))
                    else operation.name
                )
                for operation in hybrid.operations
                if isinstance(
                    operation,
                    (
                        HybridRootFileUpload,
                        HybridRootFileDelete,
                        HybridDirectoryMirror,
                        HybridDirectoryDelete,
                    ),
                )
            }
        )
    )
    expected_types = dict(hybrid.expected_path_types)
    old_existing_names = tuple(
        name
        for name in mutation_names
        if expected_types[name] is not RemotePathType.MISSING
    )
    record = HybridRecoveryRecord(
        RECOVERY_SCHEMA,
        deployment_id,
        hybrid.local.mapping,
        plan.target_fingerprint,
        stage_root,
        backup_root,
        RecoveryPhase.PREPARED,
        hybrid.expected_ownership_hash or ownership_hash(hybrid.ownership),
        ownership_hash(next_ownership),
        mutation_names,
        old_existing_names,
        command_hash=recovery_command_hash(
            plan.target.after_deploy,
            plan.target.command_timeout,
        ),
    )
    for internal in (
        ".git-deploy",
        ".git-deploy/hybrid",
        ".git-deploy/stage",
        ".git-deploy/backup",
        ".git-deploy/recovery",
    ):
        transport.make_directory(internal, mode=0o700)
    write_recovery(transport, record)
    for deployment_root in (stage_root, backup_root):
        transport.make_directory(deployment_root, mode=0o700)
    directories = {item.name: item for item in hybrid.local.directories}
    for operation in hybrid.operations:
        if isinstance(operation, HybridRootFileUpload):
            _upload_with_retry(
                frozen[operation.path],
                f"{stage_root}/{operation.path}",
                transport,
                progress,
                display_path=operation.path,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
        elif isinstance(operation, HybridDirectoryMirror):
            directory = directories[operation.name]
            staged_directory = f"{stage_root}/{operation.name}"
            transport.make_directory(staged_directory)
            for relative in sorted(
                directory.directories,
                key=lambda path: (len(PurePosixPath(path).parts), path),
            ):
                transport.make_directory(f"{staged_directory}/{relative}")
            for relative in directory.files:
                final_path = f"{operation.name}/{relative}"
                staged_path = f"{staged_directory}/{relative}"
                _upload_with_retry(
                    frozen[final_path],
                    staged_path,
                    transport,
                    progress,
                    display_path=final_path,
                    attempts=config.deploy.retries,
                    delay=config.deploy.retry_delay,
                )
    record = record.with_phase(RecoveryPhase.STAGED)
    write_recovery(transport, record)
    try:
        validate_remote_freshness(
            plan,
            config,
            transport,
            expected_recovery_records=(record,),
        )
    except StaleRemotePlanError:
        try:
            discard_staged_recovery(transport, record)
        except Exception as cleanup_error:
            print(
                "WARNING: stale Hybrid Stage cleanup is pending; review with "
                f"--remote-plan and run --recover: {cleanup_error}",
                file=sys.stderr,
                flush=True,
            )
        raise
    record = record.with_phase(RecoveryPhase.SWAPPING)
    write_recovery(transport, record)
    for operation in hybrid.operations:
        if isinstance(operation, HybridRootFileUpload):
            record = record.starting(operation.path)
            write_recovery(transport, record)
            try:
                _backup_current(
                    transport,
                    operation.path,
                    backup_root,
                    expected_types[operation.path],
                )
            except StaleRemotePlanError:
                record = record.without_active()
                write_recovery(transport, record)
                raise
            _publish_staged(
                transport,
                f"{stage_root}/{operation.path}",
                operation.path,
                expected_types[operation.path],
            )
        elif isinstance(operation, HybridDirectoryMirror):
            record = record.starting(operation.name)
            write_recovery(transport, record)
            try:
                _backup_current(
                    transport,
                    operation.name,
                    backup_root,
                    expected_types[operation.name],
                )
            except StaleRemotePlanError:
                record = record.without_active()
                write_recovery(transport, record)
                raise
            _publish_staged(
                transport,
                f"{stage_root}/{operation.name}",
                operation.name,
                expected_types[operation.name],
            )
        elif isinstance(operation, (HybridRootFileDelete, HybridDirectoryDelete)):
            name = operation.path if isinstance(operation, HybridRootFileDelete) else operation.name
            if expected_types[name] is RemotePathType.MISSING:
                _backup_current(transport, name, backup_root, expected_types[name])
                continue
            record = record.starting(name)
            write_recovery(transport, record)
            try:
                _backup_current(transport, name, backup_root, expected_types[name])
            except StaleRemotePlanError:
                record = record.without_active()
                write_recovery(transport, record)
                raise
    if any(isinstance(item, HybridOwnershipUpdate) for item in hybrid.operations):
        validate_remote_freshness(
            plan,
            config,
            transport,
            expected_recovery_records=(record,),
            check_path_types=False,
        )
        transport.write_file_atomic(
            ownership_path(hybrid.local.mapping),
            serialize_ownership(next_ownership),
        )
    record = record.with_phase(RecoveryPhase.OWNERSHIP_COMMITTED)
    write_recovery(transport, record)
    _execute_after_deploy(plan, transport, verbose=verbose)
    record = record.with_phase(RecoveryPhase.COMMANDS_COMPLETE)
    write_recovery(transport, record)
    state_store.save(
        new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
    )
    record = record.with_phase(RecoveryPhase.STATE_COMPLETE)
    write_recovery(transport, record)
    try:
        cleanup_committed_recovery(transport, record)
    except Exception as exc:
        print(
            "WARNING: hybrid cleanup is pending; review with --remote-plan and "
            f"run --recover: {exc}",
            file=sys.stderr,
            flush=True,
        )
    progress.render_summary()


def _execute_ftp_hybrid_plan(
    plan: DeploymentPlan,
    config: Config,
    state_store: StateStore,
    frozen: dict[str, Path],
    transport: FTPTransport,
    progress: ProgressReporter,
) -> None:
    """Execute one FTP in-place plan with monotonic remote Pending phases.

    Args:
        plan: Remote-complete FTP Hybrid plan approved by the user.
        config: Retry policy and stable project identity.
        state_store: Local State committed only after Remote Ownership.
        frozen: Immutable bytes captured before any remote connection.
        transport: Connected FTP adapter with a valid Capability Profile.
        progress: Shared upload progress reporter.

    Returns:
        ``None`` after State commit and best-effort internal cleanup.
    """

    hybrid = plan.hybrid
    if hybrid is None or hybrid.ftp is None or config.project_id is None:
        raise PlanError("FTP Hybrid execution requires a remote-complete FTP plan")
    ftp_plan = hybrid.ftp
    validate_remote_freshness(plan, config, transport)
    transport.ensure_root()
    if not plan.has_remote_work:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return

    pending = ftp_plan.pending
    if pending is None:
        next_state = new_state(
            plan.target.name,
            plan.target_fingerprint,
            plan.head,
            plan.output_manifest,
        )
        deployment_id = uuid.uuid4().hex
        pending = FTPHybridPending(
            FTP_PENDING_SCHEMA,
            config.project_id,
            hybrid.local.mapping,
            hybrid.local.remote,
            plan.target_fingerprint,
            deployment_id,
            FTPPendingPhase.PREPARED,
            hybrid.expected_ownership_hash or ownership_hash(hybrid.ownership),
            ownership_hash(ftp_plan.next_ownership),
            local_manifest_hash(hybrid.local, plan.output_manifest),
            plan.head,
            next_state,
            ftp_plan.next_ownership.updated_at,
            plan.non_hybrid_plan_hash,
            plan.previous_state_hash,
        )
        try:
            _ensure_ftp_internal_directories(transport, deployment_id)
            _write_ftp_pending(transport, pending)
        except BaseException as exc:
            stage_root = f".git-deploy/ftp-hybrid/stage/{deployment_id}"
            try:
                transport.remove_tree(stage_root)
            except BaseException as cleanup_error:
                exc.add_note(
                    "FTP Hybrid could not clean the Stage created before the initial "
                    f"Pending write for deployment {deployment_id}: {cleanup_error}"
                )
            try:
                transport.remove_directory(".git-deploy/ftp-hybrid/stage")
            except BaseException:
                pass
            raise
    else:
        _ensure_ftp_internal_directories(transport, pending.deployment_id)

    stage_root = f".git-deploy/ftp-hybrid/stage/{pending.deployment_id}"
    phase = pending.phase
    if phase is FTPPendingPhase.PREPARED:
        for operation in plan.operations:
            _execute_with_retry(
                operation,
                frozen,
                transport,
                progress,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
        for upload in ftp_plan.uploads:
            _stage_ftp_hybrid_file(
                upload,
                frozen[upload.path],
                stage_root,
                transport,
                progress,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
        reviewed = replace(
            plan,
            hybrid=replace(hybrid, ftp=replace(ftp_plan, pending=pending)),
        )
        validate_remote_freshness(reviewed, config, transport)
        for directory in ftp_plan.create_directories:
            transport.make_directory(directory)
        for upload in ftp_plan.uploads:
            _publish_ftp_hybrid_file(
                upload,
                frozen[upload.path],
                stage_root,
                transport,
                progress,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
        pending = pending.with_phase(FTPPendingPhase.FILES_PUBLISHED)
        _write_ftp_pending(transport, pending)
        phase = pending.phase

    if phase is FTPPendingPhase.FILES_PUBLISHED:
        for path in ftp_plan.delete_files:
            _retry_ftp_mutation(
                path,
                lambda path=path: transport.delete_typed(path),
                transport,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
            print(f"DELETE {path}")
        for path in ftp_plan.remove_directories:
            _retry_ftp_mutation(
                path,
                lambda path=path: transport.remove_directory(path),
                transport,
                attempts=config.deploy.retries,
                delay=config.deploy.retry_delay,
            )
            print(f"RMD {path}/")
        pending = pending.with_phase(FTPPendingPhase.PRUNED)
        _write_ftp_pending(transport, pending)
        phase = pending.phase

    if phase is FTPPendingPhase.PRUNED:
        ownership_data = serialize_ownership(ftp_plan.next_ownership)
        current_hash = _ftp_remote_record_hash(
            transport,
            ownership_path(hybrid.local.mapping),
            max_bytes=MAX_REMOTE_RECORD_BYTES,
        )
        expected_hash = ownership_hash(ftp_plan.next_ownership)
        if current_hash != expected_hash:
            publish_verified_bytes(
                transport,
                stage_path=f"{stage_root}/.ownership.json",
                final_path=ownership_path(hybrid.local.mapping),
                data=ownership_data,
            )
        pending = pending.with_phase(FTPPendingPhase.OWNERSHIP_COMMITTED)
        _write_ftp_pending(transport, pending)
        phase = pending.phase

    if phase is FTPPendingPhase.OWNERSHIP_COMMITTED:
        # The marker's frozen State is authoritative after Ownership commit;
        # current HEAD/build may only be used after this resume has completed.
        state_store.save(pending.next_state)
        pending = pending.with_phase(FTPPendingPhase.STATE_COMPLETE)
        _write_ftp_pending(transport, pending)
        phase = pending.phase

    if phase is FTPPendingPhase.STATE_COMPLETE:
        try:
            _cleanup_ftp_hybrid_pending(transport, pending)
        except Exception as exc:
            print(
                "WARNING: FTP Hybrid cleanup is pending; run Doctor and rerun the "
                f"deployment: {exc}",
                file=sys.stderr,
                flush=True,
            )
    progress.render_summary()


def _ensure_ftp_internal_directories(
    transport: FTPTransport,
    deployment_id: str,
) -> None:
    """Create only protected FTP Hybrid metadata and Stage directories."""

    for path in (
        ".git-deploy",
        ".git-deploy/hybrid",
        ".git-deploy/ftp-hybrid",
        ".git-deploy/ftp-hybrid/stage",
        f".git-deploy/ftp-hybrid/stage/{deployment_id}",
        ".git-deploy/ftp-hybrid/pending",
    ):
        transport.make_directory(path, mode=0o700)


def _write_ftp_pending(transport: FTPTransport, pending: FTPHybridPending) -> None:
    """Publish and re-read one Pending phase through its deployment Stage."""

    publish_verified_bytes(
        transport,
        stage_path=(
            f".git-deploy/ftp-hybrid/stage/{pending.deployment_id}/.pending.json"
        ),
        final_path=pending_path(pending.mapping),
        data=serialize_pending(pending),
    )


def _cleanup_ftp_hybrid_pending(
    transport: FTPTransport,
    pending: FTPHybridPending,
) -> None:
    """Remove only the current Stage and marker, then best-effort the shared parent.

    Args:
        transport: Connected FTP adapter.
        pending: Frozen marker identifying the deployment-owned Stage and mapping.

    Returns:
        ``None`` after current protected state is gone. Sibling Stages are preserved.
    """

    stage_root = f".git-deploy/ftp-hybrid/stage/{pending.deployment_id}"
    transport.remove_tree(stage_root)
    transport.delete_typed(pending_path(pending.mapping))
    try:
        transport.remove_directory(".git-deploy/ftp-hybrid/stage")
    except Exception:
        # A sibling Stage belongs to another or interrupted deployment. Its
        # presence must not resurrect the marker that just completed.
        pass


def _stage_ftp_hybrid_file(
    operation: FTPHybridFileUpload,
    local_path: Path,
    stage_root: str,
    transport: FTPTransport,
    progress: ProgressReporter,
    *,
    attempts: int,
    delay: float,
) -> None:
    """Upload and SHA256-verify one frozen file in the protected FTP Stage."""

    staged_path = f"{stage_root}/files/{operation.path}"

    def action() -> None:
        """Perform one complete idempotent STOR and RETR verification attempt."""

        transport.upload(
            local_path,
            staged_path,
            progress.callback(operation.path, operation.size),
        )
        actual = transport.read_file(staged_path, max_bytes=operation.size)
        if len(actual) != operation.size or hashlib.sha256(actual).hexdigest() != operation.sha256:
            raise DeployError(f"FTP staged verification mismatch for {operation.path}")

    _retry_ftp_mutation(
        operation.path,
        action,
        transport,
        on_retry=lambda: progress.record_retry(operation.path),
        attempts=attempts,
        delay=delay,
    )


def _publish_ftp_hybrid_file(
    operation: FTPHybridFileUpload,
    local_path: Path,
    stage_root: str,
    transport: FTPTransport,
    progress: ProgressReporter,
    *,
    attempts: int,
    delay: float,
) -> None:
    """Rename-replace one stage-verified file; restage with RETR if Stage is gone.

    Content identity is proven only on Stage (or restage) via full RETR SHA256.
    Publish trusts the probed Rename Replace contract plus Stage consumption and
    a final-path type check, avoiding a second full-file RETR per payload file.
    Pending/Ownership metadata still uses ``publish_verified_bytes`` (stage + final).
    """

    staged_path = f"{stage_root}/files/{operation.path}"
    upload_attempted = False

    def action() -> None:
        """Publish one stage-verified file and prove Stage consumption."""

        nonlocal upload_attempted
        upload_attempted = False
        if transport.lstat(staged_path) is RemotePathType.MISSING:
            # Retry/resume path: Stage bytes may be gone; restage and re-verify.
            upload_attempted = True
            transport.upload(
                local_path,
                staged_path,
                progress.callback(operation.path, operation.size),
            )
            staged = transport.read_file(staged_path, max_bytes=operation.size)
            if (
                len(staged) != operation.size
                or hashlib.sha256(staged).hexdigest() != operation.sha256
            ):
                raise DeployError(f"FTP restaged verification mismatch for {operation.path}")
        transport.rename_replace(staged_path, operation.path)
        if transport.lstat(staged_path) is not RemotePathType.MISSING:
            raise DeployError(f"FTP Stage was not consumed for {operation.path}")
        if transport.lstat(operation.path) is not RemotePathType.FILE:
            raise DeployError(
                f"FTP publish did not leave a regular file at {operation.path}"
            )

    def record_upload_retry() -> None:
        """Close transfer timing only when this publish attempt restaged bytes."""

        if upload_attempted:
            progress.record_retry(operation.path)

    _retry_ftp_mutation(
        operation.path,
        action,
        transport,
        on_retry=record_upload_retry,
        attempts=attempts,
        delay=delay,
    )


def _retry_ftp_mutation(
    label: str,
    action: Callable[[], None],
    transport: FTPTransport,
    *,
    on_retry: Callable[[], None] | None = None,
    attempts: int,
    delay: float,
) -> None:
    """Retry one idempotent FTP Hybrid action with a clean listing cache.

    Args:
        label: User-facing operation path.
        action: Idempotent mutation attempt.
        transport: Connected FTP transport whose caches are invalidated on retry.
        on_retry: Optional upload-accounting hook run before the retry delay.
        attempts: Maximum mutation attempts.
        delay: Retry delay in seconds.

    Returns:
        ``None`` after the action succeeds.
    """

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                transport.invalidate_connection()
                _connect_with_retry(transport, attempts=1, delay=0, ensure_root=False)
            action()
            return
        except Exception as exc:
            if attempt >= attempts:
                raise DeployError(f"{label} failed after {attempts} attempt(s): {exc}") from exc
            print(
                f"Retry {attempt}/{attempts - 1} for {label} after error: {exc}",
                flush=True,
            )
            if on_retry is not None:
                on_retry()
            if delay:
                time.sleep(delay)


def _ftp_remote_record_hash(
    transport: FTPTransport,
    path: str,
    *,
    max_bytes: int,
) -> str | None:
    """Return SHA256 for one optional typed FTP record without mutating it."""

    kind = transport.lstat(path)
    if kind is RemotePathType.MISSING:
        return None
    if kind is not RemotePathType.FILE:
        raise DeployError(f"FTP Hybrid metadata path is not a file: {path}")
    return hashlib.sha256(transport.read_file(path, max_bytes=max_bytes)).hexdigest()


def execute_recovery_plan(
    plan: ExplicitRecoveryPlan,
    state_store: StateStore,
    transport: Transport,
    *,
    verbose: bool,
) -> None:
    """Execute one explicitly reviewed Recovery without new deployment writes.

    Args:
        plan: Frozen recovery-only plan.
        state_store: Local State store for conservative post-command advancement.
        transport: Connected transport whose facts just passed freshness checks.
        verbose: Whether to print remote command execution context.

    Returns:
        ``None`` after restore or committed command/state/cleanup continuation.
    """

    if isinstance(plan, FTPRecoveryPlan):
        if not isinstance(transport, FTPTransport):
            raise PlanError("FTP Hybrid recovery requires FTPTransport semantics")
        validate_recovery_freshness(plan, transport)
        pending = plan.pending
        # Frozen Pending State remains authoritative for both post-commit phases.
        # Re-saving it makes cross-machine and deleted/stale local recovery converge.
        state_store.save(pending.next_state)
        if pending.phase is FTPPendingPhase.OWNERSHIP_COMMITTED:
            pending = pending.with_phase(FTPPendingPhase.STATE_COMPLETE)
            _write_ftp_pending(transport, pending)
        try:
            _cleanup_ftp_hybrid_pending(transport, pending)
        except Exception as exc:
            print(
                "WARNING: FTP Hybrid cleanup is pending and requires another "
                f"--recover: {exc}",
                file=sys.stderr,
                flush=True,
            )
        return
    validate_recovery_freshness(plan, transport)
    record = plan.record
    outcome = reconcile_recovery(transport, record)
    if not outcome.ownership_committed:
        print("Hybrid Recovery restored the pre-deployment remote state.")
        return
    current = record
    if outcome.commands_pending:
        _execute_remote_commands(
            plan.target,
            plan.target.after_deploy,
            transport,
            verbose=verbose,
        )
        current = current.with_phase(RecoveryPhase.COMMANDS_COMPLETE)
        write_recovery(transport, current)
    if outcome.state_pending:
        # Recovery records from v1.4.0 do not contain the original output hashes.
        # Advancing with an empty output manifest is conservative: the next normal
        # deployment re-uploads current outputs instead of trusting unknown hashes.
        # The commit must come from committed Remote Ownership rather than today's
        # HEAD, otherwise commits created after the interruption could be skipped.
        if plan.ownership is None:
            raise PlanError("committed hybrid recovery lacks Remote Ownership")
        state_store.save(
            new_state(
                plan.target.name,
                plan.target_fingerprint,
                plan.ownership.last_commit,
                {},
            )
        )
        current = current.with_phase(RecoveryPhase.STATE_COMPLETE)
        write_recovery(transport, current)
    try:
        cleanup_committed_recovery(transport, current)
    except Exception as exc:
        print(
            f"WARNING: hybrid cleanup is pending and requires another --recover: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _backup_current(
    transport: Transport,
    name: str,
    backup_root: str,
    expected_type: RemotePathType,
) -> None:
    """Move one path only when its current type equals the approved plan.

    Args:
        transport: Connected SFTP adapter.
        name: Direct Hybrid path about to be replaced or removed.
        backup_root: Deployment-owned Backup directory.
        expected_type: Exact type frozen in the reviewed Remote Plan.

    Returns:
        ``None`` after absence is reconfirmed or the expected path is backed up.
    """

    kind = transport.lstat(name)
    if kind is not expected_type:
        raise StaleRemotePlanError(
            f"remote path type changed before Hybrid swap: {name!r} "
            f"({expected_type.value} -> {kind.value})"
        )
    if expected_type is RemotePathType.MISSING:
        return
    if expected_type in {RemotePathType.SYMLINK, RemotePathType.OTHER}:
        raise DeployError(
            f"refusing to replace unsupported remote type {expected_type.value}: {name}"
        )
    destination = f"{backup_root}/{name}"
    if transport.lstat(destination) is not RemotePathType.MISSING:
        raise DeployError(f"hybrid backup destination already exists: {destination}")
    transport.rename_path(name, destination)


def _publish_staged(
    transport: Transport,
    staged_path: str,
    final_path: str,
    expected_type: RemotePathType,
) -> None:
    """Publish Stage with a fail-closed no-overwrite Missing-path contract.

    Args:
        transport: Connected SFTP adapter.
        staged_path: Deployment-owned staged file or directory.
        final_path: Reviewed direct Hybrid destination.
        expected_type: Destination type approved by the Remote Plan.

    Returns:
        ``None`` after the transport's no-overwrite rename succeeds.
    """

    try:
        transport.rename_path(staged_path, final_path)
    except DeployError as exc:
        if (
            expected_type is RemotePathType.MISSING
            and transport.lstat(staged_path) is not RemotePathType.MISSING
            and transport.lstat(final_path) is not RemotePathType.MISSING
        ):
            raise StaleRemotePlanError(
                f"remote path appeared during no-overwrite Hybrid publish: {final_path!r}"
            ) from exc
        raise


def _upload_with_retry(
    local_path: Path,
    remote_path: str,
    transport: Transport,
    progress: ProgressReporter,
    *,
    display_path: str | None = None,
    attempts: int,
    delay: float,
) -> None:
    """Retry one idempotent Hybrid upload without rebuilding local bytes.

    Args:
        local_path: Frozen bytes to upload.
        remote_path: Internal or final transport destination.
        transport: Connected upload transport.
        progress: Deployment-scoped transfer reporter.
        display_path: Optional logical final path hiding an internal Stage path.
        attempts: Maximum physical upload attempts.
        delay: Retry delay in seconds.

    Returns:
        ``None`` after one successful upload.
    """

    logical_path = display_path or remote_path

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                transport.invalidate_connection()
                _connect_with_retry(transport, attempts=1, delay=0)
            transport.upload(
                local_path,
                remote_path,
                progress.callback(logical_path, local_path.stat().st_size),
            )
            return
        except Exception as exc:
            if attempt >= attempts:
                raise DeployError(
                    f"{remote_path} failed after {attempts} attempt(s): {exc}"
                ) from exc
            print(
                f"Retry {attempt}/{attempts - 1} for {remote_path} after error: {exc}",
                flush=True,
            )
            progress.record_retry(logical_path)
            if delay:
                time.sleep(delay)


def _execute_after_deploy(
    plan: DeploymentPlan,
    transport: Transport,
    *,
    verbose: bool,
) -> None:
    """Run reviewed commands once, without retry, before committing State.

    Args:
        plan: Frozen plan containing validated commands and working directory.
        transport: Still-connected SFTP transport used for file operations.
        verbose: Whether to print non-secret execution context.

    Returns:
        ``None`` only after every configured command exits successfully.
    """

    _execute_remote_commands(
        plan.target,
        plan.target.after_deploy,
        transport,
        verbose=verbose,
    )


def _execute_remote_commands(
    target: TargetConfig,
    commands: tuple[str, ...],
    transport: Transport,
    *,
    verbose: bool,
) -> None:
    """Execute one frozen command contract through a connected transport.

    Args:
        target: Frozen endpoint, root, and command timeout.
        commands: Exact reviewed command sequence.
        transport: Connected SFTP transport.
        verbose: Whether to print endpoint execution context.

    Returns:
        ``None`` only after all commands exit successfully.
    """

    for index, command in enumerate(commands, start=1):
        marker = f"[{index}/{len(commands)}]"
        print(f"REMOTE {marker} {command}", flush=True)
        if verbose:
            endpoint = (
                f"{target.username or ''}@{target.host or target.ssh_host_alias}:"
                f"{target.port}"
            )
            print(
                f"REMOTE CONTEXT {marker} endpoint={endpoint} "
                f"cwd={target.remote_root} timeout={target.command_timeout}",
                flush=True,
            )
        try:
            transport.run_command(
                command,
                cwd=target.remote_root,
                timeout=target.command_timeout,
            )
        except Exception as exc:
            print(f"REMOTE FAILED {marker} {exc}", file=sys.stderr, flush=True)
            raise
        print(f"REMOTE OK {marker}", flush=True)


def _connect_with_retry(
    transport: Transport,
    *,
    attempts: int,
    delay: float,
    ensure_root: bool = True,
) -> None:
    """Connect and optionally ensure the remote root with retry policy.

    Args:
        transport: Adapter to connect or reconnect.
        attempts: Maximum connection attempts.
        delay: Delay between failed attempts in seconds.
        ensure_root: Whether this post-confirmation call may create the root.

    Returns:
        ``None`` after connection and any requested root creation succeeds.
    """

    for attempt in range(1, attempts + 1):
        try:
            transport.connect()
            if ensure_root:
                transport.ensure_root()
            return
        except Exception as exc:
            transport.invalidate_connection()
            if attempt >= attempts:
                raise DeployError(
                    f"remote connection failed after {attempts} attempt(s): {exc}"
                ) from exc
            print(
                f"Retry {attempt}/{attempts - 1} for remote connection after error: {exc}",
                flush=True,
            )
            if delay:
                time.sleep(delay)


def freeze_uploads(
    plan: DeploymentPlan,
    repository: GitRepository,
    staging: Path,
) -> dict[str, Path]:
    """Materialize exact source blobs and verified outputs before connecting."""

    frozen: dict[str, Path] = {}
    for operation in plan.operations:
        if not isinstance(operation, UploadOperation):
            continue
        destination = staging / operation.remote_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if operation.origin == "source":
            if not operation.git_path:
                raise PlanError(
                    f"source upload lacks a Git path: {operation.remote_path}"
                )
            repository.export_file(plan.head, operation.git_path, destination)
            if operation.sha256 is not None:
                if operation.size is None:
                    raise PlanError(
                        f"source content identity is incomplete: {operation.git_path}"
                    )
                actual = hash_file(destination)
                if actual != ManifestEntry(operation.sha256, operation.size):
                    raise PlanError(
                        "source content does not match the frozen plan: "
                        f"{operation.git_path}"
                    )
        else:
            if operation.local_path is None:
                raise PlanError(
                    f"output upload lacks a local path: {operation.remote_path}"
                )
            try:
                shutil.copyfile(operation.local_path, destination)
            except OSError as exc:
                raise PlanError(
                    f"cannot freeze output {operation.local_path}: {exc}"
                ) from exc
            expected = plan.output_manifest.get(operation.remote_path)
            actual = hash_file(destination)
            if expected is None or actual != expected:
                raise PlanError(
                    f"output changed while the deployment plan was being frozen: {operation.local_path}"
                )
        frozen[operation.remote_path] = destination
    if plan.hybrid is not None:
        for item in plan.hybrid.local.root_files:
            _freeze_hybrid_file(item.name, item.local_path, item.entry, staging, frozen)
        for directory in plan.hybrid.local.directories:
            for relative, scanned in directory.files.items():
                remote = (Path(directory.name) / Path(relative)).as_posix()
                _freeze_hybrid_file(
                    remote,
                    scanned.local_path,
                    scanned.entry,
                    staging,
                    frozen,
                )
    return frozen


def estimate_frozen_bytes(plan: DeploymentPlan, repository: GitRepository) -> int:
    """Estimate exact temporary storage needed by all planned uploads.

    Args:
        plan: Deployment operations whose upload bytes will be frozen.
        repository: Git reader used to size committed source blobs.

    Returns:
        Total bytes required, excluding small filesystem metadata overhead.
    """

    total = 0
    for operation in plan.operations:
        if not isinstance(operation, UploadOperation):
            continue
        if operation.origin == "source":
            if not operation.git_path:
                raise PlanError(f"source upload lacks a Git path: {operation.remote_path}")
            total += (
                operation.size
                if operation.size is not None
                else repository.blob_size(plan.head, operation.git_path)
            )
        elif operation.size is not None:
            total += operation.size
        elif operation.local_path is not None:
            total += operation.local_path.stat().st_size
    if plan.hybrid is not None:
        total += sum(item.entry.size for item in plan.hybrid.local.root_files)
        total += sum(item.total_size for item in plan.hybrid.local.directories)
    return total


def _freeze_hybrid_file(
    remote_path: str,
    local_path: Path,
    expected: ManifestEntry,
    staging: Path,
    frozen: dict[str, Path],
) -> None:
    """Copy and hash-verify one Hybrid byte source before remote preflight.

    Args:
        remote_path: Final relative path used as the frozen lookup key.
        local_path: Scanned aggregation file.
        expected: Immutable ``ManifestEntry`` captured during local planning.
        staging: Private local freeze root.
        frozen: Mutable frozen-path result map.

    Returns:
        ``None`` after verified bytes are bound to ``remote_path``.
    """

    destination = staging / remote_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(local_path, destination)
    except OSError as exc:
        raise PlanError(f"cannot freeze hybrid output {local_path}: {exc}") from exc
    if hash_file(destination) != expected:
        raise PlanError(f"hybrid output changed while being frozen: {local_path}")
    frozen[remote_path] = destination


def _execute_with_retry(
    operation: Operation,
    frozen: dict[str, Path],
    transport: Transport,
    progress: ProgressReporter,
    *,
    attempts: int,
    delay: float,
) -> None:
    """Retry one idempotent remote operation without rerunning the build."""

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                transport.invalidate_connection()
                _connect_with_retry(transport, attempts=1, delay=0)
            if isinstance(operation, UploadOperation):
                local = frozen[operation.remote_path]
                transport.upload(
                    local,
                    operation.remote_path,
                    progress.callback(operation.remote_path, local.stat().st_size),
                    executable=operation.executable,
                )
            else:
                transport.delete(operation.remote_path)
                print(f"DELETE {operation.remote_path}")
            return
        except Exception as exc:
            if attempt >= attempts:
                raise DeployError(
                    f"{operation.remote_path} failed after {attempts} attempt(s): {exc}"
                ) from exc
            print(
                f"Retry {attempt}/{attempts - 1} for {operation.remote_path} after error: {exc}",
                flush=True,
            )
            if isinstance(operation, UploadOperation):
                progress.record_retry(operation.remote_path)
            if delay:
                time.sleep(delay)
