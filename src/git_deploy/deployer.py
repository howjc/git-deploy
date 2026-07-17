"""Execute a frozen plan with per-file retries and delayed state commit."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from git_deploy.config import Config, TargetConfig
from git_deploy.errors import DeployError, PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore, hash_file, new_state
from git_deploy.planner import DeploymentPlan, Operation, UploadOperation
from git_deploy.progress import ProgressReporter
from git_deploy.transports import create_transport
from git_deploy.transports.base import Transport
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

    if not plan.operations:
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

    if not plan.operations:
        state_store.save(
            new_state(plan.target.name, plan.target_fingerprint, plan.head, plan.output_manifest)
        )
        print("No file changes; deployment state advanced to HEAD.")
        return
    progress = ProgressReporter(verbose)
    transport = (
        transport_factory(plan.target)
        if transport_factory is not None
        else create_transport(plan.target, connection_pool)
    )
    try:
        _connect_with_retry(
            transport,
            attempts=config.deploy.retries,
            delay=config.deploy.retry_delay,
        )
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

    commands = plan.target.after_deploy
    for index, command in enumerate(commands, start=1):
        marker = f"[{index}/{len(commands)}]"
        print(f"REMOTE {marker} {command}", flush=True)
        if verbose:
            endpoint = (
                f"{plan.target.username or ''}@{plan.target.host or plan.target.ssh_host_alias}:"
                f"{plan.target.port}"
            )
            print(
                f"REMOTE CONTEXT {marker} endpoint={endpoint} "
                f"cwd={plan.target.remote_root} timeout={plan.target.command_timeout}",
                flush=True,
            )
        try:
            transport.run_command(
                command,
                cwd=plan.target.remote_root,
                timeout=plan.target.command_timeout,
            )
        except Exception as exc:
            print(f"REMOTE FAILED {marker} {exc}", file=sys.stderr, flush=True)
            raise
        print(f"REMOTE OK {marker}", flush=True)


def _connect_with_retry(transport: Transport, *, attempts: int, delay: float) -> None:
    """Connect and ensure the remote root with the configured retry policy."""

    for attempt in range(1, attempts + 1):
        try:
            transport.connect()
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
    return total


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
