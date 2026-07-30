"""Merge committed source and output-manifest differences into one safe plan."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

from git_deploy.config import (
    Config,
    OutputConfig,
    TargetConfig,
    is_protected,
    is_source_managed,
    resolve_target_for_plan,
)
from git_deploy.errors import PlanError, StaleRemotePlanError
from git_deploy.ftp_hybrid import (
    FTPHybridCapabilities,
    FTPHybridPending,
    FTPPendingPhase,
    FTPRemoteTree,
    load_capability_profile,
    pending_local_manifest_hash,
    read_pending,
    scan_ftp_tree,
    validate_pending_resume,
    validate_remote_root_aliases,
)
from git_deploy.git import GitEntry, GitRepository
from git_deploy.hybrid import (
    HybridLocalManifest,
    HybridBackend,
    HybridOwnership,
    HybridRecoveryOutcome,
    HybridRecoveryRecord,
    RecoveryPhase,
    hybrid_content_manifest,
    inspect_recovery,
    make_ownership,
    ownership_hash,
    read_ownership_snapshot,
    read_recovery_records,
    recovery_command_hash,
    resolve_hybrid_backend,
    scan_hybrid_output,
    validate_internal_paths,
)
from git_deploy.manifest import (
    ManifestEntry,
    ScannedOutput,
    TargetState,
    scan_outputs,
    target_state_hash,
)
from git_deploy.transports.base import RemotePathType, Transport
from git_deploy.transports.ftp import FTPTransport

Origin = Literal["source", "output"]


@dataclass(frozen=True, slots=True)
class UploadOperation:
    """Describe one committed source blob or local output to upload."""

    remote_path: str
    origin: Origin
    local_path: Path | None = None
    git_path: str | None = None
    size: int | None = None
    executable: bool = False
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteOperation:
    """Describe one previously owned remote path to delete."""

    remote_path: str
    origin: Origin


Operation = UploadOperation | DeleteOperation


@dataclass(frozen=True, slots=True)
class HybridRootFileUpload:
    """Upload one current Hybrid direct file through safe temporary publish."""

    path: str
    local_path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class HybridRootFileDelete:
    """Remove one historically owned Hybrid direct file through Backup."""

    path: str


@dataclass(frozen=True, slots=True)
class HybridDirectoryMirror:
    """Stage and swap one complete current Hybrid Mirror Directory."""

    name: str
    file_count: int
    total_size: int
    adopt: bool = False


@dataclass(frozen=True, slots=True)
class HybridDirectoryDelete:
    """Move one historically owned removed directory into Backup."""

    name: str


@dataclass(frozen=True, slots=True)
class HybridAdoption:
    """Display explicit ownership adoption for one existing direct child."""

    path: str


@dataclass(frozen=True, slots=True)
class HybridOwnershipUpdate:
    """Commit the reviewed next Remote Ownership Manifest atomically."""

    mapping: str


@dataclass(frozen=True, slots=True)
class FTPHybridFileUpload:
    """Publish one current Root or Mirror file through the FTP Stage."""

    path: str
    local_path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class FTPHybridPlan:
    """Contain the exact FTP in-place publish, prune, and resume work."""

    capabilities: FTPHybridCapabilities
    pending: FTPHybridPending | None
    next_ownership: HybridOwnership
    uploads: tuple[FTPHybridFileUpload, ...]
    create_directories: tuple[str, ...]
    delete_files: tuple[str, ...]
    remove_directories: tuple[str, ...]
    adoptions: tuple[str, ...]
    remote_trees: tuple[FTPRemoteTree, ...]
    ownership_update: bool
    resume_phase: FTPPendingPhase | None = None


HybridOperation = (
    HybridRootFileUpload
    | HybridRootFileDelete
    | HybridDirectoryMirror
    | HybridDirectoryDelete
    | HybridAdoption
    | HybridOwnershipUpdate
)


@dataclass(frozen=True, slots=True)
class HybridPlan:
    """Bind the local aggregation view to optional remote ownership facts."""

    local: HybridLocalManifest
    previous_outputs: dict[str, ManifestEntry]
    backend: HybridBackend = HybridBackend.SFTP_STAGED
    ftp: FTPHybridPlan | None = None
    ownership: HybridOwnership | None = None
    operations: tuple[HybridOperation, ...] = ()
    expected_ownership_hash: str | None = None
    expected_path_types: tuple[tuple[str, RemotePathType], ...] = ()
    recovery_records: tuple[HybridRecoveryRecord, ...] = ()
    remote_complete: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """Contain only remote facts required for one explicit Hybrid recovery."""

    target: TargetConfig
    target_fingerprint: str
    project_id: str
    mapping: str
    remote: str
    ownership: HybridOwnership | None
    expected_ownership_hash: str
    record: HybridRecoveryRecord
    outcome: HybridRecoveryOutcome


@dataclass(frozen=True, slots=True)
class FTPRecoveryPlan:
    """Contain only frozen post-commit FTP Pending facts required by ``--recover``."""

    target: TargetConfig
    target_fingerprint: str
    project_id: str
    mapping: str
    remote: str
    expected_ownership_hash: str
    pending: FTPHybridPending

    @property
    def record(self) -> FTPHybridPending:
        """Return the Pending marker through the shared recovery-plan interface."""

        return self.pending

    @property
    def outcome(self) -> HybridRecoveryOutcome:
        """Describe frozen-State and cleanup work for generic CLI summaries."""

        return HybridRecoveryOutcome(
            ownership_committed=True,
            commands_pending=False,
            state_pending=self.pending.phase is FTPPendingPhase.OWNERSHIP_COMMITTED,
            cleanup_pending=True,
        )


ExplicitRecoveryPlan = RecoveryPlan | FTPRecoveryPlan


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """Contain deterministic operations and the next complete local state inputs."""

    target: TargetConfig
    target_fingerprint: str
    head: str
    previous_commit: str | None
    operations: tuple[Operation, ...]
    output_manifest: dict[str, ManifestEntry]
    full: bool
    allow_adoption: bool = False
    hybrid: HybridPlan | None = None
    non_hybrid_owned: tuple[str, ...] = ()
    previous_state_hash: str = ""
    non_hybrid_plan_hash: str = ""
    ftp_root_namespace: tuple[tuple[str, str], ...] = ()

    @property
    def upload_count(self) -> int:
        """Return the number of uploads in the operation queue."""

        regular = sum(isinstance(item, UploadOperation) for item in self.operations)
        if self.hybrid is None:
            return regular
        hybrid_files = sum(
            isinstance(item, HybridRootFileUpload) for item in self.hybrid.operations
        )
        mirrored_files = sum(
            item.file_count
            for item in self.hybrid.operations
            if isinstance(item, HybridDirectoryMirror)
        )
        if self.hybrid.ftp is not None:
            return regular + len(self.hybrid.ftp.uploads)
        return regular + hybrid_files + mirrored_files

    @property
    def delete_count(self) -> int:
        """Return the number of deletes in the operation queue."""

        regular = sum(isinstance(item, DeleteOperation) for item in self.operations)
        if self.hybrid is None:
            return regular
        if self.hybrid.ftp is not None:
            return regular + len(self.hybrid.ftp.delete_files) + len(
                self.hybrid.ftp.remove_directories
            )
        return regular + sum(
            isinstance(item, (HybridRootFileDelete, HybridDirectoryDelete))
            for item in self.hybrid.operations
        )

    @property
    def adoption_count(self) -> int:
        """Return the number of existing paths explicitly adopted by ``--full``."""

        if self.hybrid is None:
            return 0
        if self.hybrid.ftp is not None:
            return len(self.hybrid.ftp.adoptions)
        return sum(isinstance(item, HybridAdoption) for item in self.hybrid.operations)

    @property
    def operation_count(self) -> int:
        """Return reviewed remote mutations excluding display-only Adoption rows."""

        hybrid_count = 0
        if self.hybrid is not None:
            if self.hybrid.ftp is not None:
                hybrid_count = (
                    len(self.hybrid.ftp.uploads)
                    + len(self.hybrid.ftp.create_directories)
                    + len(self.hybrid.ftp.delete_files)
                    + len(self.hybrid.ftp.remove_directories)
                    + int(self.hybrid.ftp.ownership_update)
                    + int(self.hybrid.ftp.pending is not None)
                )
                return len(self.operations) + hybrid_count
            hybrid_count = sum(
                not isinstance(item, HybridAdoption) for item in self.hybrid.operations
            )
            hybrid_count += len(self.hybrid.recovery_records)
        return len(self.operations) + hybrid_count

    @property
    def has_remote_work(self) -> bool:
        """Return whether deployment must connect, mutate, or run commands."""

        local_hybrid_work = bool(
            self.hybrid
            and not self.hybrid.remote_complete
            and (
                self.hybrid.local.directories
                or any(
                    self.full
                    or self.hybrid.previous_outputs.get(item.name) != item.entry
                    for item in self.hybrid.local.root_files
                )
            )
        )
        return bool(
            self.operations
            or local_hybrid_work
            or (
                self.hybrid
                and (self.hybrid.operations or self.hybrid.recovery_records)
            )
            or (
                self.hybrid
                and self.hybrid.ftp
                and (
                    self.hybrid.ftp.uploads
                    or self.hybrid.ftp.create_directories
                    or self.hybrid.ftp.delete_files
                    or self.hybrid.ftp.remove_directories
                    or self.hybrid.ftp.ownership_update
                    or self.hybrid.ftp.pending is not None
                )
            )
        )


def create_plan(
    config: Config,
    target: TargetConfig,
    repository: GitRepository,
    state: TargetState | None,
    *,
    full: bool,
    resolved_target: TargetConfig | None = None,
) -> DeploymentPlan:
    """Build the complete source/output plan without opening a remote connection.

    Args:
        config: Validated v1-lite project configuration.
        target: Selected target environment.
        repository: Validated Git worktree reader.
        state: Last complete target state, or ``None`` for first deployment.
        full: Force all current managed content to upload.

    Returns:
        A deterministic, conflict-free deployment plan.
    """

    repository.validate()
    head = repository.head()
    resolved_target = resolved_target or resolve_target_for_plan(target)
    target_fingerprint = resolved_target.fingerprint
    effective_full = full or state is None
    if state is not None and state.target_fingerprint != target_fingerprint and not full:
        raise PlanError("target identity changed since the last success; review it and rerun with --full")
    incremental_outputs = tuple(output for output in config.outputs if output.mode == "incremental")
    hybrid_output = next((output for output in config.outputs if output.mode == "hybrid"), None)
    current_outputs = scan_outputs(incremental_outputs)
    hybrid_local = scan_hybrid_output(hybrid_output) if hybrid_output is not None else None
    entries = {entry.path: entry for entry in repository.list_head_entries()}
    source_owned = {path for path in entries if is_source_managed(path, config.source)}
    overlap = sorted(source_owned.intersection(current_outputs))
    if overlap:
        preview = ", ".join(overlap[:5])
        suffix = " ..." if len(overlap) > 5 else ""
        raise PlanError(f"source/output ownership conflict: {preview}{suffix}")
    if hybrid_local is not None:
        previous_incremental = {
            path
            for path in (state.outputs if state is not None else {})
            if _delete_removed_for(path, incremental_outputs)
        }
        _validate_hybrid_conflicts(
            config,
            hybrid_local,
            source_owned,
            set(current_outputs) | previous_incremental,
        )
    requires_source_contract = (
        resolved_target.protocol == "ftp" and hybrid_local is not None
    )
    source_operations = _plan_source(
        repository,
        config,
        state,
        head,
        effective_full,
        entries,
        require_content_identity=requires_source_contract,
    )
    if resolved_target.protocol == "ftp":
        executable_paths = [
            operation.remote_path
            for operation in source_operations
            if isinstance(operation, UploadOperation) and operation.executable
        ]
        if executable_paths:
            raise PlanError(
                "FTP cannot guarantee executable mode for source path(s): "
                + ", ".join(executable_paths[:5])
            )
    output_operations = _plan_outputs(
        incremental_outputs, current_outputs, state, effective_full
    )
    operations = _merge_operations((*source_operations, *output_operations), config)
    manifest = {path: item.entry for path, item in current_outputs.items()}
    if hybrid_local is not None:
        # Persist Mirror file hashes in Local State so FTP In-place can skip
        # unchanged nested files (Root Files already used this path).
        hybrid_entries = hybrid_content_manifest(hybrid_local)
        overlap = sorted(set(manifest) & set(hybrid_entries))
        if overlap:
            raise PlanError(
                "incremental output path collides with hybrid content path: "
                + ", ".join(overlap[:10])
            )
        manifest.update(hybrid_entries)
    ftp_root_namespace: tuple[tuple[str, str], ...] = ()
    if hybrid_local is not None and resolved_target.protocol == "ftp":
        historical_source: set[str] = set()
        if state is not None:
            if not repository.commit_exists(state.last_commit):
                raise PlanError(
                    "FTP Hybrid cannot prove the historical Source root namespace "
                    f"because State commit {state.last_commit!r} is unavailable"
                )
            historical_source = {
                entry.path
                for entry in repository.list_entries(state.last_commit)
                if is_source_managed(entry.path, config.source)
            }
        namespace_entries = [
            *(("source", path) for path in sorted(source_owned | historical_source)),
            *(
                ("incremental", path)
                for path in sorted(
                    set(current_outputs)
                    | set(state.outputs if state is not None else {})
                )
            ),
            *(("hybrid", name) for name in hybrid_local.names),
            ("internal", ".git-deploy"),
        ]
        _validate_ftp_root_namespace(namespace_entries)
        ftp_root_namespace = tuple(namespace_entries)
    plan = DeploymentPlan(
        target=resolved_target,
        target_fingerprint=target_fingerprint,
        head=head,
        previous_commit=state.last_commit if state else None,
        operations=operations,
        output_manifest=manifest,
        full=effective_full,
        allow_adoption=full,
        hybrid=(
            HybridPlan(
                hybrid_local,
                dict(state.outputs) if state is not None else {},
                resolve_hybrid_backend(resolved_target),
            )
            if hybrid_local is not None
            else None
        ),
        non_hybrid_owned=tuple(sorted((*source_owned, *current_outputs.keys()))),
        previous_state_hash=target_state_hash(state),
        ftp_root_namespace=ftp_root_namespace,
    )
    if requires_source_contract:
        return replace(plan, non_hybrid_plan_hash=deployment_contract_hash(plan, config))
    return plan


def deployment_contract_hash(plan: DeploymentPlan, config: Config) -> str:
    """Hash every immutable non-Hybrid input needed for safe FTP resume.

    Args:
        plan: Frozen local deployment plan.
        config: Source and incremental-output policy used to create the plan.

    Returns:
        Lowercase SHA256 of deterministic, machine-independent JSON.
    """

    operations: list[dict[str, object]] = []
    for operation in plan.operations:
        if isinstance(operation, UploadOperation):
            if operation.sha256 is None or operation.size is None:
                raise PlanError(
                    f"upload operation lacks frozen content identity: {operation.remote_path}"
                )
            operations.append(
                {
                    "type": "upload",
                    "origin": operation.origin,
                    "remote_path": operation.remote_path,
                    "git_path": operation.git_path,
                    "executable": operation.executable,
                    "sha256": operation.sha256,
                    "size": operation.size,
                }
            )
        else:
            operations.append(
                {
                    "type": "delete",
                    "origin": operation.origin,
                    "remote_path": operation.remote_path,
                }
            )
    incremental_policy = [
        {
            "remote": output.remote.as_posix(),
            "delete_removed": output.delete_removed,
            "mode": output.mode,
        }
        for output in sorted(
            (item for item in config.outputs if item.mode == "incremental"),
            key=lambda item: (item.remote.as_posix(), item.name or ""),
        )
    ]
    payload = {
        "head": plan.head,
        "previous_commit": plan.previous_commit,
        "previous_state_hash": plan.previous_state_hash,
        "target_fingerprint": plan.target_fingerprint,
        "full": plan.full,
        "allow_adoption": plan.allow_adoption,
        "operations": operations,
        "source_policy": {
            "include": list(config.source.include),
            "exclude": list(config.source.exclude),
            "protect": list(config.source.protect),
            "require_clean_worktree": config.source.require_clean_worktree,
        },
        "incremental_output_policy": incremental_policy,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_ftp_root_namespace(entries: Iterable[tuple[str, str]]) -> None:
    """Reject non-portable FTP root names across every ownership domain.

    Args:
        entries: Pairs of ownership domain and managed relative path.

    Returns:
        ``None`` when every root component is unique under NFC plus casefold.
    """

    roots: dict[str, tuple[str, str]] = {}
    for domain, path in entries:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise PlanError(f"unsafe {domain} path in FTP root namespace: {path!r}")
        root = candidate.parts[0]
        key = unicodedata.normalize("NFC", root).casefold()
        previous = roots.get(key)
        if previous is None:
            roots[key] = (domain, root)
            continue
        previous_domain, previous_root = previous
        if root == previous_root and root != ".git-deploy":
            continue
        raise PlanError(
            "FTP root namespace is not portable: "
            f"{previous_domain} {previous_root!r} conflicts with {domain} {root!r} "
            "under NFC plus casefold semantics"
        )


def complete_remote_plan(
    plan: DeploymentPlan,
    config: Config,
    transport: Transport,
) -> DeploymentPlan:
    """Merge remote ownership/adoption facts into one frozen local plan.

    Args:
        plan: Local plan whose upload bytes are already frozen.
        config: Project identity and safety configuration.
        transport: Connected SFTP transport used only for preflight reads.

    Returns:
        Immutable full plan ready for confirmation or read-only display.
    """

    hybrid = plan.hybrid
    if hybrid is None:
        return plan
    if config.project_id is None:
        raise PlanError("hybrid output lacks a resolved project_id")
    if hybrid.backend is HybridBackend.FTP_IN_PLACE:
        if not isinstance(transport, FTPTransport):
            raise PlanError("FTP Hybrid planning requires FTPTransport semantics")
        return _complete_ftp_remote_plan(plan, config, transport)
    validate_internal_paths(transport)
    records = read_recovery_records(
        transport,
        mapping=hybrid.local.mapping,
        target_fingerprint=plan.target_fingerprint,
    )
    if len(records) > 1:
        raise PlanError("multiple remote hybrid recovery records require manual inspection")
    ownership, ownership_snapshot = read_ownership_snapshot(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
    )
    if records:
        record = records[0]
        commands_pending = (
            ownership_snapshot == record.new_ownership_hash
            and record.phase in {
                RecoveryPhase.SWAPPING,
                RecoveryPhase.OWNERSHIP_COMMITTED,
            }
        )
        if (
            record.schema >= 2
            and commands_pending
            and record.command_hash
            != recovery_command_hash(
                plan.target.after_deploy,
                plan.target.command_timeout,
            )
        ):
            raise PlanError(
                "after_deploy commands or timeout changed since the interrupted "
                "Hybrid deployment; restore the reviewed configuration before recovery"
            )
        completed = replace(
            hybrid,
            ownership=ownership,
            expected_ownership_hash=ownership_snapshot,
            recovery_records=records,
            remote_complete=True,
        )
        return replace(plan, operations=(), hybrid=completed)
    current_files = set(hybrid.local.root_file_names)
    current_directories = set(hybrid.local.directory_names)
    current = current_files | current_directories
    old_files = set(ownership.root_files) if ownership is not None else set()
    old_directories = set(ownership.directories) if ownership is not None else set()
    old = old_files | old_directories
    _reject_historical_transfer(plan, old - current)
    types: dict[str, RemotePathType] = {}
    adoptions: set[str] = set()
    for name in sorted(current | old):
        kind = transport.lstat(name)
        if kind in {RemotePathType.SYMLINK, RemotePathType.OTHER}:
            raise PlanError(f"hybrid-owned remote path has unsupported type {kind.value}: {name}")
        types[name] = kind
        if name in current and name not in old and kind is not RemotePathType.MISSING:
            if not plan.allow_adoption:
                raise PlanError(
                    f"remote path {name!r} exists but is not owned by hybrid output "
                    f"{hybrid.local.mapping!r}; review it and rerun with --full to adopt it"
                )
            adoptions.add(name)
    operations: list[HybridOperation] = []
    for item in hybrid.local.root_files:
        previous = hybrid.previous_outputs.get(item.name)
        if (
            plan.full
            or previous != item.entry
            or types[item.name] is not RemotePathType.FILE
            or item.name in adoptions
        ):
            operations.append(
                HybridRootFileUpload(
                    item.name,
                    item.local_path,
                    item.entry.sha256,
                    item.entry.size,
                )
            )
    for item in hybrid.local.directories:
        operations.append(
            HybridDirectoryMirror(
                item.name,
                item.file_count,
                item.total_size,
                item.name in adoptions,
            )
        )
    operations.extend(HybridRootFileDelete(name) for name in sorted(old_files - current))
    operations.extend(
        HybridDirectoryDelete(name) for name in sorted(old_directories - current)
    )
    operations.extend(HybridAdoption(name) for name in sorted(adoptions))
    ownership_changed = ownership is None or (
        ownership.directories != hybrid.local.directory_names
        or ownership.root_files != hybrid.local.root_file_names
    )
    if operations or plan.operations or ownership_changed:
        operations.append(HybridOwnershipUpdate(hybrid.local.mapping))
    completed = replace(
        hybrid,
        ownership=ownership,
        operations=tuple(operations),
        expected_ownership_hash=ownership_snapshot,
        expected_path_types=tuple(sorted(types.items())),
        remote_complete=True,
    )
    return replace(plan, hybrid=completed)


def _complete_ftp_remote_plan(
    plan: DeploymentPlan,
    config: Config,
    transport: FTPTransport,
) -> DeploymentPlan:
    """Build an exact typed FTP publish/prune plan without remote mutations.

    Args:
        plan: Frozen local plan with an FTP Hybrid backend.
        config: Project identity and local output contract.
        transport: Connected FTP adapter used only for FEAT/MLSD/RETR reads.

    Returns:
        Remote-complete plan with exact uploads, deletes, RMDs, and resume phase.
    """

    hybrid = plan.hybrid
    if hybrid is None or config.project_id is None:
        raise PlanError("FTP Hybrid planning requires one mapping and project_id")
    if plan.target.runtime_dir is None:
        raise PlanError("FTP Hybrid planning lacks a Git runtime directory")
    transport.enable_utf8()
    capabilities = load_capability_profile(
        plan.target.runtime_dir,
        plan.target,
        server_banner_hash=transport.server_banner_hash(),
    )
    validate_remote_root_aliases(transport, plan.ftp_root_namespace)
    ownership, ownership_snapshot = read_ownership_snapshot(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
    )
    pending = read_pending(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
        target=plan.target,
    )
    # Schema 2 Pending Hash must ignore Mirror nested State keys (v1.7.3 shape).
    manifest_snapshot = pending_local_manifest_hash(hybrid.local, plan.output_manifest)
    if pending is not None:
        validate_pending_resume(
            pending,
            manifest_hash=manifest_snapshot,
            head=plan.head,
            non_hybrid_plan_hash=plan.non_hybrid_plan_hash,
            previous_state_hash=plan.previous_state_hash,
            current_ownership_hash=ownership_snapshot,
        )
    previous_updated_at = ownership.updated_at if ownership is not None else -1
    next_updated_at = (
        pending.created_at
        if pending is not None
        else max(int(time.time()), previous_updated_at + 1)
    )
    next_ownership = make_ownership(
        hybrid.local,
        config.project_id,
        plan.head,
        now=next_updated_at,
    )
    next_ownership_hash = ownership_hash(next_ownership)
    if pending is not None and pending.next_ownership_hash != next_ownership_hash:
        raise PlanError("FTP Hybrid Pending next Ownership does not match the local view")
    if (
        pending is not None
        and pending.phase
        in {FTPPendingPhase.OWNERSHIP_COMMITTED, FTPPendingPhase.STATE_COMPLETE}
        and ownership_snapshot != pending.next_ownership_hash
    ):
        raise PlanError("FTP Hybrid Pending phase claims committed Ownership but it is stale")

    current_files = set(hybrid.local.root_file_names)
    current_directories = set(hybrid.local.directory_names)
    current = current_files | current_directories
    old_files = set(ownership.root_files if ownership is not None else ())
    old_directories = set(ownership.directories if ownership is not None else ())
    old = old_files | old_directories
    _validate_ftp_root_namespace(
        [
            *(("non-hybrid", path) for path in plan.non_hybrid_owned),
            *(("hybrid", name) for name in sorted(current)),
            *(("historical hybrid", name) for name in sorted(old)),
            ("internal", ".git-deploy"),
        ]
    )
    validate_remote_root_aliases(
        transport,
        (
            *plan.ftp_root_namespace,
            *(("historical hybrid", name) for name in sorted(old)),
        ),
    )
    _reject_historical_transfer(plan, old - current)
    type_changes = sorted(
        (current_files & old_directories) | (current_directories & old_files)
    )
    if type_changes:
        raise PlanError(
            "FTP Hybrid cannot safely change an owned direct path between file and directory; "
            "remove or migrate it explicitly, deploy that deletion, then add the new type "
            "with --full: " + ", ".join(type_changes)
        )

    expected_types: dict[str, RemotePathType] = {}
    adoptions: set[str] = set()
    resume_owned = current if pending is not None else set()
    for name in sorted(current | old):
        kind = transport.lstat(name)
        expected_types[name] = kind
        if name in current_files and kind not in {RemotePathType.MISSING, RemotePathType.FILE}:
            raise PlanError(f"FTP Hybrid remote path type mismatch for file {name!r}: {kind.value}")
        if name in current_directories and kind not in {
            RemotePathType.MISSING,
            RemotePathType.DIRECTORY,
        }:
            raise PlanError(
                f"FTP Hybrid remote path type mismatch for directory {name!r}: {kind.value}"
            )
        if name in old_files - current and kind not in {
            RemotePathType.MISSING,
            RemotePathType.FILE,
        }:
            raise PlanError(f"FTP Hybrid historical file changed type: {name!r}")
        if name in old_directories - current and kind not in {
            RemotePathType.MISSING,
            RemotePathType.DIRECTORY,
        }:
            raise PlanError(f"FTP Hybrid historical directory changed type: {name!r}")
        if (
            name in current
            and name not in old
            and name not in resume_owned
            and kind is not RemotePathType.MISSING
        ):
            if not plan.allow_adoption:
                raise PlanError(
                    f"remote path {name!r} exists but is not owned by FTP Hybrid output "
                    f"{hybrid.local.mapping!r}; review it and rerun with --full to adopt it"
                )
            adoptions.add(name)

    directory_manifests = {item.name: item for item in hybrid.local.directories}
    remote_trees: dict[str, FTPRemoteTree] = {}
    for name in sorted(current_directories | old_directories):
        if expected_types.get(name, RemotePathType.MISSING) is RemotePathType.DIRECTORY:
            remote_trees[name] = scan_ftp_tree(transport, name)
        else:
            remote_trees[name] = FTPRemoteTree(name, (), ())
    for name in sorted(current_directories):
        local = directory_manifests[name]
        tree = remote_trees[name]
        nested_type_changes = sorted(
            (set(local.files) & set(tree.directories))
            | (set(local.directories) & set(tree.files))
        )
        if nested_type_changes:
            raise PlanError(
                "FTP Hybrid cannot safely change a Mirror path between file and directory "
                "while preserving upload-first: "
                + ", ".join(f"{name}/{path}" for path in nested_type_changes[:10])
            )

    phase = pending.phase if pending is not None else None
    publish_needed = phase not in {
        FTPPendingPhase.PRUNED,
        FTPPendingPhase.OWNERSHIP_COMMITTED,
        FTPPendingPhase.STATE_COMPLETE,
    }
    prune_needed = phase not in {
        FTPPendingPhase.PRUNED,
        FTPPendingPhase.OWNERSHIP_COMMITTED,
        FTPPendingPhase.STATE_COMPLETE,
    }
    uploads: list[FTPHybridFileUpload] = []
    if publish_needed:
        for item in hybrid.local.root_files:
            previous = hybrid.previous_outputs.get(item.name)
            if (
                pending is not None
                or plan.full
                or previous != item.entry
                or expected_types[item.name] is not RemotePathType.FILE
                or item.name in adoptions
            ):
                uploads.append(
                    FTPHybridFileUpload(
                        item.name,
                        item.local_path,
                        item.entry.sha256,
                        item.entry.size,
                    )
                )
        for directory in hybrid.local.directories:
            tree = remote_trees[directory.name]
            adopt_directory = directory.name in adoptions
            for relative, scanned in sorted(directory.files.items()):
                path = f"{directory.name}/{relative}"
                previous = hybrid.previous_outputs.get(path)
                # Same trust model as Root Files: Local State hash is content
                # proof; remote Size/Modify is not. Missing remote members and
                # adoption/full/pending still force a republish.
                if (
                    pending is not None
                    or plan.full
                    or previous != scanned.entry
                    or relative not in tree.files
                    or adopt_directory
                ):
                    uploads.append(
                        FTPHybridFileUpload(
                            path,
                            scanned.local_path,
                            scanned.entry.sha256,
                            scanned.entry.size,
                        )
                    )

    create_directories: set[str] = set()
    if publish_needed:
        for directory in hybrid.local.directories:
            create_directories.add(directory.name)
            create_directories.update(
                f"{directory.name}/{relative}" for relative in directory.directories
            )
    delete_files: set[str] = set()
    remove_directories: set[str] = set()
    if prune_needed:
        for name in old_files - current:
            if expected_types[name] is RemotePathType.FILE:
                delete_files.add(name)
        for name in current_directories:
            tree = remote_trees[name]
            local = directory_manifests[name]
            delete_files.update(f"{name}/{path}" for path in set(tree.files) - set(local.files))
            remove_directories.update(
                f"{name}/{path}" for path in set(tree.directories) - set(local.directories)
            )
        for name in old_directories - current:
            tree = remote_trees[name]
            delete_files.update(f"{name}/{path}" for path in tree.files)
            remove_directories.update(f"{name}/{path}" for path in tree.directories)
            if expected_types[name] is RemotePathType.DIRECTORY:
                remove_directories.add(name)

    if phase in {
        FTPPendingPhase.PRUNED,
        FTPPendingPhase.OWNERSHIP_COMMITTED,
        FTPPendingPhase.STATE_COMPLETE,
    }:
        for item in hybrid.local.root_files:
            if transport.lstat(item.name) is not RemotePathType.FILE:
                raise PlanError(f"FTP Hybrid resume cannot verify published file: {item.name}")
        for directory in hybrid.local.directories:
            tree = remote_trees[directory.name]
            missing = sorted(set(directory.files) - set(tree.files))
            if missing:
                raise PlanError(
                    "FTP Hybrid resume cannot verify published mirror files: "
                    + ", ".join(f"{directory.name}/{path}" for path in missing[:10])
                )
            orphan_files = sorted(set(tree.files) - set(directory.files))
            orphan_directories = sorted(set(tree.directories) - set(directory.directories))
            if orphan_files or orphan_directories:
                raise PlanError(
                    "FTP Hybrid Pending phase claims prune complete but owned orphans remain: "
                    + ", ".join(
                        [
                            *(f"{directory.name}/{path}" for path in orphan_files[:5]),
                            *(f"{directory.name}/{path}/" for path in orphan_directories[:5]),
                        ]
                    )
                )
        remaining_historical = sorted(
            name
            for name in (old_files | old_directories) - current
            if transport.lstat(name) is not RemotePathType.MISSING
        )
        if remaining_historical:
            raise PlanError(
                "FTP Hybrid Pending phase claims prune complete but historical paths remain: "
                + ", ".join(remaining_historical)
            )

    ownership_changed = ownership is None or (
        ownership.directories != next_ownership.directories
        or ownership.root_files != next_ownership.root_files
        or ownership.last_commit != next_ownership.last_commit
    )
    ownership_update = (
        phase not in {FTPPendingPhase.OWNERSHIP_COMMITTED, FTPPendingPhase.STATE_COMPLETE}
        and (
            bool(uploads or create_directories or delete_files or remove_directories)
            or bool(plan.operations)
            or ownership_changed
            or pending is not None
        )
    )
    ftp = FTPHybridPlan(
        capabilities,
        pending,
        next_ownership,
        tuple(sorted(uploads, key=lambda item: item.path)),
        tuple(sorted(create_directories, key=lambda path: (len(PurePosixPath(path).parts), path))),
        tuple(sorted(delete_files)),
        tuple(
            sorted(
                remove_directories,
                key=lambda path: (-len(PurePosixPath(path).parts), path),
            )
        ),
        tuple(sorted(adoptions)),
        tuple(remote_trees[name] for name in sorted(remote_trees)),
        ownership_update,
        phase,
    )
    completed = replace(
        hybrid,
        ownership=ownership,
        ftp=ftp,
        expected_ownership_hash=ownership_snapshot,
        expected_path_types=tuple(sorted(expected_types.items())),
        remote_complete=True,
    )
    return replace(plan, hybrid=completed)


def create_recovery_plan(
    config: Config,
    target: TargetConfig,
    transport: Transport,
) -> ExplicitRecoveryPlan | None:
    """Read one Recovery without building, scanning outputs, or loading State.

    Args:
        config: Loaded project configuration containing one Hybrid mapping.
        target: Fully resolved target bound to the local lock and remote root.
        transport: Connected read-only transport.

    Returns:
        Recovery-only plan, or ``None`` when this project has no pending record.
    """

    hybrid_outputs = tuple(output for output in config.outputs if output.mode == "hybrid")
    if not hybrid_outputs:
        return None
    if len(hybrid_outputs) != 1 or config.project_id is None:
        raise PlanError("Hybrid recovery requires one mapping and a resolved project_id")
    output = hybrid_outputs[0]
    if output.name is None:
        raise PlanError("Hybrid recovery mapping is missing its validated name")
    if target.protocol == "ftp":
        if not isinstance(transport, FTPTransport):
            raise PlanError("FTP Hybrid recovery requires FTPTransport semantics")
        if target.runtime_dir is None:
            raise PlanError("FTP Hybrid recovery lacks a Git runtime directory")
        transport.enable_utf8()
        load_capability_profile(
            target.runtime_dir,
            target,
            server_banner_hash=transport.server_banner_hash(),
        )
        validate_remote_root_aliases(transport, (("internal", ".git-deploy"),))
        _, ownership_snapshot = read_ownership_snapshot(
            transport,
            project_id=config.project_id,
            mapping=output.name,
            remote=output.remote.as_posix(),
        )
        pending = read_pending(
            transport,
            project_id=config.project_id,
            mapping=output.name,
            remote=output.remote.as_posix(),
            target=target,
        )
        if pending is None:
            return None
        if pending.phase not in {
            FTPPendingPhase.OWNERSHIP_COMMITTED,
            FTPPendingPhase.STATE_COMPLETE,
        }:
            raise PlanError(
                "FTP Hybrid Pending still requires frozen local files; rerun the normal "
                "deployment instead of --recover"
            )
        validate_pending_resume(
            pending,
            current_ownership_hash=ownership_snapshot,
        )
        return FTPRecoveryPlan(
            target,
            target.fingerprint,
            config.project_id,
            output.name,
            output.remote.as_posix(),
            ownership_snapshot,
            pending,
        )
    validate_internal_paths(transport)
    records = read_recovery_records(
        transport,
        mapping=output.name,
        target_fingerprint=target.fingerprint,
    )
    if not records:
        return None
    if len(records) != 1:
        raise PlanError("multiple remote hybrid recovery records require manual inspection")
    ownership, ownership_hash = read_ownership_snapshot(
        transport,
        project_id=config.project_id,
        mapping=output.name,
        remote=output.remote.as_posix(),
    )
    record = records[0]
    outcome = inspect_recovery(transport, record)
    if (
        outcome.commands_pending
        and record.command_hash
        != recovery_command_hash(target.after_deploy, target.command_timeout)
    ):
        raise PlanError(
            "after_deploy commands or timeout changed since the interrupted "
            "Hybrid deployment; restore the reviewed configuration before recovery"
        )
    return RecoveryPlan(
        target,
        target.fingerprint,
        config.project_id,
        output.name,
        output.remote.as_posix(),
        ownership,
        ownership_hash,
        record,
        outcome,
    )


def render_recovery_plan(plan: ExplicitRecoveryPlan) -> str:
    """Render only actions that explicit recovery will actually execute.

    Args:
        plan: Read-only Recovery plan and classified pending phases.

    Returns:
        Human-readable target, phase action, commands, and exact summary.
    """

    if isinstance(plan, FTPRecoveryPlan):
        action = (
            "SAVE FROZEN STATE + CLEANUP"
            if plan.pending.phase is FTPPendingPhase.OWNERSHIP_COMMITTED
            else "CLEANUP"
        )
        lines = [
            f"Target: {plan.target.name} "
            f"({plan.target.protocol}://{plan.target.host}{plan.target.remote_root})",
            "Mode: RECOVERY",
            f"RECOVER [{plan.pending.phase.value.lower()}] "
            f"{plan.pending.deployment_id} {action}",
            "Summary: 1 recovery action(s), 0 pending command(s), "
            f"state={'yes' if plan.outcome.state_pending else 'no'}, cleanup=yes",
        ]
        return "\n".join(lines)
    action = _recovery_action(plan.record, plan.outcome)
    lines = [
        f"Target: {plan.target.name} "
        f"({plan.target.protocol}://"
        f"{plan.target.host or plan.target.ssh_host_alias}{plan.target.remote_root})",
        "Mode: RECOVERY",
        f"RECOVER [{plan.record.phase.value.lower()}] "
        f"{plan.record.deployment_id} {action}",
    ]
    if plan.outcome.commands_pending:
        lines.extend(f"AFTER  {command}" for command in plan.target.after_deploy)
    lines.append(
        "Summary: 1 recovery action(s), "
        f"{len(plan.target.after_deploy) if plan.outcome.commands_pending else 0} "
        "pending command(s), "
        f"state={'yes' if plan.outcome.state_pending else 'no'}, "
        f"cleanup={'yes' if plan.outcome.cleanup_pending else 'no'}"
    )
    return "\n".join(lines)


def render_plan(plan: DeploymentPlan) -> str:
    """Render a stable human-readable dry-run/deploy preview.

    Args:
        plan: Completed deployment plan.

    Returns:
        Multi-line summary containing every remote operation.
    """

    recovery = (
        plan.hybrid.recovery_records[0]
        if plan.hybrid is not None and plan.hybrid.recovery_records
        else None
    )
    if recovery is not None and plan.hybrid is not None:
        committed = plan.hybrid.expected_ownership_hash == recovery.new_ownership_hash
        outcome = HybridRecoveryOutcome(
            committed,
            committed
            and recovery.phase
            in {RecoveryPhase.SWAPPING, RecoveryPhase.OWNERSHIP_COMMITTED},
            committed
            and recovery.phase
            in {
                RecoveryPhase.SWAPPING,
                RecoveryPhase.OWNERSHIP_COMMITTED,
                RecoveryPhase.COMMANDS_COMPLETE,
            },
            True,
        )
        lines = [
            f"Target: {plan.target.name} "
            f"({plan.target.protocol}://"
            f"{plan.target.host or plan.target.ssh_host_alias}{plan.target.remote_root})",
            "Mode: RECOVERY",
            *render_hybrid_plan(plan.hybrid),
        ]
        executable_commands = outcome.commands_pending and recovery.schema >= 2
        if executable_commands:
            lines.extend(f"AFTER  {command}" for command in plan.target.after_deploy)
        lines.append(
            "Summary: 1 recovery action(s), "
            f"{len(plan.target.after_deploy) if executable_commands else 0} "
            "pending command(s), "
            f"state={'yes' if outcome.state_pending else 'no'}, cleanup=yes"
        )
        return "\n".join(lines)
    mode = "FULL" if plan.full else "INCREMENTAL"
    lines = [
        f"Target: {plan.target.name} ({plan.target.protocol}://{plan.target.host or plan.target.ssh_host_alias}{plan.target.remote_root})",
        f"Mode: {mode}",
        f"Commit: {plan.previous_commit or '<first deployment>'} -> {plan.head}",
    ]
    for operation in plan.operations:
        action = "UPLOAD" if isinstance(operation, UploadOperation) else "DELETE"
        lines.append(f"{action:6} [{operation.origin}] {operation.remote_path}")
    if plan.hybrid is not None:
        lines.extend(render_hybrid_plan(plan.hybrid))
    if plan.has_remote_work:
        lines.extend(f"AFTER  {command}" for command in plan.target.after_deploy)
    command_count = len(plan.target.after_deploy) if plan.has_remote_work else 0
    lines.append(
        f"Summary: {plan.upload_count} upload(s), {plan.delete_count} delete(s), "
        f"{plan.adoption_count} adoption(s), {command_count} after-deploy command(s)"
    )
    return "\n".join(lines)


def render_hybrid_plan(hybrid: HybridPlan) -> tuple[str, ...]:
    """Render local Hybrid facts or the completed remote ownership plan."""

    lines = [
        f"HYBRID Mapping: {hybrid.local.mapping} -> {hybrid.local.remote}",
        "HYBRID BACKEND: "
        + (
            "FTP IN-PLACE"
            if hybrid.backend is HybridBackend.FTP_IN_PLACE
            else "SFTP STAGED"
        ),
    ]
    if not hybrid.remote_complete:
        for item in hybrid.local.root_files:
            lines.append(f"LOCAL   [hybrid-file] {item.name} ({item.entry.size} byte(s))")
        for item in hybrid.local.directories:
            lines.append(
                f"LOCAL   [hybrid-mirror] {item.name}/ "
                f"({item.file_count} file(s), {item.total_size} byte(s))"
            )
        if hybrid.backend is HybridBackend.FTP_IN_PLACE:
            lines.append("REMOTE  [ftp-hybrid] not read; use --remote-plan for exact orphan deletes")
            lines.append("GUARANTEE upload-first=yes, prune-last=yes, forward-resume=yes")
            lines.append(
                "LIMITS directory-atomic-swap=no, rollback=no, after_deploy=no, "
                "concurrent-last-moment-no-overwrite=no"
            )
        else:
            lines.append(
                "REMOTE  [hybrid-owner] not read; use --remote-plan for full ownership plan"
            )
        return tuple(lines)
    if hybrid.ftp is not None:
        ftp = hybrid.ftp
        lines.append(
            "SNAPSHOT [ftp-profile] "
            f"banner={ftp.capabilities.server_banner_hash[:12]} "
            f"({len(hybrid.expected_path_types)} direct path type(s))"
        )
        if ftp.pending is not None:
            lines.append(
                f"RESUME  [ftp-forward] {ftp.pending.deployment_id} "
                f"from {ftp.pending.phase.value}"
            )
        for path in ftp.create_directories:
            lines.append(f"MKDIR  [ftp-hybrid] {path}/")
        for operation in ftp.uploads:
            lines.append(f"UPLOAD [ftp-hybrid] {operation.path}")
        for path in ftp.delete_files:
            lines.append(f"DELETE [ftp-owner] {path}")
        for path in ftp.remove_directories:
            lines.append(f"RMD    [ftp-owner] {path}/")
        for path in ftp.adoptions:
            lines.append(f"ADOPT  [ftp-owner] {path}")
        if ftp.ownership_update:
            lines.append(f"OWNERSHIP UPDATE [ftp-owner] {hybrid.local.mapping}")
        if len(ftp.delete_files) > 10_000:
            lines.append("WARNING FTP Hybrid will delete more than 10,000 files")
        if len(ftp.remove_directories) > 1_000:
            lines.append("WARNING FTP Hybrid will remove more than 1,000 directories")
        owned_count = len(hybrid.ownership.root_files) + len(hybrid.ownership.directories) if hybrid.ownership else 0
        if owned_count and not hybrid.local.names:
            lines.append("WARNING FTP Hybrid will delete all previously owned paths")
        lines.append("GUARANTEE upload-first=yes, prune-last=yes, forward-resume=yes")
        lines.append(
            "LIMITS directory-atomic-swap=no, rollback=no, after_deploy=no, "
            "concurrent-last-moment-no-overwrite=no"
        )
        return tuple(lines)
    if hybrid.recovery_records:
        for record in hybrid.recovery_records:
            committed = hybrid.expected_ownership_hash == record.new_ownership_hash
            outcome = HybridRecoveryOutcome(
                committed,
                committed
                and record.phase
                in {RecoveryPhase.SWAPPING, RecoveryPhase.OWNERSHIP_COMMITTED},
                committed
                and record.phase
                in {
                    RecoveryPhase.SWAPPING,
                    RecoveryPhase.OWNERSHIP_COMMITTED,
                    RecoveryPhase.COMMANDS_COMPLETE,
                },
                True,
            )
            action = _recovery_action(record, outcome)
            lines.append(
                f"RECOVER [{record.phase.value.lower()}] {record.deployment_id} {action}"
            )
        lines.append("REMOTE  [hybrid-recovery] run with --recover after review")
        return tuple(lines)
    if hybrid.expected_ownership_hash is not None:
        lines.append(
            "SNAPSHOT [hybrid-owner] "
            f"{hybrid.expected_ownership_hash[:12]} "
            f"({len(hybrid.expected_path_types)} path type(s))"
        )
    for operation in hybrid.operations:
        if isinstance(operation, HybridRootFileUpload):
            lines.append(f"UPLOAD [hybrid-file] {operation.path}")
        elif isinstance(operation, HybridRootFileDelete):
            lines.append(f"DELETE [hybrid-owner] {operation.path}")
        elif isinstance(operation, HybridDirectoryMirror):
            suffix = " ADOPT" if operation.adopt else ""
            lines.append(
                f"MIRROR [hybrid-directory] {operation.name}/ "
                f"({operation.file_count} file(s), {operation.total_size} byte(s)){suffix}"
            )
        elif isinstance(operation, HybridDirectoryDelete):
            lines.append(f"DELETE [hybrid-owner] {operation.name}/")
        elif isinstance(operation, HybridAdoption):
            lines.append(f"ADOPT  [hybrid-owner] {operation.path}")
        else:
            lines.append(f"OWNERSHIP UPDATE [hybrid-owner] {operation.mapping}")
    return tuple(lines)


def _recovery_action(
    record: HybridRecoveryRecord,
    outcome: HybridRecoveryOutcome,
) -> str:
    """Return the exact user-facing action for one classified Recovery.

    Args:
        record: Durable Recovery phase.
        outcome: Read-only classification against current Ownership.

    Returns:
        Concise restore, command, state, or cleanup action label.
    """

    if outcome.commands_pending:
        if record.schema == 1:
            return "MANUAL: LEGACY COMMAND CONTRACT UNKNOWN"
        return "RESUME COMMANDS"
    if outcome.state_pending:
        return "SAVE STATE + CLEANUP"
    if outcome.ownership_committed or record.phase in {
        RecoveryPhase.PREPARED,
        RecoveryPhase.STAGED,
        RecoveryPhase.RESTORED,
    }:
        return "CLEANUP"
    return "RESTORE"


def validate_remote_freshness(
    plan: DeploymentPlan,
    config: Config,
    transport: Transport,
    *,
    expected_recovery_records: tuple[HybridRecoveryRecord, ...] | None = None,
    check_path_types: bool = True,
) -> None:
    """Prove execution facts still equal the user-reviewed Remote Plan.

    Args:
        plan: Remote-complete immutable deployment plan.
        config: Expected project identity.
        transport: Connected transport used only for read-only validation.
        expected_recovery_records: Optional execution-owned Recovery snapshot.
        check_path_types: Whether approved direct-path types must still match.

    Returns:
        ``None`` when ownership, recovery, and path types remain unchanged.
    """

    hybrid = plan.hybrid
    if hybrid is None or not hybrid.remote_complete or config.project_id is None:
        raise PlanError("hybrid freshness validation requires a remote-complete plan")
    if hybrid.backend is HybridBackend.FTP_IN_PLACE:
        if not isinstance(transport, FTPTransport):
            raise PlanError("FTP Hybrid freshness requires FTPTransport semantics")
        _validate_ftp_remote_freshness(plan, config, transport, check_path_types=check_path_types)
        return
    validate_internal_paths(transport)
    records = read_recovery_records(
        transport,
        mapping=hybrid.local.mapping,
        target_fingerprint=plan.target_fingerprint,
    )
    expected_records = (
        hybrid.recovery_records
        if expected_recovery_records is None
        else expected_recovery_records
    )
    if records != expected_records:
        raise StaleRemotePlanError(
            "remote recovery facts changed after plan approval; rerun and review the plan"
        )
    _, actual_ownership_hash = read_ownership_snapshot(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
    )
    if actual_ownership_hash != hybrid.expected_ownership_hash:
        raise StaleRemotePlanError(
            "remote ownership changed after plan approval; rerun and review the plan"
        )
    if check_path_types:
        for path, expected in hybrid.expected_path_types:
            actual = transport.lstat(path)
            if actual is not expected:
                raise StaleRemotePlanError(
                    f"remote path type changed after plan approval: {path!r} "
                    f"({expected.value} -> {actual.value}); rerun and review the plan"
                )


def _validate_ftp_remote_freshness(
    plan: DeploymentPlan,
    config: Config,
    transport: FTPTransport,
    *,
    check_path_types: bool,
) -> None:
    """Re-read every reviewed FTP ownership, pending, type, and tree fact."""

    hybrid = plan.hybrid
    if hybrid is None or hybrid.ftp is None or config.project_id is None:
        raise PlanError("FTP Hybrid freshness requires a remote-complete FTP plan")
    if plan.target.runtime_dir is None:
        raise PlanError("FTP Hybrid freshness lacks a Git runtime directory")
    transport.refresh_remote_metadata()
    validate_remote_root_aliases(
        transport,
        (
            *plan.ftp_root_namespace,
            *(("historical hybrid", path) for path, _ in hybrid.expected_path_types),
        ),
    )
    profile = load_capability_profile(
        plan.target.runtime_dir,
        plan.target,
        server_banner_hash=transport.server_banner_hash(),
    )
    if profile != hybrid.ftp.capabilities:
        raise StaleRemotePlanError("FTP Hybrid Capability Profile changed after plan approval")
    _, actual_ownership_hash = read_ownership_snapshot(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
    )
    if actual_ownership_hash != hybrid.expected_ownership_hash:
        raise StaleRemotePlanError(
            "FTP Hybrid Ownership changed after plan approval; rerun and review the plan"
        )
    actual_pending = read_pending(
        transport,
        project_id=config.project_id,
        mapping=hybrid.local.mapping,
        remote=hybrid.local.remote,
        target=plan.target,
    )
    if actual_pending != hybrid.ftp.pending:
        raise StaleRemotePlanError(
            "FTP Hybrid Pending Marker changed after plan approval; rerun and review the plan"
        )
    if not check_path_types:
        return
    for path, expected in hybrid.expected_path_types:
        actual = transport.lstat(path)
        if actual is not expected:
            raise StaleRemotePlanError(
                f"FTP Hybrid path type changed after plan approval: {path!r} "
                f"({expected.value} -> {actual.value})"
            )
    for expected_tree in hybrid.ftp.remote_trees:
        actual_tree = scan_ftp_tree(transport, expected_tree.root)
        if actual_tree != expected_tree:
            raise StaleRemotePlanError(
                f"FTP Hybrid managed tree changed after plan approval: {expected_tree.root!r}"
            )


def validate_recovery_freshness(
    plan: ExplicitRecoveryPlan,
    transport: Transport,
) -> HybridRecoveryOutcome:
    """Revalidate one Recovery-only plan immediately before mutation.

    Args:
        plan: User-reviewed Recovery facts.
        transport: Connected read-only transport.

    Returns:
        Current outcome when record, Ownership, and phase facts are unchanged.
    """

    if isinstance(plan, FTPRecoveryPlan):
        if not isinstance(transport, FTPTransport):
            raise PlanError(
                "FTP Hybrid recovery freshness requires FTPTransport semantics"
            )
        if plan.target.runtime_dir is None:
            raise PlanError(
                "FTP Hybrid recovery freshness lacks a Git runtime directory"
            )
        transport.enable_utf8()
        validate_remote_root_aliases(transport, (("internal", ".git-deploy"),))
        load_capability_profile(
            plan.target.runtime_dir,
            plan.target,
            server_banner_hash=transport.server_banner_hash(),
        )
        transport.refresh_remote_metadata()
        _, ownership_snapshot = read_ownership_snapshot(
            transport,
            project_id=plan.project_id,
            mapping=plan.mapping,
            remote=plan.remote,
        )
        if ownership_snapshot != plan.expected_ownership_hash:
            raise StaleRemotePlanError(
                "FTP Hybrid Ownership changed after recovery approval; rerun and review"
            )
        pending = read_pending(
            transport,
            project_id=plan.project_id,
            mapping=plan.mapping,
            remote=plan.remote,
            target=plan.target,
        )
        if pending is None:
            raise StaleRemotePlanError(
                "FTP Hybrid Pending disappeared after recovery approval; rerun and review"
            )
        if pending != plan.pending:
            raise StaleRemotePlanError(
                "FTP Hybrid Pending changed after recovery approval; rerun and review"
            )
        validate_pending_resume(
            pending,
            current_ownership_hash=ownership_snapshot,
        )
        return plan.outcome
    validate_internal_paths(transport)
    records = read_recovery_records(
        transport,
        mapping=plan.mapping,
        target_fingerprint=plan.target_fingerprint,
    )
    if records != (plan.record,):
        raise StaleRemotePlanError(
            "remote recovery facts changed after plan approval; rerun and review the plan"
        )
    _, actual_hash = read_ownership_snapshot(
        transport,
        project_id=plan.project_id,
        mapping=plan.mapping,
        remote=plan.remote,
    )
    if actual_hash != plan.expected_ownership_hash:
        raise StaleRemotePlanError(
            "remote ownership changed after recovery approval; rerun and review the plan"
        )
    outcome = inspect_recovery(transport, plan.record)
    if outcome != plan.outcome:
        raise StaleRemotePlanError(
            "remote recovery phase changed after plan approval; rerun and review the plan"
        )
    return outcome


def _plan_source(
    repository: GitRepository,
    config: Config,
    state: TargetState | None,
    head: str,
    full: bool,
    entries: dict[str, GitEntry],
    *,
    require_content_identity: bool,
) -> tuple[Operation, ...]:
    """Plan exact HEAD source blobs and optional stable content identities.

    Args:
        repository: Validated Git object reader.
        config: Source include, exclude, and protection policy.
        state: Previous successful deployment state when incremental.
        head: Frozen commit used as the incremental diff endpoint.
        full: Whether every current managed source path must upload.
        entries: Current file-like Git entries keyed by path.
        require_content_identity: Hash source only for FTP Hybrid resume binding.

    Returns:
        Deterministic source upload and deletion operations.
    """

    operations: list[Operation] = []
    if full:
        selected = tuple(
            entry
            for path, entry in entries.items()
            if is_source_managed(path, config.source)
        )
        for entry in selected:
            _require_regular_git_entry(entry)
        manifests = repository.blob_manifests(selected) if require_content_identity else {}
        for entry in selected:
            content = manifests.get(entry.path)
            operations.append(
                UploadOperation(
                    entry.path,
                    "source",
                    git_path=entry.path,
                    size=content.size if content is not None else entry.size,
                    executable=entry.mode == "100755",
                    sha256=content.sha256 if content is not None else None,
                )
            )
        return tuple(operations)
    if state is None:
        raise PlanError("internal planner error: incremental source plan has no state")
    changes = tuple(
        change
        for change in repository.diff(state.last_commit, head)
        if is_source_managed(change.path, config.source)
    )
    upload_entries: list[GitEntry] = []
    for change in changes:
        if change.status != "D":
            entry = entries.get(change.path)
            if entry is None:
                raise PlanError(f"changed source is missing from HEAD: {change.path}")
            _require_regular_git_entry(entry)
            upload_entries.append(entry)
    manifests = (
        repository.blob_manifests(tuple(upload_entries))
        if require_content_identity
        else {}
    )
    for change in changes:
        if change.status == "D":
            operations.append(DeleteOperation(change.path, "source"))
        else:
            entry = entries.get(change.path)
            assert entry is not None
            content = manifests.get(change.path)
            operations.append(
                UploadOperation(
                    change.path,
                    "source",
                    git_path=change.path,
                    size=content.size if content is not None else entry.size,
                    executable=entry.mode == "100755",
                    sha256=content.sha256 if content is not None else None,
                )
            )
    return tuple(operations)


def _plan_outputs(
    mappings: tuple[OutputConfig, ...],
    current: dict[str, ScannedOutput],
    state: TargetState | None,
    full: bool,
) -> tuple[Operation, ...]:
    """Plan output hash changes and safe removals from the prior manifest only."""

    previous = state.outputs if state else {}
    operations: list[Operation] = []
    for remote, scanned in current.items():
        old = previous.get(remote)
        if full or old != scanned.entry:
            operations.append(
                UploadOperation(
                    remote,
                    "output",
                    local_path=scanned.local_path,
                    size=scanned.entry.size,
                    sha256=scanned.entry.sha256,
                )
            )
    if not full:
        for remote in sorted(previous.keys() - current.keys()):
            if _delete_removed_for(remote, mappings):
                operations.append(DeleteOperation(remote, "output"))
    return tuple(operations)


def _delete_removed_for(remote: str, mappings: tuple[OutputConfig, ...]) -> bool:
    """Return whether the mapping owning a previous output permits deletion."""

    path = PurePosixPath(remote)
    owners = [
        mapping
        for mapping in mappings
        if path == mapping.remote or mapping.remote in path.parents
    ]
    if len(owners) > 1:
        raise PlanError(f"overlapping output mappings own prior remote path: {remote}")
    return bool(owners and owners[0].delete_removed)


def _merge_operations(operations: tuple[Operation, ...], config: Config) -> tuple[Operation, ...]:
    """Apply final protection, collision checks, deduplication, and sorting."""

    merged: dict[str, Operation] = {}
    for operation in operations:
        remote = PurePosixPath(operation.remote_path)
        normalized = remote.as_posix().strip("/")
        if remote.is_absolute() or ".." in remote.parts or not normalized:
            raise PlanError(f"unsafe remote operation path: {operation.remote_path!r}")
        if is_protected(normalized, config.source):
            raise PlanError(f"protected path reached final operation plan: {normalized}")
        prior = merged.get(normalized)
        normalized_operation: Operation
        if isinstance(operation, UploadOperation):
            normalized_operation = UploadOperation(
                normalized,
                operation.origin,
                operation.local_path,
                operation.git_path,
                operation.size,
                operation.executable,
                operation.sha256,
            )
        else:
            normalized_operation = DeleteOperation(normalized, operation.origin)
        if prior is not None and prior != normalized_operation:
            raise PlanError(
                f"remote path conflict between {prior.origin} and {operation.origin}: {normalized}"
            )
        merged[normalized] = normalized_operation
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (0 if isinstance(item, UploadOperation) else 1, item.remote_path),
        )
    )


def _validate_hybrid_conflicts(
    config: Config,
    local: HybridLocalManifest,
    source_owned: set[str],
    incremental_paths: set[str],
) -> None:
    """Reject every local ownership overlap before a remote connection.

    Args:
        config: Source protection policy and output mappings.
        local: Current Hybrid direct-child ownership proposal.
        source_owned: Current committed Source paths.
        incremental_paths: Current Incremental Output paths.

    Returns:
        ``None`` only when Hybrid ownership is disjoint and unprotected.
    """

    owned = set(local.names)
    protected = sorted(name for name in owned if is_protected(name, config.source))
    if protected:
        raise PlanError(
            "hybrid output cannot own protected direct path(s): " + ", ".join(protected)
        )
    source_conflicts = sorted(
        path for path in source_owned if PurePosixPath(path).parts[0] in owned
    )
    if source_conflicts:
        raise PlanError(
            "source/hybrid ownership conflict: " + ", ".join(source_conflicts[:5])
        )
    output_conflicts = sorted(
        path for path in incremental_paths if PurePosixPath(path).parts[0] in owned
    )
    if output_conflicts:
        raise PlanError(
            "incremental/hybrid ownership conflict: " + ", ".join(output_conflicts[:5])
        )


def _reject_historical_transfer(plan: DeploymentPlan, historical: set[str]) -> None:
    """Reject implicit ownership transfer from Hybrid to Source/Incremental.

    Args:
        plan: Local plan retaining every current non-Hybrid owned path.
        historical: Remotely owned names absent from current Hybrid local output.

    Returns:
        ``None`` only when no other local owner has claimed the historical prefix.
    """

    conflicts = sorted(
        path
        for path in plan.non_hybrid_owned
        if PurePosixPath(path).parts and PurePosixPath(path).parts[0] in historical
    )
    if conflicts:
        raise PlanError(
            "hybrid ownership cannot transfer automatically to another mapping: "
            + ", ".join(conflicts[:5])
        )


def _require_regular_git_entry(entry: GitEntry) -> None:
    """Reject symlinks and submodules that transports cannot preserve safely."""

    if entry.mode not in {"100644", "100755"}:
        raise PlanError(f"unsupported Git entry mode {entry.mode} for {entry.path!r}")
