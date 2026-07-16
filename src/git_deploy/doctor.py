"""Focused v1-lite configuration, build, Git, state, and target checks."""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from git_deploy.config import Config, TargetConfig, resolve_target_for_plan
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.transports import create_transport
from git_deploy.transports.base import Transport
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
) -> tuple[DoctorResult, ...]:
    """Run local checks then connect once to validate the selected remote root.

    Args:
        config: Validated project configuration.
        target: Selected deployment target.
        repository: Git worktree reader.
        state_store: Lightweight target-state reader.
        transport_factory: Injectable adapter factory for tests.

    Returns:
        Ordered diagnostic results; callers decide the exit code.
    """

    results: list[DoctorResult] = [DoctorResult("config", True, str(config.path))]
    resolved_target = target
    try:
        resolved_target = resolve_target_for_plan(target, runtime_dir=state_store.base)
    except Exception as exc:
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
    try:
        state = state_store.load(target.name)
        results.append(
            DoctorResult("state", True, "first deployment" if state is None else state.last_commit)
        )
    except Exception as exc:
        results.append(DoctorResult("state", False, str(exc)))
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
    except Exception as exc:
        results.append(DoctorResult("target", False, str(exc)))
    finally:
        if "transport" in locals():
            transport.close()
    return tuple(results)


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
