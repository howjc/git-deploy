"""Local Hybrid manifests and strict remote ownership/recovery records."""

from __future__ import annotations

import hashlib
import json
import stat
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from git_deploy.config import OutputConfig, TargetConfig
from git_deploy.errors import DeployError, PlanError
from git_deploy.manifest import ManifestEntry, ScannedOutput, hash_file
from git_deploy.transports.base import (
    RemotePathType,
    Transport,
    is_stable_remote_component,
)

OWNERSHIP_SCHEMA = 1
RECOVERY_SCHEMA = 2
MAX_REMOTE_RECORD_BYTES = 64 * 1024


class HybridBackend(str, Enum):
    """Select the protocol-specific Hybrid safety and execution contract."""

    SFTP_STAGED = "sftp-staged"
    FTP_IN_PLACE = "ftp-in-place"


def resolve_hybrid_backend(target: TargetConfig) -> HybridBackend:
    """Resolve one target protocol to its explicit Hybrid backend.

    Args:
        target: Validated deployment target.

    Returns:
        Backend whose guarantees must be used by planning and execution.
    """

    if target.protocol == "sftp":
        return HybridBackend.SFTP_STAGED
    if target.protocol == "ftp":
        return HybridBackend.FTP_IN_PLACE
    raise PlanError(f"unsupported protocol for Hybrid output: {target.protocol!r}")


@dataclass(frozen=True, slots=True)
class HybridRootFile:
    """Describe one direct regular file in a Local Aggregation Root."""

    name: str
    local_path: Path
    entry: ManifestEntry


@dataclass(frozen=True, slots=True)
class HybridDirectoryManifest:
    """Describe one direct Mirror Directory and all regular descendants."""

    name: str
    local_root: Path
    files: dict[str, ScannedOutput]
    directories: tuple[str, ...]
    file_count: int
    total_size: int


@dataclass(frozen=True, slots=True)
class HybridLocalManifest:
    """Contain the stable direct-child view of one Local Aggregation Root."""

    mapping: str
    remote: str
    root_files: tuple[HybridRootFile, ...]
    directories: tuple[HybridDirectoryManifest, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """Return every owned direct-child candidate in stable order."""

        return tuple(sorted((*self.root_file_names, *self.directory_names)))

    @property
    def root_file_names(self) -> tuple[str, ...]:
        """Return current direct-file names in stable order."""

        return tuple(item.name for item in self.root_files)

    @property
    def directory_names(self) -> tuple[str, ...]:
        """Return current direct-directory names in stable order."""

        return tuple(item.name for item in self.directories)


@dataclass(frozen=True, slots=True)
class HybridOwnership:
    """Record which remote direct children one Hybrid Mapping may delete."""

    schema: int
    project_id: str
    mapping: str
    remote: str
    directories: tuple[str, ...]
    root_files: tuple[str, ...]
    last_commit: str
    updated_at: int


class RecoveryPhase(str, Enum):
    """Track the narrow Stage/Swap lifecycle for one Hybrid deployment."""

    PREPARED = "PREPARED"
    STAGED = "STAGED"
    SWAPPING = "SWAPPING"
    RESTORED = "RESTORED"
    OWNERSHIP_COMMITTED = "OWNERSHIP_COMMITTED"
    COMMANDS_COMPLETE = "COMMANDS_COMPLETE"
    STATE_COMPLETE = "STATE_COMPLETE"
    CLEANUP_COMPLETE = "CLEANUP_COMPLETE"


@dataclass(frozen=True, slots=True)
class HybridRecoveryRecord:
    """Persist enough information to restore or finish one interrupted swap."""

    schema: int
    deployment_id: str
    mapping: str
    target_fingerprint: str
    stage_root: str
    backup_root: str
    phase: RecoveryPhase
    old_ownership_hash: str
    new_ownership_hash: str
    backup_names: tuple[str, ...]
    old_existing_names: tuple[str, ...]
    completed_names: tuple[str, ...] = ()
    active_name: str | None = None
    command_hash: str | None = None

    def with_phase(self, phase: RecoveryPhase) -> HybridRecoveryRecord:
        """Return an immutable record advanced to one lifecycle phase.

        Args:
            phase: New durable execution phase.

        Returns:
            Copy retaining deployment identity and recovery paths.
        """

        return replace(self, phase=phase)

    def starting(self, name: str) -> HybridRecoveryRecord:
        """Return a record durably identifying the next path mutation.

        Args:
            name: Direct owned path whose backup/swap is about to start.

        Returns:
            Copy with the prior active path completed and this path active.
        """

        completed = self.completed_names
        if self.active_name is not None:
            completed = tuple(sorted((*completed, self.active_name)))
        return replace(self, completed_names=completed, active_name=name)

    def without_active(self) -> HybridRecoveryRecord:
        """Return a record proving the active path was not mutated.

        Returns:
            Copy retaining completed paths and clearing only the active path.
        """

        return replace(self, active_name=None)

@dataclass(frozen=True, slots=True)
class HybridRecoveryOutcome:
    """Describe read-only recovery work still required after interruption."""

    ownership_committed: bool
    commands_pending: bool
    state_pending: bool
    cleanup_pending: bool


def scan_hybrid_output(output: OutputConfig) -> HybridLocalManifest:
    """Scan one Hybrid Aggregation Root without following symbolic links.

    Args:
        output: Validated Hybrid Output mapping.

    Returns:
        Stable direct files and recursive Mirror Directory manifests.
    """

    if output.mode != "hybrid" or output.name is None:
        raise PlanError("internal error: hybrid scanner received an incremental output")
    root = output.local
    if not root.exists():
        raise PlanError(f"configured hybrid output does not exist after build: {root}")
    if root.is_symlink() or not root.is_dir():
        raise PlanError(f"hybrid output must be a regular directory without symlinks: {root}")
    root_files: list[HybridRootFile] = []
    directories: list[HybridDirectoryManifest] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise PlanError(f"cannot scan hybrid output {root}: {exc}") from exc
    for child in children:
        _validate_direct_name(child.name, root)
        kind = child.lstat().st_mode
        if stat.S_ISLNK(kind):
            raise PlanError(f"hybrid output does not support symlinks: {child}")
        if stat.S_ISREG(kind):
            root_files.append(HybridRootFile(child.name, child, hash_file(child)))
        elif stat.S_ISDIR(kind):
            directories.append(_scan_hybrid_directory(child))
        else:
            raise PlanError(f"hybrid output contains an unsupported file type: {child}")
    return HybridLocalManifest(
        output.name,
        output.remote.as_posix(),
        tuple(root_files),
        tuple(directories),
    )


def make_ownership(
    local: HybridLocalManifest,
    project_id: str,
    commit: str,
    *,
    now: int | None = None,
) -> HybridOwnership:
    """Build the exact next Remote Ownership Manifest from local facts.

    Args:
        local: Frozen current Hybrid direct-child view.
        project_id: Stable credential-free project identity.
        commit: Exact source commit paired with the deployment.
        now: Optional deterministic Unix timestamp for tests.

    Returns:
        Schema-1 ownership record.
    """

    return HybridOwnership(
        OWNERSHIP_SCHEMA,
        project_id,
        local.mapping,
        local.remote,
        local.directory_names,
        local.root_file_names,
        commit,
        int(time.time()) if now is None else now,
    )


def serialize_ownership(record: HybridOwnership) -> bytes:
    """Serialize one ownership record deterministically for atomic upload.

    Args:
        record: Validated ownership value.

    Returns:
        UTF-8 JSON ending in one newline.
    """

    payload = asdict(record)
    payload["directories"] = list(record.directories)
    payload["root_files"] = list(record.root_files)
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_ownership(
    data: bytes,
    *,
    project_id: str,
    mapping: str,
    remote: str,
) -> HybridOwnership:
    """Validate untrusted Remote Ownership bytes and bind their identity.

    Args:
        data: Bounded bytes read without following a remote symlink.
        project_id: Expected project identity.
        mapping: Expected Hybrid mapping name.
        remote: Expected mapping remote root.

    Returns:
        Strict schema-1 ownership record.
    """

    if len(data) > MAX_REMOTE_RECORD_BYTES:
        raise DeployError("remote hybrid ownership manifest exceeds the size limit")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"remote hybrid ownership manifest is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "project_id",
        "mapping",
        "remote",
        "directories",
        "root_files",
        "last_commit",
        "updated_at",
    }:
        raise DeployError("remote hybrid ownership manifest has invalid fields")
    if raw.get("schema") != OWNERSHIP_SCHEMA:
        raise DeployError("remote hybrid ownership manifest has an unsupported schema")
    for field, expected in (
        ("project_id", project_id),
        ("mapping", mapping),
        ("remote", remote),
    ):
        if raw.get(field) != expected:
            raise DeployError(f"remote hybrid ownership {field} mismatch")
    directories = _parse_names(raw.get("directories"), "directories")
    root_files = _parse_names(raw.get("root_files"), "root_files")
    if set(directories).intersection(root_files):
        raise DeployError("remote hybrid ownership assigns one path as both file and directory")
    commit = raw.get("last_commit")
    updated_at = raw.get("updated_at")
    if not isinstance(commit, str) or not commit:
        raise DeployError("remote hybrid ownership last_commit is invalid")
    if not isinstance(updated_at, int) or isinstance(updated_at, bool) or updated_at < 0:
        raise DeployError("remote hybrid ownership updated_at is invalid")
    return HybridOwnership(
        OWNERSHIP_SCHEMA,
        project_id,
        mapping,
        remote,
        directories,
        root_files,
        commit,
        updated_at,
    )


def ownership_path(mapping: str) -> str:
    """Return the protected relative path for one mapping's ownership file."""

    return f".git-deploy/hybrid/{mapping}.json"


def ownership_hash(record: HybridOwnership | None) -> str:
    """Return a stable SHA256 for a present or absent ownership record.

    Args:
        record: Existing/new ownership, or ``None`` before first ownership.

    Returns:
        Hex SHA256 over deterministic bytes or an empty byte string.
    """

    data = serialize_ownership(record) if record is not None else b""
    return hashlib.sha256(data).hexdigest()


def recovery_path(deployment_id: str) -> str:
    """Return the protected relative path for one Recovery Record."""

    return f".git-deploy/recovery/{deployment_id}.json"


def serialize_recovery(record: HybridRecoveryRecord) -> bytes:
    """Serialize one Recovery Record deterministically.

    Args:
        record: Validated recovery state.

    Returns:
        UTF-8 JSON matching the record's recovery schema.
    """

    payload = asdict(record)
    payload["phase"] = record.phase.value
    payload["backup_names"] = list(record.backup_names)
    payload["old_existing_names"] = list(record.old_existing_names)
    if record.schema >= 2:
        payload["completed_names"] = list(record.completed_names)
    else:
        payload.pop("completed_names")
        payload.pop("active_name")
        payload.pop("command_hash")
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_recovery(
    data: bytes,
    *,
    mapping: str,
    target_fingerprint: str,
) -> HybridRecoveryRecord:
    """Validate one untrusted Recovery Record and its target identity.

    Args:
        data: Bounded remote record bytes.
        mapping: Expected Hybrid mapping.
        target_fingerprint: Expected physical target binding.

    Returns:
        Strict immutable recovery state.
    """

    if len(data) > MAX_REMOTE_RECORD_BYTES:
        raise DeployError("remote hybrid recovery record exceeds the size limit")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"remote hybrid recovery record is invalid JSON: {exc}") from exc
    schema_1_fields = {
        "schema",
        "deployment_id",
        "mapping",
        "target_fingerprint",
        "stage_root",
        "backup_root",
        "phase",
        "old_ownership_hash",
        "new_ownership_hash",
        "backup_names",
        "old_existing_names",
    }
    schema_2_fields = {
        *schema_1_fields,
        "completed_names",
        "active_name",
        "command_hash",
    }
    if (
        not isinstance(raw, dict)
        or raw.get("schema") not in {1, RECOVERY_SCHEMA}
        or set(raw) != (schema_1_fields if raw.get("schema") == 1 else schema_2_fields)
    ):
        raise DeployError("remote hybrid recovery record has invalid fields or schema")
    if raw.get("mapping") != mapping or raw.get("target_fingerprint") != target_fingerprint:
        raise DeployError("remote hybrid recovery identity mismatch")
    deployment_id = raw.get("deployment_id")
    if not isinstance(deployment_id, str) or not _safe_component(deployment_id):
        raise DeployError("remote hybrid recovery deployment_id is invalid")
    expected_stage = f".git-deploy/stage/{deployment_id}"
    expected_backup = f".git-deploy/backup/{deployment_id}"
    if raw.get("stage_root") != expected_stage or raw.get("backup_root") != expected_backup:
        raise DeployError("remote hybrid recovery paths are invalid")
    try:
        phase = RecoveryPhase(raw.get("phase"))
    except (TypeError, ValueError) as exc:
        raise DeployError("remote hybrid recovery phase is invalid") from exc
    for field in ("old_ownership_hash", "new_ownership_hash"):
        value = raw.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise DeployError(f"remote hybrid recovery {field} is invalid")
    names = _parse_names(raw.get("backup_names"), "backup_names")
    old_existing = _parse_names(raw.get("old_existing_names"), "old_existing_names")
    if not set(old_existing).issubset(names):
        raise DeployError("remote hybrid recovery old-existing names are inconsistent")
    completed = (
        _parse_names(raw.get("completed_names"), "completed_names")
        if raw["schema"] == RECOVERY_SCHEMA
        else ()
    )
    active = raw.get("active_name") if raw["schema"] == RECOVERY_SCHEMA else None
    command_hash = raw.get("command_hash") if raw["schema"] == RECOVERY_SCHEMA else None
    if (
        not set(completed).issubset(names)
        or active is not None
        and (not isinstance(active, str) or active not in names or active in completed)
        or raw["schema"] == RECOVERY_SCHEMA
        and not _valid_sha256(command_hash)
    ):
        raise DeployError("remote hybrid recovery path progress is inconsistent")
    return HybridRecoveryRecord(
        raw["schema"],
        deployment_id,
        mapping,
        target_fingerprint,
        expected_stage,
        expected_backup,
        phase,
        raw["old_ownership_hash"],
        raw["new_ownership_hash"],
        names,
        old_existing,
        completed,
        active,
        command_hash,
    )


def recovery_command_hash(
    commands: tuple[str, ...],
    timeout: float | None,
) -> str:
    """Hash the reviewed remote command contract stored outside Recovery.

    Args:
        commands: Frozen validated after-deploy commands.
        timeout: Frozen per-command timeout.

    Returns:
        SHA256 of a deterministic command/timeout representation.
    """

    payload = json.dumps(
        {"commands": commands, "timeout": timeout},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_ownership(
    transport: Transport,
    *,
    project_id: str,
    mapping: str,
    remote: str,
) -> HybridOwnership | None:
    """Read and validate the one Remote Ownership Manifest when present.

    Args:
        transport: Connected SFTP transport.
        project_id: Expected project identity.
        mapping: Expected Hybrid mapping.
        remote: Expected mapping remote value.

    Returns:
        Valid ownership, or ``None`` for a confirmed first deployment.
    """

    ownership, _ = read_ownership_snapshot(
        transport,
        project_id=project_id,
        mapping=mapping,
        remote=remote,
    )
    return ownership


def read_ownership_snapshot(
    transport: Transport,
    *,
    project_id: str,
    mapping: str,
    remote: str,
) -> tuple[HybridOwnership | None, str]:
    """Read ownership and return the SHA256 of its exact approved bytes.

    Args:
        transport: Connected SFTP transport.
        project_id: Expected project identity.
        mapping: Expected Hybrid mapping name.
        remote: Expected mapping remote value.

    Returns:
        Parsed ownership (or ``None``) and raw-byte hash for freshness checks.
    """

    path = ownership_path(mapping)
    kind = transport.lstat(path)
    if kind is RemotePathType.MISSING:
        return None, hashlib.sha256(b"").hexdigest()
    if kind is not RemotePathType.FILE:
        raise DeployError(
            f"remote hybrid ownership manifest must be a regular file, not {kind.value}: {path}"
        )
    data = transport.read_file(path, max_bytes=MAX_REMOTE_RECORD_BYTES)
    return (
        parse_ownership(data, project_id=project_id, mapping=mapping, remote=remote),
        hashlib.sha256(data).hexdigest(),
    )


def validate_internal_paths(transport: Transport) -> None:
    """Reject symlink or non-directory Hybrid internal path components.

    Args:
        transport: Connected SFTP transport.

    Returns:
        ``None`` when every existing internal container is a real directory.
    """

    for path in (
        ".git-deploy",
        ".git-deploy/hybrid",
        ".git-deploy/stage",
        ".git-deploy/backup",
        ".git-deploy/recovery",
    ):
        kind = transport.lstat(path)
        if kind not in {RemotePathType.MISSING, RemotePathType.DIRECTORY}:
            raise DeployError(
                f"remote hybrid internal path must be a directory, not {kind.value}: {path}"
            )


def read_recovery_records(
    transport: Transport,
    *,
    mapping: str,
    target_fingerprint: str,
) -> tuple[HybridRecoveryRecord, ...]:
    """Read every bounded Recovery Record without mutating the remote.

    Args:
        transport: Connected SFTP transport.
        mapping: Expected single Hybrid mapping.
        target_fingerprint: Expected physical target identity.

    Returns:
        Stable validated records.
    """

    root = ".git-deploy/recovery"
    kind = transport.lstat(root)
    if kind is RemotePathType.MISSING:
        return ()
    if kind is not RemotePathType.DIRECTORY:
        raise DeployError("remote hybrid recovery path is not a directory")
    records: list[HybridRecoveryRecord] = []
    for name in transport.list_directory(root):
        if not name.endswith(".json") or name in {".json", "..json"}:
            raise DeployError(f"unexpected file in remote hybrid recovery directory: {name!r}")
        path = f"{root}/{name}"
        if transport.lstat(path) is not RemotePathType.FILE:
            raise DeployError(f"remote hybrid recovery record is not a regular file: {path}")
        record = parse_recovery(
            transport.read_file(path, max_bytes=MAX_REMOTE_RECORD_BYTES),
            mapping=mapping,
            target_fingerprint=target_fingerprint,
        )
        if name != f"{record.deployment_id}.json":
            raise DeployError("remote hybrid recovery filename does not match deployment_id")
        records.append(record)
    return tuple(sorted(records, key=lambda item: item.deployment_id))


def inspect_recovery(
    transport: Transport,
    record: HybridRecoveryRecord,
) -> HybridRecoveryOutcome:
    """Validate Recovery facts without changing any remote path.

    Args:
        transport: Connected SFTP transport used only for reads.
        record: Valid record bound to this mapping and physical target.

    Returns:
        Pending command/state/cleanup classification for confirmed execution.
    """

    actual_ownership_hash = _remote_ownership_hash(transport, record.mapping)
    if actual_ownership_hash not in {
        record.old_ownership_hash,
        record.new_ownership_hash,
    }:
        raise DeployError("cannot reconcile recovery because ownership hash is unknown")
    committed = actual_ownership_hash == record.new_ownership_hash
    phase_claims_commit = record.phase in {
        RecoveryPhase.OWNERSHIP_COMMITTED,
        RecoveryPhase.COMMANDS_COMPLETE,
        RecoveryPhase.STATE_COMPLETE,
        RecoveryPhase.CLEANUP_COMPLETE,
    }
    if phase_claims_commit and not committed:
        raise DeployError("recovery phase claims committed ownership but its hash is stale")
    if record.phase in {RecoveryPhase.PREPARED, RecoveryPhase.STAGED} and committed:
        raise DeployError("recovery phase precedes swap but ownership already changed")
    if record.phase is RecoveryPhase.SWAPPING and not committed:
        progressed = (
            set(record.completed_names) | ({record.active_name} if record.active_name else set())
            if record.schema >= 2
            else set(record.old_existing_names)
        )
        for name in set(record.old_existing_names) & progressed:
            backup = f"{record.backup_root}/{name}"
            if transport.lstat(backup) is RemotePathType.MISSING:
                raise DeployError(
                    "manual inspection required: recovery backup is missing for "
                    f"previously existing path {name!r}"
                )
        if record.schema == 1:
            for name in set(record.backup_names) - set(record.old_existing_names):
                backup = f"{record.backup_root}/{name}"
                if (
                    transport.lstat(backup) is RemotePathType.MISSING
                    and transport.lstat(name) is not RemotePathType.MISSING
                ):
                    raise DeployError(
                        "manual inspection required: legacy recovery cannot prove "
                        f"whether newly existing path {name!r} was deployed"
                    )
    if record.phase is RecoveryPhase.RESTORED and committed:
        raise DeployError("restored recovery unexpectedly has committed ownership")
    commands_pending = committed and record.phase in {
        RecoveryPhase.SWAPPING,
        RecoveryPhase.OWNERSHIP_COMMITTED,
    }
    if record.schema == 1 and commands_pending:
        raise DeployError(
            "legacy command contract unknown: schema-1 Recovery cannot prove "
            "the interrupted after_deploy commands or timeout"
        )
    state_pending = committed and record.phase in {
        RecoveryPhase.SWAPPING,
        RecoveryPhase.OWNERSHIP_COMMITTED,
        RecoveryPhase.COMMANDS_COMPLETE,
    }
    return HybridRecoveryOutcome(committed, commands_pending, state_pending, True)


def reconcile_recovery(
    transport: Transport,
    record: HybridRecoveryRecord,
) -> HybridRecoveryOutcome:
    """Restore a pre-commit swap or clean a post-commit interrupted deployment.

    Args:
        transport: Connected SFTP transport allowed to mutate recovery-owned paths.
        record: Valid record bound to this mapping and physical target.

    Returns:
        Read-only outcome. Pre-commit swaps are restored and removed; committed
        records remain for command/state continuation by the deployer.
    """

    outcome = inspect_recovery(transport, record)
    if outcome.ownership_committed:
        return outcome
    if record.phase is RecoveryPhase.SWAPPING:
        affected_names = (
            tuple(sorted(set(record.completed_names) | ({record.active_name} if record.active_name else set())))
            if record.schema >= 2
            else record.backup_names
        )
        for name in affected_names:
            backup = f"{record.backup_root}/{name}"
            if transport.lstat(backup) is RemotePathType.MISSING:
                staged = f"{record.stage_root}/{name}"
                if (
                    name not in record.old_existing_names
                    and transport.lstat(staged) is not RemotePathType.MISSING
                ):
                    # A no-overwrite publication did not consume Stage, so a
                    # same-name online path belongs to the external writer.
                    continue
                if transport.lstat(name) is not RemotePathType.MISSING:
                    transport.remove_tree(name)
                continue
            if transport.lstat(name) is not RemotePathType.MISSING:
                transport.remove_tree(name)
            transport.rename_path(backup, name)
        record = record.with_phase(RecoveryPhase.RESTORED)
        write_recovery(transport, record)
    transport.remove_tree(record.stage_root)
    transport.remove_tree(record.backup_root)
    transport.remove_tree(recovery_path(record.deployment_id))
    return outcome


def discard_staged_recovery(
    transport: Transport,
    record: HybridRecoveryRecord,
) -> None:
    """Discard only pre-swap internal artifacts after a secondary stale check.

    Args:
        transport: Connected SFTP transport allowed to remove internal paths.
        record: Current Recovery record that has not started online mutation.

    Returns:
        ``None`` after Stage, empty Backup, and Recovery metadata are removed.
    """

    if record.phase not in {RecoveryPhase.PREPARED, RecoveryPhase.STAGED}:
        raise DeployError("only a pre-swap Recovery may be discarded")
    transport.remove_tree(record.stage_root)
    transport.remove_tree(record.backup_root)
    transport.remove_tree(recovery_path(record.deployment_id))


def cleanup_committed_recovery(
    transport: Transport,
    record: HybridRecoveryRecord,
) -> None:
    """Remove committed Stage/Backup data and finally its Recovery Record.

    Args:
        transport: Connected SFTP transport allowed to mutate internal paths.
        record: Record already advanced to ``STATE_COMPLETE`` or later.

    Returns:
        ``None`` after durable cleanup; failures leave the record for retry.
    """

    if record.phase not in {
        RecoveryPhase.STATE_COMPLETE,
        RecoveryPhase.CLEANUP_COMPLETE,
    }:
        raise DeployError("committed recovery cleanup requires completed local state")
    transport.remove_tree(record.stage_root)
    transport.remove_tree(record.backup_root)
    if record.phase is not RecoveryPhase.CLEANUP_COMPLETE:
        record = record.with_phase(RecoveryPhase.CLEANUP_COMPLETE)
        write_recovery(transport, record)
    transport.remove_tree(recovery_path(record.deployment_id))


def _remote_ownership_hash(transport: Transport, mapping: str) -> str:
    """Hash exact current Ownership bytes without trusting their contents."""

    ownership_file = ownership_path(mapping)
    ownership_type = transport.lstat(ownership_file)
    if ownership_type is RemotePathType.MISSING:
        return hashlib.sha256(b"").hexdigest()
    if ownership_type is not RemotePathType.FILE:
        raise DeployError("cannot reconcile recovery because ownership path is not a file")
    return hashlib.sha256(
        transport.read_file(ownership_file, max_bytes=MAX_REMOTE_RECORD_BYTES)
    ).hexdigest()


def write_recovery(transport: Transport, record: HybridRecoveryRecord) -> None:
    """Atomically publish one current Recovery Record phase."""

    transport.write_file_atomic(recovery_path(record.deployment_id), serialize_recovery(record))


def _scan_hybrid_directory(root: Path) -> HybridDirectoryManifest:
    """Recursively scan one Mirror Directory without following symlinks.

    Args:
        root: Direct child of the aggregation root.

    Returns:
        Complete regular-file manifest, including an empty directory.
    """

    files: dict[str, ScannedOutput] = {}
    directories: list[str] = []

    def visit(directory: Path) -> None:
        """Visit one verified directory and accumulate regular files."""

        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise PlanError(f"cannot scan hybrid directory {directory}: {exc}") from exc
        for child in children:
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PlanError(f"hybrid output does not support symlinks: {child}")
            if stat.S_ISDIR(mode):
                relative = child.relative_to(root).as_posix()
                if any(
                    not _safe_component(part)
                    for part in PurePosixPath(relative).parts
                ):
                    raise PlanError(
                        f"hybrid output has an unsafe relative path: {child}"
                    )
                directories.append(relative)
                visit(child)
            elif stat.S_ISREG(mode):
                relative = child.relative_to(root).as_posix()
                if any(not _safe_component(part) for part in PurePosixPath(relative).parts):
                    raise PlanError(f"hybrid output has an unsafe relative path: {child}")
                files[relative] = ScannedOutput(child, hash_file(child))
            else:
                raise PlanError(f"hybrid output contains an unsupported file type: {child}")

    visit(root)
    ordered = dict(sorted(files.items()))
    return HybridDirectoryManifest(
        root.name,
        root,
        ordered,
        tuple(sorted(directories)),
        len(ordered),
        sum(item.entry.size for item in ordered.values()),
    )


def _validate_direct_name(name: str, root: Path) -> None:
    """Reject direct names that cannot be represented as one safe remote child."""

    if name in {".git", ".deploy", ".git-deploy"} or not _safe_component(name):
        raise PlanError(f"hybrid output has an unsafe direct child below {root}: {name!r}")


def _safe_component(value: str) -> bool:
    """Return whether text is one stable SFTP-representable path component."""

    return is_stable_remote_component(value)


def _valid_sha256(value: object) -> bool:
    """Return whether an untrusted value is one lowercase SHA256 digest.

    Args:
        value: Untrusted decoded JSON value.

    Returns:
        ``True`` only for exactly 64 lowercase hexadecimal characters.
    """

    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _parse_names(value: Any, field: str) -> tuple[str, ...]:
    """Validate a sorted unique JSON array of safe direct child names."""

    if not isinstance(value, list) or any(
        not isinstance(item, str) or not _safe_component(item) for item in value
    ):
        raise DeployError(f"remote hybrid {field} is invalid")
    names = tuple(value)
    if names != tuple(sorted(set(names))):
        raise DeployError(f"remote hybrid {field} must be sorted and unique")
    return names
