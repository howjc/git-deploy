"""Transactional deployment, verification, and rollback orchestration."""

from __future__ import annotations

import hashlib
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import GitDeployError, PolicyError, RemoteDriftError
from .gitrepo import GitDeploymentPlanner
from .models import DeploymentManifest, DeploymentPlan, FileSnapshot, ProjectConfig
from .state import DeploymentStore
from .transport import RemoteTransport, open_transport


@dataclass(frozen=True)
class RemoteObservation:
    """Current bytes and mode observed for one remote path."""

    path: str
    remote_path: str
    data: bytes | None
    executable: bool | None

    @property
    def sha256(self) -> str | None:
        """Return the observed content hash, or ``None`` when absent."""

        return _sha256(self.data) if self.data is not None else None


@dataclass(frozen=True)
class RemoteCheck:
    """One human-readable baseline or manifest verification result."""

    path: str
    expected_sha256: str | None
    actual_sha256: str | None
    matches: bool


TransportFactory = Callable[[dict[str, object]], RemoteTransport]
HealthChecker = Callable[[str], None]


class DeploymentExecutor:
    """Apply and reverse one project's plans with exact local backups."""

    def __init__(
        self,
        project: ProjectConfig,
        server: dict[str, object],
        transport_factory: TransportFactory = open_transport,
        health_checker: HealthChecker | None = None,
    ):
        """Bind project policy, server settings, and injectable side effects.

        Args:
            project: Project being deployed.
            server: Raw ``[server]`` values.
            transport_factory: Function opening a connected remote transport.
            health_checker: Optional URL checker used by tests or integrations.
        """

        self.project = project
        self.server = server
        self.store = DeploymentStore(project)
        self._transport_factory = transport_factory
        self._health_checker = health_checker or _check_health_url

    def check_plan(self, plan: DeploymentPlan, force: bool = False) -> tuple[RemoteCheck, ...]:
        """Read remotely and verify a plan's source-commit baseline.

        Args:
            plan: Local Git deployment plan.
            force: Return drift results instead of rejecting them.

        Returns:
            One check result per planned path.
        """

        transport = self._transport_factory(self.server)
        try:
            observations = self._observe_plan(transport, plan)
            checks = _checks_for_plan(plan, observations)
            _reject_drift(checks, force, "remote files differ from the source commit")
            return checks
        finally:
            _close_transport(transport)

    def deploy(
        self,
        plan: DeploymentPlan,
        planner: GitDeploymentPlanner,
        force: bool = False,
    ) -> DeploymentManifest:
        """Apply a plan after backup and automatically restore on failure.

        Args:
            plan: Immutable Git deployment plan.
            planner: Planner used to read exact target-commit bytes.
            force: Permit a remote baseline mismatch while still backing it up.

        Returns:
            Successful persisted deployment manifest.
        """

        targets = {
            operation.path: planner.target_bytes(plan, operation)
            for operation in plan.files
            if operation.action == "upload"
        }
        for operation in plan.files:
            if operation.action == "upload" and _sha256(targets[operation.path]) != operation.target_sha256:
                raise GitDeployError(f"target Git blob hash changed unexpectedly: {operation.path}")

        transport = self._transport_factory(self.server)
        mutation_started = False
        manifest: DeploymentManifest | None = None
        try:
            self._require_command_support(transport)
            observations = self._observe_plan(transport, plan)
            checks = _checks_for_plan(plan, observations)
            _reject_drift(checks, force, "remote files differ from the source commit")

            manifest = self._prepare_manifest(plan, observations)
            self.store.write_manifest(manifest)
            mutation_started = True
            self._apply_plan(transport, plan, targets)
            self._run_post_steps(transport)
            self._verify_snapshots(transport, manifest.snapshots, after=True)
            manifest.status = "succeeded"
            manifest.error = None
            self.store.write_manifest(manifest)
            return manifest
        except Exception as exc:
            if manifest is None or not mutation_started:
                if isinstance(exc, GitDeployError):
                    raise
                raise GitDeployError(f"deployment preparation failed: {exc}") from exc
            self._recover_failed_deploy(transport, manifest, exc)
            raise GitDeployError(
                f"deployment {manifest.deployment_id} failed and remote files were restored: {exc}"
            ) from exc
        finally:
            _close_transport(transport)

    def verify(self, manifest: DeploymentManifest) -> tuple[RemoteCheck, ...]:
        """Verify remote state represented by a deployment manifest.

        Args:
            manifest: Persisted deployment record.

        Returns:
            One verification result per touched path.
        """

        after = manifest.status not in {"rolled_back", "auto_rolled_back"}
        transport = self._transport_factory(self.server)
        try:
            return self._verify_snapshots(transport, manifest.snapshots, after=after)
        finally:
            _close_transport(transport)

    def check_rollback(
        self,
        manifest: DeploymentManifest,
        force: bool = False,
    ) -> tuple[RemoteCheck, ...]:
        """Read-only verification that a deployment is currently reversible.

        Args:
            manifest: Successful deployment selected for rollback.
            force: Return drift results instead of rejecting them.

        Returns:
            Verification of the post-deployment state.
        """

        self._require_rollback_status(manifest)
        transport = self._transport_factory(self.server)
        try:
            checks = self._snapshot_checks(transport, manifest.snapshots, after=True)
            _reject_drift(checks, force, "remote files changed after this deployment")
            return checks
        finally:
            _close_transport(transport)

    def rollback(
        self,
        manifest: DeploymentManifest,
        force: bool = False,
    ) -> DeploymentManifest:
        """Restore a deployment's exact pre-deployment snapshot.

        Args:
            manifest: Successful deployment selected for rollback.
            force: Permit post-deployment drift while preserving recovery bytes.

        Returns:
            Updated manifest with ``rolled_back`` status.
        """

        self._require_rollback_status(manifest)
        transport = self._transport_factory(self.server)
        current: dict[str, RemoteObservation] = {}
        mutation_started = False
        try:
            self._require_command_support(transport)
            current = self._observe_snapshots(transport, manifest.snapshots)
            checks = _checks_for_snapshots(manifest.snapshots, current, after=True)
            _reject_drift(checks, force, "remote files changed after this deployment")

            manifest.status = "rollback_in_progress"
            manifest.error = None
            self.store.write_manifest(manifest)
            mutation_started = True
            self._restore_before(transport, manifest)
            self._run_post_steps(transport)
            self._verify_snapshots(transport, manifest.snapshots, after=False)
            manifest.status = "rolled_back"
            self.store.write_manifest(manifest)
            return manifest
        except Exception as exc:
            if not mutation_started:
                if isinstance(exc, GitDeployError):
                    raise
                raise GitDeployError(f"rollback preparation failed: {exc}") from exc
            recovery_error = self._restore_observations(transport, current)
            if recovery_error is None:
                manifest.status = "succeeded"
                manifest.error = f"rollback failed; post-deployment files restored: {exc}"
            else:
                manifest.status = "rollback_failed"
                manifest.error = f"rollback failed: {exc}; forward recovery failed: {recovery_error}"
            self.store.write_manifest(manifest)
            raise GitDeployError(
                f"rollback {manifest.deployment_id} failed: {manifest.error}"
            ) from exc
        finally:
            _close_transport(transport)

    def _prepare_manifest(
        self,
        plan: DeploymentPlan,
        observations: dict[str, RemoteObservation],
    ) -> DeploymentManifest:
        """Persist backups and build a prepared deployment record.

        Args:
            plan: Plan about to be applied.
            observations: Exact remote state captured after drift validation.

        Returns:
            Prepared deployment manifest.
        """

        deployment_id = self._new_deployment_id(plan.to_commit)
        snapshots: list[FileSnapshot] = []
        for index, operation in enumerate(plan.files):
            observed = observations[operation.path]
            backup_file = None
            if observed.data is not None:
                backup_file = self.store.write_backup(deployment_id, index, observed.data)
            snapshots.append(
                FileSnapshot(
                    path=operation.path,
                    remote_path=operation.remote_path,
                    before_exists=observed.data is not None,
                    before_sha256=observed.sha256,
                    backup_file=backup_file,
                    after_exists=operation.action == "upload",
                    after_sha256=operation.target_sha256,
                    before_executable=observed.executable,
                    after_executable=operation.executable if operation.action == "upload" else None,
                )
            )
        return DeploymentManifest(
            deployment_id=deployment_id,
            project=plan.project,
            repository=str(plan.repository),
            remote_root=plan.remote_root,
            from_commit=plan.from_commit,
            to_commit=plan.to_commit,
            created_at=datetime.now(UTC).isoformat(),
            status="prepared",
            snapshots=snapshots,
        )

    def _new_deployment_id(self, target_commit: str) -> str:
        """Create a sortable, collision-resistant local deployment ID.

        Args:
            target_commit: Resolved target Git commit.

        Returns:
            New deployment identifier not currently present in local state.
        """

        while True:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            candidate = f"{timestamp}-{target_commit[:12]}-{secrets.token_hex(2)}"
            if not self.store.deployment_dir(candidate).exists():
                return candidate

    def _observe_plan(
        self,
        transport: RemoteTransport,
        plan: DeploymentPlan,
    ) -> dict[str, RemoteObservation]:
        """Capture each unique remote path referenced by a plan.

        Args:
            transport: Connected remote transport.
            plan: Plan whose baseline is inspected.

        Returns:
            Observations keyed by repository path.
        """

        observations: dict[str, RemoteObservation] = {}
        for operation in plan.files:
            if operation.path in observations:
                raise PolicyError(f"deployment plan touches a path more than once: {operation.path}")
            data = transport.read_file(operation.remote_path)
            executable = transport.is_executable(operation.remote_path) if data is not None else None
            if executable is None and data is not None:
                executable = operation.expected_before_executable
            observations[operation.path] = RemoteObservation(
                path=operation.path,
                remote_path=operation.remote_path,
                data=data,
                executable=executable,
            )
        return observations

    def _observe_snapshots(
        self,
        transport: RemoteTransport,
        snapshots: list[FileSnapshot],
    ) -> dict[str, RemoteObservation]:
        """Capture each path represented by stored snapshots.

        Args:
            transport: Connected remote transport.
            snapshots: Manifest snapshots to inspect.

        Returns:
            Observations keyed by repository path.
        """

        observations: dict[str, RemoteObservation] = {}
        for snapshot in snapshots:
            data = transport.read_file(snapshot.remote_path)
            executable = transport.is_executable(snapshot.remote_path) if data is not None else None
            observations[snapshot.path] = RemoteObservation(
                path=snapshot.path,
                remote_path=snapshot.remote_path,
                data=data,
                executable=executable,
            )
        return observations

    def _apply_plan(
        self,
        transport: RemoteTransport,
        plan: DeploymentPlan,
        targets: dict[str, bytes],
    ) -> None:
        """Upload replacements first and process deletions last.

        Args:
            transport: Connected writable transport.
            plan: Plan being applied.
            targets: Exact target-commit bytes keyed by path.
        """

        for operation in plan.files:
            if operation.action == "upload":
                transport.replace_file(operation.remote_path, targets[operation.path], operation.executable)
        for operation in plan.files:
            if operation.action == "delete":
                transport.delete_file(operation.remote_path)

    def _restore_before(self, transport: RemoteTransport, manifest: DeploymentManifest) -> None:
        """Restore pre-deployment files, deleting paths that were originally absent.

        Args:
            transport: Connected writable transport.
            manifest: Deployment whose backups are restored.
        """

        for snapshot in manifest.snapshots:
            if snapshot.before_exists:
                if not snapshot.backup_file:
                    raise GitDeployError(f"manifest has no backup for {snapshot.path}")
                data = self.store.read_backup(manifest.deployment_id, snapshot.backup_file)
                if _sha256(data) != snapshot.before_sha256:
                    raise GitDeployError(f"local backup hash mismatch for {snapshot.path}")
                transport.replace_file(snapshot.remote_path, data, bool(snapshot.before_executable))
        for snapshot in manifest.snapshots:
            if not snapshot.before_exists:
                transport.delete_file(snapshot.remote_path)

    def _restore_observations(
        self,
        transport: RemoteTransport,
        observations: dict[str, RemoteObservation],
    ) -> Exception | None:
        """Best-effort restore of in-memory observations after rollback failure.

        Args:
            transport: Connected writable transport.
            observations: Remote state captured before rollback.

        Returns:
            Recovery exception, or ``None`` when forward state was restored.
        """

        try:
            for observed in observations.values():
                if observed.data is not None:
                    transport.replace_file(
                        observed.remote_path,
                        observed.data,
                        bool(observed.executable),
                    )
            for observed in observations.values():
                if observed.data is None:
                    transport.delete_file(observed.remote_path)
            return None
        except Exception as exc:  # Recovery must retain the original failure as primary context.
            return exc

    def _recover_failed_deploy(
        self,
        transport: RemoteTransport,
        manifest: DeploymentManifest,
        cause: Exception,
    ) -> None:
        """Restore backups after a partially applied deployment.

        Args:
            transport: Connected writable transport.
            manifest: Prepared deployment record.
            cause: Original deployment failure.
        """

        try:
            self._restore_before(transport, manifest)
            self._verify_snapshots(transport, manifest.snapshots, after=False)
            manifest.status = "auto_rolled_back"
            manifest.error = str(cause)
            self.store.write_manifest(manifest)
        except Exception as recovery_error:
            manifest.status = "failed"
            manifest.error = f"{cause}; automatic recovery failed: {recovery_error}"
            self.store.write_manifest(manifest)
            raise GitDeployError(
                f"deployment {manifest.deployment_id} failed and automatic recovery also failed: "
                f"{recovery_error}"
            ) from cause

    def _verify_snapshots(
        self,
        transport: RemoteTransport,
        snapshots: list[FileSnapshot],
        after: bool,
    ) -> tuple[RemoteCheck, ...]:
        """Require remote files to match one side of stored snapshots.

        Args:
            transport: Connected remote transport.
            snapshots: Manifest snapshots to verify.
            after: Verify post-deployment state when true, otherwise pre-deployment state.

        Returns:
            Successful verification results.
        """

        checks = self._snapshot_checks(transport, snapshots, after)
        side = "post-deployment" if after else "pre-deployment"
        _reject_drift(checks, False, f"remote files do not match {side} state")
        return checks

    def _snapshot_checks(
        self,
        transport: RemoteTransport,
        snapshots: list[FileSnapshot],
        after: bool,
    ) -> tuple[RemoteCheck, ...]:
        """Build hash checks for one side of stored snapshots.

        Args:
            transport: Connected remote transport.
            snapshots: Manifest snapshots to inspect.
            after: Select after-state hashes when true.

        Returns:
            Check results without enforcing them.
        """

        observations = self._observe_snapshots(transport, snapshots)
        return _checks_for_snapshots(snapshots, observations, after)

    def _require_command_support(self, transport: RemoteTransport) -> None:
        """Reject configured post commands on transports without a shell.

        Args:
            transport: Connected remote transport.
        """

        if self.project.post_commands and not transport.supports_commands:
            raise PolicyError("post_commands require SFTP/SSH and cannot run over FTP/FTPS")

    def _run_post_steps(self, transport: RemoteTransport) -> None:
        """Run configured commands and HTTP health checks in order.

        Args:
            transport: Connected remote transport.
        """

        for command in self.project.post_commands:
            code, stdout, stderr = transport.execute(command)
            if code != 0:
                detail = stderr.strip() or stdout.strip() or f"exit code {code}"
                raise GitDeployError(f"post command failed: {command}: {detail}")
        for url in self.project.health_urls:
            self._health_checker(url)

    @staticmethod
    def _require_rollback_status(manifest: DeploymentManifest) -> None:
        """Require a completed deployment before rollback.

        Args:
            manifest: Candidate deployment manifest.
        """

        if manifest.status != "succeeded":
            raise PolicyError(
                f"deployment {manifest.deployment_id} has status {manifest.status}; "
                "only succeeded deployments can be rolled back"
            )


def _checks_for_plan(
    plan: DeploymentPlan,
    observations: dict[str, RemoteObservation],
) -> tuple[RemoteCheck, ...]:
    """Compare plan baseline hashes with observed remote bytes.

    Args:
        plan: Deployment plan defining source-state hashes.
        observations: Captured remote files keyed by path.

    Returns:
        Hash check results.
    """

    return tuple(
        RemoteCheck(
            path=operation.path,
            expected_sha256=operation.expected_before_sha256,
            actual_sha256=observations[operation.path].sha256,
            matches=operation.expected_before_sha256 == observations[operation.path].sha256,
        )
        for operation in plan.files
    )


def _checks_for_snapshots(
    snapshots: list[FileSnapshot],
    observations: dict[str, RemoteObservation],
    after: bool,
) -> tuple[RemoteCheck, ...]:
    """Compare one side of manifest snapshots with remote observations.

    Args:
        snapshots: Stored before/after hashes.
        observations: Current remote files keyed by path.
        after: Select post-deployment state when true.

    Returns:
        Hash check results.
    """

    checks: list[RemoteCheck] = []
    for snapshot in snapshots:
        expected = snapshot.after_sha256 if after else snapshot.before_sha256
        actual = observations[snapshot.path].sha256
        checks.append(
            RemoteCheck(
                path=snapshot.path,
                expected_sha256=expected,
                actual_sha256=actual,
                matches=expected == actual,
            )
        )
    return tuple(checks)


def _reject_drift(checks: tuple[RemoteCheck, ...], force: bool, context: str) -> None:
    """Raise one concise drift error unless force mode is active.

    Args:
        checks: Remote hash comparisons.
        force: Suppress drift rejection when true.
        context: Human-readable operation context.
    """

    mismatches = [check for check in checks if not check.matches]
    if not mismatches or force:
        return
    details = ", ".join(
        f"{item.path} (expected {_short_hash(item.expected_sha256)}, "
        f"actual {_short_hash(item.actual_sha256)})"
        for item in mismatches[:8]
    )
    if len(mismatches) > 8:
        details += f", and {len(mismatches) - 8} more"
    raise RemoteDriftError(f"{context}: {details}")


def _check_health_url(url: str) -> None:
    """Require one configured HTTP endpoint to return a 2xx status.

    Args:
        url: Absolute HTTP or HTTPS health-check URL.
    """

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GitDeployError(f"health check URL must use HTTP or HTTPS: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "git-deploy/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
    except (OSError, urllib.error.URLError) as exc:
        raise GitDeployError(f"health check failed for {url}: {exc}") from exc
    if not 200 <= status < 300:
        raise GitDeployError(f"health check failed for {url}: HTTP {status}")


def _close_transport(transport: RemoteTransport) -> None:
    """Close a transport without masking the completed operation result.

    Args:
        transport: Connected transport to release.
    """

    try:
        transport.close()
    except Exception:
        pass


def _sha256(data: bytes) -> str:
    """Return a SHA-256 hex digest for file bytes.

    Args:
        data: File content.

    Returns:
        Lowercase hexadecimal digest.
    """

    return hashlib.sha256(data).hexdigest()


def _short_hash(value: str | None) -> str:
    """Format a hash or file absence for diagnostics.

    Args:
        value: Full hash or ``None`` for an absent file.

    Returns:
        Short display value.
    """

    return value[:12] if value else "absent"
