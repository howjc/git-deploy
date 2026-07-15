"""Latest-only stateful rollback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, PolicyError, StalePlanError
from .expected_state import ExpectedStateStore, FileEntry, build_expected_state
from .models import DeploymentManifest, ProjectConfig
from .state import DeploymentStore
from .state_executor import InMemoryTransport, restore_backup_entries, run_post_write_lifecycle
from .target_identity import TargetIdentity, policy_fingerprint_for_project
from .target_lock import TargetLock
from .transaction import TransactionStore


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of a stateful rollback.

    Attributes:
        status: ``succeeded`` or ``refused``.
        generation: New generation after rollback.
        after_state_id: New current state id.
        restored_paths: Paths restored from backups.
        deployment_id: Exact deployment that was rolled back (``rollback_of``).
    """

    status: str
    generation: int | None
    after_state_id: str | None
    restored_paths: tuple[str, ...]
    deployment_id: str | None = None


class StateRollbackService:
    """Roll back only the latest successful stateful deployment."""

    def __init__(
        self,
        project: ProjectConfig,
        identity: TargetIdentity,
        target_root: Path,
        *,
        transport: Any | None = None,
    ):
        """Bind stores and optional transport.

        Args:
            project: Project configuration.
            identity: Physical identity.
            target_root: Target state root.
            transport: Optional transport (in-memory tests or real adapter).
        """

        self.project = project
        self.identity = identity
        self.target_root = target_root
        self.transport: Any = transport if transport is not None else InMemoryTransport()
        self.state_store = ExpectedStateStore(target_root, identity)
        self.deploy_store = DeploymentStore(project, root=target_root)
        self.tx_store = TransactionStore(target_root)
        self.policy_fp = policy_fingerprint_for_project(project)

    def assert_latest_only(self, deployment_id: str) -> DeploymentManifest:
        """Refuse non-latest rollback before any connect/write when current exists.

        Args:
            deployment_id: Requested deployment id or prefix.

        Returns:
            Latest successful manifest when it matches the request.

        Raises:
            PolicyError: When non-latest is requested under established current.
        """

        current = self.state_store.read_current()
        latest = self.deploy_store.latest_successful()
        resolved = self.deploy_store.resolve_id(deployment_id)
        if current is not None and resolved != latest.deployment_id:
            # Zero connect / zero write: do not touch transport.
            raise PolicyError(
                "stateful rollback of non-latest deployment is not supported in v0.3.0 "
                f"(requested {resolved}, latest {latest.deployment_id}); deferred to v0.3"
            )
        if resolved != latest.deployment_id and current is None:
            # Legacy path may allow non-latest without current; Gate A focuses on current path.
            pass
        return self.deploy_store.load(resolved)

    def assert_rollback_eligible(self, manifest: DeploymentManifest | None = None) -> DeploymentManifest:
        """Refuse already-rolled-back / advanced / broken lineage before transport.

        Must run on the CLI path before ``open_cli_transport`` so factory/read/write
        stay zero when current no longer matches the deployment after-state or when
        v1 before-state cannot be loaded.

        Args:
            manifest: Optional deployment; default is latest successful.

        Returns:
            Eligible latest deployment manifest.

        Raises:
            PolicyError: When eligibility fails (zero remote I/O implied for caller).
        """

        from .state_guards import StateGuards

        # Rollback is a state mutation too: current/CAS/Git/policy/open-journal
        # integrity must fail closed before any transport factory or remote I/O.
        StateGuards(
            self.target_root,
            self.identity,
            expected_policy=self.policy_fp,
        ).require_clear()
        if manifest is None:
            manifest = self.deploy_store.latest_successful()
        loaded = self.state_store.load_current_state()
        if loaded is None:
            raise PolicyError("no current state; use legacy rollback without state lineage")
        pointer, current_state = loaded
        # after_state must still be current — blocks repeat rollback without connect.
        if not manifest.after_state_id:
            if (manifest.state or "") == "v1" or manifest.before_state_id:
                raise PolicyError(
                    "rollback refused: v1/lineage deployment missing after_state_id"
                )
        elif manifest.after_state_id != pointer.state_id:
            raise PolicyError(
                "rollback refused: deployment after-state does not match current "
                f"(already rolled back or current advanced by another transition); "
                f"deployment={manifest.deployment_id}"
            )
        has_lineage = bool(manifest.before_state_id) or (manifest.state or "") == "v1"
        if has_lineage:
            if not manifest.before_state_id:
                raise PolicyError(
                    "rollback refused: v1/lineage deployment missing before_state_id"
                )
            try:
                before_state = self.state_store.read_state(manifest.before_state_id)
            except (ConfigurationError, PolicyError, OSError, ValueError, KeyError) as exc:
                raise PolicyError(
                    f"rollback refused: before state unreadable or invalid "
                    f"({manifest.before_state_id[:12]}…): {exc}"
                ) from exc
            except Exception as exc:
                raise PolicyError(
                    f"rollback refused: before state integrity failure: {exc}"
                ) from exc
            if before_state.physical_fingerprint != self.identity.physical_fingerprint:
                raise PolicyError(
                    "rollback refused: before state physical identity mismatch"
                )
            if before_state.policy_fingerprint != self.policy_fp:
                raise PolicyError(
                    "rollback refused: before state managed policy mismatch"
                )
            if current_state.parent_state_id != before_state.state_id():
                raise PolicyError(
                    "rollback refused: current lineage does not point to deployment before state"
                )
            if (
                manifest.before_generation is not None
                and before_state.generation != manifest.before_generation
            ):
                raise PolicyError(
                    "rollback refused: before state generation does not match manifest"
                )
            if (
                manifest.after_generation is not None
                and current_state.generation != manifest.after_generation
            ):
                raise PolicyError(
                    "rollback refused: current generation does not match manifest"
                )
            try:
                from .git_store import PersistentGitStore

                PersistentGitStore(
                    self.target_root,
                    self.project.repository,
                ).require_tree(before_state.source_tree_id)
            except Exception as exc:
                raise PolicyError(
                    f"rollback refused: before state source tree unreadable: {exc}"
                ) from exc
        self._require_valid_before_backups(manifest)
        return manifest

    def _require_valid_before_backups(self, manifest: DeploymentManifest) -> None:
        """Verify every rollback backup before transport creation or mutation.

        Args:
            manifest: Eligible latest deployment whose before bytes may be restored.

        Returns:
            None.
        """

        import hashlib

        for snapshot in manifest.snapshots:
            if not snapshot.before_exists:
                continue
            if not snapshot.backup_file or not snapshot.before_sha256:
                raise PolicyError(
                    f"rollback refused: {snapshot.path} has incomplete before backup metadata"
                )
            try:
                data = self.deploy_store.read_backup(
                    manifest.deployment_id,
                    snapshot.backup_file,
                )
            except Exception as exc:
                raise PolicyError(
                    f"rollback refused: {snapshot.path} backup is unreadable: {exc}"
                ) from exc
            actual = hashlib.sha256(data).hexdigest()
            if actual != snapshot.before_sha256:
                raise PolicyError(
                    f"rollback refused: {snapshot.path} backup hash mismatch "
                    f"expected={snapshot.before_sha256[:12]} actual={actual[:12]}"
                )

    def rollback_latest(
        self,
        *,
        force: bool = False,
        fail_after_writes: int | None = None,
        expected_deployment_id: str | None = None,
        expected_generation: int | None = None,
        fail_at: str | None = None,
    ) -> RollbackResult:
        """Restore remote before bytes and CAS a new generation reflecting before lineage.

        Durable transaction evidence is written before the first remote mutation so
        a partial multi-file crash can be recovered via ``state recover``.

        Under target lock and before journal/remote mutation, remote bytes are
        compared to each snapshot's after-state (exists/hash/mode). Default
        refuses third-party content with zero mutations; ``force=True`` continues
        and persists the real pre-rollback remote bytes as recovery backup.

        When ``expected_deployment_id`` is set (application exact-plan path), the
        lock-held path loads that exact deployment, verifies it is still the
        latest successful and matches ``expected_generation``/current, and never
        silently re-selects a newer latest.

        P1-05: once every snapshot is restored and read-back verified, the same
        ``post_commands``/``health_urls`` lifecycle a deploy runs is applied here
        too (caches/reload/health), before advancing to ``remote_verified`` and
        CAS-ing current. If the lifecycle fails, the rollback-before remote bytes
        (captured just before this rollback's own mutation) are restored and
        generation is left unadvanced, matching the deploy failure contract.

        Args:
            force: Allow remote after-state drift (third-party content).
            fail_after_writes: Test-only injection for partial rollback crash.
            expected_deployment_id: Exact preview-bound deployment to roll back.
            expected_generation: Preview-bound current generation at plan time.
            fail_at: Test-only lifecycle injection point ``hook`` or ``health``.

        Returns:
            Rollback result including the exact ``deployment_id`` rolled back.
        """

        with TargetLock(self.target_root):
            if self.tx_store.list_open():
                raise PolicyError("unfinished transaction blocks rollback; run state recover")
            loaded = self.state_store.load_current_state()
            if loaded is None:
                raise PolicyError("no current state; use legacy rollback without state lineage")
            pointer, current_state = loaded
            if expected_generation is not None and pointer.generation != expected_generation:
                raise StalePlanError(
                    "stale_plan: current generation changed before rollback "
                    f"(expected {expected_generation}, actual {pointer.generation}); "
                    "re-preview required"
                )
            if expected_deployment_id is not None:
                # Exact reviewed deployment — never re-select latest under lock.
                try:
                    manifest = self.deploy_store.load(expected_deployment_id)
                except Exception as exc:
                    raise StalePlanError(
                        "stale_plan: expected deployment is no longer loadable; "
                        f"re-preview required: {exc}"
                    ) from exc
                latest = self.deploy_store.latest_successful()
                if latest.deployment_id != manifest.deployment_id:
                    raise StalePlanError(
                        "stale_plan: reviewed deployment is no longer latest successful "
                        f"(expected {manifest.deployment_id}, latest {latest.deployment_id}); "
                        "re-preview required"
                    )
                # Eligibility against the exact reviewed manifest (not a re-pick).
                manifest = self.assert_rollback_eligible(manifest)
            else:
                # Same gates as CLI pre-connect path (after_state match + before readable).
                manifest = self.assert_rollback_eligible()
            # P1-03: lock-held after-state check before any durable write or
            # mutation, reading each remote path exactly once — the same
            # observation is reused below for backup capture instead of a
            # second independent read (eliminating that TOCTOU window).
            observed_before_mutation = self._observe_remote_before_mutation(
                manifest, force=force
            )
            # v1 / lineage: before immutable state already validated; load for after build.
            # Snapshot-only fallback is limited to true legacy/no-lineage manifests.
            after = None
            has_lineage = bool(manifest.before_state_id) or (manifest.state or "") == "v1"
            if has_lineage:
                before_state_id = manifest.before_state_id
                if before_state_id is None:
                    raise PolicyError(
                        "rollback refused: v1/lineage deployment missing before_state_id"
                    )
                before_snap = self.state_store.read_state(before_state_id)
                after = build_expected_state(
                    generation=pointer.generation + 1,
                    parent_state_id=pointer.state_id,
                    source_tree_id=before_snap.source_tree_id,
                    applied_transition_ids=before_snap.applied_transition_ids,
                    physical_fingerprint=self.identity.physical_fingerprint,
                    policy_fingerprint=self.policy_fp,
                    files=before_snap.files,
                    deployment_id=manifest.deployment_id,
                    artifacts=before_snap.artifacts,
                )
            else:
                # Legacy/no-lineage only: reconstruct from manifest snapshots.
                introduced = set(manifest.introduced_transition_ids or [])
                remaining = tuple(
                    tid
                    for tid in current_state.applied_transition_ids
                    if tid not in introduced
                )
                before_entries = self._entries_from_manifest_before(manifest)
                after = build_expected_state(
                    generation=pointer.generation + 1,
                    parent_state_id=pointer.state_id,
                    source_tree_id=current_state.source_tree_id,
                    applied_transition_ids=remaining,
                    physical_fingerprint=self.identity.physical_fingerprint,
                    policy_fingerprint=self.policy_fp,
                    files=before_entries or current_state.files,
                    deployment_id=manifest.deployment_id,
                    artifacts=current_state.artifacts,
                )

            after_id = self.state_store.write_state(after)
            backup_entries: list[dict[str, Any]] = []
            for index, snapshot in enumerate(manifest.snapshots):
                entry: dict[str, Any] = {
                    "path": snapshot.path,
                    "remote_path": snapshot.remote_path,
                    "backup_file": None,
                    "before_exists": True,
                    "executable": bool(snapshot.after_executable),
                }
                # P1-03: reuse the single pre-mutation observation (possibly
                # third-party content when force) instead of reading again, so
                # crash recovery restores exactly the bytes that were live
                # immediately before this rollback mutated anything.
                data = observed_before_mutation.get(snapshot.remote_path)
                if data is not None:
                    rel = self.deploy_store.write_backup(
                        f"rb-{manifest.deployment_id}",
                        index,
                        data,
                    )
                    entry["backup_file"] = rel
                    entry["before_exists"] = True
                else:
                    entry["before_exists"] = False
                backup_entries.append(entry)

            journal = self.tx_store.create(
                target_id=self.identity.target_id,
                stage="prepared",
                deployment_id=f"rb-{manifest.deployment_id}",
                before_state_id=pointer.state_id,
                after_state_id=after_id,
                before_generation=pointer.generation,
                after_generation=after.generation,
                meta={
                    "kind": "rollback",
                    "rollback_of": manifest.deployment_id,
                    "backup_entries": backup_entries,
                    "paths": [snap.path for snap in manifest.snapshots],
                    "force": bool(force),
                },
            )
            journal = self.tx_store.advance(journal, "remote_mutating")
            restored: list[str] = []
            try:
                writes_done = 0
                for snapshot in manifest.snapshots:
                    if snapshot.backup_file:
                        data = self.deploy_store.read_backup(
                            manifest.deployment_id, snapshot.backup_file
                        )
                        writer = getattr(self.transport, "write_file", None)
                        if not callable(writer):
                            writer = getattr(self.transport, "replace_file")
                        writer(
                            snapshot.remote_path,
                            data,
                            executable=bool(snapshot.before_executable),
                        )
                    elif not snapshot.before_exists:
                        self.transport.delete_file(snapshot.remote_path)
                    # Post-write readback before advancing verified / CAS.
                    self._verify_rollback_snapshot(snapshot)
                    restored.append(snapshot.path)
                    writes_done += 1
                    if fail_after_writes is not None and writes_done >= fail_after_writes:
                        raise RuntimeError(
                            f"injected partial rollback failure after {writes_done} write(s)"
                        )
            except Exception as exc:
                # Leave journal open at remote_mutating for recover; do not CAS.
                raise PolicyError(
                    f"rollback partial failure; run state recover: {exc}"
                ) from exc

            try:
                # P1-05: same post-write lifecycle contract as deploy, run only
                # after every snapshot write is durable and read-back verified.
                run_post_write_lifecycle(self.transport, self.project, fail_at=fail_at)
            except Exception as exc:
                try:
                    if not journal.deployment_id:
                        raise ConfigurationError(
                            "cannot restore rollback-before bytes: journal has no deployment_id"
                        )
                    restore_backup_entries(
                        self.deploy_store,
                        self.transport,
                        journal.deployment_id,
                        backup_entries,
                    )
                    journal = self.tx_store.advance(journal, "recovered")
                except Exception as restore_exc:
                    journal = self.tx_store.advance(
                        journal,
                        "manual_recovery_required",
                        error=f"{exc}; restore failed: {restore_exc}",
                    )
                    raise PolicyError(
                        "manual_recovery_required: automatic restore failed; "
                        "rollback blocked"
                    ) from restore_exc
                raise PolicyError(
                    "rollback lifecycle (post_commands/health_urls) failed; "
                    f"rollback-before remote bytes restored, generation unchanged: {exc}"
                ) from exc

            try:
                journal = self.tx_store.advance(journal, "remote_verified")
                self.state_store.cas_advance(expected_generation=pointer.generation, state=after)
                journal = self.tx_store.advance(journal, "state_committed")
                journal = self.tx_store.advance(journal, "recovered")
            except Exception as exc:
                # Remote already reflects the rollback and lifecycle already
                # ran; only the local journal/CAS bookkeeping failed. Leave a
                # structured error and stable recovery guidance instead of an
                # uncaught traceback — do not attempt a further remote
                # mutation here (state recover owns finishing this journal).
                raise PolicyError(
                    f"rollback partial failure; run state recover: {exc}"
                ) from exc

            return RollbackResult(
                status="succeeded",
                generation=after.generation,
                after_state_id=after.state_id(),
                restored_paths=tuple(restored),
                deployment_id=manifest.deployment_id,
            )

    def _observe_remote_before_mutation(
        self,
        manifest: DeploymentManifest,
        *,
        force: bool = False,
    ) -> dict[str, bytes | None]:
        """Read each snapshot's remote bytes exactly once and verify after-state.

        P1-03: this single read is the shared ``RemoteObservation`` reused by
        the caller for backup capture immediately afterward — previously the
        after-state drift check and the pre-rollback backup capture were two
        independent reads with a live TOCTOU window between them.

        Args:
            manifest: Eligible latest deployment being rolled back.
            force: When true, allow third-party content and continue.

        Returns:
            Mapping of ``remote_path`` to observed bytes (``None`` when absent),
            for reuse as backup content.

        Raises:
            RemoteDriftError: When remote drifts from after-state and force is false.
        """

        import hashlib

        from .errors import RemoteDriftError

        observed: dict[str, bytes | None] = {}
        drifts: list[str] = []
        checker = getattr(self.transport, "is_executable", None)
        for snapshot in manifest.snapshots:
            actual = self.transport.read_file(snapshot.remote_path)
            observed[snapshot.remote_path] = actual
            if snapshot.after_exists:
                if actual is None:
                    drifts.append(f"{snapshot.path}: missing (expected after content)")
                    continue
                if snapshot.after_sha256 is not None:
                    actual_hash = hashlib.sha256(actual).hexdigest()
                    if actual_hash != snapshot.after_sha256:
                        drifts.append(
                            f"{snapshot.path}: hash drift "
                            f"expected={snapshot.after_sha256[:12]} actual={actual_hash[:12]}"
                        )
                if (
                    callable(checker)
                    and snapshot.after_executable is not None
                ):
                    mode = checker(snapshot.remote_path)
                    if mode is not None and bool(mode) != bool(snapshot.after_executable):
                        drifts.append(f"{snapshot.path}: mode drift")
            else:
                if actual is not None:
                    drifts.append(f"{snapshot.path}: unexpectedly present after delete")

        if drifts and not force:
            detail = "; ".join(drifts)
            raise RemoteDriftError(
                "rollback refused: remote after-state drift "
                f"(use --force to overwrite third-party content): {detail}"
            )
        return observed

    def _verify_rollback_snapshot(self, snapshot: Any) -> None:
        """Read-back verify one restored path against manifest before state.

        Args:
            snapshot: FileSnapshot from the deployment being rolled back.

        Returns:
            None.
        """

        import hashlib

        from .errors import ConfigurationError

        actual = self.transport.read_file(snapshot.remote_path)
        if snapshot.before_exists:
            if actual is None:
                raise ConfigurationError(
                    f"rollback readback failed: {snapshot.path} missing after restore"
                )
            if snapshot.before_sha256 is None:
                raise ConfigurationError(
                    f"rollback readback failed: {snapshot.path} missing before_sha256"
                )
            actual_hash = hashlib.sha256(actual).hexdigest()
            if actual_hash != snapshot.before_sha256:
                raise ConfigurationError(
                    f"rollback readback failed: {snapshot.path} hash mismatch "
                    f"expected={snapshot.before_sha256[:12]} actual={actual_hash[:12]}"
                )
            checker = getattr(self.transport, "is_executable", None)
            if callable(checker) and snapshot.before_executable is not None:
                # Single remote mode probe (avoid double is_executable / TOCTOU).
                mode = checker(snapshot.remote_path)
                if mode is not None and bool(mode) != bool(snapshot.before_executable):
                    raise ConfigurationError(
                        f"rollback readback failed: {snapshot.path} mode mismatch"
                    )
        else:
            if actual is not None:
                raise ConfigurationError(
                    f"rollback readback failed: {snapshot.path} still present after delete"
                )

    def _entries_from_manifest_before(self, manifest: DeploymentManifest) -> tuple[FileEntry, ...]:
        """Derive file entries from manifest before snapshots.

        Args:
            manifest: Deployment manifest.

        Returns:
            File entries describing before remote content.
        """

        entries: list[FileEntry] = []
        for snap in manifest.snapshots:
            entries.append(
                FileEntry(
                    path=snap.path,
                    owner="source",
                    content_sha256=snap.before_sha256,
                    executable=bool(snap.before_executable),
                    exists=snap.before_exists,
                )
            )
        return tuple(entries)
