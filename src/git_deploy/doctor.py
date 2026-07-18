"""Focused v1-lite configuration, build, Git, state, and target checks."""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from git_deploy.config import Config, TargetConfig, resolve_target_for_plan
from git_deploy.errors import PlanError
from git_deploy.ftp_hybrid import (
    load_capability_profile,
    probe_ftp_hybrid_capabilities,
    read_pending,
    save_capability_profile,
    scan_ftp_tree,
    validate_pending_ownership_phase,
)
from git_deploy.git import GitRepository
from git_deploy.hybrid import (
    HybridLocalManifest,
    inspect_recovery,
    read_ownership,
    read_ownership_snapshot,
    read_recovery_records,
    scan_hybrid_output,
)
from git_deploy.manifest import StateStore
from git_deploy.transports import create_transport
from git_deploy.transports.base import RemotePathType, Transport
from git_deploy.transports.openssh_sftp import OpenSSHSFTPTransport
from git_deploy.transports.ftp import FTPTransport


@dataclass(frozen=True, slots=True)
class DoctorResult:
    """Contain one named diagnostic result."""

    name: str
    ok: bool
    detail: str


def run_doctor(
    config: Config,
    target: TargetConfig,
    repository: GitRepository,
    state_store: StateStore,
    *,
    create_root: bool = False,
    probe_ftp_hybrid: bool = False,
    transport_factory=create_transport,  # noqa: ANN001
    pre_resolved_target: TargetConfig | None = None,
    resolution_error: str | None = None,
    remote_checks: bool = True,
) -> tuple[DoctorResult, ...]:
    """Run local checks then connect once to validate the selected remote root.

    Args:
        config: Validated project configuration.
        target: Selected deployment target.
        repository: Git worktree reader.
        state_store: Lightweight target-state reader.
        create_root: Whether Doctor may create a missing configured root.
        probe_ftp_hybrid: Whether the user explicitly confirmed a mutating FTP probe.
        transport_factory: Injectable adapter factory for tests.
        pre_resolved_target: Workspace-frozen target resolved before any connection.
        resolution_error: Preflight failure that must short-circuit remote checks.
        remote_checks: Whether this invocation may connect after local diagnostics.

    Returns:
        Ordered diagnostic results; callers decide the exit code.
    """

    results: list[DoctorResult] = [DoctorResult("config", True, str(config.path))]
    resolved_target = pre_resolved_target or target
    resolution_ok = resolution_error is None
    if resolution_error is not None:
        results.append(DoctorResult("target config", False, resolution_error))
    elif pre_resolved_target is None:
        try:
            resolved_target = resolve_target_for_plan(target, runtime_dir=state_store.base)
        except Exception as exc:
            resolution_ok = False
            results.append(DoctorResult("target config", False, str(exc)))
    try:
        repository.validate()
        results.append(DoctorResult("git", True, repository.head()))
    except Exception as exc:
        results.append(DoctorResult("git", False, str(exc)))
    missing = _missing_build_commands(config)
    results.append(
        DoctorResult(
            "build commands",
            not missing,
            "available" if not missing else "missing: " + ", ".join(missing),
        )
    )
    missing_outputs = [str(item.local) for item in config.outputs if not item.local.exists()]
    results.append(
        DoctorResult(
            "outputs",
            True,
            "paths valid" if not missing_outputs else "not built yet: " + ", ".join(missing_outputs),
        )
    )
    hybrid_output = next((item for item in config.outputs if item.mode == "hybrid"), None)
    if hybrid_output is not None:
        results.append(
            DoctorResult(
                "hybrid project id",
                config.project_id is not None,
                config.project_id or "missing",
            )
        )
        try:
            ignored = repository.is_ignored(hybrid_output.local)
            ignore_detail = "ignored" if ignored else "add '.deploy/' to .gitignore"
        except Exception as exc:
            ignored = False
            ignore_detail = str(exc)
        results.append(
            DoctorResult(
                "hybrid git ignore",
                ignored,
                ignore_detail,
            )
        )
        try:
            local_hybrid = scan_hybrid_output(hybrid_output)
            results.append(
                DoctorResult(
                    "hybrid local root",
                    True,
                    f"{len(local_hybrid.root_files)} root file(s), "
                    f"{len(local_hybrid.directories)} mirror directory(s)",
                )
            )
        except Exception as exc:
            local_hybrid = None
            results.append(DoctorResult("hybrid local root", False, str(exc)))
    try:
        state = state_store.load(target.name)
        results.append(
            DoctorResult("state", True, "first deployment" if state is None else state.last_commit)
        )
    except Exception as exc:
        results.append(DoctorResult("state", False, str(exc)))
    if not resolution_ok:
        return tuple(results)
    if not remote_checks:
        results.append(
            DoctorResult(
                "target",
                False,
                "remote checks skipped because workspace preflight failed",
            )
        )
        return tuple(results)
    try:
        transport: Transport = transport_factory(resolved_target)
        if isinstance(transport, OpenSSHSFTPTransport):
            results.append(DoctorResult("SSH backend", True, "Native OpenSSH"))
            results.append(
                DoctorResult(
                    "SSH alias",
                    True,
                    resolved_target.ssh_host_alias or "<missing>",
                )
            )
            results.append(
                DoctorResult(
                    "authentication",
                    True,
                    "connection may request authorization from your configured SSH Agent",
                )
            )
        else:
            results.append(DoctorResult("SSH backend", True, target.protocol.upper()))
        transport.connect()
        if isinstance(transport, OpenSSHSFTPTransport) and transport.master is not None:
            results.append(DoctorResult("SSH executable", True, transport.master.ssh))
            results.append(DoctorResult("SFTP executable", True, transport.master.sftp))
            results.append(
                DoctorResult(
                    "SSH endpoint",
                    True,
                    f"{resolved_target.username}@{resolved_target.host}:{resolved_target.port}",
                )
            )
        exists = transport.root_exists()
        if not exists and create_root:
            transport.ensure_root()
            exists = True
        results.append(
            DoctorResult(
                "target",
                exists,
                (
                    f"connected; root ready: {resolved_target.remote_root}"
                    if exists
                    else f"connected; root missing: {resolved_target.remote_root}"
                ),
            )
        )
        if exists and hybrid_output is not None and config.project_id is not None:
            if resolved_target.protocol == "ftp":
                if not isinstance(transport, FTPTransport):
                    raise TypeError("FTP Hybrid Doctor requires FTPTransport semantics")
                if probe_ftp_hybrid:
                    profile = probe_ftp_hybrid_capabilities(transport, resolved_target)
                    profile_path = save_capability_profile(state_store.base, profile)
                    results.append(
                        DoctorResult(
                            "FTP Hybrid Capability Probe",
                            True,
                            f"supported; profile saved: {profile_path}",
                        )
                    )
                profile = load_capability_profile(
                    state_store.base,
                    resolved_target,
                    server_banner_hash=transport.server_banner_hash(),
                )
                results.append(
                    DoctorResult(
                        "FTP Hybrid Capability Profile",
                        True,
                        f"valid; probed_at={profile.probed_at}",
                    )
                )
                results.append(
                    DoctorResult(
                        "FTP Server Features",
                        profile.mlsd and profile.case_sensitive_paths,
                        "case-sensitive paths, MLSD, RETR, cross-directory rename, "
                        "rename replace, DELE, RMD",
                    )
                )
                _append_ftp_hybrid_remote_results(
                    results,
                    transport,
                    resolved_target,
                    config.project_id,
                    hybrid_output.name or "<missing>",
                    local_hybrid,
                )
            else:
                _append_hybrid_remote_results(
                    results,
                    transport,
                    resolved_target,
                    config.project_id,
                    hybrid_output.name or "<missing>",
                    local_hybrid,
                )
    except Exception as exc:
        results.append(DoctorResult("target", False, str(exc)))
    finally:
        if "transport" in locals():
            transport.close()
    return tuple(results)


def _append_ftp_hybrid_remote_results(
    results: list[DoctorResult],
    transport: FTPTransport,
    target: TargetConfig,
    project_id: str,
    mapping: str,
    local_hybrid: HybridLocalManifest | None,
) -> None:
    """Append read-only FTP Ownership, Pending, types, and scan-boundary checks."""

    try:
        internal_type = transport.lstat(".git-deploy")
        internal_ok = internal_type in {RemotePathType.MISSING, RemotePathType.DIRECTORY}
        results.append(
            DoctorResult(
                "FTP Hybrid Remote Internal Paths",
                internal_ok,
                internal_type.value,
            )
        )
        if not internal_ok:
            return
        ownership, ownership_snapshot = read_ownership_snapshot(
            transport,
            project_id=project_id,
            mapping=mapping,
            remote=".",
        )
        results.append(
            DoctorResult(
                "FTP Hybrid Ownership",
                True,
                "first deployment" if ownership is None else ownership.last_commit,
            )
        )
        pending = read_pending(
            transport,
            project_id=project_id,
            mapping=mapping,
            remote=".",
            target=target,
        )
        results.append(
            DoctorResult(
                "FTP Hybrid Pending Resume",
                pending is None,
                "none" if pending is None else f"pending: {pending.phase.value}",
            )
        )
        if pending is not None:
            try:
                validate_pending_ownership_phase(pending, ownership_snapshot)
            except PlanError as exc:
                results.append(
                    DoctorResult(
                        "FTP Hybrid Pending Ownership Matrix",
                        False,
                        f"manual inspection required: {exc}",
                    )
                )
            else:
                results.append(
                    DoctorResult(
                        "FTP Hybrid Pending Ownership Matrix",
                        True,
                        f"consistent with {pending.phase.value}",
                    )
                )
        stage_parent = ".git-deploy/ftp-hybrid/stage"
        stage_type = transport.lstat(stage_parent)
        orphan_details: list[str] = []
        if stage_type is RemotePathType.DIRECTORY:
            active_id = pending.deployment_id if pending is not None else None
            for entry in transport.list_directory_typed(stage_parent):
                if entry.path == active_id:
                    continue
                stage_path = f"{stage_parent}/{entry.path}"
                if entry.kind is RemotePathType.DIRECTORY:
                    tree = scan_ftp_tree(transport, stage_path)
                    entry_count = len(tree.files) + len(tree.directories)
                else:
                    entry_count = 1
                orphan_details.append(
                    f"{entry.path} (age={_ftp_stage_age(entry.modify)}, "
                    f"entries={entry_count})"
                )
        elif stage_type is not RemotePathType.MISSING:
            orphan_details.append(f"stage-parent ({stage_type.value})")
        results.append(
            DoctorResult(
                "FTP Hybrid Orphan Stage",
                not orphan_details,
                "none" if not orphan_details else ", ".join(orphan_details),
            )
        )
        if local_hybrid is None:
            return
        old_files = set(ownership.root_files if ownership else ())
        old_directories = set(ownership.directories if ownership else ())
        current_files = set(local_hybrid.root_file_names)
        current_directories = set(local_hybrid.directory_names)
        unsafe: list[str] = []
        adoption: list[str] = []
        for name in sorted(old_files | old_directories | current_files | current_directories):
            kind = transport.lstat(name)
            if name in current_files and kind not in {RemotePathType.MISSING, RemotePathType.FILE}:
                unsafe.append(f"{name} ({kind.value}, expected file)")
            if name in current_directories and kind not in {
                RemotePathType.MISSING,
                RemotePathType.DIRECTORY,
            }:
                unsafe.append(f"{name} ({kind.value}, expected directory)")
            if name in (current_files | current_directories) - (old_files | old_directories):
                if kind is not RemotePathType.MISSING:
                    adoption.append(name)
        scan_entries = 0
        for name in sorted(current_directories | old_directories):
            if transport.lstat(name) is RemotePathType.DIRECTORY:
                tree = scan_ftp_tree(transport, name)
                scan_entries += len(tree.files) + len(tree.directories)
        results.append(
            DoctorResult(
                "FTP Hybrid Remote Types",
                not unsafe,
                "safe" if not unsafe else ", ".join(unsafe),
            )
        )
        results.append(
            DoctorResult(
                "FTP Hybrid Adoption",
                not adoption,
                "not required" if not adoption else "--full required: " + ", ".join(adoption),
            )
        )
        results.append(
            DoctorResult(
                "FTP Hybrid Remote Scan Boundary",
                True,
                f"{len(current_directories | old_directories)} managed root(s), "
                f"{scan_entries} descendant(s)",
            )
        )
    except Exception as exc:
        results.append(DoctorResult("FTP Hybrid Remote", False, str(exc)))


def _append_hybrid_remote_results(
    results: list[DoctorResult],
    transport: Transport,
    target: TargetConfig,
    project_id: str,
    mapping: str,
    local_hybrid: HybridLocalManifest | None,
) -> None:
    """Append read-only Ownership, Recovery, and owned-path diagnostics.

    Args:
        results: Mutable ordered doctor result list.
        transport: Connected SFTP adapter.
        target: Frozen physical target.
        project_id: Expected credential-free project identity.
        mapping: Hybrid mapping name.
        local_hybrid: Optional successfully scanned local Hybrid manifest.

    Returns:
        ``None`` after diagnostics; callers own connection cleanup.
    """

    try:
        internal_type = transport.lstat(".git-deploy")
        internal_ok = internal_type in {RemotePathType.MISSING, RemotePathType.DIRECTORY}
        if internal_type is RemotePathType.DIRECTORY:
            transport.list_directory(".git-deploy")
        results.append(
            DoctorResult(
                "hybrid internal directory",
                internal_ok,
                "absent (created on first deploy)"
                if internal_type is RemotePathType.MISSING
                else f"{internal_type.value}; readable",
            )
        )
        if not internal_ok:
            return
        records = read_recovery_records(
            transport,
            mapping=mapping,
            target_fingerprint=target.fingerprint,
        )
        try:
            outcomes = tuple(inspect_recovery(transport, record) for record in records)
        except Exception as exc:
            results.append(
                DoctorResult(
                    "hybrid recovery",
                    False,
                    f"manual inspection required: {exc}",
                )
            )
            return
        results.append(
            DoctorResult(
                "hybrid recovery",
                not records,
                "none"
                if not records
                else "pending: "
                + ", ".join(
                    "commands"
                    if outcome.commands_pending
                    else "cleanup"
                    if outcome.ownership_committed
                    else "restore"
                    for outcome in outcomes
                ),
            )
        )
        ownership = read_ownership(
            transport,
            project_id=project_id,
            mapping=mapping,
            remote=".",
        )
        results.append(
            DoctorResult(
                "hybrid ownership",
                True,
                "first deployment" if ownership is None else ownership.last_commit,
            )
        )
        if local_hybrid is None:
            return
        owned = set(ownership.directories if ownership else ()) | set(
            ownership.root_files if ownership else ()
        )
        unsafe: list[str] = []
        adoption: list[str] = []
        for name in sorted(owned | set(local_hybrid.names)):
            kind = transport.lstat(name)
            if kind in {RemotePathType.SYMLINK, RemotePathType.OTHER}:
                unsafe.append(f"{name} ({kind.value})")
            if name in local_hybrid.names and name not in owned and kind is not RemotePathType.MISSING:
                adoption.append(name)
        results.append(
            DoctorResult(
                "hybrid owned path types",
                not unsafe,
                "safe" if not unsafe else ", ".join(unsafe),
            )
        )
        results.append(
            DoctorResult(
                "hybrid adoption",
                not adoption,
                "not required" if not adoption else "--full required: " + ", ".join(adoption),
            )
        )
    except Exception as exc:
        results.append(DoctorResult("hybrid remote", False, str(exc)))


def _missing_build_commands(config: Config) -> tuple[str, ...]:
    """Return simple executable names that cannot be found on PATH."""

    missing: list[str] = []
    for step in config.build.steps:
        try:
            tokens = shlex.split(step)
        except ValueError:
            missing.append(f"invalid shell syntax: {step}")
            continue
        command = next((token for token in tokens if "=" not in token), None)
        if command and command not in {"cd", "export", "if", "for", "while"}:
            available = (
                (config.project_root / command).is_file()
                if "/" in command
                else shutil.which(command) is not None
            )
            if not available:
                missing.append(command)
    return tuple(dict.fromkeys(missing))


def _ftp_stage_age(modify: str | None) -> str:
    """Render a conservative age for one optional MLSD ``modify`` timestamp.

    Args:
        modify: UTC ``YYYYMMDDhhmmss`` fact, optionally with fractional seconds.

    Returns:
        Whole seconds such as ``42s``, or ``unknown`` for absent/invalid facts.
    """

    if modify is None:
        return "unknown"
    try:
        modified = datetime.strptime(modify.split(".", 1)[0], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - modified).total_seconds()))
    return f"{seconds}s"
