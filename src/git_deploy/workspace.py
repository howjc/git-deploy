"""Thin multi-repository orchestration with independent project state and locks."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from git_deploy.builder import run_build
from git_deploy.config import Config, TargetConfig, load_config, resolve_target_for_plan
from git_deploy.deployer import TransportFactory
from git_deploy.doctor import DoctorResult, run_doctor
from git_deploy.errors import ConfigError, PlanError
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.planner import UploadOperation, render_hybrid_plan
from git_deploy.prepared import PreparedDeployment, execute_prepared, prepare_project
from git_deploy.transports import create_transport
from git_deploy.transports.openssh_sftp import OpenSSHMaster, SSHConnectionPool

REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceRepository:
    """Name and locate one independent repository in deployment order."""

    name: str
    path: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Represent the intentionally thin workspace configuration."""

    path: Path
    root: Path
    default_target: str | None
    repositories: tuple[WorkspaceRepository, ...]

    def target(self, requested: str | None) -> str:
        """Return one target name shared by every repository."""

        target = requested or self.default_target
        if not target:
            raise ConfigError("workspace target is required because default_target is not set")
        return target


@dataclass(frozen=True, slots=True)
class WorkspacePreflight:
    """Bind one repository to its loaded config and frozen physical target."""

    repository: WorkspaceRepository
    config: Config
    target: TargetConfig


def load_workspace(path: Path) -> WorkspaceConfig:
    """Load a workspace containing only repository name/path/order.

    Args:
        path: Workspace TOML selected by discovery or ``--workspace``.

    Returns:
        Validated thin-workspace configuration in declared order.
    """

    path = path.expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"workspace file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read workspace {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("workspace root must be a table")
    unknown = sorted(set(raw) - {"default_target", "repositories"})
    if unknown:
        raise ConfigError(f"unknown workspace field(s): {', '.join(unknown)}")
    default = raw.get("default_target")
    if default is not None and (not isinstance(default, str) or not default.strip()):
        raise ConfigError("workspace default_target must be a non-empty string")
    entries = raw.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("workspace requires at least one [[repositories]] entry")
    root = path.parent.resolve()
    repositories: list[WorkspaceRepository] = []
    names: set[str] = set()
    paths: set[Path] = set()
    for index, value in enumerate(entries):
        if not isinstance(value, dict):
            raise ConfigError(f"repositories[{index}] must be a table")
        extra = sorted(set(value) - {"name", "path"})
        if extra:
            raise ConfigError(
                f"unknown repositories[{index}] field(s): {', '.join(extra)}"
            )
        name = _required_string(value.get("name"), f"repositories[{index}].name")
        if name in {".", ".."} or not REPOSITORY_NAME_PATTERN.fullmatch(name):
            raise ConfigError(
                f"repositories[{index}].name must match [A-Za-z0-9._-]+ "
                "and be at most 64 characters"
            )
        path_text = _required_string(value.get("path"), f"repositories[{index}].path")
        if name in names:
            raise ConfigError(f"duplicate workspace repository name: {name}")
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigError(f"repositories[{index}].path must stay inside the workspace")
        repository_path = (root / relative).resolve()
        if not repository_path.is_relative_to(root):
            raise ConfigError(f"repositories[{index}].path resolves outside the workspace")
        if repository_path in paths:
            raise ConfigError(f"duplicate workspace repository path: {repository_path}")
        if not repository_path.is_dir():
            raise ConfigError(f"workspace repository directory not found: {repository_path}")
        config_path = repository_path / "deploy.toml"
        if not config_path.is_file():
            raise ConfigError(f"repository {name!r} is missing deploy.toml: {config_path}")
        names.add(name)
        paths.add(repository_path)
        repositories.append(WorkspaceRepository(name, repository_path, config_path))
    return WorkspaceConfig(path, root, default.strip() if isinstance(default, str) else None, tuple(repositories))


def prepare_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    *,
    full: bool,
    skip_build: bool,
) -> tuple[str, tuple[PreparedDeployment, ...]]:
    """Prepare every repository before any remote connection can occur."""

    target, preflight = preflight_workspace(workspace, requested_target)
    prepared: list[PreparedDeployment] = []
    locks: list[TargetLock | None] = []
    try:
        # Acquire every independent repository lock before the first Build so a
        # later busy repository cannot waste or invalidate earlier preparation.
        for item in preflight:
            if item.target.runtime_dir is None:
                raise PlanError("workspace deployment preflight lacks a Git runtime directory")
            lock = TargetLock(item.target.runtime_dir, target)
            lock.acquire()
            locks.append(lock)
        for index, item in enumerate(preflight):
            print(f"Preparing {item.repository.name}...")
            prepared.append(
                prepare_project(
                    item.repository.name,
                    item.repository.config_path,
                    target,
                    full=full,
                    skip_build=skip_build,
                    prepared_config=item.config,
                    prepared_target=item.target,
                    prepared_lock=locks[index],
                )
            )
            locks[index] = None
        return target, tuple(prepared)
    except BaseException:
        for item in reversed(prepared):
            item.close()
        for lock in reversed(locks):
            if lock is not None:
                lock.release()
        raise


def render_workspace_plan(target: str, prepared: tuple[PreparedDeployment, ...]) -> str:
    """Render every repository operation and one deterministic total summary.

    Args:
        target: Shared logical target name.
        prepared: Fully frozen projects awaiting one confirmation.

    Returns:
        Combined plan including physical endpoints, commits, and frozen bytes.
    """

    lines = [f"Workspace Target: {target}"]
    uploads = 0
    deletes = 0
    commands = 0
    frozen_bytes = 0
    for item in prepared:
        lines.append("")
        lines.append(f"[{item.name}]")
        alias = (
            f"{item.plan.target.ssh_host_alias} -> "
            if item.plan.target.ssh_host_alias
            else ""
        )
        lines.append(
            "  Target: "
            f"{alias}{item.plan.target.username}@{item.plan.target.host}:"
            f"{item.plan.target.port}:{item.plan.target.remote_root} "
            f"({item.plan.target.protocol})"
        )
        lines.append(f"  Mode: {'FULL' if item.plan.full else 'INCREMENTAL'}")
        lines.append(
            f"  Commit: {item.plan.previous_commit or '<first deployment>'} -> "
            f"{item.plan.head}"
        )
        if not item.plan.operations and item.plan.hybrid is None:
            lines.append("  No changes")
        else:
            for operation in item.plan.operations:
                action = "UPLOAD" if isinstance(operation, UploadOperation) else "DELETE"
                lines.append(f"  {action:6} [{operation.origin}] {operation.remote_path}")
        if item.plan.hybrid is not None:
            lines.extend(f"  {line}" for line in render_hybrid_plan(item.plan.hybrid))
        if item.plan.has_remote_work:
            lines.extend(f"  AFTER  {command}" for command in item.plan.target.after_deploy)
        command_count = (
            len(item.plan.target.after_deploy) if item.plan.has_remote_work else 0
        )
        lines.append(
            f"  Summary: {item.plan.upload_count} upload(s), {item.plan.delete_count} delete(s), "
            f"{item.plan.adoption_count} adoption(s), "
            f"{command_count} after-deploy command(s)"
        )
        uploads += item.plan.upload_count
        deletes += item.plan.delete_count
        commands += command_count
        frozen_bytes += item.frozen_bytes
    lines.extend(
        (
            "",
            f"Total: {uploads} upload(s), {deletes} delete(s), "
            f"{commands} after-deploy command(s), {frozen_bytes} frozen byte(s)",
        )
    )
    return "\n".join(lines)


def execute_workspace(
    prepared: tuple[PreparedDeployment, ...],
    *,
    verbose: bool = False,
    transport_factory: TransportFactory | None = None,
    connection_pool: SSHConnectionPool | None = None,
) -> tuple[str, ...]:
    """Deploy repositories sequentially, sharing Native OpenSSH connections."""

    pool = connection_pool or SSHConnectionPool()
    owns_pool = connection_pool is None
    completed: list[str] = []
    try:
        for item in prepared:
            print(f"Deploying {item.name}...")
            execute_prepared(
                item,
                verbose=verbose,
                transport_factory=transport_factory,
                connection_pool=pool,
            )
            completed.append(item.name)
        return tuple(completed)
    finally:
        for item in prepared:
            item.close()
        if owns_pool:
            pool.close_all()


def run_workspace_build(workspace: WorkspaceConfig, requested_target: str | None) -> None:
    """Load every repository locally, validate an explicit target, and build.

    Args:
        workspace: Validated workspace and repository order.
        requested_target: Optional explicit target name to validate locally.

    Returns:
        ``None`` after all builds succeed.
    """

    loaded: list[tuple[WorkspaceRepository, Config]] = []
    for item in workspace.repositories:
        config = load_config(item.config_path)
        # Build configuration is not target-specific. Preserve the optional
        # positional target only as a typo check without resolving a remote.
        if requested_target is not None:
            config.target(requested_target)
        loaded.append((item, config))
    for item, config in loaded:
        print(f"Building {item.name}...")
        run_build(config.build, config.project_root)


def run_workspace_doctor(
    workspace: WorkspaceConfig,
    requested_target: str | None,
    *,
    create_root: bool,
) -> tuple[tuple[str, tuple[DoctorResult, ...]], ...]:
    """Run per-repository diagnostics with one shared Native OpenSSH pool.

    Args:
        workspace: Validated workspace and repository order.
        requested_target: Shared explicit/default target name.
        create_root: Whether all-preflight-success may create missing roots.

    Returns:
        Repository names paired with ordered diagnostic results.
    """

    target_name = workspace.target(requested_target)
    loaded: list[
        tuple[
            WorkspaceRepository,
            Config,
            GitRepository,
            StateStore,
            TargetConfig | None,
            str | None,
        ]
    ] = []
    for item in workspace.repositories:
        config = load_config(item.config_path)
        target = config.target(target_name)
        repository = GitRepository(config.project_root)
        try:
            common = repository.common_dir()
        except PlanError:
            common = config.project_root / ".git"
        store = StateStore(common)
        try:
            resolved = resolve_target_for_plan(target, runtime_dir=store.base)
            _validate_native_tools(resolved)
            error = None
        except Exception as exc:
            resolved = None
            error = str(exc)
        loaded.append((item, config, repository, store, resolved, error))
    preflight_error: str | None = None
    errors = [error for *_, error in loaded if error is not None]
    if not errors:
        try:
            _validate_remote_ownership(
                tuple(
                    WorkspacePreflight(item, config, resolved)
                    for item, config, _, _, resolved, _ in loaded
                    if resolved is not None
                )
            )
        except ConfigError as exc:
            preflight_error = str(exc)
    pool = SSHConnectionPool()
    results: list[tuple[str, tuple[DoctorResult, ...]]] = []
    try:
        workspace_failed = bool(errors or preflight_error)
        for item, config, repository, store, resolved, error in loaded:
            target = config.target(target_name)
            checks = run_doctor(
                config,
                target,
                repository,
                store,
                create_root=create_root,
                transport_factory=lambda selected: create_transport(selected, pool),
                pre_resolved_target=resolved,
                resolution_error=error or preflight_error,
                remote_checks=not workspace_failed,
            )
            results.append((item.name, checks))
        return tuple(results)
    finally:
        pool.close_all()


def preflight_workspace(
    workspace: WorkspaceConfig,
    requested_target: str | None,
) -> tuple[str, tuple[WorkspacePreflight, ...]]:
    """Resolve every physical target and ownership boundary before any build.

    Args:
        workspace: Parsed thin-workspace configuration.
        requested_target: Unified explicit/default target name.
    Returns:
        Shared target name and repository contexts in deployment order.
    """

    target_name = workspace.target(requested_target)
    contexts: list[WorkspacePreflight] = []
    for item in workspace.repositories:
        config = load_config(item.config_path)
        target = config.target(target_name)
        repository = GitRepository(config.project_root)
        repository.validate()
        runtime_dir = StateStore(repository.common_dir()).base
        resolved = resolve_target_for_plan(target, runtime_dir=runtime_dir)
        _validate_native_tools(resolved)
        contexts.append(WorkspacePreflight(item, config, resolved))
    result = tuple(contexts)
    _validate_remote_ownership(result)
    return target_name, result


def _validate_native_tools(target: TargetConfig) -> None:
    """Discover POSIX OpenSSH tools during preflight without connecting.

    Args:
        target: Frozen target that may select the Native backend.

    Returns:
        ``None`` when required local commands are valid or not applicable.
    """

    if target.protocol == "sftp" and target.ssh_host_alias:
        OpenSSHMaster(target).key


def _validate_remote_ownership(contexts: tuple[WorkspacePreflight, ...]) -> None:
    """Reject equal or nested roots on one resolved physical endpoint.

    Args:
        contexts: All resolved repositories before any Build or Lock.

    Returns:
        ``None`` when every remote ownership boundary is disjoint.
    """

    for index, left in enumerate(contexts):
        for right in contexts[index + 1 :]:
            left_target = left.target
            right_target = right.target
            left_endpoint = (
                left_target.protocol,
                left_target.host.lower() if left_target.host else None,
                left_target.username,
                left_target.port,
            )
            right_endpoint = (
                right_target.protocol,
                right_target.host.lower() if right_target.host else None,
                right_target.username,
                right_target.port,
            )
            if left_endpoint != right_endpoint:
                continue
            left_root = left_target.remote_root
            right_root = right_target.remote_root
            if (
                left_root == right_root
                or left_root in right_root.parents
                or right_root in left_root.parents
            ):
                endpoint = (
                    f"{left_target.username}@{left_target.host}:{left_target.port}"
                )
                raise ConfigError(
                    f"workspace repositories {left.repository.name!r} and "
                    f"{right.repository.name!r} manage overlapping remote roots: "
                    f"{endpoint}:{left_root} and {endpoint}:{right_root}"
                )


def _required_string(value: Any, name: str) -> str:
    """Require a non-empty workspace string."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()
