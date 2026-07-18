"""Merge committed source and output-manifest differences into one safe plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from git_deploy.config import (
    Config,
    OutputConfig,
    TargetConfig,
    is_protected,
    is_source_managed,
    resolve_target_for_plan,
)
from git_deploy.errors import PlanError, StaleRemotePlanError
from git_deploy.git import GitEntry, GitRepository
from git_deploy.hybrid import (
    HybridLocalManifest,
    HybridOwnership,
    HybridRecoveryRecord,
    RecoveryPhase,
    read_ownership_snapshot,
    read_recovery_records,
    recovery_command_hash,
    scan_hybrid_output,
    validate_internal_paths,
)
from git_deploy.manifest import ManifestEntry, ScannedOutput, TargetState, scan_outputs
from git_deploy.transports.base import RemotePathType, Transport

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
    ownership: HybridOwnership | None = None
    operations: tuple[HybridOperation, ...] = ()
    expected_ownership_hash: str | None = None
    expected_path_types: tuple[tuple[str, RemotePathType], ...] = ()
    recovery_records: tuple[HybridRecoveryRecord, ...] = ()
    remote_complete: bool = False


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
        return regular + hybrid_files + mirrored_files

    @property
    def delete_count(self) -> int:
        """Return the number of deletes in the operation queue."""

        regular = sum(isinstance(item, DeleteOperation) for item in self.operations)
        if self.hybrid is None:
            return regular
        return regular + sum(
            isinstance(item, (HybridRootFileDelete, HybridDirectoryDelete))
            for item in self.hybrid.operations
        )

    @property
    def adoption_count(self) -> int:
        """Return the number of existing paths explicitly adopted by ``--full``."""

        if self.hybrid is None:
            return 0
        return sum(isinstance(item, HybridAdoption) for item in self.hybrid.operations)

    @property
    def operation_count(self) -> int:
        """Return reviewed remote mutations excluding display-only Adoption rows."""

        hybrid_count = 0
        if self.hybrid is not None:
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
    source_operations = _plan_source(repository, config, state, head, effective_full, entries)
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
    output_operations = _plan_outputs(incremental_outputs, current_outputs, state, effective_full)
    operations = _merge_operations((*source_operations, *output_operations), config)
    manifest = {path: item.entry for path, item in current_outputs.items()}
    if hybrid_local is not None:
        manifest.update({item.name: item.entry for item in hybrid_local.root_files})
    return DeploymentPlan(
        target=resolved_target,
        target_fingerprint=target_fingerprint,
        head=head,
        previous_commit=state.last_commit if state else None,
        operations=operations,
        output_manifest=manifest,
        full=effective_full,
        allow_adoption=full,
        hybrid=(
            HybridPlan(hybrid_local, dict(state.outputs) if state is not None else {})
            if hybrid_local is not None
            else None
        ),
        non_hybrid_owned=tuple(sorted((*source_owned, *current_outputs.keys()))),
    )


def complete_remote_plan(
    plan: DeploymentPlan,
    config: Config,
    transport: Transport,
    *,
    allow_recovery: bool,
) -> DeploymentPlan:
    """Merge remote ownership/adoption facts into one frozen local plan.

    Args:
        plan: Local plan whose upload bytes are already frozen.
        config: Project identity and safety configuration.
        transport: Connected SFTP transport used only for preflight reads.
        allow_recovery: Retained for API compatibility; recovery is always planned
            read-only and requires explicit confirmed execution.

    Returns:
        Immutable full plan ready for confirmation or read-only display.
    """

    hybrid = plan.hybrid
    if hybrid is None:
        return plan
    if config.project_id is None:
        raise PlanError("hybrid output lacks a resolved project_id")
    del allow_recovery
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
        return replace(plan, hybrid=completed)
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


def render_plan(plan: DeploymentPlan) -> str:
    """Render a stable human-readable dry-run/deploy preview.

    Args:
        plan: Completed deployment plan.

    Returns:
        Multi-line summary containing every remote operation.
    """

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

    lines = [f"HYBRID Mapping: {hybrid.local.mapping} -> {hybrid.local.remote}"]
    if not hybrid.remote_complete:
        for item in hybrid.local.root_files:
            lines.append(f"LOCAL   [hybrid-file] {item.name} ({item.entry.size} byte(s))")
        for item in hybrid.local.directories:
            lines.append(
                f"LOCAL   [hybrid-mirror] {item.name}/ "
                f"({item.file_count} file(s), {item.total_size} byte(s))"
            )
        lines.append("REMOTE  [hybrid-owner] not read; use --remote-plan for full ownership plan")
        return tuple(lines)
    if hybrid.recovery_records:
        for record in hybrid.recovery_records:
            committed = hybrid.expected_ownership_hash == record.new_ownership_hash
            if committed and record.phase in {
                RecoveryPhase.SWAPPING,
                RecoveryPhase.OWNERSHIP_COMMITTED,
            }:
                action = "RESUME COMMANDS"
            elif committed and record.phase is RecoveryPhase.COMMANDS_COMPLETE:
                action = "SAVE STATE + CLEANUP"
            elif committed or record.phase in {
                RecoveryPhase.PREPARED,
                RecoveryPhase.STAGED,
                RecoveryPhase.RESTORED,
            }:
                action = "CLEANUP"
            else:
                action = "RESTORE"
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


def validate_remote_freshness(
    plan: DeploymentPlan,
    config: Config,
    transport: Transport,
) -> None:
    """Prove execution facts still equal the user-reviewed Remote Plan.

    Args:
        plan: Remote-complete immutable deployment plan.
        config: Expected project identity.
        transport: Connected transport used only for read-only validation.

    Returns:
        ``None`` when ownership, recovery, and path types remain unchanged.
    """

    hybrid = plan.hybrid
    if hybrid is None or not hybrid.remote_complete or config.project_id is None:
        raise PlanError("hybrid freshness validation requires a remote-complete plan")
    validate_internal_paths(transport)
    records = read_recovery_records(
        transport,
        mapping=hybrid.local.mapping,
        target_fingerprint=plan.target_fingerprint,
    )
    if records != hybrid.recovery_records:
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
    for path, expected in hybrid.expected_path_types:
        actual = transport.lstat(path)
        if actual is not expected:
            raise StaleRemotePlanError(
                f"remote path type changed after plan approval: {path!r} "
                f"({expected.value} -> {actual.value}); rerun and review the plan"
            )


def _plan_source(
    repository: GitRepository,
    config: Config,
    state: TargetState | None,
    head: str,
    full: bool,
    entries: dict[str, GitEntry],
) -> tuple[Operation, ...]:
    """Plan exact HEAD source blobs and Git-derived deletions."""

    operations: list[Operation] = []
    if full:
        for path, entry in entries.items():
            if is_source_managed(path, config.source):
                _require_regular_git_entry(entry)
                operations.append(
                    UploadOperation(
                        path,
                        "source",
                        git_path=path,
                        executable=entry.mode == "100755",
                    )
                )
        return tuple(operations)
    if state is None:
        raise PlanError("internal planner error: incremental source plan has no state")
    for change in repository.diff(state.last_commit, head):
        if not is_source_managed(change.path, config.source):
            continue
        if change.status == "D":
            operations.append(DeleteOperation(change.path, "source"))
        else:
            entry = entries.get(change.path)
            if entry is None:
                raise PlanError(f"changed source is missing from HEAD: {change.path}")
            _require_regular_git_entry(entry)
            operations.append(
                UploadOperation(
                    change.path,
                    "source",
                    git_path=change.path,
                    executable=entry.mode == "100755",
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
                UploadOperation(remote, "output", local_path=scanned.local_path, size=scanned.entry.size)
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
