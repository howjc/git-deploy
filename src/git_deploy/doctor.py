"""Focused v1-lite configuration, build, Git, state, and target checks."""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from git_deploy.config import Config, TargetConfig, resolve_target_for_plan
from git_deploy.git import GitRepository
from git_deploy.hybrid import (
    HybridLocalManifest,
    inspect_recovery,
    read_ownership,
    read_recovery_records,
    scan_hybrid_output,
)
from git_deploy.manifest import StateStore
from git_deploy.transports import create_transport
from git_deploy.transports.base import RemotePathType, Transport
from git_deploy.transports.openssh_sftp import OpenSSHSFTPTransport


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
