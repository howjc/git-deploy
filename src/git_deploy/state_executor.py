"""Stateful deploy orchestration: drift, prepare-before-mutate, commit, restore."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .errors import ConfigurationError, PolicyError, RemoteDriftError
from .expected_state import ExpectedState, ExpectedStateStore, FileEntry, build_expected_state
from .models import DeploymentManifest, FileSnapshot, PlannedFile, ProjectConfig
from .object_store import ContentAddressedStore
from .state import DeploymentStore
from .state_guards import StateGuards
from .state_planner import SourceDiffPlan
from .target_identity import TargetIdentity, policy_fingerprint_for_project
from .target_lock import TargetLock
from .transaction import TransactionJournal, TransactionStore



@dataclass
class FakeRemotePath:
    """In-memory remote file for tests.

    Attributes:
        data: File bytes or ``None`` if absent.
        executable: Mode bit when present.
    """

    data: bytes | None = None
    executable: bool = False


@dataclass
class InMemoryTransport:
    """Minimal fake transport used by Gate A state executor tests."""

    files: dict[str, FakeRemotePath] = field(default_factory=dict)
    write_calls: int = 0
    delete_calls: int = 0
    read_calls: int = 0
    connected: bool = True

    def read_file(self, remote_path: str) -> bytes | None:
        """Read one remote path.

        Args:
            remote_path: Absolute remote path.

        Returns:
            Bytes or ``None`` when absent.
        """

        self.read_calls += 1
        item = self.files.get(remote_path)
        return None if item is None else item.data

    def write_file(self, remote_path: str, data: bytes, executable: bool = False) -> None:
        """Write one remote path.

        Args:
            remote_path: Absolute remote path.
            data: Bytes to store.
            executable: Executable bit.

        Returns:
            None.
        """

        self.write_calls += 1
        self.files[remote_path] = FakeRemotePath(data=data, executable=executable)

    def delete_file(self, remote_path: str) -> None:
        """Delete one remote path if present.

        Args:
            remote_path: Absolute remote path.

        Returns:
            None.
        """

        self.delete_calls += 1
        self.files.pop(remote_path, None)

    def exists(self, remote_path: str) -> bool:
        """Return whether a path exists.

        Args:
            remote_path: Absolute remote path.

        Returns:
            Existence flag.
        """

        item = self.files.get(remote_path)
        return item is not None and item.data is not None

    def is_executable(self, remote_path: str) -> bool | None:
        """Return executable bit when known.

        Args:
            remote_path: Absolute remote path.

        Returns:
            Executable flag or ``None``.
        """

        item = self.files.get(remote_path)
        if item is None or item.data is None:
            return None
        return item.executable

    def close(self) -> None:
        """Close the fake transport.

        Returns:
            None.
        """

        self.connected = False

    def run_command(self, command: str) -> None:
        """No-op remote command for hooks (test alias).

        Args:
            command: Command string.

        Returns:
            None.
        """

        return None

    def execute(self, command: str) -> tuple[int, str, str]:
        """Product-compatible shell hook surface used by real SFTP transports.

        Args:
            command: Remote command string.

        Returns:
            ``(exit_code, stdout, stderr)`` with success exit 0.
        """

        self.run_command(command)
        return 0, "", ""


@dataclass(frozen=True)
class DriftDecision:
    """Result of comparing actual remote bytes to current/target expectations.

    Attributes:
        path: Managed path.
        status: ``ok``, ``satisfied``, or ``drift``.
        actual_sha256: Observed hash.
        current_sha256: Expected current hash.
        target_sha256: Desired target hash.
    """

    path: str
    status: str
    actual_sha256: str | None
    current_sha256: str | None
    target_sha256: str | None


class StateDeploymentExecutor:
    """Execute stateful deploys against fake or real transports."""

    def __init__(
        self,
        project: ProjectConfig,
        identity: TargetIdentity,
        target_root: Path,
        *,
        transport: Any | None = None,
        content_provider: Callable[[str], bytes] | None = None,
    ):
        """Bind project, identity, and optional transport.

        Args:
            project: Project configuration.
            identity: Physical target identity.
            target_root: Target state root.
            transport: Injected transport (in-memory tests or real adapter).
            content_provider: Maps path → target bytes for uploads.
        """

        self.project = project
        self.identity = identity
        self.target_root = target_root
        self.transport: Any = transport if transport is not None else InMemoryTransport()
        self.content_provider = content_provider or (lambda path: b"")
        self.state_store = ExpectedStateStore(target_root, identity)
        self.tx_store = TransactionStore(target_root)
        self.cas = ContentAddressedStore(target_root)
        # Target-scoped deployment/backup store: isolate physical targets.
        self.deploy_store = DeploymentStore(project, root=target_root)
        self.policy_fp = policy_fingerprint_for_project(project)

    def evaluate_drift(
        self,
        files: Sequence[PlannedFile],
        *,
        current_entries: Mapping[str, FileEntry] | None = None,
    ) -> list[DriftDecision]:
        """Classify remote actual vs current baseline and target.

        actual=current → ok (may change); actual=target → satisfied; else drift.

        Args:
            files: Planned mutations.
            current_entries: Optional current file map by path.

        Returns:
            Drift decisions per planned path.
        """

        current_map = dict(current_entries or {})
        decisions: list[DriftDecision] = []
        for item in files:
            actual = self.transport.read_file(item.remote_path)
            actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None
            current_hash = item.expected_before_sha256
            if item.path in current_map:
                current_hash = current_map[item.path].content_sha256
            target_hash = item.target_sha256
            if actual_hash == target_hash or (
                actual is None and target_hash is None and item.action == "delete"
            ):
                # Target already present wins even when it also matches current.
                status = "satisfied"
            elif actual_hash == current_hash or (
                actual is None and current_hash is None and item.expected_before_sha256 is None
            ):
                status = "ok"
            else:
                status = "drift"
            decisions.append(
                DriftDecision(
                    path=item.path,
                    status=status,
                    actual_sha256=actual_hash,
                    current_sha256=current_hash,
                    target_sha256=target_hash,
                )
            )
        return decisions

    def reject_third_content(self, decisions: Sequence[DriftDecision], *, force: bool = False) -> None:
        """Block third-content drift unless force allows confirmed content drift.

        Args:
            decisions: Drift decisions.
            force: Allow confirmed remote content drift only.

        Returns:
            None.
        """

        blockers = [item for item in decisions if item.status == "drift"]
        if not blockers:
            return
        if force:
            return
        detail = ", ".join(
            f"{item.path}(actual={item.actual_sha256} current={item.current_sha256} target={item.target_sha256})"
            for item in blockers
        )
        raise RemoteDriftError(f"remote content drift against current snapshot: {detail}")

    def filter_effective(self, files: Sequence[PlannedFile], decisions: Sequence[DriftDecision]) -> list[PlannedFile]:
        """Keep only paths that are not already satisfied at target.

        Args:
            files: Planned files.
            decisions: Drift decisions.

        Returns:
            Effective mutation list.
        """

        status = {item.path: item.status for item in decisions}
        return [item for item in files if status.get(item.path) != "satisfied"]

    def prepare(
        self,
        *,
        plan: SourceDiffPlan,
        files: Sequence[PlannedFile],
        before_state: ExpectedState | None,
        after_state: ExpectedState,
        deployment_id: str,
    ) -> TransactionJournal:
        """Durable-stage after state, backups, and prepared journal before any remote write.

        Args:
            plan: Source plan.
            files: Effective files to mutate.
            before_state: Current state or ``None``.
            after_state: Staged after state.
            deployment_id: Deployment identifier.

        Returns:
            Prepared journal.
        """

        guards = StateGuards(
            self.target_root,
            self.identity,
            expected_policy=self.policy_fp,
        )
        guards.require_clear()

        write_calls_before = getattr(self.transport, "write_calls", 0)
        after_id = self.state_store.write_state(after_state)
        # Persist after-state content refs into CAS so guards/verify can rehash.
        # Unchanged paths must already be in CAS from prior deploys/bootstrap.
        for entry in after_state.files:
            if not entry.exists or not entry.content_sha256:
                continue
            if self.cas.contains(entry.content_sha256):
                self.cas.get(entry.content_sha256)
                continue
            try:
                data = self.content_provider(entry.path)
            except Exception as exc:
                raise ConfigurationError(
                    f"CAS missing and content provider failed for {entry.path}: {exc}"
                ) from exc
            # Legitimate empty files are b""; only None means unavailable when typed so.
            if data is None:  # type: ignore[comparison-overlap]
                raise ConfigurationError(
                    f"CAS missing and content provider returned None for {entry.path}"
                )
            if not isinstance(data, (bytes, bytearray)):
                raise ConfigurationError(
                    f"content provider must return bytes for {entry.path}"
                )
            digest = self.cas.put(bytes(data))
            if digest != entry.content_sha256:
                raise ConfigurationError(
                    f"content digest mismatch for {entry.path}: "
                    f"expected {entry.content_sha256[:12]} got {digest[:12]}"
                )
        backup_refs: list[str] = []
        # Map each effective path to an optional durable backup for restore.
        backup_entries: list[dict[str, Any]] = []
        for index, item in enumerate(files):
            data = self.transport.read_file(item.remote_path)
            entry: dict[str, Any] = {
                "path": item.path,
                "remote_path": item.remote_path,
                "backup_file": None,
                "before_exists": data is not None,
                "executable": bool(item.expected_before_executable),
            }
            if data is not None:
                rel = self.deploy_store.write_backup(deployment_id, index, data)
                backup_refs.append(rel)
                entry["backup_file"] = rel
                self.cas.put(data)
            if item.action != "delete" and item.target_sha256 is not None:
                try:
                    target_data = self.content_provider(item.path)
                except Exception as exc:
                    raise ConfigurationError(
                        f"content provider failed for {item.path}: {exc}"
                    ) from exc
                if target_data is None:  # type: ignore[comparison-overlap]
                    raise ConfigurationError(
                        f"content provider returned None for {item.path}"
                    )
                if not isinstance(target_data, (bytes, bytearray)):
                    raise ConfigurationError(
                        f"content provider must return bytes for {item.path}"
                    )
                digest = self.cas.put(bytes(target_data))
                if digest != item.target_sha256:
                    raise ConfigurationError(
                        f"content digest mismatch for {item.path}: "
                        f"expected {item.target_sha256[:12]} got {digest[:12]}"
                    )
            backup_entries.append(entry)

        before_id = before_state.state_id() if before_state else None
        before_gen = before_state.generation if before_state else None
        journal = self.tx_store.create(
            target_id=self.identity.target_id,
            stage="prepared",
            deployment_id=deployment_id,
            before_state_id=before_id,
            after_state_id=after_id,
            before_generation=before_gen,
            after_generation=after_state.generation,
            backup_refs=backup_refs,
            meta={
                "introduced": list(plan.introduced_transition_ids),
                "paths": [item.path for item in files],
                "backup_entries": backup_entries,
            },
        )
        write_calls_after = getattr(self.transport, "write_calls", 0)
        if write_calls_after != write_calls_before:
            raise ConfigurationError("transport write occurred before prepared journal was durable")
        return journal

    def mutate_remote(
        self,
        journal: TransactionJournal,
        files: Sequence[PlannedFile],
        *,
        fail_after_writes: int | None = None,
        fail_before_hooks: bool = False,
    ) -> TransactionJournal:
        """Enter remote_mutating, apply uploads/deletes, and post-write verify.

        Does **not** advance to ``remote_verified``; callers must complete hooks
        and health first, then call ``mark_remote_verified``.

        Args:
            journal: Prepared journal.
            files: Effective files.
            fail_after_writes: When set, raise after this many successful mutations
                to simulate partial upload/delete for recovery tests.
            fail_before_hooks: When true, raise after mutations+read-back so tests
                can assert the journal is still ``remote_mutating``.

        Returns:
            Journal still at ``remote_mutating`` after successful mutations.
        """

        journal = self.tx_store.advance(journal, "remote_mutating")
        writes_done = 0
        try:
            for item in files:
                if item.action == "delete":
                    self.transport.delete_file(item.remote_path)
                else:
                    data = self.content_provider(item.path)
                    self.transport.write_file(
                        item.remote_path,
                        data,
                        executable=item.executable,
                    )
                # Immediate post-write read-back verify before next mutation.
                self._verify_remote_path(item)
                writes_done += 1
                if fail_after_writes is not None and writes_done >= fail_after_writes:
                    raise RuntimeError(
                        f"injected partial remote failure after {writes_done} mutation(s)"
                    )
            if fail_before_hooks:
                raise RuntimeError("injected crash after mutations before hooks")
            return journal
        except Exception:
            # Stay in remote_mutating so recovery can restore.
            raise

    def _verify_remote_path(self, item: PlannedFile) -> None:
        """Read-back verify one mutated remote path against plan target.

        Args:
            item: Planned mutation just applied.

        Returns:
            None.
        """

        actual = self.transport.read_file(item.remote_path)
        if item.action == "delete":
            if actual is not None:
                raise ConfigurationError(
                    f"post-write verify failed: {item.path} still present after delete"
                )
            return
        if actual is None:
            raise ConfigurationError(
                f"post-write verify failed: {item.path} missing after upload"
            )
        actual_hash = hashlib.sha256(actual).hexdigest()
        if item.target_sha256 and actual_hash != item.target_sha256:
            raise ConfigurationError(
                f"post-write verify failed: {item.path} hash mismatch "
                f"expected={item.target_sha256[:12]} actual={actual_hash[:12]}"
            )
        checker = getattr(self.transport, "is_executable", None)
        if callable(checker) and item.executable:
            mode = checker(item.remote_path)
            if mode is False:
                raise ConfigurationError(
                    f"post-write verify failed: {item.path} executable bit missing"
                )

    def mark_remote_verified(self, journal: TransactionJournal) -> TransactionJournal:
        """Advance journal to remote_verified after hooks and health succeed.

        Args:
            journal: Journal currently in remote_mutating.

        Returns:
            Journal at remote_verified.
        """

        return self.tx_store.advance(journal, "remote_verified")

    def commit_state(
        self,
        journal: TransactionJournal,
        after_state: ExpectedState,
        *,
        before_generation: int | None,
    ) -> tuple[TransactionJournal, ExpectedState]:
        """CAS-advance current after remote_verified.

        Args:
            journal: Verified journal.
            after_state: After state snapshot.
            before_generation: Expected generation for CAS.

        Returns:
            Updated journal and state.
        """

        self.state_store.cas_advance(expected_generation=before_generation, state=after_state)
        journal = self.tx_store.advance(journal, "state_committed")
        journal = self.tx_store.advance(journal, "recovered")
        return journal, after_state

    def restore_from_backups(self, journal: TransactionJournal) -> list[str]:
        """Restore remote bytes solely from durable journal backup entries.

        This is the pure recovery path: for each prepared ``backup_entries`` row,
        either rewrite the remote path from the deployment backup file, or delete
        the path when it did not exist before mutation. Does not advance generation.

        Args:
            journal: Journal carrying ``deployment_id`` and ``backup_entries`` meta.

        Returns:
            List of remote paths restored or deleted.
        """

        if not journal.deployment_id:
            raise ConfigurationError("cannot restore without deployment_id on journal")
        entries = list(journal.meta.get("backup_entries") or [])
        if not entries and journal.backup_refs:
            # Legacy journals without structured entries cannot safely map refs.
            raise ConfigurationError(
                "journal lacks backup_entries mapping; cannot restore from backup_refs alone"
            )
        touched: list[str] = []
        for entry in entries:
            remote_path = str(entry["remote_path"])
            backup_file = entry.get("backup_file")
            if backup_file:
                data = self.deploy_store.read_backup(journal.deployment_id, str(backup_file))
                self.transport.write_file(
                    remote_path,
                    data,
                    executable=bool(entry.get("executable")),
                )
            else:
                # Path was absent before mutation: remove partial upload residue.
                self.transport.delete_file(remote_path)
            touched.append(remote_path)
        return touched

    def auto_restore(
        self,
        journal: TransactionJournal,
        files: Sequence[PlannedFile],
        *,
        before_state: ExpectedState | None,
    ) -> TransactionJournal:
        """Restore remote bytes from durable backups and keep before generation.

        Args:
            journal: Journal during failure.
            files: Files that may need restore (used only when meta is incomplete).
            before_state: Before state to retain as current.

        Returns:
            Journal marked recovered.
        """

        del files  # restore is driven by durable journal backup_entries only
        self.restore_from_backups(journal)

        if before_state is not None:
            # Ensure current remains before (do not CAS forward).
            current = self.state_store.read_current()
            if current is not None and current.generation != before_state.generation:
                raise ConfigurationError("auto restore must keep before generation")
        # From remote_mutating the legal recovery terminal is recovered.
        if journal.stage == "remote_mutating":
            return self.tx_store.advance(journal, "recovered")
        if journal.stage == "prepared":
            return self.tx_store.advance(journal, "recovered")
        if journal.stage not in {"recovered", "manual_recovery_required"}:
            return self.tx_store.advance(journal, "recovered")
        return journal

    def deploy(
        self,
        plan: SourceDiffPlan,
        files: Sequence[PlannedFile],
        *,
        force: bool = False,
        fail_at: str | None = None,
    ) -> dict[str, Any]:
        """Run a full stateful deploy with prepare→mutate→commit.

        Args:
            plan: Source plan.
            files: Planned files (pre-filter).
            force: Allow confirmed content drift.
            fail_at: Test hook: ``upload``, ``delete``, ``hook``, ``health``.

        Returns:
            Result summary dict.
        """

        with TargetLock(self.target_root):
            loaded = self.state_store.load_current_state()
            before_state = loaded[1] if loaded else None
            before_gen = before_state.generation if before_state else None
            decisions = self.evaluate_drift(files)
            self.reject_third_content(decisions, force=force)

            # Repeated no-op: all ok and no introduced transitions with matching current.
            if plan.static_noop or (
                not plan.introduced_transition_ids
                and all(item.status in {"ok", "satisfied"} for item in decisions)
                and not any(item.status == "ok" and item.actual_sha256 != item.target_sha256 for item in decisions)
                and all(item.status == "satisfied" or item.target_sha256 == item.actual_sha256 for item in decisions)
            ):
                if plan.static_noop or all(
                    d.status == "satisfied" or d.actual_sha256 == d.target_sha256 for d in decisions
                ):
                    if any(d.status == "drift" for d in decisions):
                        self.reject_third_content(decisions, force=force)
                    writes = getattr(self.transport, "write_calls", 0)
                    return {
                        "status": "already deployed",
                        "write_calls": writes,
                        "generation": before_gen,
                    }

            effective = self.filter_effective(files, decisions)

            # State-only reconciliation: no mutations needed but transitions/lineage change.
            if not effective and plan.introduced_transition_ids:
                return self._state_only(plan, before_state, before_gen)

            if not effective and not plan.introduced_transition_ids:
                return {
                    "status": "already deployed",
                    "write_calls": getattr(self.transport, "write_calls", 0),
                    "generation": before_gen,
                }

            after_state = self._build_after_state(plan, before_state, files)
            deployment_id = _new_deployment_id()
            journal = self.prepare(
                plan=plan,
                files=effective,
                before_state=before_state,
                after_state=after_state,
                deployment_id=deployment_id,
            )

            try:
                # Crash windows: upload → read-back → hooks → health → remote_verified.
                fail_after = 1 if fail_at in {"upload", "delete"} else None
                journal = self.mutate_remote(
                    journal,
                    effective,
                    fail_after_writes=fail_after,
                    fail_before_hooks=(fail_at == "before_hooks"),
                )
                # Stay remote_mutating until hooks and health fully succeed.
                self._run_post_hooks_and_health(fail_at=fail_at)
                journal = self.mark_remote_verified(journal)
                journal, after_state = self.commit_state(
                    journal,
                    after_state,
                    before_generation=before_gen,
                )
                self._write_manifest(
                    deployment_id,
                    journal,
                    before_state,
                    after_state,
                    effective,
                    status="succeeded",
                )
                return {
                    "status": "succeeded",
                    "generation": after_state.generation,
                    "transaction_id": journal.transaction_id,
                    "deployment_id": deployment_id,
                }
            except Exception as exc:
                try:
                    journal = self.auto_restore(journal, effective, before_state=before_state)
                    self._write_manifest(
                        deployment_id,
                        journal,
                        before_state,
                        before_state,
                        effective,
                        status="failed",
                        error=str(exc),
                    )
                    # restored=True still means the deploy failed for CLI exit mapping.
                    return {
                        "status": "restored",
                        "restored": True,
                        "ok": False,
                        "generation": before_gen,
                        "error": str(exc),
                        "transaction_id": journal.transaction_id,
                        "deployment_id": deployment_id,
                    }
                except Exception as restore_exc:
                    journal = self.tx_store.advance(
                        journal,
                        "manual_recovery_required",
                        error=f"{exc}; restore failed: {restore_exc}",
                    )
                    raise PolicyError(
                        "manual_recovery_required: automatic restore failed; deploy blocked"
                    ) from restore_exc

    def _state_only(
        self,
        plan: SourceDiffPlan,
        before_state: ExpectedState | None,
        before_gen: int | None,
    ) -> dict[str, Any]:
        """Advance generation without remote mutation (reconciliation).

        Args:
            plan: Source plan with introduced transitions.
            before_state: Current state.
            before_gen: Current generation.

        Returns:
            Result summary.
        """

        if self.tx_store.list_open():
            raise PolicyError("unfinished transaction blocks state-only reconciliation")
        after_state = self._build_after_state(plan, before_state, ())
        writes_before = getattr(self.transport, "write_calls", 0)
        # Non-terminal until CAS succeeds: prepared → write after_id → CAS → recovered.
        journal = self.tx_store.create(
            target_id=self.identity.target_id,
            stage="prepared",
            before_state_id=before_state.state_id() if before_state else None,
            after_state_id=None,
            before_generation=before_gen,
            after_generation=after_state.generation,
            meta={"kind": "state_only", "introduced": list(plan.introduced_transition_ids)},
        )
        after_id = self.state_store.write_state(after_state)
        # Same-stage patch so crash-before-CAS still records after_state_id for recover.
        journal = self.tx_store.advance(journal, "prepared", after_state_id=after_id)
        self.state_store.cas_advance(expected_generation=before_gen, state=after_state)
        # CAS already advanced current; terminal journal is separate. Crash here
        # leaves open prepared journal with after_id for recover to finalize once.
        fail_post_cas = getattr(self, "_fail_after_state_only_cas", False)
        if fail_post_cas:
            raise ConfigurationError("injected state-only crash after CAS before terminal journal")
        journal = self.tx_store.advance(journal, "recovered", after_state_id=after_id)
        writes_after = getattr(self.transport, "write_calls", 0)
        if writes_after != writes_before:
            raise ConfigurationError("state-only transition must not write remote")
        return {
            "status": "reconciled",
            "generation": after_state.generation,
            "write_calls": 0,
            "transaction_id": journal.transaction_id,
        }

    def _build_after_state(
        self,
        plan: SourceDiffPlan,
        before_state: ExpectedState | None,
        files: Sequence[PlannedFile],
    ) -> ExpectedState:
        """Construct the full after expected state from before + planned mutations.

        Retains unchanged managed paths from ``before_state``, then applies each
        planned upload/delete/mode change. State-only transitions pass ``files=()``
        and keep the full before file table.

        Args:
            plan: Source plan.
            before_state: Previous state.
            files: Full planned file set for this deploy (not only effective).

        Returns:
            After state with generation+1.
        """

        generation = 1 if before_state is None else before_state.generation + 1
        parent = before_state.state_id() if before_state else None
        by_path: dict[str, FileEntry] = {}
        if before_state is not None:
            for entry in before_state.files:
                by_path[entry.path] = entry
        for item in files:
            if item.action == "delete":
                by_path.pop(item.path, None)
                continue
            by_path[item.path] = FileEntry(
                path=item.path,
                owner="source",
                content_sha256=item.target_sha256,
                executable=item.executable,
                exists=True,
            )
        entries = tuple(by_path[path] for path in sorted(by_path))
        return build_expected_state(
            generation=generation,
            parent_state_id=parent,
            source_tree_id=plan.after_tree_id,
            applied_transition_ids=plan.applied_transition_ids,
            physical_fingerprint=self.identity.physical_fingerprint,
            policy_fingerprint=self.policy_fp,
            files=entries,
        )

    def _write_manifest(
        self,
        deployment_id: str,
        journal: TransactionJournal,
        before_state: ExpectedState | None,
        after_state: ExpectedState | None,
        files: Sequence[PlannedFile],
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        """Persist a deployment manifest with v0.2 lineage and durable backup refs.

        Args:
            deployment_id: Deployment id.
            journal: Transaction journal (supplies backup_entries for restore).
            before_state: Before state.
            after_state: After state.
            files: Touched files.
            status: Manifest status.
            error: Optional error.

        Returns:
            None.
        """

        from datetime import UTC, datetime

        backup_by_path: dict[str, dict[str, Any]] = {}
        for entry in journal.meta.get("backup_entries") or []:
            if isinstance(entry, dict) and entry.get("path") is not None:
                backup_by_path[str(entry["path"])] = entry

        snapshots = []
        for item in files:
            backup_meta = backup_by_path.get(item.path, {})
            before_exists = bool(backup_meta.get("before_exists", item.expected_before_sha256 is not None))
            snapshots.append(
                FileSnapshot(
                    path=item.path,
                    remote_path=item.remote_path,
                    before_exists=before_exists,
                    before_sha256=item.expected_before_sha256,
                    backup_file=backup_meta.get("backup_file"),
                    after_exists=item.action != "delete",
                    after_sha256=item.target_sha256,
                    before_executable=(
                        bool(backup_meta["executable"])
                        if "executable" in backup_meta
                        else item.expected_before_executable
                    ),
                    after_executable=item.executable if item.action != "delete" else None,
                )
            )
        manifest = DeploymentManifest(
            deployment_id=deployment_id,
            project=self.project.name,
            repository=str(self.project.repository),
            remote_root=self.project.remote_root,
            from_commit="",
            to_commit="",
            created_at=datetime.now(UTC).isoformat(),
            status=status,
            snapshots=snapshots,
            error=error,
            remote=self.project.remote,
            before_state_id=before_state.state_id() if before_state else None,
            after_state_id=after_state.state_id() if after_state else None,
            before_generation=before_state.generation if before_state else None,
            after_generation=after_state.generation if after_state else None,
            introduced_transition_ids=list(journal.meta.get("introduced", [])),
            physical_fingerprint=self.identity.physical_fingerprint,
            policy_fingerprint=self.policy_fp,
            transaction_id=journal.transaction_id,
            target_id=self.identity.target_id,
            state="v1",
        )
        self.deploy_store.write_manifest(manifest)

    def _run_post_hooks_and_health(self, *, fail_at: str | None = None) -> None:
        """Execute configured post commands and health checks after remote mutation.

        Uses the same product transport surface as legacy deploy: ``execute``
        (SftpTransport / FtpTransport), not a test-only ``run_command`` alias.

        Args:
            fail_at: Test-only injection point ``hook`` or ``health``.

        Returns:
            None.
        """

        from .errors import GitDeployError

        if fail_at == "hook":
            raise RuntimeError("hook failed")
        if self.project.post_commands:
            supports = getattr(self.transport, "supports_commands", True)
            if supports is False:
                raise PolicyError("post_commands require SFTP/SSH and cannot run over FTP/FTPS")
            executor = getattr(self.transport, "execute", None)
            if not callable(executor):
                # Fallback for minimal fakes that only implement run_command.
                runner = getattr(self.transport, "run_command", None)
                if not callable(runner):
                    raise PolicyError(
                        "post_commands require a transport that supports execute()"
                    )
                for command in self.project.post_commands:
                    runner(command)
            else:
                for command in self.project.post_commands:
                    code, stdout, stderr = executor(command)
                    if int(code) != 0:
                        detail = (stderr or stdout or f"exit code {code}").strip()
                        raise GitDeployError(f"post command failed: {command}: {detail}")
        if fail_at == "health":
            raise RuntimeError("health failed")
        if self.project.health_urls:
            from .executor import _check_health_url

            for url in self.project.health_urls:
                _check_health_url(url)


def _new_deployment_id() -> str:
    """Allocate a deployment identifier.

    Returns:
        Time-based deployment id.
    """

    import secrets
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"
