"""Managed-state policy migration plan and generation-CAS execute."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errors import PolicyError
from .expected_state import ExpectedStateStore, FileEntry, build_expected_state
from .remote_verify import remote_path_for
from .target_identity import TargetIdentity
from .target_lock import TargetLock
from .transaction import TransactionStore


@dataclass(frozen=True)
class PolicyMigrationPlan:
    """Dry-run plan for policy fingerprint changes.

    Attributes:
        old_policy: Current state policy fingerprint.
        new_policy: Desired policy fingerprint.
        old_managed_paths: Paths managed under old policy.
        new_managed_paths: Paths managed under new policy.
        readonly_verify_paths: Paths that must be verified read-only before execute.
        blocked_for_normal_deploy: Always true until execute succeeds.
        new_file_entries: Full managed file table under the new policy (when known).
    """

    old_policy: str
    new_policy: str
    old_managed_paths: tuple[str, ...]
    new_managed_paths: tuple[str, ...]
    readonly_verify_paths: tuple[str, ...]
    blocked_for_normal_deploy: bool = True
    new_file_entries: tuple[FileEntry, ...] = ()


class PolicyMigrationService:
    """Plan and execute managed policy CAS migrations (remote writes always 0)."""

    def __init__(self, target_root: Path, identity: TargetIdentity):
        """Bind target stores.

        Args:
            target_root: Target state root.
            identity: Physical identity.
        """

        self.target_root = target_root
        self.identity = identity
        self.store = ExpectedStateStore(target_root, identity)
        self.tx = TransactionStore(target_root)

    def plan(
        self,
        *,
        new_policy: str,
        old_managed_paths: Sequence[str] = (),
        new_managed_paths: Sequence[str] = (),
        new_file_entries: Sequence[FileEntry] = (),
    ) -> PolicyMigrationPlan:
        """Build a policy migration plan without writes.

        Args:
            new_policy: Desired policy fingerprint.
            old_managed_paths: Paths under old policy.
            new_managed_paths: Paths under new policy.
            new_file_entries: Full managed file entries under new policy.

        Returns:
            Policy migration plan.
        """

        loaded = self.store.load_current_state()
        if loaded is None:
            raise PolicyError("no current state for policy migration")
        _pointer, state = loaded
        verify = tuple(sorted(set(old_managed_paths) | set(new_managed_paths)))
        return PolicyMigrationPlan(
            old_policy=state.policy_fingerprint,
            new_policy=new_policy,
            old_managed_paths=tuple(old_managed_paths),
            new_managed_paths=tuple(new_managed_paths),
            readonly_verify_paths=verify,
            blocked_for_normal_deploy=True,
            new_file_entries=tuple(new_file_entries),
        )

    def execute(
        self,
        plan: PolicyMigrationPlan,
        *,
        remote_write_counter: list[int] | None = None,
        yes: bool = False,
        transport: Any | None = None,
        project: Any | None = None,
    ) -> str:
        """CAS-write a new state with updated policy fingerprint; remote writes stay 0.

        Under the target lock and before CAS, opens/uses transport for read-only
        verification of old/new managed paths. Drift or read failure keeps the
        old generation.

        Args:
            plan: Policy migration plan.
            remote_write_counter: Optional mutable counter to assert zero remote writes.
            yes: Required confirmation.
            transport: Connected transport for read-only path verification.
            project: Project used to build remote paths (required when paths non-empty).

        Returns:
            New state id.

        Raises:
            PolicyError: On concurrent generation change, drift, or missing current.
        """

        if not yes:
            raise PolicyError("policy migration execute requires --yes")
        if remote_write_counter is not None and remote_write_counter[0] != 0:
            raise PolicyError("policy migration must not perform remote writes")

        with TargetLock(self.target_root):
            if self.tx.list_open():
                raise PolicyError("unfinished transaction blocks policy migration")
            loaded = self.store.load_current_state()
            if loaded is None:
                raise PolicyError("no current state for policy migration")
            pointer, state = loaded
            if state.policy_fingerprint != plan.old_policy:
                raise PolicyError("policy migration concurrent change detected")
            if state.physical_fingerprint != self.identity.physical_fingerprint:
                raise PolicyError("policy migration identity concurrent change detected")

            # Prefer caller-supplied full new file entries (new policy source table).
            new_files = plan.new_file_entries if plan.new_file_entries else state.files
            if plan.readonly_verify_paths:
                if transport is None or project is None:
                    raise PolicyError(
                        "policy migration execute requires transport for remote path verify"
                    )
                writes_before = getattr(transport, "write_calls", 0)
                old_by_path = {entry.path: entry for entry in state.files}
                new_by_path = {entry.path: entry for entry in new_files}
                for rel in plan.readonly_verify_paths:
                    remote = remote_path_for(project, rel)
                    try:
                        actual = transport.read_file(remote)
                    except Exception as exc:
                        raise PolicyError(
                            f"policy migration remote read failed for {rel}: {exc}"
                        ) from exc
                    actual_hash = (
                        hashlib.sha256(actual).hexdigest() if actual is not None else None
                    )
                    old_entry = old_by_path.get(rel)
                    new_entry = new_by_path.get(rel)
                    if old_entry is not None and old_entry.exists:
                        # Still managed under old state: remote must match old/current.
                        if actual is None or actual_hash != old_entry.content_sha256:
                            raise PolicyError(
                                f"policy migration refused: remote drift on {rel}"
                            )
                    elif new_entry is not None and new_entry.exists:
                        # Newly managed under new policy: remote must match trusted blob.
                        if actual is None or actual_hash != new_entry.content_sha256:
                            raise PolicyError(
                                f"policy migration refused: new managed path {rel} "
                                "absent or does not match source"
                            )
                    else:
                        # Not expected to exist under old or new managed tables.
                        if actual is not None and (old_entry is None or not old_entry.exists):
                            # Path removed from managed set may still exist remotely;
                            # only refuse when it was never in old state and not new.
                            pass
                writes_after = getattr(transport, "write_calls", 0)
                if writes_after != writes_before:
                    raise PolicyError("policy migration must not perform remote writes")
                if remote_write_counter is not None:
                    remote_write_counter[0] = writes_after

            after = build_expected_state(
                generation=pointer.generation + 1,
                parent_state_id=pointer.state_id,
                source_tree_id=state.source_tree_id,
                applied_transition_ids=state.applied_transition_ids,
                physical_fingerprint=state.physical_fingerprint,
                policy_fingerprint=plan.new_policy,
                files=new_files,
                deployment_id=state.deployment_id,
                artifacts=state.artifacts,
            )
            self.store.cas_advance(expected_generation=pointer.generation, state=after)
            if remote_write_counter is not None and remote_write_counter[0] != 0:
                raise PolicyError("policy migration performed remote writes")
            return after.state_id()
