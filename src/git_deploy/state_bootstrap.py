"""First-time state bootstrap and inferred baseline planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Protocol, Sequence

from .errors import ConfigurationError, PolicyError
from .expected_state import ExpectedState, ExpectedStateStore, FileEntry, build_expected_state
from .gitrepo import GitRepository
from .models import ProjectConfig
from .remote_verify import remote_path_for
from .state_composer import StateComposer
from .target_identity import TargetIdentity, policy_fingerprint_for_project
from .target_lock import TargetLock
from .transaction import TransactionStore


class ReadableRemote(Protocol):
    """Minimal remote read surface for bootstrap verification."""

    def read_file(self, remote_path: str) -> bytes | None:
        """Return remote bytes or ``None`` when absent."""

        ...


@dataclass(frozen=True)
class BootstrapPlan:
    """Local plan describing how current state would be established.

    Attributes:
        mode: ``revision`` or ``empty``.
        revision: Resolved commit when mode is revision.
        generation: Always 1 for first current.
        applied_transition_ids: First-parent ancestry marked applied for revision mode.
        source_tree_id: Tree id that will become current.
        dry_run: Whether the plan was requested without writes.
        managed_paths: Relative paths that must be read-verified before write.
    """

    mode: str
    revision: str | None
    generation: int
    applied_transition_ids: tuple[str, ...]
    source_tree_id: str
    dry_run: bool
    managed_paths: tuple[str, ...] = ()


class StateBootstrapService:
    """Infer baselines and explicitly bootstrap generation 1 current state."""

    def __init__(
        self,
        project: ProjectConfig,
        identity: TargetIdentity,
        target_root: Path,
    ):
        """Bind project and target stores.

        Args:
            project: Project configuration.
            identity: Physical target identity.
            target_root: Target state root.
        """

        self.project = project
        self.identity = identity
        self.target_root = target_root
        self.repo = GitRepository(project.repository)
        self.composer = StateComposer(self.repo)
        self.store = ExpectedStateStore(target_root, identity)
        self.transactions = TransactionStore(target_root)

    def plan_inferred(
        self,
        revision: str,
        *,
        dry_run: bool = True,
        managed_paths: Sequence[str] | None = None,
    ) -> BootstrapPlan:
        """Plan an inferred baseline from a known Git revision.

        Marks first-parent ancestry as applied so subsequent range selections
        remain idempotent. Does not write generation when dry_run/failed.

        Args:
            revision: Known Git revision.
            dry_run: When true, no state is written.
            managed_paths: Paths to read-verify; default lists tree files.

        Returns:
            Bootstrap plan.
        """

        commit = self.repo.resolve_commit(revision)
        chain = self.repo.first_parent_chain(commit)
        applied = tuple(self.composer.transition_id_for_commit(c).as_str() for c in reversed(chain))
        tree = self.repo._run_text("rev-parse", f"{commit}^{{tree}}").strip()
        if managed_paths is None:
            listed = self.repo._run_text("ls-tree", "-r", "--name-only", tree).splitlines()
            managed_paths = tuple(path for path in listed if path)
        return BootstrapPlan(
            mode="revision",
            revision=commit,
            generation=1,
            applied_transition_ids=applied,
            source_tree_id=tree,
            dry_run=dry_run,
            managed_paths=tuple(managed_paths),
        )

    def derive_empty_managed_paths(self) -> tuple[str, ...]:
        """Derive the full managed destination set that must be absent for empty bootstrap.

        Concrete include patterns are used when present; otherwise HEAD tracked files
        matching include/exclude/protected policy are treated as managed destinations
        that must not exist on the remote before an empty baseline is written.

        Returns:
            Sorted unique relative paths to verify absent.
        """

        from fnmatch import fnmatch

        concrete = [
            pattern.lstrip("./")
            for pattern in self.project.include
            if pattern and not any(char in pattern for char in "*?[")
        ]
        if concrete:
            return tuple(sorted(set(concrete)))
        try:
            head = self.repo._run_text("rev-parse", "HEAD").strip()
            listed = self.repo._run_text("ls-tree", "-r", "--name-only", head).splitlines()
        except Exception:
            listed = []
        paths: list[str] = []
        include = self.project.include or ("**",)
        for path in listed:
            if not path:
                continue
            if any(fnmatch(path, pattern) for pattern in self.project.protected):
                continue
            if any(fnmatch(path, pattern) for pattern in self.project.exclude):
                continue
            if include == ("**",) or not include:
                paths.append(path)
                continue
            if any(fnmatch(path, pattern) for pattern in include):
                paths.append(path)
        return tuple(sorted(set(paths)))

    def plan_empty(
        self,
        *,
        dry_run: bool = True,
        managed_paths: Sequence[str] | None = None,
    ) -> BootstrapPlan:
        """Plan an empty baseline requiring verified-absent managed paths.

        Args:
            dry_run: When true, no state is written.
            managed_paths: Paths that must be absent on the remote before write.
                When ``None``, derived from source/artifact policy.

        Returns:
            Bootstrap plan for empty tree.

        Raises:
            PolicyError: When the managed set is empty but policy cannot prove
                there are no managed targets (wildcard includes with no inventory).
        """

        from .errors import PolicyError

        if managed_paths is None:
            managed_paths = self.derive_empty_managed_paths()
        managed_tuple = tuple(managed_paths)
        if not managed_tuple:
            has_wildcard = any(
                any(char in pattern for char in "*?[") for pattern in (self.project.include or ())
            )
            # Empty set is only safe when we can prove no managed targets exist.
            # Wildcard-only include with no HEAD inventory is not proof.
            if has_wildcard and (self.project.include or ("**",)) != ():
                # Allow only when repository truly has zero tracked files.
                try:
                    head = self.repo._run_text("rev-parse", "HEAD").strip()
                    listed = [
                        line
                        for line in self.repo._run_text("ls-tree", "-r", "--name-only", head).splitlines()
                        if line
                    ]
                except Exception:
                    listed = ["?"]
                if listed:
                    raise PolicyError(
                        "empty bootstrap refused: managed path set is empty but "
                        "source policy still manages tracked files; cannot treat "
                        "empty verification loop as success"
                    )
        tree = self.repo.empty_tree()
        return BootstrapPlan(
            mode="empty",
            revision=None,
            generation=1,
            applied_transition_ids=(),
            source_tree_id=tree,
            dry_run=dry_run,
            managed_paths=managed_tuple,
        )

    def verify_remote_for_plan(
        self,
        plan: BootstrapPlan,
        transport: ReadableRemote,
        *,
        write_counter: list[int] | None = None,
    ) -> tuple[FileEntry, ...]:
        """Read-only verify managed remote paths before any generation write.

        - ``revision`` mode: each managed path must match the Git blob at the
          planned revision (or be absent only when the tree lacks it).
        - ``empty`` mode: every managed path must be absent.

        Args:
            plan: Bootstrap plan with managed paths.
            transport: Read-only remote transport.
            write_counter: Optional ``[write_calls]`` snapshot; must not change.

        Returns:
            File entries describing verified remote content for the new state.

        Raises:
            PolicyError: On mismatch, unexpected presence (empty), or unknown adopt.
        """

        writes_before = write_counter[0] if write_counter is not None else None
        entries: list[FileEntry] = []
        if plan.mode == "empty":
            for path in plan.managed_paths:
                remote_path = remote_path_for(self.project, path)
                actual = transport.read_file(remote_path)
                if actual is not None:
                    raise PolicyError(
                        f"empty bootstrap refused: remote path already exists: {path}"
                    )
                entries.append(
                    FileEntry(
                        path=path,
                        owner="source",
                        content_sha256=None,
                        exists=False,
                    )
                )
        elif plan.mode == "revision":
            if plan.revision is None:
                raise ConfigurationError("revision bootstrap plan missing revision")
            for path in plan.managed_paths:
                remote_path = remote_path_for(self.project, path)
                actual = transport.read_file(remote_path)
                blob = self.repo.blob(plan.revision, path)
                if blob is None:
                    if actual is not None:
                        raise PolicyError(
                            f"revision bootstrap refused: remote has unexpected path {path}"
                        )
                    entries.append(
                        FileEntry(path=path, owner="source", content_sha256=None, exists=False)
                    )
                    continue
                expected = blob.sha256
                actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None
                if actual is None:
                    raise PolicyError(
                        f"revision bootstrap refused: remote missing expected path {path}"
                    )
                if actual_hash != expected:
                    raise PolicyError(
                        f"revision bootstrap refused: remote drift on {path} "
                        f"(expected={expected[:12]} actual={actual_hash[:12] if actual_hash else None})"
                    )
                entries.append(
                    FileEntry(
                        path=path,
                        owner="source",
                        content_sha256=expected,
                        executable=blob.executable,
                        exists=True,
                    )
                )
        else:
            raise PolicyError(f"unsupported bootstrap mode for remote verify: {plan.mode}")

        if write_counter is not None and write_counter[0] != writes_before:
            raise PolicyError("bootstrap remote verify must not write remote")
        return tuple(entries)

    def execute(
        self,
        plan: BootstrapPlan,
        *,
        files: Sequence[FileEntry] = (),
        yes: bool = False,
        transport: ReadableRemote | None = None,
        write_counter: list[int] | None = None,
        skip_remote_verify: bool = False,
        precommit_validator: Callable[[BootstrapPlan], None] | None = None,
    ) -> ExpectedState:
        """Write generation 1 current after required remote verification.

        Remote read-only verification may run outside the lock. Under the same
        target lock the service reconfirms no current, runs optional final Git
        store/tree precommit validation, then CAS-advances generation 1. There
        is intentionally no post-CAS integrity step that could fail after a
        visible current is published.

        Args:
            plan: Bootstrap plan (revision or empty).
            files: Optional precomputed entries; when empty and transport is set,
                entries are built from read-only remote verification.
            yes: Required to actually write (CLI ``--yes``).
            transport: Remote transport for mandatory path verification.
            write_counter: Optional write counter that must stay unchanged during verify.
            skip_remote_verify: Only for pure local unit fixtures; production paths
                must pass transport.
            precommit_validator: Optional callable run under the target lock
                immediately before CAS (e.g. final ``require_tree``). Failures
                must leave no generation-1 current.

        Returns:
            Written expected state.

        Raises:
            PolicyError: When current already exists, adopt unknown, or remote fails verify.
            ConfigurationError: When dry_run plan is executed without yes.
        """

        if plan.mode not in {"revision", "empty"}:
            raise PolicyError(f"unsupported bootstrap mode: {plan.mode}")
        if plan.mode in {"adopt", "unknown"}:
            raise PolicyError("unknown remote adopt is not supported")
        if plan.dry_run and not yes:
            raise ConfigurationError("bootstrap dry-run does not write current state")
        if not yes:
            raise ConfigurationError("bootstrap requires --yes to write current state")
        if self.store.read_current() is not None:
            raise PolicyError("current state already exists; bootstrap refused")

        verified_files: tuple[FileEntry, ...]
        if files:
            verified_files = tuple(files)
            if transport is not None and not skip_remote_verify:
                # Still enforce remote checks even when entries are supplied.
                verified_files = self.verify_remote_for_plan(
                    plan, transport, write_counter=write_counter
                )
        elif transport is not None and not skip_remote_verify:
            verified_files = self.verify_remote_for_plan(
                plan, transport, write_counter=write_counter
            )
        elif plan.mode == "empty" and not plan.managed_paths and skip_remote_verify:
            verified_files = ()
        elif plan.mode == "revision" and skip_remote_verify:
            verified_files = tuple(files)
        else:
            raise PolicyError(
                "bootstrap requires read-only remote verification of managed paths "
                "before writing generation 1 (unknown remote adopt is not supported)"
            )

        policy = policy_fingerprint_for_project(self.project)
        state = build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=plan.source_tree_id,
            applied_transition_ids=plan.applied_transition_ids,
            physical_fingerprint=self.identity.physical_fingerprint,
            policy_fingerprint=policy,
            files=verified_files,
        )
        # Same lock: no-current recheck → final Git precommit → durable journal
        # → CAS gen1 → terminal. The journal makes an after-replace failure
        # recoverable even when current.json is already visible.
        with TargetLock(self.target_root):
            if self.store.read_current() is not None:
                raise PolicyError("current state already exists; bootstrap refused")
            if self.transactions.list_open():
                raise PolicyError(
                    "unfinished transaction blocks bootstrap; run state recover"
                )
            if precommit_validator is not None:
                precommit_validator(plan)
            after_state_id = self.store.write_state(state)
            journal = self.transactions.create(
                target_id=self.identity.target_id,
                stage="prepared",
                before_state_id=None,
                after_state_id=after_state_id,
                before_generation=None,
                after_generation=1,
                meta={"kind": "bootstrap", "mode": plan.mode},
            )
            self.store.cas_advance(expected_generation=None, state=state)
            self.transactions.advance(journal, "recovered")
        return state

    def refuse_unknown_adopt(self) -> None:
        """Explicitly refuse unknown remote adopt.

        Returns:
            None.
        """

        raise PolicyError("unknown remote adopt is not supported")
