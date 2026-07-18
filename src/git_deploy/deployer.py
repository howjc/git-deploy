"""Execute a frozen plan with per-file retries and delayed state commit."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from git_deploy.config import Config, TargetConfig
from git_deploy.errors import DeployError, PlanError, StaleRemotePlanError
from git_deploy.git import GitRepository
from git_deploy.hybrid import (
    RECOVERY_SCHEMA,
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
    HybridDirectoryDelete,
    HybridDirectoryMirror,
    HybridOwnershipUpdate,
    HybridRootFileDelete,
    HybridRootFileUpload,
    Operation,
    RecoveryPlan,
    UploadOperation,
    complete_remote_plan,
    validate_recovery_freshness,
    validate_remote_freshness,
)
from git_deploy.progress import ProgressReporter
from git_deploy.transports import create_transport
from git_deploy.transports.base import RemotePathType, Transport
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

    Returns:
        ``None`` after this project's remote operations and state commit succeed.
    """

    if not plan.operations and plan.hybrid is None:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return
    progress = ProgressReporter(verbose)
    transport = prepared_transport or (
        transport_factory(plan.target)
        if transport_factory is not None
        else create_transport(plan.target, connection_pool)
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


def execute_recovery_plan(
    plan: RecoveryPlan,
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
    attempts: int,
    delay: float,
) -> None:
    """Retry one idempotent Hybrid upload without rebuilding local bytes."""

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                transport.invalidate_connection()
                _connect_with_retry(transport, attempts=1, delay=0)
            transport.upload(
                local_path,
                remote_path,
                progress.callback(remote_path, local_path.stat().st_size),
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
                raise PlanError(f"source upload lacks a Git path: {operation.remote_path}")
            repository.export_file(plan.head, operation.git_path, destination)
        else:
            if operation.local_path is None:
                raise PlanError(f"output upload lacks a local path: {operation.remote_path}")
            try:
                shutil.copyfile(operation.local_path, destination)
            except OSError as exc:
                raise PlanError(f"cannot freeze output {operation.local_path}: {exc}") from exc
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
            total += repository.blob_size(plan.head, operation.git_path)
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
            if delay:
                time.sleep(delay)
