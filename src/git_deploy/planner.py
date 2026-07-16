"""Merge committed source and output-manifest differences into one safe plan."""

from __future__ import annotations

from dataclasses import dataclass
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
from git_deploy.errors import PlanError
from git_deploy.git import GitEntry, GitRepository
from git_deploy.manifest import ManifestEntry, ScannedOutput, TargetState, scan_outputs

Origin = Literal["source", "output"]


@dataclass(frozen=True, slots=True)
class UploadOperation:
    """Describe one committed source blob or local output to upload."""

    remote_path: str
    origin: Origin
    local_path: Path | None = None
    git_path: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class DeleteOperation:
    """Describe one previously owned remote path to delete."""

    remote_path: str
    origin: Origin


Operation = UploadOperation | DeleteOperation


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

    @property
    def upload_count(self) -> int:
        """Return the number of uploads in the operation queue."""

        return sum(isinstance(item, UploadOperation) for item in self.operations)

    @property
    def delete_count(self) -> int:
        """Return the number of deletes in the operation queue."""

        return sum(isinstance(item, DeleteOperation) for item in self.operations)


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
    current_outputs = scan_outputs(config.outputs)
    entries = {entry.path: entry for entry in repository.list_head_entries()}
    source_owned = {path for path in entries if is_source_managed(path, config.source)}
    overlap = sorted(source_owned.intersection(current_outputs))
    if overlap:
        preview = ", ".join(overlap[:5])
        suffix = " ..." if len(overlap) > 5 else ""
        raise PlanError(f"source/output ownership conflict: {preview}{suffix}")
    source_operations = _plan_source(repository, config, state, head, effective_full, entries)
    output_operations = _plan_outputs(config.outputs, current_outputs, state, effective_full)
    operations = _merge_operations((*source_operations, *output_operations), config)
    manifest = {path: item.entry for path, item in current_outputs.items()}
    return DeploymentPlan(
        target=resolved_target,
        target_fingerprint=target_fingerprint,
        head=head,
        previous_commit=state.last_commit if state else None,
        operations=operations,
        output_manifest=manifest,
        full=effective_full,
    )


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
    lines.append(f"Summary: {plan.upload_count} upload(s), {plan.delete_count} delete(s)")
    return "\n".join(lines)


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
                operations.append(UploadOperation(path, "source", git_path=path))
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
            operations.append(UploadOperation(change.path, "source", git_path=change.path))
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


def _require_regular_git_entry(entry: GitEntry) -> None:
    """Reject symlinks and submodules that transports cannot preserve safely."""

    if entry.mode not in {"100644", "100755"}:
        raise PlanError(f"unsupported Git entry mode {entry.mode} for {entry.path!r}")
